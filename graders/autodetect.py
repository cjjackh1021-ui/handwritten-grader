"""自动识别: 用户只上传试卷/作业/实验报告, 程序自己判断类型、拆题、找参考答案并批改。

流程(每份作答):
1. 关键词启发式先判一遍, 明显是实验报告的直接走实验报告引擎, 不花模型调用;
2. 其余交给模型「识别 + 拆题」: 判断文档类型, 把试卷拆成小题并标题型
   (选择/判断/填空/简答/解答/作文), 同时转写学生作答;
3. 参考答案: 上传文件夹里名字含「答案」的文件作为答案卷(整批只解析一次);
   没有答案卷的题由模型先解题生成参考答案, 同批次同一道题只解一次(缓存);
4. 客观题(选择/判断/数值填空)由程序直接判分; 简答/解答/作文复用 assignments 的
   生成-校验-重批循环; 最后汇总成一份带逐题明细的成绩。
"""

from __future__ import annotations

import difflib
import re
import threading
import json
from dataclasses import replace
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from . import assignments, math_check, providers, annotations
from .assignments import TaskSpec, _vision_choice, _vision_supported, parse_json
from .extract import ReportInfo

ANSWER_KEY_RE = re.compile(r"(答案|评分标准|评分细则|answer)", re.IGNORECASE)

QUESTION_TYPES = {"choice": "选择题", "judge": "判断题", "fill": "填空题",
                  "short": "简答题", "math": "解答题", "essay": "作文"}
OBJECTIVE_TYPES = ("choice", "judge", "fill")
DEFAULT_QUESTION_SCORE = {"choice": 3, "judge": 2, "fill": 3, "short": 10, "math": 12, "essay": 40}

MAX_SEGMENT_IMAGES = 8
MAX_SEGMENT_CHARS = 12000
SEGMENT_MAX_TOKENS = 8000
COMMENT_MAX = assignments.COMMENT_MAX

_LAB_HINTS = ("实验目的", "实验原理", "实验步骤", "实验内容", "实验报告", "实验结果",
              "实验总结", "实验环境", "实验数据", "实验分析", "实验器材", "实验心得")
_EXAM_HINT = re.compile(r"选择题|填空题|判断题|解答题|简答题|计算题|作文|阅读理解|本题\s*满?分?\s*\d+\s*分"
                        r"|每小题|共\s*\d+\s*分|[（(]\s*\d+\s*分\s*[)）]")

_GUARD = assignments._INJECTION_GUARD

SEGMENT_SYSTEM = f"""你是阅卷助理。先判断这份学生文档的类型, 若是试卷/作业则把它拆成需要批改的小题。
{_GUARD}
只返回一个 JSON 对象:
{{
  "doc_type": "exam(试卷/作业/练习)" 或 "lab_report(实验报告)" 或 "other",
  "subject": "学科, 如 数学/语文/英语/物理",
  "student_name": "卷面上写的学生姓名(没有填空串)",
  "student_id": "卷面上写的学号/考号(没有填空串)",
  "questions": [
    {{"no": "题号(印刷的题号; 若各大题内重新从1编号, 写成「二.3」; 小问写成「17(1)」)",
      "type": "choice(选择) | judge(判断) | fill(填空) | short(简答/名词解释/论述/阅读问答) | math(计算/证明/解答, 需看步骤) | essay(作文/书面表达)",
      "question": "完整题干(选择题含全部选项; 作文写题目和写作要求)",
      "student_answer": "学生作答原文, 照实转写, 写错的不要改正; 未作答填空串",
      "full_score": 卷面标注的该题分值(没有标注填 null),
      "pages": [该题所在页码],
      "answer_state": "present(已作答)|blank(确认空白)|unreadable(无法识别)",
      "transcription_confirmed": 是否已根据原图确认转写内容且未按参考答案修正学生的错误,
      "question_block_ids": [题干对应的OCR块ID], "answer_block_ids": [作答对应的OCR块ID],
      "regions": [{{"page": 页码, "role": "question或answer", "bbox": [x0,y0,x1,y1]}}],
      "location_confident": 是否能可靠定位该小题,
      "word_limit": 作文字数要求(无则 0),
      "genre": "作文文体(无则空串)"}}
  ]
}}
要求: 只列学生需要作答的题; 大题含多个小问时拆成小问; 区分印刷题干与学生手写作答;
优先引用提供的OCR块ID; regions仅在OCR漏检时补充，坐标为原图左上角起的0至1归一化坐标;
不能凭参考答案纠正学生原文。模糊、缺边、严重弯曲、OCR冲突未解决时不得确认转写或位置。
若整份文档就是一篇作文, 输出一道 type=essay 的题(看不到题目则 question 留空);
实验报告或无法拆题的文档 questions 给空数组。"""

ANSWER_KEY_SYSTEM = """你是阅卷助理。下面是一份试卷的参考答案/评分标准, 请按题号整理。
只返回一个 JSON 对象:
{
  "questions": [
    {"no": "题号(与试卷一致; 各大题内重新编号时写成「二.3」, 小问写成「17(1)」)",
     "answer": "参考答案(选择题写字母, 判断题写 对/错, 填空题多个空用「；」分隔, 解答题写完整过程)",
     "final_answer": "解答题的最终答案(其余题型空串)",
     "points": "评分要点, 每行「要点或步骤 | 分值」(没有写空串)",
     "full_score": 该题分值(没有写 null)}
  ]
}"""

SOLVE_OBJECTIVE_SYSTEM = """你是该学科的资深教师, 请给下列客观题出标准答案。只返回一个 JSON 对象:
{"answers": [{"no": "题号", "answer": "选择题写字母(多选写全部字母); 判断题写 对 或 错; 填空题多个空用「；」分隔"}]}"""

SOLVE_SUBJECTIVE_SYSTEM = """你是该学科的资深教师, 请给这道题写标准答案和评分要点, 供阅卷使用。只返回一个 JSON 对象:
{
  "reference": "完整参考答案(解答题写出关键步骤)",
  "final_answer": "解答题的最终答案(简答题空串)",
  "points": [{"name": "得分点或解题步骤", "score": 分值}]
}
要求: points 分值之和必须等于给定满分; 步骤按解题顺序排列。"""


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------
def heuristic_doc_type(text: str) -> tuple[str | None, str]:
    """关键词判断; 只在很有把握时给出结论, 否则返回 None 交给模型。"""
    lab = [h for h in _LAB_HINTS if h in (text or "")]
    exam = _EXAM_HINT.findall(text or "")
    if len(lab) >= 2 and len(exam) <= 1:
        return "lab", "命中实验报告关键词: " + "、".join(lab[:4])
    return None, f"实验报告关键词 {len(lab)} 个, 试卷关键词 {len(exam)} 个, 交给模型判断"


def norm_no(no) -> str:
    s = unicodedata.normalize("NFKC", str(no or "")).strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"^第", "", s)
    return re.sub(r"[题、.:]+$", "", s)


def _digits(no: str) -> str:
    return ".".join(re.findall(r"\d+", no))


def _visual_pages(report: ReportInfo, limit: int) -> list[str]:
    return [p.render_path for p in report.pages if p.needs_visual and p.render_path][:limit]


def _call_json(choice, system: str, user: str, images: list[str], trace: list, name: str,
               max_tokens: int | None = None, attempts: int = 2) -> dict:
    """调模型拿 JSON, 解析失败重试一次; 有图且模型支持视觉时带图。"""
    use_images = bool(images) and _vision_supported(choice)
    target = _vision_choice(choice) if use_images else choice
    last_error = ""
    for attempt in range(1, attempts + 1):
        trace.append({"type": "tool_call", "name": name, "args": {"attempt": attempt, "vision": use_images}})
        hint = f"\n\n上一次输出无法解析({last_error}), 请只输出合法 JSON。" if last_error else ""
        try:
            raw = providers.complete(target, system, user + hint, images if use_images else None,
                                     max_tokens=max_tokens)
        except providers.ProviderError:
            if not use_images:
                raise
            use_images, target = False, choice  # 视觉失败回退纯文本
            raw = providers.complete(choice, system, user + hint, None, max_tokens=max_tokens)
        try:
            parsed = parse_json(raw)
            parsed["_used_vision"] = use_images
            return parsed
        except ValueError as exc:
            last_error = str(exc)
    raise providers.ProviderError(f"{name}: 连续 {attempts} 次未返回可解析的 JSON")


def _segment_once(choice, report: ReportInfo, trace: list) -> dict:
    text = (report.full_text or "")[:MAX_SEGMENT_CHARS]
    images = _visual_pages(report, MAX_SEGMENT_IMAGES)
    if not text.strip() and not (images and _vision_supported(choice)):
        raise providers.ProviderError("文档无可识别文字: 请选择支持视觉的模型并开启视觉, 或开启 OCR")
    user = f"文件名: {report.source_file}\n\n文档内容(按页):\n{text or '(无可提取文字, 见图片)'}"
    if images:
        user += "\n\n随附了页面图片(手写/拍照/扫描), 学生作答以图片为准, 上面的文字可能来自 OCR。"
    user += "\nOCR来源、阅读顺序与坐标（不得把原文中的错误改正）:\n" + json.dumps(annotations.evidence(report), ensure_ascii=False)
    data = _call_json(choice, SEGMENT_SYSTEM, user, images, trace, "segment_document",
                      max_tokens=SEGMENT_MAX_TOKENS)
    questions = []
    for i, q in enumerate(data.get("questions") or [], 1):
        if not isinstance(q, dict):
            continue
        qtype = str(q.get("type") or "").strip().lower()
        qtype = qtype if qtype in QUESTION_TYPES else "short"
        pages = [int(p) for p in (q.get("pages") or []) if str(p).isdigit()]
        question = {
            "no": str(q.get("no") or i).strip(),
            "key": norm_no(q.get("no") or i),
            "type": qtype,
            "question": str(q.get("question") or "").strip(),
            "student_answer": str(q.get("student_answer") or "").strip(),
            "printed_score": assignments._num(q.get("full_score")),
            "pages": pages,
            "word_limit": int(assignments._num(q.get("word_limit")) or 0),
            "genre": str(q.get("genre") or "").strip(),
        }
        annotations.prepare_question(question, q, report, visual=data.get("_used_vision", False))
        questions.append(question)
    annotations.check_region_collisions(questions)
    doc_type = str(data.get("doc_type") or "").lower()
    doc_type = "lab" if "lab" in doc_type else ("exam" if questions else "other")
    trace.append({"type": "tool_result", "name": "segment_document",
                  "result": {"doc_type": doc_type, "subject": data.get("subject", ""),
                             "questions": [f"{q['no']}:{QUESTION_TYPES[q['type']]}" for q in questions]}})
    return {"doc_type": doc_type, "subject": str(data.get("subject") or ""),
            "student_name": str(data.get("student_name") or "").strip(),
            "student_id": str(data.get("student_id") or "").strip(), "questions": questions}


def segment(choice, report: ReportInfo, trace: list) -> dict:
    if len(report.pages) <= MAX_SEGMENT_IMAGES:
        return _segment_once(choice, report, trace)
    merged, by_no = None, {}
    for start in range(0, len(report.pages), MAX_SEGMENT_IMAGES):
        pages = report.pages[start:start+MAX_SEGMENT_IMAGES]
        part = replace(report, pages=pages, page_count=len(pages),
                       full_text="\n".join(f"【第{p.page_num}页】\n{p.text}" for p in pages))
        data = _segment_once(choice, part, trace)
        if merged is None: merged = data
        for q in data["questions"]:
            if q["no"] not in by_no:
                by_no[q["no"]] = q
            else:
                previous = by_no[q["no"]]
                previous["student_answer"] += "\n" + q["student_answer"]
                previous["regions"].extend(q["regions"])
                previous["pages"] = sorted(set(previous["pages"] + q["pages"]))
                previous["recognition_review"] |= q["recognition_review"]
                previous["position_review"] |= q["position_review"]
    merged["questions"] = list(by_no.values())
    annotations.check_region_collisions(merged["questions"])
    return merged


def parse_answer_key(choice, key_reports: list[ReportInfo], trace: list) -> dict[str, dict]:
    text = "\n\n".join(f"【{r.source_file}】\n{r.full_text}" for r in key_reports)[:MAX_SEGMENT_CHARS]
    images = [img for r in key_reports for img in _visual_pages(r, MAX_SEGMENT_IMAGES)][:MAX_SEGMENT_IMAGES]
    data = _call_json(choice, ANSWER_KEY_SYSTEM, f"参考答案内容:\n{text or '(见图片)'}", images, trace,
                      "parse_answer_key", max_tokens=SEGMENT_MAX_TOKENS)
    key = {}
    for q in data.get("questions") or []:
        if isinstance(q, dict) and str(q.get("answer") or "").strip():
            key[norm_no(q.get("no"))] = {
                "answer": str(q.get("answer")).strip(),
                "final_answer": str(q.get("final_answer") or "").strip(),
                "points": str(q.get("points") or "").strip(),
                "full": assignments._num(q.get("full_score")),
            }
    trace.append({"type": "tool_result", "name": "parse_answer_key", "result": {"count": len(key)}})
    return key


# ---------------------------------------------------------------------------
# 批次上下文: 答案卷与解题结果在同批学生间共享
# ---------------------------------------------------------------------------
class _Once:
    """同一个 key 只计算一次; 并发线程等待首个计算者的结果, 失败则允许下次重算。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict = {}

    def get(self, key, fn):
        with self._lock:
            entry = self._data.get(key)
            owner = entry is None
            if owner:
                entry = {"event": threading.Event(), "value": None, "error": None}
                self._data[key] = entry
        if owner:
            try:
                entry["value"] = fn()
            except Exception as exc:  # noqa: BLE001
                entry["error"] = exc
                with self._lock:
                    self._data.pop(key, None)
            finally:
                entry["event"].set()
        else:
            entry["event"].wait()
        if entry["error"] is not None:
            raise entry["error"]
        return entry["value"]


def _similar(a: str, b: str) -> bool:
    a, b = re.sub(r"\s+", "", a)[:120], re.sub(r"\s+", "", b)[:120]
    if not a or not b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.55


@dataclass
class AutoContext:
    choice: providers.ModelChoice
    lab_grader: Callable  # (report, others) -> result dict, 由 app 按所选引擎提供
    key_reports: list = field(default_factory=list)

    def __post_init__(self):
        self._once = _Once()
        self._lock = threading.Lock()
        self._solved: dict[str, list[tuple[str, dict]]] = {}
        self.key_trace: list = []

    def answer_key(self) -> dict[str, dict]:
        if not self.key_reports:
            return {}

        def load():
            try:
                return parse_answer_key(self.choice, self.key_reports, self.key_trace)
            except providers.ProviderError as exc:  # 答案卷读不出来就退回 AI 解题, 不让整批失败
                self.key_error = str(exc)
                return {}

        return self._once.get(("answer_key",), load)

    def _cached(self, q: dict) -> dict | None:
        with self._lock:
            for text, ref in self._solved.get(q["key"], []):
                if _similar(text, q["question"]):
                    return ref
        return None

    def _store(self, q: dict, ref: dict) -> None:
        with self._lock:
            self._solved.setdefault(q["key"], []).append((q["question"], ref))

    def references(self, questions: list[dict], report: ReportInfo, trace: list) -> list[dict]:
        key = self.answer_key()
        by_digits = {_digits(k): v for k, v in key.items() if _digits(k)}
        refs: list[dict | None] = [None] * len(questions)
        objective_missing, subjective_missing = [], []
        for i, q in enumerate(questions):
            if q.get("recognition_review"):
                refs[i] = {"answer": "", "source": "none", "full": None}
                continue
            hit = key.get(q["key"]) or by_digits.get(_digits(q["key"]))
            if hit:
                refs[i] = {**hit, "source": "key"}
            elif q["type"] == "essay" or not q["student_answer"]:  # 作文无标准答案; 未作答直接 0 分, 不必解题
                refs[i] = {"answer": "", "final_answer": "", "points": "", "full": None, "source": "none"}
            elif (cached := self._cached(q)) is not None:
                refs[i] = cached
            elif q["type"] in OBJECTIVE_TYPES:
                objective_missing.append(i)
            else:
                subjective_missing.append(i)

        if objective_missing:
            batch = [questions[i] for i in objective_missing]
            solved = self._once.get(("objective",) + tuple(f"{q['key']}|{q['question'][:30]}" for q in batch),
                                    lambda: self._solve_objective(batch, report, trace))
            for i in objective_missing:
                refs[i] = solved.get(questions[i]["key"]) or {"answer": "", "source": "none"}
        for i in subjective_missing:
            q = questions[i]
            full = self.full_score(q, None)
            refs[i] = self._once.get(("subjective", q["key"], q["question"][:30]),
                                     lambda q=q, full=full: self._solve_subjective(q, full, report, trace))
        return refs

    def _solve_objective(self, batch: list[dict], report: ReportInfo, trace: list) -> dict[str, dict]:
        lines = [f"{q['no']}. [{QUESTION_TYPES[q['type']]}] {q['question']}" for q in batch]
        data = _call_json(self.choice, SOLVE_OBJECTIVE_SYSTEM, "题目:\n" + "\n\n".join(lines),
                          _visual_pages(report, 4), trace, "solve_objective")
        answers = {norm_no(a.get("no")): str(a.get("answer") or "").strip()
                   for a in data.get("answers") or [] if isinstance(a, dict)}
        out = {}
        for q in batch:
            ref = {"answer": answers.get(q["key"], ""), "final_answer": "", "points": "", "full": None,
                   "source": "ai" if answers.get(q["key"]) else "none"}
            out[q["key"]] = ref
            if ref["answer"]:
                self._store(q, ref)
        return out

    def _solve_subjective(self, q: dict, full: float, report: ReportInfo, trace: list) -> dict:
        pages = [p.render_path for p in report.pages
                 if p.page_num in q["pages"] and p.needs_visual and p.render_path][:2]
        user = f"题型: {QUESTION_TYPES[q['type']]}    满分: {assignments._fmt_num(full)}分\n题目:\n{q['question']}"
        data = _call_json(self.choice, SOLVE_SUBJECTIVE_SYSTEM, user, pages, trace, "solve_question")
        points = "\n".join(f"{p.get('name')} | {assignments._fmt_num(p.get('score'))}"
                           for p in data.get("points") or []
                           if isinstance(p, dict) and p.get("name") and assignments._num(p.get("score")))
        ref = {"answer": str(data.get("reference") or "").strip(),
               "final_answer": str(data.get("final_answer") or "").strip(),
               "points": points, "full": None, "source": "ai"}
        if ref["answer"]:
            self._store(q, ref)
        return ref

    @staticmethod
    def full_score(q: dict, ref: dict | None) -> float:
        for value in ((ref or {}).get("full"), q.get("printed_score")):
            if value and value > 0:
                return float(value)
        return float(DEFAULT_QUESTION_SCORE[q["type"]])


# ---------------------------------------------------------------------------
# 逐题判分
# ---------------------------------------------------------------------------
_TRUE = {"对", "√", "✓", "✔", "T", "TRUE", "正确", "是", "Y", "YES", "R", "RIGHT"}
_FALSE = {"错", "×", "✗", "✘", "X", "F", "FALSE", "错误", "否", "N", "NO", "WRONG"}


def _letters(s: str) -> set[str]:
    s = unicodedata.normalize("NFKC", s or "").upper()
    s = re.sub(r"^(答案|答|选)[:：]?", "", s.strip())
    m = re.match(r"^\s*([A-H](?:[\s,，、和与]*[A-H])*)(?![A-Z])", s)
    if m:
        return set(re.findall(r"[A-H]", m.group(1)))
    return set(re.findall(r"[A-H]", s)) if len(s) <= 8 else set()


def _judge(s: str) -> bool | None:
    t = re.sub(r"[\s。.!！()（）]", "", unicodedata.normalize("NFKC", s or "").upper())
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    return None


def grade_objective(q: dict, ref: dict, full: float) -> dict | None:
    """确定性判分; 判不了(无参考/文字填空不一致)返回 None, 交给模型。"""
    student, reference = q["student_answer"], ref.get("answer", "")
    if not reference:
        return None
    if q["type"] == "choice":
        s, r = _letters(student), _letters(reference)
        if not r or not s:
            return None
        if s == r:
            score, verdict = full, "正确"
        elif len(r) > 1 and s < r:
            score, verdict = full / 2, "少选"  # 多选题少选得一半, 错选不得分
        else:
            score, verdict = 0.0, "错误"
        detail = {"student": "".join(sorted(s)), "reference": "".join(sorted(r)), "method": "letters"}
    elif q["type"] == "judge":
        s, r = _judge(student), _judge(reference)
        if s is None or r is None:
            return None
        score, verdict = (full, "正确") if s == r else (0.0, "错误")
        detail = {"student": "对" if s else "错", "reference": "对" if r else "错", "method": "judge"}
    else:
        check = math_check.check_answer(student, reference)
        decisive = check["comparable"] and (check["method"] not in ("text", "none") or check["equal"])
        if not decisive:
            return None
        score, verdict = (full, "正确") if check["equal"] else (0.0, "错误")
        detail = {"student": student, "reference": reference, "method": check["method"], "check": check}
    return {"score": score, "comment": verdict, "reason": f"学生答 {detail['student']}, 参考答案 {detail['reference']}",
            "details": detail, "graded_by": "program"}


def _sub_report(report: ReportInfo, q: dict) -> ReportInfo:
    if q.get("regions") or any(p.render_path for p in report.pages):
        return annotations.crop_report(report, q)
    pages = [p for p in report.pages if p.page_num in q["pages"]] or list(report.pages)
    return ReportInfo(
        source_path=report.source_path, source_sha256=report.source_sha256,
        source_file=report.source_file, student_name=report.student_name,
        student_id=report.student_id, page_count=len(pages),
        full_text=q["student_answer"], pages=pages,
    )


def grade_question(ctx: AutoContext, q: dict, ref: dict, report: ReportInfo) -> dict:
    full = ctx.full_score(q, ref)
    base = {"no": q["no"], "type": q["type"], "label": QUESTION_TYPES[q["type"]], "question": q["question"],
            "student_answer": q["student_answer"], "reference": ref.get("answer", ""),
            "reference_source": ref.get("source", "none"), "full": full,
            "default_score": not (ref.get("full") or q.get("printed_score"))}
    base.update({k: q[k] for k in ("id", "regions", "pages", "answer_state", "recognition_review", "position_review", "grading_review", "review_reasons") if k in q})
    base["_question"] = q
    base["_reference"] = ref
    if q.get("recognition_review"):
        return {**base, "score": 0.0, "comment": "待复核", "reason": "作答尚未确认，未计入暂计总分", "graded_by": "pending"}
    if not q["student_answer"]:
        return {**base, "score": 0.0, "comment": "未作答", "reason": "未作答", "graded_by": "program"}
    if q["type"] in OBJECTIVE_TYPES:
        res = grade_objective(q, ref, full)
        if res is not None:
            return {**base, **res}
        spec = TaskSpec(task_type="short", question=f"【第{q['no']}题】{q['question']}",
                        reference=ref.get("answer") or "（无参考答案, 请先自行解答再判分）",
                        full_score=max(1.0, full), points_text=f"答案正确 | {assignments._fmt_num(max(1.0, full))}",
                        rules="客观题: 与参考答案意思一致即得满分, 多个空按空平均给分。")
    else:
        spec = TaskSpec(
            task_type=q["type"], question=f"【第{q['no']}题】{q['question'] or '（未识别到题目, 按通用标准评价）'}",
            reference=ref.get("answer") or ("" if q["type"] == "essay" else "（无参考答案, 请先自行解答再判分）"),
            full_score=max(1.0, full), points_text=ref.get("points", ""),
            rules=assignments.DEFAULT_RULES[q["type"]], final_answer=ref.get("final_answer", ""),
            genre=q["genre"], word_limit=q["word_limit"],
        )
    res = assignments.grade_submission(ctx.choice, spec, _sub_report(report, q))
    out = {**base, "score": min(full, float(res.get("raw_score", res["score"]))), "comment": res["comment"], "reason": res["reason"],
           "details": res.get("details"), "dimensions": res.get("dimensions"),
           "graded_by": "model", "attempts": res.get("steps")}
    out["task_spec"] = spec.as_dict()
    if res.get("transcription_issue"):
        out.update(recognition_review=True, answer_state="unreadable", score=0.0, comment="待复核",
                   reason="逐题裁图中仍存在无法确认的字迹，未计入暂计总分")
        out.setdefault("review_reasons", []).append("逐题裁图中的作答尚未确认")
    if res.get("warning"):
        out["warning"] = res["warning"]
    return out


def _summary_comment(questions: list[dict], total: float, full: float) -> str:
    wrong = [q["no"] for q in questions if q["score"] < q["full"] - 1e-6]
    if not wrong:
        return "全部正确，继续保持"
    rate = f"得分率{round(total / full * 100)}%，" if full else ""
    for n in range(len(wrong), 0, -1):
        text = f"{rate}第{','.join(wrong[:n])}{'等' if n < len(wrong) else ''}题需订正"
        if len(text) <= COMMENT_MAX:
            return text
    return f"{rate}多题需订正"[:COMMENT_MAX]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def grade_auto(ctx: AutoContext, report: ReportInfo, others=None) -> dict:
    trace: list[dict] = []
    doc_type, evidence = heuristic_doc_type(report.full_text)
    trace.append({"type": "tool_result", "name": "detect_doc_type",
                  "result": {"method": "keywords", "doc_type": doc_type or "undecided", "evidence": evidence}})
    if doc_type == "lab":
        return _as_lab(ctx, report, others, "keywords", evidence)

    seg = segment(ctx.choice, report, trace)
    if seg["doc_type"] == "lab" or (seg["doc_type"] == "other" and not seg["questions"]):
        if seg["doc_type"] == "other":
            raise providers.ProviderError("未能识别为试卷或实验报告(没有拆出可批改的题目)")
        return _as_lab(ctx, report, others, "model", "模型判定为实验报告")
    if seg["student_name"] and report.student_name in ("", report.source_file.rsplit(".", 1)[0]):
        report.student_name = seg["student_name"]
    if seg["student_id"] and not report.student_id:
        report.student_id = seg["student_id"]

    questions = seg["questions"]
    refs = ctx.references(questions, report, trace)
    if ctx.key_trace and not any(t.get("name") == "parse_answer_key" for t in trace):
        trace.extend(ctx.key_trace[-2:])
    graded = []
    for q, ref in zip(questions, refs):
        try:
            g = grade_question(ctx, q, ref, report)
        except Exception as exc:  # noqa: BLE001  # 单题失败不拖垮整卷
            g = {"no": q["no"], "type": q["type"], "label": QUESTION_TYPES[q["type"]], "question": q["question"],
                 "student_answer": q["student_answer"], "reference": ref.get("answer", ""),
                 "reference_source": ref.get("source", "none"), "full": ctx.full_score(q, ref),
                 "score": 0.0, "comment": "批改失败", "reason": str(exc)[:200], "error": str(exc)[:200]}
            g.update({k: q[k] for k in ("id", "regions", "pages", "answer_state", "recognition_review", "position_review", "review_reasons") if k in q})
            g.update(grading_review=True, _question=q, _reference=ref)
        graded.append(g)
        trace.append({"type": "tool_result", "name": f"grade_q{q['no']}",
                      "result": {"type": g["label"], "score": g["score"], "full": g["full"],
                                 "by": g.get("graded_by", "error"), "ref": g["reference_source"]}})

    total = sum(g["score"] for g in graded)
    full = sum(g["full"] for g in graded)
    score = int(total + 0.5)  # Legacy API field; raw_score preserves fractional totals.
    comment = _summary_comment(graded, total, full)
    reason = "；".join(f"第{g['no']}题{assignments._fmt_num(g['score'])}/{assignments._fmt_num(g['full'])}"
                      + (f"({g['comment']})" if g["score"] < g["full"] else "") for g in graded)
    dims = [{"name": f"第{g['no']}题·{g['label']}", "score": g["score"], "full": g["full"], "comment": g["comment"]}
            for g in graded]
    warnings = [f"第{g['no']}题{'批改失败' if g.get('error') else '校验未通过'}" for g in graded
                if g.get("error") or g.get("warning")]
    ai_refs = sum(1 for g in graded if g["reference_source"] == "ai")
    trace.append({"type": "final", "grade": {"score": score, "full": assignments._fmt_num(full), "comment": comment}})
    result = {
        "score": score, "raw_score": total, "comment": comment, "reason": reason[:400], "dimensions": dims,
        "dimension_summary": "; ".join(f"{g['no']}:{assignments._fmt_num(g['score'])}/{assignments._fmt_num(g['full'])}"
                                       for g in graded),
        "task_type": "exam", "full_score": full or 100,
        "details": {"subject": seg["subject"], "questions": graded, "answer_key": bool(ctx.key_reports),
                    "ai_reference_count": ai_refs, "detected_by": "model"},
        "detected": {"doc_type": "exam", "by": "model", "evidence": f"拆出 {len(graded)} 道题"},
        "trace": trace, "engine": "auto",
        "steps": sum(1 for t in trace if t["type"] == "tool_call") + sum(g.get("attempts") or 0 for g in graded),
    }
    if warnings:
        result["warning"] = "、".join(warnings)
    return annotations.summarize(result)


def _as_lab(ctx: AutoContext, report: ReportInfo, others, by: str, evidence: str) -> dict:
    result = dict(ctx.lab_grader(report, others))
    result["task_type"] = "lab"
    result["full_score"] = 100
    result["detected"] = {"doc_type": "lab", "by": by, "evidence": evidence}
    return result
