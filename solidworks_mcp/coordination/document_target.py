"""Opt-in selection of an already open document inside the owned CAD operation.

Importing this module is COM-free. Selection never opens, saves, closes, rebuilds
or changes configuration. A failed/uncertain activation is not undone by another
unverified activation: the caller must fence the queue for operator review.
"""
from __future__ import annotations

import ntpath
import re


class TargetDocumentError(ValueError):
    def __init__(self, code, message, *, requires_recovery=False):
        super().__init__(message)
        self.code = code
        self.requires_recovery = requires_recovery


def _reject(code, message, *, uncertain=False):
    raise TargetDocumentError(code, message, requires_recovery=uncertain)


def path_key(path):
    return ntpath.normcase(ntpath.normpath(path))


def normalize_target(value, *, expected_present=False):
    """Validate before any COM access; expected_document keeps compare-only use."""
    if expected_present:
        _reject("DOCUMENT_COORDINATION_CONFLICT",
                "Use target_document or expected_document, never both")
    if isinstance(value, str):
        value = {"path": value}
    if not isinstance(value, dict) or set(value) - {"path", "document_type", "configuration"}:
        _reject("INVALID_TARGET_DOCUMENT", "Target must specify an absolute saved model path")
    path = value.get("path")
    if not isinstance(path, str) or not path or "\x00" in path:
        _reject("INVALID_TARGET_DOCUMENT", "Target must specify an absolute saved model path")
    path = path.replace("/", "\\")
    drive, tail = ntpath.splitdrive(path)
    valid_drive = bool(re.fullmatch(r"[A-Za-z]:", drive))
    valid_unc = bool(re.fullmatch(r"\\\\[^\\?\.][^\\]*\\[^\\]+", drive))
    if (not (valid_drive or valid_unc) or not tail.startswith("\\") or
            ntpath.splitext(path)[1].lower() not in {".sldprt", ".sldasm", ".slddrw"}):
        _reject("INVALID_TARGET_DOCUMENT", "Target must be an absolute SLDPRT, SLDASM or SLDDRW path")
    kind = value.get("document_type")
    if "document_type" in value and (isinstance(kind, bool) or not isinstance(kind, int) or kind not in (1, 2, 3)):
        _reject("INVALID_TARGET_DOCUMENT", "document_type must be 1, 2 or 3")
    config = value.get("configuration")
    if "configuration" in value and (not isinstance(config, str) or not config.strip()):
        _reject("INVALID_TARGET_DOCUMENT", "configuration must be a nonempty exact name")
    return {**value, "path": ntpath.normpath(path)}


def _snapshot(automation):
    """Keep refs only inside the STA; require complete, stable preservation data."""
    from ..automation.com_utils import com_get
    inventory = automation._strict_document_inventory()
    documents = com_get(automation.app, "GetDocuments")
    if documents is None and inventory["count"] == 0:
        documents = []
    if not isinstance(documents, (tuple, list)) or len(documents) != inventory["count"]:
        raise ValueError("Open-document enumeration changed")
    refs = {}
    for doc in documents:
        state = automation._strict_document_state(doc)
        if state not in inventory["documents"] or state["key"] in refs:
            raise ValueError("Document identity changed during enumeration")
        refs[state["key"]] = doc
    return inventory, refs


def _verify_no_running_command(app):
    """Non-modal feature/PropertyManager edits also make activation unsafe."""
    import pythoncom
    from win32com.client import VARIANT
    command = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, -2147483648)
    title = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "<unwritten>")
    # Fail closed if the COM call does not overwrite this output argument.
    active = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, True)
    result = app.GetRunningCommandInfo(command, title, active)
    values = (command.value, title.value, active.value) if result is None else result
    # IsUiActive is the API's authoritative command/PMP activity flag. SW2025
    # can write an idle command ID and False while leaving PMTitle untouched.
    # The title is descriptive, not an idle-state witness; null BSTR is valid
    # too. Keep requiring a written integer command and an explicit False flag.
    if (not isinstance(values, (tuple, list)) or len(values) != 3 or
            isinstance(values[0], bool) or not isinstance(values[0], int) or values[0] == -2147483648 or
            (values[1] is not None and not isinstance(values[1], str)) or values[2] is not False):
        _reject("TARGET_DOCUMENT_UNSAFE_EDIT", "Finish the active command or PropertyManager edit before target selection")


def _verify_assembly_edit_target(doc, state):
    """ActiveDoc is not necessarily the geometry target of an assembly edit.

    This identity check applies even when selection is a no-op. It must not
    reject ordinary same-model sketches or dirty state just because they exist.
    """
    from ..automation.com_utils import com_get, typed
    if state["type"] != 2:
        return
    assembly = typed(doc, "IAssemblyDoc")
    edit_target = com_get(assembly if assembly is not None else doc, "GetEditTarget")
    edit_path = com_get(edit_target, "GetPathName")
    edit_kind = com_get(edit_target, "GetType")
    if (not isinstance(edit_path, str) or not edit_path or
            path_key(edit_path) != path_key(state["path"]) or
            isinstance(edit_kind, bool) or not isinstance(edit_kind, int) or edit_kind != state["type"]):
        _reject("TARGET_DOCUMENT_UNSAFE_EDIT", "Finish in-context assembly editing before target selection")


def _verify_edit_state(doc, state):
    from ..automation.com_utils import com_get
    from ..automation.references import active_sketch_state
    blocking = com_get(doc, "GetBlockingState")
    if isinstance(blocking, bool) or not isinstance(blocking, int) or blocking != 0:
        _reject("TARGET_DOCUMENT_UNSAFE_EDIT", "Document is blocked or its edit state is unknown")
    known, sketch = active_sketch_state(doc)
    if not known or sketch is not None:
        _reject("TARGET_DOCUMENT_UNSAFE_EDIT", "Finish sketch editing before target selection")
    _verify_assembly_edit_target(doc, state)


def _activate(app, name):
    """Invoke once, with explicit no-rebuild semantics and a checked status.

    ISldWorks.ActivateDoc3 accepts a loaded document name including extension.
    swDontRebuildActiveDoc is 1. Zero is swUserDecision and could show a dialog.
    Do not use com_get here: its invocation fallback can repeat a mutation.
    """
    from ..automation.com_utils import out_i4
    errors = out_i4(-1)
    returned = app.ActivateDoc3(name, False, 1, errors)
    status = errors.value
    # Also normalize a typed makepy (document, out-status) return if encountered.
    if isinstance(returned, (tuple, list)):
        if len(returned) != 2:
            raise ValueError("Unexpected activation return shape")
        returned, status = returned
    if isinstance(status, bool) or not isinstance(status, int) or status != 0 or returned is None:
        _reject("TARGET_DOCUMENT_ACTIVATION_FAILED",
                "Activation returned an error, warning or no document; inspect CAD before resuming",
                uncertain=True)
    return returned


def select_target(automation, target, ui_ready):
    """Select and prove identity before legacy preflight; returns journal metadata.

    ui_ready is a non-COM, target-process-scoped probe. The caller starts the
    operation watchdog before entering this function and holds the CAD owner.
    """
    activation_attempted = False
    try:
        if not ui_ready():
            _reject("TARGET_DOCUMENT_UI_BLOCKED", "CAD UI must be ready before target selection")
        before, refs = _snapshot(automation)
        matches = [state for state in before["documents"]
                   if state["path"] and path_key(state["path"]) == path_key(target["path"])]
        if len(matches) != 1:
            _reject("TARGET_DOCUMENT_NOT_OPEN", "Target must identify exactly one already open saved model")
        chosen = matches[0]
        extension_kind = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}[ntpath.splitext(target["path"])[1].lower()]
        if (chosen["type"] != extension_kind or
                "document_type" in target and chosen["type"] != target["document_type"] or
                "configuration" in target and chosen["configuration"] != target["configuration"]):
            _reject("TARGET_DOCUMENT_MISMATCH", "Target type or configuration does not match; no configuration was changed")
        active = before["active_document"]
        switching = active is None or active["key"] != chosen["key"]
        _verify_assembly_edit_target(refs[chosen["key"]], chosen)
        if switching:
            name = ntpath.basename(chosen["path"])
            stem = ntpath.splitext(name)[0].casefold()
            for state in before["documents"]:
                if state["key"] == chosen["key"]:
                    continue
                if ((state["path"] and ntpath.basename(state["path"]).casefold() == name.casefold()) or
                        (not state["path"] and state["title"].casefold() in {name.casefold(), stem})):
                    _reject("TARGET_DOCUMENT_AMBIGUOUS", "Another open document has an ambiguous activation name")
            if chosen["dirty"] or active is not None and (not active["path"] or active["dirty"]):
                _reject("TARGET_DOCUMENT_UNSAVED_CHANGES", "Save active and target models before switching; nothing was saved automatically")
            _verify_no_running_command(automation.app)
            _verify_edit_state(refs[chosen["key"]], chosen)
            if active is not None:
                _verify_edit_state(refs[active["key"]], active)
            if not ui_ready():
                _reject("TARGET_DOCUMENT_UI_BLOCKED", "CAD UI changed before target selection")
            _verify_no_running_command(automation.app)
            activation_attempted = True
            returned = _activate(automation.app, name)
            if automation._strict_document_state(returned) != chosen:
                _reject("TARGET_DOCUMENT_ACTIVATION_MISMATCH", "Activation returned a different model or changed state", uncertain=True)
        after, after_refs = _snapshot(automation)
        if (after["documents"] != before["documents"] or after["session"] != before["session"] or
                after["active_document"] != chosen):
            _reject("TARGET_DOCUMENT_PRESERVATION_FAILED", "Active identity or document preservation could not be verified",
                    uncertain=activation_attempted)
        if not ui_ready():
            _reject("TARGET_DOCUMENT_UI_BLOCKED", "CAD UI is blocked after target selection", uncertain=activation_attempted)
        if switching:
            _verify_no_running_command(automation.app)
            _verify_edit_state(after_refs[chosen["key"]], chosen)
        else:
            _verify_assembly_edit_target(after_refs[chosen["key"]], chosen)
        return {"selected": True, "activated": switching, "document_type": chosen["type"],
                "configuration_verified": True, "inventory_preserved": True}
    except TargetDocumentError as exc:
        if activation_attempted:
            exc.requires_recovery = True
        raise
    except Exception as exc:
        raise TargetDocumentError("TARGET_DOCUMENT_STATE_UNAVAILABLE",
            "Target identity, edit state or preservation could not be verified",
            requires_recovery=activation_attempted) from exc
