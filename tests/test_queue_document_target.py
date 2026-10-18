"""Document selection tests use synthetic COM objects only, never native CAD."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import multiprocessing
import ntpath
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp.coordination.cad_backend import NativeBackend, require_local_dispatch
from solidworks_mcp.coordination.client import QueueClient
from solidworks_mcp.coordination.document_target import normalize_target, select_target, TargetDocumentError
from solidworks_mcp.coordination.protocol import unwrap_result, QueueError

A = r"C:\synthetic\model-A.SLDPRT"
B = r"C:\synthetic\model-B.SLDPRT"


class Document:
    def __init__(self, path, kind=1, config="Default"):
        self.path, self.kind = path, kind
        self.title = ntpath.basename(path) or "Unsaved1"
        self.dirty = False
        self.blocking = 0
        self.sketch = None
        self.editing_self = True
        self.value = 0
        self.ConfigurationManager = types.SimpleNamespace(
            ActiveConfiguration=types.SimpleNamespace(Name=config))
        self.SketchManager = types.SimpleNamespace(ActiveSketch=None)

    def GetPathName(self): return self.path
    def GetTitle(self): return self.title
    def GetType(self): return self.kind
    def GetSaveFlag(self): return self.dirty
    def GetBlockingState(self): return self.blocking
    def GetActiveSketch2(self): return self.sketch
    def GetEditTarget(self): return self if self.editing_self else Document(r"C:\synthetic\component.SLDPRT")


class Application:
    def __init__(self):
        self.documents = [Document(A), Document(B)]
        self.ActiveDoc = self.documents[0]
        self.Visible = self.UserControl = True
        self.calls = []
        self.on_activate = None
        self.running_command = False

    def GetDocuments(self): return list(self.documents)
    def GetDocumentCount(self): return len(self.documents)

    def GetRunningCommandInfo(self, command, title, active):
        command.value = 0
        title.value = ""
        active.value = self.running_command

    def ActivateDoc3(self, name, preferences, option, errors):
        self.calls.append((name, preferences, option))
        if self.on_activate:
            return self.on_activate(name, errors)
        self.ActiveDoc = next(doc for doc in self.documents if ntpath.basename(doc.path) == name)
        errors.value = 0
        return self.ActiveDoc


class Automation:
    def __init__(self, app):
        self.app = self._sw_app = app

    def _strict_document_inventory(self):
        from solidworks_mcp.automation.documents import DocumentOperations
        return DocumentOperations._strict_document_inventory(self)

    def _strict_document_state(self, doc):
        from solidworks_mcp.automation.documents import DocumentOperations
        return DocumentOperations._strict_document_state(self, doc)

    @staticmethod
    def _document_identity_path(path):
        return ntpath.normcase(ntpath.normpath(path)) if path else ""


def synthetic_boundaries(stack):
    """Tripwires catch accidental native attachment; interface wrapping is fake."""
    for member in ("GetActiveObject", "GetObject", "Dispatch", "DispatchEx"):
        stack.enter_context(patch("win32com.client." + member,
            side_effect=AssertionError("Native COM attachment is forbidden in this test")))
    stack.enter_context(patch("pythoncom.CoCreateInstance",
        side_effect=AssertionError("Native COM creation is forbidden in this test")))
    stack.enter_context(patch("solidworks_mcp.automation.references.typed", return_value=None))
    stack.enter_context(patch("solidworks_mcp.automation.com_utils.typed", return_value=None))
    stack.enter_context(patch("solidworks_mcp.coordination.native_watchdog.inspect_target_windows",
                             return_value={"ready": True}))
    watcher = stack.enter_context(patch("solidworks_mcp.coordination.native_watchdog.NativeWatchdog"))
    watcher.return_value.event.return_value = None
    return watcher.return_value


def backend(directory, app, dispatch):
    native = NativeBackend({"enabled": True, "target_pid": 12345,
        "target_created_at": 1000., "expected_major": 33}, Path(directory))
    native._thread = threading.get_ident()
    native._loop = asyncio.new_event_loop()
    native._validate_activation = Mock()
    native._verify_bound = Mock()
    native._server = types.SimpleNamespace(sw_automation=Automation(app),
        _is_effectively_mutating=lambda name, args: name != "get_document_info",
        _dispatch_local_tool=dispatch)
    return native


class TargetValidationTests(unittest.TestCase):
    def test_absolute_paths_and_optional_assertions(self):
        self.assertEqual(normalize_target("c:/synthetic/../synthetic/model-A.SLDPRT")["path"],
                         r"c:\synthetic\model-A.SLDPRT")
        self.assertEqual(normalize_target(r"\\server\share\part.sldprt")["path"],
                         r"\\server\share\part.sldprt")
        self.assertEqual(normalize_target({"path": B, "configuration": "Default", "document_type": 1})["document_type"], 1)

    def test_invalid_paths_and_unknown_properties_reject(self):
        for value in (None, {}, [], "relative.sldprt", "C:part.sldprt", r"\part.sldprt",
                      r"\\?\C:\part.sldprt", "C:\\x.step", {"path": B, "title": "B"},
                      {"path": B, "configuration": ""}, {"path": B, "document_type": True},
                      {"path": B, "document_type": 4}, {"path": B, "document_type": 1.0}):
            with self.subTest(value=value), self.assertRaises(TargetDocumentError):
                normalize_target(value)

    def test_both_guards_conflict_even_if_paths_match(self):
        with self.assertRaisesRegex(TargetDocumentError, "never both"):
            normalize_target(B, expected_present=True)


class TargetSelectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        synthetic_boundaries(self.stack)
        self.app = Application()
        self.automation = Automation(self.app)

    def select(self, target=B, ready=lambda: True):
        return select_target(self.automation, normalize_target(target), ready)

    def reject(self, code, *, target=B, recovery=False):
        with self.assertRaises(TargetDocumentError) as raised:
            self.select(target)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.requires_recovery, recovery)
        return raised.exception

    def test_selects_exact_open_model_without_rebuild_or_preferences(self):
        before = self.automation._strict_document_inventory()
        proof = self.select()
        self.assertTrue(proof["activated"])
        self.assertEqual(self.app.ActiveDoc.path, B)
        self.assertEqual(self.app.calls, [("model-B.SLDPRT", False, 1)])
        self.assertEqual(before["documents"], self.automation._strict_document_inventory()["documents"])

    def test_already_active_dirty_model_is_noop(self):
        self.app.ActiveDoc.dirty = True
        self.assertFalse(self.select(A)["activated"])
        self.assertEqual(self.app.calls, [])

    def test_already_active_sketch_and_command_are_left_for_normal_tool_preflight(self):
        self.app.ActiveDoc.sketch = object()
        self.app.ActiveDoc.dirty = True
        self.app.running_command = True
        self.assertFalse(self.select(A)["activated"])
        self.assertEqual(self.app.calls, [])

    def test_already_active_assembly_in_context_edit_is_not_same_model(self):
        target = r"C:\synthetic\assembly.SLDASM"
        assembly = Document(target, kind=2)
        assembly.editing_self = False
        self.app.documents[0] = self.app.ActiveDoc = assembly
        self.reject("TARGET_DOCUMENT_UNSAFE_EDIT", target=target)
        self.assertEqual(self.app.calls, [])

    def test_already_active_assembly_unknown_edit_target_rejects(self):
        target = r"C:\synthetic\assembly.SLDASM"
        assembly = Document(target, kind=2)
        self.app.documents[0] = self.app.ActiveDoc = assembly
        for unknown in (None, object()):
            assembly.GetEditTarget = lambda: unknown
            self.reject("TARGET_DOCUMENT_STATE_UNAVAILABLE", target=target)
        self.assertEqual(self.app.calls, [])

    def test_already_active_assembly_keeps_same_model_dirty_sketch_continuity(self):
        target = r"C:\synthetic\assembly.SLDASM"
        assembly = Document(target, kind=2)
        assembly.sketch, assembly.dirty = object(), True
        self.app.documents[0] = self.app.ActiveDoc = assembly
        self.app.running_command = True
        self.assertFalse(self.select(target)["activated"])
        self.assertEqual(self.app.calls, [])

    def test_noop_selection_rechecks_assembly_edit_identity_after_inventory(self):
        target = r"C:\synthetic\assembly.SLDASM"
        assembly = Document(target, kind=2)
        self.app.documents[0] = self.app.ActiveDoc = assembly
        targets = iter((assembly, Document(B)))
        observed = []
        def changing_edit_target():
            observed.append(True)
            return next(targets)
        assembly.GetEditTarget = changing_edit_target
        self.reject("TARGET_DOCUMENT_UNSAFE_EDIT", target=target)
        self.assertEqual(len(observed), 2)
        self.assertEqual(self.app.calls, [])

    def test_already_active_path_does_not_need_unambiguous_activation_basename(self):
        self.app.documents.append(Document(r"C:\other\model-A.SLDPRT"))
        self.assertFalse(self.select(A)["activated"])
        self.assertEqual(self.app.calls, [])

    def test_missing_or_closed_target_does_not_open_it(self):
        self.reject("TARGET_DOCUMENT_NOT_OPEN", target=r"C:\synthetic\closed.SLDPRT")
        self.assertEqual(self.app.calls, [])

    def test_duplicate_basename_rejects_before_activation(self):
        self.app.documents.append(Document(r"C:\other\model-B.SLDPRT"))
        self.reject("TARGET_DOCUMENT_AMBIGUOUS")
        self.assertEqual(self.app.calls, [])

    def test_unsaved_name_collision_rejects(self):
        duplicate = Document("")
        duplicate.title = "model-B"
        self.app.documents.append(duplicate)
        self.reject("TARGET_DOCUMENT_AMBIGUOUS")

    def test_duplicate_path_or_incomplete_inventory_is_unknown(self):
        self.app.documents.append(Document(B))
        self.reject("TARGET_DOCUMENT_STATE_UNAVAILABLE")
        self.assertEqual(self.app.calls, [])

    def test_unknown_dirty_flag_blocks(self):
        self.app.documents[1].dirty = None
        self.reject("TARGET_DOCUMENT_STATE_UNAVAILABLE")

    def test_dirty_active_or_target_blocks_real_switch(self):
        for index in (0, 1):
            with self.subTest(index=index):
                self.app.documents[index].dirty = True
                self.reject("TARGET_DOCUMENT_UNSAVED_CHANGES")
                self.app.documents[index].dirty = False
        self.assertEqual(self.app.calls, [])

    def test_unsaved_active_model_blocks_switch(self):
        self.app.ActiveDoc.path = ""
        self.reject("TARGET_DOCUMENT_UNSAVED_CHANGES")

    def test_active_and_target_sketch_edit_block(self):
        for index in (0, 1):
            self.app.documents[index].sketch = object()
            self.reject("TARGET_DOCUMENT_UNSAFE_EDIT")
            self.app.documents[index].sketch = None
        self.assertEqual(self.app.calls, [])

    def test_nonzero_and_unknown_blocking_state_block(self):
        for value in (1, 2, 3, None, True):
            self.app.ActiveDoc.blocking = value
            self.reject("TARGET_DOCUMENT_UNSAFE_EDIT")
        self.assertEqual(self.app.calls, [])

    def test_in_context_assembly_edit_blocks(self):
        self.app.ActiveDoc.kind = 2
        self.app.ActiveDoc.editing_self = False
        self.reject("TARGET_DOCUMENT_UNSAFE_EDIT")

    def test_nonmodal_command_or_property_manager_blocks(self):
        self.app.running_command = True
        self.reject("TARGET_DOCUMENT_UNSAFE_EDIT")
        self.assertEqual(self.app.calls, [])

    def test_unreadable_command_state_blocks(self):
        self.app.GetRunningCommandInfo = Mock(side_effect=RuntimeError("Unavailable"))
        self.reject("TARGET_DOCUMENT_STATE_UNAVAILABLE")
        self.assertEqual(self.app.calls, [])

    def test_partially_unwritten_command_outputs_block(self):
        def incomplete(command, title, active):
            active.value = False
        self.app.GetRunningCommandInfo = incomplete
        self.reject("TARGET_DOCUMENT_UNSAFE_EDIT")

    def test_native_idle_byref_can_leave_property_manager_title_unwritten(self):
        observed = []
        def native_idle(command, title, active):
            observed.append((command.value, title.value, active.value))
            # SW2025 observed idle reply: no return, command -3, untouched title.
            command.value = -3
            active.value = False
        self.app.GetRunningCommandInfo = native_idle
        self.assertTrue(self.select()["activated"])
        self.assertEqual(observed, [(-2147483648, "<unwritten>", True)] * 3)
        self.assertEqual(len(self.app.calls), 1)

    def test_idle_command_id_is_not_hardcoded_to_observed_negative_value(self):
        for command_id in (-4, -1, 0, 9):
            with self.subTest(command_id=command_id):
                self.app.ActiveDoc = self.app.documents[0]
                self.app.GetRunningCommandInfo = lambda *args: (command_id, None, False)
                self.assertTrue(self.select()["activated"])

    def test_typed_idle_return_allows_null_or_unwritten_title(self):
        for title in (None, "<unwritten>", ""):
            with self.subTest(title=title):
                self.app.ActiveDoc = self.app.documents[0]
                self.app.GetRunningCommandInfo = lambda *args: (-3, title, False)
                self.assertTrue(self.select()["activated"])

    def test_byref_idle_return_allows_null_title(self):
        def native_idle(command, title, active):
            command.value = -3
            title.value = None
            active.value = False
        self.app.GetRunningCommandInfo = native_idle
        self.assertTrue(self.select()["activated"])

    def test_all_unwritten_command_outputs_block(self):
        self.app.GetRunningCommandInfo = lambda *args: None
        self.reject("TARGET_DOCUMENT_UNSAFE_EDIT")
        self.assertEqual(self.app.calls, [])

    def test_command_written_but_unknown_active_flag_blocks(self):
        def incomplete(command, title, active):
            command.value = -3
        self.app.GetRunningCommandInfo = incomplete
        self.reject("TARGET_DOCUMENT_UNSAFE_EDIT")
        self.assertEqual(self.app.calls, [])

    def test_malformed_command_return_or_field_types_block(self):
        for value in ((-3, False), (-3, None, False, None), "idle", 0, {},
                      (True, None, False), (-2147483648, None, False),
                      (-3, 0, False), (-3, None, 0), (-3, None, None)):
            with self.subTest(value=value):
                self.app.GetRunningCommandInfo = lambda *args: value
                self.reject("TARGET_DOCUMENT_UNSAFE_EDIT")
                self.assertEqual(self.app.calls, [])

    def test_active_command_with_null_title_still_blocks(self):
        def active_command(command, title, active):
            command.value = 9
            title.value = None
            active.value = True
        self.app.GetRunningCommandInfo = active_command
        self.reject("TARGET_DOCUMENT_UNSAFE_EDIT")
        self.assertEqual(self.app.calls, [])

    def test_unwritten_activation_status_fences(self):
        def incomplete(name, errors):
            self.app.ActiveDoc = self.app.documents[1]
            return self.app.ActiveDoc
        self.app.on_activate = incomplete
        self.reject("TARGET_DOCUMENT_ACTIVATION_FAILED", recovery=True)
        self.assertEqual(len(self.app.calls), 1)

    def test_configuration_or_type_mismatch_does_not_switch(self):
        self.reject("TARGET_DOCUMENT_MISMATCH", target={"path": B, "configuration": "Other"})
        self.reject("TARGET_DOCUMENT_MISMATCH", target={"path": B, "document_type": 2})
        self.assertEqual(self.app.calls, [])

    def test_document_type_must_match_native_extension_even_without_assertion(self):
        self.app.documents[1].kind = 2
        self.reject("TARGET_DOCUMENT_MISMATCH")

    def test_missing_active_document_can_select_a_clean_loaded_target(self):
        self.app.ActiveDoc = None
        self.assertTrue(self.select()["activated"])

    def test_post_activation_sketch_or_command_change_fences(self):
        for field in ("sketch", "running_command"):
            self.app = Application()
            self.automation = Automation(self.app)
            def unexpected_edit(name, errors):
                errors.value = 0
                self.app.ActiveDoc = self.app.documents[1]
                if field == "sketch": self.app.ActiveDoc.sketch = object()
                else: self.app.running_command = True
                return self.app.ActiveDoc
            self.app.on_activate = unexpected_edit
            self.reject("TARGET_DOCUMENT_UNSAFE_EDIT", recovery=True)

    def test_post_activation_in_context_assembly_edit_fences(self):
        target = r"C:\synthetic\assembly.SLDASM"
        self.app.documents[1] = Document(target, kind=2)
        def unexpected_edit(name, errors):
            errors.value = 0
            self.app.ActiveDoc = self.app.documents[1]
            self.app.ActiveDoc.editing_self = False
            return self.app.ActiveDoc
        self.app.on_activate = unexpected_edit
        self.reject("TARGET_DOCUMENT_UNSAFE_EDIT", target=target, recovery=True)

    def test_modal_guard_prevents_any_inventory_read(self):
        self.automation._strict_document_inventory = Mock(side_effect=AssertionError("No COM"))
        with self.assertRaises(TargetDocumentError) as raised:
            self.select(ready=lambda: False)
        self.assertEqual(raised.exception.code, "TARGET_DOCUMENT_UI_BLOCKED")
        self.automation._strict_document_inventory.assert_not_called()

    def test_null_activation_and_warning_require_recovery(self):
        self.app.on_activate = lambda name, errors: None
        self.reject("TARGET_DOCUMENT_ACTIVATION_FAILED", recovery=True)
        def warning(name, errors):
            self.app.ActiveDoc = self.app.documents[1]
            errors.value = 1
            return self.app.ActiveDoc
        self.app.on_activate = warning
        self.reject("TARGET_DOCUMENT_ACTIVATION_FAILED", recovery=True)
        self.assertEqual(len(self.app.calls), 2)

    def test_exception_after_activation_is_not_retried_or_restored(self):
        def fail(name, errors):
            self.app.ActiveDoc = self.app.documents[1]
            raise RuntimeError("Synthetic uncertain activation")
        self.app.on_activate = fail
        self.reject("TARGET_DOCUMENT_STATE_UNAVAILABLE", recovery=True)
        self.assertEqual(self.app.ActiveDoc.path, B)
        self.assertEqual(len(self.app.calls), 1)

    def test_returned_wrong_doc_and_unchanged_active_doc_both_fence(self):
        def unchanged(name, errors):
            errors.value = 0
            return self.app.documents[0]
        self.app.on_activate = unchanged
        self.reject("TARGET_DOCUMENT_ACTIVATION_MISMATCH", recovery=True)
        def wrong_active(name, errors):
            errors.value = 0
            return self.app.documents[1]
        self.app.on_activate = wrong_active
        self.reject("TARGET_DOCUMENT_PRESERVATION_FAILED", recovery=True)

    def test_unrelated_dirty_or_configuration_change_fences(self):
        def mutate_other(name, errors):
            errors.value = 0
            self.app.ActiveDoc.dirty = True
            self.app.ActiveDoc = self.app.documents[1]
            return self.app.ActiveDoc
        self.app.on_activate = mutate_other
        self.reject("TARGET_DOCUMENT_PRESERVATION_FAILED", recovery=True)

    def test_typed_return_normalization(self):
        def typed_return(name, errors):
            self.app.ActiveDoc = self.app.documents[1]
            return self.app.ActiveDoc, 0
        self.app.on_activate = typed_return
        self.assertTrue(self.select()["activated"])


class NativeTargetRoutingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        synthetic_boundaries(self.stack)
        self.app = Application()
        self.directory = tempfile.TemporaryDirectory(prefix="target-double-")
        self.addCleanup(self.directory.cleanup)
        self.calls = []
        async def dispatch(name, arguments):
            require_local_dispatch()
            self.calls.append((name, arguments, self.app.ActiveDoc.path))
            return [{"type": "text", "text": json.dumps({"success": True, "data": {}})}]
        self.native = backend(self.directory.name, self.app, dispatch)
        self.addCleanup(self.native._loop.close)

    def execute(self, target=B, **arguments):
        return unwrap_result(self.native.execute("execute_cad_plan", {
            **arguments, "_coordination": {"target_document": target}}))

    def test_target_switch_precedes_complete_plan_dispatch(self):
        operations = [{"op": "rename_body", "args": {}}, {"op": "body_volume", "args": {}}]
        reply = self.execute(operations=operations)
        self.assertTrue(reply["success"])
        self.assertEqual(self.calls, [("execute_cad_plan", {"operations": operations}, B)])
        self.assertTrue(reply["data"]["document_selection"]["inventory_preserved"])

    def test_precheck_rejection_does_not_dispatch_or_fence(self):
        reply = self.execute(r"C:\synthetic\closed.SLDPRT")
        self.assertFalse(reply["success"])
        self.assertFalse(reply["data"]["requires_recovery"])
        self.assertEqual(self.calls, [])

    def test_native_idle_shape_allows_selected_plan_dispatch(self):
        def native_idle(command, title, active):
            command.value = -3
            active.value = False
        self.app.GetRunningCommandInfo = native_idle
        reply = self.execute(operations=[])
        self.assertTrue(reply["success"])
        self.assertEqual(self.calls, [("execute_cad_plan", {"operations": []}, B)])
        self.assertEqual(len(self.app.calls), 1)

    def test_unknown_or_active_command_never_activates_or_dispatches_geometry(self):
        for value in (None, (-2147483648, None, False), (-3, None, True),
                      (-3, None, 0), (-3, None, False, None)):
            with self.subTest(value=value):
                self.app.GetRunningCommandInfo = lambda *args: value
                reply = self.execute(operations=[{"op": "rename_body", "args": {}}])
                self.assertEqual(reply["error_name"], "TARGET_DOCUMENT_UNSAFE_EDIT")
                self.assertFalse(reply["data"]["requires_recovery"])
                self.assertEqual(self.calls, [])
                self.assertEqual(self.app.calls, [])

    def test_active_command_after_selection_fences_before_geometry_dispatch(self):
        replies = iter([(-3, None, False), (-3, None, False), (9, None, True)])
        self.app.GetRunningCommandInfo = lambda *args: next(replies)
        reply = self.execute(operations=[{"op": "rename_body", "args": {}}])
        self.assertEqual(reply["error_name"], "TARGET_DOCUMENT_UNSAFE_EDIT")
        self.assertTrue(reply["data"]["requires_recovery"])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.app.calls), 1)

    def test_uncertain_activation_does_not_dispatch_and_fences(self):
        self.app.on_activate = lambda name, errors: None
        reply = self.execute()
        self.assertTrue(reply["data"]["requires_recovery"])
        self.assertEqual(self.calls, [])

    def test_conflict_is_rejected_before_native_binding(self):
        reply = unwrap_result(self.native.execute("execute_cad_plan", {
            "_coordination": {"target_document": B, "expected_document": B}}))
        self.assertEqual(reply["error_name"], "DOCUMENT_COORDINATION_CONFLICT")
        self.native._verify_bound.assert_not_called()

    def test_target_does_not_enable_document_creation_or_opening(self):
        for name in ("create_new_part", "open_document", "set_units"):
            reply = unwrap_result(self.native.execute(name, {"_coordination": {"target_document": B}}))
            self.assertEqual(reply["error_name"], "TARGET_DOCUMENT_UNSUPPORTED_TOOL")
        self.native._verify_bound.assert_not_called()

    def test_old_compare_only_guard_still_does_not_switch(self):
        reply = unwrap_result(self.native.execute("execute_cad_plan", {
            "_coordination": {"expected_document": B}}))
        self.assertEqual(reply["error_name"], "EXPECTED_DOCUMENT_MISMATCH")
        self.assertEqual(self.app.calls, [])

    def test_read_request_can_select_without_mutation_dispatch(self):
        reply = unwrap_result(self.native.execute("get_document_info", {
            "_coordination": {"target_document": B}}))
        self.assertTrue(reply["success"])
        self.assertEqual(self.calls[0][2], B)

    def test_already_active_sketch_edit_dispatches_same_document_without_activation(self):
        self.app.ActiveDoc.sketch = object()
        self.app.ActiveDoc.dirty = True
        self.app.running_command = True
        for tool in ("draw_line", "close_sketch"):
            reply = unwrap_result(self.native.execute(tool, {
                "_coordination": {"target_document": A}}))
            self.assertTrue(reply["success"])
            self.assertFalse(reply["data"]["document_selection"]["activated"])
        self.assertEqual([item[0] for item in self.calls], ["draw_line", "close_sketch"])
        self.assertEqual({item[2] for item in self.calls}, {A})
        self.assertEqual(self.app.calls, [])

    def test_already_active_assembly_component_edit_never_dispatches_geometry(self):
        target = r"C:\synthetic\assembly.SLDASM"
        assembly = Document(target, kind=2)
        assembly.editing_self = False
        self.app.documents[0] = self.app.ActiveDoc = assembly
        reply = self.execute(target)
        self.assertEqual(reply["error_name"], "TARGET_DOCUMENT_UNSAFE_EDIT")
        self.assertFalse(reply["data"]["requires_recovery"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.app.calls, [])

    def test_already_active_assembly_unknown_edit_target_never_dispatches_geometry(self):
        target = r"C:\synthetic\assembly.SLDASM"
        assembly = Document(target, kind=2)
        def unknown_edit_target():
            raise RuntimeError("Unknown edit identity")
        assembly.GetEditTarget = unknown_edit_target
        self.app.documents[0] = self.app.ActiveDoc = assembly
        reply = self.execute(target)
        self.assertEqual(reply["error_name"], "TARGET_DOCUMENT_STATE_UNAVAILABLE")
        self.assertFalse(reply["data"]["requires_recovery"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.app.calls, [])


def _target_broker(directory):
    """A real test-only broker with the production native boundary over doubles."""
    os.environ["SOLIDWORKS_MCP_TEST_BACKEND"] = "1"
    from solidworks_mcp.coordination.broker import Broker
    with ExitStack() as stack:
        synthetic_boundaries(stack)
        app = Application()
        timeline = Path(directory) / "target-timeline.jsonl"
        async def dispatch(name, arguments):
            require_local_dispatch()
            doc = app.ActiveDoc
            with timeline.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": "start", "path": doc.path,
                                         "label": arguments["label"]}) + "\n")
            # Two synthetic geometry steps remain inside one selected operation.
            for _ in range(2):
                time.sleep(.02)
                if app.ActiveDoc is not doc:
                    raise AssertionError("Active document changed inside the plan")
                doc.value += 1
            with timeline.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": "end", "path": doc.path,
                    "label": arguments["label"], "value": doc.value}) + "\n")
            return [{"type": "text", "text": json.dumps({"success": True, "data": {
                "path": doc.path, "value": doc.value}})}]
        native = backend(directory, app, dispatch)
        native.identity = lambda: {"backend": "synthetic_document_target", "target_pid": os.getpid()}
        native.pump = lambda: None
        native.close = native._loop.close
        broker = Broker(directory, "test")
        broker._initialize_backend = lambda: native
        broker.backend = native
        broker.run()


def _target_client(directory, target, index, barrier, output):
    os.environ["SOLIDWORKS_MCP_TEST_BACKEND"] = "1"
    client = QueueClient(directory, backend="test", auto_start=False)
    barrier.wait(15)
    replies = []
    for iteration in range(3):
        reply = unwrap_result(client.call_tool("execute_cad_plan", {
            "label": f"client-{index}-{iteration}",
            "coordination": {"target_document": target,
                "request_id": f"target-client-{index}-{iteration}", "wait_timeout_sec": 0}}))
        replies.append(reply)
        barrier.wait(15)
    output.put({"pid": os.getpid(), "replies": replies})


class TargetBrokerProcessTests(unittest.TestCase):
    def test_two_processes_keep_a_and_b_plans_isolated_in_one_fifo_queue(self):
        previous = os.environ.get("SOLIDWORKS_MCP_TEST_BACKEND")
        os.environ["SOLIDWORKS_MCP_TEST_BACKEND"] = "1"
        ctx = multiprocessing.get_context("spawn")
        processes = []
        client = None
        with tempfile.TemporaryDirectory(prefix="target-queue-synthetic-") as directory:
            try:
                broker_process = ctx.Process(target=_target_broker, args=(directory,))
                processes.append(broker_process)
                broker_process.start()
                client = QueueClient(directory, backend="test", auto_start=False)
                deadline = time.monotonic() + 15
                while True:
                    try:
                        client.request("status")
                        break
                    except (OSError, QueueError):
                        if not broker_process.is_alive() or time.monotonic() > deadline:
                            self.fail("Synthetic document broker did not become ready")
                        time.sleep(.025)
                barrier, output = ctx.Barrier(2), ctx.Queue()
                children = [ctx.Process(target=_target_client,
                    args=(directory, target, index, barrier, output)) for index, target in enumerate((A, B))]
                processes.extend(children)
                for child in children: child.start()
                results = [output.get(timeout=20) for _ in children]
                for child in children:
                    child.join(10)
                    self.assertEqual(child.exitcode, 0)
                self.assertEqual(len({item["pid"] for item in results}), 2)
                for item in results:
                    self.assertTrue(all(reply["success"] for reply in item["replies"]))
                jobs = client.request("list", {"limit": 20})["jobs"]
                jobs = [client.wait(job["id"], timeout=15) for job in jobs]
                self.assertEqual(len(jobs), 6)
                self.assertTrue(all(job["state"] == "completed" for job in jobs))
                timeline = [json.loads(line) for line in (Path(directory) / "target-timeline.jsonl").read_text().splitlines()]
                self.assertEqual([item["kind"] for item in timeline], ["start", "end"] * 6)
                ordered = sorted(jobs, key=lambda job: job["seq"])
                expected_labels = [job["request_id"].replace("target-", "") for job in ordered]
                self.assertEqual([item["label"] for item in timeline if item["kind"] == "start"], expected_labels)
                for path in (A, B):
                    ends = [item for item in timeline if item["kind"] == "end" and item["path"] == path]
                    self.assertEqual([item["value"] for item in ends], [2, 4, 6])
                self.assertFalse(client.request("status")["recovery_required"])
                output.close()
                output.join_thread()
            finally:
                if client is not None:
                    try: client.request("shutdown")
                    except (OSError, QueueError): pass
                for process in processes:
                    process.join(10)
                    if process.is_alive():
                        # Only exact synthetic children created by this test.
                        process.terminate()
                        process.join(5)
                if previous is None: os.environ.pop("SOLIDWORKS_MCP_TEST_BACKEND", None)
                else: os.environ["SOLIDWORKS_MCP_TEST_BACKEND"] = previous


if __name__ == "__main__":
    unittest.main()
