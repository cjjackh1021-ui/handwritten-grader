"""Validated edits to one question; location edits never call a grading model."""
from __future__ import annotations
import copy
import math
import time
from dataclasses import replace
from . import annotations, assignments, autodetect, extract


def aggregate(entry):
    questions = entry.get("details", {}).get("questions", [])
    if not questions: return entry
    total = sum(float(q.get("score", 0)) for q in questions)
    if not entry.get("manual_total_adjustment"): entry["score"] = total
    entry["full_score"] = sum(float(q["full"]) for q in questions)
    entry["dimensions"] = [{"name": f"第{q['no']}题", "score": q["score"], "full": q["full"], "comment": q.get("comment", "")} for q in questions]
    entry["dimension_summary"] = "; ".join(f"{q['no']}:{q['score']:g}/{q['full']:g}" for q in questions)
    entry["reason"] = entry["score_reason"] = entry["dimension_summary"]
    annotations.summarize(entry)
    entry["comment"] = "部分题目待复核" if entry["score_provisional"] else autodetect._summary_comment(questions, total, entry["full_score"])
    return entry


def valid_score(value, full):
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= float(full): raise ValueError("分数须在 0 至满分之间")
    return score


def edit_question(entry, question_id, payload, choice=None):
    entry = copy.deepcopy(entry)
    questions = entry.get("details", {}).get("questions", [])
    q = next((q for q in questions if q.get("id") == question_id), None)
    if q is None: raise ValueError("题目不存在；旧任务需重新批改以获得逐题信息")
    before = copy.deepcopy(q)
    action = payload.get("action")
    if action == "score":
        q["score"] = valid_score(payload["score"], q["full"])
        q.update(grading_review=False, graded_by="human", comment="人工确认分数")
        q.pop("error", None); q.pop("warning", None)
        if payload.get("confirm_answer") is True:
            if q.get("answer_state") == "unreadable": raise ValueError("请先修正无法识别的作答")
            q["recognition_review"] = False
        entry.pop("manual_total_adjustment", None)
    elif action == "location":
        pages = {p["page_num"]: p for p in entry.get("pages", [])}
        regions = payload.get("regions", q.get("regions", []))
        if not isinstance(regions, list) or not regions: raise ValueError("至少需要一个题目区域")
        for r in regions:
            if not isinstance(r, dict) or r.get("page") not in pages or not annotations.valid_box(r.get("bbox")):
                raise ValueError("区域坐标超出页面或页码无效")
            if r.get("role") not in ("question", "answer"): raise ValueError("区域类型无效")
        q["regions"] = [{**r, "source": "human"} for r in regions]
        q["pages"] = sorted({r["page"] for r in regions})
        q["position_review"] = False
        annotations.check_region_collisions(questions)
        if q["position_review"]: raise ValueError("作答区域与其他小题重叠，请缩小区域")
        if "placement" in payload:
            placement = payload["placement"]
            last_page = max(q["pages"])
            if placement.get("page") != last_page or not annotations.valid_box(placement.get("bbox")):
                raise ValueError("标记须位于此题最后一页，且不能超出页面")
            q["placement"] = {**placement, "manual": True}
            occupied = [r["bbox"] for other in questions for r in other.get("regions", []) if r["page"] == last_page]
            occupied.extend(b["bbox"] for b in pages[last_page].get("ocr", {}).get("blocks", []) if annotations.valid_box(b.get("bbox")))
            occupied.extend(other["placement"]["bbox"] for other in questions if other is not q and other.get("placement", {}).get("page") == last_page)
            if not annotations.find_placement(q, pages[last_page], occupied): raise ValueError("标记位置可能覆盖原文或其他标记，请移至空白处")
    elif action == "transcript":
        if choice is None or not choice.api_key: raise ValueError("文字修正后重批需要提供当前批改模型的 API Key")
        state = payload.get("answer_state", "present")
        text = str(payload.get("student_answer", "")).strip()
        if state not in ("present", "blank") or (state == "present" and not text) or (state == "blank" and text):
            raise ValueError("请明确区分确认空白与有作答，并填写一致的文字")
        if not entry.get("report_snapshot"): raise ValueError("旧任务缺少页面快照，请重新批改")
        q.update(student_answer=text, answer_state=state, recognition_review=False, grading_review=False)
        report = extract.report_from_dict(entry["report_snapshot"])
        # Teacher transcription is authoritative; it must not be silently rewritten from pixels.
        choice = replace(choice, use_vision=False)
        if q.get("_question"):
            original = {**q["_question"], **{k: q[k] for k in ("student_answer", "answer_state", "recognition_review", "grading_review", "position_review", "regions", "pages")}}
            ctx = autodetect.AutoContext(choice=choice, lab_grader=lambda *_: None)
            ref = q.get("_reference", {})
            if not ref.get("answer") and text: ref = ctx.references([original], report, [])[0]
            updated = autodetect.grade_question(ctx, original, ref, report)
        else:
            spec = assignments.TaskSpec.from_dict(q["task_spec"])
            sub = replace(annotations.crop_report(report, q), full_text=text)
            if state == "blank":
                updated = dict(score=0.0, comment="确认未作答", reason="教师确认空白", grading_review=False)
            else:
                result = assignments.grade_submission(choice, spec, sub)
                updated = {k: result[k] for k in ("comment", "reason", "details", "dimensions") if k in result}
                updated.update(score=result.get("raw_score", result["score"]), grading_review=bool(result.get("warning")))
                if result.get("warning"): updated["warning"] = result["warning"]
        q.pop("error", None); q.pop("warning", None)
        q.update(updated)
        q["recognition_review"] = False
        entry.pop("manual_total_adjustment", None)
    else:
        raise ValueError("未知复核操作")
    q["review_reasons"] = (["作答转写尚未确认"] if q.get("recognition_review") else []) + (["题目或标记位置待确认"] if q.get("position_review") else []) + (["评分待确认"] if q.get("grading_review") or q.get("warning") or q.get("error") else [])
    entry.setdefault("review_history", []).append({"time": time.time(), "question_id": question_id, "action": action,
                                                   "before": {k: before.get(k) for k in ("score", "student_answer", "regions", "placement")}})
    return aggregate(entry)
