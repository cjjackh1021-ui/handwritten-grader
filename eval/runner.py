"""Real-entry subprocess runner, durable per-attempt records, and local reports."""
import copy
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

from graders import eval_policy, providers, config
from . import judge
from .suites import check_split, digest, load, read_json, safe_file, write_json

ROOT = Path(__file__).resolve().parents[1]


def implementation_hash():
    files = [*sorted((ROOT / "graders").glob("*.py")), *sorted((ROOT / "eval").glob("*.py"))]
    return digest({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


def run(suite_dir, split_path, candidate_path, out, *, partition="search", repeats=1,
        timeout=180, provider="deepseek", model="", base_url="", use_vision=False,
        ocr_mode="off", engine="agent", replay=None, final_selection=None):
    if not isinstance(repeats, int) or not 1 <= repeats <= 100 or not 0 < timeout <= 3600:
        raise ValueError("重复次数须为 1..100，超时须为 0..3600 秒")
    if provider not in ("deepseek", "openai", "anthropic", "custom"):
        raise ValueError("不支持的服务商")
    if engine not in ("simple", "agent") or ocr_mode not in ("off", "tesseract", "paddle", "hybrid"):
        raise ValueError("不支持的引擎或 OCR 模式")
    parsed = urlsplit(base_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url 不得包含凭据、查询参数或片段")
    suite = load(suite_dir)
    split = read_json(split_path)
    check_split(suite, split)
    if partition not in ("search", "validation", "locked-test"):
        raise ValueError("未知数据划分")
    candidate = eval_policy.validate(read_json(candidate_path))
    audit = judge.audit(suite)
    if not audit["healthy"]:
        raise ValueError("评测裁判健康检查未通过，先补齐或修正对照标注")
    mode = "replay" if replay else "live"
    choice = providers.ModelChoice(provider=provider, api_key="", model=model,
                                   base_url=base_url, use_vision=use_vision).resolved()
    if mode == "live" and not os.getenv(config.PROVIDERS[provider]["env_key"]):
        raise ValueError("真实评测需要服务商 API Key 环境变量；离线功能验证请使用 --replay")
    fixture = read_json(replay) if replay else None
    options = dict(provider=provider, model=choice.model, base_url=choice.base_url, use_vision=choice.use_vision,
                   ocr_mode=ocr_mode, engine=engine if suite["manifest"]["task_type"] == "lab" else "assignment")
    code_hash = implementation_hash()
    if partition == "locked-test":
        if not final_selection:
            raise ValueError("locked-test 仅供定稿确认；先完成 validation 比较并提供 --final-selection")
        selection = read_json(final_selection)
        if (not selection.get("adopt_candidate") or selection.get("dataset") != suite["fingerprint"]
                or selection.get("implementation") != code_hash or mode != "live"
                or selection.get("partition") != "validation" or selection.get("timeout") != timeout
                or digest(candidate) not in selection.get("candidate_hashes", [])
                or selection.get("options") != options or selection.get("split_digest") != digest(split)):
            raise ValueError("最终确认不匹配已冻结的验证集决策、候选版本或运行配置")
        if repeats != selection.get("repeats"):
            raise ValueError("最终确认必须沿用预先选定的重复次数")
        ledger = Path(split_path).with_suffix(".locked-test.json")
        lock = ledger.with_suffix(".mutex")
        acquired = False
        try:
            with lock.open("x", encoding="utf-8"):
                acquired = True
                used = read_json(ledger) if ledger.exists() else {
                    "selection": digest(selection), "candidates": []}
                if used["selection"] != digest(selection) or digest(candidate) in used["candidates"]:
                    raise ValueError("locked-test 已绑定另一选择或已使用该版本；不能反复窥视后继续调优")
                used["candidates"].append(digest(candidate))
                write_json(ledger, used)
        except FileExistsError:
            raise ValueError("另一个最终确认正在登记，请稍后重试") from None
        finally:
            # Only remove a lock owned by this invocation.
            if acquired:
                lock.unlink(missing_ok=True)
    cases = [c for c in suite["cases"] if split["case_assignments"][c["id"]] == partition]
    if not cases:
        raise ValueError("所选划分没有案例")
    destination = Path(out).resolve() / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    destination.mkdir(parents=True)
    snapshot = {
        "suite": suite["manifest"], "dataset": suite["fingerprint"], "split_digest": digest(split),
        "implementation": code_hash, "candidate": candidate, "candidate_hash": digest(candidate),
        "mode": mode, "partition": partition, "repeats": repeats, "timeout": timeout, "options": options,
        "fixture_hash": digest(fixture) if fixture is not None else None,
        "human_verified": all(suite["labels"][c["id"]]["human_verified"] for c in suite["cases"]),
        "groups_per_split": {s: len({c["group_id"] for c in suite["cases"] if split["case_assignments"][c["id"]] == s})
                             for s in ("search", "validation", "locked-test")},
    }
    write_json(destination / "config.json", snapshot)
    write_json(destination / "audit.json", audit)
    records = []
    for case in cases:
        for repeat in range(repeats):
            work = destination / "attempts" / f"{case['id']}-{repeat}"
            work.mkdir(parents=True)
            public = copy.deepcopy(case["input"])
            paths = []
            for index, relative in enumerate(public.get("files", [])):
                source = safe_file(suite["root"], relative)
                copied = work / f"source-{index}{source.suffix}"
                shutil.copy2(source, copied)
                paths.append(str(copied))
            public["files"] = paths
            payload = {"input": public, "candidate": candidate, "task_type": suite["manifest"]["task_type"],
                       "options": options, "mode": mode}
            if fixture is not None:
                payload["replies"] = fixture.get(case["id"], [])
            write_json(work / "input.json", payload)
            started = time.monotonic()
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", JOB_TTL_DAYS="0",
                       PYTHONPATH=str(ROOT), HANDWRITING_OCR_MODE=ocr_mode)
            status = {"status": "runner_error", "output": None}
            try:
                proc = subprocess.Popen([sys.executable, "-m", "eval.worker", "--input", str(work / "input.json"),
                                       "--output", str(work / "output.json")], cwd=work, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=os.name != "nt",
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                proc.wait(timeout=timeout)
                if proc.returncode == 0 and (work / "output.json").is_file():
                    status = read_json(work / "output.json")
                    if not isinstance(status, dict) or status.get("status") not in (
                            "completed", "timeout", "service_error", "invalid_output", "truncated", "runner_error"):
                        status = {"status": "runner_error", "output": None}
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                status = {"status": "timeout", "output": None}
            except (OSError, ValueError):
                status = {"status": "runner_error", "output": None}
            calls_by_id = {}
            if (work / "calls.jsonl").exists():
                for line in (work / "calls.jsonl").read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                        calls_by_id[event["attempt_id"]] = event
                    except json.JSONDecodeError:
                        pass  # A terminated in-flight append is not counted as known usage.
            calls = list(calls_by_id.values())
            label = suite["labels"][case["id"]]
            record = {"case_id": case["id"], "group_id": case["group_id"], "repeat": repeat,
                      "review_expected": label["review_required"], "full_score": sum(r["full"] for r in public["rules"]),
                      "seconds": time.monotonic()-started, "calls": calls, **status}
            if status["status"] == "completed":
                try:
                    record["judgment"] = judge.judge(case, label, status["output"])
                except Exception as exc:
                    record["judge_error"] = type(exc).__name__
            records.append(record)
            with (destination / "records.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    report = {**snapshot, "metrics": judge.summarize(records), "records_sha256": hashlib.sha256((destination / "records.jsonl").read_bytes()).hexdigest(),
              "claim": "探索/功能验证；不能据此宣称真实准确率提高" if mode == "replay" or not snapshot["human_verified"] else "单版本评测；提升结论须经过配对比较"}
    write_json(destination / "report.json", report)
    render_report(destination, report, records)
    return destination


def render_report(destination, report, records):
    esc = lambda v: html.escape(str(v))
    parts = ["<!doctype html><meta charset='utf-8'><title>批改质量评测</title>",
             "<style>body{max-width:1100px;margin:32px auto;font:16px system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{border:1px solid #ccc;padding:12px;margin:10px 0}.bad{color:#b42318}</style>",
             "<h1>批改质量评测</h1><p>" + esc(report["claim"]) + "</p>",
             "<h2>全部请求口径</h2><pre>" + esc(json.dumps(report["metrics"], ensure_ascii=False, indent=2)) + "</pre>"]
    for r in records:
        work = destination / "attempts" / f"{r['case_id']}-{r['repeat']}"
        parts.append("<details><summary>" + esc(f"{r['case_id']} #{r['repeat']} · {r['status']}") + "</summary>")
        for p in sorted(work.glob("source-*")):
            href = p.relative_to(destination).as_posix()
            parts.append(f'<p><a href="{esc(href)}">原始输入 {esc(p.name)}</a></p>')
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                parts.append(f'<img style="max-width:100%" src="{esc(href)}">')
        public = read_json(work / "input.json")["input"]
        parts.append("<h3>学生原文</h3><pre>" + esc(public.get("answer", "")) + "</pre>")
        parts.append("<h3>批改、扣分证据、工具轨迹与判定</h3><pre>" + esc(json.dumps(r, ensure_ascii=False, indent=2)) + "</pre></details>")
    (destination / "report.html").write_text("\n".join(parts), encoding="utf-8")
