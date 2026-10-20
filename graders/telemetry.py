"""Optional, context-local provider telemetry. Never records credentials or prompts."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import uuid

_sink = ContextVar("grading_telemetry", default=None)


@contextmanager
def capture(sink=None):
    events = [] if sink is None else sink
    token = _sink.set(events)
    try:
        yield events
    finally:
        _sink.reset(token)


def begin_attempt(payload):
    sink = _sink.get()
    if sink is None:
        return None
    event = {
        "attempt_id": uuid.uuid4().hex, "pending": True,
        "requested_model": payload.get("model"),
        "actual_model": None, "usage": None, "finish_reason": None,
        "seconds": None, "http_status": None, "error_type": None,
        "parameters": {k: payload[k] for k in ("temperature", "max_tokens", "thinking") if k in payload},
    }
    sink.append(event)
    return event


def record_attempt(event, response, elapsed, error=None):
    if event is None:
        return
    event.update(pending=False, seconds=round(elapsed, 6),
                 http_status=getattr(response, "status_code", None),
                 error_type=type(error).__name__ if error else None)
    if response is not None:
        try:
            data = response.json()
            event["actual_model"] = data.get("model") if isinstance(data.get("model"), str) else None
            usage = data.get("usage")
            if isinstance(usage, dict):
                event["usage"] = {k: v for k, v in usage.items()
                                  if k in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens",
                                           "total_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                                  and isinstance(v, (int, float)) and not isinstance(v, bool)
                                  and math.isfinite(v) and v >= 0}
            choices = data.get("choices") or []
            event["finish_reason"] = choices[0].get("finish_reason") if choices else data.get("stop_reason")
        except (ValueError, TypeError, AttributeError, IndexError):
            pass
    sink = _sink.get()
    if isinstance(sink, Journal):
        sink.flush(event)


class Journal(list):
    """Flush each attempt before a parent-enforced timeout can terminate a worker."""
    def __init__(self, path):
        super().__init__()
        self.path = path

    def append(self, value):
        super().append(value)
        self.flush(value)

    def flush(self, value):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
