"""评估指标：MAE / RMSE / 命中率 / 相关系数 / 维度命中率。

用于对比不同批改引擎(simple/agent/multi/framework)相对**人工标注**的接近程度，
量化「agent 是否优于单次调用」。
"""

from __future__ import annotations

import math


def mae(preds: list[float], trues: list[float]) -> float:
    """平均绝对误差。"""
    if not preds:
        return 0.0
    return sum(abs(p - t) for p, t in zip(preds, trues)) / len(preds)


def rmse(preds: list[float], trues: list[float]) -> float:
    """均方根误差。"""
    if not preds:
        return 0.0
    return math.sqrt(sum((p - t) ** 2 for p, t in zip(preds, trues)) / len(preds))


def within_tol(preds: list[float], trues: list[float], tol: float = 5.0) -> float:
    """|误差|≤tol 的比例(命中率)。"""
    if not preds:
        return 0.0
    return sum(1 for p, t in zip(preds, trues) if abs(p - t) <= tol) / len(preds)


def bias(preds: list[float], trues: list[float]) -> float:
    """系统性偏差(正=偏松, 负=偏严)。"""
    if not preds:
        return 0.0
    return sum(p - t for p, t in zip(preds, trues)) / len(preds)


def pearson(preds: list[float], trues: list[float]) -> float:
    """皮尔逊相关系数(与人工打分的一致性趋势)。"""
    n = len(preds)
    if n < 2:
        return 0.0
    mp = sum(preds) / n
    mt = sum(trues) / n
    cov = sum((p - mp) * (t - mt) for p, t in zip(preds, trues))
    vp = math.sqrt(sum((p - mp) ** 2 for p in preds))
    vt = math.sqrt(sum((t - mt) ** 2 for t in trues))
    if vp == 0 or vt == 0:
        return 0.0
    return cov / (vp * vt)


def dimension_hit_rate(pred_dims_list: list[list[dict]],
                       true_dims_list: list[list[dict]],
                       tol: float = 3.0) -> dict:
    """分维度命中率：按维度名匹配, 统计 |预测分-标注分|≤tol 的维度比例。

    返回 {"hit_rate": 命中比例, "matched": 已匹配维度数, "total": 标注维度总数}。
    仅统计标注里给了分维度的样本。
    """
    matched = 0
    hit = 0
    total = 0
    for pred_dims, true_dims in zip(pred_dims_list, true_dims_list):
        if not true_dims:
            continue
        pred_map = {_norm(d.get("name", "")): d.get("score") for d in (pred_dims or [])}
        for td in true_dims:
            total += 1
            name = _norm(td.get("name", ""))
            ps = pred_map.get(name)
            if ps is None:
                # 尝试宽松包含匹配
                ps = next((v for k, v in pred_map.items() if k and (k in name or name in k)), None)
            if ps is None:
                continue
            matched += 1
            try:
                if abs(float(ps) - float(td.get("score"))) <= tol:
                    hit += 1
            except (TypeError, ValueError):
                continue
    return {
        "hit_rate": (hit / total) if total else 0.0,
        "coverage": (matched / total) if total else 0.0,
        "hit": hit,
        "matched": matched,
        "total": total,
    }


def _norm(name: str) -> str:
    return "".join(str(name).split()).lower()


def summarize(preds: list[float], trues: list[float],
              pred_dims_list=None, true_dims_list=None,
              tol: float = 5.0, dim_tol: float = 3.0) -> dict:
    """把一个引擎的预测汇总成一组指标。"""
    out = {
        "n": len(preds),
        "mae": round(mae(preds, trues), 3),
        "rmse": round(rmse(preds, trues), 3),
        "acc@%g" % tol: round(within_tol(preds, trues, tol), 3),
        "bias": round(bias(preds, trues), 3),
        "pearson": round(pearson(preds, trues), 3),
    }
    if pred_dims_list is not None and true_dims_list is not None:
        dim = dimension_hit_rate(pred_dims_list, true_dims_list, dim_tol)
        out["dim_hit_rate@%g" % dim_tol] = round(dim["hit_rate"], 3)
        out["dim_coverage"] = round(dim["coverage"], 3)
        out["dim_matched"] = dim["matched"]
        out["dim_total"] = dim["total"]
    return out
