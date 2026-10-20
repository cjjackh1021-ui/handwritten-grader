"""Install this checkout's grading skills without copying models or credentials."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", choices=("grade-handwritten-work", "grade-lab-reports"), default="grade-handwritten-work")
    parser.add_argument("--skills-dir", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills")
    parser.add_argument("--update", action="store_true", help="Update a copy installed from this same checkout")
    args = parser.parse_args()
    source = ROOT / "skills/codex/.agents/skills" / args.skill
    dest = args.skills_dir.resolve() / args.skill
    if dest.exists():
        local = dest / "local.json"
        known = json.loads(local.read_text(encoding="utf-8")) if local.exists() else {}
        if not args.update or known.get("project_dir") != str(ROOT):
            raise SystemExit("同名技能已存在；仅可用 --update 更新从当前项目安装的副本")
    shutil.copytree(source, dest, dirs_exist_ok=args.update, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "local.json"))
    (dest / "local.json").write_text(json.dumps({"project_dir": str(ROOT)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"skill": args.skill, "installed": str(dest), "project": str(ROOT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
