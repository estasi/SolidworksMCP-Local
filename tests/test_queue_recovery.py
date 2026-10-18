"""Crash-safe pre-COM recovery boundaries; all native calls are doubles."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from solidworks_mcp.coordination.broker import Broker
from solidworks_mcp.coordination.journal import Journal, JournalError, UNSTARTED_NATIVE_CONTEXT
from solidworks_mcp.coordination.protocol import QueueError, result_content


class PreNativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.broker = Broker(self.temporary.name, "native")
        self.journal = self.broker.journal = Journal(Path(self.temporary.name) / "queue.sqlite3")
        self.journal.enqueue("save_document", {}, "recovery-boundary", "client")
        self.job = self.journal.claim_next(self.broker.owner_id,
                                         execution_context=UNSTARTED_NATIVE_CONTEXT)
        self.payload = {"expected_job_ids": [self.job["id"]], "operator": "test",
                        "note": "Checked current synthetic idle CAD state"}

    def verified_backend(self):
        backend = Mock()
        backend.validate_recovery.return_value = {"verified": True}
        self.broker._initialize_backend = Mock(return_value=backend)
        self.broker._preflight_recovery_ui = Mock()
        return backend

    def test_restarted_recovery_checks_native_window_before_com_initialization(self):
        self.journal.recover_started()
        self.broker._initialize_backend = Mock(side_effect=AssertionError("Must not attach blocked CAD"))
        with patch("solidworks_mcp.coordination.broker.validate_activation", return_value={"target_pid": 44}), \
             patch("solidworks_mcp.coordination.native_watchdog.inspect_target_windows",
                   return_value={"ready": False}) as inspection:
            self.broker._recover(self.payload)
        inspection.assert_called_once_with(44)
        self.broker._initialize_backend.assert_not_called()
        self.assertEqual(self.broker.last_error["code"], "CAD_UI_NOT_READY")
        self.assertTrue(self.journal.status()["recovery_required"])

    def test_activation_failure_before_com_can_be_reconciled_after_repair(self):
        self.broker._initialize_backend = Mock(side_effect=AssertionError("Must not attach CAD"))
        with patch("solidworks_mcp.coordination.broker.validate_activation",
                   side_effect=QueueError("NOT_ACTIVATED", "Synthetic missing activation")):
            self.broker._execute(self.job)
        self.broker._initialize_backend.assert_not_called()
        self.assertEqual(self.journal.get(self.job["id"])["state"], "recovery_required")
        backend = self.verified_backend()
        with patch("solidworks_mcp.coordination.broker.process_is_same_alive",
                   side_effect=AssertionError("No previous native execution exists")):
            self.broker._recover(self.payload)
        backend.validate_recovery.assert_called_once()
        self.assertIsNone(self.broker.last_error)
        self.assertEqual(self.journal.get(self.job["id"])["state"], "failed")
        self.assertFalse(self.journal.status()["recovery_required"])

    def test_process_death_between_claim_and_com_binding_retains_proof(self):
        # Simulate a fresh dispatcher opening the durable journal after process death.
        fresh = Journal(self.journal.path)
        fresh.recover_started()
        stored = fresh.get(self.job["id"])
        self.assertEqual(stored["execution_context"], UNSTARTED_NATIVE_CONTEXT)
        self.assertIsNone(stored["result"])
        self.verified_backend()
        self.broker._recover(self.payload)
        self.assertEqual(fresh.get(self.job["id"])["state"], "failed")

    def test_native_context_replaces_proof_once_and_cannot_revert(self):
        context = {"target_pid": 44, "target_created_at": 123.0, "expected_major": 33}
        self.journal.set_context(self.job["id"], self.broker.owner_id, context)
        self.assertEqual(self.journal.get(self.job["id"])["execution_context"], context)
        with self.assertRaises(JournalError):
            self.journal.set_context(self.job["id"], self.broker.owner_id, UNSTARTED_NATIVE_CONTEXT)
        self.journal.recover_started()
        self.broker._initialize_backend = Mock(side_effect=AssertionError("Must not attach CAD"))
        with patch("solidworks_mcp.coordination.broker.process_is_same_alive", return_value=True):
            self.broker._recover(self.payload)
        self.broker._initialize_backend.assert_not_called()
        self.assertEqual(self.broker.last_error["code"], "PREVIOUS_CAD_OUTCOME_UNKNOWN")

    def test_result_only_no_execution_claim_is_not_durable_proof(self):
        self.journal.set_context(self.job["id"], self.broker.owner_id,
                                 {"target_pid": 44, "target_created_at": 123.0})
        self.journal.mark_recovery(self.job["id"], self.broker.owner_id, "test")
        self.journal.record_late_result(self.job["id"], self.broker.owner_id,
            result_content(False, "Untrusted result claim", {"native_execution_not_started": True}))
        self.broker._initialize_backend = Mock(side_effect=AssertionError("Must not attach CAD"))
        with patch("solidworks_mcp.coordination.broker.process_is_same_alive", return_value=True):
            self.broker._recover(self.payload)
        self.broker._initialize_backend.assert_not_called()
        self.assertEqual(self.broker.last_error["code"], "PREVIOUS_CAD_OUTCOME_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
