"""Versioned public cases, separately held labels, and grouped three-way splits."""
import hashlib
import json
import math
from pathlib import Path
import random
import re

SPLITS = ("search", "validation", "locked-test")
TASKS = ("short", "math", "essay", "lab")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def safe_file(root, relative):
    value = (root / relative).resolve()
    if not value.is_relative_to(root.resolve()) or not value.is_file():
        raise ValueError(f"样本文件必须位于数据集内且存在：{relative}")
    return value


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load(directory):
    root = Path(directory).resolve()
    manifest = read_json(root / "suite.json")
    for key in ("id", "task_type", "subject", "rubric_version", "judge_version", "input_mode"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise ValueError("suite.json 缺少 " + key)
    if manifest["task_type"] not in TASKS:
        raise ValueError("不支持的 suite 题型")
    cases = rows(root / "cases.jsonl")
    labels = rows(root / "labels.private.jsonl")
    if not cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("case id 必须非空且唯一")
    by_id = {x["id"]: x for x in labels}
    if len(by_id) != len(labels) or set(by_id) != {c["id"] for c in cases}:
        raise ValueError("隐藏标注须与所有 case id 一一对应")
    group_strata = {}
    asset_hashes = {}
    scales = set()
    for case in cases:
        if set(case) - {"id", "group_id", "stratum", "input"}:
            raise ValueError("案例只允许 id/group_id/stratum/input，教师标注必须分开保存")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", case["id"]) or case["id"] in (".", ".."):
            raise ValueError("case id 只能使用安全的字母数字名称")
        for key in ("group_id", "stratum"):
            if not isinstance(case.get(key), str) or not case[key]:
                raise ValueError("案例缺少 " + key)
        if case["group_id"] in group_strata and group_strata[case["group_id"]] != case["stratum"]:
            raise ValueError("同一相关组必须属于同一分层；不能把变体分到不同层")
        group_strata[case["group_id"]] = case["stratum"]
        inp = case["input"]
        if set(inp) - {"student_id", "student_name", "answer", "files", "task", "criteria", "rules"}:
            raise ValueError("input 包含未批准字段；不得把隐藏标注传给 agent")
        if not isinstance(inp.get("student_id"), str) or not inp["student_id"]:
            raise ValueError("每份输入必须有学生 ID")
        rules = inp.get("rules", [])
        if not rules or len({r["id"] for r in rules}) != len(rules) or len({r["name"] for r in rules}) != len(rules):
            raise ValueError("公开评分项须有唯一规则 ID")
        for rule in rules:
            if set(rule) != {"id", "name", "full", "reason_codes"}:
                raise ValueError("公开规则只允许 id/name/full/reason_codes")
            if not number(rule["full"]) or rule["full"] <= 0 or not rule["reason_codes"]:
                raise ValueError("规则满分和错误代码无效")
            if (not all(isinstance(rule[k], str) and rule[k].strip() for k in ("id", "name"))
                    or not isinstance(rule["reason_codes"], list)
                    or not all(isinstance(c, str) and c.strip() for c in rule["reason_codes"])):
                raise ValueError("规则 ID、名称及错误代码须是非空文本")
        full = sum(r["full"] for r in rules)
        scales.add(full)
        if manifest["task_type"] != "lab":
            from graders.assignments import TaskSpec
            if set(inp.get("task", {})) - set(TaskSpec.__dataclass_fields__):
                raise ValueError("task 中存在未批准字段")
            spec = TaskSpec.from_dict(inp.get("task", {}))
            if spec.task_type != manifest["task_type"] or spec.validate():
                raise ValueError("任务类型或题目设定无效")
            points, _ = spec.points()
            if [(r["name"], r["full"]) for r in rules] != [(p["name"], p["full"]) for p in points]:
                raise ValueError("公开规则必须与实际 TaskSpec 评分项完全一致，不能隐式缩放")
            if full != spec.full_score:
                raise ValueError("公开规则总和必须等于题目满分")
        elif full != 100 or not inp.get("criteria"):
            raise ValueError("实验报告当前入口使用 100 分制且必须提供评分细则")
        label = by_id[case["id"]]
        for key in ("tolerance", "dimension_tolerance"):
            if not number(label.get(key, 0)) or label.get(key, 0) < 0:
                raise ValueError("标注容差必须是非负有限数字")
        if not number(label.get("score")) or not 0 <= label["score"] <= full:
            raise ValueError("教师标注分数越界或无效")
        if not isinstance(label.get("review_required"), bool) or not isinstance(label.get("human_verified"), bool):
            raise ValueError("标注必须明确 review_required 和 human_verified")
        dims = label.get("dimensions", {})
        if set(dims) != {r["id"] for r in rules} or any(
                not number(dims[r["id"]]) or not 0 <= dims[r["id"]] <= r["full"] for r in rules):
            raise ValueError("教师分项标注必须完整有效")
        if abs(sum(dims.values()) - label["score"]) > 1e-6:
            raise ValueError("教师分项和与总分不一致")
        deductions = label.get("deductions", [])
        if not isinstance(deductions, list):
            raise ValueError("教师扣分标注格式无效")
        for d in deductions:
            rule = next((r for r in rules if r["id"] == d.get("rule_id")), None)
            if not rule or d.get("reason_code") not in rule["reason_codes"] or not isinstance(d.get("evidence_any"), list):
                raise ValueError("教师扣分标注的规则/原因/证据无效")
            if not d["evidence_any"] or not all(isinstance(q, str) for q in d["evidence_any"]):
                raise ValueError("证据须为文本列表，缺失内容用空字符串作为锚点")
        if len({d["rule_id"] for d in deductions}) != len(deductions):
            raise ValueError("每个评分项只能有一条教师扣分标注，可列出多个允许的证据锚点")
        if {d["rule_id"] for d in deductions} != {r["id"] for r in rules if dims[r["id"]] < r["full"]}:
            raise ValueError("每个教师扣分项必须有原因与证据标注")
        if not isinstance(inp.get("answer", ""), str):
            raise ValueError("answer 必须是原文字符串")
        for relative in inp.get("files", []):
            path = safe_file(root, relative)
            if path.suffix.lower() not in (".pdf", ".docx", ".txt", ".md", ".jpg", ".jpeg", ".png", ".bmp", ".webp"):
                raise ValueError("不支持的公开附件格式")
            asset_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if len(scales) != 1:
        raise ValueError("不同分值量表必须放在独立 suite 中，不能混合平均")
    return {"root": root, "manifest": manifest, "cases": cases, "labels": by_id,
            "fingerprint": digest({"manifest": manifest, "cases": cases, "labels": labels, "assets": asset_hashes})}


def make_split(suite, seed=42):
    groups = {}
    for case in suite["cases"]:
        groups.setdefault(case["stratum"], set()).add(case["group_id"])
    assignment = {}
    rng = random.Random(seed)
    counts = {s: 0 for s in SPLITS}
    for stratum, ids in sorted(groups.items()):
        ids = sorted(ids)
        rng.shuffle(ids)
        for group in ids:
            split = min(SPLITS, key=lambda s: counts[s])
            assignment[group] = split
            counts[split] += 1
    return {"dataset": suite["fingerprint"], "seed": seed, "group_assignments": assignment,
            "case_assignments": {c["id"]: assignment[c["group_id"]] for c in suite["cases"]},
            "groups_per_split": counts}


def check_split(suite, split):
    if split["dataset"] != suite["fingerprint"]:
        raise ValueError("数据或标注已变化，须重新划分并重跑所有比较版本")
    if set(split["case_assignments"]) != {c["id"] for c in suite["cases"]}:
        raise ValueError("划分必须覆盖全部案例且不可增加案例")
    seen = {}
    for case in suite["cases"]:
        part = split["case_assignments"][case["id"]]
        if part not in SPLITS or seen.get(case["group_id"], part) != part:
            raise ValueError("相关题目组跨集合泄漏或划分名称无效")
        seen[case["group_id"]] = part
    if seen != split["group_assignments"]:
        raise ValueError("案例划分与相关组划分不一致")
