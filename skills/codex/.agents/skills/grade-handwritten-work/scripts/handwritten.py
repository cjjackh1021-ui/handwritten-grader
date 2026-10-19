"""Launch the shared grader in its own environment from any Codex working directory."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    skill = Path(__file__).resolve().parents[1]
    settings = skill / "local.json"
    configured = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    override = os.environ.get("GRADER_PROJECT_DIR") or configured.get("project_dir")
    candidates = [Path(override)] if override else list(Path(__file__).resolve().parents) + [Path.cwd()]
    root = next((p.resolve() for p in candidates if (p / "graders/codex_handwriting.py").is_file()), None)
    if root is None:
        raise SystemExit("未找到批改项目；运行项目 scripts/install_codex_skill.py 或设置 GRADER_PROJECT_DIR。")
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise SystemExit(f"主环境未初始化：{python}。按项目 README 安装 requirements.txt。")
    # Preserve caller-relative arguments while imports use the configured checkout.
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return subprocess.call([str(python), "-X", "utf8", "-m", "graders.codex_handwriting", *sys.argv[1:]],
                           env=env, stdout=sys.stdout, stderr=sys.stderr,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    sys.exit(main())
