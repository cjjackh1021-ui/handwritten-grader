"""框架版批改 Agent —— 基于 OpenAI Agents SDK 的对照实现。

与手写的 :mod:`graders.agent`(自己维护 messages / tool 循环 / 自校验) 相对照,
这里用官方 **OpenAI Agents SDK** 声明式地搭同一套「工具 + agent」, 由框架托管
ReAct 循环。目的是横向比较两种实现方式的取舍(可控性 vs 样板代码)。

要点:
- 通过 ``OpenAIChatCompletionsModel`` + 自定义 ``AsyncOpenAI(base_url, api_key)``
  复用现有 provider(DeepSeek/OpenAI/自定义, 均为 OpenAI 兼容)。
- 工具复用 :mod:`graders.agent_tools` 的执行逻辑, 通过 ``RunContextWrapper`` 携带
  单份报告的 :class:`ToolContext`。
- 关闭 SDK 自带 tracing(避免依赖 OpenAI 平台 key)。
- 视觉: 框架托管消息循环, 不便中途注入图像, 故 view_pages 回退到 OCR/文本;
  需要真·视觉复核时请用手写 agent(engine=agent) 或多智能体(engine=multi)。

返回结构与其它引擎一致: {score, comment, reason, dimensions, dimension_summary,
trace, steps, engine="framework"}。若未安装 SDK, :func:`grade_report_framework`
抛出 :class:`providers.ProviderError`。
"""

from __future__ import annotations

import json

from . import agent as _agent
from . import agent_tools, config, grading, providers
from .agent_tools import ToolContext

try:  # 惰性可选依赖
    from agents import (
        Agent,
        OpenAIChatCompletionsModel,
        RunContextWrapper,
        Runner,
        function_tool,
        set_tracing_disabled,
    )
    from openai import AsyncOpenAI

    _SDK_OK = True
    set_tracing_disabled(True)
except Exception as _exc:  # noqa: BLE001
    _SDK_OK = False
    _IMPORT_ERROR = str(_exc)


INSTRUCTIONS = """你是一位严谨的实验报告批改 Agent, 必须用工具收集证据后再打分:
- 先 get_report_info 了解结构, 用 read_text 读正文, 需要时 get_rubric 复看标准、view_pages 看图线索、check_similarity 查重。
- 最后调用 submit_grade 提交: 总分(0-100)、评语(≤20字)、给分依据(≤80字), 以及覆盖评分标准每个维度的
  dimensions(以 JSON 字符串传入, 各维度得分之和≈总分)。
评分从宽把握, 认真完成的报告尽量给80分以上。"""


if _SDK_OK:

    @function_tool(strict_mode=False)
    def get_rubric(wrapper: "RunContextWrapper[ToolContext]") -> str:
        """读取本次批改所用的评分标准全文。"""
        return json.dumps(agent_tools.execute(wrapper.context, "get_rubric", {}), ensure_ascii=False)

    @function_tool(strict_mode=False)
    def get_report_info(wrapper: "RunContextWrapper[ToolContext]") -> str:
        """获取报告元信息: 学生、总页数、每页字数与图片数、哪些页需要看图。"""
        return json.dumps(agent_tools.execute(wrapper.context, "get_report_info", {}), ensure_ascii=False)

    @function_tool(strict_mode=False)
    def read_text(wrapper: "RunContextWrapper[ToolContext]", page_num: int = 0, max_chars: int = 4000) -> str:
        """读取报告正文。page_num=0 读全文, 否则读指定页。"""
        args = {"max_chars": max_chars}
        if page_num:
            args["page_num"] = page_num
        return json.dumps(agent_tools.execute(wrapper.context, "read_text", args), ensure_ascii=False)

    @function_tool(strict_mode=False)
    def view_pages(wrapper: "RunContextWrapper[ToolContext]", page_nums: list[int]) -> str:
        """查看指定页面的线索(框架版回退为 OCR/文本), 适用于图表/截图/代码密集页。"""
        return json.dumps(agent_tools.execute(wrapper.context, "view_pages", {"page_nums": page_nums}), ensure_ascii=False)

    @function_tool(strict_mode=False)
    def check_similarity(wrapper: "RunContextWrapper[ToolContext]", threshold: float = 0.8) -> str:
        """检查本报告与本批其他报告的相似度, 作为查重线索。"""
        return json.dumps(agent_tools.execute(wrapper.context, "check_similarity", {"threshold": threshold}), ensure_ascii=False)

    @function_tool(strict_mode=False)
    def submit_grade(wrapper: "RunContextWrapper[ToolContext]", score: int, comment: str,
                     reason: str, dimensions_json: str = "[]") -> str:
        """提交最终评分。dimensions_json 为维度数组的 JSON 字符串:
        [{"name":..,"score":..,"full":..,"comment":..}], 各维度得分之和应等于总分。"""
        try:
            dims = json.loads(dimensions_json) if dimensions_json else []
        except json.JSONDecodeError:
            dims = []
        agent_tools.execute(wrapper.context, "submit_grade", {
            "score": score, "comment": comment, "reason": reason, "dimensions": dims,
        })
        return json.dumps({"received": True}, ensure_ascii=False)

    _TOOLS = [get_rubric, get_report_info, read_text, view_pages, check_similarity, submit_grade]


def _build_model(choice: providers.ModelChoice):
    choice = choice.resolved()
    if config.PROVIDERS.get(choice.provider, {}).get("kind") != "openai":
        raise providers.ProviderError("框架版仅支持 OpenAI 兼容 provider(DeepSeek/OpenAI/自定义)")
    client = AsyncOpenAI(base_url=choice.base_url, api_key=choice.api_key)
    return OpenAIChatCompletionsModel(model=choice.model, openai_client=client), choice


def _trace_from_result(result) -> list[dict]:
    """从 SDK RunResult.new_items 提炼一条轻量可视化轨迹。"""
    trace: list[dict] = []
    for item in getattr(result, "new_items", []) or []:
        itype = type(item).__name__
        raw = getattr(item, "raw_item", None)
        if itype == "ToolCallItem":
            name = getattr(raw, "name", "") or ""
            args = getattr(raw, "arguments", "") or ""
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                pass
            trace.append({"type": "tool_call", "name": name, "args": args})
        elif itype == "ToolCallOutputItem":
            out = getattr(item, "output", "")
            trace.append({"type": "tool_result", "name": "", "result": _agent._summarize_result(
                out if isinstance(out, dict) else {"output": str(out)})})
        elif itype == "MessageOutputItem":
            text = ""
            try:
                from agents import ItemHelpers
                text = ItemHelpers.text_message_output(item)
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                trace.append({"type": "thought", "text": text[:500]})
    return trace


def grade_report_framework(choice, criteria, report, other_texts=None, max_turns: int = 12) -> dict:
    """用 OpenAI Agents SDK 跑同一套工具流程, 返回与其它引擎一致的评分结构。"""
    if not _SDK_OK:
        raise providers.ProviderError(f"未安装 openai-agents SDK, 无法使用框架版: {_IMPORT_ERROR}")

    model, choice = _build_model(choice)
    ctx = ToolContext(report=report, criteria=criteria, use_vision=False,
                      other_texts=other_texts or [])
    agent_obj = Agent(name="LabReportGrader", instructions=INSTRUCTIONS,
                      tools=_TOOLS, model=model)
    user = (f"请批改学生【{report.student_name}】(学号 {report.student_id or '未知'})的实验报告, "
            f"共 {report.page_count} 页。先了解结构再读正文, 最后用 submit_grade 提交评分。")

    result = Runner.run_sync(agent_obj, user, context=ctx, max_turns=max_turns)
    trace = _trace_from_result(result)

    if ctx.submitted is None:
        raise providers.ProviderError("框架版 Agent 未产出评分(未调用 submit_grade)")
    normalized = _agent._normalize_submit(ctx.submitted)
    trace.append({"type": "final", "grade": {k: normalized[k] for k in ("score", "comment", "dimension_summary")}})
    steps = sum(1 for s in trace if s["type"] == "tool_call")
    return {**normalized, "trace": trace, "steps": steps, "engine": "framework"}
