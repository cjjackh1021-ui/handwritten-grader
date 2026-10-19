"""Isolated, serial JSONL workers. No model imports or downloads in Flask."""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

from . import config

ROOT = config.BASE_DIR
ENGINES = ("paddle", "chandra", "handwriting")


def python_for(engine):
    override = os.getenv(f"{engine.upper()}_PYTHON")
    return Path(override) if override else ROOT / f".venv-{engine}" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def capabilities():
    manifest = ROOT / "assets" / "handwriting" / "manifest.json"
    result = {engine: {"available": python_for(engine).is_file() and
                    (manifest.is_file() if engine == "handwriting" else (ROOT / "models" / engine / "ready.json").is_file()),
                    "local": True} for engine in ENGINES}
    for engine, status in result.items():
        worker = globals().get("WORKERS", {}).get(engine)
        status["last_error"] = getattr(worker, "last_error", "")
        status["runtime_status"] = ("error" if status["last_error"] else "verified" if getattr(worker, "verified", False) else "not_checked")
    return result


class WorkerError(RuntimeError):
    pass


class Worker:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.process = None
        self.timer = None
        self.last_error = ""
        self.verified = False

    def stop(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
            if self.process:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                self.process.stdin.close()
                self.process.stdout.close()
                self.process = None

    def request(self, payload, timeout=None):
        timeout = timeout or int(os.getenv(f"{self.engine.upper()}_TIMEOUT", "240"))
        with self.lock:
            if self.timer:
                self.timer.cancel()
            if not capabilities()[self.engine]["available"]:
                raise WorkerError(f"{self.engine} 未初始化，请运行 scripts/setup_handwriting.py")
            if self.process is None or self.process.poll() is not None:
                env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                       "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                       "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True"}
                self.process = subprocess.Popen(
                    [str(python_for(self.engine)), str(ROOT / "workers" / "runner.py"), self.engine],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", cwd=str(ROOT), env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc = self.process
            responses = queue.Queue()
            def read():
                try:
                    responses.put(proc.stdout.readline())
                except Exception:
                    responses.put("")
            try:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                proc.stdin.flush()
                threading.Thread(target=read, daemon=True).start()
                line = responses.get(timeout=timeout)
                reply = json.loads(line)
                if not reply.get("ok"):
                    self.last_error = reply.get("error", "本地组件失败")
                    raise WorkerError(self.last_error)
                self.last_error = ""
                self.verified = True
                return reply["result"]
            except (queue.Empty, BrokenPipeError, ValueError, OSError) as exc:
                self.stop()
                self.last_error = f"{self.engine} 超时或进程异常，增强识别未完成"
                raise WorkerError(self.last_error) from exc
            finally:
                self.timer = threading.Timer(int(os.getenv("LOCAL_WORKER_IDLE_SECONDS", "60")), self.stop)
                self.timer.daemon = True
                self.timer.start()


WORKERS = {name: Worker(name) for name in ENGINES}
atexit.register(lambda: [w.stop() for w in WORKERS.values()])


def request(engine, payload, timeout=None):
    return WORKERS[engine].request(payload, timeout)
