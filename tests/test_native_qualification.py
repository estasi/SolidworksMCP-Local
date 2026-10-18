"""Offline contract checks for the opt-in native qualification harness."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from scripts import qualify_native_queue as qualification
from solidworks_mcp.coordination.cad_backend import validate_script


def metrics(depth=.02):
    return {"depth_m": depth, "body_count": 1,
            "bbox_m": [-.01, -.01, 0, .01, .01, depth],
            "volume_m3": .02 * .02 * depth, "solidworks_revision": "33.2.0"}


class NativeQualificationTests(unittest.TestCase):
    def test_help_and_missing_idle_ack_never_import_native_routes(self):
        for arguments, code in ((["--help"], 0),
                                (["--output-dir", "synthetic-never-created"], 2)):
            with self.subTest(arguments=arguments), \
                    patch.object(qualification, "_imports", side_effect=AssertionError("Native route imported")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    qualification.main(arguments)
                self.assertEqual(stopped.exception.code, code)

    def test_generated_scripts_meet_trusted_synchronous_contract(self):
        for source in (qualification._seed_script(),
                       qualification._increment_script("synthetic-worker", .05),
                       qualification._SCRIPT_HELPERS + "\nresult = json.dumps(snapshot())\n"):
            validate_script(source)
            compile(source, "<native-qualification-synthetic>", "exec")

    def test_script_snapshot_uses_existing_mass_adapter_and_exact_volume(self):
        body = types.SimpleNamespace(GetBodyBox=lambda: metrics()["bbox_m"])
        dimension = types.SimpleNamespace(SystemValue=.02)
        doc = types.SimpleNamespace(GetBodies2=lambda *_: [body], Parameter=lambda _: dimension)
        auto = types.SimpleNamespace(_body_mass_props=Mock(return_value={"volume_exact": .000008}))
        def com_get(target, name, *args):
            value = getattr(target, name)
            return value(*args) if callable(value) else value
        context = {"doc": doc, "automation": auto, "com_get": com_get,
                   "sw": types.SimpleNamespace(RevisionNumber="33.2.0"), "json": json}
        exec(qualification._SCRIPT_HELPERS, context)
        snapshot = context["snapshot"]()
        self.assertEqual(snapshot["volume_m3"], .000008)
        auto._body_mass_props.assert_called_once_with(body, "m")
        qualification._check_geometry(snapshot, .02)

    def test_seed_uses_closed_fully_defined_profile_and_typed_feature_route(self):
        state = {"created": False}
        body = types.SimpleNamespace(GetBodyBox=lambda: metrics()["bbox_m"])
        doc = types.SimpleNamespace(FeatureManager=object(), EditRebuild3=True,
            GetBodies2=lambda *_: [body] if state["created"] else [],
            Parameter=lambda _: types.SimpleNamespace(SystemValue=.02))
        profile = Mock(return_value={"success": True, "data": {
            "status": "fully_defined", "sketch_name": "QueueQualificationProfile"}})
        def extrude(**kwargs):
            state["created"] = True
            return {"success": True, "data": {"typed_feature_manager": True,
                                              "feature_name": kwargs["feature_name"]}}
        auto = types.SimpleNamespace(create_parametric_sketch=profile,
            exit_sketch=Mock(return_value={"success": True, "data": {"verified": True}}),
            advanced_extrude=Mock(side_effect=extrude),
            save_document=Mock(return_value={"success": True}),
            _body_mass_props=Mock(return_value={"volume_exact": .000008}))
        def com_get(target, name, *args):
            value = getattr(target, name)
            return value(*args) if callable(value) else value
        context = {"doc": doc, "automation": auto, "com_get": com_get,
                   "sw": types.SimpleNamespace(RevisionNumber="33.2.0"), "json": json,
                   "typed": lambda obj, interface: object()}
        exec(qualification._seed_script(), context)
        request = profile.call_args.kwargs
        self.assertEqual(request["solve"], {"mode": "locked_trace", "target": "fully_defined"})
        self.assertEqual(request["validation"], {"require_closed": True, "closed_contours": 1})
        self.assertEqual(len(request["constraints"]), 4)
        self.assertEqual(len(request["entities"]), 4)
        auto.exit_sketch.assert_called_once_with()
        auto.advanced_extrude.assert_called_once_with(
            sketch_name="QueueQualificationProfile", depth=20, unit="mm",
            feature_name="QueueQualificationBoss", end_condition="blind",
            auto_verify=True, auto_flags=False)
        self.assertTrue(json.loads(context["result"])["seed_typed_feature_manager"])

    def test_failed_native_helper_preserves_full_structured_diagnostics(self):
        context = {"json": json}
        exec(qualification._SCRIPT_HELPERS, context)
        failure = {"success": False, "message": "Synthetic COM failure",
                   "data": {"api_error": "test", "typed_feature_manager": True,
                            "diagnostics": {"contours": 0}}}
        with self.assertRaises(RuntimeError) as stopped:
            context["require_ok"](failure, "TYPED_EXTRUSION")
        diagnostic = json.loads(str(stopped.exception))
        self.assertEqual(diagnostic["native_result"], failure)
        self.assertEqual(diagnostic["operation"], "TYPED_EXTRUSION")

    def test_admission_preserves_identity_trust_and_stable_request_id(self):
        client = Mock()
        client.submit.return_value = {"id": "job", "request_id": "request", "seq": 1,
            "state": "completed", "execution_context": {"target_pid": 42},
            "result": [{"type": "text", "text": '{"success":true,"data":{}}'}]}
        expected = {"path": r"C:\synthetic\queue.SLDPRT", "document_type": 1}
        _, summary = qualification._operation(client, "execute_python", {"code": "result = 1"},
            "request", "phase", expected=expected, trusted=True)
        args, kwargs = client.submit.call_args
        self.assertEqual(args[1]["_coordination"], {"expected_document": expected, "trusted_python": True})
        self.assertEqual(kwargs["request_id"], "request")
        self.assertEqual(summary["native_target_pid"], 42)
        client.wait.assert_not_called()

    def test_recovery_required_is_not_accepted_as_finished_qualification(self):
        client = Mock()
        native_result = [{"type": "text", "text": '{"success":false,"data":{"api_error":"synthetic"}}'}]
        client.submit.return_value = {"id": "job", "request_id": "request", "seq": 1,
                                      "state": "recovery_required", "result": native_result,
                                      "reason": "native failure"}
        with self.assertRaises(qualification.QualificationFailure) as stopped:
            qualification._operation(client, "execute_python", {"code": "result = 1"},
                                     "request", "phase", trusted=True)
        self.assertEqual(stopped.exception.code, "NATIVE_JOB_RECOVERY_REQUIRED")
        self.assertEqual(stopped.exception.job["native_result"], native_result)
        self.assertEqual(stopped.exception.job["journal_reason"], "native failure")

    def test_geometry_checks_count_shape_volume_and_revision(self):
        qualification._check_geometry(metrics(), .02)
        changes = ({"body_count": 2}, {"depth_m": .021}, {"volume_m3": .001},
                   {"solidworks_revision": "34.0.0"}, {"bbox_m": [0, 0, 0, .01, .01, .02]})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(qualification.QualificationFailure):
                qualification._check_geometry({**metrics(), **change}, .02)

    def test_reopen_tolerates_native_roundoff_but_rejects_translation(self):
        before = metrics()
        reopened = copy.deepcopy(before)
        reopened["volume_m3"] += 1e-16
        reopened["bbox_m"][0] += 1e-12
        qualification._check_reopened_geometry(before, reopened)
        reopened["bbox_m"] = [value + .001 for value in before["bbox_m"]]
        with self.assertRaises(qualification.QualificationFailure):
            qualification._check_reopened_geometry(before, reopened)

    def test_saved_working_file_hash_detects_same_size_same_timestamp_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "working.SLDPRT"
            path.write_bytes(b"original")
            inventory = {"documents": [{"key": "saved", "path": str(path)},
                                        {"key": "unsaved", "path": ""}]}
            before = qualification._working_file_evidence(inventory)
            self.assertEqual(set(before), {"saved"})
            self.assertEqual(before, qualification._working_file_evidence(inventory))
            stamp = path.stat()
            path.write_bytes(b"modified")
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            after = qualification._working_file_evidence(inventory)
            self.assertEqual(before["saved"]["size"], after["saved"]["size"])
            self.assertEqual(before["saved"]["mtime_ns"], after["saved"]["mtime_ns"])
            self.assertNotEqual(before["saved"]["sha256"], after["saved"]["sha256"])

    def test_missing_saved_working_file_fails_before_native_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            inventory = {"documents": [{"key": "saved", "path": str(Path(temporary) / "absent.SLDPRT")}]}
            with self.assertRaises(qualification.QualificationFailure) as stopped:
                qualification._working_file_evidence(inventory)
            self.assertEqual(stopped.exception.code, "WORKING_FILE_EVIDENCE_UNAVAILABLE")

    def test_document_preservation_permits_only_synthetic_addition_and_active_change(self):
        synthetic_path = os.path.abspath("synthetic-qualification.SLDPRT")
        synthetic = {"key": os.path.normcase(os.path.realpath(synthetic_path)),
                     "path": synthetic_path, "title": "synthetic-qualification.SLDPRT",
                     "type": 1, "dirty": False, "configuration": "Default"}
        working = {"key": "unsaved:3:drawing1", "path": "", "title": "Drawing1",
                   "type": 3, "dirty": True, "configuration": None}
        before = {"strict": True, "count": 1, "documents": [working], "active_document": working}
        after = {"strict": True, "count": 2, "documents": [working, synthetic],
                 "active_document": synthetic}
        qualification._check_document_preservation(before, after, synthetic_path)
        for field, value in (("dirty", False), ("configuration", "unexpected"), ("path", "changed")):
            changed = copy.deepcopy(after)
            changed["documents"][0][field] = value
            with self.subTest(field=field), self.assertRaises(qualification.QualificationFailure):
                qualification._check_document_preservation(before, changed, synthetic_path)
        changed = copy.deepcopy(after)
        changed["documents"].append({**working, "key": "another"})
        changed["count"] += 1
        with self.assertRaises(qualification.QualificationFailure):
            qualification._check_document_preservation(before, changed, synthetic_path)

    def test_preservation_accepts_initially_empty_session(self):
        synthetic_path = os.path.abspath("synthetic-qualification.SLDPRT")
        synthetic = {"key": os.path.normcase(os.path.realpath(synthetic_path)), "type": 1}
        before = {"strict": True, "count": 0, "documents": [], "active_document": None}
        after = {"strict": True, "count": 1, "documents": [synthetic], "active_document": synthetic}
        qualification._check_document_preservation(before, after, synthetic_path)

    def test_empty_automation_owned_session_fails_before_creating_document(self):
        inventory = {"strict": True, "count": 0, "documents": [], "active_document": None,
                     "session": {"user_control": False, "visible": True}}
        with self.assertRaises(qualification.QualificationFailure) as stopped:
            qualification._preservation_preflight(inventory)
        self.assertEqual(stopped.exception.code, "EMPTY_AUTOMATION_OWNED_SESSION")
        inventory["session"]["user_control"] = True
        qualification._preservation_preflight(inventory)
        inventory["session"]["user_control"] = None
        with self.assertRaises(qualification.QualificationFailure):
            qualification._preservation_preflight(inventory)


if __name__ == "__main__":
    unittest.main()
