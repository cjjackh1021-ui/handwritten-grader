"""批改 Agent 的工具注册表。

把已有的 graders 能力包装成可被大模型 tool calling 调用的工具：
- get_rubric        读取评分标准
- get_report_info   报告元信息(页数/每页字数/图片数/是否需视觉复核)
- read_text         读取正文(可按页)
- view_pages        查看指定页面图像(视觉复核; 无视觉能力时回退 OCR 文本)
- check_similarity  与本批其他报告的相似度(查重线索)
- submit_grade      提交最终评分(终止工具, 触发自校验)

每个工具是一个 :class:`Tool` (名称/描述/JSON schema/执行函数)。工具执行绑定在
:class:`ToolContext` 上, 上下文携带当前报告、评分标准与本批其他文本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import extract


@dataclass
class ToolContext:
    """一次(单份报告)批改会话的共享状态。"""

    report: "extract.ReportInfo"
    criteria: str
    use_vision: bool = True
    other_texts: list[dict] = field(default_factory=list)  # [{student, text}] 本批其他报告
    # 运行期累积
    viewed_pages: set[int] = field(default_factory=set)
    pending_images: list[str] = field(default_factory=list)  # 待注入对话的图片路径
    submitted: dict | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[[ToolContext, dict], dict]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def _get_rubric(ctx: ToolContext, args: dict) -> dict:
    return {"rubric": ctx.criteria}


def _get_report_info(ctx: ToolContext, args: dict) -> dict:
    r = ctx.report
    return {
        "student_name": r.student_name,
        "student_id": r.student_id,
        "page_count": r.page_count,
        "pages": [
            {
                "page_num": p.page_num,
                "char_count": p.char_count,
                "image_count": p.image_count,
                "needs_visual": p.needs_visual,
            }
            for p in r.pages
        ],
        "has_visual_pages": any(p.needs_visual for p in r.pages),
    }


def _read_text(ctx: ToolContext, args: dict) -> dict:
    page_num = args.get("page_num")
    max_chars = int(args.get("max_chars", 4000))
    if page_num:
        page = next((p for p in ctx.report.pages if p.page_num == int(page_num)), None)
        if not page:
            return {"error": f"页码 {page_num} 不存在"}
        return {"page_num": page.page_num, "text": page.text[:max_chars]}
    return {"text": ctx.report.full_text[:max_chars], "truncated": len(ctx.report.full_text) > max_chars}


def _view_pages(ctx: ToolContext, args: dict) -> dict:
    nums = args.get("page_nums") or []
    if not isinstance(nums, list):
        nums = [nums]
    nums = [int(n) for n in nums][:4]
    loaded, ocr_fallback = [], []
    for n in nums:
        page = next((p for p in ctx.report.pages if p.page_num == n), None)
        if not page:
            continue
        ctx.viewed_pages.add(n)
        if ctx.use_vision and page.render_path and Path(page.render_path).is_file():
            ctx.pending_images.append(page.render_path)
            loaded.append(n)
        else:
            # 无视觉能力/无渲染图 -> 回退 OCR/正文
            txt = page.ocr_text or page.text
            ocr_fallback.append({"page_num": n, "text": txt[:1500]})
    result = {"viewed_pages": loaded}
    if loaded and ctx.use_vision:
        result["note"] = "已加载页面图像, 见随后的用户消息中的图片, 请结合图片内容评分。"
    if ocr_fallback:
        result["ocr_text"] = ocr_fallback
        result["note_ocr"] = "当前模型/页面无可用图像, 已改用 OCR/文本。"
    return result


def _check_similarity(ctx: ToolContext, args: dict) -> dict:
    from .grading import check_plagiarism

    if not ctx.other_texts:
        return {"similar": [], "note": "本批仅一份报告或未提供对比文本"}
    texts = [{"key": "self", "student": ctx.report.student_name, "text": ctx.report.full_text}]
    for i, o in enumerate(ctx.other_texts):
        texts.append({"key": f"other-{i}", "student": o.get("student", ""), "text": o.get("text", "")})
    result = check_plagiarism(texts, threshold=float(args.get("threshold", 0.8)))
    self_res = result.get("self", {"plagiarism": False, "similar_with": []})
    return {"plagiarism": self_res.get("plagiarism", False), "similar_with": self_res.get("similar_with", [])}


def _submit_grade(ctx: ToolContext, args: dict) -> dict:
    # 只记录; 真正的校验在 agent 循环里做, 便于给出可纠正的反馈
    ctx.submitted = {
        "score": args.get("score"),
        "comment": args.get("comment", ""),
        "reason": args.get("reason", ""),
        "dimensions": args.get("dimensions", []) or [],
        **{k: args[k] for k in ("deductions", "review_required", "review_reason") if k in args},
    }
    return {"received": True}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
TOOLS: dict[str, Tool] = {
    "get_rubric": Tool(
        "get_rubric", "读取本次批改所用的评分标准全文。",
        {"type": "object", "properties": {}}, _get_rubric,
    ),
    "get_report_info": Tool(
        "get_report_info",
        "获取报告元信息: 学生姓名/学号、总页数、每页字数与图片数、哪些页需要看图复核。开始时先调用它了解结构。",
        {"type": "object", "properties": {}}, _get_report_info,
    ),
    "read_text": Tool(
        "read_text",
        "读取报告正文。不传 page_num 读全文(截断), 传 page_num 读某一页。",
        {
            "type": "object",
            "properties": {
                "page_num": {"type": "integer", "description": "页码, 省略则读全文"},
                "max_chars": {"type": "integer", "description": "最多返回字符数, 默认4000"},
            },
        }, _read_text,
    ),
    "view_pages": Tool(
        "view_pages",
        "查看指定页面的图像做视觉复核(适用于图表/截图/代码密集页)。图多的报告必须看图后再评分。一次最多4页。",
        {
            "type": "object",
            "properties": {
                "page_nums": {"type": "array", "items": {"type": "integer"}, "description": "要查看的页码列表"},
            },
            "required": ["page_nums"],
        }, _view_pages,
    ),
    "check_similarity": Tool(
        "check_similarity",
        "检查本报告与本批其他报告的相似度, 作为查重线索(不要仅凭话题相似判抄袭)。",
        {"type": "object", "properties": {"threshold": {"type": "number", "description": "相似度阈值, 默认0.8"}}},
        _check_similarity,
    ),
    "submit_grade": Tool(
        "submit_grade",
        "提交最终评分。必须包含总分、评语(不超过20字)、给分依据, 以及覆盖评分标准每个维度的 dimensions(各维度得分之和应等于总分)。",
        {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "description": "总分 0-100"},
                "comment": {"type": "string", "description": "评语, <=20字, 写在报告上"},
                "reason": {"type": "string", "description": "给分依据, <=80字"},
                "dimensions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "score": {"type": "number"},
                            "full": {"type": "number"},
                            "comment": {"type": "string"},
                        },
                        "required": ["name", "score", "full"],
                    },
                },
            },
            "required": ["score", "comment", "reason", "dimensions"],
        }, _submit_grade,
    ),
}


def tool_schemas() -> list[dict]:
    from . import eval_policy
    import copy
    schemas = copy.deepcopy([t.schema() for t in TOOLS.values()])
    if eval_policy.instructions():
        submit = next(s["function"]["parameters"]["properties"] for s in schemas
                      if s["function"]["name"] == "submit_grade")
        submit.update(deductions={"type": "array", "items": {"type": "object", "properties": {
            "rule_id": {"type": "string"}, "evidence": {"type": "string"},
            "reason_code": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["rule_id", "evidence", "reason_code", "reason"]}},
            review_required={"type": "boolean"}, review_reason={"type": "string"})
    return schemas


def execute(ctx: ToolContext, name: str, args: dict) -> dict:
    tool = TOOLS.get(name)
    if not tool:
        return {"error": f"未知工具: {name}"}
    try:
        return tool.func(ctx, args or {})
    except Exception as exc:  # noqa: BLE001
        return {"error": f"工具执行失败: {exc}"}
