"""实验报告智能批改系统 - Flask 后端。

特性:
- 网页端选择 provider / 模型 / 填 API Key(密码框, 不落盘) 实时切换模型。
- 上传文件夹 (PDF/DOCX/TXT/MD), SSE 逐份进度。
- 图片密集页多模态视觉评分(自动压缩), 失败回退 OCR/文本。
- 限流/瞬时故障指数退避重试; 支持只重跑失败项。
- 分维度评分; 产出结果表 + 折线图、学生分组 Excel、盖分 PDF 的 ZIP。
- 任务落盘持久化(进程重启后仍可下载), 启动清理超期产物。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import traceback
import uuid
import threading
from functools import wraps
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file
from flask_cors import CORS
from graders import (
    agent,
    agent_framework,
    assignments,
    autodetect,
    config,
    convert,
    extract,
    grading,
    multi_agent,
    providers,
    summary,
    stamp,
    annotations, ocr, local_workers,
)

VALID_ENGINES = {"simple", "agent", "multi", "framework"}

load_dotenv()
config.ensure_dirs()

app = Flask(__name__)
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB

# 内存中的批改任务 (job_id -> 状态)。启动时从磁盘 job.json 恢复。
JOBS: dict[str, dict] = {}
JOB_LOCKS = {}


def job_lock(job_id):
    return JOB_LOCKS.setdefault(job_id, threading.RLock())


def serialized_job(function):
    @wraps(function)
    def wrapped(job_id, *args, **kwargs):
        with job_lock(job_id):
            return function(job_id, *args, **kwargs)
    return wrapped

MAX_WORKERS = int(os.getenv("GRADE_WORKERS", "3"))
JOB_TTL_DAYS = float(os.getenv("JOB_TTL_DAYS", "7"))


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_part(part: str) -> str:
    # 不用 secure_filename: 它会把「张三」这类中文目录名清空, 学生子文件夹就分不开了
    return _UNSAFE_CHARS.sub("_", part).strip(" .") or "f"


def safe_relpath(filename: str) -> Path:
    """保留 webkitdirectory 的相对子目录结构, 逐段做安全化。"""
    parts = [p for p in filename.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return Path(*[_safe_part(p) for p in parts or ["file"]])


def choice_from_form(form) -> providers.ModelChoice:
    provider = form.get("provider", "deepseek")
    api_key = form.get("api_key", "").strip()
    if not api_key:
        preset = config.PROVIDERS.get(provider, {})
        api_key = os.getenv(preset.get("env_key", ""), "") if preset else ""
    return providers.ModelChoice(
        provider=provider,
        api_key=api_key,
        model=form.get("model", "").strip(),
        base_url=form.get("base_url", "").strip(),
        use_vision=form.get("use_vision", "true").lower() != "false",
    )


# ---------------------------------------------------------------------------
# 持久化 & 清理
# ---------------------------------------------------------------------------
def save_job_meta(job_id: str) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    meta = {k: v for k, v in job.items() if k != "_reports_obj"}
    meta["job_id"] = job_id
    try:
        temporary = Path(job["job_dir"], "job.json.tmp")
        temporary.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(Path(job["job_dir"], "job.json"))
    except Exception:
        app.logger.exception("任务元数据写入失败: %s", job_id)


def load_jobs_from_disk() -> None:
    if not config.OUTPUT_DIR.is_dir():
        return
    for meta_path in config.OUTPUT_DIR.glob("*/job.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            jid = meta.get("job_id") or meta_path.parent.name
            JOBS[jid] = meta
        except Exception:
            continue


def cleanup_old_jobs() -> None:
    if not config.OUTPUT_DIR.is_dir() or JOB_TTL_DAYS <= 0:
        return
    cutoff = time.time() - JOB_TTL_DAYS * 86400
    for child in config.OUTPUT_DIR.iterdir():
        try:
            if child.is_dir() and child.name != "_work" and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                JOBS.pop(child.name, None)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# 提取阶段
# ---------------------------------------------------------------------------
def _load_one(path: Path, work_dir: Path, staging: Path, ocr_enabled: bool, render_vision: bool, ocr_mode=None):
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract.inspect_pdf(path, work_dir, ocr_enabled, render_vision, ocr_mode)
    if ext in (".txt", ".md"):
        return extract.inspect_text_file(path)
    if ext in extract.IMAGE_EXT:
        return extract.inspect_image(path, work_dir, ocr_enabled, ocr_mode)
    conv = convert.convert_docx(path, staging)
    if conv.pdf_path:
        info = extract.inspect_pdf(Path(conv.pdf_path), work_dir, ocr_enabled, render_vision, ocr_mode)
        info.origin_path = conv.source_path
        info.origin_sha256 = conv.source_sha256
        return info
    if conv.text is not None:
        return extract.report_from_text(path, conv.text, conv.source_sha256)
    raise ValueError(conv.error or "DOCX 无法处理")


def load_reports(job_dir: Path, ocr_enabled: bool, render_vision: bool, assignment_mode: bool = False,
                 answer_keys: list | None = None, ocr_mode=None):
    """把 job_dir 下所有支持的文件解析为 ReportInfo 列表。

    assignment_mode: 作业题型。允许图片, 且「上传文件夹/学生名/若干文件」按学生子文件夹合并为一份作答
    (例如一篇作文拍了 3 张照片)。直接放在上传文件夹根下的文件各算一份。
    answer_keys: 传入列表时(自动识别模式), 文件名含「答案/评分标准/answer」的文件解析进该列表,
    作为整批的参考答案, 不当成学生作答。
    """
    work_dir = job_dir / "_work"
    staging = job_dir / "_staging"
    reports, errors = [], []
    skip = {"_work", "_staging", "marked"}
    files = sorted(p for p in job_dir.rglob("*")
                   if p.is_file() and extract.is_supported(p.name, allow_images=assignment_mode)
                   and not skip & set(p.relative_to(job_dir).parts))
    if answer_keys is not None:
        keys = [p for p in files if autodetect.ANSWER_KEY_RE.search(p.stem)]
        files = [p for p in files if p not in keys]
        for path in keys:
            try:
                answer_keys.append(_load_one(path, work_dir, staging, ocr_enabled, render_vision, ocr_mode))
            except Exception as exc:  # noqa: BLE001
                errors.append({"source_file": path.name, "error": f"答案文件解析失败: {exc}"})
    groups: dict[Path, list[Path]] = {}
    for path in files:
        rel = path.relative_to(job_dir)
        key = path.parent if assignment_mode and len(rel.parts) >= 3 else path
        groups.setdefault(key, []).append(path)
    for key, paths in groups.items():
        infos = []
        for path in paths:
            try:
                infos.append(_load_one(path, work_dir, staging, ocr_enabled, render_vision, ocr_mode))
            except Exception as exc:  # noqa: BLE001
                errors.append({"source_file": path.name, "error": str(exc)})
        if infos:
            reports.append(extract.merge_reports(infos, key) if len(paths) > 1 else infos[0])
    return reports, errors


# ---------------------------------------------------------------------------
# 评分与产物
# ---------------------------------------------------------------------------
def build_entry(report, result: dict, status: str, error: str) -> dict:
    pp, pr = grading.problem_pages(report)
    dims = result.get("dimensions", []) or []
    entry = {
        "schema_version": 2,
        "report_id": annotations.stable_id(report.source_path, report.source_sha256),
        "report_snapshot": asdict(report),
        "pages": [asdict(p) for p in report.pages],
        "review_required": result.get("review_required", False),
        "score_provisional": result.get("score_provisional", False),
        "document_state": result.get("document_state", "final"),
        "source_path": report.source_path,
        "source_sha256": report.source_sha256,
        "source_file": report.source_file,
        "student_name": report.student_name,
        "student_id": report.student_id,
        "score": result.get("raw_score", result.get("score", 0)),
        "comment": result.get("comment", ""),
        "reason": result.get("reason", ""),
        "score_reason": result.get("reason", ""),
        "dimensions": dims,
        "dimension_summary": result.get("dimension_summary") or grading.format_dimensions(dims),
        "problem_pages": pp,
        "problem_reasons": pr,
        "status": status,
        "error": error,
        "engine": result.get("engine", "simple"),
        "task_type": result.get("task_type", "lab"),
        "full_score": result.get("full_score", 100),
    }
    if result.get("details"):
        entry["details"] = result["details"]
    if result.get("detected"):
        entry["detected"] = result["detected"]
    # agent / multi / framework 模式额外携带可解释轨迹
    if "trace" in result:
        entry["trace"] = result["trace"]
        entry["steps"] = result.get("steps")
    if result.get("review"):
        entry["review"] = result["review"]
    if result.get("warning"):
        entry["warning"] = result["warning"]
    # 训练导出用, finalize 时剥离, 不进网页 SSE
    if result.get("messages"):
        entry["_messages"] = result["messages"]
    entry["_needs_visual"] = any(
        getattr(p, "needs_visual", False) for p in getattr(report, "pages", []) or []
    )
    return entry


def grade_lab(choice, criteria, report, engine="simple", others=None) -> dict:
    """按所选引擎批改一份实验报告:
      - simple:    单次调用;
      - agent:     手写 ReAct tool-calling 循环 + 自校验;
      - multi:     Grader + Reviewer 双智能体复核;
      - framework: OpenAI Agents SDK 框架版对照。
    """
    if engine == "agent":
        return agent.grade_report_agent(choice, criteria, report, other_texts=others)
    if engine == "multi":
        return multi_agent.grade_report_multi(choice, criteria, report, other_texts=others)
    if engine == "framework":
        return agent_framework.grade_report_framework(choice, criteria, report, other_texts=others)
    return grading.grade_report(choice, criteria, report)


def make_auto_context(choice, criteria, engine, key_reports) -> autodetect.AutoContext:
    return autodetect.AutoContext(
        choice=choice, key_reports=key_reports,
        lab_grader=lambda report, others: grade_lab(choice, criteria, report, engine, others),
    )


def grade_reports(choice, criteria, reports, engine="simple", progress=None, task=None, auto=None) -> list[dict]:
    """并发评分一批 ReportInfo, 返回 entry 列表 (保持输入顺序)。

    task: 作业题型的 TaskSpec (简答/数学/作文), 给定时忽略 engine, 走 assignments 生成-校验-重批循环。
    auto: 自动识别模式的 AutoContext, 逐份判断是实验报告还是试卷; 实验报告仍按 engine 批改。
    """
    graded: list[dict | None] = [None] * len(reports)
    fail_extra = {"task_type": task.task_type, "full_score": task.full_score} if task else {}

    def work(i: int):
        report = reports[i]
        others = [{"student": rp.student_name, "text": rp.full_text}
                  for j, rp in enumerate(reports) if j != i]
        try:
            if auto is not None:
                result = autodetect.grade_auto(auto, report, others)
            elif task is not None:
                result = assignments.grade_submission(choice, task, report)
                result = annotations.single_question(result, report, task)
            else:
                result = grade_lab(choice, criteria, report, engine, others)
            return i, build_entry(report, result, "success", "")
        except Exception as exc:  # noqa: BLE001
            failed = {"score": 0, "comment": "批改失败", "reason": str(exc), **fail_extra}
            return i, build_entry(report, failed, "error", str(exc))

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS)) as pool:
        futures = [pool.submit(work, i) for i in range(len(reports))]
        for fut in as_completed(futures):
            i, entry = fut.result()
            graded[i] = entry
            done += 1
            if progress:
                progress(done, entry)
    return [g for g in graded if g is not None]


def report_statistics(reports, options, extract_errors=None):
    scores = [r["score"] for r in reports if r.get("status", "success") == "success"]
    return {
        "total": len(reports), "average_score": round(sum(scores)/len(scores), 1) if scores else 0,
        "max_score": max(scores) if scores else 0, "min_score": min(scores) if scores else 0,
        "plagiarism_count": sum(bool(r.get("plagiarism_info", {}).get("plagiarism")) for r in reports),
        "errors": len(extract_errors or []) + sum(r.get("status") == "error" for r in reports),
        "pending_review": sum(bool(r.get("review_required")) for r in reports),
        "export_errors": sum(bool(r.get("stamp_error")) for r in reports),
        "task_type": options.get("task_type", "lab"),
        "full_score": (options.get("task") or {}).get("full_score", 100),
    }


def finalize_job(job_id, job_dir, graded_list, criteria, options,
                 extract_errors, text_map=None) -> dict:
    """抄袭检测(可选) + 盖分 + Excel + 持久化, 返回 statistics/artifacts。"""
    try:
        from posttrain.from_agent import write_job_rollouts
        write_job_rollouts(job_dir, graded_list, criteria)
    except Exception:
        for entry in graded_list:
            entry.pop("_messages", None)
            entry.pop("_needs_visual", None)

    text_map = text_map or {}
    plagiarism_on = options.get("plagiarism", False)
    if plagiarism_on:
        texts = [{"key": r["source_path"], "student": r["student_name"],
                  "text": text_map.get(r["source_path"], "")} for r in graded_list]
        plag_map = grading.check_plagiarism(texts)
    else:
        plag_map = {}
    for r in graded_list:
        r["plagiarism_info"] = plag_map.get(r["source_path"], {"plagiarism": False, "similar_with": []})

    try:
        for r in graded_list:
            r["annotate_questions"] = options.get("annotate_questions", True)
            r["handwriting_style"] = options.get("handwriting_style", "liguofu")
        batch = stamp.stamp_batch(graded_list, job_dir)
    except Exception as exc:  # noqa: BLE001
        batch = {"error": str(exc)}

    xlsx_path = job_dir / "批改结果.xlsx"
    try:
        summary.build_workbook(graded_list, xlsx_path)
        excel_ready = True
    except Exception:
        excel_ready = False

    JOBS[job_id] = {
        "reports": graded_list,
        "job_dir": str(job_dir),
        "xlsx": str(xlsx_path),
        "zip": batch.get("zip_path", ""),
        "batch_json": batch.get("batch_results_path", ""),
        "criteria": criteria,
        "options": options,
        "created_at": JOBS.get(job_id, {}).get("created_at") or time.time(),
        "updated_at": time.time(),
        "extract_errors": extract_errors,
    }
    save_job_meta(job_id)

    statistics = report_statistics(graded_list, options, extract_errors)
    artifacts = {
        "excel": f"/download/{job_id}/excel" if excel_ready else "",
        "zip": f"/download/{job_id}/zip" if batch.get("zip_path") else "",
    }
    return {"statistics": statistics, "artifacts": artifacts}


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


def default_criteria() -> str:
    f = config.DEFAULT_CRITERIA_FILE
    return f.read_text(encoding="utf-8") if f.is_file() else ""


@app.route("/config")
def get_config():
    return jsonify({
        "providers": config.provider_public_config(),
        "capabilities": {
            "components": local_workers.capabilities(),
            "tesseract": bool(config.find_tesseract()),
            "soffice": bool(config.find_soffice()),
        },
        "default_criteria": default_criteria(),
        "ocr_modes": sorted(ocr.MODES),
        "default_ocr_mode": ocr.mode(),
        "handwriting_styles": {"liguofu": "李国夫手写体", "kaishu": "神韵英子楷书"},
        "task_types": assignments.TASK_TYPES,
        "default_rules": assignments.DEFAULT_RULES,
        "default_full_score": assignments.DEFAULT_FULL_SCORE,
        "default_essay_dimensions": assignments.DEFAULT_ESSAY_DIMENSIONS,
    })


@app.route("/verify_key", methods=["POST"])
def verify_key():
    choice = choice_from_form(request.form or request.json or {})
    if not choice.api_key:
        return jsonify({"ok": False, "message": "未提供 API Key"}), 400
    ok, message = providers.verify_key(choice)
    return jsonify({"ok": ok, "message": message})


@app.route("/grade", methods=["POST"])
def grade():
    task_type = request.form.get("task_type", "lab").lower()
    if task_type not in assignments.TASK_TYPES and task_type != "auto":
        task_type = "lab"
    task = None
    criteria = (request.form.get("grading_criteria") or "").strip()
    if task_type == "auto":
        criteria = criteria or default_criteria()
    elif task_type == "lab":
        if not criteria:
            return jsonify({"error": "请填写评分标准"}), 400
    else:
        task = assignments.spec_from_form(task_type, {**request.form.to_dict(), "criteria": criteria})
        problem = task.validate()
        if problem:
            return jsonify({"error": problem}), 400
        criteria = task.rules
    choice = choice_from_form(request.form)
    if not choice.api_key:
        return jsonify({"error": "请填写 API Key"}), 400

    engine = request.form.get("engine", "simple").lower()
    if engine not in VALID_ENGINES:
        engine = "simple"
    options = {
        "ocr_mode": request.form.get("ocr_mode", ocr.mode()),
        "annotate_questions": request.form.get("annotate_questions", "true").lower() != "false",
        "handwriting_style": request.form.get("handwriting_style", "liguofu"),
        "ocr_enabled": request.form.get("ocr_enabled", "true").lower() != "false",
        "plagiarism": request.form.get("plagiarism", "false").lower() == "true",
        "engine": engine if task is None else "assignment",
        "task_type": task_type,
        "task": task.as_dict() if task else None,
        "provider": choice.provider,
        "model": choice.model,
        "base_url": choice.base_url,
        "use_vision": choice.use_vision,
    }
    auto_mode = task_type == "auto"
    if options["ocr_mode"] not in ocr.MODES or options["handwriting_style"] not in ("liguofu", "kaishu"):
        return jsonify({"error": "OCR 模式或手写样式无效"}), 400
    allow_images = task is not None or auto_mode

    files = request.files.getlist("reports")
    files = [f for f in files if f and f.filename and extract.is_supported(f.filename, allow_images=allow_images)]
    if not files:
        hint = "PDF/DOCX/TXT/MD" + ("/JPG/PNG" if allow_images else "")
        return jsonify({"error": f"请选择包含作答文件的文件夹 (支持 {hint})"}), 400

    job_id = uuid.uuid4().hex[:12]
    job_dir = config.OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        dest = job_dir / safe_relpath(f.filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        f.save(str(dest))

    def stream():
        try:
            yield sse({"type": "stage", "stage": "extract", "message": "正在解析文件..."})
            key_reports: list | None = [] if auto_mode else None
            reports, extract_errors = load_reports(job_dir, options["ocr_enabled"], choice.use_vision or options["annotate_questions"],
                                                   assignment_mode=allow_images, answer_keys=key_reports, ocr_mode=options["ocr_mode"])
            total = len(reports)
            if total == 0:
                yield sse({"type": "error", "message": "没有可解析的作答文件"})
                return
            if auto_mode:
                keys = "、".join(r.source_file for r in key_reports) or "无(由 AI 先解题生成参考答案)"
                message = f"自动识别并批改 {total} 份作答, 参考答案: {keys}"
            else:
                message = f"开始批改 {total} 份{assignments.TASK_TYPES[task_type]}..."
            yield sse({"type": "stage", "stage": "grade", "total": total, "message": message})

            auto = make_auto_context(choice, criteria, engine, key_reports) if auto_mode else None
            events: list[dict] = []
            graded_list = grade_reports(
                choice, criteria, reports, engine=engine, task=task, auto=auto,
                progress=lambda done, entry: events.append({
                    "type": "progress", "index": done, "total": total,
                    "student": entry["student_name"], "score": entry["score"], "status": entry["status"],
                }),
            )
            for ev in events:
                yield sse(ev)

            yield sse({"type": "stage", "stage": "stamp", "message": "抄袭检测/盖分/汇总..."})
            text_map = {rp.source_path: rp.full_text for rp in reports}
            final = finalize_job(job_id, job_dir, graded_list, criteria, options, extract_errors, text_map)

            yield sse({
                "type": "done", "job_id": job_id, "results": graded_list,
                "statistics": final["statistics"], "extract_errors": extract_errors,
                "artifacts": final["artifacts"],
            })
        except Exception as exc:  # noqa: BLE001
            yield sse({"type": "error", "message": f"服务器错误: {exc}",
                       "trace": traceback.format_exc()[-1500:]})

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/regrade_failed/<job_id>", methods=["POST"])
@serialized_job
def regrade_failed(job_id: str):
    """只重跑失败/异常的报告 (需重新提供 provider/key)。"""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在或已过期"}), 404
    choice = choice_from_form(request.form)
    if not choice.api_key:
        return jsonify({"error": "请填写 API Key"}), 400

    criteria = job.get("criteria", "")
    options = dict(job.get("options", {}))
    options.update({"provider": choice.provider, "model": choice.model,
                    "base_url": choice.base_url, "use_vision": choice.use_vision})
    job_dir = Path(job["job_dir"])
    existing = {r["source_path"]: r for r in job.get("reports", [])}
    failed_paths = {p for p, r in existing.items() if r.get("status") != "success"}
    if not failed_paths:
        return jsonify({"success": True, "message": "没有失败项", "results": job.get("reports", []),
                        "artifacts": {"excel": f"/download/{job_id}/excel", "zip": f"/download/{job_id}/zip"}})

    task = assignments.TaskSpec.from_dict(options["task"]) if options.get("task") else None
    auto_mode = options.get("task_type") == "auto"
    key_reports: list | None = [] if auto_mode else None
    reports, _ = load_reports(job_dir, options.get("ocr_enabled", True), choice.use_vision or options.get("annotate_questions", True),
                              assignment_mode=task is not None or auto_mode, answer_keys=key_reports, ocr_mode=options.get("ocr_mode"))
    to_regrade = [rp for rp in reports if rp.source_path in failed_paths]
    if not to_regrade:
        return jsonify({"error": "无法重新解析失败项的源文件"}), 500

    engine = options.get("engine", "simple")
    auto = make_auto_context(choice, criteria, engine, key_reports) if auto_mode else None
    regraded = grade_reports(choice, criteria, to_regrade, engine=engine, task=task, auto=auto)
    for entry in regraded:
        existing[entry["source_path"]] = entry
    merged = list(existing.values())

    text_map = {rp.source_path: rp.full_text for rp in reports}
    final = finalize_job(job_id, job_dir, merged, criteria, options,
                         job.get("extract_errors", []), text_map)
    still_failed = sum(1 for r in merged if r.get("status") != "success")
    return jsonify({"success": True, "results": merged, "statistics": final["statistics"],
                    "artifacts": final["artifacts"], "still_failed": still_failed})


@app.route("/download/<job_id>/<kind>")
def download(job_id: str, kind: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在或已过期"}), 404
    if kind == "excel" and job.get("xlsx") and Path(job["xlsx"]).is_file():
        return send_file(job["xlsx"], as_attachment=True, download_name="批改结果.xlsx")
    if kind == "zip" and job.get("zip") and Path(job["zip"]).is_file():
        lab = job.get("options", {}).get("task_type", "lab") == "lab"
        return send_file(job["zip"], as_attachment=True,
                         download_name="已批改实验报告.zip" if lab else "已批改作业.zip")
    return jsonify({"error": "文件不存在"}), 404


@app.route("/regenerate/<job_id>", methods=["POST"])
@serialized_job
def regenerate(job_id: str):
    """上传编辑后的 Excel, 按新分数/评语重新盖分并重建产物。"""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在或已过期"}), 404
    edited = request.files.get("workbook")
    if not edited:
        return jsonify({"error": "请上传编辑后的 Excel"}), 400
    job_dir = Path(job["job_dir"])
    tmp_xlsx = job_dir / "_edited.xlsx"
    edited.save(str(tmp_xlsx))
    try:
        rows = summary.read_workbook(tmp_xlsx)
        merged = summary.merge_edits_into_batch(rows, Path(job["batch_json"]))
        for r in merged:
            r.setdefault("plagiarism_info", {"plagiarism": False, "similar_with": []})
            r.setdefault("problem_pages", "无")
            r.setdefault("problem_reasons", "无")
        batch = stamp.stamp_batch(merged, job_dir)
        summary.build_workbook(merged, Path(job["xlsx"]))
        JOBS[job_id]["reports"] = merged
        JOBS[job_id]["zip"] = batch.get("zip_path", job.get("zip", ""))
        JOBS[job_id]["batch_json"] = batch.get("batch_results_path", job.get("batch_json", ""))
        JOBS[job_id]["updated_at"] = time.time()
        save_job_meta(job_id)
        return jsonify({"success": True, "results": merged,
                        "statistics": report_statistics(merged, job.get("options", {}), job.get("extract_errors", [])),
                        "artifacts": {"excel": f"/download/{job_id}/excel", "zip": f"/download/{job_id}/zip"}})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"重生成失败: {exc}"}), 500


@app.route("/cleanup/<job_id>", methods=["POST"])
@serialized_job
def cleanup(job_id: str):
    job = JOBS.pop(job_id, None)
    if job:
        shutil.rmtree(job["job_dir"], ignore_errors=True)
    return jsonify({"success": True})


from graders import review_routes
review_routes.register(app, JOBS, job_lock, choice_from_form, finalize_job)

# 启动时: 恢复历史任务 + 清理超期产物
load_jobs_from_disk()
cleanup_old_jobs()


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000, threaded=True)
