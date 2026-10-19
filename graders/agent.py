"""批改 Agent —— 手写的最小 ReAct / tool-calling 循环。

与「一次 prompt → 一个 JSON」的 :func:`grading.grade_report` 不同, 这里模型自主决定
调用哪些工具(看结构、读正文、看图、查重), 循环 观察→思考→行动, 最后用 submit_grade
产出评分; 提交后做 **自校验**(维度分之和=总分、评语≤20字、图多必须看过图), 不通过则
把问题反馈给模型要求修正后重新提交。全过程记录为可解释的 **执行轨迹 trace**。

仅走 OpenAI 兼容的 tool calling(DeepSeek / OpenAI / 自定义)。
"""

from __future__ import annotations

import json

from . import agent_tools, grading, providers
from .agent_tools import ToolContext

MAX_STEPS = 8
SCORE_SUM_TOLERANCE = 2  # 各维度分之和与总分允许的偏差

SYSTEM_PROMPT = """你是一位严谨的实验报告批改 Agent。你不能只凭直觉打分, 必须用工具主动收集证据:
- 先调用 get_report_info 了解报告结构(页数、每页字数与图片数、哪些页需要看图)。
- 用 read_text 阅读正文; 对图片密集/图表/代码截图页, 必须调用 view_pages 看图后再评分。
- 需要时用 get_rubric 复看评分标准, 用 check_similarity 获取查重线索。
- 最后调用 submit_grade 提交: 总分(0-100)、评语(≤20字)、给分依据(≤80字), 以及覆盖评分标准
  每个维度的 dimensions, 且各维度得分之和应等于总分。
尽量用最少的步骤完成。评分从宽把握, 认真完成的报告尽量给80分以上。"""


def _initial_user(report) -> str:
    return (
        f"请批改学生【{report.student_name}】(学号 {report.student_id or '未知'})的实验报告, "
        f"共 {report.page_count} 页。先了解结构再按需读正文/看图, 最后用 submit_grade 提交评分。"
    )


def _summarize_result(result: dict) -> dict:
    """压缩工具结果用于 trace 展示(避免把整段文本塞进轨迹)。"""
    out = {}
    for k, v in (result or {}).items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + "…"
        elif isinstance(v, list) and len(v) > 8:
            out[k] = v[:8] + ["…"]
        else:
            out[k] = v
    return out


def _normalize_submit(sub: dict) -> dict:
    import re

    try:
        score = int(round(float(sub.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    comment = re.sub(r"\s+", "", str(sub.get("comment", "") or ""))
    reason = str(sub.get("reason", "") or "").strip()
    dims = []
    for d in sub.get("dimensions", []) or []:
        if not isinstance(d, dict):
            continue
        dims.append({
            "name": str(d.get("name", "")),
            "score": d.get("score"),
            "full": d.get("full"),
            "comment": str(d.get("comment", "") or ""),
        })
    return {
        "score": score,
        "comment": comment,
        "reason": reason,
        "dimensions": dims,
        "dimension_summary": grading.format_dimensions(dims),
    }


def _verify(ctx: ToolContext, choice) -> tuple[bool, str, dict]:
    normalized = _normalize_submit(ctx.submitted or {})
    issues = []
    if not (0 <= normalized["score"] <= 100):
        issues.append("总分需在0-100之间")
    if len(normalized["comment"]) == 0:
        issues.append("评语不能为空")
    if len(normalized["comment"]) > 20:
        issues.append(f"评语{len(normalized['comment'])}字, 超过20字上限")
    if not normalized["dimensions"]:
        issues.append("必须给出覆盖评分标准的 dimensions")
    else:
        s = 0.0
        ok_sum = True
        for d in normalized["dimensions"]:
            if d["score"] is None:
                ok_sum = False
                break
            s += float(d["score"])
        if ok_sum and abs(s - normalized["score"]) > SCORE_SUM_TOLERANCE:
            issues.append(f"各维度得分之和({s:g})与总分({normalized['score']})不一致")
    # 图多但没看图
    has_visual = any(p.needs_visual for p in ctx.report.pages)
    if has_visual and choice.use_vision and not ctx.viewed_pages:
        issues.append("报告含图片密集页, 请先用 view_pages 看图再评分")
    return (not issues), "; ".join(issues), normalized


def _strip_content(content):
    """去掉图像 base64, 截断过长文本, 使轨迹可落盘、可进 SFT。"""
    if isinstance(content, list):
        blocks = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                blocks.append({"type": "text", "text": str(block.get("text", ""))[:8000]})
            else:
                blocks.append({"type": "text", "text": "[页面图像已省略]"})
        return blocks
    if isinstance(content, str) and len(content) > 12000:
        return content[:12000] + "…"
    return content


def messages_for_export(messages: list[dict]) -> list[dict]:
    """真实 agent 对话的可序列化副本（供后训练, 不含图片字节）。"""
    out = []
    for msg in messages or []:
        item = {"role": msg.get("role"), "content": _strip_content(msg.get("content"))}
        if msg.get("tool_calls"):
            item["tool_calls"] = msg["tool_calls"]
        if msg.get("tool_call_id"):
            item["tool_call_id"] = msg["tool_call_id"]
        if msg.get("name"):
            item["name"] = msg["name"]
        out.append(item)
    return out


def _with_messages(payload: dict, messages: list[dict]) -> dict:
    payload["messages"] = messages_for_export(messages)
    return payload


def grade_report_agent(choice, criteria, report, other_texts=None, max_steps: int = MAX_STEPS) -> dict:
    """对单份报告运行 agent 循环, 返回 {score, comment, reason, dimensions, dimension_summary, trace, steps}。"""
    choice = choice.resolved()
    ctx = ToolContext(report=report, criteria=criteria, use_vision=choice.use_vision,
                      other_texts=other_texts or [])
    tools = agent_tools.tool_schemas()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _initial_user(report)},
    ]
    trace: list[dict] = []

    for step in range(max_steps):
        msg = providers.chat(choice, messages, tools=tools)
        messages.append(msg)  # 原样回填 assistant 消息(含 tool_calls)

        if msg.get("content"):
            trace.append({"type": "thought", "text": str(msg["content"])[:500]})

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            trace.append({"type": "note", "text": "未调用工具, 提示其提交评分"})
            messages.append({"role": "user", "content": "请通过 submit_grade 工具提交最终评分。"})
            continue

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            trace.append({"type": "tool_call", "name": name, "args": args})
            result = agent_tools.execute(ctx, name, args)
            trace.append({"type": "tool_result", "name": name, "result": _summarize_result(result)})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False),
            })

        # 注入本轮请求查看的页面图像
        if ctx.pending_images and choice.use_vision:
            blocks = providers.image_content_blocks(ctx.pending_images)
            if blocks:
                messages.append({"role": "user",
                                 "content": [{"type": "text", "text": "以下是你请求查看的页面图像："}] + blocks})
                trace.append({"type": "vision", "pages": sorted(ctx.viewed_pages), "count": len(blocks)})
            ctx.pending_images = []

        # 提交后自校验
        if ctx.submitted is not None:
            ok, issues, normalized = _verify(ctx, choice)
            if ok:
                trace.append({"type": "final", "grade": {k: normalized[k] for k in ("score", "comment", "dimension_summary")}})
                return _with_messages(
                    {**normalized, "trace": trace, "steps": step + 1, "engine": "agent"},
                    messages,
                )
            trace.append({"type": "reflect", "issues": issues})
            messages.append({"role": "user",
                             "content": f"你的提交未通过自校验：{issues}。请修正后重新调用 submit_grade。"})
            ctx.submitted = None

    # 步数用尽: 用最后一次提交(若有), 否则报错
    if ctx.submitted is not None:
        normalized = _normalize_submit(ctx.submitted)
        trace.append({"type": "final", "grade": {"score": normalized["score"]}, "warning": "达到最大步数"})
        return _with_messages(
            {**normalized, "trace": trace, "steps": max_steps, "engine": "agent",
             "warning": "达到最大步数, 采用最后一次提交"},
            messages,
        )
    raise providers.ProviderError("Agent 在最大步数内未产出评分")
