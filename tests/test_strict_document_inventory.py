"""Strict document-preservation reads using in-memory COM doubles only."""
import types
import unittest
from unittest.mock import Mock

from solidworks_mcp.automation.documents import DocumentOperations


def document(path=r"C:\synthetic\working.SLDPRT", title="working.SLDPRT", kind=1, dirty=False):
    return types.SimpleNamespace(GetPathName=path, GetTitle=title, GetType=kind,
        GetSaveFlag=dirty, ConfigurationManager=types.SimpleNamespace(
            ActiveConfiguration=types.SimpleNamespace(Name="Default")))


class Harness(DocumentOperations):
    is_connected = True

    def __init__(self, documents, active=None):
        self._sw_app = types.SimpleNamespace(GetDocuments=tuple(documents),
            GetDocumentCount=len(documents), ActiveDoc=active, UserControl=True, Visible=True)

    def _result(self, success, message, error_code=0, data=None):
        return {"success": success, "message": message, "data": data or {}}

    def _error(self, code, message, **kwargs):
        return {"success": False, "code": code, "message": message}


class StrictDocumentInventoryTests(unittest.TestCase):
    def test_part_assembly_drawing_unsaved_and_active_state_are_complete(self):
        part = document()
        assembly = document(r"C:\synthetic\working.SLDASM", "working.SLDASM", 2, True)
        drawing = document("", "Drawing1", 3, True)
        del drawing.ConfigurationManager  # Drawings must not query model configuration.
        h = Harness([part, assembly, drawing], assembly)
        result = h.list_open_documents(strict=True)
        self.assertTrue(result["success"])
        data = result["data"]
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["active_document"]["type"], 2)
        self.assertTrue(data["active_document"]["dirty"])
        self.assertEqual(data["active_document"]["configuration"], "Default")
        self.assertIsNone(next(row for row in data["documents"] if row["type"] == 3)["configuration"])

    def test_zero_documents_is_explicit_success(self):
        h = Harness([])
        h._sw_app.GetDocuments = None
        self.assertEqual(h.list_open_documents(strict=True)["data"],
            {"strict": True, "count": 0, "documents": [], "active_document": None,
             "session": {"user_control": True, "visible": True}})

    def test_application_ownership_and_visibility_reads_are_required(self):
        for field in ("UserControl", "Visible"):
            h = Harness([])
            setattr(h._sw_app, field, None)
            self.assertFalse(h.list_open_documents(strict=True)["success"])

    def test_unknown_identity_dirty_or_configuration_fails_closed(self):
        for field, value in (("GetPathName", None), ("GetTitle", ""),
                             ("GetType", True), ("GetSaveFlag", None),
                             ("ConfigurationManager", None)):
            with self.subTest(field=field):
                doc = document()
                setattr(doc, field, value)
                self.assertFalse(Harness([doc], doc).list_open_documents(strict=True)["success"])

    def test_failed_com_reads_are_not_replaced_with_defaults(self):
        doc = document()
        doc.GetSaveFlag = lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))
        result = Harness([doc], doc).list_open_documents(strict=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "DOCUMENT_IDENTITY_UNAVAILABLE")

    def test_missing_duplicate_or_changed_enumeration_fails_closed(self):
        doc = document()
        for h in (Harness([doc, doc], doc), Harness([doc], document(title="other"))):
            self.assertFalse(h.list_open_documents(strict=True)["success"])
        h = Harness([doc], doc)
        h._sw_app.GetDocumentCount = 2
        self.assertFalse(h.list_open_documents(strict=True)["success"])
        h._sw_app.GetDocumentCount = Mock(side_effect=[1, 2])
        self.assertFalse(h.list_open_documents(strict=True)["success"])

    def test_default_legacy_inventory_does_not_require_strict_members(self):
        h = Harness([])
        h._sw_app = types.SimpleNamespace(GetFirstDocument=None)
        self.assertTrue(h.list_open_documents()["success"])
        self.assertFalse(h.list_open_documents(strict="yes")["success"])


if __name__ == "__main__":
    unittest.main()
