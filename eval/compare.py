"""Paired group bootstrap and bounded, evidence-preserving candidate selection."""
import difflib
import hashlib
from pathlib import Path
import random
import math
from statistics import mean

from . import judge
from .suites import digest, read_json, rows, write_json

DEFAULT_GUARDS = {
    "min_groups": 5, "min_improvement": 0.0, "max_coverage_drop": 0.0,
    "max_delivery_drop": 0.0, "max_correct_drop": 0.0, "max_score_error_increase": 0.0,
    "max_latency_ratio": 1.5, "max_token_ratio": 1.25,
}


def verified_run(directory):
    directory = Path(directory)
    report = read_json(directory / "report.json")
    path = directory / "records.jsonl"
    if hashlib.sha256(path.read_bytes()).hexdigest() != report["records_sha256"]:
        raise ValueError("运行明细已变更，不能沿用原汇总")
    records = rows(path)
    if judge.summarize(records) != report["metrics"]:
        raise ValueError("汇总无法从明细重新计算")
    return report, records


def paired_interval(values, seed=42, draws=2000):
    """values: group -> paired deltas, repeats are averaged within each case first."""
    groups = list(values)
    if not groups:
        return {"mean": None, "low": None, "high": None, "groups": 0}
    point = mean(v for g in groups for v in values[g])
    rng = random.Random(seed)
    estimates = []
    for _ in range(draws):
        chosen = rng.choices(groups, k=len(groups))
        estimates.append(mean(v for g in chosen for v in values[g]))
    estimates.sort()
    return {"mean": point, "low": estimates[int(draws*.025)],
            "high": estimates[min(draws-1, int(draws*.975))], "groups": len(groups)}


def values(record):
    j = record.get("judgment")
    completed = record["status"] == "completed"
    valid = completed and isinstance(j, dict)
    return {
        "unsupported": j["unsupported_deductions"]/max(1, j["deductions"]) if valid and not j["review"] else 0,
        "coverage": float(valid and not j["review"]),
        "delivery": float(completed),
        "correct": float(valid and j["accepted"]),
        # Product guard: failure/abstention on a clearly gradable answer cannot
        # disappear from the score-error denominator. This is NOT reported MAE.
        "penalized_score_error": 0 if record["review_expected"] else
            j["score_error"] if valid and j["score_error"] is not None else record["full_score"],
    }


def compare(baseline, candidate, out, guards=None, hypothesis="", history=None, max_rounds=5):
    guards = {**DEFAULT_GUARDS, **(guards or {})}
    if set(guards) != set(DEFAULT_GUARDS):
        raise ValueError("未知比较护栏")
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0 for v in guards.values()):
        raise ValueError("比较护栏必须是非负有限数字")
    if not isinstance(guards["min_groups"], int) or guards["min_groups"] < 2:
        raise ValueError("至少需要两个独立相关组才能估计配对不确定性")
    a, ar = verified_run(baseline)
    b, br = verified_run(candidate)
    for key in ("dataset", "suite", "split_digest", "implementation", "partition", "repeats", "options", "mode", "fixture_hash", "timeout"):
        if a[key] != b[key]:
            raise ValueError("版本不可配对比较：" + key)
    keyed = lambda rs: {(r["case_id"], r["repeat"]): r for r in rs}
    aa, bb = keyed(ar), keyed(br)
    if not aa or len(aa) != len(ar) or len(bb) != len(br) or set(aa) != set(bb):
        raise ValueError("两版本须有完全相同且不重复的案例/重复编号，不能丢弃失败请求")
    versions = lambda rs: {e["actual_model"] for r in rs for e in r.get("calls", []) if e.get("actual_model")}
    if versions(ar) and versions(br) and versions(ar) != versions(br):
        raise ValueError("服务返回的实际模型版本不同，须在一致版本上重新配对评测")
    per_case = {}
    for key in aa:
        x, y = aa[key], bb[key]
        if x["group_id"] != y["group_id"] or x["review_expected"] != y["review_expected"]:
            raise ValueError("配对组或教师复核标签不一致")
        vx, vy = values(x), values(y)
        row = per_case.setdefault(key[0], {"group": x["group_id"], "deltas": {m: [] for m in vx}})
        for metric in vx:
            row["deltas"][metric].append(vy[metric]-vx[metric])
    intervals = {}
    for metric in ("unsupported", "coverage", "delivery", "correct", "penalized_score_error"):
        groups = {}
        for case in per_case.values():
            groups.setdefault(case["group"], []).append(mean(case["deltas"][metric]))
        intervals[metric] = paired_interval(groups)
    enough = (a["mode"] == "live" and a["human_verified"] and b["human_verified"]
              and all(n >= guards["min_groups"] for n in a["groups_per_split"].values()))
    gate = {
        "evidence_improved": intervals["unsupported"]["high"] is not None and intervals["unsupported"]["high"] < -guards["min_improvement"],
        "coverage": intervals["coverage"]["low"] is not None and intervals["coverage"]["low"] >= -guards["max_coverage_drop"],
        "delivery": intervals["delivery"]["low"] is not None and intervals["delivery"]["low"] >= -guards["max_delivery_drop"],
        "correct_delivery": intervals["correct"]["low"] is not None and intervals["correct"]["low"] >= -guards["max_correct_drop"],
        "score_error": intervals["penalized_score_error"]["high"] is not None and intervals["penalized_score_error"]["high"] <= guards["max_score_error_increase"],
        "judge_health": a["metrics"]["judge_errors"] == b["metrics"]["judge_errors"] == 0,
        "latency": b["metrics"]["mean_end_to_end_seconds"] <= a["metrics"]["mean_end_to_end_seconds"] * guards["max_latency_ratio"],
    }
    am, bm = a["metrics"], b["metrics"]
    usage_complete = all(m["provider_attempts"] > 0 and m["usage_known_attempts"] == m["provider_attempts"] for m in (am, bm))
    at = am["known_input_tokens"] + am["known_output_tokens"]
    bt = bm["known_input_tokens"] + bm["known_output_tokens"]
    gate["tokens"] = usage_complete and bt <= at * guards["max_token_ratio"]
    adopt = enough and a["partition"] in ("validation", "locked-test") and all(gate.values())
    result = {
        "dataset": a["dataset"], "implementation": a["implementation"], "options": a["options"],
        "split_digest": a["split_digest"], "repeats": a["repeats"], "partition": a["partition"], "timeout": a["timeout"],
        "candidate_hashes": [a["candidate_hash"], b["candidate_hash"]],
        "baseline": str(Path(baseline).resolve()), "candidate": str(Path(candidate).resolve()),
        "hypothesis": hypothesis, "guards": guards, "paired_intervals": intervals,
        "gates": gate, "formal_sample_requirements_met": enough, "adopt_candidate": adopt,
        "decision": "可在冻结版本上进行最终确认" if adopt and a["partition"] == "validation" else
                    "最终确认通过" if adopt else "保持基线；样本不足、效果未超出波动或护栏未通过",
        "note": "差值为候选减基线；按相关题目组重采样，重复调用先按案例求均值。不会自动修改产品默认配置。",
    }
    if history:
        if not hypothesis.strip() or not 1 <= max_rounds <= 20:
            raise ValueError("迭代记录需要具体假设，且最多 1..20 轮")
        p = Path(history)
        journal = read_json(p) if p.exists() else {"contract": digest({
            "dataset": a["dataset"], "guards": guards, "implementation": a["implementation"],
            "baseline": a["candidate_hash"]}), "rounds": []}
        contract = digest({"dataset": a["dataset"], "guards": guards,
                           "implementation": a["implementation"], "baseline": a["candidate_hash"]})
        if journal["contract"] != contract:
            raise ValueError("优化中不能修改教师标注、裁判、护栏、实现或基线；须建立新实验并重跑")
        if len(journal["rounds"]) >= max_rounds or (len(journal["rounds"]) >= 3 and
                not any(r["adopt_candidate"] for r in journal["rounds"][-3:])):
            raise ValueError("已达到轮次上限或连续三轮无可采纳改进，停止自动比较")
        journal["rounds"].append(result)
        write_json(p, journal)
    out = Path(out)
    write_json(out, result)
    import json
    patch = "".join(difflib.unified_diff(
        json.dumps(a["candidate"], ensure_ascii=False, indent=2).splitlines(True),
        json.dumps(b["candidate"], ensure_ascii=False, indent=2).splitlines(True),
        fromfile="baseline-policy.json", tofile="candidate-policy.json"))
    out.with_suffix(".patch").write_text(patch, encoding="utf-8")
    return result
