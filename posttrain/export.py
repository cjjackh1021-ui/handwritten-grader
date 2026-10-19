"""轨迹 → 后训练语料：SFT / DPO / RFT。

从 agent ``trace`` 还原 tool-calling 对话, 供:
- SFT: 高奖励完整轨迹（模仿「会用工具的批改员」）
- RFT: 每题保留组内奖励最高的 K 条（rejection fine-tuning）
- DPO: 同题 chosen=高奖励 / rejected=低奖励, 或同轨迹里「校验失败提交 vs 修正后提交」

导出格式兼容常见 OpenAI messages jsonl, 可直接喂给 Axolotl / LLaMA-Factory / TRL SFTTrainer。
"""

from __future__ import annotations

import json
from pathlib import Path

from graders.agent import SYSTEM_PROMPT, _initial_user
from posttrain.rewards import score_rollout


def trace_to_messages(report, criteria: str, trace: list[dict]) -> list[dict]:
    """把执行轨迹压成一份 tool-calling 对话（供 SFT）。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _initial_user(report) + f"\n\n评分标准：\n{criteria[:1500]}"},
    ]
    pending_calls = []
    pending_results = []

    def flush_turn():
        nonlocal pending_calls, pending_results
        if not pending_calls:
            pending_results = []
            return
        tool_calls = []
        for i, (name, args) in enumerate(pending_calls):
            tool_calls.append({
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
        messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
        for i, (name, result) in enumerate(pending_results):
            messages.append({
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "name": name,
                "content": json.dumps(result, ensure_ascii=False)[:4000],
            })
        pending_calls, pending_results = [], []

    for step in trace or []:
        t = step.get("type")
        if t == "tool_call":
            pending_calls.append((step.get("name", ""), step.get("args") or {}))
        elif t == "tool_result":
            pending_results.append((step.get("name", ""), step.get("result") or {}))
            if len(pending_results) >= len(pending_calls):
                flush_turn()
        elif t == "reflect":
            flush_turn()
            messages.append({"role": "user", "content": f"你的提交未通过自校验：{step.get('issues','')}。请修正后重新调用 submit_grade。"})
        elif t == "thought":
            # 思考文本并入下一轮 assistant; 这里单独记一条, SFT 可学「先想后做」
            if step.get("text"):
                flush_turn()
                messages.append({"role": "assistant", "content": step["text"]})
        elif t == "final":
            flush_turn()
    flush_turn()
    return messages


def rollout_record(report, criteria: str, grade: dict | None, label: dict | None,
                   trace: list[dict], extra: dict | None = None,
                   messages: list[dict] | None = None) -> dict:
    rb = score_rollout(grade, label, trace, report=report)
    rec = {
        "student": getattr(report, "student_name", ""),
        "file": getattr(report, "source_file", ""),
        "label": label or {},
        "grade": {
            "score": (grade or {}).get("score"),
            "comment": (grade or {}).get("comment", ""),
            "dimensions": (grade or {}).get("dimensions") or [],
        } if grade else None,
        "reward": rb.as_dict(),
        "trace": trace,
        "messages": messages if messages else trace_to_messages(report, criteria, trace),
        "source": "trace",
    }
    if extra:
        rec.update(extra)
    return rec


def dpo_pairs_from_group(records: list[dict], min_gap: float = 0.05) -> list[dict]:
    """同一 prompt 下, 最高奖励 vs 最低奖励组成 DPO 对。"""
    if len(records) < 2:
        return []
    ranked = sorted(records, key=lambda r: r["reward"]["total"], reverse=True)
    best, worst = ranked[0], ranked[-1]
    if best["reward"]["total"] - worst["reward"]["total"] < min_gap:
        return []
    prompt = [m for m in best["messages"] if m["role"] in ("system", "user")][:2]
    return [{
        "prompt": prompt,
        "chosen": best["messages"],
        "rejected": worst["messages"],
        "chosen_reward": best["reward"]["total"],
        "rejected_reward": worst["reward"]["total"],
        "file": best.get("file", ""),
    }]


def dpo_pairs_from_reflect(record: dict) -> list[dict]:
    """同一轨迹里, 校验失败的 submit vs 最终成功 submit → 天然 preference。"""
    trace = record.get("trace") or []
    submits = []
    for i, s in enumerate(trace):
        if s.get("type") != "tool_call" or s.get("name") != "submit_grade":
            continue
        window = trace[i + 1:i + 3]
        is_fail = any(x.get("type") == "reflect" for x in window)
        submits.append({"args": s.get("args") or {}, "failed": is_fail})
    failed_args = [s["args"] for s in submits if s["failed"]]
    ok_args = [s["args"] for s in submits if not s["failed"]]
    if not failed_args or not ok_args:
        return []
    prompt = [m for m in record.get("messages", []) if m["role"] in ("system", "user")][:2]
    def as_msg(args):
        return [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_0", "type": "function",
            "function": {"name": "submit_grade", "arguments": json.dumps(args, ensure_ascii=False)},
        }]}]
    return [{
        "prompt": prompt,
        "chosen": as_msg(ok_args[-1]),
        "rejected": as_msg(failed_args[0]),
        "source": "self-verify-reflect",
        "file": record.get("file", ""),
    }]


def rft_keep(groups: list[list[dict]], top_k: int = 1) -> list[dict]:
    """Rejection fine-tuning: 每组只留奖励最高的 top_k 条。"""
    kept = []
    for g in groups:
        ranked = sorted(g, key=lambda r: r["reward"]["total"], reverse=True)
        kept.extend(ranked[:top_k])
    return kept


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def export_all(groups: list[list[dict]], out_dir: Path, rft_k: int = 1) -> dict:
    """写出 sft.jsonl / rft.jsonl / dpo.jsonl, 返回计数。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    flat = [r for g in groups for r in g]
    sft = [{"messages": r["messages"], "reward": r["reward"]["total"], "file": r.get("file")}
           for r in flat if r.get("messages")]
    rft_rows = rft_keep(groups, top_k=rft_k)
    rft = [{"messages": r["messages"], "reward": r["reward"]["total"], "file": r.get("file")}
           for r in rft_rows]
    dpo = []
    for g in groups:
        dpo.extend(dpo_pairs_from_group(g))
    for r in flat:
        dpo.extend(dpo_pairs_from_reflect(r))
    write_jsonl(out_dir / "sft.jsonl", sft)
    write_jsonl(out_dir / "rft.jsonl", rft)
    write_jsonl(out_dir / "dpo.jsonl", dpo)
    write_jsonl(out_dir / "rollouts.jsonl", flat)
    return {"sft": len(sft), "rft": len(rft), "dpo": len(dpo), "rollouts": len(flat)}
