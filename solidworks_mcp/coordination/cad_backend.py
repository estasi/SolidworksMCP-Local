"""Single-apartment native executor. Importing this module never contacts CAD.

Only the broker may construct this backend. Activation and the machine-wide
owner lock are enforced by the broker; a second, thread-bound capability guards
the legacy dispatcher so an MCP client cannot accidentally bypass the queue.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import importlib
import json
import ntpath
from pathlib import Path
import threading
from typing import Any


_LOCAL_DISPATCH: ContextVar[tuple[int, object] | None] = ContextVar(
    "solidworks_broker_dispatch", default=None)


@contextmanager
def local_dispatch_context(owner: object):
    """Private capability scoped to one complete operation on its STA thread."""
    token = _LOCAL_DISPATCH.set((threading.get_ident(), owner))
    try:
        yield
    finally:
        _LOCAL_DISPATCH.reset(token)


def require_local_dispatch() -> object:
    value = _LOCAL_DISPATCH.get()
    if value is None or value[0] != threading.get_ident():
        raise RuntimeError("CAD_DISPATCH_REQUIRES_BROKER: use the shared queue")
    return value[1]


class NativeExecutionError(RuntimeError):
    """An identity or runtime failure that must leave the queue fenced."""


def failure(code: str, message: str, *, requires_recovery=False,
            details=None) -> list[dict]:
    return [{"type": "text", "text": json.dumps({
        "success": False, "message": message, "error_code": 999,
        "error_name": code, "data": {
            "requires_recovery": requires_recovery,
            "native_execution_returned": True,
            "error": {"code": code, "details": details or {}}}},
        ensure_ascii=False)}]


def validate_script(code: str) -> None:
    """Reject common ways to escape the operation lifetime; NOT a sandbox.

    Scripts are trusted local code. Dynamic import, process/thread creation and
    independent COM connections are disallowed by contract, even when an
    obfuscated form cannot be detected statically.
    """
    tree = ast.parse(code)
    forbidden_modules = {
        "threading", "_thread", "multiprocessing", "subprocess", "asyncio",
        "concurrent", "ctypes", "win32com", "pythoncom", "comtypes",
        "win32api", "win32process", "importlib", "runpy", "joblib"}
    forbidden_calls = {
        "__import__", "eval", "exec", "compile", "GetObject", "Dispatch",
        "DispatchEx", "GetActiveObject", "CoInitialize", "CoInitializeEx",
        "CoUninitialize", "CoCreateInstance", "Thread", "Timer", "Process",
        "Popen", "start_new_thread", "startfile", "system", "popen",
        "fork", "spawn", "spawnl", "spawnlp", "spawnv", "spawnvp",
        "CreateProcess", "ShellExecute", "ShellExecuteEx", "submit",
        "create_task", "ensure_future", "run_in_executor", "to_thread",
        "call_later", "call_soon", "SetTimer", "register", "ExitApp"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
            if forbidden_modules.intersection(names):
                raise ValueError("Script imports a process/thread/COM escape module")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in forbidden_modules:
                raise ValueError("Script imports a process/thread/COM escape module")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor,
                               ast.AsyncWith)):
            raise ValueError("Background/async script execution is not allowed")
        elif isinstance(node, ast.Call):
            name = (node.func.id if isinstance(node.func, ast.Name) else
                    node.func.attr if isinstance(node.func, ast.Attribute) else "")
            if name in forbidden_calls:
                raise ValueError(f"Script call {name} escapes the operation contract")


def document_guard(automation, expected: Any, *, required: bool) -> dict | None:
    """Compare the active identity AFTER waiting, BEFORE legacy preflight.

    A saved document requires an absolute Windows path. An unsaved document
    requires an explicit title and allow_unsaved_document, never a wildcard.
    Optional configuration and document type further constrain the identity.
    """
    if expected is None:
        if required:
            return {"code": "EXPECTED_DOCUMENT_REQUIRED",
                    "message": "coordination.expected_document is required for "
                               "active-document changes"}
        return None
    if isinstance(expected, str):
        expected = {"path": expected}
    if not isinstance(expected, dict) or not expected:
        return {"code": "INVALID_EXPECTED_DOCUMENT",
                "message": "expected_document must contain an absolute path, "
                           "or an unsaved title with explicit permission"}
    try:
        app = automation.app
        doc = app.ActiveDoc if app is not None else None
        if doc is None:
            return {"code": "EXPECTED_DOCUMENT_MISMATCH",
                    "message": "No active document matches the queued operation"}
        from ..automation.com_utils import com_get
        # Convenience helpers default unreadable paths to an empty string.
        # That would misclassify a saved document as explicitly allowed unsaved
        # work. Identity reads must fail closed, never use those fallbacks.
        actual = {"path": com_get(doc, "GetPathName"),
                  "title": com_get(doc, "GetTitle")}
        if not isinstance(actual["path"], str) or not isinstance(actual["title"], str):
            raise ValueError("Native document identity is not textual")
        path = expected.get("path")
        if path:
            if not isinstance(path, str) or not ntpath.isabs(path):
                return {"code": "INVALID_EXPECTED_DOCUMENT",
                        "message": "Expected document path must be absolute"}
            matches = (ntpath.normcase(ntpath.normpath(actual["path"])) ==
                       ntpath.normcase(ntpath.normpath(path)))
        else:
            matches = (expected.get("allow_unsaved_document") is True and
                       isinstance(expected.get("title"), str) and
                       bool(expected["title"].strip()) and
                       not actual["path"] and
                       actual["title"] == expected["title"])
        if "title" in expected:
            matches = matches and actual["title"] == expected["title"]
        if "document_type" in expected:
            actual["document_type"] = int(com_get(doc, "GetType"))
            matches = matches and actual["document_type"] == expected["document_type"]
        if "configuration" in expected:
            configuration = doc.ConfigurationManager.ActiveConfiguration
            actual["configuration"] = str(com_get(configuration, "Name"))
            matches = matches and actual["configuration"] == expected["configuration"]
        if not matches:
            return {"code": "EXPECTED_DOCUMENT_MISMATCH",
                    "message": "The active document changed while this operation "
                               "was queued", "actual": actual}
    except Exception as exc:
        return {"code": "DOCUMENT_IDENTITY_UNAVAILABLE",
                "message": "Active document identity could not be verified",
                "error_type": type(exc).__name__, "requires_recovery": True}
    return None


# These operations intentionally do not act upon the currently active document.
_NO_ACTIVE_DOCUMENT = {
    "connect_solidworks", "create_new_part", "create_new_assembly",
    "open_document", "get_solidworks_info", "get_capabilities",
    "get_session_metrics", "set_units"}
_VIEW_MUTATIONS = {
    "take_screenshot", "section_screenshot", "normal_to_sketch",
    "set_view_orientation", "zoom_to", "zoom_to_fit", "select_by_id",
    "select_face_by_ray", "clear_selection"}
_CLIENT_ONLY = {"get_job_result", "list_jobs", "get_queue_status",
                "cancel_queued_job", "acknowledge_queue_recovery", "queue_events",
                "get_queue_request"}


class NativeBackend:
    def __init__(self, activation: dict, state_dir: Path):
        self.activation = dict(activation)
        self.state_dir = Path(state_dir)
        self._thread: int | None = None
        self._server = None
        self._pythoncom = None
        self._loop = None
        self._identity: dict = {key: activation[key] for key in
                               ("target_pid", "target_created_at", "expected_major")
                               if key in activation}
        self._original_connect = None

    def _require_thread(self):
        if self._thread is None or self._thread != threading.get_ident():
            raise NativeExecutionError("Native CAD executor used from the wrong thread")

    def _verify_process(self):
        from .ownership import process_identity
        expected_pid = int(self.activation["target_pid"])
        actual = process_identity(expected_pid)
        if (not actual or abs(float(actual.get("created_at", 0)) -
                              float(self.activation.get("target_created_at", 0))) > .002):
            raise NativeExecutionError("Activated SOLIDWORKS process is absent or replaced")
        return actual

    def _verify_bound(self):
        self._require_thread()
        self._verify_process()
        from ..automation.com_utils import com_get
        app = self._server.sw_automation.app
        if app is None:
            raise NativeExecutionError("Bound SOLIDWORKS application is unavailable")
        if int(com_get(app, "GetProcessID")) != int(self.activation["target_pid"]):
            raise NativeExecutionError("COM application PID differs from activation")
        version = str(com_get(app, "RevisionNumber"))
        if int(version.split(".")[0]) != int(self.activation["expected_major"]):
            raise NativeExecutionError("SOLIDWORKS major version differs from activation")
        self._identity.update({"target_pid": int(self.activation["target_pid"]),
                               "target_created_at": self.activation["target_created_at"],
                               "expected_major": int(self.activation["expected_major"]),
                               "version": version})
        return self._identity

    def initialize(self):
        if self._thread is not None:
            self._require_thread()
            return
        if self.activation.get("enabled") is not True:
            raise NativeExecutionError("Native backend has not been activated")
        if int(self.activation.get("expected_major", 0)) != 33:
            raise NativeExecutionError("This deployment is qualified only for SOLIDWORKS 2025")
        self._validate_activation()
        self._verify_process()
        import pythoncom
        import win32com.client
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        self._pythoncom = pythoncom
        self._thread = threading.get_ident()
        try:
            self._server = importlib.import_module("solidworks_mcp.server")
            # GetActiveObject cannot launch SOLIDWORKS. A different ROT instance
            # is rejected rather than rebound through a version-independent ProgID.
            app = win32com.client.GetActiveObject("SldWorks.Application")
            app = win32com.client.dynamic.Dispatch(app._oleobj_)
            self._server.sw_automation._sw_app = app
            self._server.sw_automation._connected = True
            self._verify_bound()
            self._original_connect = self._server.sw_automation.connect
            self._server.sw_automation.connect = self._bound_connect
            self._loop = asyncio.new_event_loop()
        except BaseException:
            self.close()
            raise

    def _validate_activation(self):
        from .platform_guard import validate_activation
        current = validate_activation(self.state_dir)
        if any(current.get(key) != self.activation.get(key) for key in
               ("target_pid", "target_created_at", "expected_major", "source_root")):
            raise NativeExecutionError("Activation changed; drain and restart the dispatcher")

    def _bound_connect(self):
        """Replace the legacy launch/rebind path for every nested helper."""
        require_local_dispatch()
        try:
            identity = self._verify_bound()
            return {"success": True, "message": "Connected to the activated instance",
                    "error_code": 0, "error_name": "swSuccess",
                    "data": {**identity, "launched": False, "queue_owned": True}}
        except Exception as exc:
            return json.loads(failure("CAD_IDENTITY_LOST", str(exc),
                                      requires_recovery=True)[0]["text"])

    def identity(self) -> dict:
        """Cached only: safe for journal/status readers and a blocked COM call."""
        return copy.deepcopy(self._identity)

    def pump(self):
        self._require_thread()
        self._pythoncom.PumpWaitingMessages()

    def execute(self, tool: str, arguments: dict) -> list[dict]:
        self._require_thread()
        if tool in _CLIENT_ONLY:
            return failure("QUEUE_CONTROL_ROUTE_ERROR", "Control must use the broker route")
        arguments = copy.deepcopy(arguments or {})
        coordination = arguments.pop("_coordination", {})
        if not isinstance(coordination, dict):
            return failure("INVALID_COORDINATION", "Internal coordination must be an object")
        from .document_target import normalize_target, select_target, TargetDocumentError
        target = None
        if "target_document" in coordination:
            try:
                target = normalize_target(coordination["target_document"],
                    expected_present="expected_document" in coordination)
                if tool in _NO_ACTIVE_DOCUMENT:
                    return failure("TARGET_DOCUMENT_UNSUPPORTED_TOOL",
                                   "This tool does not operate on a selected existing document")
            except TargetDocumentError as exc:
                return failure(exc.code, str(exc))
        # Defence in depth: the broker must remap async Python at submission.
        if tool == "execute_python_async":
            tool = "execute_python"
        if tool == "execute_python":
            if coordination.get("trusted_python") is not True:
                return failure("TRUSTED_PYTHON_REQUIRED",
                               "Raw Python requires coordination.trusted_python=true "
                               "and synchronous execution without threads, processes "
                               "or independent COM connections")
            try:
                validate_script(arguments.get("code", ""))
            except (SyntaxError, ValueError, TypeError) as exc:
                return failure("SCRIPT_OPERATION_CONTRACT", str(exc))
        if (arguments.get("recovery") or {}).get("auto_recover"):
            return failure("AUTOMATIC_RECOVERY_DISABLED",
                           "Queued operations require explicit recovery after uncertainty")
        if (arguments.get("ui_guard") or {}).get("auto_resolve_known"):
            return failure("AUTOMATIC_DIALOG_RECOVERY_DISABLED",
                           "This queue observes dialogs and fences the operation; "
                           "resolve dialogs explicitly before acknowledging recovery")
        try:
            self._validate_activation()
            if target is not None:
                from .native_watchdog import inspect_target_windows
                if not inspect_target_windows(int(self.activation["target_pid"])).get("ready"):
                    return failure("TARGET_DOCUMENT_UI_BLOCKED",
                                   "CAD UI must be ready before target selection")
            self._verify_bound()
        except Exception as exc:
            return failure("CAD_IDENTITY_LOST", str(exc), requires_recovery=True)
        mutating = (self._server._is_effectively_mutating(tool, arguments) or
                    tool in _VIEW_MUTATIONS)
        problem = (document_guard(
            self._server.sw_automation, coordination.get("expected_document"),
            required=mutating and tool not in _NO_ACTIVE_DOCUMENT)
            if target is None else None)
        if problem:
            return failure(problem["code"], problem["message"],
                           requires_recovery=problem.get("requires_recovery", False),
                           details=problem)

        from .native_watchdog import NativeWatchdog
        watchdog = NativeWatchdog(self.activation, self.state_dir,
                                  arguments.get("ui_guard") or {})
        dispatched = False
        selection = None
        try:
            watchdog.start()
            with local_dispatch_context(self):
                if target is not None:
                    selection = select_target(self._server.sw_automation, target,
                        lambda: inspect_target_windows(int(self.activation["target_pid"])).get("ready") is True)
                    problem = document_guard(self._server.sw_automation, target, required=True)
                    if problem:
                        raise TargetDocumentError(problem["code"], problem["message"],
                                                  requires_recovery=True)
                dispatched = True
                result = self._loop.run_until_complete(
                    self._server._dispatch_local_tool(tool, arguments))
            serialized = [item.model_dump(mode="json") if hasattr(item, "model_dump")
                          else dict(item) for item in result]
        except TargetDocumentError as exc:
            serialized = failure(exc.code, str(exc), requires_recovery=exc.requires_recovery)
        except Exception as exc:
            serialized = failure("NATIVE_OPERATION_FAILED", str(exc),
                                 requires_recovery=True)
        finally:
            watchdog.stop()
        event = watchdog.event()
        if event:
            return failure("CAD_WATCHDOG_BLOCKED",
                           "CAD returned after a UI/watchdog interruption; review required",
                           requires_recovery=True,
                           details={"watchdog": event, "returned_result": serialized})
        # A failure in a mutation may have left partial geometry or files. Even
        # a legacy rollback claim is reviewed explicitly; no automatic replay.
        for item in serialized:
            if item.get("type") == "text":
                try:
                    payload = json.loads(item["text"])
                except (ValueError, KeyError):
                    return failure("INVALID_NATIVE_RESULT", "Result is not JSON",
                                   requires_recovery=True)
                payload.setdefault("data", {})["native_execution_returned"] = True
                if tool == "execute_python":
                    payload["data"]["raw_python_trusted"] = True
                if selection is not None:
                    payload["data"]["document_selection"] = selection
                if dispatched and mutating and not payload.get("success", False):
                    payload["data"]["requires_recovery"] = True
                item["text"] = json.dumps(payload, ensure_ascii=False)
                break
        return serialized

    def validate_recovery(self, jobs: list[dict]) -> dict:
        """Late return or proven old-process death is required, never a lease."""
        self._require_thread()
        from .ownership import process_is_same_alive
        from .journal import UNSTARTED_NATIVE_CONTEXT
        for job in jobs:
            identity = job.get("execution_context") or {}
            if identity == UNSTARTED_NATIVE_CONTEXT:
                # Written atomically with claim and replaced with a CAD process
                # identity before any native initialization. A result field on
                # its own is not sufficient proof of this pre-COM boundary.
                continue
            result = job.get("result")
            try:
                payload = json.loads(result[0]["text"])
                returned = payload.get("data", {}).get("native_execution_returned") is True
            except (TypeError, IndexError, KeyError, ValueError, AttributeError):
                returned = False
            if returned:
                continue
            if isinstance(identity, str):
                identity = json.loads(identity)
            pid = identity.get("target_pid")
            created = identity.get("target_created_at")
            if not pid or created is None:
                return {"verified": False, "reason": "Uncertain job has no CAD process identity"}
            alive = process_is_same_alive(int(pid), created)
            if alive is not False:
                return {"verified": False, "reason":
                        "Original uncertain SOLIDWORKS process is alive or cannot be inspected"}
        try:
            # This method must run on the STA owner, not a control connection.
            self._validate_activation()
            from .native_watchdog import inspect_target_windows
            ui = inspect_target_windows(int(self.activation["target_pid"]))
            if not ui.get("ready"):
                return {"verified": False,
                        "reason": "Activated CAD window is unavailable or blocked",
                        "ui": ui}
            self._verify_bound()
        except Exception as exc:
            return {"verified": False, "reason": str(exc)}
        return {"verified": True, "identity": self.identity()}

    def close(self):
        if self._thread is None:
            return
        self._require_thread()
        if self._server is not None:
            automation = self._server.sw_automation
            if self._original_connect is not None:
                automation.connect = self._original_connect
            automation._sw_app = None
            automation._connected = False
        if self._loop is not None:
            self._loop.close()
        self._loop = None
        self._server = None
        self._pythoncom.CoUninitialize()
        self._pythoncom = None
        self._thread = None
