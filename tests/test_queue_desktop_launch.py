"""Pure mocked launch provenance guards; no Explorer or CAD calls."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from solidworks_mcp import __version__
from solidworks_mcp.coordination import desktop_launch, launch
from solidworks_mcp.coordination.protocol import QueueError


class DesktopProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = {"path": str(self.root / "state"), "device": 10, "inode": 20}
        self.journal = {"path": str(self.root / "state" / "queue.sqlite3"), "device": 10, "inode": 21}
        self.explorer = {"pid": 100, "creation_token": "old-explorer"}
        self.current = {"pid": 200, "creation_token": "child"}
        self.ticket = Path(self.state["path"]) / "launch" / ("a" * 32 + ".json")
        self.value = {"format": 1, "nonce": "a" * 32, "created_at": time.time(),
            "source_root": str(desktop_launch.source_root()), "version": __version__,
            "base_executable": str(Path(sys.executable).resolve()), "scope": "same-user:session=1",
            "explorer": self.explorer, "state": self.state, "journal": self.journal, "backend": "test"}
        for name, value in (("user_session_identity", self.value["scope"]),
                            ("verify_explorer", self.explorer), ("process_identity", self.current)):
            mock = patch.object(desktop_launch, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(desktop_launch, "physical_identity", side_effect=lambda path:
            self.journal if Path(path).name == "queue.sqlite3" else self.state)
        self.physical = mock.start()
        self.addCleanup(mock.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)

    def test_validated_desktop_ticket_preserves_original_journal_identity(self):
        proof = desktop_launch.validate_ticket(self.value, self.ticket)
        self.assertEqual(proof["journal_before"], self.journal)
        self.assertEqual(proof["process"], self.current)
        self.assertEqual(proof["explorer"], self.explorer)
        self.assertEqual(proof["mode"], "desktop")

    def test_wrong_parent_or_scope_refuses_before_journal_inspection(self):
        for method, wrong, expected in (
            ("verify_explorer", {"pid": 101, "creation_token": "new"}, "DESKTOP_LAUNCH_UNVERIFIED"),
            ("user_session_identity", "another-user:session=1", "DESKTOP_SCOPE_MISMATCH")):
            with self.subTest(method=method), patch.object(desktop_launch, method, return_value=wrong):
                self.physical.reset_mock()
                with self.assertRaises(QueueError) as rejected:
                    desktop_launch.validate_ticket(self.value, self.ticket)
                self.assertEqual(rejected.exception.code, expected)
                self.physical.assert_not_called()

    def test_replaced_physical_journal_is_rejected(self):
        self.physical.side_effect = lambda path: (
            {**self.journal, "inode": 99} if Path(path).name == "queue.sqlite3" else self.state)
        with self.assertRaises(QueueError) as rejected:
            desktop_launch.validate_ticket(self.value, self.ticket)
        self.assertEqual(rejected.exception.code, "JOURNAL_IDENTITY_CHANGED")

    def test_expired_request_and_moved_ticket_are_rejected(self):
        for change, path in (({"created_at": time.time() - 100}, self.ticket),
                             ({}, self.ticket.with_name("other.json"))):
            with self.subTest(change=change):
                value = {**self.value, **change}
                with self.assertRaises(QueueError):
                    desktop_launch.validate_ticket(value, path)

    def test_native_physical_base_is_pinned_and_revalidated(self):
        value = {**self.value, "backend": "native", "local_appdata": str(self.root / "physical")}
        with patch.object(desktop_launch, "default_state_dir", return_value=Path(self.state["path"])):
            desktop_launch.validate_ticket(value, self.ticket)
        self.assertEqual(os.environ["LOCALAPPDATA"], value["local_appdata"])
        with patch.object(desktop_launch, "default_state_dir", return_value=self.root / "wrong"):
            with self.assertRaises(QueueError) as rejected:
                desktop_launch.validate_ticket(value, self.ticket)
        self.assertEqual(rejected.exception.code, "NONCANONICAL_STATE")

    @unittest.skipUnless(os.name == "nt", "Windows job APIs")
    def test_retained_os_job_requires_live_matching_desktop_proof(self):
        proof = {"mode": "desktop", "process": self.current, "explorer": self.explorer,
                 "scope": self.value["scope"]}
        with patch.object(launch, "_verified_desktop", proof), \
                patch("solidworks_mcp.coordination.ownership.process_identity", return_value=self.current), \
                patch("solidworks_mcp.coordination.ownership.user_session_identity", return_value=self.value["scope"]), \
                patch("win32job.IsProcessInJob", return_value=True):
            launch.require_independent_lifetime()
            with patch.object(desktop_launch, "verify_explorer", return_value={"pid": 100, "creation_token": "reused"}):
                with self.assertRaises(QueueError):
                    launch.require_independent_lifetime()


if __name__ == "__main__":
    unittest.main()
