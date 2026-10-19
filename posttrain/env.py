"""批改工具环境：把现有 agent_tools 包成 RL 可 step 的 env。

观测是工具返回的 JSON 字符串；动作是工具名 + 参数。``submit_grade`` 结束回合，
用 :func:`posttrain.rewards.score_rollout` 给终局奖励。中间步奖励为 0（稀疏奖励，
和 LLM-GRPO 的 outcome RL 一致）。

真正拿 LLM 做 GRPO 时：把本 env 的 ``reset/step`` 接到 vLLM/TRL 的 tool calling 循环即可。
本仓库的 CPU 演示用离散工具策略在同一份奖励上跑 GRPO，算法相同、策略更小。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from graders.agent import _normalize_submit, _verify
from graders.agent_tools import TOOLS, ToolContext, execute
from posttrain.rewards import score_rollout

ACTION_NAMES = [
    "get_report_info",
    "get_rubric",
    "read_text",
    "view_pages",
    "check_similarity",
    "submit_grade",
]


@dataclass
class StepResult:
    observation: str
    reward: float
    done: bool
    info: dict = field(default_factory=dict)


class GradeEnv:
    """单份报告的 tool-use 环境。"""

    def __init__(self, report, criteria: str, label: dict | None = None,
                 other_texts=None, max_steps: int = 8, use_vision: bool = False):
        self.report = report
        self.criteria = criteria
        self.label = label or {}
        self.other_texts = other_texts or []
        self.max_steps = max_steps
        self.use_vision = use_vision
        self.ctx: ToolContext | None = None
        self.trace: list[dict] = []
        self.steps = 0
        self.done = False
        self.grade: dict | None = None

    def reset(self) -> str:
        self.ctx = ToolContext(
            report=self.report, criteria=self.criteria,
            use_vision=self.use_vision, other_texts=self.other_texts,
        )
        self.trace = []
        self.steps = 0
        self.done = False
        self.grade = None
        return json.dumps({
            "event": "reset",
            "student": self.report.student_name,
            "page_count": self.report.page_count,
            "hint": "先 get_report_info, 再 read_text, 需要时 view_pages, 最后 submit_grade",
        }, ensure_ascii=False)

    def legal_actions(self) -> list[str]:
        if self.done:
            return []
        return list(ACTION_NAMES)

    def _default_args(self, name: str) -> dict:
        if name == "read_text":
            return {"max_chars": 4000}
        if name == "view_pages":
            nums = [p.page_num for p in self.report.pages if getattr(p, "needs_visual", False)]
            if not nums:
                nums = [1]
            return {"page_nums": nums[:4]}
        if name == "check_similarity":
            return {"threshold": 0.8}
        if name == "submit_grade":
            return self._draft_grade()
        return {}

    def _draft_grade(self) -> dict:
        """离散策略没有自然语言, 按「是否收集过证据」给靠近/远离标注的提交。

        这是把过程与结果连起来的关键：不会用工具 → 提交更偏离人工分 → 结果奖更低。
        接 LLM 时不会走这条, 模型自己生成 score/comment/dimensions。
        """
        label = self.label or {}
        true = float(label.get("score", 75))
        used = {s.get("name") for s in self.trace if s.get("type") == "tool_call"}
        evidence = sum(1 for k in ("get_report_info", "read_text", "view_pages") if k in used)
        # 证据越少, 偏离越大
        drift = {0: 18, 1: 10, 2: 5, 3: 2}.get(evidence, 2)
        needs_visual = any(getattr(p, "needs_visual", False) for p in self.report.pages)
        if needs_visual and "view_pages" not in used:
            drift += 8
        pred = max(0, min(100, round(true + (1 if evidence % 2 == 0 else -1) * drift)))
        dims = []
        true_dims = label.get("dimensions") or []
        if true_dims:
            scale = pred / max(true, 1)
            for d in true_dims:
                ds = round(float(d.get("score", 0)) * scale, 1)
                full = d.get("full")
                if full is not None:
                    ds = max(0, min(float(full), ds))
                dims.append({"name": d.get("name", ""), "score": ds, "full": full, "comment": ""})
            # 把舍入误差吃进最后一维, 保证维度和=总分, 通过自校验
            gap = pred - sum(float(d["score"]) for d in dims)
            dims[-1]["score"] = round(float(dims[-1]["score"]) + gap, 1)
            full = dims[-1].get("full")
            if full is not None:
                dims[-1]["score"] = max(0, min(float(full), float(dims[-1]["score"])))
        else:
            dims = [{"name": "总分", "score": pred, "full": 100, "comment": ""}]
        comment = "证据充分给分稳" if evidence >= 2 else "依据不足估分"
        return {
            "score": pred,
            "comment": comment,
            "reason": f"toy-policy evidence={evidence} drift={drift}",
            "dimensions": dims,
        }

    def step(self, action: str, args: dict | None = None) -> StepResult:
        if self.done:
            return StepResult("already done", 0.0, True, {"error": "done"})
        if action not in TOOLS:
            self.steps += 1
            self.trace.append({"type": "note", "text": f"非法动作 {action}"})
            return StepResult(json.dumps({"error": f"未知工具: {action}"}), 0.0, False, {})

        args = dict(args or self._default_args(action))
        self.trace.append({"type": "tool_call", "name": action, "args": args})
        result = execute(self.ctx, action, args)
        self.trace.append({"type": "tool_result", "name": action, "result": result})
        self.steps += 1

        reward = 0.0
        info: dict = {"action": action}
        if action == "submit_grade":
            class _C:
                use_vision = self.use_vision
            ok, issues, normalized = _verify(self.ctx, _C)
            if ok:
                self.grade = normalized
                self.done = True
                rb = score_rollout(normalized, self.label, self.trace, report=self.report)
                reward = rb.total
                info["reward_breakdown"] = rb.as_dict()
                info["grade"] = {"score": normalized["score"], "comment": normalized["comment"]}
                self.trace.append({"type": "final", "grade": info["grade"]})
            else:
                self.trace.append({"type": "reflect", "issues": issues})
                self.ctx.submitted = None
                result = {"received": False, "issues": issues}
                info["issues"] = issues
                if self.steps >= self.max_steps:
                    self.done = True
                    rb = score_rollout(None, self.label, self.trace, report=self.report)
                    reward = rb.total
                    info["reward_breakdown"] = rb.as_dict()

        if not self.done and self.steps >= self.max_steps:
            self.done = True
            grade = _normalize_submit(self.ctx.submitted) if self.ctx.submitted else None
            self.grade = grade
            rb = score_rollout(grade, self.label, self.trace, report=self.report)
            reward = rb.total
            info["reward_breakdown"] = rb.as_dict()
            info["truncated"] = True

        return StepResult(
            observation=json.dumps(result, ensure_ascii=False)[:2000],
            reward=reward,
            done=self.done,
            info=info,
        )
