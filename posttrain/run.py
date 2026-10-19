"""后训练 / Agent RL 入口。

用法::

    python -m posttrain.run --self-test
    python -m posttrain.run collect --dataset eval/sample_dataset --out posttrain/runs
    python -m posttrain.run grpo --dataset eval/sample_dataset --epochs 20
    python -m posttrain.run from-agent --jobs output --dataset eval/sample_dataset
    python -m posttrain.run from-agent --live --dataset eval/sample_dataset --api-key sk-xxx
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from graders import config  # noqa: E402
from posttrain.env import GradeEnv  # noqa: E402
from posttrain.export import export_all, rollout_record  # noqa: E402
from posttrain.from_agent import collect_live, export_groups, load_saved_groups  # noqa: E402
from posttrain.grpo import ToolPolicy, rollout_once, train_tool_grpo  # noqa: E402


def _criteria(path: str) -> str:
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8")
    if config.DEFAULT_CRITERIA_FILE.is_file():
        return config.DEFAULT_CRITERIA_FILE.read_text(encoding="utf-8")
    return "总分100, 按完成质量给分。"


def _load_items(dataset: str):
    from eval import dataset as ds
    return ds.load_dataset(Path(dataset))


def cmd_collect(args) -> int:
    """用当前工具策略(或均匀随机)采集组内 rollouts, 导出 SFT/DPO/RFT。"""
    items = _load_items(args.dataset)
    criteria = _criteria(args.criteria)
    rng = random.Random(args.seed)
    policy = ToolPolicy.zeros()  # 均匀随机 = 探索采集
    groups = []
    for it in items:
        env = GradeEnv(it["report"], criteria, label=it["label"], max_steps=6)
        group_recs = []
        for _ in range(args.group_size):
            raw = rollout_once(env, policy, rng)
            rec = rollout_record(
                it["report"], criteria, raw["grade"], it["label"], raw["trace"],
                extra={"n_steps": raw["n_steps"]},
            )
            group_recs.append(rec)
        groups.append(group_recs)
        print(f"  {it['file']}: rewards="
              + ", ".join(f"{r['reward']['total']:.3f}" for r in group_recs))
    out = Path(args.out)
    counts = export_all(groups, out, rft_k=args.rft_k)
    print("已导出", counts, "→", out)
    return 0


def cmd_grpo(args) -> int:
    items = _load_items(args.dataset)
    criteria = _criteria(args.criteria)
    print(f"GRPO 训练: {len(items)} 份报告, group={args.group_size}, epochs={args.epochs}")
    result = train_tool_grpo(
        items, criteria, group_size=args.group_size,
        epochs=args.epochs, lr=args.lr, seed=args.seed,
    )
    print(f"mean reward: {result['first_mean_reward']} → {result['final_mean_reward']}")
    print("贪心策略行为:")
    for ev in result["evals"]:
        print(f"  {ev['file']}: R={ev['reward']:.3f}  tools={ev['tools']}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    serial = {
        "first_mean_reward": result["first_mean_reward"],
        "final_mean_reward": result["final_mean_reward"],
        "history": result["history"],
        "evals": result["evals"],
        "action_names": result["action_names"],
        "policy_bias": result["policy_bias"],
    }
    (out / "grpo_report.json").write_text(
        json.dumps(serial, ensure_ascii=False, indent=2), encoding="utf-8")
    md = (
        f"# 工具选择 GRPO\n\n"
        f"- 样本数: {len(items)}\n"
        f"- 组大小 G: {args.group_size}\n"
        f"- mean reward: **{result['first_mean_reward']} → {result['final_mean_reward']}**\n\n"
        "| file | reward | tools |\n| --- | --- | --- |\n"
        + "\n".join(
            f"| {e['file']} | {e['reward']:.3f} | {' → '.join(e['tools'])} |"
            for e in result["evals"]
        )
        + "\n"
    )
    (out / "grpo_report.md").write_text(md, encoding="utf-8")
    print("报告:", out / "grpo_report.md")
    return 0


def cmd_from_agent(args) -> int:
    """真实 agent 轨迹导出。--live 现场调用；否则读 --jobs 下的落盘轨迹。"""
    import os

    from graders import providers

    out = Path(args.out)
    if args.live:
        items = _load_items(args.dataset)
        criteria = _criteria(args.criteria)
        api_key = args.api_key or os.getenv(
            config.PROVIDERS.get(args.provider, {}).get("env_key", ""), "")
        if not api_key:
            print("现场采集需要 --api-key，或改用 --jobs 导出已保存轨迹")
            return 2
        choice = providers.ModelChoice(
            provider=args.provider, api_key=api_key, model=args.model,
            base_url=args.base_url, use_vision=not args.no_vision,
        )
        print(f"现场 agent: {len(items)} 份 × {args.repeats} 次")
        groups = collect_live(items, choice, criteria, repeats=args.repeats)
    else:
        if not args.jobs:
            print("请指定 --jobs <output 目录或 job 目录>，或加 --live")
            return 2
        groups = load_saved_groups(Path(args.jobs), dataset=args.dataset)
        print(f"已读取 {sum(len(g) for g in groups)} 条轨迹, {len(groups)} 组")
    for g in groups:
        if not g:
            continue
        rewards = ", ".join(f"{r['reward']['total']:.3f}" for r in g)
        print(f"  {g[0].get('file')}: {rewards}")
    counts = export_groups(groups, out, rft_k=args.rft_k)
    print("已导出", counts, "→", out)
    return 0


def cmd_selftest() -> int:
    from posttrain.grpo import grpo_loss, group_advantages
    from posttrain.rewards import score_rollout

    # 1) 奖励: 贴近标注 + 用对工具 > 乱打分
    class P:
        needs_visual = False
        page_num = 1

    class R:
        student_name = "测"
        source_file = "t.txt"
        page_count = 1
        pages = [P()]
        full_text = "hello"

    label = {"score": 90, "dimensions": [{"name": "实验目的与原理阐述", "score": 18, "full": 18}]}
    good_grade = {"score": 90, "comment": "完成充分", "reason": "ok",
                  "dimensions": [{"name": "实验目的与原理阐述", "score": 18, "full": 18}]}
    bad_grade = {"score": 50, "comment": "差", "reason": "x",
                 "dimensions": [{"name": "实验目的与原理阐述", "score": 50, "full": 18}]}
    good_trace = [
        {"type": "tool_call", "name": "get_report_info", "args": {}},
        {"type": "tool_call", "name": "read_text", "args": {}},
        {"type": "tool_call", "name": "submit_grade", "args": {}},
    ]
    bad_trace = [{"type": "tool_call", "name": "submit_grade", "args": {}}]
    rg = score_rollout(good_grade, label, good_trace, report=R())
    rb = score_rollout(bad_grade, label, bad_trace, report=R())
    assert rg.total > rb.total, (rg.total, rb.total)

    # 2) GRPO 优势: 相对化后均值为 0
    adv = group_advantages([1.0, 0.5, 0.0, 0.5])
    assert abs(sum(adv)) < 1e-6
    # 提高正优势样本的 logp, 代理损失应更小（更负）
    loss_hi = grpo_loss([1.0, 0.0], [0.0, 0.0], [1.0, -1.0])
    loss_lo = grpo_loss([-1.0, 0.0], [0.0, 0.0], [1.0, -1.0])
    assert loss_hi < loss_lo, (loss_hi, loss_lo)

    # 3) env 能跑完
    from eval.dataset import load_dataset
    items = load_dataset(Path(__file__).resolve().parent.parent / "eval" / "sample_dataset")
    env = GradeEnv(items[0]["report"], "标准", label=items[0]["label"], max_steps=5)
    env.reset()
    env.step("get_report_info")
    env.step("read_text")
    st = env.step("submit_grade")
    assert st.done and st.reward > 0, st

    print("self-test OK", {"good": round(rg.total, 3), "bad": round(rb.total, 3),
                           "submit_R": round(st.reward, 3)})

    # 4) 真实 agent 循环(打桩) → 落盘 → export_all
    _selftest_agent_export(items[:1])
    return 0


def _selftest_agent_export(items) -> None:
    import json
    import tempfile

    from graders import providers
    from posttrain.from_agent import load_saved_groups, write_job_rollouts

    state = {"n": 0}
    long_comment = "这是一条故意写得很长用来触发自校验失败的评语内容"

    def fake_chat(choice, messages, tools=None, **kwargs):
        state["n"] += 1
        phase = (state["n"] - 1) % 4
        if phase == 0:
            return {"role": "assistant", "content": "先看结构",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "get_report_info", "arguments": "{}"}}]}
        if phase == 1:
            return {"role": "assistant", "content": "读正文",
                    "tool_calls": [{"id": "c2", "type": "function",
                                    "function": {"name": "read_text", "arguments": "{}"}}]}
        dims = [{"name": "实验目的与原理阐述", "score": 18, "full": 18},
                {"name": "其余", "score": 74, "full": 82}]
        if phase == 2:
            args = {"score": 92, "comment": long_comment, "reason": "先交一版", "dimensions": dims}
        else:
            args = {"score": 92, "comment": "结构完整实验充分", "reason": "修正评语长度", "dimensions": dims}
        return {"role": "assistant", "content": "提交",
                "tool_calls": [{"id": "c3", "type": "function",
                                "function": {"name": "submit_grade",
                                             "arguments": json.dumps(args, ensure_ascii=False)}}]}

    providers.chat = fake_chat
    choice = providers.ModelChoice(provider="deepseek", api_key="stub", model="deepseek-chat", use_vision=False)
    groups = collect_live(items, choice, "评分标准", repeats=1)
    assert groups and groups[0][0]["messages"], "真实 agent 应带回 messages"
    assert any(m.get("role") == "tool" for m in groups[0][0]["messages"])
    assert groups[0][0]["source"] == "agent-live"

    report = items[0]["report"]
    entry = {
        "source_file": report.source_file,
        "student_name": report.student_name,
        "source_path": report.source_path,
        "status": "success",
        "score": groups[0][0]["grade"]["score"],
        "comment": groups[0][0]["grade"]["comment"],
        "reason": "",
        "dimensions": groups[0][0]["grade"]["dimensions"],
        "trace": groups[0][0]["trace"],
        "engine": "agent",
        "_messages": groups[0][0]["messages"],
        "_needs_visual": False,
    }
    with tempfile.TemporaryDirectory() as tmp:
        n = write_job_rollouts(tmp, [entry], "评分标准")
        assert n == 1 and "_messages" not in entry
        loaded = load_saved_groups(Path(tmp), dataset=str(_ROOT / "eval" / "sample_dataset"))
        counts = export_groups(loaded, Path(tmp) / "out", rft_k=1)
    assert counts["sft"] >= 1 and counts["rollouts"] >= 1
    assert counts["dpo"] >= 1, counts  # 同轨迹里失败提交 vs 修正提交
    print("agent-export OK", counts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="批改 Agent 后训练 / GRPO")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    def add_common(p):
        p.add_argument("--dataset", default=str(_ROOT / "eval" / "sample_dataset"))
        p.add_argument("--criteria", default="")
        p.add_argument("--out", default=str(_ROOT / "posttrain" / "runs"))
        p.add_argument("--group-size", type=int, default=4)
        p.add_argument("--seed", type=int, default=0)

    p1 = sub.add_parser("collect", help="采集轨迹并导出 SFT/DPO/RFT jsonl")
    add_common(p1)
    p1.add_argument("--rft-k", type=int, default=1)

    p2 = sub.add_parser("grpo", help="CPU 上训练工具选择 GRPO")
    add_common(p2)
    p2.add_argument("--epochs", type=int, default=20)
    p2.add_argument("--lr", type=float, default=0.12)

    p3 = sub.add_parser("from-agent", help="真实 agent 轨迹导出 SFT/DPO/RFT")
    p3.add_argument("--jobs", default="", help="output 目录、单个 job 目录，或 agent_rollouts.json")
    p3.add_argument("--dataset", default=str(_ROOT / "eval" / "sample_dataset"),
                    help="含 labels.json 的目录，用来重算结果奖励；没有标注也可导出")
    p3.add_argument("--out", default=str(_ROOT / "posttrain" / "runs" / "agent"))
    p3.add_argument("--rft-k", type=int, default=1)
    p3.add_argument("--live", action="store_true", help="现场调用 agent，而不是读落盘轨迹")
    p3.add_argument("--repeats", type=int, default=1, help="每份报告现场跑几次，>1 才能做组内 DPO")
    p3.add_argument("--provider", default="deepseek")
    p3.add_argument("--api-key", default="")
    p3.add_argument("--model", default="")
    p3.add_argument("--base-url", default="")
    p3.add_argument("--criteria", default="")
    p3.add_argument("--no-vision", action="store_true")

    args = ap.parse_args(argv)
    if args.self_test or args.cmd is None and getattr(args, "self_test", False):
        if args.self_test:
            return cmd_selftest()
    if args.cmd == "collect":
        return cmd_collect(args)
    if args.cmd == "grpo":
        return cmd_grpo(args)
    if args.cmd == "from-agent":
        return cmd_from_agent(args)
    if args.self_test:
        return cmd_selftest()
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
