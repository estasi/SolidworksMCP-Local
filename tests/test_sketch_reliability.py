"""Regression checks for locale, sketch identity, and failure restoration."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.references import (
    active_sketch_state, exit_active_sketch, same_sketch_point, select_sketch_origin,
    select_standard_plane)
from tests.test_v6 import FakeAutomation, FakePoint, FakeSegment


IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
TOP = [1, 0, 0, 0, 0, -1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0]


class Plane:
    def __init__(self, name, transform):
        self.Name = name
        self.transform = transform
        self.next = None
        self.selected = False

    def GetTypeName2(self):
        return "RefPlane"

    def GetSpecificFeature2(self):
        return SimpleNamespace(Transform=SimpleNamespace(ArrayData=self.transform))

    def GetNextFeature(self):
        return self.next

    def Select2(self, append, mark):
        self.selected = True
        return True


def plane_doc(*planes):
    for left, right in zip(planes, planes[1:]):
        left.next = right
    return SimpleNamespace(FirstFeature=planes[0], ClearSelection2=lambda _: None)


class IdentifiedPoint(FakePoint):
    def __init__(self, sketch, point_id, x=0.0, y=0.0):
        super().__init__(x, y)
        self.owner, self.point_id = sketch, point_id

    def GetSketch(self):
        return self.owner

    def GetID(self):
        return self.point_id


class SketchReliabilityTests(unittest.TestCase):
    def test_russian_planes_resolved_by_geometry_without_rename(self):
        top, front = Plane("Сверху", TOP), Plane("Спереди", IDENTITY)
        result = select_standard_plane(plane_doc(top, front), "Front")
        self.assertTrue(result["success"])
        self.assertTrue(front.selected)
        self.assertFalse(top.selected)
        self.assertEqual(front.Name, "Спереди")

    def test_wrongly_named_offset_plane_is_never_standard_front(self):
        offset = list(IDENTITY)
        offset[11] = 0.005
        wrong, front = Plane("Front Plane", offset), Plane("Renamed", IDENTITY)
        result = select_standard_plane(plane_doc(wrong, front), "Front")
        self.assertTrue(result["success"])
        self.assertEqual(result["name"], "Renamed")
        self.assertFalse(wrong.selected)

    def test_duplicate_or_unknown_plane_geometry_fails_closed(self):
        a, b = Plane("A", IDENTITY), Plane("B", IDENTITY)
        self.assertFalse(select_standard_plane(plane_doc(a, b), "Front")["success"])
        self.assertFalse(select_standard_plane(plane_doc(Plane("Спереди", None)), "Front")["success"])

    def test_already_exited_does_not_toggle_new_sketch(self):
        calls = []
        doc = SimpleNamespace(SketchManager=SimpleNamespace(ActiveSketch=None),
                              InsertSketch2=lambda _: calls.append(True))
        result = exit_active_sketch(doc)
        self.assertTrue(result["success"])
        self.assertFalse(result["was_active"])
        self.assertEqual(calls, [])

    def test_unreadable_edit_state_is_not_reported_as_exit(self):
        self.assertFalse(exit_active_sketch(SimpleNamespace())["success"])

    def test_typed_active_sketch_accessor_overrides_stale_dynamic_null(self):
        sketch = object()
        doc = SimpleNamespace(SketchManager=SimpleNamespace(ActiveSketch=None))
        typed_doc = SimpleNamespace(GetActiveSketch2=lambda: sketch)
        with patch("solidworks_mcp.automation.references.typed", side_effect=lambda obj, name:
                   typed_doc if name == "IModelDoc2" else None):
            known, actual = active_sketch_state(doc)
        self.assertTrue(known)
        self.assertIs(actual, sketch)

    def test_refused_exit_fails_without_false_success(self):
        doc = SimpleNamespace(SketchManager=SimpleNamespace(
            ActiveSketch=object(), InsertSketch=lambda _: None),
            ClearSelection2=lambda _: None, InsertSketch2=lambda _: None)
        with patch("solidworks_mcp.automation.references.time.sleep"):
            self.assertFalse(exit_active_sketch(doc)["success"])

    def test_verified_exit_checks_both_available_accessors(self):
        manager = SimpleNamespace(ActiveSketch=object())
        manager.InsertSketch = lambda _: setattr(manager, "ActiveSketch", None)
        doc = SimpleNamespace(SketchManager=manager, ClearSelection2=lambda _: None,
                              GetActiveSketch2=lambda: manager.ActiveSketch)
        self.assertTrue(exit_active_sketch(doc)["verified"])

    def test_exit_prefers_typed_insert_without_rebuild(self):
        manager = SimpleNamespace(ActiveSketch=object())
        calls = []
        def close(rebuild):
            calls.append(rebuild)
            manager.ActiveSketch = None
        typed_manager = SimpleNamespace(InsertSketch=close)
        doc = SimpleNamespace(SketchManager=manager, ClearSelection2=lambda _: None)
        with patch("solidworks_mcp.automation.references.typed", return_value=typed_manager):
            self.assertTrue(exit_active_sketch(doc)["success"])
        self.assertEqual(calls, [False])

    def test_solidworks_2025_type_one_origin_verified_through_origin_feature(self):
        selected = SimpleNamespace(Type=1, X=0, Y=0, Z=0,
                                   Select4=lambda *args: True)
        manager = SimpleNamespace(GetSelectedObjectCount2=lambda _: 1,
                                  GetSelectedObject6=lambda *args: selected,
                                  GetSelectedObjectType3=lambda *args: 25)
        feature = SimpleNamespace(GetTypeName2=lambda: "OriginProfileFeature",
            GetSpecificFeature2=lambda: SimpleNamespace(GetSketchPoints2=lambda: [selected]),
            GetNextFeature=lambda: None)
        doc = SimpleNamespace(SelectionManager=manager, FirstFeature=feature)
        sketch = SimpleNamespace(ModelToSketchTransform=SimpleNamespace(
            Inverse=SimpleNamespace(ArrayData=IDENTITY)))
        with patch("solidworks_mcp.automation.references.select_by_id2") as coordinate_select:
            self.assertTrue(select_sketch_origin(doc, sketch))
            coordinate_select.assert_not_called()

    def test_coordinate_origin_selection_requires_origin_point_type(self):
        selected = SimpleNamespace(Type=8)
        manager = SimpleNamespace(GetSelectedObjectCount2=lambda _: 1,
                                  GetSelectedObject6=lambda *args: selected,
                                  GetSelectedObjectType3=lambda *args: 25)
        doc = SimpleNamespace(SelectionManager=manager)
        sketch = SimpleNamespace(ModelToSketchTransform=SimpleNamespace(
            Inverse=SimpleNamespace(ArrayData=IDENTITY)))
        with patch("solidworks_mcp.automation.references.select_by_id2", return_value=True) as select:
            self.assertTrue(select_sketch_origin(doc, sketch))
            self.assertEqual(select.call_args.args[1:3], ("", "EXTSKETCHPOINT"))
            selected.Type = 10
            self.assertFalse(select_sketch_origin(doc, sketch))

    def test_same_id_in_different_sketches_is_not_same_point(self):
        automation = FakeAutomation()
        sketch = automation.doc.sketch
        left = IdentifiedPoint(sketch, (0, 1))
        right = IdentifiedPoint(object(), (0, 1))
        self.assertFalse(same_sketch_point(automation.doc, sketch, left, right))
        self.assertFalse(same_sketch_point(automation.doc, sketch, right, right))

    def test_shared_point_id_with_verified_owner_skips_redundant_coincident(self):
        automation = FakeAutomation()
        sketch = automation.doc.sketch
        left, right = IdentifiedPoint(sketch, (0, 1)), IdentifiedPoint(sketch, (0, 1))
        records = {"a": {"points": {"end": left}}, "b": {"points": {"start": right}}}
        result = automation._apply_constraint(automation.doc, records,
            {"type": "coincident", "entities": ["a.end", "b.start"]})
        self.assertEqual(result["status"], "already_satisfied")
        self.assertEqual(sketch.relations, [])

    def test_same_coordinates_different_ids_still_create_relation(self):
        automation = FakeAutomation()
        sketch = automation.doc.sketch
        left, right = IdentifiedPoint(sketch, (0, 1)), IdentifiedPoint(sketch, (0, 2))
        records = {"a": {"points": {"end": left}}, "b": {"points": {"start": right}}}
        result = automation._apply_constraint(automation.doc, records,
            {"type": "coincident", "entities": ["a.end", "b.start"]})
        self.assertEqual(result["status"], "created")
        self.assertEqual(len(sketch.relations), 1)

    def test_unknown_relation_readback_never_accepts_void_com_success(self):
        automation = FakeAutomation()
        sketch = automation.doc.sketch
        sketch.GetRelations = lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))
        segment = FakeSegment((0, 0, 0), (1, 0, 0))
        with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
            automation._apply_constraint(automation.doc, {"a": {"object": segment}},
                {"type": "horizontal", "entities": ["a"]}, allow_redundant=True)
        self.assertEqual(sketch.relations, [])

    def test_unknown_solver_status_never_infers_zero_dof_from_no_free_items(self):
        automation = FakeAutomation()
        automation.doc.sketch.GetConstrainedStatus = lambda: None
        result = automation._analyze_sketch_objects(
            automation.doc, automation.doc.feature, automation.doc.sketch)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["remaining_dof"])
        self.assertIsNone(result["heuristic_dof_estimate"])

    def test_view_failure_does_not_mislabel_successful_sketch_activation(self):
        automation = FakeAutomation()
        automation._auto_normal_to = lambda *args, **kwargs: {"success": False}
        self.assertTrue(automation._activate_sketch_feature(automation.doc, automation.doc.feature))
        self.assertFalse(automation._last_sketch_activation_orientation["success"])

    def test_null_active_getfeature_matches_actual_feature_specific_sketch(self):
        automation = FakeAutomation()
        automation.doc.sketch.GetFeature = lambda: None
        with patch.object(automation.doc.SketchManager, "InsertSketch") as toggle:
            self.assertTrue(automation._activate_sketch_feature(
                automation.doc, automation.doc.feature, orient=False))
            toggle.assert_not_called()

    def test_reactivation_verifies_specific_sketch_when_getfeature_returns_null(self):
        automation = FakeAutomation()
        automation.doc.sketch.GetFeature = lambda: None
        manager = automation.doc.SketchManager
        manager.ActiveSketch = None
        manager.InsertSketch = lambda _: setattr(manager, "ActiveSketch", automation.doc.sketch)
        self.assertTrue(automation._activate_sketch_feature(
            automation.doc, automation.doc.feature, orient=False))

    def test_null_getfeature_never_activates_another_specific_sketch(self):
        automation = FakeAutomation()
        other = SimpleNamespace(GetFeature=lambda: None)
        manager = automation.doc.SketchManager
        manager.ActiveSketch = None
        manager.InsertSketch = lambda _: setattr(manager, "ActiveSketch", other)
        self.assertFalse(automation._activate_sketch_feature(
            automation.doc, automation.doc.feature, orient=False))

    def test_dimension_created_before_idimension_failure_is_rolled_back(self):
        automation = FakeAutomation()
        display = SimpleNamespace(GetDimension2=lambda _: None,
                                  Select4=lambda *args: True)
        automation.doc.AddDimension2 = lambda *args: display
        deleted = []
        automation._delete_objects = lambda doc, items: deleted.extend(items) or len(items)
        result = automation.add_dimensions_batch("Sketch1", [{"type": "length"}])
        self.assertFalse(result["success"])
        self.assertEqual(deleted, [display])
        self.assertEqual(result["data"]["error"]["details"]["created_before_failure"], 1)
        self.assertTrue(result["data"]["error"]["document_restored"])

    def test_failed_sketch_setup_discovers_and_rolls_back_inserted_feature(self):
        automation = FakeAutomation()
        automation.create_sketch = lambda _: {"success": False, "data": {"sketch_created": True}}
        automation._find_new_sketch_feature = lambda *args: automation.doc.feature
        rollback = []
        automation._rollback_created_sketch = lambda *args: rollback.append(args) or True
        result = automation.create_parametric_sketch("Failure", entities=[
            {"id": "a", "type": "line", "start": [0, 0], "end": [1, 0]}])
        self.assertFalse(result["success"])
        self.assertTrue(result["data"]["document_restored"])
        self.assertEqual(len(rollback), 1)

    def test_rebuild_exit_reactivates_own_created_sketch_before_view_verification(self):
        automation = FakeAutomation()
        manager = automation.doc.SketchManager
        def rebuild():
            manager.ActiveSketch = None
            return True
        activation_calls = []
        def insert(rebuild_on_exit):
            activation_calls.append(rebuild_on_exit)
            manager.ActiveSketch = automation.doc.sketch
        automation.doc.EditRebuild3 = rebuild
        manager.InsertSketch = insert
        def orient(*args, **kwargs):
            self.assertIs(manager.ActiveSketch, automation.doc.sketch)
            return {"success": True, "data": {"verified": True}}
        automation._auto_normal_to = orient
        result = automation.create_parametric_sketch("Reactivated", entities=[
            {"id": "a", "type": "line", "start": [0, 0], "end": [1, 0]}])
        self.assertTrue(result["success"], result)
        self.assertEqual(activation_calls, [False])
        self.assertIs(manager.ActiveSketch, automation.doc.sketch)

    def test_equation_addition_uses_typed_dispatch_and_reports_verified_count(self):
        automation = FakeAutomation()
        dynamic = SimpleNamespace(Add2=lambda *args: self.fail("Dynamic Add2 must not run"))
        equation_manager = SimpleNamespace(count=0, Status=0)
        equation_manager.GetCount = lambda: equation_manager.count
        def add(index, equation, solve):
            equation_manager.count += 1
            return 0
        equation_manager.Add2 = add
        automation.doc.GetEquationMgr = lambda: dynamic
        with patch("solidworks_mcp.automation.parametric.typed", side_effect=lambda obj, name:
                   equation_manager if name == "IEquationMgr" else None):
            result = automation.create_parametric_sketch("EquationSketch", entities=[
                {"id": "a", "type": "line", "start": [0, 0], "end": [1, 0]}],
                equations=[{"dimension": "D1@Existing", "expression": '"D2@Existing" * 5 / 2'}])
        self.assertTrue(result["success"], result)
        evidence = result["data"]["equations"][0]["verification"]
        self.assertEqual(evidence["adapter"], "typed")
        self.assertEqual((evidence["count_before"], evidence["count_after"]), (0, 1))

    def test_rejected_external_equation_cannot_claim_local_sketch_cleanup_restored_document(self):
        automation = FakeAutomation()
        manager = SimpleNamespace(GetCount=lambda: 0, Add2=lambda *args: -1, Status=42)
        automation.doc.GetEquationMgr = lambda: manager
        rollback = []
        automation._rollback_created_sketch = lambda *args: rollback.append(args) or True
        result = automation.create_parametric_sketch("RejectedEquation", entities=[
            {"id": "a", "type": "line", "start": [0, 0], "end": [1, 0]}],
            equations=[{"dimension": "D1@Existing", "expression": "50"}])
        self.assertFalse(result["success"])
        error = result["data"]["error"]
        self.assertFalse(error["document_restored"])
        self.assertEqual(len(rollback), 1)
        evidence = error["details"]["equation_attempts"][0]
        self.assertEqual((evidence["index"], evidence["status"], evidence["count_after"]), (-1, 42, 0))

    def test_equation_failure_inside_native_transaction_defers_child_cleanup(self):
        automation = FakeAutomation()
        automation._runtime.active_transaction_id = "outer-test-transaction"
        automation.doc.GetEquationMgr = lambda: SimpleNamespace(
            GetCount=lambda: 0, Add2=lambda *args: -1, Status=42)
        with patch.object(automation, "_rollback_created_sketch") as rollback:
            result = automation.create_parametric_sketch("NestedEquation", entities=[
                {"id": "a", "type": "line", "start": [0, 0], "end": [1, 0]}],
                equations=[{"dimension": "D1@Existing", "expression": "50"}])
            rollback.assert_not_called()
        self.assertFalse(result["success"])
        self.assertTrue(result["data"]["error"]["details"]["rollback_deferred_to_transaction"])
        self.assertFalse(result["data"]["error"]["document_restored"])


if __name__ == "__main__":
    unittest.main()
