"""多智能体批改：Grader（批改）+ Reviewer（复核）。

单次调用/单 agent 的通病是「一锤定音」，缺少交叉复核。这里引入第二个智能体做
**独立复核**：Reviewer 拿到评分标准、报告正文与 Grader 的评分，判断给分是否合理、
维度是否自洽、评语是否贴切，必要时给出**修正后的最终评分**并说明理由。

流水线::

    Grader（agent 或单次） ──grade──▶ Reviewer（独立复核，可修正） ──▶ 最终评分

- provider 为 OpenAI 兼容(DeepSeek/OpenAI/自定义)时, Grader 默认走 :func:`agent.grade_report_agent`
  (带工具调用与自校验), 否则回退 :func:`grading.grade_report` 单次调用。
- Reviewer 走 :func:`providers.grade` 单次 JSON, 全 provider 通用(含 Anthropic)。

返回结构在 Grader 结果基础上追加 ``review`` 元信息与合并后的 ``trace``, ``engine="multi"``。
"""

from __future__ import annotations

import json
import re

from . import agent, config, grading, providers

REVIEW_MAX_TEXT = 6000

REVIEWER_SYSTEM = """你是一位资深的实验报告【复核】老师。另一位老师已经给出初评, 你的职责不是重新从零打分,
而是**独立审阅**初评是否公允、是否符合评分标准, 并在必要时修正。请重点检查:
1. 总分是否与报告的实际质量、评分标准相符(过松/过严都要纠正);
2. 各维度得分是否有依据、之和是否等于总分;
3. 评语是否贴切(≤20字), 给分依据是否站得住脚。
只返回一个 JSON 对象:
{
  "score": 复核后的最终总分(0-100),
  "comment": "最终评语(≤20字)",
  "reason": "最终给分依据(≤80字)",
  "dimensions": [{"name":..., "score":..., "full":..., "comment":...}],
  "agree": true/false,           // 是否认同初评总分(±2分内视为认同)
  "review_notes": "复核意见(说明维持或修正的理由, ≤60字)"
}
维持初评时 score 保持不变即可; 修正时给出你认为正确的分数。dimensions 需覆盖评分标准每个维度。"""


def _build_reviewer_user(criteria: str, report, grader_result: dict) -> str:
    dims = grader_result.get("dimensions") or []
    dim_line = grading.format_dimensions(dims) or "(初评未给出分维度)"
    return (
        f"学生：{report.student_name}\n"
        f"评分标准：\n{criteria}\n\n"
        f"报告正文(截断)：\n{report.full_text[:REVIEW_MAX_TEXT]}\n\n"
        "另一位老师的初评：\n"
        f"- 总分：{grader_result.get('score')}\n"
        f"- 评语：{grader_result.get('comment', '')}\n"
        f"- 依据：{grader_result.get('reason', '')}\n"
        f"- 分维度：{dim_line}\n\n"
        "请独立复核上述初评是否合理, 按系统要求返回最终评分 JSON。"
    )


def _parse_review_meta(raw: str) -> dict:
    """从 Reviewer 原始输出里补取 agree / review_notes(providers.grade 不解析这两个字段)。"""
    meta = {"agree": None, "review_notes": ""}
    if not raw:
        return meta
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return meta
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return meta
    if isinstance(data.get("agree"), bool):
        meta["agree"] = data["agree"]
    meta["review_notes"] = str(data.get("review_notes", "") or "").strip()
    return meta


def _grade_with_grader(choice, criteria, report, other_texts):
    """Grader 阶段：OpenAI 兼容用 agent, 否则单次调用。返回 (result, grader_kind)。"""
    kind = config.PROVIDERS.get(choice.provider, {}).get("kind", "openai")
    if kind == "openai":
        try:
            return agent.grade_report_agent(choice, criteria, report, other_texts=other_texts), "agent"
        except providers.ProviderError:
            pass  # 回退单次
    return grading.grade_report(choice, criteria, report), "simple"


def grade_report_multi(choice, criteria, report, other_texts=None, max_steps: int = agent.MAX_STEPS) -> dict:
    """Grader 初评 → Reviewer 复核修正, 返回带 review 元信息与合并 trace 的最终评分。"""
    grader_result, grader_kind = _grade_with_grader(choice, criteria, report, other_texts or [])
    grader_score = int(grader_result.get("score", 0) or 0)

    trace = list(grader_result.get("trace") or [])
    if not trace:
        # 单次 Grader 没有 trace, 造一个初评节点便于展示
        trace.append({"type": "final", "grade": {
            "score": grader_score, "comment": grader_result.get("comment", ""),
            "dimension_summary": grader_result.get("dimension_summary", ""),
        }})

    # ---- Reviewer 复核 ----
    reviewer_user = _build_reviewer_user(criteria, report, grader_result)
    try:
        reviewed = providers.grade(choice, REVIEWER_SYSTEM, reviewer_user)
    except providers.ProviderError as exc:
        # 复核失败: 保留初评, 记一笔
        trace.append({"type": "review", "ok": False, "notes": f"复核调用失败, 保留初评: {exc}"})
        out = dict(grader_result)
        out.update({"engine": "multi", "trace": trace,
                    "messages": grader_result.get("messages") or [],
                    "review": {"grader_score": grader_score, "reviewer_score": grader_score,
                               "delta": 0, "agree": None, "notes": f"复核失败: {exc}", "grader_kind": grader_kind}})
        return out

    meta = _parse_review_meta(reviewed.get("raw", ""))
    reviewer_score = int(reviewed.get("score", 0) or 0)
    delta = reviewer_score - grader_score
    dims = reviewed.get("dimensions") or grader_result.get("dimensions") or []
    dim_summary = grading.format_dimensions(dims)

    review_info = {
        "grader_score": grader_score,
        "reviewer_score": reviewer_score,
        "delta": delta,
        "agree": meta["agree"] if meta["agree"] is not None else (abs(delta) <= 2),
        "notes": meta["review_notes"],
        "grader_kind": grader_kind,
    }
    trace.append({"type": "review", "ok": True, "grader_score": grader_score,
                  "reviewer_score": reviewer_score, "delta": delta,
                  "agree": review_info["agree"], "notes": review_info["notes"]})
    trace.append({"type": "final", "grade": {"score": reviewer_score,
                  "comment": reviewed.get("comment", ""), "dimension_summary": dim_summary}})

    steps = grader_result.get("steps")
    return {
        "score": reviewer_score,
        "comment": reviewed.get("comment", "") or grader_result.get("comment", ""),
        "reason": reviewed.get("reason", "") or grader_result.get("reason", ""),
        "dimensions": dims,
        "dimension_summary": dim_summary,
        "trace": trace,
        "steps": (steps + 1) if isinstance(steps, int) else None,
        "engine": "multi",
        "review": review_info,
        "messages": grader_result.get("messages") or [],
    }
