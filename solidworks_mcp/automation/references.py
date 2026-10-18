"""Verified, locale-independent sketch references and edit-state transitions."""

import math
import time

from .com_utils import (com_get, create_select_data, resolve_solidworks_constant,
                        select_by_id2, transform_point, typed)


_UNREADABLE = object()


def active_sketch_state(doc):
    """Return (known, sketch); an unreadable accessor is not proof of exit."""
    manager = com_get(doc, "SketchManager", default=None)
    typed_manager = typed(manager, "ISketchManager")
    typed_doc = typed(doc, "IModelDoc2")
    typed_values = (com_get(typed_manager, "ActiveSketch", default=_UNREADABLE),
                    com_get(typed_doc, "GetActiveSketch2", default=_UNREADABLE))
    typed_available = [value for value in typed_values if value is not _UNREADABLE]
    if typed_available:
        # Typed invocation avoids a stale dynamic-dispatch property binding.
        for value in typed_available:
            if value is not None:
                return True, value
        return True, None
    values = (com_get(manager, "ActiveSketch", default=_UNREADABLE),
              com_get(doc, "GetActiveSketch2", default=_UNREADABLE))
    available = [value for value in values if value is not _UNREADABLE]
    for value in available:
        if value is not None:
            return True, value
    return bool(available), None


def exit_active_sketch(doc):
    """Exit only an observed active sketch and verify the resulting state.

    InsertSketch is a toggle. Never invoke it when already outside a sketch,
    or repeat it blindly while SolidWorks is publishing the previous exit.
    """
    known, active = active_sketch_state(doc)
    if not known:
        return {"success": False, "verified": False, "was_active": None,
                "attempts": 0, "message": "Active sketch state is unreadable"}
    if active is None:
        return {"success": True, "verified": True, "was_active": False,
                "attempts": 0, "message": "Already outside sketch edit mode"}
    errors = []
    manager = com_get(doc, "SketchManager", default=None)
    typed_manager = typed(manager, "ISketchManager")
    exit_manager = typed_manager if typed_manager is not None else manager
    for attempt, (owner, member) in enumerate(
            ((exit_manager, "InsertSketch"), (doc, "InsertSketch2")), 1):
        try:
            doc.ClearSelection2(True)
            # UpdateEditRebuild=False exits even when the failing sketch cannot
            # solve. Rebuild after verified exit, not inside the invalid edit.
            com_get(owner, member, False)
        except Exception as exc:
            errors.append(str(exc))
        # A successful COM invocation can publish ActiveSketch asynchronously.
        for _ in range(10):
            known, active = active_sketch_state(doc)
            if known and active is None:
                return {"success": True, "verified": True, "was_active": True,
                        "attempts": attempt, "message": "Exited sketch"}
            time.sleep(0.025)
        if not known:
            break
    return {"success": False, "verified": False, "was_active": True,
            "attempts": attempt, "message": "Sketch exit was not verified",
            "errors": errors}


def _features(doc):
    feature = com_get(doc, "FirstFeature", default=None)
    for _ in range(10000):
        if feature is None:
            return
        yield feature
        feature = com_get(feature, "GetNextFeature", default=None)


def select_standard_plane(doc, plane):
    """Select a plane through the origin with the requested world normal.

    IRefPlane.Transform maps canonical Front-plane coordinates to model space.
    Names are metadata only: renamed and localized planes keep their geometry.
    Ambiguous duplicate planes are rejected unless the request names one.
    """
    key = str(plane).strip().lower()
    aliases = {"front": "front", "front plane": "front", "спереди": "front",
               "top": "top", "top plane": "top", "сверху": "top",
               "right": "right", "right plane": "right", "справа": "right"}
    canonical = aliases.get(key)
    if canonical is None:
        return {"success": False, "message": f"Unknown standard plane: {plane}"}
    axis = {"front": 2, "top": 1, "right": 0}[canonical]
    candidates = []
    for feature in _features(doc):
        if str(com_get(feature, "GetTypeName2", default="")).lower() != "refplane":
            continue
        reference = com_get(feature, "GetSpecificFeature2", default=None)
        transform = com_get(reference, "Transform", default=None)
        values = com_get(transform, "ArrayData", default=None)
        try:
            data = [float(value) for value in values]
            normal = data[6:9]
            origin = data[9:12]
            length = math.sqrt(sum(value * value for value in normal))
            if (len(data) < 13 or not all(math.isfinite(value) for value in data)
                    or length <= 1e-12):
                continue
            normal = [value / length for value in normal]
            if abs(abs(normal[axis]) - 1.0) > 1e-8:
                continue
            if abs(sum(a * b for a, b in zip(origin, normal))) > 1e-8:
                continue
        except (TypeError, ValueError):
            continue
        candidates.append((feature, str(com_get(feature, "Name", default=""))))
    if len(candidates) > 1:
        exact = [candidate for candidate in candidates if candidate[1].lower() == key]
        if len(exact) == 1:
            candidates = exact
    if len(candidates) != 1:
        return {"success": False, "message": "Standard plane geometry is unavailable or ambiguous",
                "plane": canonical, "candidates": [name for _, name in candidates]}
    feature, name = candidates[0]
    doc.ClearSelection2(True)
    selected = bool(com_get(feature, "Select2", False, 0, default=False))
    if not selected:
        selected = select_by_id2(doc, name, "PLANE")
    return {"success": selected, "name": name, "plane": canonical,
            "method": "reference_plane_transform", "geometry_verified": True}


def select_sketch_origin(doc, sketch, append=False):
    """Select and verify an actual origin point, independent of UI language."""
    transform = com_get(sketch, "ModelToSketchTransform", default=None)
    inverse = com_get(transform, "Inverse", default=None)
    data = com_get(inverse, "ArrayData", default=None)
    if data is None:
        return False
    try:
        origin = transform_point(data, (0.0, 0.0, 0.0))
        origin_type = resolve_solidworks_constant("swSketchPointType_Origin")
        selection_type = resolve_solidworks_constant("swSelEXTSKETCHPOINTS")
    except (TypeError, ValueError, LookupError):
        return False
    manager = com_get(doc, "SelectionManager", default=None)
    # SW2025's model origin reports Type=1, not swSketchPointType_Origin.
    # Resolve it from OriginProfileFeature, whose type is locale independent,
    # and select its real point object. IDs/coordinates alone are not identity.
    for feature in _features(doc):
        if str(com_get(feature, "GetTypeName2", default="")) != "OriginProfileFeature":
            continue
        profile = com_get(feature, "GetSpecificFeature2", default=None)
        points = com_get(profile, "GetSketchPoints2", default=None) or []
        for point in points:
            try:
                coords = [float(com_get(point, axis)) for axis in ("X", "Y", "Z")]
            except (TypeError, ValueError, AttributeError):
                continue
            if math.dist(coords, origin) > 1e-8:
                continue
            selection_data = create_select_data(doc, 0)
            if not com_get(point, "Select4", append, selection_data, default=False):
                continue
            count = com_get(manager, "GetSelectedObjectCount2", -1, default=0)
            selected = com_get(manager, "GetSelectedObject6", count, -1, default=None)
            kind = com_get(manager, "GetSelectedObjectType3", count, -1, default=None)
            return bool(kind == selection_type and same_object(doc, selected, point))
    if not select_by_id2(doc, "", "EXTSKETCHPOINT", *origin, append=append):
        return False
    count = com_get(manager, "GetSelectedObjectCount2", -1, default=0)
    if not count:
        return False
    selected = com_get(manager, "GetSelectedObject6", count, -1, default=None)
    kind = com_get(manager, "GetSelectedObjectType3", count, -1, default=None)
    # EXTSKETCHPOINT alone could be an external point from a different sketch.
    return bool(kind == selection_type and selected is not None and
                com_get(selected, "Type", default=None) == origin_type)


def same_object(doc, left, right):
    """Compare COM identity, then document-scoped persistent references."""
    if left is None or right is None:
        return False
    if left is right:
        return True
    left_ole, right_ole = getattr(left, "_oleobj_", None), getattr(right, "_oleobj_", None)
    if left_ole is not None and right_ole is not None:
        try:
            if left_ole == right_ole:
                return True
        except Exception:
            pass
    extension = com_get(doc, "Extension", default=None)
    left_ref = com_get(extension, "GetPersistReference3", left, default=None)
    right_ref = com_get(extension, "GetPersistReference3", right, default=None)
    try:
        return bool(left_ref and right_ref and bytes(left_ref) == bytes(right_ref))
    except (TypeError, ValueError):
        return False


def same_sketch_point(doc, sketch, left, right):
    """GetID is unique only for points in the same, verified owning sketch."""
    if left is None or right is None or sketch is None:
        return False
    left_owner = com_get(left, "GetSketch", default=None)
    right_owner = com_get(right, "GetSketch", default=None)
    if not (same_object(doc, sketch, left_owner) and
            same_object(doc, sketch, right_owner)):
        return False
    if same_object(doc, left, right):
        return True
    left_id = com_get(left, "GetID", default=None)
    right_id = com_get(right, "GetID", default=None)
    try:
        return len(left_id) == 2 and tuple(left_id) == tuple(right_id)
    except TypeError:
        return False
