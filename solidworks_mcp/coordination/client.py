"""Thin MCP-side client. No CAD objects, threads or preflight live here."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

from .ownership import process_identity
from .platform_guard import (default_state_dir, private_directory, read_json,
                             source_root, validate_activation)
from .protocol import (MAX_MESSAGE_BYTES, PROTOCOL_VERSION, QueueError,
                       TERMINAL_STATES, decode, encode, finite_seconds,
                       result_content, unwrap_result)


class QueueClient:
    def __init__(self, state_dir=None, *, backend="native", auto_start=True,
                 expected_owner_id=None):
        environment_owner = (os.environ.get("SOLIDWORKS_MCP_EXPECTED_OWNER_ID")
                             if backend == "native" else None)
        for owner in (expected_owner_id, environment_owner):
            if owner is not None and (not isinstance(owner, str) or len(owner) != 32
                    or any(character not in "0123456789abcdef" for character in owner)):
                raise QueueError("INVALID_EXPECTED_OWNER_ID",
                                 "Expected broker owner must be 32 lowercase hexadecimal characters")
        if (expected_owner_id is not None and environment_owner is not None
                and expected_owner_id != environment_owner):
            raise QueueError("INVALID_EXPECTED_OWNER_ID",
                             "Explicit and environment broker owner expectations differ")
        self.expected_owner_id = (expected_owner_id if expected_owner_id is not None
                                  else environment_owner)
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.backend = backend
        # Qualification clients must attach to their recorded broker generation;
        # losing it must never launch a replacement and continue the same test.
        self.auto_start = auto_start if self.expected_owner_id is None else False
        self.client_id = uuid.uuid4().hex
        if backend == "native" and self.state_dir.resolve() != default_state_dir().resolve():
            raise QueueError("NONCANONICAL_STATE", "Native clients must use the shared canonical queue")
        if backend == "test" and os.environ.get("SOLIDWORKS_MCP_TEST_BACKEND") != "1":
            raise QueueError("TEST_BACKEND_DISABLED", "Test executor requires explicit opt-in")

    def _endpoint(self):
        endpoint = read_json(self.state_dir / "endpoint.json")
        if endpoint.get("protocol") != PROTOCOL_VERSION or endpoint.get("backend") != self.backend:
            raise QueueError("PROTOCOL_MISMATCH", "Queue endpoint is incompatible")
        if endpoint.get("host") != "127.0.0.1":
            raise QueueError("INVALID_ENDPOINT", "Only a local queue endpoint is allowed")
        if (self.expected_owner_id is not None
                and endpoint.get("owner_id") != self.expected_owner_id):
            raise QueueError("BROKER_OWNER_CHANGED", "Queue endpoint belongs to another dispatcher owner")
        if os.path.normcase(endpoint.get("source_root", "")) != os.path.normcase(str(source_root())):
            raise QueueError("RELEASE_MISMATCH", "Stop the previous release's broker before upgrading")
        expected = endpoint.get("process") or {}
        try:
            actual = process_identity(expected["pid"])
        except (OSError, KeyError, ValueError) as exc:
            raise QueueError("BROKER_NOT_RUNNING", "Queue endpoint process has exited") from exc
        if actual.get("creation_token") != expected.get("creation_token"):
            raise QueueError("BROKER_NOT_RUNNING", "Stale queue endpoint")
        if self.backend == "native":
            from solidworks_mcp import __version__
            proof = endpoint.get("launch") or {}
            if endpoint.get("version") != __version__:
                raise QueueError("RELEASE_MISMATCH", "Shared dispatcher version differs from the client")
            if proof.get("mode") not in {"desktop", "no_job"} or proof.get("process") != expected:
                raise QueueError("BROKER_LIFETIME_UNVERIFIED", "Existing dispatcher has no verified independent lifetime")
            if proof["mode"] == "desktop":
                self._validate_desktop_endpoint(endpoint)
            elif proof.get("retained_os_job") is not False:
                raise QueueError("BROKER_LIFETIME_UNVERIFIED", "Dispatcher no-job proof is inconsistent")
        return endpoint

    def _validate_desktop_endpoint(self, endpoint):
        from .desktop_launch import physical_identity
        from .ownership import user_session_identity
        proof = endpoint.get("launch") or {}
        nonce = proof.get("nonce")
        if (not isinstance(nonce, str) or len(nonce) != 32
                or any(character not in "0123456789abcdef" for character in nonce)):
            raise QueueError("DESKTOP_LAUNCH_UNVERIFIED", "Dispatcher launch nonce is invalid")
        if proof.get("state") != physical_identity(self.state_dir):
            raise QueueError("STATE_IDENTITY_CHANGED", "Dispatcher is bound to another physical journal directory")
        receipt = read_json(Path(self.state_dir).resolve() / "launch" / (nonce + ".receipt.json"))
        if (proof != receipt or proof.get("mode") != "desktop"
                or proof.get("scope") != user_session_identity()
                or proof.get("process") != endpoint.get("process")):
            raise QueueError("DESKTOP_LAUNCH_UNVERIFIED", "Dispatcher receipt does not match its process, user and physical queue")

    def _rpc(self, endpoint, action, payload=None):
        request = {"protocol": PROTOCOL_VERSION, "token": endpoint["token"],
                   "action": action, "payload": payload or {}}
        with socket.create_connection(("127.0.0.1", int(endpoint["port"])), timeout=10) as connection:
            connection.sendall(encode(request))
            with connection.makefile("rb") as stream:
                raw = stream.readline(MAX_MESSAGE_BYTES + 2)
            if not raw.endswith(b"\n"):
                raise QueueError("TRANSPORT_INTERRUPTED", "Queue response was interrupted")
            reply = decode(raw)
        if reply.get("ok") is not True:
            error = reply.get("error") or {}
            raise QueueError(error.get("code", "QUEUE_ERROR"), error.get("message", "Queue request failed"))
        return reply["value"]

    def ensure_broker(self, *, desktop=False):
        try:
            endpoint = self._endpoint()
            self._rpc(endpoint, "status")
            return endpoint
        except (OSError, QueueError) as exc:
            if isinstance(exc, QueueError) and exc.code in {"RELEASE_MISMATCH", "PROTOCOL_MISMATCH", "UNAUTHORIZED", "BROKER_LIFETIME_UNVERIFIED", "STATE_IDENTITY_CHANGED", "BROKER_OWNER_CHANGED"}:
                raise
            if not self.auto_start:
                raise QueueError("BROKER_NOT_RUNNING", "Shared dispatcher is not running") from exc
        private_directory(self.state_dir)
        launched = None
        desktop_request = None
        # Native Windows startup always uses the verified existing desktop.
        # Synthetic tests opt in explicitly to exercise that exact route.
        if os.name == "nt" and (self.backend == "native" or desktop):
            from .desktop_launch import launch_desktop
            desktop_request = launch_desktop(self.state_dir, self.backend)
        else:
            launched = self._start_breakaway()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                endpoint = self._endpoint()
                self._rpc(endpoint, "status")
                if desktop_request:
                    proof = endpoint.get("launch") or {}
                    # Another concurrently launched verified dispatcher can win
                    # the instance lease. Its receipt must still bind its exact
                    # endpoint generation and this physical state directory.
                    self._validate_desktop_endpoint(endpoint)
                return endpoint
            except (OSError, QueueError) as exc:
                if isinstance(exc, QueueError) and exc.code == "BROKER_OWNER_CHANGED":
                    raise
                from .launch import EXIT_LIFETIME_UNVERIFIED
                if launched is not None and launched.poll() == EXIT_LIFETIME_UNVERIFIED:
                    raise QueueError("BROKER_DETACH_FAILED",
                        "Broker lifetime independence could not be verified; no queue or CAD was opened")
                time.sleep(.1)
        raise QueueError("BROKER_START_FAILED", "Shared dispatcher did not become ready; inspect private diagnostics")

    def _start_breakaway(self):
        command = [sys.executable, "-m", "solidworks_mcp.coordination.launch",
                   "--backend", self.backend]
        if self.backend == "test":
            command += ["--state-dir", str(self.state_dir)]
        with (self.state_dir / "broker-start.log").open("ab") as errors:
            try:
                launched = subprocess.Popen(command, cwd=source_root(), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=errors, close_fds=True,
                    # A new console/process group alone does not escape the
                    # frontend's Windows Job Object. Never retry attached.
                    creationflags=(0x01000000 | 0x08000000 | 0x00000200) if os.name == "nt" else 0,
                    start_new_session=os.name != "nt")
            except OSError as exc:
                raise QueueError("BROKER_DETACH_FAILED",
                    "Independent broker launch was denied. Start the broker from an "
                    "operator-owned session outside the MCP host job; no attached fallback is allowed") from exc
        return launched

    def request(self, action, payload=None):
        return self._rpc(self.ensure_broker(), action, payload)

    def submit(self, tool, arguments, *, request_id=None, queue_timeout_sec=3600,
               execution_timeout_sec=300):
        if self.backend == "native":
            validate_activation(self.state_dir)
        payload = {"tool": tool, "arguments": arguments,
                   "request_id": request_id or uuid.uuid4().hex,
                   "client_id": self.client_id, "queue_timeout_sec": queue_timeout_sec,
                   "execution_timeout_sec": execution_timeout_sec}
        try:
            return self.request("submit", payload)
        except (OSError, QueueError) as exc:
            if isinstance(exc, QueueError) and exc.code not in {"TRANSPORT_INTERRUPTED", "BROKER_NOT_RUNNING"}:
                raise
            # If admission committed but its response was lost, the SAME durable
            # request ID retrieves the original job. Never generate another ID.
            return self.request("submit", payload)

    def wait(self, job_id, timeout=25):
        deadline = time.monotonic() + finite_seconds(timeout, 25, maximum=60)
        while True:
            job = self.request("get", {"job_id": job_id})
            if job["state"] in TERMINAL_STATES or time.monotonic() >= deadline:
                return job
            time.sleep(min(.15, max(0, deadline - time.monotonic())))

    def _job_content(self, job, *, original_result=False):
        state = job["state"]
        if original_result and state in {"completed", "failed"} and job.get("result"):
            content = copy.deepcopy(job["result"])
            parsed = unwrap_result(content)
            parsed.setdefault("data", {})["queue"] = {
                "job_id": job["id"], "request_id": job["request_id"], "state": state,
                "seq": job["seq"], "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at")}
            content[0]["text"] = json.dumps(parsed, ensure_ascii=False, allow_nan=False)
            return content
        success = state in {"queued", "running", "completed"}
        data = copy.deepcopy(job)
        data["status"] = {"queued": "pending", "completed": "done", "failed": "error"}.get(state, state)
        data["job_id"] = job["id"]
        if job.get("result"):
            parsed = unwrap_result(job["result"])
            data["execution_result"] = parsed
            for key in ("stdout", "stderr"):
                if key in (parsed.get("data") or {}):
                    data[key] = parsed["data"][key]
        return result_content(success, f"Job {job['id']}: {state}", data,
                              "RECOVERY_REQUIRED" if state == "recovery_required" else None)

    def call_tool(self, name, arguments):
        arguments = copy.deepcopy(arguments or {})
        request_id = None
        accepted_job = None
        try:
            if name == "get_queue_status":
                return result_content(True, "Shared CAD queue status", self.request("status"))
            if name == "list_jobs":
                return result_content(True, "Shared CAD jobs", self.request("list", arguments))
            if name == "get_job_result":
                job = self.wait(arguments.get("job_id"),
                                finite_seconds(arguments.get("timeout"), 0, maximum=60))
                return self._job_content(job)
            if name == "get_queue_request":
                return self._job_content(self.request("lookup", arguments))
            if name == "cancel_queued_job":
                return self._job_content(self.request("cancel", arguments))
            if name == "queue_events":
                return result_content(True, "CAD queue timeline", self.request("events", arguments))
            if name == "acknowledge_queue_recovery":
                return result_content(True, "Recovery verification requested; gate is still closed",
                                      self.request("recover", arguments))
            coordination = arguments.pop("coordination", {}) or {}
            if not isinstance(coordination, dict) or "_coordination" in arguments:
                raise QueueError("INVALID_REQUEST", "Invalid coordination metadata")
            known = {"request_id", "expected_document", "target_document", "trusted_python", "queue_timeout_sec", "execution_timeout_sec", "wait_timeout_sec"}
            if set(coordination) - known:
                raise QueueError("INVALID_REQUEST", "Unknown coordination property")
            metadata = {key: coordination[key] for key in ("expected_document", "target_document", "trusted_python")
                        if key in coordination}
            if metadata:
                arguments["_coordination"] = metadata
            asynchronous = name == "execute_python_async"
            tool = "execute_python" if asynchronous else name
            execution_timeout = finite_seconds(coordination.get("execution_timeout_sec"),
                (arguments.get("budget") or {}).get("max_elapsed_sec", 300))
            queue_timeout = finite_seconds(coordination.get("queue_timeout_sec"), 3600)
            wait_timeout = finite_seconds(coordination.get("wait_timeout_sec"), 25, maximum=60)
            if queue_timeout <= 0 or execution_timeout <= 0:
                raise QueueError("INVALID_REQUEST", "Queue and execution timeouts must be positive")
            request_id = coordination.get("request_id") or uuid.uuid4().hex
            job = self.submit(tool, arguments, request_id=request_id,
                queue_timeout_sec=queue_timeout,
                execution_timeout_sec=execution_timeout)
            accepted_job = job["id"]
            if asynchronous:
                return self._job_content(job)
            job = self.wait(job["id"], wait_timeout)
            return self._job_content(job, original_result=True)
        except (QueueError, OSError, ValueError, TypeError) as exc:
            recovery = {}
            if request_id is not None:
                recovery = {"request_id": request_id, "job_id": accepted_job,
                            "instruction": "Use get_queue_request with this request_id before retrying; retain the same request_id"}
            return result_content(False, str(exc), recovery,
                                  code=getattr(exc, "code", "QUEUE_UNAVAILABLE"))
