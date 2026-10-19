"""Question geometry, review states and deterministic grading marks."""
from __future__ import annotations
from dataclasses import replace
import hashlib
import math
import re
from pathlib import Path

from .ocr import valid_box


def stable_id(*parts):
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def intersects(a, b, margin=0):
    return a[0] < b[2]+margin and a[2]+margin > b[0] and a[1] < b[3]+margin and a[3]+margin > b[1]


def evidence(report, pages=None):
    out = []
    for p in report.pages:
        if pages is not None and p.page_num not in pages: continue
        out.append({"page": p.page_num, "issues": p.ocr.get("issues", []),
                    "sources": [{"engine": r["engine"], "text": r.get("text", "")} for r in p.ocr.get("results", [])],
                    "blocks": [{**b, "id": f"p{p.page_num}:{b['id']}"} for b in p.ocr.get("blocks", [])]})
    return out


def prepare_question(q, raw, report, visual=False):
    q["id"] = stable_id(report.source_sha256, q["no"])
    blocks = {f"p{p.page_num}:{b['id']}": (p.page_num, b) for p in report.pages for b in p.ocr.get("blocks", [])}
    regions = []
    for role in ("question", "answer"):
        grouped = {}
        for bid in raw.get(f"{role}_block_ids", []) or []:
            if bid in blocks:
                page, block = blocks[bid]
                if valid_box(block["bbox"]): grouped.setdefault(page, []).append((bid, block["bbox"]))
        for page, items in grouped.items():
            regions.append({"page": page, "role": role, "bbox": union([b for _, b in items]),
                            "block_ids": [bid for bid, _ in items], "source": "ocr"})
    if visual and raw.get("location_confident") is True:
        for region in raw.get("regions", []) or []:
            if not isinstance(region, dict): continue
            page = region.get("page")
            if page in [p.page_num for p in report.pages] and valid_box(region.get("bbox")):
                if any(r["page"] == page and r["role"] == region.get("role") for r in regions): continue
                regions.append({"page": page, "bbox": list(map(float, region["bbox"])),
                                "role": "answer" if region.get("role") == "answer" else "question", "source": "vision"})
    q["regions"] = regions
    q["pages"] = sorted(set(q.get("pages", [])) | {r["page"] for r in regions})
    has_images = any(p.render_path for p in report.pages)
    state = raw.get("answer_state")
    if state not in ("present", "blank", "unreadable"):
        state = "unreadable" if has_images and not q["student_answer"].strip() else ("present" if q["student_answer"].strip() else "blank")
    confirmed = not any(p.needs_visual for p in report.pages) or (visual and raw.get("transcription_confirmed") is True)
    inconsistent = (state == "blank" and bool(q["student_answer"].strip())) or (state == "present" and not q["student_answer"].strip())
    q.update(answer_state=state, recognition_review=state == "unreadable" or not confirmed or inconsistent,
             position_review=has_images and not any(r["role"] == "answer" for r in regions), grading_review=False)
    q["review_reasons"] = []
    if q["recognition_review"]: q["review_reasons"].append("作答转写尚未确认，请对照原图")
    if q["position_review"]: q["review_reasons"].append("题目位置尚未确认")
    # Only explicit numbered stems are checked; numeric answers are never treated as question numbers.
    for region in regions:
        if region["role"] != "question" or not region.get("block_ids"): continue
        first = blocks[region["block_ids"][0]][1]["text"]
        match = re.match(r"^\s*(\d+(?:[（(]\d+[）)])?)[.、．]\s*", first)
        expected = re.sub(r"[（）]", lambda m: "(" if m[0]=="（" else ")", str(q["no"]))
        if match and match[1].replace("（","(").replace("）",")") != expected:
            q["position_review"] = True
            q["review_reasons"].append("OCR 题干编号与小题编号不一致")
    return q


def check_region_collisions(questions):
    ids = {}
    for i, q in enumerate(questions):
        identity = q.get("id")
        if identity and identity in ids:
            previous = ids[identity]
            q["id"] = stable_id(identity, q.get("regions"), i)
            for item in (q, previous):
                item["position_review"] = True
                item.setdefault("review_reasons", []).append("题号重复，请确认小题区域")
        elif identity:
            ids[identity] = q
    for i, q in enumerate(questions):
        for other in questions[i+1:]:
            for a in q.get("regions", []):
                for b in other.get("regions", []):
                    if a["page"] != b["page"] or a.get("role") != "answer" or b.get("role") != "answer": continue
                    box, ob = a["bbox"], b["bbox"]
                    intersection = max(0, min(box[2], ob[2])-max(box[0], ob[0]))*max(0, min(box[3], ob[3])-max(box[1], ob[1]))
                    minimum = min((box[2]-box[0])*(box[3]-box[1]), (ob[2]-ob[0])*(ob[3]-ob[1]))
                    if minimum and intersection/minimum > .35:
                        for item in (q, other):
                            item["position_review"] = True
                            item.setdefault("review_reasons", []).append("相邻小题作答区域重叠")


def mark_for(question):
    if any(question.get(k) for k in ("recognition_review", "position_review", "grading_review", "error", "warning")):
        return None
    score, full = question.get("score"), question.get("full", question.get("full_score"))
    if score is None or full is None: return None
    try: score, full = float(score), float(full)
    except (ValueError, TypeError): return None
    if not math.isfinite(score) or not math.isfinite(full) or full <= 0 or not 0 <= score <= full:
        return None
    if abs(score-full) < 1e-6: return "correct"
    return "partial" if score > 0 else "incorrect"


def crop_report(report, question):
    from PIL import Image
    pages = []
    for p in report.pages:
        regions = [r["bbox"] for r in question.get("regions", []) if r["page"] == p.page_num]
        if not regions or not p.render_path: continue
        box = union(regions)
        with Image.open(p.render_path) as image:
            w, h = image.size
            pixels = (max(0,int(box[0]*w)-8), max(0,int(box[1]*h)-8), min(w,math.ceil(box[2]*w)+8), min(h,math.ceil(box[3]*h)+8))
            dest = Path(p.render_path).parent / "question_crops" / f"{question['id']}-{p.page_num}-{stable_id(box)}.png"
            dest.parent.mkdir(parents=True, exist_ok=True)
            image.crop(pixels).save(dest)
        pages.append(replace(p, render_path=str(dest), needs_visual=True, text=question.get("student_answer", "")))
    # Missing geometry must not silently pass unrelated whole-page answers to a grader.
    return replace(report, pages=pages, page_count=len(pages), full_text=question.get("student_answer", ""))


def summarize(result):
    questions = result.get("details", {}).get("questions", [])
    for q in questions:
        q["mark"] = mark_for(q)
        q["review_required"] = q["mark"] is None
    result["review_required"] = result.get("status") == "error" or any(q["review_required"] for q in questions)
    result["score_provisional"] = result.get("status") == "error" or any(q.get("recognition_review") or q.get("grading_review") or q.get("error") or q.get("warning") for q in questions)
    result["document_state"] = "draft" if result["review_required"] else "final"
    return result


def single_question(result, report, spec):
    if not any(p.render_path for p in report.pages): return result
    raw = {"answer_state": result.get("answer_state", "present"),
           "transcription_confirmed": result.get("transcription_confirmed", False),
           "location_confident": True,
           "regions": [{"page": p.page_num, "role": "answer", "bbox": union([b["bbox"] for b in p.ocr.get("blocks", []) if valid_box(b.get("bbox"))])}
                       for p in report.pages if any(valid_box(b.get("bbox")) for b in p.ocr.get("blocks", []))]}
    answer = "" if raw["answer_state"] == "blank" else (result.get("student_transcript") or report.full_text)
    q = {"no": "1", "type": spec.task_type, "question": spec.question, "student_answer": answer,
         "pages": [p.page_num for p in report.pages], "key": "1", "printed_score": spec.full_score,
         "word_limit": spec.word_limit, "genre": spec.genre}
    prepare_question(q, raw, report, visual=True)
    q.update(score=result.get("raw_score", result["score"]), full=spec.full_score, comment=result["comment"],
             reason=result["reason"], details=dict(result["details"]), task_spec=spec.as_dict(), reference=spec.reference,
             warning=result.get("warning", ""), grading_review=bool(result.get("warning")))
    result["details"]["questions"] = [q]
    return summarize(result)


def find_placement(question, page, occupied):
    from PIL import Image, ImageOps
    if question.get("position_review"): return None
    regions = [r for r in question.get("regions", []) if r["page"] == page["page_num"]]
    if not regions: return None
    anchor = union([r["bbox"] for r in regions if r.get("role") == "answer"] or [r["bbox"] for r in regions])
    with Image.open(page["render_path"]) as image:
        w, h = image.size
        sw, sh = min(.09, 45/max(1, page.get("metadata", {}).get("width_pt", w*.48))), min(.07, 36/max(1,page.get("metadata", {}).get("height_pt", h*.48)))
        candidates = [question["placement"]["bbox"]] if question.get("placement", {}).get("manual") else [
            [x, y, x+sw, y+sh] for y in (max(.01, anchor[3]-sh), max(.01, anchor[1]))
            for x in (anchor[2]+.012, .985-sw, anchor[0]-sw-.012)]
        for box in candidates:
            if not valid_box(box) or any(intersects(box, other, .003) for other in occupied): continue
            crop = ImageOps.grayscale(image.crop((int(box[0]*w),int(box[1]*h),int(box[2]*w),int(box[3]*h))))
            hist = crop.histogram()
            if sum(hist[:165])/max(1,sum(hist)) > .025: continue
            return {"page": page["page_num"], "bbox": box, "manual": bool(question.get("placement", {}).get("manual"))}
    return None
