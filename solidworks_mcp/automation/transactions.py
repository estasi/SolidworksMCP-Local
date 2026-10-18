"""Transactional execution, CAD plans, capabilities, and model graphs."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import inspect
import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pythoncom
import win32com.client

from ..constants import SwErrors, SwSaveAsOptions
from .com_utils import (com_get, detect_modal_dialog, get_typed_module,
                        out_i4, null_dispatch, typed)
from .documents import normalize_save_result
from .references import active_sketch_state, exit_active_sketch
from .runtime import atomic_json_write, default_state_dir


from .. import __version__ as MCP_VERSION
MAX_PLAN_OPERATIONS = 100


logger = logging.getLogger(__name__)


class TransactionOperations:
    """Mixin providing safe compound operations over one COM apartment."""

    # Explicit whitelist.  Raw Python and recursive transaction/plan calls are
    # intentionally absent.
    PLAN_OPERATIONS = {
        "save_document", "create_parametric_sketch", "add_dimensions_batch",
        "advanced_extrude", "advanced_cut", "revolve_boss", "shell",
        "fillet_edges", "chamfer_edges", "reference_plane", "reference_axis",
        "linear_pattern", "circular_pattern", "mirror_feature",
        "rename_feature", "rename_body", "delete_feature", "show_body",
        "hide_body", "set_body_color", "set_body_transparency",
        "export_file", "export_bundle", "create_revolved_body",
        "create_swept_member", "create_multibody_insert",
        "create_semantic_primitive", "image_to_sketch",
        "export_sketch_geometry", "render_sketch_svg",
        "compare_sketches", "compare_sketch_to_reference",
        "compare_body_silhouette_to_image", "check_clearance",
        "body_volume", "probe_rays", "probe_section",
        "rename_new_body", "verify_named_body", "validate_sweep_path",
        "validate_sweep_profile", "create_sweep_feature",
    }

    # These operations may modify existing objects without adding a feature
    # that can simply be removed. They need a verified native undo group.
    EXISTING_STATE_OPERATIONS = {
        "add_dimensions_batch", "rename_feature", "rename_body",
        "delete_feature", "show_body", "hide_body", "set_body_color",
        "set_body_transparency", "rename_new_body",
    }
    # Display properties lack authoritative rollback snapshots. Live SW2025
    # also rejected grouped Undo across the dimension-edit sketch lifecycle.
    # Do not advertise transactional rollback for these operations.
    UNVERIFIED_ROLLBACK_OPERATIONS = {
        "show_body", "hide_body", "set_body_color", "set_body_transparency",
        "add_dimensions_batch",
    }
    INVARIANT_KEYS = {
        "expected_new_features", "required_bodies", "solid_body_count",
        "required_files", "no_modal_dialog", "sketch_status", "volume_m3",
    }

    @staticmethod
    def _elapsed_budget_exceeded(started: float,
                                 budget: Optional[Dict[str, Any]]) -> bool:
        limit = float((budget or {}).get("max_elapsed_sec", 300))
        if limit < 0:
            raise ValueError("max_elapsed_sec must be non-negative")
        return time.monotonic() - started >= limit

    def _walk_features_tx(self, doc):
        result = []
        feat = com_get(doc, "FirstFeature", default=None)
        guard = 0
        while feat is not None and guard < 10000:
            guard += 1
            result.append(feat)
            feat = com_get(feat, "GetNextFeature", default=None)
        return result

    def _persist_reference(self, doc, obj):
        try:
            raw = doc.Extension.GetPersistReference3(obj)
            if raw:
                return base64.b64encode(bytes(raw)).decode("ascii")
        except Exception:
            pass
        return None

    def _transaction_snapshot(self, doc,
                              include_persistent_ids: bool = False) -> Dict[str, Any]:
        """Capture geometry and dimension evidence, not just object names.

        This is verification evidence for native Undo, not an authoritative
        serialization of a SOLIDWORKS document. Save-copy checkpoints retain
        the complete pre-transaction model, including unsaved edits.
        """
        features = []
        feature_objects = self._walk_features_tx(doc)
        for index, feat in enumerate(feature_objects):
            features.append({
                "index": index,
                "name": str(com_get(feat, "Name", default="?")),
                "type": str(com_get(feat, "GetTypeName2", default="?")),
                "persistent_id": (self._persist_reference(doc, feat)
                                  if include_persistent_ids else None),
            })
        dimensions, dimensions_complete = [], True
        pending, seen = list(feature_objects), set()
        while pending and len(seen) < 10000:
            feat = pending.pop(0)
            key = (str(com_get(feat, "Name", default="?")),
                   str(com_get(feat, "GetTypeName2", default="?")))
            if key in seen:
                continue
            seen.add(key)
            try:
                display = com_get(feat, "GetFirstDisplayDimension")
                guard = 0
                while display is not None and guard < 10000:
                    guard += 1
                    dimension = com_get(display, "GetDimension2", 0)
                    value = float(com_get(dimension, "SystemValue"))
                    if not math.isfinite(value):
                        raise ValueError("Non-finite dimension value")
                    dimensions.append({
                        "name": str(com_get(dimension, "FullName")),
                        "system_value": value,
                        "driven_state": com_get(dimension, "DrivenState"),
                    })
                    display = com_get(feat, "GetNextDisplayDimension", display)
                if display is not None:
                    dimensions_complete = False
                child = com_get(feat, "GetFirstSubFeature")
                guard = 0
                while child is not None and guard < 10000:
                    guard += 1
                    pending.append(child)
                    child = com_get(child, "GetNextSubFeature")
                if child is not None:
                    dimensions_complete = False
            except Exception:
                dimensions_complete = False
        if pending:
            dimensions_complete = False
        bodies = []
        geometry_complete = True
        try:
            for body in doc.GetBodies2(0, False) or []:
                bbox = com_get(body, "GetBodyBox", default=None)
                mass = com_get(body, "GetMassProperties", 1.0, default=None)
                if not mass or len(mass) < 5:
                    body_typed = typed(body, "IBody2")
                    mass = (com_get(body_typed, "GetMassProperties", 1.0,
                                    default=None) if body_typed else None)
                volume = float(mass[3]) if mass and len(mass) >= 5 else None
                if not bbox or volume is None or not math.isfinite(volume):
                    geometry_complete = False
                bodies.append({
                    "name": str(com_get(body, "Name", default="?")),
                    "bbox_m": list(bbox) if bbox else None,
                    "volume_m3": volume,
                    "faces": len(com_get(body, "GetFaces", default=[]) or []),
                    "persistent_id": (self._persist_reference(doc, body)
                                      if include_persistent_ids else None),
                })
        except Exception:
            geometry_complete = False
        active_sketch = None
        active_sketch_known = False
        try:
            active_sketch_known, sketch = active_sketch_state(doc)
            if sketch is not None:
                sf = com_get(sketch, "GetFeature", default=None)
                active_sketch = com_get(sf, "Name", default="<active>") or "<active>"
        except Exception:
            active_sketch_known = False
        path = self._get_doc_path(doc)
        return {
            "document": self._get_doc_title(doc), "path": path,
            "saved_mtime": os.path.getmtime(path) if path and os.path.exists(path)
            else None,
            "features": features, "bodies": bodies,
            "dimensions": sorted(dimensions, key=lambda item: item["name"]),
            "dimensions_complete": dimensions_complete,
            "geometry_complete": geometry_complete,
            "dirty": com_get(doc, "GetSaveFlag", default=None),
            "volume_m3": (sum(body["volume_m3"] for body in bodies)
                          if geometry_complete else None),
            "feature_count": len(features), "solid_body_count": len(bodies),
            "active_sketch": active_sketch,
            "active_sketch_known": bool(active_sketch_known),
            "captured_at": time.time(),
        }

    def _save_copy(self, doc, path: str) -> Dict[str, Any]:
        path = os.path.abspath(path)
        original_path = self._get_doc_path(doc)
        original_title = self._get_doc_title(doc)
        original_dirty = com_get(doc, "GetSaveFlag", default=None)
        if original_path and os.path.normcase(path) == os.path.normcase(
                os.path.abspath(original_path)):
            return {"success": False, "path": path,
                    "message": "Checkpoint must not overwrite the source document"}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            return {"success": False, "path": path,
                    "message": "Checkpoint path already exists; choose a new path"}
        errors, warnings = out_i4(), out_i4()
        ok, error_code, warning_code = False, 0, 0
        try:
            ret = doc.Extension.SaveAs(
                path, 0, int(SwSaveAsOptions.swSaveAsOptions_Copy),
                null_dispatch(), errors, warnings)
            ok, error_code, warning_code = normalize_save_result(
                ret, errors, warnings)
        except Exception:
            try:
                # IModelDoc2.SaveAs3 returns a swFileSaveError bitmask: zero
                # means success (unlike Extension.SaveAs's Boolean result).
                error_code = int(doc.SaveAs3(
                    path, 0, int(SwSaveAsOptions.swSaveAsOptions_Copy)))
                ok = error_code == 0
            except Exception:
                ok = False
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        identity_preserved = (self._get_doc_path(doc) == original_path and
                              self._get_doc_title(doc) == original_title)
        if original_dirty is True and not com_get(doc, "GetSaveFlag", default=True):
            com_get(doc, "SetSaveFlag", default=None)
        dirty_preserved = (original_dirty is not True or
                           com_get(doc, "GetSaveFlag", default=None) is True)
        return {"success": bool(exists and ok and identity_preserved and dirty_preserved), "path": path,
                "size_bytes": os.path.getsize(path) if exists else 0,
                "errors": error_code, "warnings": warning_code,
                "source_identity_preserved": identity_preserved,
                "source_unsaved_flag_preserved": dirty_preserved}

    @staticmethod
    def _sha256(path):
        if not path or not os.path.exists(path):
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _document_path_key(path):
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))

    def _checkpoint_protected_paths(self, doc):
        """Enumerate every open file before pruning; incomplete means no prune."""
        unreadable = object()
        try:
            documents = com_get(self._sw_app, "GetDocuments", default=unreadable)
            if documents is unreadable:
                app = typed(self._sw_app, "ISldWorks")
                documents = com_get(app, "GetDocuments", default=unreadable)
            # An active doc exists, so None/empty cannot prove a complete list.
            if not isinstance(documents, (tuple, list)) or not documents:
                return None, "Open document enumeration is unavailable"
            protected = set()
            for opened in documents:
                path = com_get(opened, "GetPathName", default=unreadable)
                if not isinstance(path, str):
                    return None, "An open document path is unreadable"
                if path:
                    protected.add(self._document_path_key(path))
            source = self._get_doc_path(doc)
            if source:
                source = self._document_path_key(source)
                if source not in protected:
                    return None, "Open document enumeration omitted the source"
                protected.add(source)
            return protected, None
        except Exception as exc:
            return None, f"Cannot verify open document paths: {exc}"

    def _create_checkpoint(self, doc, transaction_name: str,
                           checkpoint: Optional[Dict[str, Any]],
                           snapshot: Dict[str, Any], arguments=None):
        checkpoint = checkpoint or {}
        if checkpoint.get("mode") == "none":
            return {"success": True, "path": None, "manifest": None}
        path = checkpoint.get("path")
        if not path:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", transaction_name)
            path = str(default_state_dir() / "checkpoints" /
                       f"{int(time.time() * 1000)}_{safe_name}_{uuid.uuid4().hex[:8]}.SLDPRT")
        saved = self._save_copy(doc, path)
        if not saved["success"]:
            return saved
        try:
            sw_version = str(com_get(self._sw_app, "RevisionNumber",
                                     default="unknown"))
        except Exception:
            sw_version = "unknown"
        manifest = {
            "schema": "solidworks-mcp/checkpoint/v1",
            "transaction": transaction_name,
            "checkpoint": saved,
            "source": {"path": snapshot.get("path"),
                       "sha256": self._sha256(snapshot.get("path"))},
            "solidworks_version": sw_version, "mcp_version": MCP_VERSION,
            "unit": self._units.default_unit.value,
            "features": snapshot["features"], "bodies": snapshot["bodies"],
            "dimensions": snapshot.get("dimensions", []),
            "source_dirty": snapshot.get("dirty"),
            "arguments": arguments or {}, "created_at": time.time(),
        }
        manifest_path = saved["path"] + ".manifest.json"
        atomic_json_write(manifest_path, manifest)
        saved["manifest"] = manifest_path
        keep_last = max(1, int(checkpoint.get(
            "keep_last_n", getattr(self._config, "keep_last_n_checkpoints", 10))))
        directory = Path(saved["path"]).parent
        # Never treat unrelated parts in an explicit checkpoint directory as
        # disposable checkpoints. Only our versioned manifests grant ownership.
        owned = []
        for item in directory.glob("*.SLDPRT"):
            try:
                metadata = json.loads(Path(str(item) + ".manifest.json").read_text(
                    encoding="utf-8"))
                if (isinstance(metadata, dict) and
                        metadata.get("schema") == "solidworks-mcp/checkpoint/v1"):
                    owned.append(item)
            except (OSError, ValueError):
                continue
        protected, retention_error = self._checkpoint_protected_paths(doc)
        # An open checkpoint can itself be the user's current source document.
        # Protect all open documents, not only the newly saved checkpoint.
        if protected is not None:
            protected.add(self._document_path_key(saved["path"]))
        candidates = [] if protected is None else [
            item for item in owned if self._document_path_key(item) not in protected]
        checkpoints = sorted(candidates,
                             key=lambda item: item.stat().st_mtime,
                             reverse=True)
        removed = []
        # The just-created recovery file always occupies one retained slot,
        # even if an old file has a future modification timestamp.
        for old in checkpoints[max(0, keep_last - 1):]:
            try:
                old.unlink()
                sidecar = Path(str(old) + ".manifest.json")
                if sidecar.exists():
                    sidecar.unlink()
                removed.append(str(old))
            except OSError:
                pass
        saved["retention_removed"] = removed
        if retention_error:
            saved["retention_skipped"] = retention_error
        self._runtime.last_checkpoint_at = time.time()
        self._runtime.increment("checkpoints_created")
        return saved

    def _delete_created_features(self, doc, before, after):
        old_ids = {f.get("persistent_id") for f in before["features"]
                   if f.get("persistent_id")}
        old_names = {f["name"] for f in before["features"]}
        created = [f for f in after["features"] if
                   ((f.get("persistent_id") and f["persistent_id"] not in old_ids)
                    or (not f.get("persistent_id") and f["name"] not in old_names))]
        deleted = []
        failed = []
        for item in reversed(created):
            # Absorbed profiles can predate the transaction. Remove only the
            # positively identified new feature; independently new sketches
            # are removed by their own entry later in the reverse-order walk.
            result = self.delete_feature(item["name"], delete_absorbed=False)
            if result.get("success"):
                deleted.append(item["name"])
            else:
                failed.append(item["name"])
        return deleted, failed

    @staticmethod
    def _snapshots_equivalent(left, right):
        if not all(snapshot.get("dimensions_complete") and
                   snapshot.get("geometry_complete") and
                   snapshot.get("active_sketch_known")
                   for snapshot in (left, right)):
            return False
        # Undo/rebuild commonly leaves an originally clean doc dirty; that is
        # safe. Losing an originally dirty flag is not, as closing could then
        # silently discard pre-transaction unsaved edits.
        if left.get("dirty") is True and right.get("dirty") is not True:
            return False

        def equal(a, b):
            if isinstance(a, float) or isinstance(b, float):
                return (isinstance(a, (int, float)) and
                        isinstance(b, (int, float)) and
                        math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12))
            if isinstance(a, dict) and isinstance(b, dict):
                return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
            if isinstance(a, list) and isinstance(b, list):
                return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
            return a == b

        # Identity is included: opening a differently named checkpoint is not
        # restoration of the original in-memory document.
        return all(equal(left.get(key), right.get(key)) for key in (
            "document", "path", "features", "dimensions", "bodies",
            "solid_body_count", "active_sketch"))

    @staticmethod
    def _begin_undo_group(doc, name):
        state = {"started": False, "finished": False, "name": name}
        try:
            # StartRecordingUndoObject is void (VT_VOID in the installed
            # SOLIDWORKS type library), unlike FinishRecordingUndoObject2.
            # Successful dispatch, including its normal None return, starts
            # recording. A missing/rejected call raises and is fail-closed.
            extension = typed(doc.Extension, "IModelDocExtension")
            if extension is None:
                raise RuntimeError("Typed IModelDocExtension is unavailable for undo recording")
            result = extension.StartRecordingUndoObject()
            state["started"] = result is None or result is True
        except Exception as exc:
            state["error"] = str(exc)
        return state

    @staticmethod
    def _finish_undo_group(doc, state):
        if state.get("started") and not state.get("finish_attempted"):
            state["finish_attempted"] = True
            try:
                # Keep it visible: SW may discard a hidden first Undo item.
                extension = typed(doc.Extension, "IModelDocExtension")
                if extension is None:
                    raise RuntimeError("Typed IModelDocExtension is unavailable for undo finalization")
                state["finished"] = bool(extension.FinishRecordingUndoObject2(
                    state["name"], False))
            except Exception as exc:
                state["error"] = str(exc)
        return state

    @staticmethod
    def _nested_rollback_proven(steps):
        """Accept one atomic failure's own cleanup, with no prior mutation.

        Snapshot equality is necessary but not a full serialization. Require
        an explicit child restoration result before claiming that there is
        nothing left for the outer transaction to undo.
        """
        read_only = {"body_volume", "verify_named_body"}
        changed = [step for step in steps
                   if not step.get("skipped") and
                   (step.get("data") or {}).get("operation") not in read_only]
        if len(changed) != 1 or changed[0].get("success"):
            return False
        error = ((changed[0].get("data") or {}).get("error") or {})
        return error.get("document_restored") is True

    def _rollback_transaction(self, doc, before, checkpoint_path,
                              undo=None, mutations_started=True,
                              allow_delete=False, nested_rollback_verified=False):
        """Undo in place, preserving source identity and unsaved user changes.

        A checkpoint is a recovery artifact, never silently substituted for
        the original document or saved over its on-disk version.
        """
        undo = undo or {}
        deleted, failed, fallback_error = [], [], None
        cleanup_attempted = False
        current = self._transaction_snapshot(doc)
        restored = not mutations_started
        exit_result = None
        if mutations_started:
            finished_before_exit = bool(undo.get("finished"))
            exit_result = exit_active_sketch(doc)
            if exit_result.get("success"):
                self._finish_undo_group(doc, undo)
                if (before.get("dirty") is True and
                        com_get(doc, "GetSaveFlag", default=None) is not True):
                    com_get(doc, "SetSaveFlag", default=None)
                current = self._transaction_snapshot(doc)
                already_matches = self._snapshots_equivalent(before, current)
                if already_matches:
                    # An atomic child may already have removed its failed
                    # sketch. Undoing its cleanup would recreate that sketch.
                    # Never undo an already matching model; claim restoration
                    # only when the child supplied independent cleanup proof.
                    undo["skipped_reason"] = "Model already matches the pre-transaction snapshot"
                    undo["nested_rollback_verified"] = bool(nested_rollback_verified)
                elif finished_before_exit and exit_result.get("was_active"):
                    # Exiting an edit session after the recording was already
                    # finalized may push another Undo item. Never undo a
                    # possibly unrelated entry under that ambiguity.
                    undo["error"] = "Sketch edit ended after undo group was finalized"
                elif undo.get("finished"):
                    try:
                        # EditUndo2 has a void return. Only read-back can
                        # establish that the intended group was restored.
                        model = typed(doc, "IModelDoc2")
                        if model is None:
                            raise RuntimeError("Typed IModelDoc2 is unavailable for undo")
                        model.EditUndo2(1)
                        undo["attempted"] = True
                        com_get(doc, "EditRebuild3")
                    except Exception as exc:
                        undo["error"] = str(exc)
                elif allow_delete and not undo.get("finished"):
                    cleanup_attempted = True
                    deleted, failed = self._delete_created_features(
                        doc, before, current)
                    com_get(doc, "EditRebuild3", default=False)
                if (before.get("dirty") is True and
                        com_get(doc, "GetSaveFlag", default=None) is not True):
                    # Preserve the source's pre-existing save obligation.
                    # A subsequent snapshot, not this void call, verifies it.
                    com_get(doc, "SetSaveFlag", default=None)
                current = self._transaction_snapshot(doc)
                restored = self._snapshots_equivalent(before, current)
                # A rich snapshot alone is still not a full model image.
                # Accept it only following an undo or controlled append-only
                # cleanup, never merely because object names happen to match.
                restored = bool(restored and (
                    (already_matches and nested_rollback_verified) or
                    undo.get("attempted") or (cleanup_attempted and not failed)))
            else:
                undo["error"] = "Could not safely exit sketch editing"
        checkpoint_reopened = False
        recovery = None
        if not restored and checkpoint_path and os.path.isfile(checkpoint_path):
            try:
                # Do not close or overwrite the original dirty document. The
                # checkpoint contains its complete pre-transaction state.
                recovery = self.open_document(checkpoint_path)
                checkpoint_reopened = bool(recovery.get("success"))
            except Exception as exc:
                fallback_error = str(exc)
        self._runtime.increment("rollbacks")
        return {"rolled_back": bool(restored), "document_restored": bool(restored),
                "deleted_features": deleted, "delete_failures": failed,
                "undo": undo, "exit_sketch": exit_result,
                "checkpoint_reopened": checkpoint_reopened,
                "recovery_document": recovery,
                "original_document": {"title": before.get("document"),
                                      "path": before.get("path")},
                "original_document_closed": False,
                "original_file_overwritten_by_rollback": False,
                "fallback_error": fallback_error, "current": current}

    def _call_operation(self, name: str, args: Dict[str, Any]) -> Dict:
        if name not in self.PLAN_OPERATIONS:
            return self._error(
                "INVALID_PLAN", f"Operation '{name}' is not whitelisted",
                recommended_actions=[
                    "Use a supported native CAD operation; arbitrary logic "
                    "belongs in execute_python_async outside a CAD plan."])
        args = dict(args or {})
        for common in ("budget", "idempotency_key", "allow_unsaved_document",
                       "save_path", "ui_guard"):
            args.pop(common, None)
        if name == "show_body":
            return self.set_body_visibility(args.get("name", ""), True)
        if name == "hide_body":
            return self.set_body_visibility(args.get("name", ""), False)
        method = getattr(self, name, None)
        if method is None:
            return self._error("CAPABILITY_UNAVAILABLE",
                               f"Operation '{name}' is unavailable")
        try:
            signature = inspect.signature(method)
            accepted = {k: v for k, v in args.items()
                        if k in signature.parameters}
            return method(**accepted)
        except Exception as exc:
            hresult = getattr(exc, "hresult", None)
            return self._error(
                "COM_MEMBER_MISMATCH", f"{name} failed: {exc}",
                com_hresult=hresult, recoverable=True,
                recommended_actions=[
                    "Inspect capabilities and the structured COM details; do "
                    "not retry more than once without changing the strategy."])

    @staticmethod
    def _resolve_step_refs(value, steps):
        if isinstance(value, str) and value.startswith("$steps."):
            parts = value.split(".")[1:]
            current = steps
            for part in parts:
                current = current[int(part)] if isinstance(current, list) else current[part]
            return current
        if isinstance(value, list):
            return [TransactionOperations._resolve_step_refs(v, steps)
                    for v in value]
        if isinstance(value, dict):
            return {k: TransactionOperations._resolve_step_refs(v, steps)
                    for k, v in value.items()}
        return value

    def _condition_matches(self, condition, steps):
        if not condition:
            return True
        if set(condition) - {"step_success", "body_exists", "feature_exists"}:
            raise ValueError("Unsafe/unknown plan condition")
        if "step_success" in condition:
            index = int(condition["step_success"])
            if index >= len(steps) or not steps[index].get("success"):
                return False
        doc, err = self.get_active_doc()
        if err:
            return False
        snapshot = self._transaction_snapshot(doc)
        if "body_exists" in condition and condition["body_exists"] not in {
                b["name"] for b in snapshot["bodies"]}:
            return False
        if "feature_exists" in condition and condition["feature_exists"] not in {
                f["name"] for f in snapshot["features"]}:
            return False
        return True

    def _validate_invariants(self, invariants, before, after, step_results):
        invariants = invariants or {}
        failures = [f"unsupported invariant '{key}'" for key in
                    sorted(set(invariants) - self.INVARIANT_KEYS)]
        feature_names = {f["name"] for f in after["features"]}
        before_features = {f["name"] for f in before["features"]}
        body_names = {b["name"] for b in after["bodies"]}
        for name in invariants.get("expected_new_features", []):
            if name not in feature_names or name in before_features:
                failures.append(f"expected new feature '{name}'")
        for name in invariants.get("required_bodies", []):
            if name not in body_names:
                failures.append(f"required body '{name}'")
        expected_bodies = invariants.get("solid_body_count")
        count = after["solid_body_count"]
        if isinstance(expected_bodies, int) and count != expected_bodies:
            failures.append(f"solid_body_count={count}, expected {expected_bodies}")
        elif isinstance(expected_bodies, dict):
            if count < expected_bodies.get("min", count):
                failures.append("solid body count below minimum")
            if count > expected_bodies.get("max", count):
                failures.append("solid body count above maximum")
        for path in invariants.get("required_files", []):
            if not os.path.exists(path) or os.path.getsize(path) <= 0:
                failures.append(f"required file missing/empty: {path}")
        if "volume_m3" in invariants:
            expected = invariants["volume_m3"]
            actual = after.get("volume_m3")
            if not isinstance(actual, (float, int)) or not math.isfinite(actual):
                failures.append("solid volume is unavailable")
            elif isinstance(expected, (float, int)) and not isinstance(expected, bool):
                if not math.isfinite(expected) or not math.isclose(
                        actual, expected, rel_tol=1e-8, abs_tol=1e-15):
                    failures.append(f"volume_m3={actual}, expected {expected}")
            elif isinstance(expected, dict) and expected and not (
                    set(expected) - {"min", "max"}):
                try:
                    if (not all(math.isfinite(float(v)) for v in expected.values()) or
                            actual < float(expected.get("min", actual)) or
                            actual > float(expected.get("max", actual))):
                        failures.append(f"volume_m3={actual} outside {expected}")
                except (TypeError, ValueError):
                    failures.append("volume_m3 bounds must be finite numbers")
            else:
                failures.append("volume_m3 must be a number or min/max bounds")
        if invariants.get("no_modal_dialog", True) and detect_modal_dialog().get("modal"):
            failures.append("SolidWorks UI is blocked by a modal dialog")
        for sketch_name, expected in invariants.get("sketch_status", {}).items():
            status = self.analyze_sketch_dof(sketch_name,
                                             include_recommendations=False)
            actual = (status.get("data") or {}).get("status")
            if actual != expected:
                failures.append(
                    f"sketch '{sketch_name}' status={actual}, expected={expected}")
        return failures

    def run_transaction(self, name: str, operations: List[Dict[str, Any]],
                        checkpoint: Dict[str, Any] = None,
                        invariants: Dict[str, Any] = None,
                        on_failure: str = "rollback",
                        idempotency_key: str = None,
                        save_policy: Dict[str, Any] = None,
                        budget: Dict[str, Any] = None,
                        allow_unsaved_document: bool = False,
                        save_path: str = None) -> Dict:
        cached = self._runtime.idempotent_get(idempotency_key)
        if cached is not None:
            cached.setdefault("data", {})["idempotent_replay"] = True
            return cached
        if not name:
            return self._error("INVALID_PLAN", "Transaction name is required")
        if not isinstance(operations, list) or len(operations) > MAX_PLAN_OPERATIONS:
            return self._error(
                "INVALID_PLAN",
                f"operations must be a list with at most {MAX_PLAN_OPERATIONS} items")
        if invariants is not None and (not isinstance(invariants, dict) or
                set(invariants) - self.INVARIANT_KEYS):
            return self._error(
                "INVALID_PLAN", "Unknown or invalid transaction invariants",
                details={"supported_invariants": sorted(self.INVARIANT_KEYS)})
        if on_failure not in {"rollback", "leave_partial"}:
            return self._error("INVALID_PLAN", "Invalid on_failure policy")
        if any(not isinstance(op, dict) or op.get("op") not in self.PLAN_OPERATIONS
               for op in operations):
            return self._error("INVALID_PLAN", "Plan contains an unsupported operation")
        unverifiable = sorted({op["op"] for op in operations} &
                              self.UNVERIFIED_ROLLBACK_OPERATIONS)
        if any(op["op"] == "create_parametric_sketch" and
               isinstance(op.get("args"), dict) and op["args"].get("equations")
               for op in operations):
            unverifiable.append("create_parametric_sketch with equations")
        if on_failure == "rollback" and unverifiable:
            return self._error(
                "CAPABILITY_UNAVAILABLE",
                "Verified rollback is unavailable for the requested operations; no operations or checkpoint started",
                document_restored=True,
                details={"unsupported_operations": unverifiable,
                         "committed": False, "mutations_started": False,
                         "checkpoint_started": False})
        if on_failure == "rollback" and any(
                op["op"] == "save_document" for op in operations):
            return self._error(
                "INVALID_PLAN", "Use save_after_success instead of saving inside a rollback plan")
        doc, err = self.get_active_doc()
        if err:
            return err
        if not self._get_doc_path(doc):
            if save_path:
                saved = self.save_document(save_path)
                if not saved.get("success"):
                    return saved
                doc, _ = self.get_active_doc()
            elif not allow_unsaved_document:
                return self._error(
                    "DOCUMENT_UNSAVED",
                    "Compound mutation requires save_path or explicit "
                    "allow_unsaved_document=true",
                    recommended_actions=["Provide an absolute .SLDPRT save_path."])
        save_policy = save_policy or {}
        if save_policy.get("save_before"):
            save_before_result = self.save_document()
            if not save_before_result.get("success"):
                return save_before_result
        logger.info("Transaction name=%s stage=snapshot_before begin", name)
        before = self._transaction_snapshot(doc)
        if not before.get("active_sketch_known"):
            return self._error(
                "CAPABILITY_UNAVAILABLE",
                "Cannot verify sketch edit state; no operations executed",
                document_restored=True,
                details={"committed": False, "mutations_started": False})
        if on_failure == "rollback" and before.get("active_sketch") is not None:
            return self._error(
                "CAPABILITY_UNAVAILABLE",
                "Rollback plans must start outside sketch edit mode",
                document_restored=True,
                details={"active_sketch": before["active_sketch"],
                         "committed": False, "mutations_started": False})
        logger.info(
            "Transaction name=%s stage=snapshot_before complete features=%s bodies=%s",
            name, before["feature_count"], before["solid_body_count"])
        tx_id = uuid.uuid4().hex
        self._runtime.active_transaction_id = tx_id
        effective_checkpoint = checkpoint
        if save_policy.get("save_checkpoint") is False:
            effective_checkpoint = {"mode": "none"}
        elif save_policy.get("keep_last_n_checkpoints") is not None:
            effective_checkpoint = dict(checkpoint or {})
            effective_checkpoint["keep_last_n"] = save_policy[
                "keep_last_n_checkpoints"]
        logger.info("Transaction id=%s name=%s stage=checkpoint begin mode=%s",
                    tx_id, name, (effective_checkpoint or {}).get("mode"))
        try:
            checkpoint_result = self._create_checkpoint(
                doc, name, effective_checkpoint, before,
                {"operations": operations, "invariants": invariants})
        except Exception as exc:
            self._runtime.active_transaction_id = None
            return self._error(
                "TRANSACTION_ROLLBACK_FAILED", "Could not create checkpoint metadata",
                document_restored=True, details={"exception": str(exc),
                                                "mutations_started": False})
        logger.info(
            "Transaction id=%s name=%s stage=checkpoint complete success=%s path=%s",
            tx_id, name, bool(checkpoint_result.get("success")),
            checkpoint_result.get("path"))
        if not checkpoint_result.get("success"):
            self._runtime.active_transaction_id = None
            return self._error(
                "TRANSACTION_ROLLBACK_FAILED",
                "Could not create the required checkpoint",
                recoverable=False, details=checkpoint_result)
        steps = []
        started = time.monotonic()
        failure = None
        save_result = None
        undo = {"started": False, "finished": False, "name": f"MCP {name} {tx_id}"}
        mutations_started = False
        existing_state_may_change = any(
            op["op"] in self.EXISTING_STATE_OPERATIONS or
            (op["op"] == "create_parametric_sketch" and
             isinstance(op.get("args"), dict) and op["args"].get("equations"))
            for op in operations)
        logger.info("Transaction id=%s name=%s stage=execute begin steps=%s",
                    tx_id, name, len(operations))
        try:
            if (on_failure == "rollback" and operations and
                    not self._elapsed_budget_exceeded(started, budget)):
                undo = self._begin_undo_group(doc, undo["name"])
                if existing_state_may_change and (not undo.get("started") or
                        not before.get("dimensions_complete") or
                        not before.get("geometry_complete")):
                    self._finish_undo_group(doc, undo)
                    return self._error(
                        "CAPABILITY_UNAVAILABLE",
                        "Cannot safely roll back changes to existing objects; no operations executed",
                        document_restored=True,
                        details={"undo": undo, "checkpoint": checkpoint_result,
                                 "committed": False, "mutations_started": False,
                                 "dimensions_complete": before.get("dimensions_complete"),
                                 "geometry_complete": before.get("geometry_complete")})
            for index, operation in enumerate(operations):
                if self._elapsed_budget_exceeded(started, budget):
                    self._runtime.increment("budget_exceeded")
                    failure = self._error(
                        "BUDGET_EXCEEDED", "Transaction elapsed-time budget exceeded",
                        details={"step": index})
                    break
                if not isinstance(operation, dict) or "op" not in operation:
                    failure = self._error("INVALID_PLAN",
                                          f"Invalid operation at index {index}")
                    break
                if not self._condition_matches(operation.get("when"), steps):
                    steps.append({"success": True, "skipped": True,
                                  "message": "condition did not match"})
                    continue
                args = self._resolve_step_refs(operation.get("args", {}), steps)
                step_started = time.monotonic()
                logger.info(
                    "Transaction id=%s name=%s step=%s op=%s stage=begin",
                    tx_id, name, index, operation["op"])
                mutations_started = True
                step = self._call_operation(operation["op"], args)
                logger.info(
                    "Transaction id=%s name=%s step=%s op=%s "
                    "stage=complete success=%s elapsed_ms=%.3f",
                    tx_id, name, index, operation["op"],
                    bool(step.get("success")),
                    (time.monotonic() - step_started) * 1000.0)
                step.setdefault("data", {})["step_index"] = index
                step["data"]["operation"] = operation["op"]
                steps.append(step)
                if not step.get("success") and not operation.get(
                        "continue_on_failure", False):
                    failure = step
                    break
            logger.info("Transaction id=%s name=%s stage=snapshot_after begin",
                        tx_id, name)
            after = self._transaction_snapshot(doc)
            logger.info("Transaction id=%s name=%s stage=snapshot_after complete",
                        tx_id, name)
            invariant_failures = [] if failure else self._validate_invariants(
                invariants, before, after, steps)
            if invariant_failures:
                failure = self._error(
                    "INVARIANT_FAILED", "Transaction invariant failed",
                    details={"failures": invariant_failures})
            if not failure:
                self._finish_undo_group(doc, undo)
                if undo.get("started") and not undo.get("finished"):
                    failure = self._error(
                        "TRANSACTION_ROLLBACK_FAILED", "Could not finish the native undo group",
                        details={"undo": undo})
            if not failure and save_policy.get("save_after_success", True):
                save_result = self.save_document()
                if not save_result.get("success"):
                    failure = self._error(
                        "DOCUMENT_UNSAVED", "Transaction save failed",
                        details={"save_result": save_result})
                else:
                    self._runtime.increment("files_saved")
                    if after["solid_body_count"]:
                        self._runtime.last_saved_body_at = time.time()
            if failure:
                rollback = ({"rolled_back": False, "document_restored": False}
                            if on_failure != "rollback" else
                            self._rollback_transaction(
                                doc, before, checkpoint_result.get("path"),
                                undo=undo, mutations_started=mutations_started,
                                allow_delete=not existing_state_may_change,
                                nested_rollback_verified=self._nested_rollback_proven(steps)))
                code = ((failure.get("data") or {}).get("error") or {}).get(
                    "code", "INVARIANT_FAILED")
                result = self._error(
                    code, f"Transaction '{name}' failed",
                    document_restored=rollback.get("document_restored"),
                    details={"transaction_id": tx_id, "committed": False,
                             "rolled_back": rollback.get("rolled_back"),
                             "checkpoint": checkpoint_result,
                             "changed_objects": self._diff_snapshots(before, after),
                             "steps": steps, "cause": failure,
                             "save_result": save_result, "rollback": rollback})
                return result
            result = self._result(
                True, f"Transaction '{name}' committed", SwErrors.swSuccess,
                {"transaction_id": tx_id, "committed": True,
                 "rolled_back": False, "checkpoint": checkpoint_result,
                 "changed_objects": self._diff_snapshots(before, after),
                 "steps": steps, "invariants": "passed",
                 "save_result": save_result,
                 "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
            self._runtime.idempotent_put(idempotency_key, result)
            logger.info("Transaction id=%s name=%s stage=committed", tx_id, name)
            return result
        except Exception as exc:
            after = self._transaction_snapshot(doc)
            rollback = ({"rolled_back": False, "document_restored": False}
                        if on_failure != "rollback" else
                        self._rollback_transaction(
                            doc, before, checkpoint_result.get("path"),
                            undo=undo, mutations_started=mutations_started,
                            allow_delete=not existing_state_may_change,
                            nested_rollback_verified=self._nested_rollback_proven(steps)))
            return self._error(
                "COM_MEMBER_MISMATCH", f"Transaction '{name}' crashed: {exc}",
                com_hresult=getattr(exc, "hresult", None),
                document_restored=rollback.get("document_restored"),
                details={"transaction_id": tx_id, "committed": False,
                         "rolled_back": rollback.get("rolled_back"),
                         "checkpoint": checkpoint_result, "steps": steps,
                         "rollback": rollback})
        finally:
            self._finish_undo_group(doc, undo)
            self._runtime.active_transaction_id = None

    @staticmethod
    def _diff_snapshots(before, after):
        bf, af = {f["name"] for f in before["features"]}, {
            f["name"] for f in after["features"]}
        bb, ab = {b["name"] for b in before["bodies"]}, {
            b["name"] for b in after["bodies"]}
        return {"features_created": sorted(af - bf),
                "features_deleted": sorted(bf - af),
                "bodies_created": sorted(ab - bb),
                "bodies_deleted": sorted(bb - ab)}

    def execute_cad_plan(self, plan_id: str, operations: List[Dict[str, Any]],
                         transaction: Dict[str, Any] = None,
                         invariants: Dict[str, Any] = None,
                         unit: str = None, budget: Dict[str, Any] = None,
                         allow_unsaved_document: bool = False,
                         save_path: str = None) -> Dict:
        if unit:
            for operation in operations or []:
                operation.setdefault("args", {}).setdefault("unit", unit)
        transaction = transaction or {}
        return self.run_transaction(
            name=plan_id, operations=operations,
            checkpoint={"mode": "save_copy"} if transaction.get(
                "checkpoint_before", True) else {"mode": "none"},
            invariants=invariants,
            on_failure="rollback" if transaction.get(
                "rollback_on_failure", True) else "leave_partial",
            idempotency_key=plan_id, budget=budget,
            allow_unsaved_document=allow_unsaved_document,
            save_path=save_path)

    def get_session_metrics(self) -> Dict:
        return self._result(True, "Session metrics", SwErrors.swSuccess,
                            self._runtime.report())

    def get_capabilities(self) -> Dict:
        modules = {name: importlib.util.find_spec(name) is not None for name in
                   ("cv2", "numpy", "scipy", "skimage", "PIL", "shapely")}
        version = "not connected"
        if self._sw_app is not None:
            version = str(com_get(self._sw_app, "RevisionNumber",
                                  default="unknown"))
        limitations = [
            "Perspective images require explicit homography or trace_as_is.",
            "Region matting supports silhouette modes; stroke_centerlines, "
            "stroke_edges, and all_visible_edges require the separate line-art backend.",
            "Existing sweep paths/profiles must be named sketches and require "
            "SOLIDWORKS read-back. Newly declared paths are validated before "
            "COM. On SW2026 circular members use a materialized circle sketch; "
            "the special circular-profile API is disabled because it can block COM.",
            "Persistent IDs can become invalid after topology-changing rebuilds.",
            "Unknown modal dialogs are never auto-confirmed.",
            "Autotrace and Picture to Sketch are interactive PropertyManager "
            "features and expose no public API entry point.",
            "Equation B-splines above the per-entity control-point cap are "
            "rejected before COM because SW2026 import cost grows nonlinearly.",
            "SW2026 curve-parameter and tessellation read-back can block for "
            "minutes on dense fit-splines; construction-reference image "
            "commits therefore batch explicit NURBS per loop and use bounded "
            "metadata/endpoint read-back.",
        ]
        try:
            from .deep_vectorization import capability_report
            deep_vectorization = capability_report()
        except Exception as exc:
            deep_vectorization = {"available": False, "error": str(exc)}
        return self._result(True, "Capabilities", SwErrors.swSuccess, {
            "solidworks_version": version, "mcp_version": MCP_VERSION,
            "typed_interfaces": get_typed_module() is not None,
            "operations": sorted(self.PLAN_OPERATIONS | {
                "run_transaction", "execute_cad_plan", "analyze_sketch_dof",
                "get_session_metrics", "get_capabilities", "sync_model_graph"}),
            "geometry_backend": {"shapely": modules["shapely"],
                                 "scipy": modules["scipy"]},
            "image_backend": modules,
            "deep_vectorization": deep_vectorization,
            "vectorization_controls": {
                "projection_modes": [
                    "orthographic", "homography", "trace_as_is"],
                "output_modes": [
                    "locked_trace", "minimal_parametric",
                    "reference_spline", "construction_reference"],
                "homography_requires_ordered_source_quad": True,
                "post_com_reverse_raster_required": True,
                "default_max_control_points_per_spline": 64,
                "cad_cost_model": "batched_composite_nurbs_with_bounded_readback",
            },
            "body_silhouette_comparison": {
                "default_candidate_source": "native_mesh",
                "candidate_sources": ["native_mesh",
                                      "screenshot_segmentation"],
                "native_mesh_projection": "selected_body_stl_triangle_union",
                "viewport_role": "independent_visual_evidence",
                "requires_explicit_metric_transform": True,
            },
            "max_plan_operations": MAX_PLAN_OPERATIONS,
            "export_formats": ["sldprt", "step", "stp", "stl", "iges",
                               "x_t", "x_b", "3mf", "sat", "wrl", "ply"],
            "known_limitations": limitations,
        })

    def recover_environment(self, retry_operation: Dict[str, Any] = None,
                            max_retries: int = 1) -> Dict:
        max_retries = max(0, min(int(max_retries), 1))
        actions = []
        ui = detect_modal_dialog()
        if ui.get("modal"):
            return self._error(
                "MODAL_DIALOG_BLOCKING",
                "Recovery stopped because a dialog requires explicit policy",
                details={"ui": ui, "actions": actions})
        doc, err = self.get_active_doc()
        if err:
            return err
        try:
            doc.ClearSelection2(True)
            actions.append("selection_cleared")
        except Exception:
            pass
        try:
            if doc.SketchManager.ActiveSketch is not None:
                doc.SketchManager.InsertSketch(True)
                actions.append("sketch_edit_exited")
        except Exception:
            pass
        freeze = self.ensure_features_not_frozen(doc)
        actions.append("freeze_bar_checked")
        retried = None
        if retry_operation and max_retries:
            retried = self._call_operation(retry_operation.get("op", ""),
                                           retry_operation.get("args", {}))
        return self._result(True, "Environment recovery completed",
                            SwErrors.swSuccess,
                            {"state": "UI_RECOVERED", "actions": actions,
                             "freeze_bar": freeze, "retry": retried})

    def sync_model_graph(self, graph_id: str, nodes: List[Dict[str, Any]],
                         mode: str = "apply", invariants=None,
                         save_path: str = None,
                         allow_unsaved_document: bool = False) -> Dict:
        if mode not in {"plan", "apply"}:
            return self._error("INVALID_PLAN", "mode must be plan or apply")
        node_by_id = {}
        for node in nodes or []:
            node_id = node.get("id")
            if not node_id or node_id in node_by_id:
                return self._error("INVALID_PLAN", "Node IDs must be unique")
            node_by_id[node_id] = node
        visiting, visited, order = set(), set(), []

        def visit(node_id):
            if node_id in visiting:
                raise ValueError(f"Cycle detected at '{node_id}'")
            if node_id in visited:
                return
            if node_id not in node_by_id:
                raise ValueError(f"Unknown dependency '{node_id}'")
            visiting.add(node_id)
            for dep in node_by_id[node_id].get("depends_on", []):
                visit(dep)
            visiting.remove(node_id)
            visited.add(node_id)
            order.append(node_id)

        try:
            for node_id in node_by_id:
                visit(node_id)
        except ValueError as exc:
            return self._error("INVALID_PLAN", str(exc))
        doc, err = self.get_active_doc()
        if err:
            return err
        current = self._transaction_snapshot(doc)
        feature_names = {f["name"] for f in current["features"]}
        operations, diff = [], []
        previous = self._runtime.model_graphs.get(graph_id, {})
        for node_id in order:
            node = node_by_id[node_id]
            digest = hashlib.sha256(json.dumps(
                node, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            expected = node.get("expected_feature")
            unchanged = previous.get(node_id) == digest and (
                not expected or expected in feature_names)
            action = "unchanged" if unchanged else "create_or_update"
            diff.append({"id": node_id, "action": action,
                         "expected_feature": expected})
            if not unchanged:
                operations.append({"op": node.get("op"),
                                   "args": node.get("args", {})})
        if mode == "plan":
            return self._result(True, "Model graph diff", SwErrors.swSuccess,
                                {"graph_id": graph_id, "order": order,
                                 "diff": diff, "operations": operations})
        desired_digest = hashlib.sha256(json.dumps(
            {node_id: node_by_id[node_id] for node_id in order},
            sort_keys=True, default=str).encode("utf-8")).hexdigest()
        result = self.execute_cad_plan(
            f"model_graph:{graph_id}:{desired_digest[:16]}", operations,
            transaction={"checkpoint_before": True,
                         "rollback_on_failure": True},
            invariants=invariants, save_path=save_path,
            allow_unsaved_document=allow_unsaved_document)
        if result.get("success"):
            self._runtime.model_graphs[graph_id] = {
                node_id: hashlib.sha256(json.dumps(
                    node_by_id[node_id], sort_keys=True, default=str
                ).encode("utf-8")).hexdigest() for node_id in order}
            result.setdefault("data", {})["graph_diff"] = diff
            result["data"]["desired_graph_sha256"] = desired_digest
        return result
