"""Small JSON wire protocol; no pickle or COM objects cross the boundary."""
from __future__ import annotations

import json
import math

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "recovery_required"})
CONTROL_TOOLS = frozenset({"get_queue_status", "get_job_result", "list_jobs",
                           "cancel_queued_job", "queue_events",
                           "get_queue_request", "acknowledge_queue_recovery"})


class QueueError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def finite_seconds(value, default, maximum=86400):
    value = default if value is None else value
    if isinstance(value, bool):
        raise QueueError("INVALID_REQUEST", "A timeout must be a finite number")
    value = float(value)
    if not math.isfinite(value) or value < 0 or value > maximum:
        raise QueueError("INVALID_REQUEST", "Timeout is outside the allowed range")
    return value


def encode(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise QueueError("MESSAGE_TOO_LARGE", "Queue message exceeds the size limit")
    return raw + b"\n"


def decode(raw):
    if len(raw) > MAX_MESSAGE_BYTES:
        raise QueueError("MESSAGE_TOO_LARGE", "Queue message exceeds the size limit")
    value = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (
        (_ for _ in ()).throw(ValueError("Non-finite JSON number"))))
    if not isinstance(value, dict):
        raise QueueError("INVALID_REQUEST", "Queue request must be an object")
    return value


def result_content(success, message, data=None, code=None):
    result = {"success": success, "message": message,
              "error_code": 0 if success else 999,
              "error_name": "swSuccess" if success else (code or "QUEUE_ERROR"),
              "data": data or {}}
    return [{"type": "text", "text": json.dumps(result, ensure_ascii=False,
                                                 allow_nan=False)}]


def unwrap_result(content):
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            try:
                result = json.loads(first["text"])
                if isinstance(result, dict):
                    return result
            except (ValueError, KeyError):
                pass
    raise QueueError("INVALID_BACKEND_RESULT", "Executor returned an invalid result")
