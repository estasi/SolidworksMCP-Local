"""Deployment/recovery gates without any live CAD calls."""
import json
import os
from pathlib import Path
import tempfile
import time
import subprocess
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp.coordination import platform_guard as guards
from solidworks_mcp.coordination.broker import Broker
from solidworks_mcp.coordination.client import QueueClient
from solidworks_mcp.coordination.journal import Journal
from solidworks_mcp.coordination.protocol import QueueError, decode, finite_seconds


class PlatformTests(unittest.TestCase):
    def test_deadline_during_initialization_never_starts_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = Broker(temporary, "native")
            broker.journal = Journal(Path(temporary) / "queue.sqlite3")
            job = broker.journal.enqueue("save_document", {}, "initialization-test", "client",
                                         execution_timeout_sec=.01)
            job = broker.journal.claim_next(broker.owner_id)
            backend = Mock()
            def delayed_initialization():
                time.sleep(.06)
                return backend
            broker._initialize_backend = delayed_initialization
            activation = {"target_pid": 123, "target_created_at": 1000, "expected_major": 33}
            with patch("solidworks_mcp.coordination.broker.validate_activation", return_value=activation):
                broker._execute(job)
            backend.execute.assert_not_called()
            stored = broker.journal.get(job["id"])
            self.assertEqual(stored["state"], "recovery_required")
            self.assertTrue(json.loads(stored["result"][0]["text"])["data"]["operation_not_started"])

    def test_lost_wait_response_retains_durable_request_and_job_handles(self):
        client = QueueClient()
        client.submit = Mock(return_value={"id": "accepted-job"})
        client.wait = Mock(side_effect=OSError("Connection interrupted"))
        content = client.call_tool("save_document", {"coordination": {"request_id": "stable-request"}})
        payload = json.loads(content[0]["text"])
        self.assertFalse(payload["success"])
        self.assertEqual(payload["data"]["request_id"], "stable-request")
        self.assertEqual(payload["data"]["job_id"], "accepted-job")

    def test_invalid_response_wait_never_admits_a_cad_job(self):
        client = QueueClient()
        client.submit = Mock(side_effect=AssertionError("Invalid request must not submit"))
        content = client.call_tool("save_document", {"coordination": {"wait_timeout_sec": 61}})
        self.assertFalse(json.loads(content[0]["text"])["success"])
        client.submit.assert_not_called()

    def test_unknown_python_and_relative_launcher_are_blockers(self):
        allowed = str(guards.source_root() / "solidworks_mcp_server.py")
        processes = [{"pid": 10, "name": "python.exe", "script": None},
                     {"pid": 11, "name": "python.exe", "script": "solidworks_mcp_server.py"},
                     {"pid": 12, "name": "python.exe", "script": allowed},
                     {"pid": 13, "name": "SLDWORKS.exe", "script": None}]
        self.assertEqual([p["pid"] for p in guards.legacy_processes(processes)], [10, 11])

    @unittest.skipUnless(os.name == "nt", "PowerShell inventory classifier regression")
    def test_module_and_inline_legacy_entrypoints_are_selected_and_blocked(self):
        """Exercise the actual PowerShell filter against a synthetic CIM shim.

        The shim replaces Get-CimInstance before the production script runs;
        neither real process inventory nor COM/native CAD is inspected.
        """
        allowed = str(guards.source_root() / "solidworks_mcp_server.py")
        cases = [
            (101, "python.exe", "python.exe -m solidworks_mcp.server"),
            (102, "pythonw.exe", "pythonw.exe -m solidworks_mcp_server"),
            (103, "py.exe", 'py.exe -3.11 -m "solidworks_mcp.server"'),
            (104, "pyw.exe", "pyw.exe -m solidworks_mcp_server"),
            (105, "python3.11.exe", "python3.11.exe -msolidworks_mcp.server"),
            (106, "python.exe", f'python.exe -m solidworks_mcp.server "{allowed}"'),
            (107, "python.exe", 'python.exe -c "from solidworks_mcp.server import run; run()"'),
            (108, "python.exe", 'python.exe -c "import solidworks_mcp_server; solidworks_mcp_server.run()"'),
            (109, "python.exe", None),
            (110, "python.exe", 'python.exe "C:\\synthetic-old\\solidworks_mcp_server.py"'),
            (111, "python.exe", f'python.exe "{allowed}"'),
            (112, "SLDWORKS.exe", "SLDWORKS.exe"),
            (113, "python.exe", "python.exe -m solidworks_mcp.coordination.broker"),
            (114, "python.exe", "python.exe -m unrelated_package"),
            (115, "python.exe", "python.exe -m solidworks_mcp.server_extra"),
        ]
        synthetic = [{"ProcessId": pid, "Name": name, "CommandLine": command,
                      "CreationDate": "2026-01-01T00:00:00Z"}
                     for pid, name, command in cases]
        literal = json.dumps(synthetic).replace("'", "''")
        shim = ("$script:queueSyntheticProcesses = ConvertFrom-Json '" + literal + "'\n"
                "function Get-CimInstance { param([string]$ClassName) "
                "$script:queueSyntheticProcesses }\n")
        real_run = subprocess.run

        def synthetic_run(command, **kwargs):
            self.assertEqual(command[-2], "-Command")
            return real_run([*command[:-1], shim + command[-1]], **kwargs)

        with patch.object(guards.subprocess, "run", side_effect=synthetic_run):
            inventory = guards.process_inventory_raw()
        self.assertEqual([item["pid"] for item in inventory], list(range(101, 113)))
        by_pid = {item["pid"]: item for item in inventory}
        for pid in range(101, 110):
            self.assertIsNone(by_pid[pid]["script"])
        self.assertEqual([item["pid"] for item in guards.legacy_processes(inventory)], list(range(101, 111)))
        self.assertNotIn("CommandLine", json.dumps(inventory))

    def test_native_activation_rechecks_legacy_and_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            activation = {"format": 1, "enabled": True, "source_root": str(guards.source_root()),
                          "expected_major": 33, "target_pid": 55, "target_created_at": 1000.1}
            guards.atomic_json(state / "activation.json", activation)
            good = {"pid": 55, "name": "SLDWORKS.exe", "created_at": 1000.1}
            with patch.object(guards, "process_inventory", return_value=[good]):
                self.assertEqual(guards.validate_activation(state), activation)
            with patch.object(guards, "process_inventory", return_value=[good,
                    {"pid": 99, "name": "python.exe", "script": None}]):
                with self.assertRaises(QueueError) as error:
                    guards.validate_activation(state)
                self.assertEqual(error.exception.code, "LEGACY_CLIENTS_PRESENT")
            with patch.object(guards, "process_inventory", return_value=[{**good, "created_at": 1001}]):
                with self.assertRaises(QueueError) as error:
                    guards.validate_activation(state)
                self.assertEqual(error.exception.code, "CAD_INSTANCE_CHANGED")

    def test_protocol_rejects_nonfinite_and_nonobject(self):
        for payload in (b'[]', b'{"value":NaN}', b'{"value":Infinity}'):
            with self.assertRaises((ValueError, QueueError)):
                decode(payload)
        for value in (float("nan"), float("inf"), -1, True):
            with self.assertRaises((ValueError, QueueError)):
                finite_seconds(value, 10)

    def test_recovery_proof_precedes_any_native_com_initialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = Broker(temporary, "native")
            broker.journal = Mock()
            broker.journal.get.return_value = {"id": "job", "state": "recovery_required",
                "execution_context": {"target_pid": 44, "target_created_at": 123.0},
                "result": None}
            broker._initialize_backend = Mock(side_effect=AssertionError("COM must not be touched"))
            with patch("solidworks_mcp.coordination.broker.process_is_same_alive", return_value=True):
                broker._recover({"expected_job_ids": ["job"], "operator": "test", "note": "test"})
            self.assertEqual(broker.last_error["code"], "PREVIOUS_CAD_OUTCOME_UNKNOWN")
            broker._initialize_backend.assert_not_called()
            broker.journal.acknowledge_recovery.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows DACL test")
    def test_private_directory_removes_preexisting_explicit_everyone_grants(self):
        import ntsecuritycon
        import win32security
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            root.mkdir()
            child = root / "endpoint.json"
            child.write_text("{}")
            everyone = win32security.ConvertStringSidToSid("S-1-1-0")
            acl = win32security.ACL()
            acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, everyone)
            for path in (root, child):
                win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                    win32security.DACL_SECURITY_INFORMATION, None, None, acl, None)
            guards.private_directory(root)
            for path in (root, child):
                descriptor = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                    win32security.DACL_SECURITY_INFORMATION)
                secured = descriptor.GetSecurityDescriptorDacl()
                sids = [win32security.ConvertSidToStringSid(secured.GetAce(i)[2])
                        for i in range(secured.GetAceCount())]
                self.assertNotIn("S-1-1-0", sids)
                self.assertEqual(len(sids), 2)


if __name__ == "__main__":
    unittest.main()
