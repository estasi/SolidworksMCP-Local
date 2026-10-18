"""Attach-only owner pinning; all endpoint/process/transport calls are synthetic."""
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp import __version__
from solidworks_mcp.coordination import client as client_module
from solidworks_mcp.coordination.client import QueueClient
from solidworks_mcp.coordination.protocol import PROTOCOL_VERSION, QueueError


OWNER_ENV = "SOLIDWORKS_MCP_EXPECTED_OWNER_ID"
OWNER = "a" * 32
OTHER = "b" * 32


class OwnerPinTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"SOLIDWORKS_MCP_TEST_BACKEND": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(OWNER_ENV, None)
        self.state = Path("C:/synthetic-owner-pin/state")
        canonical = patch.object(client_module, "default_state_dir", return_value=self.state)
        self.default_state = canonical.start()
        self.addCleanup(canonical.stop)
        self.process = {"pid": 12345, "creation_token": "synthetic-generation"}
        self.endpoint = {
            "protocol": PROTOCOL_VERSION, "backend": "native", "host": "127.0.0.1",
            "source_root": str(client_module.source_root()), "owner_id": OWNER,
            "process": self.process, "version": __version__,
            "launch": {"mode": "no_job", "process": self.process, "retained_os_job": False},
        }
        for name, value in (("read_json", self.endpoint), ("process_identity", self.process)):
            mocked = patch.object(client_module, name, return_value=value)
            setattr(self, name, mocked.start())
            self.addCleanup(mocked.stop)
        launch = patch("solidworks_mcp.coordination.desktop_launch.launch_desktop",
                       side_effect=AssertionError("No real desktop launch in owner tests"))
        self.desktop_launch = launch.start()
        self.addCleanup(launch.stop)

    def client(self, **kwargs):
        value = QueueClient(expected_owner_id=OWNER, **kwargs)
        value._rpc = Mock(return_value={"owner_id": OWNER})
        value._start_breakaway = Mock(side_effect=AssertionError("Pinned clients must not launch"))
        return value

    def assert_rejected(self, client, code):
        with self.assertRaises(QueueError) as rejected:
            client.ensure_broker()
        self.assertEqual(rejected.exception.code, code)
        client._start_breakaway.assert_not_called()
        self.desktop_launch.assert_not_called()

    def test_native_environment_is_captured_at_construction_and_disables_start(self):
        os.environ[OWNER_ENV] = OWNER
        client = QueueClient(auto_start=True)
        os.environ[OWNER_ENV] = OTHER
        self.assertEqual(client.expected_owner_id, OWNER)
        self.assertFalse(client.auto_start)

    def test_invalid_native_environment_fails_before_state_inspection(self):
        for value in ("", "a" * 31, "a" * 33, "A" * 32, "g" * 32, " " + OWNER,
                      "00000000-0000-0000-0000-000000000000"):
            with self.subTest(value=value), patch.dict(os.environ, {OWNER_ENV: value}):
                self.default_state.reset_mock()
                with self.assertRaises(QueueError) as rejected:
                    QueueClient()
                self.assertEqual(rejected.exception.code, "INVALID_EXPECTED_OWNER_ID")
                self.default_state.assert_not_called()
        self.read_json.assert_not_called()

    def test_invalid_explicit_pin_fails_closed(self):
        for value in (False, 42, [], {}, ""):
            with self.subTest(value=value), self.assertRaises(QueueError) as rejected:
                QueueClient(expected_owner_id=value)
            self.assertEqual(rejected.exception.code, "INVALID_EXPECTED_OWNER_ID")

    def test_matching_explicit_and_environment_pin_is_valid(self):
        os.environ[OWNER_ENV] = OWNER
        client = QueueClient(expected_owner_id=OWNER)
        self.assertEqual(client.expected_owner_id, OWNER)
        self.assertFalse(client.auto_start)

    def test_conflicting_explicit_and_environment_pin_is_rejected(self):
        os.environ[OWNER_ENV] = OTHER
        with self.assertRaises(QueueError) as rejected:
            QueueClient(expected_owner_id=OWNER)
        self.assertEqual(rejected.exception.code, "INVALID_EXPECTED_OWNER_ID")
        self.read_json.assert_not_called()

    def test_native_default_start_policy_is_unchanged_without_pin(self):
        self.assertIsNone(QueueClient().expected_owner_id)
        self.assertTrue(QueueClient().auto_start)
        self.assertFalse(QueueClient(auto_start=False).auto_start)

    def test_test_backend_does_not_inherit_native_environment(self):
        os.environ[OWNER_ENV] = "invalid-native-pin"
        client = QueueClient(self.state, backend="test")
        self.assertIsNone(client.expected_owner_id)
        self.assertTrue(client.auto_start)

    def test_explicit_test_backend_pin_is_supported_and_attach_only(self):
        client = QueueClient(self.state, backend="test", expected_owner_id=OWNER)
        self.assertEqual(client.expected_owner_id, OWNER)
        self.assertFalse(client.auto_start)

    def test_valid_owner_retains_process_and_native_lifetime_checks(self):
        client = self.client()
        self.assertIs(client.ensure_broker(), self.endpoint)
        self.process_identity.assert_called_once_with(self.process["pid"])
        client._rpc.assert_called_once_with(self.endpoint, "status")
        client._start_breakaway.assert_not_called()
        self.desktop_launch.assert_not_called()

    def test_changed_or_missing_owner_is_preserved_and_never_launched(self):
        for owner in (OTHER, None):
            with self.subTest(owner=owner):
                self.read_json.return_value = {**self.endpoint, "owner_id": owner}
                client = self.client()
                self.assert_rejected(client, "BROKER_OWNER_CHANGED")
                client._rpc.assert_not_called()
        self.process_identity.assert_not_called()

    def test_owner_change_between_requests_stops_same_client(self):
        self.read_json.side_effect = [self.endpoint, {**self.endpoint, "owner_id": OTHER}]
        client = self.client()
        client.ensure_broker()
        self.assert_rejected(client, "BROKER_OWNER_CHANGED")
        self.assertEqual(client._rpc.call_count, 1)

    def test_stopped_process_never_restarts(self):
        self.process_identity.side_effect = OSError("Synthetic exited process")
        client = self.client()
        self.assert_rejected(client, "BROKER_NOT_RUNNING")
        client._rpc.assert_not_called()

    def test_missing_endpoint_never_starts(self):
        self.read_json.side_effect = FileNotFoundError("Synthetic absent endpoint")
        client = self.client()
        self.assert_rejected(client, "BROKER_NOT_RUNNING")
        client._rpc.assert_not_called()

    def test_transport_failure_never_starts(self):
        client = self.client()
        client._rpc.side_effect = ConnectionRefusedError("Synthetic stopped listener")
        self.assert_rejected(client, "BROKER_NOT_RUNNING")
        self.assertEqual(client._rpc.call_count, 1)

    def test_reused_pid_with_same_owner_field_still_rejects(self):
        self.process_identity.return_value = {**self.process, "creation_token": "replacement"}
        client = self.client()
        self.assert_rejected(client, "BROKER_NOT_RUNNING")
        client._rpc.assert_not_called()

    def test_pin_does_not_bypass_native_version_or_lifetime(self):
        for changed, code in (({"version": "other"}, "RELEASE_MISMATCH"),
                              ({"launch": {}}, "BROKER_LIFETIME_UNVERIFIED")):
            with self.subTest(code=code):
                self.read_json.return_value = {**self.endpoint, **changed}
                client = self.client()
                self.assert_rejected(client, code)
                client._rpc.assert_not_called()

    def test_submit_never_retries_owner_change(self):
        client = self.client()
        client.request = Mock(side_effect=QueueError("BROKER_OWNER_CHANGED", "Synthetic changed owner"))
        with patch.object(client_module, "validate_activation"), self.assertRaises(QueueError) as rejected:
            client.submit("synthetic", {}, request_id="stable-request")
        self.assertEqual(rejected.exception.code, "BROKER_OWNER_CHANGED")
        self.assertEqual(client.request.call_count, 1)

    def test_unpinned_synthetic_start_still_works(self):
        client = QueueClient(self.state, backend="test")
        client._endpoint = Mock(side_effect=[FileNotFoundError("Synthetic absent"), self.endpoint])
        client._rpc = Mock(return_value={})
        client._start_breakaway = Mock()
        with patch.object(client_module, "private_directory"):
            self.assertIs(client.ensure_broker(), self.endpoint)
        client._start_breakaway.assert_called_once()
        self.desktop_launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
