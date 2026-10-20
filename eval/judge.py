"""Deterministic, teacher-annotated grading-quality judge; never an agent input."""
import copy
from statistics import mean
from .suites import digest, number


def judge(case, label, output):
    inp = case["input"]
    rules = {r["id"]: r for r in inp["rules"]}
    issues = []
    if not isinstance(output, dict):
        return {"accepted": False, "issues": ["invalid_output"], "score_error": None,
                "unsupported_deductions": 0, "deductions": 0, "review": False}
    review = output.get("review_required") is True or output.get("score_provisional") is True
    if "review_required" in output and not isinstance(output["review_required"], bool):
        issues.append("invalid_review_flag")
    if output.get("student_id") != inp["student_id"]:
        issues.append("wrong_student")
    if review != label["review_required"]:
        issues.append("missed_review" if label["review_required"] else "unnecessary_review")
    if review and not output.get("review_reason") and not output.get("warning"):
        issues.append("review_without_reason")
    score = output.get("raw_score", output.get("score"))
    full = sum(r["full"] for r in rules.values())
    valid_score = number(score) and 0 <= score <= full
    score_error = abs(score - label["score"]) if valid_score and not review and not label["review_required"] else None
    if not valid_score:
        issues.append("invalid_score")
    elif not review and not label["review_required"] and score_error > label.get("tolerance", 0):
        issues.append("wrong_score")
    dims = output.get("dimensions") or []
    by_name = {}
    for d in dims:
        if not isinstance(d, dict) or d.get("name") in by_name:
            issues.append("invalid_dimensions")
            continue
        by_name[d.get("name")] = d
    predicted_losses = set()
    if not review:
        if set(by_name) != {r["name"] for r in rules.values()}:
            issues.append("dimension_coverage")
        total = 0
        for rid, rule in rules.items():
            d = by_name.get(rule["name"], {})
            s = d.get("score")
            if not number(s) or not 0 <= s <= rule["full"] or d.get("full") != rule["full"]:
                issues.append("invalid_dimension_score")
                continue
            total += s
            if s < rule["full"]:
                predicted_losses.add(rid)
            if not label["review_required"] and abs(s - label["dimensions"][rid]) > label.get("dimension_tolerance", 0):
                issues.append("wrong_dimension_score")
        if valid_score and abs(total - score) > 1e-6:
            issues.append("score_sum_mismatch")
    claims = output.get("deductions", [])
    if not isinstance(claims, list):
        issues.append("invalid_deductions")
        claims = []
    supported = set()
    unsupported = 0
    claimed = set()
    for claim in claims:
        if not isinstance(claim, dict):
            unsupported += 1
            continue
        rid = claim.get("rule_id")
        quote = claim.get("evidence")
        truth = [d for d in label["deductions"] if d["rule_id"] == rid
                 and d["reason_code"] == claim.get("reason_code")]
        expected_quotes = [q for d in truth for q in d["evidence_any"]]
        # Teacher anchor matches are semantic labels; exact student quotations
        # are an additional anti-fabrication check for text-based submissions.
        quote_ok = isinstance(quote, str) and (quote in expected_quotes or
                   any(q and q in quote for q in expected_quotes))
        if inp.get("answer"):
            quote_ok = quote_ok and (not quote or quote in inp["answer"])
        if rid in claimed or rid not in predicted_losses or not truth or not quote_ok or not claim.get("reason"):
            unsupported += 1
        else:
            supported.add(rid)
        claimed.add(rid)
    missing = set()
    if not review:
        missing = predicted_losses - supported - claimed
        unsupported += len(missing)
        if unsupported or predicted_losses - supported:
            issues.append("unsupported_deduction")
    return {
        "accepted": not issues, "issues": sorted(set(issues)), "score_error": score_error,
        "unsupported_deductions": 0 if review else unsupported, "deductions": 0 if review else len(claims) + len(missing),
        "review": review, "review_expected": label["review_required"],
    }


def oracle(case, label):
    """A judge control only. Must never be used as a worker/model response."""
    return {
        "student_id": case["input"]["student_id"], "score": label["score"],
        "review_required": label["review_required"], "review_reason": "教师要求确认关键字迹" if label["review_required"] else "",
        "dimensions": [{"name": r["name"], "full": r["full"], "score": label["dimensions"][r["id"]]}
                       for r in case["input"]["rules"]],
        "deductions": [{"rule_id": d["rule_id"], "reason_code": d["reason_code"],
                        "evidence": d["evidence_any"][0] if d["evidence_any"] else "",
                        "reason": "教师标注的扣分原因"} for d in label["deductions"]],
    }


def audit(suite):
    results = []
    mutations_rejected = {n: 0 for n in ("all_full", "all_zero", "wrong_reason", "wrong_student", "always_review", "fabricated_quote")}
    before = digest({"cases": suite["cases"], "labels": suite["labels"]})
    for case in suite["cases"]:
        label = suite["labels"][case["id"]]
        gold = oracle(case, label)
        first = judge(case, label, gold)
        second = judge(case, label, copy.deepcopy(gold))
        results.append({"case_id": case["id"], "positive_passed": first["accepted"],
                        "stable": first == second})
        for mode in mutations_rejected:
            bad = copy.deepcopy(gold)
            if mode in ("all_full", "all_zero"):
                bad.update(review_required=False, review_reason="", deductions=[])
                for dim in bad["dimensions"]:
                    dim["score"] = dim["full"] if mode == "all_full" else 0
                bad["score"] = sum(d["score"] for d in bad["dimensions"])
            elif mode == "wrong_student":
                bad["student_id"] = "__different_student__"
            elif mode == "always_review":
                bad.update(review_required=True, review_reason="一律转人工")
            elif not bad["deductions"] or label["review_required"]:
                continue
            elif mode == "wrong_reason":
                bad["deductions"][0]["reason_code"] = "__incorrect_reason__"
            else:
                bad["deductions"][0]["evidence"] = "__not_in_student_answer__"
            mutations_rejected[mode] += not judge(case, label, bad)["accepted"]
    unchanged = before == digest({"cases": suite["cases"], "labels": suite["labels"]})
    # Require both required-review and clearly gradable controls.
    review_cases = sum(l["review_required"] for l in suite["labels"].values())
    healthy = (all(r["positive_passed"] and r["stable"] for r in results) and
               all(v > 0 for v in mutations_rejected.values()) and
               0 < review_cases < len(suite["cases"]) and unchanged)
    return {"healthy": healthy, "positive_controls": results, "negative_controls_rejected": mutations_rejected,
            "required_review_controls": review_cases, "inputs_unchanged": unchanged,
            "note": "确定性证据裁判的健康检查；不是实际模型准确率评测。"}


def summarize(records):
    n = len(records)
    completed = [r for r in records if r["status"] == "completed"]
    judged = [r for r in completed if isinstance(r.get("judgment"), dict)]
    errors = [r["judgment"]["score_error"] for r in judged if r["judgment"]["score_error"] is not None]
    claims = sum(r["judgment"]["deductions"] for r in judged)
    unsupported = sum(r["judgment"]["unsupported_deductions"] for r in judged)
    reviewed = sum(r["judgment"]["review"] for r in judged)
    expected_review = sum(r["review_expected"] for r in records)
    events = [e for r in records for e in r.get("calls", [])]
    usage_known = [e for e in events if isinstance(e.get("usage"), dict)
                   and any(number(e["usage"].get(k)) for k in ("prompt_tokens", "input_tokens"))
                   and any(number(e["usage"].get(k)) for k in ("completion_tokens", "output_tokens"))]
    def tokens(e, keys):
        return next((e["usage"][k] for k in keys if number(e["usage"].get(k))), 0)
    return {
        "requested": n, "completed": len(completed), "judged": len(judged),
        "failures": n-len(completed), "judge_errors": len(completed)-len(judged),
        "delivery_rate": len(completed)/n if n else None,
        "failure_rate": (n-len(completed))/n if n else None,
        "correct_delivery_rate": sum(r["judgment"]["accepted"] for r in judged)/n if n else None,
        "automatic_coverage": sum(not r["judgment"]["review"] for r in judged)/n if n else None,
        "review_rate": reviewed/n if n else None,
        "required_review_recall": sum(r["review_expected"] and r["judgment"]["review"] for r in judged)/expected_review if expected_review else None,
        "unnecessary_review_rate": sum(not r["review_expected"] and r["judgment"]["review"] for r in judged)/(n-expected_review) if n>expected_review else None,
        "score_mae_completed_clear": mean(errors) if errors else None,
        "score_measurements": len(errors),
        "unsupported_deductions": unsupported, "deductions": claims,
        "unsupported_deduction_rate": unsupported/claims if claims else None,
        "provider_attempts": len(events), "usage_known_attempts": len(usage_known),
        "known_input_tokens": sum(tokens(e, ("prompt_tokens", "input_tokens")) for e in usage_known),
        "known_output_tokens": sum(tokens(e, ("completion_tokens", "output_tokens")) for e in usage_known),
        "cost": None, "cost_note": "未配置价格；未知或被终止的请求用量不记为零。",
        "mean_end_to_end_seconds": mean(r["seconds"] for r in records) if n else None,
        "error_categories": {kind: sum(r["status"] == kind for r in records)
                             for kind in sorted({r["status"] for r in records if r["status"] != "completed"})},
    }
