"""One isolated grading invocation. Input contains public material only."""
import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

from graders import agent, assignments, config, convert, eval_policy, extract, grading, providers, telemetry
from .suites import read_json, write_json


def report_from_input(inp, work, ocr_mode):
    reports = []
    for name in inp.get("files", []):
        path = Path(name)
        if path.suffix.lower() == ".docx":
            converted = convert.convert_docx(path, work / "converted")
            if not converted.pdf_path:
                raise ValueError("Word 转换失败")
            path = Path(converted.pdf_path)
        if path.suffix.lower() == ".pdf":
            report = extract.inspect_pdf(path, work, ocr_enabled=ocr_mode != "off",
                                         render_for_vision=True, ocr_mode=ocr_mode)
        elif path.suffix.lower() in extract.IMAGE_EXT:
            report = extract.inspect_image(path, work, ocr_enabled=ocr_mode != "off", ocr_mode=ocr_mode)
        elif path.suffix.lower() in (".txt", ".md"):
            report = extract.inspect_text_file(path)
        else:
            raise ValueError("不支持的评测输入格式")
        reports.append(report)
    if inp.get("answer") or not reports:
        path = work / "answer.txt"
        path.write_text(inp.get("answer", ""), encoding="utf-8")
        reports.insert(0, extract.inspect_text_file(path))
    report = reports[0] if len(reports) == 1 else extract.merge_reports(reports, work)
    report.student_id = inp["student_id"]
    report.student_name = inp.get("student_name", inp["student_id"])
    return report


def replay_transport(replies):
    """Replay provider responses through the real provider/agent parsers."""
    queue = list(replies)
    if not queue:
        raise ValueError("缺少离线模型响应")
    class Response:
        status_code = 200
        headers = {}
        text = ""
        def __init__(self, body):
            self.body = body
        def json(self):
            return self.body
    def post(url, *, headers, json, timeout):
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        message = reply if isinstance(reply, dict) and ("tool_calls" in reply or "role" in reply) else {
            "role": "assistant", "content": __import__("json").dumps(reply, ensure_ascii=False)
            if isinstance(reply, dict) else reply}
        return Response({"model": "offline-replay", "choices": [{"message": message, "finish_reason": "stop"}]})
    return post


def run(payload, destination):
    work = destination.parent
    config.OUTPUT_DIR = work / "products"
    config.WORK_DIR = work / "work"
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    config.WORK_DIR.mkdir(exist_ok=True)
    calls = telemetry.Journal(work / "calls.jsonl")
    output = {"status": "runner_error", "output": None}
    try:
        options = payload["options"]
        report = report_from_input(payload["input"], config.WORK_DIR, options["ocr_mode"])
        preset = config.PROVIDERS.get(options["provider"], {})
        key = "offline-fixture" if payload["mode"] == "replay" else os.getenv(preset.get("env_key", ""), "")
        if not key:
            raise ValueError("未设置所选服务商的 API Key 环境变量")
        choice = providers.ModelChoice(provider=options["provider"], api_key=key, model=options["model"],
                                       base_url=options["base_url"], use_vision=options["use_vision"]).resolved()
        def invoke():
            if payload["task_type"] == "lab":
                fn = {"simple": grading.grade_report, "agent": agent.grade_report_agent}[options["engine"]]
                return fn(choice, payload["input"]["criteria"], report)
            return assignments.grade_submission(choice, assignments.TaskSpec.from_dict(payload["input"]["task"]), report)
        with telemetry.capture(calls), eval_policy.activate(payload["candidate"], payload["input"]["rules"]):
            if payload["mode"] == "replay":
                if config.PROVIDERS[choice.provider]["kind"] != "openai":
                    raise ValueError("离线 HTTP 回放仅使用 OpenAI 兼容协议")
                with patch.object(providers.requests, "post", replay_transport(payload["replies"])):
                    result = invoke()
            else:
                result = invoke()
        result["student_id"] = report.student_id
        result["student_name"] = report.student_name
        # Persist every grading/tool step, not private labels or image base64.
        result.pop("messages", None)
        output.update(status="completed", output=result)
        if calls and calls[-1]["finish_reason"] in ("length", "max_tokens"):
            output.update(status="truncated")
    except providers.RateLimitError as exc:
        output.update(status="timeout" if calls and all((c.get("error_type") or "").endswith("Timeout") for c in calls)
                      else "service_error", error=type(exc).__name__)
    except providers.ProviderError as exc:
        failure = "invalid_output" if "JSON" in str(exc) else "service_error"
        if calls and all((c.get("error_type") or "").endswith("Timeout") for c in calls):
            failure = "timeout"
        if calls and calls[-1].get("finish_reason") in ("length", "max_tokens"):
            failure = "truncated"
        output.update(status=failure, error=type(exc).__name__)
    except json.JSONDecodeError as exc:
        output.update(status="truncated" if calls and calls[-1].get("finish_reason") in ("length", "max_tokens")
                      else "invalid_output", error=type(exc).__name__)
    except Exception as exc:
        # Exception messages may include upstream HTTP secrets; persist the class
        # and safe local reason only, never response bodies or environment values.
        output.update(status="runner_error", error=type(exc).__name__)
    write_json(destination, output)
    return output


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    run(read_json(args.input), args.output)


if __name__ == "__main__":
    main()
