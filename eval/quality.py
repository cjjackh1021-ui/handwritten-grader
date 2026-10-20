"""CLI: python -m eval.quality {audit,split,run,compare}."""
import argparse
import json
from pathlib import Path

from . import judge, runner
from .compare import compare
from .suites import load, make_split, read_json, write_json


def main(argv=None):
    p = argparse.ArgumentParser(description="批改 Agent 的独立质量评测与有限候选迭代")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("audit", "split"):
        s = sub.add_parser(name)
        s.add_argument("--suite", type=Path, required=True)
        s.add_argument("--out", type=Path, required=True)
        if name == "split":
            s.add_argument("--seed", type=int, default=42)
    s = sub.add_parser("run")
    for name in ("suite", "split-file", "candidate", "out"):
        s.add_argument("--"+name, type=Path, required=True)
    s.add_argument("--partition", choices=("search", "validation", "locked-test"), default="search")
    s.add_argument("--repeats", type=int, default=1)
    s.add_argument("--timeout", type=float, default=180)
    s.add_argument("--provider", default="deepseek")
    s.add_argument("--model", default="")
    s.add_argument("--base-url", default="")
    s.add_argument("--vision", action="store_true")
    s.add_argument("--ocr-mode", default="off")
    s.add_argument("--engine", choices=("agent", "simple"), default="agent")
    s.add_argument("--replay", type=Path)
    s.add_argument("--final-selection", type=Path)
    s = sub.add_parser("compare")
    for name in ("baseline", "candidate", "out"):
        s.add_argument("--"+name, type=Path, required=True)
    s.add_argument("--guards", type=Path)
    s.add_argument("--hypothesis", default="")
    s.add_argument("--history", type=Path)
    s.add_argument("--max-rounds", type=int, default=5)
    args = p.parse_args(argv)
    try:
        if args.command == "audit":
            result = judge.audit(load(args.suite))
            write_json(args.out, result)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["healthy"] else 1
        if args.command == "split":
            write_json(args.out, make_split(load(args.suite), args.seed))
            print(str(args.out))
        elif args.command == "run":
            result = runner.run(args.suite, args.split_file, args.candidate, args.out,
                partition=args.partition, repeats=args.repeats, timeout=args.timeout, provider=args.provider,
                model=args.model, base_url=args.base_url, use_vision=args.vision, ocr_mode=args.ocr_mode,
                engine=args.engine, replay=args.replay, final_selection=args.final_selection)
            print(str(result))
        elif args.command == "compare":
            result = compare(args.baseline, args.candidate, args.out,
                guards=read_json(args.guards) if args.guards else None, hypothesis=args.hypothesis,
                history=args.history, max_rounds=args.max_rounds)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        p.exit(2, f"评测配置错误：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
