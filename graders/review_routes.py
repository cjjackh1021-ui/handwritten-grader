"""Preview and question review routes, scoped to persisted job artifacts."""
import io
from pathlib import Path
from flask import jsonify, request, send_file
from PIL import Image
from . import annotations, review


def register(app, jobs, lock, choice_from_form, finalize):
    def find(job_id, report_id):
        job = jobs.get(job_id)
        if not job: raise LookupError("任务不存在或已过期")
        item = next((r for r in job["reports"] if r.get("report_id") == report_id), None)
        if item is None: raise LookupError("作答不存在")
        return job, item

    @app.get("/review/<job_id>/<report_id>")
    def get_review(job_id, report_id):
        try:
            _, item = find(job_id, report_id)
            return jsonify(item)
        except LookupError as exc: return jsonify(error=str(exc)), 404

    @app.get("/preview/<job_id>/<report_id>/<int:page_number>")
    def preview(job_id, report_id, page_number):
        try:
            job, item = find(job_id, report_id)
            if request.args.get("marked") == "true":
                paths = item.get("marked_pngs", [])
                path = paths[page_number-1] if 0 < page_number <= len(paths) else ""
            else:
                page = next((p for p in item.get("pages", []) if p["page_num"] == page_number), {})
                path = page.get("render_path", "")
            if not path: raise LookupError("页面预览不可用")
            path = Path(path).resolve()
            if not path.is_relative_to(Path(job["job_dir"]).resolve()) or not path.is_file(): raise LookupError("页面不存在")
            qid = request.args.get("question")
            if qid:
                q = next((q for q in item.get("details", {}).get("questions", []) if q.get("id") == qid), {})
                boxes = [r["bbox"] for r in q.get("regions", []) if r["page"] == page_number]
                if not boxes: raise LookupError("此题没有该页区域")
                with Image.open(path) as image:
                    box = annotations.union(boxes)
                    image = image.crop(tuple(int(v*(image.width if i%2 == 0 else image.height)) for i,v in enumerate(box)))
                    data = io.BytesIO(); image.save(data, format="PNG"); data.seek(0)
                return send_file(data, mimetype="image/png", max_age=0)
            return send_file(path, mimetype="image/png", max_age=0)
        except LookupError as exc: return jsonify(error=str(exc)), 404

    @app.post("/review/<job_id>/<report_id>/<question_id>")
    def change_question(job_id, report_id, question_id):
        with lock(job_id):
            try:
                job, item = find(job_id, report_id)
                data = request.get_json(silent=True) or {}
                updated = review.edit_question(item, question_id, data, choice_from_form(data) if data.get("action") == "transcript" else None)
                reports = [updated if r is item else r for r in job["reports"]]
                final = finalize(job_id, Path(job["job_dir"]), reports, job.get("criteria", ""), job.get("options", {}), job.get("extract_errors", []))
                return jsonify(success=True, results=reports, **final)
            except LookupError as exc: return jsonify(error=str(exc)), 404
            except (ValueError, KeyError, TypeError) as exc: return jsonify(error=str(exc)), 400
            except Exception as exc: return jsonify(error=f"单题复核未完成，原结果已保留：{exc}"), 502
