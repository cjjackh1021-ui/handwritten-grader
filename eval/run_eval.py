"""评估入口：对比不同批改引擎相对人工标注的 MAE / 维度命中率等指标。

用法示例::

    # 离线演示(无需 API, 走 mock 批改器, 证明评估流水线可用)
    python -m eval.run_eval --dataset eval/sample_dataset --engines simple,agent,multi --mock

    # 真实评估(用某 provider 的 key 实际跑各引擎)
    python -m eval.run_eval --dataset eval/sample_dataset \
        --engines simple,agent,multi,framework \
        --provider deepseek --api-key sk-xxx

产出:
    - 终端打印 Markdown 指标表
    - <out>/report.json 与 <out>/report.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval import dataset as ds  # noqa: E402
from eval import metrics as M  # noqa: E402
from graders import config, providers  # noqa: E402

ENGINES = ["simple", "agent", "multi", "framework"]


def _grade_real(engine, choice, criteria, report, others):
    from graders import agent, agent_framework, grading, multi_agent

    if engine == "agent":
        return agent.grade_report_agent(choice, criteria, report, other_texts=others)
    if engine == "multi":
        return multi_agent.grade_report_multi(choice, criteria, report, other_texts=others)
    if engine == "framework":
        return agent_framework.grade_report_framework(choice, criteria, report, other_texts=others)
    return grading.grade_report(choice, criteria, report)


def run_engine(engine, items, criteria, choice, mock: bool) -> dict:
    """跑一个引擎, 返回预测与指标。"""
    preds, trues = [], []
    pred_dims, true_dims = [], []
    per_item = []
    others = [{"student": it["report"].student_name, "text": it["report"].full_text} for it in items]
    errors = 0
    t0 = time.time()
    for idx, it in enumerate(items):
        report, label = it["report"], it["label"]
        try:
            if mock:
                res = ds.mock_grade(report, label, engine)
            else:
                other_texts = [o for j, o in enumerate(others) if j != idx]
                res = _grade_real(engine, choice, criteria, report, other_texts)
            pred = float(res.get("score", 0))
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"  [err] {engine} / {it['file']}: {exc}")
            continue
        preds.append(pred)
        trues.append(float(label.get("score", 0)))
        pred_dims.append(res.get("dimensions") or [])
        true_dims.append(label.get("dimensions") or [])
        per_item.append({"file": it["file"], "true": label.get("score"),
                         "pred": pred, "abs_err": abs(pred - float(label.get("score", 0)))})
    elapsed = round(time.time() - t0, 2)
    stats = M.summarize(preds, trues, pred_dims, true_dims)
    stats["errors"] = errors
    stats["seconds"] = elapsed
    return {"metrics": stats, "items": per_item}


def _fmt_table(results: dict) -> str:
    """把各引擎指标拼成 Markdown 表。"""
    cols = ["mae", "rmse", "acc@5", "bias", "pearson", "dim_hit_rate@3", "dim_coverage", "errors", "seconds"]
    header = "| engine | " + " | ".join(cols) + " |"
    sep = "|" + "---|" * (len(cols) + 1)
    lines = [header, sep]
    for engine, data in results.items():
        m = data["metrics"]
        row = [engine] + [str(m.get(c, "")) for c in cols]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _best_line(results: dict) -> str:
    ranked = sorted(results.items(), key=lambda kv: kv[1]["metrics"].get("mae", 9e9))
    if not ranked:
        return ""
    best, worst = ranked[0], ranked[-1]
    bm, wm = best[1]["metrics"]["mae"], worst[1]["metrics"]["mae"]
    delta = round(wm - bm, 3)
    return (f"**MAE 最低(最接近人工)**: `{best[0]}` = {bm}；"
            f"相比最高的 `{worst[0]}`({wm}) 降低 {delta} 分。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="批改引擎评估：MAE / 维度命中率对比")
    ap.add_argument("--dataset", required=True, help="数据集目录(含 labels.json)")
    ap.add_argument("--engines", default="simple,agent,multi",
                    help="逗号分隔, 可选 " + "/".join(ENGINES))
    ap.add_argument("--mock", action="store_true", help="离线模拟(不调用 API)")
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--criteria", default="", help="评分标准文件, 默认用内置 references 标准")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "reports"))
    args = ap.parse_args(argv)

    engines = [e.strip() for e in args.engines.split(",") if e.strip() in ENGINES]
    if not engines:
        print("没有有效引擎")
        return 2

    print(f"加载数据集: {args.dataset}")
    items = ds.load_dataset(Path(args.dataset))
    print(f"  共 {len(items)} 份带标注的报告")
    if not items:
        return 2

    criteria = ""
    if args.criteria and Path(args.criteria).is_file():
        criteria = Path(args.criteria).read_text(encoding="utf-8")
    elif config.DEFAULT_CRITERIA_FILE.is_file():
        criteria = config.DEFAULT_CRITERIA_FILE.read_text(encoding="utf-8")

    choice = None
    if not args.mock:
        api_key = args.api_key or os.getenv(
            config.PROVIDERS.get(args.provider, {}).get("env_key", ""), "")
        if not api_key:
            print("真实评估需要 --api-key(或设置对应环境变量), 或改用 --mock")
            return 2
        choice = providers.ModelChoice(provider=args.provider, api_key=api_key,
                                       model=args.model, base_url=args.base_url, use_vision=True)

    results = {}
    for engine in engines:
        print(f"\n== 运行引擎: {engine} {'(mock)' if args.mock else ''} ==")
        results[engine] = run_engine(engine, items, criteria, choice, args.mock)
        print("  指标:", json.dumps(results[engine]["metrics"], ensure_ascii=False))

    table = _fmt_table(results)
    best = _best_line(results)
    print("\n" + table + "\n\n" + best)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset": str(args.dataset),
        "mode": "mock" if args.mock else f"real:{args.provider}",
        "n": len(items),
        "results": results,
    }
    (out_dir / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = (f"# 批改引擎评估报告\n\n- 数据集: `{args.dataset}`（{len(items)} 份）\n"
          f"- 模式: {'离线模拟(mock)' if args.mock else '真实调用 ' + args.provider}\n\n"
          f"{table}\n\n{best}\n\n"
          "> 指标含义：MAE/RMSE 越低越好；acc@5=误差≤5分比例；bias 正=偏松；"
          "pearson 越高与人工趋势越一致；dim_hit_rate@3=分维度误差≤3分的命中率。\n")
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    print(f"\n已写出: {out_dir / 'report.json'} 与 {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
