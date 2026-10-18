"""Failure-path regression tests; no live SOLIDWORKS/COM connection."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from solidworks_mcp.automation.documents import DocumentOperations
from solidworks_mcp.automation.runtime import RuntimeState, error_result
from solidworks_mcp.automation.transactions import TransactionOperations


class DisplayDimension:
    def __init__(self, doc):
        self.doc = doc

    def GetDimension2(self, configuration):
        return SimpleNamespace(FullName="Width@Sketch1", SystemValue=self.doc.width,
                               DrivenState=0)


class Feature:
    Name = "Sketch1"

    def __init__(self, doc):
        self.doc = doc

    def GetTypeName2(self):
        return "ProfileFeature"

    def GetNextFeature(self):
        return None

    def GetFirstDisplayDimension(self):
        return DisplayDimension(self.doc)

    def GetNextDisplayDimension(self, current):
        return None

    def GetFirstSubFeature(self):
        return None


class Body:
    Name = "Boss-Extrude1"

    def __init__(self, doc):
        self.doc = doc

    def GetBodyBox(self):
        return [0, 0, 0, self.doc.width, .02, .01]

    def GetMassProperties(self, density):
        return [0, 0, 0, self.doc.width * .02 * .01, .001, 0]

    def GetFaces(self):
        return [1, 2, 3, 4, 5, 6]


class Extension:
    def __init__(self, doc):
        self.doc = doc
        self.start_ok = True
        self.finish_ok = True

    def StartRecordingUndoObject(self):
        if self.start_ok:
            self.doc.undo_width = self.doc.width
            self.doc.undo_dirty = self.doc.dirty
            return None  # native SOLIDWORKS method is void
        raise RuntimeError("undo recording unavailable")

    def FinishRecordingUndoObject2(self, label, hidden):
        assert hidden is False
        return self.finish_ok

    def SaveAs(self, path, version, options, export, errors, warnings):
        # A copy deliberately includes *unsaved* in-memory dimensions.
        Path(path).write_text(str(self.doc.width), encoding="utf-8")
        return True


class Document:
    def __init__(self, path=""):
        self.path = path
        self.title = "Original.SLDPRT"
        self.width = .045  # existing unsaved edit: disk fixture contains .040
        self.dirty = True
        self.undo_error = False
        self.undo_noop = False
        self.undo_calls = 0
        self.save_return = (False, 32, 0)
        self.Extension = Extension(self)
        self.SketchManager = SimpleNamespace(ActiveSketch=None)

    def FirstFeature(self):
        return Feature(self)

    def GetBodies2(self, kind, hidden):
        return [Body(self)]

    def GetSaveFlag(self):
        return self.dirty

    def SetSaveFlag(self):
        self.dirty = True

    def GetPathName(self):
        return self.path

    def EditUndo2(self, count):
        self.undo_calls += 1
        if self.undo_error:
            raise RuntimeError("undo rejected")
        if not self.undo_noop:
            self.width, self.dirty = self.undo_width, self.undo_dirty

    def EditRebuild3(self):
        return True

    def Save3(self, options, errors, warnings):
        return self.save_return


class Harness(TransactionOperations, DocumentOperations):
    def __init__(self, doc):
        self.doc = doc
        self._runtime = RuntimeState()
        self.closed = []
        self.opened = []
        self.operations_run = []
        self._sw_app = SimpleNamespace(CloseDoc=self.closed.append,
                                       GetDocuments=lambda: [self.doc],
                                       RevisionNumber="34.0")
        self._config = SimpleNamespace(keep_last_n_checkpoints=10)
        self._units = SimpleNamespace(default_unit=SimpleNamespace(value="mm"))

    def get_active_doc(self):
        return self.doc, None

    def _get_doc_path(self, doc):
        return doc.path

    def _get_doc_title(self, doc):
        return doc.title

    def _result(self, success, message, error_code=0, data=None):
        return {"success": success, "message": message, "data": data or {}}

    def _error(self, code, message, **kwargs):
        return error_result(self, code, message, **kwargs)

    def open_document(self, path):
        self.opened.append(path)
        return self._result(True, "Opened recovery copy", data={"path": path})

    def _call_operation(self, name, args):
        self.operations_run.append(name)
        self.doc.width = args.get("width", .060)
        return self._result(not args.get("fail", False), "operation")

    def delete_feature(self, *args, **kwargs):
        raise AssertionError("Existing dimension edit must not use feature deletion")


class TransactionReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "Original.SLDPRT"
        self.path.write_text("0.040 on disk", encoding="utf-8")
        self.doc = Document(str(self.path))
        self.h = Harness(self.doc)
        self.modal = patch("solidworks_mcp.automation.transactions.detect_modal_dialog",
                           return_value={"modal": False})
        self.modal.start()
        self.addCleanup(self.modal.stop)
        self.typed = patch("solidworks_mcp.automation.documents.typed",
                           side_effect=lambda doc, interface: doc)
        self.typed.start()
        self.addCleanup(self.typed.stop)
        self.transaction_typed = patch("solidworks_mcp.automation.transactions.typed",
                                       side_effect=lambda obj, interface: obj)
        self.transaction_typed_mock = self.transaction_typed.start()
        self.addCleanup(self.transaction_typed.stop)

    def run_plan(self, *, fail=False, save=True, checkpoint=True):
        # The harness injects a numeric state change under a supported existing
        # object operation to test rollback-engine failure handling. It does
        # not assert that native rename changes dimensions or that dimension
        # batch transactions are supported (those now fail closed separately).
        return self.h.run_transaction(
            "dimension_change", [{"op": "rename_feature",
                                  "args": {"width": .060, "fail": fail}}],
            checkpoint=({"path": str(Path(self.tmp.name) / "checkpoint.SLDPRT")}
                        if checkpoint else {"mode": "none"}),
            save_policy={"save_after_success": save},
            idempotency_key="dimension-change")

    def test_typed_false_save_tuple_is_not_success(self):
        self.doc.save_return = (False, 0, 0)
        self.assertFalse(self.h.save_document()["success"])

    def test_typed_true_with_error_bitmask_is_failure(self):
        self.doc.save_return = (True, 32, 2)
        result = self.h.save_document()
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["errors"], 32)

    def test_typed_true_with_warning_only_is_success(self):
        self.doc.save_return = (True, 0, 2)
        self.assertTrue(self.h.save_document()["success"])

    def test_dynamic_tuple_false_is_not_truthy_success(self):
        with patch("solidworks_mcp.automation.documents.typed", return_value=None):
            self.doc.save_return = (False, 0, 0)
            self.assertFalse(self.h.save_document()["success"])

    def test_dynamic_byref_error_prevents_close(self):
        def save(options, errors, warnings):
            errors.value = 32
            return True
        self.doc.Save3 = save
        with patch("solidworks_mcp.automation.documents.typed", return_value=None):
            result = self.h.close_document(save=True)
        self.assertFalse(result["success"])
        self.assertFalse(result["data"]["closed"])
        self.assertEqual(self.h.closed, [])

    def test_same_names_and_count_do_not_hide_changed_dimensions(self):
        before = self.h._transaction_snapshot(self.doc)
        self.doc.width = .060
        after = self.h._transaction_snapshot(self.doc)
        self.assertEqual(before["features"], after["features"])
        self.assertEqual(before["solid_body_count"], after["solid_body_count"])
        self.assertFalse(self.h._snapshots_equivalent(before, after))

    def test_failed_save_rolls_back_unsaved_dimension_and_never_caches_success(self):
        result = self.run_plan()
        self.assertFalse(result["success"])
        error = result["data"]["error"]
        self.assertEqual(error["code"], "DOCUMENT_UNSAVED")
        self.assertTrue(error["document_restored"])
        self.assertEqual(self.doc.width, .045)
        self.assertTrue(self.doc.dirty)
        self.assertEqual(self.doc.path, str(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "0.040 on disk")
        self.assertEqual((Path(self.tmp.name) / "checkpoint.SLDPRT").read_text(), "0.045")
        self.assertEqual(self.h.closed, [])
        self.assertEqual(self.h._runtime.idempotency, {})
        self.assertEqual(self.h._runtime.metrics["files_saved"], 0)

    def test_native_undo_noop_is_reported_as_failed_rollback_and_keeps_original(self):
        self.doc.undo_noop = True
        result = self.run_plan(fail=True, save=False)
        error = result["data"]["error"]
        self.assertFalse(error["document_restored"])
        details = error["details"]
        self.assertFalse(details["committed"])
        self.assertFalse(details["rollback"]["rolled_back"])
        self.assertTrue(details["rollback"]["checkpoint_reopened"])
        self.assertEqual(self.h.closed, [])
        self.assertEqual(self.doc.path, str(self.path))
        self.assertEqual(self.path.read_text(), "0.040 on disk")

    def test_native_undo_exception_does_not_claim_recovery_copy_is_original(self):
        self.doc.undo_error = True
        result = self.run_plan(fail=True, save=False)
        rollback = result["data"]["error"]["details"]["rollback"]
        self.assertFalse(rollback["document_restored"])
        self.assertTrue(rollback["checkpoint_reopened"])
        self.assertIn("undo rejected", rollback["undo"]["error"])
        self.assertEqual(self.h.closed, [])

    def test_existing_object_changes_fail_closed_without_native_undo(self):
        self.doc.Extension.start_ok = False
        result = self.run_plan(save=False)
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(self.h.operations_run, [])
        self.assertEqual(self.doc.width, .045)

    def test_existing_object_changes_fail_closed_without_typed_undo_interface(self):
        with patch("solidworks_mcp.automation.transactions.typed", return_value=None):
            result = self.run_plan(save=False, checkpoint=False)
        self.assertEqual(result["data"]["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(self.h.operations_run, [])

    def test_equation_sketch_rollback_rejected_before_checkpoint_or_operations(self):
        result = self.h.run_transaction(
            "equation", [{"op": "create_parametric_sketch", "args": {
                "equations": [{"dimension": "Width@Existing", "expression": "60mm"}]}}],
            checkpoint={"mode": "none"}, save_policy={"save_after_success": False})
        self.assertEqual(result["data"]["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertFalse(result["data"]["error"]["details"]["checkpoint_started"])
        self.assertEqual(self.h.operations_run, [])

    def test_dimension_batch_rollback_rejected_before_even_checkpoint_attempt(self):
        with patch.object(self.h, "_create_checkpoint",
                          side_effect=AssertionError("No checkpoint should be attempted")):
            result = self.h.run_transaction(
                "dimensions", [{"op": "add_dimensions_batch"}],
                checkpoint={"path": str(Path(self.tmp.name) / "must_not_exist.SLDPRT")})
        error = result["data"]["error"]
        self.assertEqual(error["code"], "CAPABILITY_UNAVAILABLE")
        self.assertFalse(error["details"]["mutations_started"])
        self.assertFalse(error["details"]["checkpoint_started"])
        self.assertEqual(self.h.operations_run, [])

    def test_explicit_leave_partial_allows_dimension_and_equation_operations(self):
        for operation in ({"op": "add_dimensions_batch"},
                          {"op": "create_parametric_sketch", "args": {
                              "equations": [{"dimension": "D1@Existing", "expression": "60mm"}]}}):
            result = self.h.run_transaction(
                "explicit_partial", [operation], on_failure="leave_partial",
                checkpoint={"mode": "none"}, save_policy={"save_after_success": False})
            self.assertTrue(result["success"])

    def test_atomic_child_cleanup_is_not_undone_again(self):
        self.h._call_operation = lambda name, args: self.h._error(
            "SKETCH_BUILD_FAILED", "Child removed its temporary sketch",
            document_restored=True)
        result = self.h.run_transaction(
            "already_clean", [{"op": "create_parametric_sketch"}],
            checkpoint={"mode": "none"}, save_policy={"save_after_success": False})
        error = result["data"]["error"]
        self.assertTrue(error["document_restored"])
        self.assertEqual(self.doc.undo_calls, 0)
        self.assertTrue(error["details"]["rollback"]["undo"]["nested_rollback_verified"])

    def test_matching_snapshot_without_child_proof_does_not_claim_restoration(self):
        self.h._call_operation = lambda name, args: self.h._error(
            "SKETCH_BUILD_FAILED", "Failure with unknown side effects",
            document_restored=False)
        result = self.h.run_transaction(
            "unknown_cleanup", [{"op": "create_parametric_sketch"}],
            checkpoint={"mode": "none"}, save_policy={"save_after_success": False})
        self.assertFalse(result["data"]["error"]["document_restored"])
        self.assertEqual(self.doc.undo_calls, 0)

    def test_child_cleanup_proof_does_not_cover_prior_successful_mutation(self):
        def operation(name, args):
            if name == "rename_feature":
                self.doc.width = .060
                return self.h._result(True, "Dimension changed")
            return self.h._error("SKETCH_BUILD_FAILED", "Child cleanup complete",
                                 document_restored=True)
        self.h._call_operation = operation
        result = self.h.run_transaction(
            "earlier_change", [{"op": "rename_feature"},
                               {"op": "create_parametric_sketch"}],
            checkpoint={"mode": "none"}, save_policy={"save_after_success": False})
        self.assertTrue(result["data"]["error"]["document_restored"])
        self.assertEqual(self.doc.undo_calls, 1)
        self.assertEqual(self.doc.width, .045)

    def test_undo_recording_calls_explicit_typed_interfaces(self):
        state = self.h._begin_undo_group(self.doc, "Typed group")
        self.h._finish_undo_group(self.doc, state)
        self.assertTrue(state["started"] and state["finished"])
        interfaces = [call.args[1] for call in self.transaction_typed_mock.mock_calls]
        self.assertEqual(interfaces, ["IModelDocExtension", "IModelDocExtension"])

    def append_only_cleanup_after_failed_group(self, change_existing=False):
        added = []
        self.h._walk_features_tx = lambda doc: [Feature(doc), *added]
        before = self.h._transaction_snapshot(self.doc)
        new_feature = Feature(self.doc)
        new_feature.Name = "NewFeature"
        added.append(new_feature)
        if change_existing:
            self.doc.width = .060
        deleted = []
        def delete(name, delete_absorbed=False):
            self.assertEqual(name, "NewFeature")
            self.assertFalse(delete_absorbed, "Pre-existing absorbed sketch must survive")
            deleted.append(name)
            added.clear()
            return self.h._result(True, "Removed only new feature")
        self.h.delete_feature = delete
        result = self.h._rollback_transaction(
            self.doc, before, None, allow_delete=True,
            undo={"started": True, "finished": False, "finish_attempted": True,
                  "name": "failed-native-group"})
        self.assertEqual(deleted, ["NewFeature"])
        self.assertEqual(self.doc.undo_calls, 0)
        self.assertEqual([f.Name for f in self.h._walk_features_tx(self.doc)], ["Sketch1"])
        return result

    def test_failed_native_group_still_cleans_only_appended_features(self):
        result = self.append_only_cleanup_after_failed_group()
        self.assertTrue(result["document_restored"])
        self.assertEqual(self.doc.width, .045)

    def test_append_cleanup_cannot_hide_changes_to_existing_geometry(self):
        result = self.append_only_cleanup_after_failed_group(change_existing=True)
        self.assertFalse(result["document_restored"])
        self.assertEqual(self.doc.width, .060, "Cleanup must not rewrite existing dimensions")

    def test_rollback_never_undoes_when_sketch_exit_cannot_be_verified(self):
        with patch("solidworks_mcp.automation.transactions.exit_active_sketch",
                   return_value={"success": False, "was_active": True}):
            result = self.run_plan(fail=True, save=False)
        self.assertFalse(result["data"]["error"]["document_restored"])
        self.assertEqual(self.doc.undo_calls, 0)
        self.assertEqual(self.h.closed, [])

    def test_partial_snapshot_cannot_claim_restoration(self):
        snapshot = self.h._transaction_snapshot(self.doc)
        snapshot["dimensions_complete"] = False
        self.assertFalse(self.h._snapshots_equivalent(snapshot, snapshot))

    def test_unreadable_edit_state_cannot_pass_snapshot_or_transaction_gate(self):
        with patch("solidworks_mcp.automation.transactions.active_sketch_state",
                   return_value=(False, None)):
            snapshot = self.h._transaction_snapshot(self.doc)
            self.assertFalse(snapshot["active_sketch_known"])
            self.assertFalse(self.h._snapshots_equivalent(snapshot, snapshot))
            result = self.run_plan(save=False, checkpoint=False)
        error = result["data"]["error"]
        self.assertEqual(error["code"], "CAPABILITY_UNAVAILABLE")
        self.assertFalse(error["details"]["mutations_started"])
        self.assertEqual(self.h.operations_run, [])

    def test_active_sketch_without_feature_is_not_mistaken_for_exited(self):
        self.doc.SketchManager.ActiveSketch = SimpleNamespace(GetFeature=lambda: None)
        snapshot = self.h._transaction_snapshot(self.doc)
        self.assertTrue(snapshot["active_sketch_known"])
        self.assertEqual(snapshot["active_sketch"], "<active>")
        result = self.run_plan(save=False, checkpoint=False)
        error = result["data"]["error"]
        self.assertEqual(error["code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(error["details"]["active_sketch"], "<active>")
        self.assertEqual(self.h.operations_run, [])

    def test_prior_dirty_flag_must_survive_but_clean_to_dirty_is_allowed(self):
        before = self.h._transaction_snapshot(self.doc)
        self.doc.dirty = False
        after = self.h._transaction_snapshot(self.doc)
        self.assertFalse(self.h._snapshots_equivalent(before, after))
        self.assertTrue(self.h._snapshots_equivalent(after, before))
        after["dirty"] = None
        self.assertFalse(self.h._snapshots_equivalent(before, after))

    def test_rollback_restores_preexisting_unsaved_flag_after_native_undo(self):
        def undo(count):
            self.doc.width = self.doc.undo_width
            self.doc.dirty = False
        self.doc.EditUndo2 = undo
        result = self.run_plan(fail=True, save=False, checkpoint=False)
        self.assertTrue(result["data"]["error"]["document_restored"])
        self.assertTrue(self.doc.dirty)
        self.assertEqual(self.doc.width, .045)

    def test_rollback_cannot_claim_restoration_when_dirty_flag_stays_cleared(self):
        def undo(count):
            self.doc.width = self.doc.undo_width
            self.doc.dirty = False
        self.doc.EditUndo2 = undo
        self.doc.SetSaveFlag = lambda: None
        result = self.run_plan(fail=True, save=False, checkpoint=False)
        self.assertFalse(result["data"]["error"]["document_restored"])
        self.assertFalse(self.doc.dirty)

    def test_unverifiable_visibility_and_appearance_fail_before_operations(self):
        for operation in ("show_body", "hide_body", "set_body_color",
                          "set_body_transparency"):
            with self.subTest(operation=operation):
                result = self.h.run_transaction(
                    "appearance", [{"op": operation}],
                    checkpoint={"mode": "none"},
                    save_policy={"save_after_success": False})
                error = result["data"]["error"]
                self.assertEqual(error["code"], "CAPABILITY_UNAVAILABLE")
                self.assertFalse(error["details"]["mutations_started"])
                self.assertEqual(self.h.operations_run, [])

    def test_explicit_leave_partial_policy_does_not_claim_appearance_rollback(self):
        result = self.h.run_transaction(
            "appearance", [{"op": "hide_body"}], on_failure="leave_partial",
            checkpoint={"mode": "none"}, save_policy={"save_after_success": False})
        self.assertTrue(result["success"])
        self.assertEqual(self.h.operations_run, ["hide_body"])

    def test_unknown_invariant_rejected_before_mutation(self):
        result = self.h.run_transaction(
            "bad_invariant", [{"op": "add_dimensions_batch"}],
            invariants={"volume": 8000})
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["error"]["code"], "INVALID_PLAN")
        self.assertEqual(self.h.operations_run, [])

    def test_volume_invariant_is_measured_and_checked(self):
        snapshot = self.h._transaction_snapshot(self.doc)
        failures = self.h._validate_invariants(
            {"volume_m3": 999.0}, snapshot, snapshot, [])
        self.assertTrue(any("volume_m3" in failure for failure in failures))

    def test_checkpoint_cannot_overwrite_original(self):
        result = self.h._save_copy(self.doc, str(self.path))
        self.assertFalse(result["success"])
        self.assertEqual(self.path.read_text(), "0.040 on disk")

    def test_checkpoint_false_tuple_with_written_file_still_fails(self):
        def partial_save(path, *args):
            Path(path).write_text("partial data")
            return False, 32, 0
        self.doc.Extension.SaveAs = partial_save
        result = self.h._save_copy(self.doc, str(Path(self.tmp.name) / "partial.SLDPRT"))
        self.assertFalse(result["success"])

    def test_legacy_saveas3_zero_error_code_means_success(self):
        def unavailable(*args):
            raise RuntimeError("extension unavailable")
        def saveas3(path, version, options):
            Path(path).write_text("saved copy")
            return 0
        self.doc.Extension.SaveAs = unavailable
        self.doc.SaveAs3 = saveas3
        result = self.h._save_copy(self.doc, str(Path(self.tmp.name) / "legacy.SLDPRT"))
        self.assertTrue(result["success"])

    def test_checkpoint_metadata_failure_clears_active_transaction_before_mutation(self):
        with patch("solidworks_mcp.automation.transactions.atomic_json_write",
                   side_effect=OSError("metadata cannot be written")):
            result = self.run_plan()
        self.assertFalse(result["success"])
        self.assertIsNone(self.h._runtime.active_transaction_id)
        self.assertEqual(self.h.operations_run, [])

    def test_checkpoint_retention_does_not_delete_unrelated_part(self):
        self.h._config.keep_last_n_checkpoints = 1
        self.run_plan(save=False)
        self.assertTrue(self.path.exists())

    @staticmethod
    def mark_owned_checkpoint(path):
        if not path.exists():
            path.write_text("checkpoint data", encoding="utf-8")
        Path(str(path) + ".manifest.json").write_text(json.dumps({
            "schema": "solidworks-mcp/checkpoint/v1"}), encoding="utf-8")
        os.utime(path, (1000000, 1000000))

    def make_retained_checkpoint(self):
        return self.h._create_checkpoint(
            self.doc, "retention",
            {"path": str(Path(self.tmp.name) / "new.SLDPRT"), "keep_last_n": 1},
            self.h._transaction_snapshot(self.doc))

    def test_retention_protects_source_and_other_open_checkpoint_files(self):
        other = Path(self.tmp.name) / "other_open.SLDPRT"
        closed = Path(self.tmp.name) / "closed.SLDPRT"
        for path in (self.path, other, closed):
            self.mark_owned_checkpoint(path)
        other_doc = Document(str(other))
        self.h._sw_app.GetDocuments = lambda: [self.doc, other_doc]
        result = self.make_retained_checkpoint()
        self.assertTrue(result["success"])
        for path in (self.path, other):
            self.assertTrue(path.exists())
            self.assertTrue(Path(str(path) + ".manifest.json").exists())
        self.assertFalse(closed.exists())
        self.assertFalse(Path(str(closed) + ".manifest.json").exists())
        self.assertEqual(result["retention_removed"], [str(closed)])

    def test_retention_skips_deletion_if_open_document_enumeration_fails(self):
        closed = Path(self.tmp.name) / "closed.SLDPRT"
        self.mark_owned_checkpoint(closed)
        def unavailable():
            raise RuntimeError("document enumeration failed")
        self.h._sw_app.GetDocuments = unavailable
        with patch("solidworks_mcp.automation.transactions.typed", return_value=None):
            result = self.make_retained_checkpoint()
        self.assertTrue(result["success"])
        self.assertTrue(closed.exists())
        self.assertEqual(result["retention_removed"], [])
        self.assertIn("retention_skipped", result)

    def test_retention_skips_deletion_if_one_open_document_path_is_unknown(self):
        closed = Path(self.tmp.name) / "closed.SLDPRT"
        self.mark_owned_checkpoint(closed)
        self.h._sw_app.GetDocuments = lambda: [self.doc, SimpleNamespace()]
        result = self.make_retained_checkpoint()
        self.assertTrue(closed.exists())
        self.assertEqual(result["retention_removed"], [])
        self.assertIn("unreadable", result["retention_skipped"])

    def test_retention_skips_enumeration_that_omits_named_source(self):
        closed = Path(self.tmp.name) / "closed.SLDPRT"
        self.mark_owned_checkpoint(closed)
        self.h._sw_app.GetDocuments = lambda: [Document("")]
        result = self.make_retained_checkpoint()
        self.assertTrue(closed.exists())
        self.assertEqual(result["retention_removed"], [])
        self.assertIn("omitted the source", result["retention_skipped"])

    def test_retention_never_prunes_the_new_recovery_copy_based_on_timestamp(self):
        closed = Path(self.tmp.name) / "future.SLDPRT"
        self.mark_owned_checkpoint(closed)
        original_save = self.doc.Extension.SaveAs
        def save_with_older_timestamp(path, *args):
            result = original_save(path, *args)
            os.utime(path, (1, 1))
            return result
        self.doc.Extension.SaveAs = save_with_older_timestamp
        result = self.make_retained_checkpoint()
        self.assertTrue(result["success"])
        self.assertTrue(Path(result["path"]).exists())
        self.assertFalse(closed.exists())

    def test_successful_transaction_alone_is_cached(self):
        self.doc.save_return = (True, 0, 0)
        result = self.run_plan()
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["committed"])
        self.assertIsNotNone(self.h._runtime.idempotent_get("dimension-change"))
        self.assertEqual(self.doc.width, .060)


if __name__ == "__main__":
    unittest.main()
