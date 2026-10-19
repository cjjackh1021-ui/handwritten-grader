"""工具选择上的 GRPO（Group Relative Policy Optimization）。

与 DeepSeek-R1 / TRL GRPOTrainer 同一套更新式, 只是策略不是 7B LLM, 而是
「状态特征 → 下一步调用哪个工具」的小 softmax。用来证明:

1. 奖励函数能驱动策略学到「先看结构/读正文, 图多要看图, 最后提交」;
2. 组内相对优势 (r - mean(r)) / std(r) 比绝对奖励更稳;
3. 同一 env + 同一 reward 可无缝换成 vLLM+LoRA 的真 LLM-GRPO。

不依赖 PyTorch, CPU 数秒可跑完。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from posttrain.env import ACTION_NAMES, GradeEnv
from posttrain.rewards import score_rollout

N_ACTIONS = len(ACTION_NAMES)
N_FEATURES = 8  # 见 features()


def features(env: GradeEnv) -> list[float]:
    """紧凑状态: 是否已看结构/正文/图/标准、是否需要图、步数比例、是否刚被打回。"""
    used = {s.get("name") for s in env.trace if s.get("type") == "tool_call"}
    needs_visual = any(getattr(p, "needs_visual", False) for p in env.report.pages)
    reflected = any(s.get("type") == "reflect" for s in env.trace)
    return [
        1.0 if "get_report_info" in used else 0.0,
        1.0 if "read_text" in used else 0.0,
        1.0 if "view_pages" in used else 0.0,
        1.0 if "get_rubric" in used else 0.0,
        1.0 if needs_visual else 0.0,
        env.steps / max(env.max_steps, 1),
        1.0 if reflected else 0.0,
        1.0 if env.ctx and env.ctx.submitted else 0.0,
    ]


def softmax(logits: list[float]) -> list[float]:
    m = max(logits)
    exps = [math.exp(x - m) for x in logits]
    s = sum(exps) or 1.0
    return [e / s for e in exps]


@dataclass
class ToolPolicy:
    """线性策略 π(a|s) = softmax(W a · φ(s) + b_a)。"""

    W: list[list[float]]  # [n_actions][n_features]
    b: list[float]

    @classmethod
    def zeros(cls) -> "ToolPolicy":
        return cls(W=[[0.0] * N_FEATURES for _ in range(N_ACTIONS)], b=[0.0] * N_ACTIONS)

    def logits(self, phi: list[float]) -> list[float]:
        out = []
        for i in range(N_ACTIONS):
            out.append(sum(self.W[i][j] * phi[j] for j in range(N_FEATURES)) + self.b[i])
        return out

    def probs(self, phi: list[float]) -> list[float]:
        return softmax(self.logits(phi))

    def sample(self, phi: list[float], rng: random.Random) -> tuple[int, float]:
        p = self.probs(phi)
        r = rng.random()
        acc = 0.0
        idx = N_ACTIONS - 1
        for i, pi in enumerate(p):
            acc += pi
            if r <= acc:
                idx = i
                break
        logp = math.log(max(p[idx], 1e-12))
        return idx, logp

    def logprob(self, phi: list[float], action_idx: int) -> float:
        return math.log(max(self.probs(phi)[action_idx], 1e-12))

    def copy(self) -> "ToolPolicy":
        return ToolPolicy(W=[row[:] for row in self.W], b=self.b[:])


def group_advantages(rewards: list[float], eps: float = 1e-6) -> list[float]:
    """GRPO: A_i = (r_i - mean) / std。组内相对化, 不需要 critic。"""
    n = len(rewards)
    if n == 0:
        return []
    mean = sum(rewards) / n
    var = sum((r - mean) ** 2 for r in rewards) / n
    std = math.sqrt(var) + eps
    return [(r - mean) / std for r in rewards]


def grpo_loss(new_logps: list[float], old_logps: list[float], advantages: list[float],
              clip: float = 0.2) -> float:
    """动作序列级 GRPO 代理损失（越小越好, 已取负）。

    L = - mean_i min( ratio_i * A_i, clip(ratio_i) * A_i )
    ratio = exp(new - old)
    """
    if not advantages:
        return 0.0
    vals = []
    for lg_new, lg_old, adv in zip(new_logps, old_logps, advantages):
        ratio = math.exp(max(-20, min(20, lg_new - lg_old)))
        unclipped = ratio * adv
        clipped_ratio = min(1.0 + clip, max(1.0 - clip, ratio))
        clipped = clipped_ratio * adv
        vals.append(min(unclipped, clipped))
    return -sum(vals) / len(vals)


def rollout_once(env: GradeEnv, policy: ToolPolicy, rng: random.Random) -> dict:
    env.reset()
    phis, actions, logps = [], [], []
    total_r = 0.0
    while not env.done:
        phi = features(env)
        # 提交前若已有证据, 略微偏向 submit, 但仍由策略决定
        idx, logp = policy.sample(phi, rng)
        name = ACTION_NAMES[idx]
        step = env.step(name)
        phis.append(phi)
        actions.append(idx)
        logps.append(logp)
        total_r = step.reward if step.done else total_r
        if env.done:
            break
    return {
        "reward": total_r,
        "phis": phis,
        "actions": actions,
        "logps": logps,
        "trace": list(env.trace),
        "grade": env.grade,
        "n_steps": env.steps,
    }


def _dlog_softmax(probs: list[float], action_idx: int, phi: list[float]):
    """∂ log π(a|s) / ∂W[k][j] 与 ∂ / ∂b[k]。"""
    # d log p_a / d logit_k = 1[k=a] - p_k
    gW = [[0.0] * N_FEATURES for _ in range(N_ACTIONS)]
    gb = [0.0] * N_ACTIONS
    for k in range(N_ACTIONS):
        factor = (1.0 if k == action_idx else 0.0) - probs[k]
        gb[k] = factor
        for j in range(N_FEATURES):
            gW[k][j] = factor * phi[j]
    return gW, gb


def apply_grpo_update(policy: ToolPolicy, group: list[dict], lr: float = 0.08,
                      clip: float = 0.2) -> float:
    """用一组 rollouts 的相对优势更新策略, 返回代理损失。"""
    adv = group_advantages([g["reward"] for g in group])
    # 序列 logprob 之和
    old_seq = [sum(g["logps"]) for g in group]
    # 用当前策略重算 new logprob（on-policy 时与 old 接近）
    new_seq = []
    for g in group:
        s = 0.0
        for phi, a in zip(g["phis"], g["actions"]):
            s += policy.logprob(phi, a)
        new_seq.append(s)
    loss = grpo_loss(new_seq, old_seq, adv, clip=clip)

    # 策略梯度: 对每条轨迹, 把 A_i * ∇logπ 累加。clip 比率为 1 时退化为 REINFORCE。
    gW = [[0.0] * N_FEATURES for _ in range(N_ACTIONS)]
    gb = [0.0] * N_ACTIONS
    n = max(len(group), 1)
    for g, A, lg_new, lg_old in zip(group, adv, new_seq, old_seq):
        ratio = math.exp(max(-20, min(20, lg_new - lg_old)))
        # PPO-clip: 若已经出界且优势同号, 梯度为 0
        clipped = ratio < (1 - clip) or ratio > (1 + clip)
        if clipped and ((A > 0 and ratio > 1 + clip) or (A < 0 and ratio < 1 - clip)):
            continue
        for phi, a in zip(g["phis"], g["actions"]):
            probs = policy.probs(phi)
            dW, db = _dlog_softmax(probs, a, phi)
            for k in range(N_ACTIONS):
                gb[k] += A * db[k] / n
                for j in range(N_FEATURES):
                    gW[k][j] += A * dW[k][j] / n

    for k in range(N_ACTIONS):
        policy.b[k] += lr * gb[k]
        for j in range(N_FEATURES):
            policy.W[k][j] += lr * gW[k][j]
    return loss


def train_tool_grpo(items: list[dict], criteria: str, group_size: int = 4,
                    epochs: int = 25, lr: float = 0.1, seed: int = 0) -> dict:
    """在样例集上训练工具策略, 返回训练曲线与学到的行为统计。"""
    rng = random.Random(seed)
    policy = ToolPolicy.zeros()
    history = []
    for ep in range(epochs):
        ep_rewards = []
        losses = []
        for it in items:
            env = GradeEnv(it["report"], criteria, label=it["label"], max_steps=6)
            group = [rollout_once(env, policy, rng) for _ in range(group_size)]
            losses.append(apply_grpo_update(policy, group, lr=lr))
            ep_rewards.extend(g["reward"] for g in group)
        mean_r = sum(ep_rewards) / max(len(ep_rewards), 1)
        history.append({"epoch": ep, "mean_reward": round(mean_r, 4),
                        "loss": round(sum(losses) / max(len(losses), 1), 4)})

    # 评估贪心策略行为
    def greedy_rollout(it):
        env = GradeEnv(it["report"], criteria, label=it["label"], max_steps=6)
        env.reset()
        while not env.done:
            phi = features(env)
            probs = policy.probs(phi)
            used = {s.get("name") for s in env.trace if s.get("type") == "tool_call"}
            one_shot = {"get_report_info", "get_rubric", "check_similarity"}
            cand = [i for i, n in enumerate(ACTION_NAMES)
                    if n not in one_shot or n not in used]
            if not cand:
                cand = list(range(N_ACTIONS))
            idx = max(cand, key=lambda i: probs[i])
            env.step(ACTION_NAMES[idx])
        tools = [s.get("name") for s in env.trace if s.get("type") == "tool_call"]
        rb = score_rollout(env.grade, env.label, env.trace, report=env.report)
        return {"reward": rb.total, "tools": tools, "file": it["file"]}

    evals = [greedy_rollout(it) for it in items]
    return {
        "history": history,
        "final_mean_reward": history[-1]["mean_reward"] if history else 0,
        "first_mean_reward": history[0]["mean_reward"] if history else 0,
        "evals": evals,
        "policy_bias": policy.b,
        "action_names": ACTION_NAMES,
    }
