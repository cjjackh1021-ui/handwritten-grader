"""Opt-in candidate policy shared by real grading entries and the eval runner.

This contains no teacher labels. Default grading behavior is unchanged.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import json

_policy = ContextVar("grading_candidate", default=None)
FIELDS = {"name", "prompt_addendum", "require_evidence"}


def validate(candidate):
    if not isinstance(candidate, dict) or set(candidate) - FIELDS:
        raise ValueError("候选版本仅允许 name、prompt_addendum、require_evidence")
    if not isinstance(candidate.get("name"), str) or not candidate["name"].strip():
        raise ValueError("候选版本必须有名称")
    if not isinstance(candidate.get("prompt_addendum", ""), str):
        raise ValueError("prompt_addendum 必须是文字")
    if not isinstance(candidate.get("require_evidence", False), bool):
        raise ValueError("require_evidence 必须是布尔值")
    if len(candidate.get("prompt_addendum", "")) > 12000:
        raise ValueError("候选提示过长")
    return copy.deepcopy(candidate)


@contextmanager
def activate(candidate, rules):
    candidate = validate(candidate)
    token = _policy.set({"candidate": candidate, "rules": copy.deepcopy(rules)})
    try:
        yield
    finally:
        _policy.reset(token)


def instructions():
    value = _policy.get()
    if not value:
        return ""
    candidate = value["candidate"]
    text = "\n" + candidate.get("prompt_addendum", "")
    if candidate.get("require_evidence"):
        text += (
            "\n评分细则仍是唯一的分值依据。每项扣分须在 deductions 数组中提供 "
            "{rule_id, evidence, reason_code, reason}；rule_id 取下面的公开评分项，"
            "evidence 必须引用学生原文，reason_code 用公开规则允许的错误代码，reason 简述原因。"
            "遗漏可用 evidence=\"\"、reason_code=\"missing\"，并明确指出遗漏什么。"
            "无法确认关键字迹或找不到扣分依据时设置 review_required=true 和 review_reason，"
            "清晰且能判定的答案应正常批改；满分时 deductions=[]。"
            "通过 submit_grade 工具提交时也使用这些字段。\n公开评分项："
            + json.dumps(value["rules"], ensure_ascii=False)
        )
    return text


def fields(raw):
    return {k: copy.deepcopy(raw[k]) for k in ("deductions", "review_required", "review_reason")
            if k in raw}


def finalize(result, raw, text):
    result = {**result, **fields(raw)}
    if result.get("review_required") is True:
        result.update(score_provisional=True, warning=result.get("warning") or
                      result.get("review_reason") or "评分待复核")
    value = _policy.get()
    if not value or not value["candidate"].get("require_evidence"):
        return result
    rules = {r["id"]: r for r in value["rules"]}
    deductions = raw.get("deductions", [])
    issues = []
    if not isinstance(deductions, list):
        deductions = []
        issues.append("扣分证据格式无效")
    covered = set()
    for item in deductions:
        if not isinstance(item, dict):
            issues.append("扣分证据格式无效")
            continue
        rule = rules.get(item.get("rule_id"))
        if rule is None or item.get("reason_code") not in rule.get("reason_codes", []):
            issues.append("扣分未关联有效评分项及错误代码")
            continue
        quote = item.get("evidence")
        if not isinstance(quote, str) or (quote and quote not in text):
            issues.append("扣分引用不是学生原文")
        elif not quote and item.get("reason_code") != "missing":
            issues.append("扣分缺少学生原文证据")
        elif not str(item.get("reason", "")).strip():
            issues.append("扣分缺少说明")
        else:
            covered.add(rule["id"])
    for dim in result.get("dimensions", []):
        if dim.get("score") is None or dim.get("full") is None:
            continue
        if float(dim["score"]) < float(dim["full"]):
            rule = next((r for r in rules.values() if r["name"] == dim.get("name")), None)
            if rule is None or rule["id"] not in covered:
                issues.append("扣分项缺少可追溯证据：" + str(dim.get("name")))
    if issues:
        result.update(review_required=True, review_reason="；".join(dict.fromkeys(issues)))
    if result.get("review_required") is True:
        result["score_provisional"] = True
        result["warning"] = result.get("warning") or result.get("review_reason") or "评分待复核"
    return result
