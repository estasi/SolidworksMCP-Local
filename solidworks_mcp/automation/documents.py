"""
SolidWorks Document Operations
------------------------------
Create, open, save, and manage SolidWorks documents.
"""

import os
import logging
import traceback
from typing import Optional, Dict

import win32com.client
import pythoncom

from ..constants import SwErrors, SwDocumentTypes, SwFileTypes, SwSaveAsOptions
from ..utils import find_template
from .com_utils import com_get, resolve_solidworks_constant, typed

logger = logging.getLogger(__name__)


def normalize_save_result(result, errors=0, warnings=0):
    """Normalize makepy (bool, errors, warnings) and dynamic BYREF saves.

    A nonempty tuple is not evidence of success, and a true return with a
    nonzero swFileSaveError bitmask must still be treated as a failed save.
    """
    errors = getattr(errors, "value", errors)
    warnings = getattr(warnings, "value", warnings)
    if isinstance(result, (tuple, list)):
        if len(result) != 3:
            return False, -1, 0
        result, errors, warnings = result
    try:
        errors, warnings = int(errors), int(warnings)
    except (TypeError, ValueError):
        return False, -1, 0
    return bool(result) and errors == 0, errors, warnings


class DocumentOperations:
    """
    Mixin class for document operations

    Requires parent class to have:
    - self._sw_app: SolidWorks application object
    - self.is_connected: Connection status property
    - self.connect(): Connection method
    - self._result(): Result factory method
    - self._units: UnitConverter instance
    """

    @staticmethod
    def _document_identity_path(path):
        return os.path.normcase(os.path.realpath(os.path.abspath(path))) if path else ""

    def _document_identity(self, doc):
        """Read identity without silently converting failed COM reads to blanks."""
        path = com_get(doc, "GetPathName")
        title = com_get(doc, "GetTitle")
        kind = com_get(doc, "GetType")
        if not isinstance(path, str) or not isinstance(title, str) or not title:
            raise ValueError("Document identity is unavailable")
        if kind not in (1, 2, 3):
            raise ValueError("Unknown document type; identity cannot be verified")
        key = self._document_identity_path(path)
        return {"key": key or f"unsaved:{kind}:{title.casefold()}",
                "path": path, "title": title, "type": kind,
                "dirty": com_get(doc, "GetSaveFlag", default=None)}

    def _document_inventory(self):
        documents = com_get(self._sw_app, "GetDocuments")
        if documents is None:
            count = com_get(self._sw_app, "GetDocumentCount")
            if count != 0:
                raise ValueError("Open-document enumeration is unavailable")
            return []
        if not isinstance(documents, (tuple, list)):
            raise ValueError("Unexpected open-document enumeration result")
        result = [self._document_identity(doc) for doc in documents]
        count = com_get(self._sw_app, "GetDocumentCount", default=len(result))
        if count != len(result):
            raise ValueError("Open-document enumeration is incomplete")
        keys = [item["key"] for item in result]
        if len(set(keys)) != len(keys):
            raise ValueError("Open documents have ambiguous identities")
        return result

    def _resolve_document_template(self, document_type):
        """Resolve configured, discovered, or SOLIDWORKS-default template."""
        kind = str(document_type).lower()
        attributes = {
            "part": "part_template",
            "assembly": "assembly_template",
            "drawing": "drawing_template",
        }
        preference_names = {
            "part": "swDefaultTemplatePart",
            "assembly": "swDefaultTemplateAssembly",
            "drawing": "swDefaultTemplateDrawing",
        }
        if kind not in attributes:
            return None, "unsupported"
        configured = str(getattr(
            self._config, attributes[kind], "auto") or "auto")
        if configured.lower() != "auto":
            path = os.path.abspath(configured)
            return (path, "mcp_config") if os.path.isfile(path) else (
                None, "configured_path_missing")
        discovered = find_template(kind)
        if discovered and os.path.isfile(discovered):
            return os.path.abspath(discovered), "filesystem_discovery"
        try:
            preference = resolve_solidworks_constant(preference_names[kind])
            native_default = str(com_get(
                self._sw_app, "GetUserPreferenceStringValue", preference,
                default="") or "")
            if native_default and os.path.isfile(native_default):
                return os.path.abspath(native_default), "solidworks_user_preference"
        except (LookupError, OSError):
            pass
        return None, "not_configured"

    def _create_from_template(self, document_type):
        template, source = self._resolve_document_template(document_type)
        if not template:
            return None, self._error(
                "CAPABILITY_UNAVAILABLE",
                f"No valid {document_type} template is configured",
                recoverable=True,
                recommended_actions=[
                    "Set the default template in SOLIDWORKS System Options, "
                    "or configure its absolute path in solidworks_mcp/config.json."],
                details={"document_type": document_type,
                         "template_source": source})
        doc = self._sw_app.NewDocument(template, 0, 0, 0)
        if doc is None:
            return None, self._error(
                "COM_MEMBER_MISMATCH",
                f"SOLIDWORKS rejected the {document_type} template",
                recoverable=True,
                details={"template": template, "template_source": source})
        return doc, {"template": template, "template_source": source}

    def create_new_part(self) -> Dict:
        """
        Create a new part document

        Returns:
            Result dictionary with document info
        """
        try:
            if not self.is_connected:
                r = self.connect()
                if not r["success"]:
                    return r

            doc, template_info = self._create_from_template("part")
            if doc is None:
                return template_info

            # Set view
            try:
                doc.ShowNamedView2("*Isometric", 7)
                doc.ViewZoomtofit2()
            except:
                pass

            title = self._get_doc_title(doc)

            return self._result(True, f"Created part: {title}",
                              SwErrors.swSuccess,
                              {"name": title, "type": "Part",
                               **template_info})

        except Exception as e:
            logger.error(f"Create part error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFileLoadError)

    def create_new_assembly(self) -> Dict:
        """
        Create a new assembly document

        Returns:
            Result dictionary with document info
        """
        try:
            if not self.is_connected:
                r = self.connect()
                if not r["success"]:
                    return r

            doc, template_info = self._create_from_template("assembly")
            if doc is None:
                return template_info

            try:
                doc.ShowNamedView2("*Isometric", 7)
                doc.ViewZoomtofit2()
            except:
                pass

            title = self._get_doc_title(doc)

            return self._result(True, f"Created assembly: {title}",
                              SwErrors.swSuccess,
                              {"name": title, "type": "Assembly",
                               **template_info})

        except Exception as e:
            logger.error(f"Create assembly error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFileLoadError)

    def create_new_drawing(self, paper_size: str = "A4") -> Dict:
        """
        Create a new drawing document

        Args:
            paper_size: Paper size (A4, A3, A2, A1, Letter)

        Returns:
            Result dictionary with document info
        """
        try:
            if not self.is_connected:
                r = self.connect()
                if not r["success"]:
                    return r

            doc, template_info = self._create_from_template("drawing")
            if doc is None:
                return template_info

            title = self._get_doc_title(doc)

            return self._result(True, f"Created drawing: {title}",
                              SwErrors.swSuccess,
                              {"name": title, "type": "Drawing",
                               "paper_size": paper_size, **template_info})

        except Exception as e:
            logger.error(f"Create drawing error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFileLoadError)

    def open_document(self, filepath: str) -> Dict:
        """
        Open an existing document

        Args:
            filepath: Path to SolidWorks file

        Returns:
            Result dictionary
        """
        try:
            if not self.is_connected:
                r = self.connect()
                if not r["success"]:
                    return r

            if not os.path.exists(filepath):
                return self._result(False, f"File not found: {filepath}",
                                  SwErrors.swFileNotFoundError)

            filepath = os.path.abspath(filepath)
            requested_key = self._document_identity_path(filepath)
            try:
                inventory = self._document_inventory()
            except Exception as exc:
                return self._error(
                    "DOCUMENT_IDENTITY_UNAVAILABLE",
                    "Cannot verify open documents before opening the requested file",
                    details={"reason": str(exc), "path": filepath})
            existing = next((item for item in inventory
                             if item["key"] == requested_key), None)
            if existing:
                active, active_error = self.get_active_doc()
                if not active_error and self._document_identity(active)["key"] == requested_key:
                    return self._result(
                        True, f"Already open: {existing['title']}", SwErrors.swSuccess,
                        {"name": existing["title"], "path": filepath,
                         "already_open": True, "reopened": False})
                return self._error(
                    "DOCUMENT_ALREADY_OPEN",
                    "Requested path is already open in a different document window",
                    details={"path": filepath, "already_open": True,
                             "reopened": False},
                    recommended_actions=[
                        "Activate the existing document window explicitly; no duplicate was opened."])
            title_conflicts = [item for item in inventory if
                               item["title"].casefold() == os.path.basename(filepath).casefold() or
                               (item["path"] and os.path.basename(item["path"]).casefold() ==
                                os.path.basename(filepath).casefold())]
            if title_conflicts:
                return self._error(
                    "DOCUMENT_TITLE_CONFLICT",
                    "Another open document has the requested file name at a different path",
                    details={"path": filepath, "conflicts": title_conflicts},
                    recommended_actions=[
                        "Keep both files intact and resolve the document-name conflict before reopening."])

            # Determine document type from extension
            ext = os.path.splitext(filepath)[1].lower()
            type_map = {
                ".sldprt": SwDocumentTypes.swDocPART,
                ".sldasm": SwDocumentTypes.swDocASSEMBLY,
                ".slddrw": SwDocumentTypes.swDocDRAWING,
            }
            doc_type = type_map.get(ext, SwDocumentTypes.swDocPART)

            # Open document
            errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

            doc = self._sw_app.OpenDoc6(filepath, int(doc_type), 0, "", errors, warnings)

            if doc is None or errors.value != 0:
                return self._result(False, f"Failed to open (error {errors.value})",
                                  SwErrors.swFileLoadError)

            opened_identity = self._document_identity(doc)
            active, active_error = self.get_active_doc()
            if (opened_identity["key"] != requested_key or active_error or
                    self._document_identity(active)["key"] != requested_key):
                return self._error(
                    "DOCUMENT_IDENTITY_CONFLICT",
                    "SOLIDWORKS did not activate the exact requested document",
                    details={"path": filepath, "returned_document": opened_identity})

            title = self._get_doc_title(doc)

            return self._result(True, f"Opened: {title}",
                              SwErrors.swSuccess,
                              {"name": title, "path": filepath,
                               "already_open": False, "reopened": True})

        except Exception as e:
            logger.error(f"Open document error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFileLoadError)

    def save_document(self, filepath: str = None) -> Dict:
        """
        Save the active document
        FIXED v4.1: Use doc.SaveAs() as primary method (avoids COM type mismatch
        with Extension.SaveAs where None parameter fails in SW 2025).

        Args:
            filepath: Path to save (None = save in place)

        Returns:
            Result dictionary
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            if filepath:
                # Ensure absolute path
                filepath = os.path.abspath(filepath)

                # Ensure directory exists
                dir_path = os.path.dirname(filepath)
                if dir_path and not os.path.exists(dir_path):
                    os.makedirs(dir_path)

                saved = False
                method_used = ""

                # Method 1: doc.SaveAs (simplest, most reliable for SW 2025)
                try:
                    result = doc.SaveAs(filepath)
                    if normalize_save_result(result)[0]:
                        saved = True
                        method_used = "SaveAs"
                except Exception as e:
                    logger.debug(f"doc.SaveAs failed: {e}")

                # Method 2: Extension.SaveAs with proper VARIANT null dispatch
                if not saved:
                    try:
                        empty_export = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
                        errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                        warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

                        result = doc.Extension.SaveAs(
                            filepath, 0, 0, empty_export, errors, warnings
                        )
                        if normalize_save_result(result, errors, warnings)[0]:
                            saved = True
                            method_used = "Extension.SaveAs"
                    except Exception as e:
                        logger.debug(f"Extension.SaveAs failed: {e}")

                # Method 3: Extension.SaveAs2
                if not saved:
                    try:
                        errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                        warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

                        result = doc.Extension.SaveAs2(
                            filepath, 0, 0, None, "", False, errors, warnings
                        )
                        if normalize_save_result(result, errors, warnings)[0]:
                            saved = True
                            method_used = "Extension.SaveAs2"
                    except Exception as e:
                        logger.debug(f"Extension.SaveAs2 failed: {e}")

                if not saved:
                    return self._result(False, "Save failed - all methods attempted",
                                      SwErrors.swFileSaveError)

                return self._result(True, f"Saved: {filepath} [{method_used}]",
                                  SwErrors.swSuccess,
                                  {"path": filepath, "method": method_used})
            else:
                # Save in place. Save3 has [in,out] errors/warnings params:
                # the typed wrapper requires explicit placeholders and
                # returns a tuple (ok, errors, warnings); calling the
                # dynamic version without them raises Type mismatch.
                ok = False
                errors_v = 0
                warnings_v = 0
                saved_via = ""

                md_typed = typed(doc, "IModelDoc2")
                if md_typed is not None:
                    try:
                        ret = md_typed.Save3(
                            int(SwSaveAsOptions.swSaveAsOptions_Silent), 0, 0)
                        ok, errors_v, warnings_v = normalize_save_result(ret)
                        saved_via = "IModelDoc2.Save3(typed)"
                    except Exception as e:
                        logger.debug(f"Typed Save3 failed: {e}")

                if not ok and not saved_via:
                    # Dynamic fallback with VARIANT byref out-params
                    try:
                        errors = win32com.client.VARIANT(
                            pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                        warnings = win32com.client.VARIANT(
                            pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                        ret = doc.Save3(
                            int(SwSaveAsOptions.swSaveAsOptions_Silent),
                            errors, warnings)
                        ok, errors_v, warnings_v = normalize_save_result(
                            ret, errors, warnings)
                        saved_via = "Save3(dynamic)"
                    except Exception as e:
                        logger.debug(f"Dynamic Save3 failed: {e}")

                if not ok:
                    return self._result(False,
                        f"Save failed (errors={errors_v}, warnings={warnings_v})",
                        SwErrors.swFileSaveError,
                        {"errors": errors_v, "warnings": warnings_v})

                path = self._get_doc_path(doc)
                return self._result(True, f"Saved: {path}",
                                  SwErrors.swSuccess,
                                  {"path": path, "method": saved_via,
                                   "errors": errors_v, "warnings": warnings_v})

        except Exception as e:
            logger.error(f"Save error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFileSaveError)

    def close_document(self, save: bool = False) -> Dict:
        """
        Close the active document

        Args:
            save: Save before closing

        Returns:
            Result dictionary
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            title = self._get_doc_title(doc)

            if save:
                # Reuse save_document: raw doc.Save3(0,0,0) mishandles the
                # [in,out] errors/warnings params (Type mismatch on SW2026)
                saved = self.save_document()
                if not saved.get("success"):
                    return self._result(
                        False, "Document kept open because save failed",
                        SwErrors.swFileSaveError,
                        {"document": title, "closed": False,
                         "save_result": saved})

            try:
                intended = self._document_identity(doc)
                before = self._document_inventory()
                active, active_error = self.get_active_doc()
                if (active_error or self._document_identity(active)["key"] != intended["key"] or
                        sum(item["key"] == intended["key"] for item in before) != 1):
                    raise ValueError("Active document changed or is missing from enumeration")
                same_title = [item for item in before
                              if item["title"].casefold() == intended["title"].casefold()]
                if len(same_title) > 1 and any(not item["path"] for item in same_title):
                    raise ValueError("Same-title unsaved document makes close ambiguous")
            except Exception as exc:
                return self._error(
                    "DOCUMENT_IDENTITY_UNAVAILABLE",
                    "Document kept open because its close target cannot be verified",
                    details={"closed": False, "reason": str(exc)})

            # Official SOLIDWORKS example 'Get Names of Creators of Features
            # (C#)' passes the same absolute Filename to OpenDoc6 and CloseDoc.
            # A bare title can close a different document in another directory.
            close_name = intended["path"] or intended["title"]
            self._sw_app.CloseDoc(close_name)
            try:
                after = self._document_inventory()
            except Exception as exc:
                return self._error(
                    "DOCUMENT_CLOSE_UNVERIFIED", "Close was requested but read-back failed",
                    details={"document": intended, "closed": None,
                             "close_argument": close_name, "reason": str(exc)})
            remaining = {item["key"]: item for item in after}
            expected = {item["key"]: item for item in before
                        if item["key"] != intended["key"]}
            target_closed = intended["key"] not in remaining
            others_preserved = all(remaining.get(key) == item for key, item in expected.items())
            verified = target_closed and others_preserved and len(remaining) == len(expected)
            details = {"document": intended["title"], "path": intended["path"],
                       "closed": target_closed, "close_argument": close_name,
                       "other_documents_preserved": others_preserved,
                       "remaining_document_count": len(after)}
            if not verified:
                details["missing_other_documents"] = [item for key, item in expected.items()
                                                       if key not in remaining]
                return self._error(
                    "DOCUMENT_CLOSE_UNVERIFIED",
                    "Close result did not match the intended document and preserved-document inventory",
                    details=details)
            return self._result(True, f"Closed: {intended['title']}",
                                SwErrors.swSuccess, details)

        except Exception as e:
            logger.error(f"Close error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def get_document_info(self) -> Dict:
        """
        Get information about the active document.
        FIXED v5.0: GetType is a PROPERTY (int) in SW 2025/2026 dynamic COM -
        calling it as doc.GetType() raised "'int' object is not callable".
        All member access now goes through com_get().

        Returns:
            Result dictionary with document details
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            type_names = {
                0: "None",
                1: "Part",
                2: "Assembly",
                3: "Drawing"
            }

            doc_type = com_get(doc, "GetType", default=-1)
            title = self._get_doc_title(doc)
            path = self._get_doc_path(doc)

            info = {
                "title": title,
                "path": path if path else "Not saved",
                "type": type_names.get(doc_type, "Unknown"),
                "type_code": doc_type,
            }

            # Extra diagnostics (best effort)
            try:
                info["feature_count"] = com_get(
                    doc, "GetFeatureCount", default=None)
            except Exception:
                pass
            try:
                active_sketch = doc.SketchManager.ActiveSketch
                info["has_active_sketch"] = active_sketch is not None
            except Exception:
                info["has_active_sketch"] = None
            if doc_type == int(SwDocumentTypes.swDocPART):
                try:
                    bodies = doc.GetBodies2(0, False)  # solid bodies incl. hidden
                    info["solid_body_count"] = len(bodies) if bodies else 0
                except Exception:
                    info["solid_body_count"] = None

            return self._result(True, f"{title} ({info['type']})",
                              SwErrors.swSuccess, info)

        except Exception as e:
            logger.error(f"Get info error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def _strict_document_state(self, doc) -> Dict:
        """Read preservation evidence; unavailable state is never a blank value."""
        path = com_get(doc, "GetPathName")
        title = com_get(doc, "GetTitle")
        kind = com_get(doc, "GetType")
        dirty = com_get(doc, "GetSaveFlag")
        if (not isinstance(path, str) or not isinstance(title, str) or not title or
                (path and not os.path.isabs(path)) or
                isinstance(kind, bool) or not isinstance(kind, int) or kind not in (1, 2, 3) or
                not isinstance(dirty, bool)):
            raise ValueError("Document identity or dirty state is unavailable")
        configuration = None
        if kind in (1, 2):
            manager = com_get(doc, "ConfigurationManager")
            active = com_get(manager, "ActiveConfiguration")
            configuration = com_get(active, "Name")
            if not isinstance(configuration, str) or not configuration:
                raise ValueError("Document configuration is unavailable")
        # Drawings do not have an active model configuration. Do not ask their
        # referenced models to resolve or activate a configuration for this read.
        key = self._document_identity_path(path)
        return {"key": key or f"unsaved:{kind}:{title.casefold()}",
                "path": path, "title": title, "type": kind,
                "dirty": dirty, "configuration": configuration}

    def _strict_document_inventory(self) -> Dict:
        user_control = com_get(self._sw_app, "UserControl")
        visible = com_get(self._sw_app, "Visible")
        if not isinstance(user_control, bool) or not isinstance(visible, bool):
            raise ValueError("Application lifetime or visibility state is unavailable")
        count = com_get(self._sw_app, "GetDocumentCount")
        documents = com_get(self._sw_app, "GetDocuments")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Open-document count is unavailable")
        if documents is None and count == 0:
            documents = []
        if not isinstance(documents, (tuple, list)) or len(documents) != count:
            raise ValueError("Open-document enumeration is incomplete")
        states = [self._strict_document_state(doc) for doc in documents]
        keys = [item["key"] for item in states]
        if len(set(keys)) != count:
            raise ValueError("Open documents have ambiguous identities")
        active = com_get(self._sw_app, "ActiveDoc")
        active_state = self._strict_document_state(active) if active is not None else None
        if active_state is not None and active_state not in states:
            raise ValueError("Active document changed during enumeration")
        final_count = com_get(self._sw_app, "GetDocumentCount")
        if isinstance(final_count, bool) or not isinstance(final_count, int) or final_count != count:
            raise ValueError("Open documents changed during enumeration")
        return {"strict": True, "count": count,
                "documents": sorted(states, key=lambda item: item["key"]),
                "active_document": active_state,
                "session": {"user_control": user_control, "visible": visible}}

    def list_open_documents(self, strict: bool = False) -> Dict:
        """
        List all open documents

        Returns:
            Result dictionary with document list
        """
        try:
            if not isinstance(strict, bool):
                return self._error("INVALID_INPUT", "strict must be a boolean")
            if not self.is_connected:
                r = self.connect()
                if not r["success"]:
                    return r

            if strict:
                try:
                    inventory = self._strict_document_inventory()
                except Exception as exc:
                    return self._error("DOCUMENT_IDENTITY_UNAVAILABLE",
                                       "Strict document inventory could not be verified",
                                       details={"reason": str(exc)})
                return self._result(True, f"{inventory['count']} document(s) verified",
                                    SwErrors.swSuccess, inventory)

            docs = []

            # GetFirstDocument is a PROPERTY in SW 2025 COM, not a method.
            # Calling it as a method raises DISP_E_MEMBERNOTFOUND
            # (-2147352573, "Member not found").
            try:
                doc = self._sw_app.GetFirstDocument
            except AttributeError:
                doc = self._sw_app.GetFirstDocument()

            while doc:
                try:
                    # GetTitle / GetType are properties in SW 2025
                    title = doc.GetTitle
                    try:
                        doc_type = doc.GetType
                    except:
                        doc_type = doc.GetType()

                    type_names = {1: "Part", 2: "Assembly", 3: "Drawing"}

                    docs.append({
                        "title": title,
                        "type": type_names.get(doc_type, "Unknown")
                    })
                except:
                    pass

                # GetNext is a PROPERTY in SW 2025 COM
                try:
                    doc = doc.GetNext
                except:
                    break

            return self._result(True, f"{len(docs)} document(s) open",
                              SwErrors.swSuccess, {"documents": docs})

        except Exception as e:
            logger.error(f"List documents error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)
