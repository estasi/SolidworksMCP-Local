"""Queue routing and native safety boundaries, using only synthetic COM doubles."""

import asyncio
import contextvars
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp.coordination.cad_backend import (
    NativeBackend, NativeExecutionError, document_guard, failure,
    local_dispatch_context, require_local_dispatch, validate_script)


def result(content):
    return json.loads(content[0]["text"] if isinstance(content[0], dict)
                      else content[0].text)


class Document:
    def __init__(self, path=r"C:\fixtures\model.SLDPRT", title="model.SLDPRT"):
        self.path = path
        self.title = title
        self.GetPathName = lambda: self.path
        self.GetTitle = lambda: self.title
        self.GetType = 1
        self.ConfigurationManager = types.SimpleNamespace(
            ActiveConfiguration=types.SimpleNamespace(Name="Default"))


def automation(doc=None):
    doc = doc or Document()
    return types.SimpleNamespace(app=types.SimpleNamespace(ActiveDoc=doc),
                                 _get_doc_path=lambda target: target.path,
                                 _get_doc_title=lambda target: target.title)


class DispatchGuardTests(unittest.TestCase):
    def test_no_local_capability(self):
        with self.assertRaisesRegex(RuntimeError, "REQUIRES_BROKER"):
            require_local_dispatch()

    def test_capability_lasts_exactly_one_scope(self):
        owner = object()
        with local_dispatch_context(owner):
            self.assertIs(require_local_dispatch(), owner)
        with self.assertRaises(RuntimeError):
            require_local_dispatch()

    def test_copied_context_cannot_authorize_another_thread(self):
        results = []
        with local_dispatch_context(object()):
            context = contextvars.copy_context()
            def child():
                try:
                    context.run(require_local_dispatch)
                except RuntimeError:
                    results.append("blocked")
            worker = threading.Thread(target=child)
            worker.start()
            worker.join()
        self.assertEqual(results, ["blocked"])


class ScriptContractTests(unittest.TestCase):
    def test_synchronous_geometry_and_constants_allowed(self):
        validate_script("import math\nresult = com_get(doc, 'GetTitle')\nprint(math.pi)")

    def test_obvious_background_and_rebinding_are_rejected(self):
        cases = [
            "import threading\nthreading.Thread(target=work).start()",
            "from concurrent.futures import ThreadPoolExecutor",
            "import subprocess\nsubprocess.Popen(['python'])",
            "os.startfile('program.exe')",
            "win32com.Dispatch('SldWorks.Application')",
            "pythoncom.CoInitialize()",
            "__import__('threading')",
            "async def work(): pass",
            "sw.ExitApp()",
        ]
        for code in cases:
            with self.subTest(code=code), self.assertRaises(ValueError):
                validate_script(code)


class DocumentGuardTests(unittest.TestCase):
    def test_required_identity_checked_before_snapshot(self):
        target = Mock()
        self.assertEqual(document_guard(target, None, required=True)["code"],
                         "EXPECTED_DOCUMENT_REQUIRED")
        self.assertEqual(target.mock_calls, [])

    def test_saved_path_case_insensitive_windows_normalization(self):
        self.assertIsNone(document_guard(automation(),
            {"path": r"c:/fixtures/MODEL.SLDPRT", "configuration": "Default",
             "document_type": 1}, required=True))

    def test_same_basename_other_directory_is_rejected(self):
        issue = document_guard(automation(), r"C:\other\model.SLDPRT", required=True)
        self.assertEqual(issue["code"], "EXPECTED_DOCUMENT_MISMATCH")

    def test_relative_saved_path_is_invalid(self):
        self.assertEqual(document_guard(automation(), "model.SLDPRT", required=True)["code"],
                         "INVALID_EXPECTED_DOCUMENT")

    def test_unsaved_requires_exact_title_and_explicit_permission(self):
        target = automation(Document("", "Part1"))
        self.assertIsNotNone(document_guard(target, {"title": "Part1"}, required=True))
        self.assertIsNone(document_guard(target,
            {"title": "Part1", "allow_unsaved_document": True}, required=True))
        self.assertIsNotNone(document_guard(target,
            {"title": "Part2", "allow_unsaved_document": True}, required=True))

    def test_unreadable_identity_is_not_an_empty_document(self):
        target = automation()
        target.app.ActiveDoc.GetPathName = Mock(side_effect=RuntimeError("unreadable"))
        issue = document_guard(target, r"C:\fixtures\model.SLDPRT", required=True)
        self.assertEqual(issue["code"], "DOCUMENT_IDENTITY_UNAVAILABLE")
        self.assertTrue(issue["requires_recovery"])

    def test_saved_document_path_read_failure_cannot_masquerade_as_unsaved(self):
        target = automation(Document(r"C:\fixtures\Part1.SLDPRT", "Part1"))
        target.app.ActiveDoc.GetPathName = Mock(side_effect=RuntimeError("COM failed"))
        issue = document_guard(target, {"title": "Part1", "allow_unsaved_document": True},
                               required=True)
        self.assertEqual(issue["code"], "DOCUMENT_IDENTITY_UNAVAILABLE")
        self.assertTrue(issue["requires_recovery"])


class NativeBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.activation = {"enabled": True, "target_pid": 12345,
                           "target_created_at": 1000., "expected_major": 33,
                           "source_root": "synthetic"}
        self.backend = NativeBackend(self.activation, Path(self.temp.name))
        self.backend._thread = threading.get_ident()
        self.backend._loop = asyncio.new_event_loop()
        self.addCleanup(self.backend._loop.close)
        self.backend._validate_activation = Mock()
        self.backend._verify_bound = Mock(return_value=self.activation)
        self.called = []

        async def dispatch(name, args):
            self.assertIs(require_local_dispatch(), self.backend)
            self.called.append((name, args))
            return [{"type": "text", "text": json.dumps({"success": True, "data": {}})}]

        self.backend._server = types.SimpleNamespace(
            sw_automation=automation(), _dispatch_local_tool=dispatch,
            _is_effectively_mutating=lambda name, args: name in {
                "execute_cad_plan", "run_transaction", "execute_python", "create_new_part",
                "close_document", "save_document"})
        watcher = patch("solidworks_mcp.coordination.native_watchdog.NativeWatchdog")
        self.watchdog = watcher.start().return_value
        self.watchdog.event.return_value = None
        self.addCleanup(watcher.stop)
        window_probe = patch("solidworks_mcp.coordination.native_watchdog.inspect_target_windows",
                             return_value={"ready": True})
        self.window_probe = window_probe.start()
        self.addCleanup(window_probe.stop)

    def args(self, **args):
        return {**args, "_coordination": {"expected_document": r"C:\fixtures\model.SLDPRT"}}

    def test_all_steps_share_one_dispatch_and_coordination_is_stripped(self):
        operations = [{"op": "create_sketch", "args": {}}, {"op": "draw_circle", "args": {}}]
        reply = self.backend.execute("execute_cad_plan", self.args(operations=operations))
        self.assertTrue(result(reply)["success"])
        self.assertTrue(result(reply)["data"]["native_execution_returned"])
        self.assertEqual(self.called, [("execute_cad_plan", {"operations": operations})])
        self.watchdog.start.assert_called_once()
        self.watchdog.stop.assert_called_once()
        with self.assertRaises(RuntimeError):
            require_local_dispatch()

    def test_active_document_change_blocks_before_legacy_preflight(self):
        self.backend._server.sw_automation.app.ActiveDoc.path = r"C:\other\model.SLDPRT"
        reply = self.backend.execute("run_transaction", self.args())
        self.assertEqual(result(reply)["error_name"], "EXPECTED_DOCUMENT_MISMATCH")
        self.assertEqual(self.called, [])
        self.watchdog.start.assert_not_called()

    def test_close_requires_identity_even_with_save_false(self):
        reply = self.backend.execute("close_document", {"save": False})
        self.assertEqual(result(reply)["error_name"], "EXPECTED_DOCUMENT_REQUIRED")
        self.assertEqual(self.called, [])

    def test_new_document_has_no_prior_identity_requirement(self):
        self.assertTrue(result(self.backend.execute("create_new_part", {}))["success"])

    def test_async_script_uses_synchronous_owning_thread(self):
        args = self.args(code="result = 1")
        args["_coordination"]["trusted_python"] = True
        reply = self.backend.execute("execute_python_async", args)
        self.assertTrue(result(reply)["success"])
        self.assertEqual(self.called[0][0], "execute_python")

    def test_control_never_touches_com_or_legacy_preflight(self):
        self.backend._verify_bound.side_effect = AssertionError("No COM allowed")
        for name in ("get_job_result", "list_jobs", "get_queue_status"):
            self.assertEqual(result(self.backend.execute(name, {}))["error_name"],
                             "QUEUE_CONTROL_ROUTE_ERROR")

    def test_auto_recovery_and_background_scripts_do_not_dispatch(self):
        reply = self.backend.execute("save_document", self.args(recovery={"auto_recover": True}))
        self.assertEqual(result(reply)["error_name"], "AUTOMATIC_RECOVERY_DISABLED")
        args = self.args(code="import threading")
        args["_coordination"]["trusted_python"] = True
        reply = self.backend.execute("execute_python", args)
        self.assertEqual(result(reply)["error_name"], "SCRIPT_OPERATION_CONTRACT")
        self.assertEqual(self.called, [])

    def test_python_is_denied_by_default_even_if_statically_safe(self):
        reply = self.backend.execute("execute_python", self.args(code="result = 1"))
        self.assertEqual(result(reply)["error_name"], "TRUSTED_PYTHON_REQUIRED")
        self.assertEqual(self.called, [])

    def test_watchdog_event_keeps_recovery_fence_after_cad_returned(self):
        self.watchdog.event.return_value = {"code": "TARGET_CAD_UI_BLOCKED"}
        reply = result(self.backend.execute("save_document", self.args()))
        self.assertFalse(reply["success"])
        self.assertTrue(reply["data"]["requires_recovery"])

    def test_mutation_failure_keeps_recovery_fence(self):
        async def failed(*args):
            return [{"type": "text", "text": json.dumps({"success": False})}]
        self.backend._server._dispatch_local_tool = failed
        self.assertTrue(result(self.backend.execute("save_document", self.args()))
                        ["data"]["requires_recovery"])

    def test_recovery_rejects_alive_uncertain_original_and_unknown_visibility(self):
        job = {"execution_context": self.activation, "result": None}
        for alive in (True, None):
            with patch("solidworks_mcp.coordination.ownership.process_is_same_alive", return_value=alive):
                self.assertFalse(self.backend.validate_recovery([job])["verified"])

    def test_recovery_requires_genuine_return_not_synthetic_broker_error(self):
        job = {"execution_context": self.activation,
               "result": [{"type": "text", "text": '{"success":false,"data":{}}'}]}
        with patch("solidworks_mcp.coordination.ownership.process_is_same_alive", return_value=True):
            self.assertFalse(self.backend.validate_recovery([job])["verified"])
            job["result"] = failure("ERROR", "Returned failure", requires_recovery=True)
            self.assertTrue(self.backend.validate_recovery([job])["verified"])

    def test_recovery_accepts_proven_process_death_only_with_fresh_bound_verification(self):
        job = {"execution_context": self.activation, "result": None}
        with patch("solidworks_mcp.coordination.ownership.process_is_same_alive", return_value=False):
            self.assertTrue(self.backend.validate_recovery([job])["verified"])
            self.backend._verify_bound.assert_called_once()
            self.backend._verify_bound.side_effect = RuntimeError("new instance unverified")
            self.assertFalse(self.backend.validate_recovery([job])["verified"])

    def test_recovery_ui_blocker_is_rejected_before_com_probe(self):
        job = {"execution_context": self.activation,
               "result": failure("ERROR", "returned", requires_recovery=True)}
        self.window_probe.return_value = {"ready": False, "code": "TARGET_CAD_UI_BLOCKED"}
        self.backend._verify_bound.side_effect = AssertionError("COM must not be probed")
        self.assertFalse(self.backend.validate_recovery([job])["verified"])
        self.backend._verify_bound.assert_not_called()

    def test_recovery_accepts_exact_durable_pre_com_claim_sentinel(self):
        job = {"execution_context": {"native_execution_not_started": True,
                                     "context_version": 1}, "result": None}
        with patch("solidworks_mcp.coordination.ownership.process_is_same_alive",
                   side_effect=AssertionError("No native process was bound")):
            self.assertTrue(self.backend.validate_recovery([job])["verified"])
        self.backend._verify_bound.assert_called_once()

    def test_recovery_does_not_accept_pre_com_marker_in_result_or_partial_context(self):
        for context in ({}, {"native_execution_not_started": True},
                        {"native_execution_not_started": True, "context_version": 1,
                         "unexpected": True}):
            job = {"execution_context": context, "result": [{"type": "text", "text":
                   json.dumps({"success": False, "data": {"native_execution_not_started": True}})}]}
            self.assertFalse(self.backend.validate_recovery([job])["verified"])


class NativeInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.activation = {"enabled": True, "target_pid": 12345,
                           "target_created_at": 1000., "expected_major": 33}
        self.backend = NativeBackend(self.activation, Path(self.temp.name))

    def test_activation_rejection_precedes_any_com_initialization(self):
        with patch.object(self.backend, "_validate_activation", side_effect=RuntimeError("legacy active")), \
                patch("pythoncom.CoInitializeEx") as initialize, \
                patch("win32com.client.GetActiveObject") as attach:
            with self.assertRaisesRegex(RuntimeError, "legacy active"):
                self.backend.initialize()
            initialize.assert_not_called()
            attach.assert_not_called()

    def test_unactivated_or_wrong_version_never_attaches(self):
        for update in ({"enabled": False}, {"expected_major": 34}):
            backend = NativeBackend({**self.activation, **update}, Path(self.temp.name))
            with patch("win32com.client.GetActiveObject") as attach:
                with self.assertRaises(NativeExecutionError):
                    backend.initialize()
                attach.assert_not_called()

    def test_wrong_rot_pid_fails_before_revision_or_legacy_connect(self):
        app = types.SimpleNamespace(_oleobj_=object(), GetProcessID=lambda: 98765)
        fake = types.SimpleNamespace(app=app, connect=Mock())
        with patch.object(self.backend, "_validate_activation"), \
                patch.object(self.backend, "_verify_process"), \
                patch("pythoncom.CoInitializeEx") as init, \
                patch("pythoncom.CoUninitialize") as uninit, \
                patch("win32com.client.GetActiveObject", return_value=app), \
                patch("win32com.client.dynamic.Dispatch", return_value=app), \
                patch("solidworks_mcp.coordination.cad_backend.importlib.import_module",
                      return_value=types.SimpleNamespace(sw_automation=fake)):
            with self.assertRaisesRegex(NativeExecutionError, "PID differs"):
                self.backend.initialize()
            init.assert_called_once()
            uninit.assert_called_once()
            fake.connect.assert_not_called()
            self.assertFalse(fake._connected)

    def test_cim_millisecond_precision_does_not_reject_same_generation(self):
        with patch("solidworks_mcp.coordination.ownership.process_identity",
                   return_value={"created_at": 1000.0008}):
            self.backend._verify_process()
        with patch("solidworks_mcp.coordination.ownership.process_identity",
                   return_value={"created_at": 1000.1}):
            with self.assertRaises(NativeExecutionError):
                self.backend._verify_process()


class NativeWatchdogTests(unittest.TestCase):
    def test_window_inspection_is_pid_scoped_without_title_or_com_calls(self):
        from solidworks_mcp.coordination.native_watchdog import inspect_target_windows
        gui = types.SimpleNamespace(
            IsWindowVisible=lambda hwnd: True,
            GetWindowRect=lambda hwnd: (0, 0, 1000, 600),
            GetWindow=lambda hwnd, flag: 0,
            IsWindowEnabled=lambda hwnd: hwnd != 2,
            EnumWindows=lambda callback, arg: [callback(hwnd, arg) for hwnd in (1, 2)])
        process = types.SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (0, 123 if hwnd == 1 else 999))
        with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}):
            state = inspect_target_windows(123)
            self.assertTrue(state["ready"])
            self.assertEqual(state["main_window_hwnd"], 1)
            self.assertFalse(inspect_target_windows(999)["ready"])

    def test_clean_early_watcher_exit_is_still_failure(self):
        from solidworks_mcp.coordination.native_watchdog import NativeWatchdog
        with tempfile.TemporaryDirectory() as temp:
            watcher = NativeWatchdog({}, Path(temp))
            watcher.directory.mkdir(parents=True)
            watcher.process = Mock()
            watcher.process.poll.return_value = 0
            watcher.process.returncode = 0
            watcher.stop()
            self.assertEqual(watcher.event()["code"], "WATCHDOG_EXITED_BEFORE_OPERATION_RETURN")


class MCPRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from solidworks_mcp import server
        cls.server = server

    def test_initialization_advertises_package_build_and_stable_server_name(self):
        from solidworks_mcp import __version__
        options = self.server.server.create_initialization_options()
        self.assertEqual(options.server_name, "SolidworksMCP")
        self.assertEqual(options.server_version, __version__)

    def test_public_tools_only_call_client_in_worker_thread(self):
        owner_thread = threading.get_ident()
        calls = []
        def queue_call(name, args):
            calls.append((name, args, threading.get_ident()))
            return [{"type": "text", "text": '{"success":true}'}]
        with patch("solidworks_mcp.coordination.client.QueueClient") as client:
            client.return_value.call_tool.side_effect = queue_call
            with patch.object(self.server.sw_automation, "_document_snapshot",
                              side_effect=AssertionError("Client performed COM preflight")):
                for name in ("save_document", "get_job_result", "list_jobs", "get_queue_status"):
                    reply = asyncio.run(self.server.call_tool(name, {"test": True}))
                    self.assertTrue(result(reply)["success"])
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(call[2] != owner_thread for call in calls))

    def test_local_dispatch_fails_before_preflight_without_owner(self):
        with patch.object(self.server.sw_automation, "_document_snapshot",
                          side_effect=AssertionError("Preflight happened")):
            with self.assertRaisesRegex(RuntimeError, "REQUIRES_BROKER"):
                asyncio.run(self.server._dispatch_local_tool("save_document", {}))

    def test_no_legacy_async_worker_route(self):
        with self.assertRaisesRegex(RuntimeError, "QueueClient"):
            self.server._execute_python_async("pass")

    def test_schema_exposes_whole_operation_guard_and_non_com_controls(self):
        tools = {tool.name: tool for tool in asyncio.run(self.server.list_tools())}
        self.assertIn("coordination", tools["execute_cad_plan"].inputSchema["properties"])
        self.assertNotIn("coordination", tools["get_queue_status"].inputSchema["properties"])
        self.assertIn("acknowledge_queue_recovery", tools)
        self.assertEqual(tools["list_open_documents"].inputSchema["properties"]["strict"],
                         {"type": "boolean", "default": False})


if __name__ == "__main__":
    unittest.main()
