"""Run lab-report tools in the linked project's environment from any working directory."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

COMMANDS = {"convert": "convert_word_reports.py", "extract": "extract_reports.py",
            "stamp": "stamp_pdfs.py", "summary": "build_summary.py", "verify": "verify_outputs.py"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["doctor", "eval", *COMMANDS])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    skill = Path(__file__).resolve().parents[1]
    settings = skill / "local.json"
    configured = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
    override = os.environ.get("GRADER_PROJECT_DIR") or configured.get("project_dir")
    candidates = [Path(override)] if override else list(Path(__file__).resolve().parents)
    root = next((p.resolve() for p in candidates if (p / "graders/stamp_cli.py").is_file()), None)
    if root is None:
        raise SystemExit("未找到批改项目；从项目运行 scripts/install_codex_skill.py --skill grade-lab-reports，或设置 GRADER_PROJECT_DIR。")
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise SystemExit(f"主环境未初始化：{python}；请按项目 README 安装依赖。")
    env = dict(os.environ, GRADER_PROJECT_DIR=str(root), PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    if args.command == "doctor":
        code = ("import importlib.util,json,sys; from graders import config,local_workers; "
                "modules={m:bool(importlib.util.find_spec(m)) for m in ['pdfplumber','pypdf','reportlab','openpyxl','docx','pypdfium2']}; "
                "print(json.dumps({'project':str(config.BASE_DIR),'python':sys.executable,'modules':modules,"
                "'soffice':config.find_soffice(),'handwriting':local_workers.capabilities()['handwriting'],"
                "'grading':'current Codex session; no external grading API'},ensure_ascii=False)); "
                "sys.exit(0 if all(modules.values()) and local_workers.capabilities()['handwriting']['available'] else 1)")
        command = [str(python), "-X", "utf8", "-c", code]
    elif args.command == "eval":
        command = [str(python), "-X", "utf8", "-m", "eval.quality", *args.arguments]
    else:
        command = [str(python), "-X", "utf8", str(skill / "scripts" / COMMANDS[args.command]), *args.arguments]
    return subprocess.call(command, env=env, stdout=sys.stdout, stderr=sys.stderr,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    sys.exit(main())
