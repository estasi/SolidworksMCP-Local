"""Document close/open identity tests. No live SOLIDWORKS calls."""

import tempfile
import unittest
from pathlib import Path

from solidworks_mcp.automation.documents import DocumentOperations
from solidworks_mcp.automation.runtime import error_result


class Document:
    def __init__(self, path="", title=None, dirty=False):
        self.path = path
        self.title = title or Path(path).name
        self.dirty = dirty
        self.kind = 1

    def GetPathName(self):
        return self.path

    def GetTitle(self):
        return self.title

    def GetType(self):
        return self.kind

    def GetSaveFlag(self):
        return self.dirty


class App:
    def __init__(self, documents, active):
        self.documents = list(documents)
        self.ActiveDoc = active
        self.close_calls = []
        self.open_calls = []
        self.close_noop = False
        self.close_wrong = False
        self.enumeration_error = False

    def GetDocuments(self):
        if self.enumeration_error:
            raise RuntimeError("enumeration unavailable")
        return tuple(self.documents)

    def GetDocumentCount(self):
        return len(self.documents)

    def CloseDoc(self, name):
        self.close_calls.append(name)
        if self.close_noop:
            return
        if self.close_wrong:
            selected = self.documents[0]
        else:
            selected = next((doc for doc in self.documents
                             if doc.path == name or doc.title == name), None)
        if selected:
            self.documents.remove(selected)
            if self.ActiveDoc is selected:
                self.ActiveDoc = self.documents[-1] if self.documents else None

    def OpenDoc6(self, filepath, kind, options, configuration, errors, warnings):
        self.open_calls.append(filepath)
        self.ActiveDoc = Document(filepath)
        self.documents.append(self.ActiveDoc)
        return self.ActiveDoc


class Harness(DocumentOperations):
    is_connected = True

    def __init__(self, app):
        self._sw_app = app
        self.saved = {"success": True}

    def get_active_doc(self):
        return self._sw_app.ActiveDoc, None

    def _get_doc_title(self, doc):
        return doc.title

    def _get_doc_path(self, doc):
        return doc.path

    def _result(self, success, message, error_code=0, data=None):
        return {"success": success, "message": message, "data": data or {}}

    def _error(self, code, message, **kwargs):
        return error_result(self, code, message, **kwargs)

    def save_document(self):
        return self.saved


class DocumentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.path1 = root / "cycle01" / "native_cycle.SLDPRT"
        self.path3 = root / "cycle03" / "native_cycle.SLDPRT"
        for path in (self.path1, self.path3):
            path.parent.mkdir()
            path.write_text("synthetic fixture")
        self.other = Document(str(self.path1), dirty=True)
        self.target = Document(str(self.path3), dirty=False)
        self.app = App([self.other, self.target], self.target)
        self.h = Harness(self.app)

    def test_same_title_saved_documents_close_exact_full_path_and_preserve_other(self):
        result = self.h.close_document()
        self.assertTrue(result["success"], result)
        self.assertEqual(self.app.close_calls, [str(self.path3)])
        self.assertEqual(self.app.documents, [self.other])
        self.assertTrue(self.other.dirty)
        self.assertTrue(result["data"]["other_documents_preserved"])

    def test_unsaved_same_title_conflict_is_rejected_without_closing(self):
        self.other.path = ""
        result = self.h.close_document()
        self.assertFalse(result["success"])
        self.assertEqual(self.app.close_calls, [])
        self.assertEqual(len(self.app.documents), 2)

    def test_duplicate_unsaved_identities_are_rejected(self):
        self.other.path = self.target.path = ""
        self.assertFalse(self.h.close_document()["success"])
        self.assertEqual(self.app.close_calls, [])

    def test_unique_unsaved_document_can_close_after_inventory_verification(self):
        self.target.path = ""
        self.target.title = "UnsavedPart"
        result = self.h.close_document()
        self.assertTrue(result["success"])
        self.assertEqual(self.app.close_calls, ["UnsavedPart"])
        self.assertEqual(self.app.documents, [self.other])

    def test_unknown_enumeration_prevents_close(self):
        self.app.enumeration_error = True
        result = self.h.close_document()
        self.assertFalse(result["success"])
        self.assertEqual(self.app.close_calls, [])

    def test_unknown_document_type_prevents_close(self):
        self.other.kind = 99
        self.assertFalse(self.h.close_document()["success"])
        self.assertEqual(self.app.close_calls, [])

    def test_close_noop_is_not_reported_as_success(self):
        self.app.close_noop = True
        result = self.h.close_document()
        self.assertFalse(result["success"])
        self.assertFalse(result["data"]["error"]["details"]["closed"])

    def test_wrong_native_close_is_reported_with_missing_other_document(self):
        self.app.close_wrong = True
        result = self.h.close_document()
        self.assertFalse(result["success"])
        details = result["data"]["error"]["details"]
        self.assertFalse(details["closed"])
        self.assertFalse(details["other_documents_preserved"])
        self.assertEqual(details["missing_other_documents"][0]["path"], str(self.path1))

    def test_failed_save_still_prevents_close(self):
        self.h.saved = {"success": False, "message": "save failed"}
        result = self.h.close_document(save=True)
        self.assertFalse(result["success"])
        self.assertFalse(result["data"]["closed"])
        self.assertEqual(self.app.close_calls, [])

    def test_active_switch_during_save_prevents_close(self):
        def save_and_switch():
            self.app.ActiveDoc = self.other
            return {"success": True}
        self.h.save_document = save_and_switch
        self.assertFalse(self.h.close_document(save=True)["success"])
        self.assertEqual(self.app.close_calls, [])

    def test_open_exact_path_already_active_returns_explicit_no_reopen(self):
        result = self.h.open_document(str(self.path3))
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["already_open"])
        self.assertFalse(result["data"]["reopened"])
        self.assertEqual(self.app.open_calls, [])

    def test_open_exact_path_inactive_returns_explicit_conflict(self):
        result = self.h.open_document(str(self.path1))
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["error"]["code"], "DOCUMENT_ALREADY_OPEN")
        self.assertEqual(self.app.open_calls, [])

    def test_same_title_other_path_blocks_reopen_without_generic_com_error(self):
        self.h.close_document()
        result = self.h.open_document(str(self.path3))
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["error"]["code"], "DOCUMENT_TITLE_CONFLICT")
        self.assertEqual(self.app.open_calls, [])

    def test_close_then_reopen_exact_path_when_no_title_conflict(self):
        self.other.title = "OtherPart.SLDPRT"
        self.other.path = str(self.path1.with_name("OtherPart.SLDPRT"))
        result = self.h.close_document()
        self.assertTrue(result["success"])
        reopened = self.h.open_document(str(self.path3))
        self.assertTrue(reopened["success"], reopened)
        self.assertTrue(reopened["data"]["reopened"])
        self.assertEqual(self.app.ActiveDoc.path, str(self.path3))
        self.assertIn(self.other, self.app.documents)


if __name__ == "__main__":
    unittest.main()
