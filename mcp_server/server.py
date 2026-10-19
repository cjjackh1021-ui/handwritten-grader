"""实验报告批改 MCP Server —— 把批改能力作为工具对外暴露。

通过 Model Context Protocol 把本项目的核心能力开放给任意 MCP 客户端
(Cursor / Claude Desktop / 其它 agent):

- extract_report        解析单份报告(PDF/DOCX/TXT/MD) → 元信息 + 正文
- get_default_rubric    读取内置默认评分标准
- grade_report          用指定 provider/engine 批改单份报告 → 评分 JSON
- grade_assignment      批改简答题 / 数学步骤题 / 作文(支持拍照作答) → 分项评分 JSON
- grade_document        自动识别试卷/实验报告并批改(试卷自动拆题、逐题判分)
- check_similarity      多份报告两两查重
- build_summary_excel   把评分结果写成分组 Excel

运行(stdio)::

    python -m mcp_server.server
    # 或
    python mcp_server/server.py

兼容 mcp 1.x(FastMCP) 与 2.x(MCPServer)。配置样例见 mcp_server/README.md。
"""

from __future__ import annotations

import json
from dataclasses import asdict
import sys
from pathlib import Path

# 允许以脚本方式直接运行时找到 graders 包
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from graders import (  # noqa: E402
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
    annotations,
)

# --- 兼容 mcp 1.x / 2.x ------------------------------------------------------
try:  # 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore
except Exception:  # noqa: BLE001  # 2.x 更名为 MCPServer
    from mcp.server.mcpserver import MCPServer as _Server  # type: ignore

mcp = _Server(name="lab-report-grader", instructions=(
    "实验报告自动批改工具集：解析报告、读取评分标准、按 provider/engine 批改、查重、导出 Excel。"
))


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _load_report(path: str, ocr_mode: str | None = None):
    """把单个文件解析成 ReportInfo(供工具复用)。"""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在: {p}")
    work = config.WORK_DIR
    work.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower()
    if ext == ".pdf":
        return extract.inspect_pdf(p, work, ocr_enabled=True, render_for_vision=True, ocr_mode=ocr_mode)
    if ext in (".txt", ".md"):
        return extract.inspect_text_file(p)
    if ext in extract.IMAGE_EXT:
        return extract.inspect_image(p, work, ocr_enabled=True, ocr_mode=ocr_mode)
    if ext == ".docx":
        conv = convert.convert_docx(p, work / "_staging")
        if conv.pdf_path:
            info = extract.inspect_pdf(Path(conv.pdf_path), work, True, True, ocr_mode)
            info.origin_path = conv.source_path
            return info
        if conv.text is not None:
            return extract.report_from_text(p, conv.text, conv.source_sha256)
        raise ValueError(conv.error or "DOCX 无法解析")
    raise ValueError(f"不支持的文件类型: {ext}")


def _make_choice(provider: str, api_key: str, model: str, base_url: str, use_vision: bool):
    import os

    if not api_key:
        preset = config.PROVIDERS.get(provider, {})
        api_key = os.getenv(preset.get("env_key", ""), "") if preset else ""
    return providers.ModelChoice(provider=provider, api_key=api_key, model=model,
                                 base_url=base_url, use_vision=use_vision)


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------
@mcp.tool(description="解析单份实验报告(PDF/DOCX/TXT/MD), 返回学生信息、逐页元数据与正文文本。")
def extract_report(path: str, max_chars: int = 6000, ocr_mode: str | None = None) -> dict:
    r = _load_report(path, ocr_mode)
    return {
        "student_name": r.student_name,
        "student_id": r.student_id,
        "page_count": r.page_count,
        "pages": [
            {"page_num": p.page_num, "char_count": p.char_count,
             "image_count": p.image_count, "needs_visual": p.needs_visual, "metadata": p.metadata, "ocr": p.ocr}
            for p in r.pages
        ],
        "text": r.full_text[:max_chars],
        "truncated": len(r.full_text) > max_chars,
    }


@mcp.tool(description="读取内置的默认评分标准(Markdown 文本)。")
def get_default_rubric() -> str:
    f = config.DEFAULT_CRITERIA_FILE
    return f.read_text(encoding="utf-8") if f.is_file() else ""


@mcp.tool(description=(
    "批改单份实验报告。engine 可选 simple/agent/multi/framework; provider 可选 "
    "deepseek/openai/anthropic/custom。未显式传 api_key 时读取对应环境变量。返回评分 JSON。"))
def grade_report(path: str, criteria: str = "", provider: str = "deepseek",
                 api_key: str = "", model: str = "", base_url: str = "",
                 engine: str = "agent", use_vision: bool = True) -> dict:
    report = _load_report(path)
    criteria = criteria or get_default_rubric()
    choice = _make_choice(provider, api_key, model, base_url, use_vision)
    if engine == "agent":
        result = agent.grade_report_agent(choice, criteria, report)
    elif engine == "multi":
        result = multi_agent.grade_report_multi(choice, criteria, report)
    elif engine == "framework":
        result = agent_framework.grade_report_framework(choice, criteria, report)
    else:
        result = grading.grade_report(choice, criteria, report)
    return {
        "student_name": report.student_name,
        "student_id": report.student_id,
        "score": result.get("score"),
        "comment": result.get("comment", ""),
        "reason": result.get("reason", ""),
        "dimensions": result.get("dimensions", []),
        "dimension_summary": result.get("dimension_summary")
        or grading.format_dimensions(result.get("dimensions", [])),
        "engine": result.get("engine", engine),
        "steps": result.get("steps"),
        "review": result.get("review"),
    }


@mcp.tool(description=(
    "批改单份作业: task_type 为 short(简答题)/math(数学步骤题)/essay(作文)。path 可为 PDF/DOCX/TXT/MD "
    "或手写拍照(JPG/PNG)。question 题目必填; short/math 需 reference 参考答案; points 每行一个得分点/步骤/维度, "
    "可写「要点 | 分值」; math 可给 final_answer 让程序核验等价。模型输出会经程序校验, 不通过自动重批。"))
def grade_assignment(path: str, task_type: str, question: str, reference: str = "",
                     points: str = "", full_score: int = 0, rules: str = "",
                     final_answer: str = "", genre: str = "", level: str = "", word_limit: int = 0,
                     provider: str = "deepseek", api_key: str = "", model: str = "",
                     base_url: str = "", use_vision: bool = True, ocr_mode: str | None = None) -> dict:
    spec = assignments.TaskSpec(
        task_type=task_type, question=question, reference=reference,
        full_score=full_score or assignments.DEFAULT_FULL_SCORE.get(task_type, 10),
        points_text=points, rules=rules or assignments.DEFAULT_RULES.get(task_type, ""),
        final_answer=final_answer, genre=genre, level=level, word_limit=word_limit,
    )
    problem = spec.validate()
    if problem:
        raise ValueError(problem)
    report = _load_report(path, ocr_mode)
    choice = _make_choice(provider, api_key, model, base_url, use_vision)
    result = annotations.single_question(assignments.grade_submission(choice, spec, report), report, spec)
    result["pages"] = [asdict(p) for p in report.pages]
    result.pop("trace", None)
    return {"student_name": report.student_name, "student_id": report.student_id, **result}


@mcp.tool(description=(
    "自动识别并批改一份学生文档(试卷/作业/实验报告, 支持 PDF/DOCX/TXT/图片): 实验报告按评分标准批改; "
    "试卷自动拆题、识别题型(选择/判断/填空/简答/解答/作文)并逐题判分。answer_key_path 可给参考答案文件, "
    "不给则由模型先解题生成参考答案。"))
def grade_document(path: str, answer_key_path: str = "", criteria: str = "", provider: str = "deepseek",
                   api_key: str = "", model: str = "", base_url: str = "", engine: str = "simple",
                   use_vision: bool = True, ocr_mode: str | None = None) -> dict:
    report = _load_report(path, ocr_mode)
    choice = _make_choice(provider, api_key, model, base_url, use_vision)
    criteria = criteria or get_default_rubric()
    labs = {"agent": agent.grade_report_agent, "multi": multi_agent.grade_report_multi,
            "framework": agent_framework.grade_report_framework}

    def lab_grader(rp, others):
        if engine in labs:
            return labs[engine](choice, criteria, rp)
        return grading.grade_report(choice, criteria, rp)

    ctx = autodetect.AutoContext(choice=choice, lab_grader=lab_grader,
                                 key_reports=[_load_report(answer_key_path, ocr_mode)] if answer_key_path else [])
    result = autodetect.grade_auto(ctx, report)
    result["pages"] = [asdict(p) for p in report.pages]
    result.pop("trace", None)
    result.pop("messages", None)
    return {"student_name": report.student_name, "student_id": report.student_id, **result}


@mcp.tool(description="对多份报告做两两 TF-IDF 余弦查重, 返回每份的疑似抄袭对象。paths 为文件路径列表。")
def check_similarity(paths: list[str], threshold: float = 0.8) -> dict:
    texts = []
    for pth in paths:
        try:
            r = _load_report(pth)
            texts.append({"key": str(Path(pth).resolve()), "student": r.student_name, "text": r.full_text})
        except Exception as exc:  # noqa: BLE001
            texts.append({"key": str(pth), "student": "", "text": "", "error": str(exc)})
    result = grading.check_plagiarism(texts, threshold=threshold)
    return {k: v for k, v in result.items()}


@mcp.tool(description=(
    "把评分结果列表写成分组 Excel。items_json 为 JSON 数组, 每项含 student_name/student_id/"
    "score/comment/reason/dimension_summary 等字段; 返回生成的 xlsx 绝对路径。"))
def build_summary_excel(items_json: str, out_path: str = "") -> dict:
    items = json.loads(items_json)
    for it in items:
        it.setdefault("status", "success")
        it.setdefault("problem_pages", "无")
        it.setdefault("problem_reasons", "无")
        it.setdefault("plagiarism_info", {"plagiarism": False, "similar_with": []})
    out = Path(out_path).expanduser().resolve() if out_path else (config.OUTPUT_DIR / "mcp_summary.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)
    summary.build_workbook(items, out)
    return {"path": str(out), "count": len(items)}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
