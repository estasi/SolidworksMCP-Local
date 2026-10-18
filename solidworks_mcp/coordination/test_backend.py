"""Synthetic executor for process/crash tests. Never imports COM.

Only enabled with BOTH --backend test and SOLIDWORKS_MCP_TEST_BACKEND=1.
It writes only to the explicitly isolated state directory.
"""
import json
import os
import time
from pathlib import Path

from .platform_guard import atomic_json
from .protocol import QueueError, result_content


class TestBackend:
    def __init__(self, activation, state_dir):
        if os.environ.get("SOLIDWORKS_MCP_TEST_BACKEND") != "1":
            raise QueueError("TEST_BACKEND_DISABLED", "Test executor requires explicit opt-in")
        self.directory = Path(state_dir)

    def initialize(self):
        pass

    def identity(self):
        return {"backend": "test", "target_pid": os.getpid(), "target_created_at": 0}

    def execute(self, tool, arguments):
        if tool != "synthetic_operation":
            raise QueueError("TEST_TOOL_REQUIRED", "Synthetic executor only accepts its test operation")
        def event(kind):
            with (self.directory / "synthetic-timeline.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": kind, "at": time.time(),
                                         "label": arguments.get("label"), "pid": os.getpid()}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        event("start")
        path = self.directory / "synthetic-model.json"
        before = json.loads(path.read_text()) if path.exists() else {"value": 0}
        time.sleep(min(10, max(0, float(arguments.get("delay_before", 0)))))
        if arguments.get("crash_before"):
            os._exit(72)
        after = {"value": before["value"] + int(arguments.get("increment", 1))}
        atomic_json(path, after)
        if arguments.get("crash_after"):
            os._exit(73)
        time.sleep(min(10, max(0, float(arguments.get("delay_after", 0)))))
        if arguments.get("fail"):
            atomic_json(path, before)
            event("rollback")
            return result_content(False, "Synthetic failure; rollback verified", {"rollback_verified": True})
        event("end")
        return result_content(True, "Synthetic operation completed", after)

    def pump(self):
        pass

    def validate_recovery(self, jobs):
        # Test-only backend has no external CAD process which could keep running.
        return {"verified": True, "backend_idle_verified": True, "backend": "test"}

    def close(self):
        pass
