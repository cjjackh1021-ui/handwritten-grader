"""Run legacy function tests and new regressions in an isolated output directory."""
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ["JOB_TTL_DAYS"]="0"
os.environ["HANDWRITING_OCR_MODE"]="off"
from graders import config
with tempfile.TemporaryDirectory(prefix="grader-tests-") as td:
    config.OUTPUT_DIR=Path(td);config.WORK_DIR=Path(td)/"_work"
    suite=unittest.TestSuite()
    for name in ("test_assignments.py","test_autodetect.py","test_deepseek_vision.py","test_handwriting.py","test_codex_handwriting.py","test_eval_quality.py"):
        namespace=runpy.run_path(str(ROOT/"tests"/name))
        for key,value in namespace.items():
            if key.startswith("test_") and callable(value):suite.addTest(unittest.FunctionTestCase(value))
            elif isinstance(value,type) and issubclass(value,unittest.TestCase):suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(value))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
