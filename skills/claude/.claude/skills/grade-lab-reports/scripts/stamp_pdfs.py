#!/usr/bin/env python3
"""Shared handwriting-web renderer; requires this project and initialized local fonts."""
import os
from pathlib import Path
import sys
candidates = [Path(os.environ['GRADER_PROJECT_DIR'])] if os.getenv('GRADER_PROJECT_DIR') else []
candidates += list(Path(__file__).resolve().parents)
root = next((p for p in candidates if (p/'graders/stamp_cli.py').is_file()), None)
if root is None:
    raise SystemExit('Set GRADER_PROJECT_DIR to the grading project and initialize scripts/setup_handwriting.py --components handwriting')
sys.path.insert(0,str(root))
from graders.stamp_cli import main
if __name__ == '__main__':
    sys.exit(main())
