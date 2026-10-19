"""批改 Agent 的可训练奖励：结果奖 + 过程奖 + 格式奖。

这是后训练 / Agent RL 的核心，而不是「再包一层框架」。

- **结果奖 (outcome)**：相对人工标注的接近程度。MAE 越小越好，分维度命中加分。
- **格式奖 (format)**：总分范围、评语长度、维度之和、是否提交。对应 agent 自校验。
- **过程奖 (process)**：是否先看结构/读正文、图多是否 view_pages、步数是否过长、是否经过失败提交。

LLM-GRPO / RFT / DPO 都可以直接吃 ``score_rollout()`` 的标量奖励。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from eval.metrics import dimension_hit_rate


@dataclass
class RewardBreakdown:
    total: float
    outcome: float
    process: float
    format: float
    parts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "total": round(self.total, 4),
            "outcome": round(self.outcome, 4),
            "process": round(self.process, 4),
            "format": round(self.format, 4),
            "parts": self.parts,
        }


def _tools_used(trace: list[dict]) -> list[str]:
    return [s.get("name", "") for s in (trace or []) if s.get("type") == "tool_call"]


def outcome_reward(pred_score, true_score, pred_dims=None, true_dims=None,
                   mae_scale: float = 12.0, dim_tol: float = 3.0) -> tuple[float, dict]:
    """结果奖 ∈ [0, 1]。无标注时返回 0.5（中性，不误导过程学习）。"""
    parts = {}
    if true_score is None:
        return 0.5, {"mae": None, "note": "无人工标注, 结果奖中性"}
    try:
        mae = abs(float(pred_score) - float(true_score))
    except (TypeError, ValueError):
        mae = 50.0
    closeness = math.exp(-mae / mae_scale)
    parts["mae"] = round(mae, 3)
    parts["closeness"] = round(closeness, 4)
    dim_bonus = 0.0
    if true_dims:
        hit = dimension_hit_rate([pred_dims or []], [true_dims], tol=dim_tol)
        dim_bonus = hit["hit_rate"]
        parts["dim_hit_rate"] = round(dim_bonus, 4)
    # 接近度为主, 维度命中为辅
    value = 0.7 * closeness + 0.3 * (dim_bonus if true_dims else closeness)
    return float(value), parts


def format_reward(grade: dict | None, trace: list[dict] | None = None) -> tuple[float, dict]:
    """格式奖 ∈ [0, 1]。对齐 graders.agent._verify 的约束。"""
    parts = {"submitted": False}
    if not grade:
        return 0.0, parts
    parts["submitted"] = True
    score = 1.0
    try:
        s = float(grade.get("score", 0))
        if not (0 <= s <= 100):
            score -= 0.4
            parts["score_range"] = False
        else:
            parts["score_range"] = True
    except (TypeError, ValueError):
        score -= 0.4
        parts["score_range"] = False

    comment = str(grade.get("comment", "") or "").strip()
    comment = "".join(comment.split())
    if not comment:
        score -= 0.3
        parts["comment_ok"] = False
    elif len(comment) > 20:
        score -= 0.2
        parts["comment_ok"] = False
    else:
        parts["comment_ok"] = True

    dims = grade.get("dimensions") or []
    if not dims:
        score -= 0.3
        parts["dims_ok"] = False
    else:
        total = 0.0
        ok_sum = True
        for d in dims:
            if d.get("score") is None:
                ok_sum = False
                break
            try:
                total += float(d["score"])
            except (TypeError, ValueError):
                ok_sum = False
                break
        if ok_sum:
            try:
                match = abs(total - float(grade.get("score", 0))) <= 2
            except (TypeError, ValueError):
                match = False
            parts["dims_sum_ok"] = match
            if not match:
                score -= 0.2
        else:
            parts["dims_sum_ok"] = False
            score -= 0.2
        parts["dims_ok"] = True

    n_reflect = sum(1 for s in (trace or []) if s.get("type") == "reflect")
    parts["n_reflect"] = n_reflect
    if n_reflect:
        score -= min(0.15, 0.05 * n_reflect)
    return max(0.0, min(1.0, score)), parts


def process_reward(trace: list[dict], report=None, max_steps: int = 8) -> tuple[float, dict]:
    """过程奖 ∈ [0, 1]。奖励「先收集证据再打分」的工具使用。"""
    used = _tools_used(trace)
    used_set = set(used)
    parts: dict = {"tools": used}
    score = 0.2  # 基础分, 避免全零梯度

    if "get_report_info" in used_set:
        score += 0.2
        parts["got_info"] = True
    if "read_text" in used_set:
        score += 0.2
        parts["read_text"] = True
    if "get_rubric" in used_set:
        score += 0.05
        parts["got_rubric"] = True

    needs_visual = False
    if report is not None:
        needs_visual = any(getattr(p, "needs_visual", False) for p in getattr(report, "pages", []) or [])
    parts["needs_visual"] = needs_visual
    if needs_visual:
        if "view_pages" in used_set:
            score += 0.25
            parts["viewed"] = True
        else:
            score -= 0.2
            parts["viewed"] = False
    elif "view_pages" in used_set:
        # 无图却看图: 轻微浪费, 不重罚
        score -= 0.02
        parts["viewed_unneeded"] = True

    if "submit_grade" in used_set:
        score += 0.15
        parts["submitted"] = True
    else:
        score -= 0.3
        parts["submitted"] = False

    n_steps = sum(1 for s in (trace or []) if s.get("type") == "tool_call")
    parts["n_tool_calls"] = n_steps
    if n_steps > max_steps:
        score -= 0.1
    elif n_steps >= 2:
        score += 0.05  # 至少有「观察 + 提交」

    return max(0.0, min(1.0, score)), parts


def score_rollout(grade: dict | None, label: dict | None, trace: list[dict] | None = None,
                  report=None, weights: tuple[float, float, float] = (0.55, 0.25, 0.20)
                  ) -> RewardBreakdown:
    """综合奖励。默认结果 0.55 / 过程 0.25 / 格式 0.20。

    无人工标注时结果项为中性 0.5, 过程+格式仍可学「像一个合格批改 agent」。
    """
    label = label or {}
    true_score = label.get("score")
    true_dims = label.get("dimensions")
    pred_score = (grade or {}).get("score")
    pred_dims = (grade or {}).get("dimensions")
    o, o_parts = outcome_reward(pred_score, true_score, pred_dims, true_dims)
    p, p_parts = process_reward(trace or [], report=report)
    f, f_parts = format_reward(grade, trace)
    wo, wp, wf = weights
    total = wo * o + wp * p + wf * f
    return RewardBreakdown(
        total=total, outcome=o, process=p, format=f,
        parts={"outcome": o_parts, "process": p_parts, "format": f_parts},
    )
