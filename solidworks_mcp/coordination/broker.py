"""One durable queue, one COM-owning main thread, independent control transport.

Timeout/quarantine does NOT kill a thread or release the OS lease. A late result
is recorded, but the recovery gate remains until explicitly reconciled.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import socketserver
import threading
import time
import uuid

from .journal import Journal, JournalError, UNSTARTED_NATIVE_CONTEXT
from .ownership import ProcessLease, process_identity, process_is_same_alive
from .platform_guard import (atomic_json, default_state_dir, private_directory,
                             source_root, validate_activation)
from .protocol import (MAX_MESSAGE_BYTES, PROTOCOL_VERSION, QueueError, decode,
                       encode, finite_seconds, result_content, unwrap_result)


def public_job(job):
    if job is None:
        raise QueueError("JOB_NOT_FOUND", "Unknown queue job")
    return {key: value for key, value in job.items()
            if key not in {"arguments", "arguments_json", "fingerprint"}}


class Broker:
    def __init__(self, state_dir, backend="native"):
        self.state_dir = Path(state_dir)
        self.backend_name = backend
        if backend not in {"native", "test"}:
            raise ValueError("Unknown backend")
        if backend == "test" and os.environ.get("SOLIDWORKS_MCP_TEST_BACKEND") != "1":
            raise QueueError("TEST_BACKEND_DISABLED", "Test executor requires explicit opt-in")
        self.owner_id = uuid.uuid4().hex
        # Test resources can be independent; the real CAD resource never depends
        # on release path, journal path, PID (which may change), or MCP version.
        lease_name = "solidworks-cad" if backend == "native" else "test:" + str(self.state_dir.resolve())
        self.lease = ProcessLease(lease_name)
        self.journal = None
        self.backend = None
        self.token = secrets.token_hex(32)
        self.accepting = True
        self.stopping = threading.Event()
        self.wakeup = threading.Event()
        self.poisoned = threading.Event()
        self.active_job = None
        self.state_lock = threading.RLock()
        self.recovery_request = None
        self.last_error = None
        self.server = None
        self.endpoint = None

    def _safe_error(self, exc):
        # Caller code/arguments and credentials must never be included in generic
        # transport diagnostics. Structured CAD results live in private storage.
        return {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:500]}

    def request(self, request):
        if request.get("protocol") != PROTOCOL_VERSION:
            raise QueueError("PROTOCOL_MISMATCH", "Unsupported coordination protocol")
        token = request.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self.token):
            raise QueueError("UNAUTHORIZED", "Invalid local queue credential")
        action = request.get("action")
        payload = request.get("payload") or {}
        if not isinstance(payload, dict):
            raise QueueError("INVALID_REQUEST", "Payload must be an object")
        if action == "status":
            with self.state_lock:
                return {**self.journal.status(), "owner_id": self.owner_id,
                        "backend": self.backend_name, "accepting": self.accepting,
                        "active_job_id": self.active_job,
                        "executor_busy": self.active_job is not None,
                        "recovery_check_pending": self.recovery_request is not None,
                        "fatal_gate": self.poisoned.is_set(), "last_error": self.last_error,
                        "protocol": PROTOCOL_VERSION}
        if action == "submit":
            if self.backend_name == "native":
                # Direct local protocol users receive the same deployment gate
                # as MCP clients. The worker rechecks after queue waiting.
                validate_activation(self.state_dir)
            with self.state_lock:
                if not self.accepting or self.poisoned.is_set():
                    raise QueueError("QUEUE_NOT_ACCEPTING", "Queue is draining or requires repair")
                tool = payload.get("tool")
                args = payload.get("arguments") or {}
                if not isinstance(tool, str) or not tool or not isinstance(args, dict):
                    raise QueueError("INVALID_REQUEST", "Tool and argument object are required")
                queue_timeout = finite_seconds(payload.get("queue_timeout_sec"), 3600)
                execution_timeout = finite_seconds(payload.get("execution_timeout_sec"), 300)
                if queue_timeout <= 0 or execution_timeout <= 0:
                    raise QueueError("INVALID_REQUEST", "Queue and execution timeouts must be positive")
                job = self.journal.enqueue(tool, args, payload.get("request_id"),
                    payload.get("client_id"),
                    queue_timeout_sec=queue_timeout,
                    execution_timeout_sec=execution_timeout)
            self.wakeup.set()
            return public_job(job)
        if action == "get":
            return public_job(self.journal.get(payload.get("job_id")))
        if action == "lookup":
            return public_job(self.journal.get_by_request(payload.get("request_id")))
        if action == "list":
            return {"jobs": [{key: value for key, value in public_job(job).items() if key != "result"}
                             for job in self.journal.list_jobs(limit=int(payload.get("limit", 100)))]}
        if action == "events":
            return {"events": self.journal.events(payload.get("job_id"), limit=int(payload.get("limit", 100)))}
        if action == "cancel":
            return public_job(self.journal.cancel(payload.get("job_id")))
        if action == "shutdown":
            with self.state_lock:
                self.accepting = False
                self.stopping.set()
            self.wakeup.set()
            return {"draining": True, "active_job_id": self.active_job,
                    "queued_jobs_preserved": True}
        if action == "recover":
            if not payload.get("operator") or not payload.get("note") or not payload.get("expected_job_ids"):
                raise QueueError("RECOVERY_ACK_REQUIRED", "Operator, note and exact recovery job IDs are required")
            with self.state_lock:
                if self.active_job is not None or self.recovery_request is not None:
                    raise QueueError("EXECUTOR_BUSY", "Executor has not returned; recovery is unsafe")
                self.recovery_request = payload
            self.wakeup.set()
            return {"recovery_check_requested": True, "gate_released": False}
        raise QueueError("UNKNOWN_ACTION", "Unknown queue control action")

    def _initialize_backend(self):
        if self.backend_name == "native":
            activation = validate_activation(self.state_dir)
            if self.backend is None:
                from .cad_backend import NativeBackend
                candidate = NativeBackend(activation, self.state_dir)
                candidate.initialize()
                self.backend = candidate
        elif self.backend is None:
            from .test_backend import TestBackend
            self.backend = TestBackend({}, self.state_dir)
            self.backend.initialize()
        return self.backend

    def _deadline(self, job, returned):
        deadline = job["execution_timeout_sec"]
        if returned.wait(deadline):
            return
        # Hold state_lock across observation+transition: completion cannot race
        # the recovery transition into releasing a following job.
        with self.state_lock:
            if returned.is_set():
                return
            try:
                self.journal.mark_recovery(job["id"], self.owner_id,
                                           "EXECUTION_TIMEOUT_OUTCOME_UNKNOWN")
            except Exception:
                self.poisoned.set()
                self.last_error = {"code": "JOURNAL_WRITE_FAILED", "message": "Timeout could not be persisted"}

    def _execute(self, job):
        with self.state_lock:
            self.active_job = job["id"]
        returned = threading.Event()
        monitor = threading.Thread(target=self._deadline, args=(job, returned), daemon=True)
        monitor.start()
        try:
            if self.backend_name == "native":
                activation = validate_activation(self.state_dir)
                self.journal.set_context(job["id"], self.owner_id, {
                    "target_pid": activation["target_pid"],
                    "target_created_at": activation["target_created_at"],
                    "expected_major": activation["expected_major"],
                })
            backend = self._initialize_backend()
            if self.backend_name == "test":
                self.journal.set_context(job["id"], self.owner_id, backend.identity())
            with self.state_lock:
                still_running = self.journal.get(job["id"])["state"] == "running"
            if still_running:
                result = backend.execute(job["tool"], job["arguments"])
            else:
                # Initialization returned after its deadline. Do not begin a
                # new mutation after the recovery gate was already raised.
                result = result_content(False, "Initialization returned after deadline; operation was not started",
                    {"requires_recovery": True, "native_execution_returned": self.backend_name == "native",
                     "operation_not_started": True}, "INITIALIZATION_TIMEOUT")
            parsed = unwrap_result(result)
            requires_recovery = bool((parsed.get("data") or {}).get("requires_recovery"))
            with self.state_lock:
                returned.set()
                if requires_recovery:
                    self.journal.mark_recovery(job["id"], self.owner_id, "EXECUTOR_REQUIRES_RECOVERY")
                self.journal.finish(job["id"], self.owner_id, result,
                                    "completed" if parsed.get("success") else "failed")
                self.last_error = None
        except BaseException as exc:
            with self.state_lock:
                returned.set()
                self.last_error = self._safe_error(exc)
                try:
                    # Once claimed, unknown infrastructure failure is never
                    # translated into an automatically retryable CAD operation.
                    self.journal.mark_recovery(job["id"], self.owner_id, self.last_error["code"])
                    self.journal.record_late_result(job["id"], self.owner_id,
                        result_content(False, "Executor stopped; reconcile the job before resuming",
                                       {"requires_recovery": True}, self.last_error["code"]))
                except Exception:
                    self.poisoned.set()
        finally:
            returned.set()
            monitor.join(timeout=2)
            with self.state_lock:
                self.active_job = None

    def _preflight_recovery_ui(self):
        # A restarted broker has no backend yet. Check Win32 readiness BEFORE
        # initialize(), which itself attaches and probes the COM application.
        from .native_watchdog import inspect_target_windows
        activation = validate_activation(self.state_dir)
        ui = inspect_target_windows(int(activation["target_pid"]))
        if not ui.get("ready"):
            raise QueueError("CAD_UI_NOT_READY", "Activated CAD window is absent or blocked; recovery cannot probe COM")

    def _recover(self, payload):
        try:
            jobs = [self.journal.get(job_id) for job_id in payload["expected_job_ids"]]
            if any(job is None for job in jobs):
                raise QueueError("JOB_NOT_FOUND", "Recovery includes an unknown job")
            if self.backend_name == "native":
                # Never probe an uncertain old CAD process just to discover
                # whether it is still busy. Establish a completed call or a
                # dead previous process generation using non-COM evidence first.
                for job in jobs:
                    context = job.get("execution_context") or {}
                    if context == UNSTARTED_NATIVE_CONTEXT:
                        continue
                    parsed = unwrap_result(job["result"]) if job.get("result") else {}
                    returned = (parsed.get("data") or {}).get("native_execution_returned") is True
                    previous = {"pid": context.get("target_pid"),
                                "created_at": context.get("target_created_at"),
                                "creation_time": context.get("target_created_at")}
                    if not returned:
                        if not previous["pid"] or previous["created_at"] is None:
                            raise QueueError("RECOVERY_IDENTITY_UNKNOWN", "Uncertain CAD process was not bound; manual investigation required")
                        if process_is_same_alive(previous["pid"], previous["created_at"]) is not False:
                            raise QueueError("PREVIOUS_CAD_OUTCOME_UNKNOWN", "Previous CAD generation is still alive or cannot be verified")
                self._preflight_recovery_ui()
            backend = self._initialize_backend()
            evidence = backend.validate_recovery(jobs)
            verified = bool(evidence.get("verified") or evidence.get("backend_idle_verified"))
            if not verified:
                raise QueueError("RECOVERY_NOT_VERIFIED", "CAD state has not been reconciled")
            self.journal.acknowledge_recovery(payload["note"], payload["operator"],
                payload["expected_job_ids"], backend_idle_verified=verified)
            self.last_error = None
        except Exception as exc:
            self.last_error = self._safe_error(exc)
        finally:
            with self.state_lock:
                self.recovery_request = None

    def run(self):
        if not self.lease.acquire(timeout=0):
            raise QueueError("BROKER_ALREADY_RUNNING", "Another dispatcher owns this CAD resource")
        try:
            private_directory(self.state_dir)
            self.journal = Journal(self.state_dir / "queue.sqlite3")
            self.journal.recover_started()
            self.server = ControlServer(("127.0.0.1", 0), Handler)
            self.server.broker = self
            identity = process_identity()
            from solidworks_mcp import __version__
            from .launch import lifetime_proof
            self.endpoint = {"protocol": PROTOCOL_VERSION, "host": "127.0.0.1",
                             "port": self.server.server_address[1], "token": self.token,
                             "process": identity, "owner_id": self.owner_id,
                             "backend": self.backend_name, "source_root": str(source_root()),
                             "version": __version__, "launch": lifetime_proof()}
            atomic_json(self.state_dir / "endpoint.json", self.endpoint)
            serving = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .1}, daemon=True)
            serving.start()
            while not self.stopping.is_set():
                if self.recovery_request is not None and not self.poisoned.is_set():
                    self._recover(self.recovery_request)
                with self.state_lock:
                    job = (None if self.poisoned.is_set() or self.stopping.is_set()
                           else self.journal.claim_next(self.owner_id,
                               execution_context=(UNSTARTED_NATIVE_CONTEXT
                                   if self.backend_name == "native" else None)))
                    if job:
                        self.active_job = job["id"]
                if job:
                    self._execute(job)
                else:
                    if self.backend:
                        self.backend.pump()
                    self.wakeup.wait(.05)
                    self.wakeup.clear()
            if self.backend:
                self.backend.close()
            self.server.shutdown()
            serving.join(timeout=5)
            self.server.server_close()
        finally:
            # No worker is force-terminated. This finally only runs after the
            # executor has actually returned (process death is recovered later).
            endpoint_path = self.state_dir / "endpoint.json"
            if self.endpoint and endpoint_path.exists():
                try:
                    existing = json.loads(endpoint_path.read_text(encoding="utf-8"))
                    if existing.get("owner_id") == self.owner_id:
                        endpoint_path.unlink()
                except (OSError, ValueError):
                    pass
            self.lease.release()


class ControlServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        # Avoid logging payloads, auth tokens, Python source or sensitive paths.
        pass


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        try:
            raw = self.rfile.readline(MAX_MESSAGE_BYTES + 2)
            if not raw.endswith(b"\n"):
                raise QueueError("INVALID_REQUEST", "Expected a bounded JSON line")
            value = self.server.broker.request(decode(raw))
            response = {"ok": True, "value": value}
        except Exception as exc:
            response = {"ok": False, "error": self.server.broker._safe_error(exc)}
        try:
            self.wfile.write(encode(response))
        except (OSError, QueueError):
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--backend", choices=["native", "test"], default="native")
    args = parser.parse_args(argv)
    if args.backend == "native" and args.state_dir and args.state_dir.resolve() != default_state_dir().resolve():
        parser.error("Native broker uses the canonical per-user state directory")
    state = args.state_dir or default_state_dir()
    try:
        if args.backend == "native":
            # Direct module execution must not bypass the verified bootstrap.
            # This precedes lease acquisition, journal recovery and all COM.
            from .launch import require_independent_lifetime
            require_independent_lifetime()
        Broker(state, args.backend).run()
    except Exception as exc:
        # Only code on stderr; private runtime paths and payloads stay private.
        import sys
        print(getattr(exc, "code", type(exc).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
