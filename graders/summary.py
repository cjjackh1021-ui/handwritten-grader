"""学生分组 Excel 汇总（openpyxl 重写 build_summary.mjs）。

去除 Codex 专有的 @oai/artifact-tool 依赖。功能：
- 把批改结果按 学号 -> 姓名 -> 报告名 排序, 使同一学生的报告连续。
- 生成 批改结果.xlsx（可编辑）。
- 支持从已编辑的 xlsx 回读（--from-xlsx 等价），产出修订后的评分 JSON，
  供重新盖分。

列: 姓名 / 学号 / 分数 / 评语 / 给分依据 / 抄袭嫌疑 / 问题页码 / 问题原因 / 来源文件
"""

from __future__ import annotations

import json
import copy
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from . import annotations, review

HEADERS = [
    "姓名",
    "学号",
    "分数",
    "满分",
    "题型",
    "分维度得分",
    "评语",
    "给分依据",
    "抄袭嫌疑",
    "问题页码",
    "问题原因",
    "来源文件",
    "作答ID",
    "结果状态",
]
COL_WIDTHS = [14, 16, 8, 8, 12, 34, 30, 40, 10, 12, 24, 28, 22, 16]
TASK_LABELS = {"lab": "实验报告", "short": "简答题", "math": "数学步骤题", "essay": "作文", "exam": "试卷"}


def _sort_key(item: dict):
    return (
        str(item.get("student_id", "") or "~"),  # 空学号排后
        str(item.get("student_name", "")),
        str(item.get("source_file", "")),
    )


def build_workbook(reports: list[dict], out_xlsx: Path) -> str:
    out_xlsx = Path(out_xlsx)
    out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        [r for r in reports if r.get("status", "success") == "success"],
        key=_sort_key,
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "批改结果"
    ws.sheet_view.showGridLines = False

    header_fill = PatternFill("solid", fgColor="667EEA")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    wrap = Alignment(vertical="top", wrap_text=True)

    ws.append(HEADERS)
    for col, width in enumerate(COL_WIDTHS, start=1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", horizontal="center")
        ws.column_dimensions[get_column_letter(col)].width = width

    prev_student = None
    band = False
    for item in rows:
        plag = item.get("plagiarism_info", {}) or {}
        plag_flag = "是" if plag.get("plagiarism") else "否"
        student_key = (item.get("student_id"), item.get("student_name"))
        if student_key != prev_student:
            band = not band
            prev_student = student_key
        ws.append(
            [
                item.get("student_name", ""),
                item.get("student_id", ""),
                item.get("score", 0),
                item.get("full_score") or 100,
                TASK_LABELS.get(item.get("task_type") or "lab", item.get("task_type")),
                item.get("dimension_summary", ""),
                item.get("comment", ""),
                item.get("reason", ""),
                plag_flag,
                item.get("problem_pages", "无"),
                item.get("problem_reasons", "无"),
                item.get("source_file", ""),
                item.get("report_id", ""),
                "暂计" if item.get("score_provisional") else ("位置待复核" if item.get("review_required") else "正式"),
            ]
        )
        r = ws.max_row
        fill = PatternFill("solid", fgColor="F3F5FF") if band else None
        for col in range(1, len(HEADERS) + 1):
            cell = ws.cell(row=r, column=col)
            cell.alignment = wrap
            if fill:
                cell.fill = fill
        # 学号存成文本, 避免前导零丢失
        ws.cell(row=r, column=2).number_format = "@"

    ws.freeze_panes = "A2"
    detail = wb.create_sheet("逐题明细")
    detail.append(["作答ID", "题目ID", "姓名", "题号", "分数", "满分", "标记", "复核状态", "学生作答", "评语"])
    for item in rows:
        for q in item.get("details", {}).get("questions", []):
            detail.append([item.get("report_id", ""), q.get("id", ""), item.get("student_name", ""), str(q["no"]),
                           q["score"], q["full"], q.get("mark") or "待复核", "待复核" if q.get("review_required") else "已确认",
                           q.get("student_answer", ""), q.get("comment", "")])
    detail.freeze_panes = "E2"
    for col in range(1, 11):
        detail.column_dimensions[get_column_letter(col)].width = 24 if col not in (9, 10) else 45
        detail.cell(1, col).fill = header_fill
        detail.cell(1, col).font = header_font
    for sheet in wb:
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, str): cell.data_type = "s"
                cell.alignment = wrap
    note = wb.create_sheet("编辑说明")
    note.append(["仅编辑逐题明细的分数列；识别文字及位置请在网页复核。修改逐题分数后自动更新总分。"])
    note.append(["仅修改批改结果的总分属于人工总分调整，不反推逐题对错。作答ID、题目ID请保留。"])
    note.append(["无法识别的题目仍需网页确认文字；单改分数不会清除识别疑问。"])
    note.column_dimensions["A"].width = 110
    wb.save(str(out_xlsx))
    return str(out_xlsx.resolve())


def read_workbook(xlsx_path: Path) -> list[dict]:
    """从已编辑的 xlsx 回读评分, 用于重新盖分。"""
    xlsx_path = Path(xlsx_path)
    wb = load_workbook(str(xlsx_path))
    ws = wb["批改结果"] if "批改结果" in wb.sheetnames else wb.active
    header = [c.value for c in ws[1]]
    idx = {name: header.index(name) for name in HEADERS if name in header}
    out = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row is None or all(v in (None, "") for v in row):
            continue

        def get(name, default=""):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) and row[i] is not None else default

        out.append(
            {
                "student_name": str(get("姓名")),
                "student_id": str(get("学号")),
                "score": float(get("分数", 0) or 0),
                "report_id": str(get("作答ID")),
                "comment": str(get("评语")),
                "reason": str(get("给分依据")),
                "problem_pages": str(get("问题页码", "无")),
                "problem_reasons": str(get("问题原因", "无")),
                "source_file": str(get("来源文件")),
            }
        )
    by_id = {r["report_id"]: r for r in out if r["report_id"]}
    if "逐题明细" in wb.sheetnames:
        sheet = wb["逐题明细"]
        heads = [c.value for c in sheet[1]]
        for values in sheet.iter_rows(min_row=2, values_only=True):
            row = dict(zip(heads, values))
            rid, qid = str(row.get("作答ID") or ""), str(row.get("题目ID") or "")
            if not rid and not qid: continue
            if rid not in by_id: raise ValueError("逐题明细中的作答ID不属于汇总表")
            by_id[rid].setdefault("question_edits", []).append({"id": qid, "score": row.get("分数")})
    wb.close()
    return out


def merge_edits_into_batch(edited_rows: list[dict], batch_json: Path) -> list[dict]:
    """把 xlsx 回读的评分合并回 batch_results.json（按 source_file 匹配）, 返回合并后的 reports。"""
    batch = json.loads(Path(batch_json).read_text(encoding="utf-8"))
    merged = copy.deepcopy(batch.get("reports", []))
    seen = set()
    for edit in edited_rows:
        candidates = [r for r in merged if r.get("report_id") == edit["report_id"]] if edit.get("report_id") else [r for r in merged if r.get("source_file") == edit["source_file"]]
        if len(candidates) != 1: raise ValueError("无法唯一匹配作答，请保留新版表中的作答ID")
        base = candidates[0]
        if id(base) in seen: raise ValueError("汇总表出现重复作答")
        seen.add(id(base))
        score = review.valid_score(edit["score"], base.get("full_score", 100))
        total_changed = score != float(base["score"])
        changed = False
        questions = {q.get("id"): q for q in base.get("details", {}).get("questions", [])}
        q_seen = set()
        for change in edit.get("question_edits", []):
            qid = change["id"]
            if qid not in questions or qid in q_seen: raise ValueError("题目ID无效或重复")
            q_seen.add(qid)
            q = questions[qid]
            value = review.valid_score(change["score"], q["full"])
            if value != float(q["score"]):
                q.update(score=value, grading_review=False, graded_by="human", comment="Excel 人工改分")
                q.pop("warning", None); q.pop("error", None)
                changed = True
        if changed:
            base.pop("manual_total_adjustment", None)
            review.aggregate(base)
            if total_changed and abs(score-base["score"]) > 1e-6: raise ValueError("逐题合计与人工总分不一致，请只修改一种分数或保持一致")
        elif total_changed:
            base["manual_total_adjustment"] = {"before": base["score"], "after": score}
            base["score"] = score
        for k in ("student_name", "student_id", "comment", "reason"):
            if k in edit and (not changed or k not in ("comment", "reason")): base[k] = edit[k]
        annotations.summarize(base)
    return merged
