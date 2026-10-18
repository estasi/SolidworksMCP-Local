"""Synthetic classifier/kernel reconciliation; no SolidWorks or COM calls."""
import ctypes
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp.coordination import platform_guard as guards
from solidworks_mcp.coordination import process_scan as scan
from solidworks_mcp.coordination.protocol import QueueError


def row(pid=10, **changes):
    stamp = changes.get("created_at", 1000.123)
    token = (str(116444736000000000 + round(stamp * 1000) * 10000)
             if type(stamp) in (int, float) and 0 < stamp < 1e11 else None)
    return {"pid": pid, "name": "python.exe", "created_at": 1000.123,
            "creation_cim_token": token, "script": None, **changes}


def observation(status="alive", token="116444746001230000"):
    return {"status": status, "creation_token": token}


class CoherentInventoryTests(unittest.TestCase):
    def test_live_unknown_and_legacy_remain_blockers(self):
        legacy = str(Path("C:/synthetic-old") / "solidworks_mcp_server.py")
        allowed = str(guards.source_root() / "solidworks_mcp_server.py")
        raw = Mock(return_value=[row(), row(11, script=legacy), row(12, script=allowed)])
        inspected = Mock(return_value=observation())
        result = scan.coherent_inventory(raw, inspected)
        self.assertEqual([item["pid"] for item in guards.legacy_processes(result)], [10, 11])
        self.assertEqual(raw.call_count, 1)
        self.assertEqual(inspected.call_count, 3)

    def test_exited_first_snapshot_requires_complete_rescan(self):
        raw = Mock(side_effect=[[row()], []])
        inspected = Mock(return_value=observation("exited"))
        self.assertEqual(scan.coherent_inventory(raw, inspected), [])
        self.assertEqual(raw.call_count, 2)
        self.assertEqual(inspected.call_count, 1)

    def test_exited_then_new_live_unknown_is_not_silently_dropped(self):
        raw = Mock(side_effect=[[row()], [row(created_at=1001.123)]])
        inspected = Mock(side_effect=[observation("exited"), observation("alive", "new")])
        result = scan.coherent_inventory(raw, inspected)
        self.assertEqual([item["pid"] for item in guards.legacy_processes(result)], [10])
        self.assertEqual(result[0]["created_at"], 1001.123)

    def test_final_exited_row_is_independently_proven_before_exclusion(self):
        raw = Mock(return_value=[row()])
        inspected = Mock(return_value=observation("exited"))
        self.assertEqual(scan.coherent_inventory(raw, inspected), [])
        self.assertEqual(raw.call_count, 2)
        self.assertEqual(inspected.call_count, 2)

    def test_missing_pid_is_not_exit_proof_but_fresh_empty_snapshot_is_coherent(self):
        raw = Mock(side_effect=[[row()], []])
        self.assertEqual(scan.coherent_inventory(raw, Mock(return_value=observation("missing"))), [])
        self.assertEqual(raw.call_count, 2)

    def test_reused_pid_refreshes_classification_and_keeps_new_live_unknown(self):
        raw = Mock(side_effect=[[row()], [row(created_at=1001.123)]])
        inspected = Mock(side_effect=[observation("reused"), observation()])
        self.assertEqual(len(guards.legacy_processes(scan.coherent_inventory(raw, inspected))), 1)

    def test_missing_or_reused_pid_on_second_scan_fails_closed(self):
        for status in ("missing", "reused"):
            with self.subTest(status=status):
                raw = Mock(return_value=[row()])
                inspected = Mock(return_value=observation(status))
                with self.assertRaises(QueueError) as raised:
                    scan.coherent_inventory(raw, inspected)
                self.assertEqual(raised.exception.code, "PROCESS_SCAN_FAILED")
                self.assertEqual(raw.call_count, 2)
                self.assertEqual(raised.exception.process_diagnostics[-1]["kernel_status"], status)

    def test_observation_error_is_not_retried_or_ignored(self):
        raw = Mock(return_value=[row()])
        error = scan.scan_error("Denied", [scan.safe_row(row(), "open_failed")])
        with self.assertRaises(QueueError) as raised:
            scan.coherent_inventory(raw, Mock(side_effect=error))
        self.assertIs(raised.exception, error)
        self.assertEqual(raw.call_count, 1)

    def test_nonfinite_malformed_and_nonmillisecond_timestamps_fail_closed(self):
        for stamp in (float("nan"), float("inf"), "1000.123", None, True, -1, 0,
                      10**400, 1000.123456):
            with self.subTest(stamp=stamp):
                inspected = Mock()
                with self.assertRaises(QueueError):
                    scan.coherent_inventory(lambda: [row(created_at=stamp)], inspected)
                inspected.assert_not_called()

    def test_malformed_pid_script_and_duplicate_pid_fail_closed(self):
        for rows in ([row(pid=True)], [row(pid=-1)], [row(pid=2**32)],
                     [row(script=[])], [row(script="x" * 4097)], [row(), row()]):
            with self.subTest(rows=str(rows)[:80]):
                with self.assertRaises(QueueError):
                    scan.coherent_inventory(lambda: rows, Mock(return_value=observation()))

    def test_missing_invalid_or_inconsistent_cim_token_fails_closed(self):
        for token in (None, "", 123, "not-a-token", "116444746001230001",
                      "116444746101230000", "1" * 100):
            with self.subTest(token=token):
                with self.assertRaises(QueueError):
                    scan.coherent_inventory(lambda: [row(creation_cim_token=token)], Mock())

    def test_diagnostic_projection_never_includes_command_line_or_unbounded_values(self):
        original = row(CommandLine="private unrelated argument", name="x" * 129,
                       script="x" * 4097, created_at=float("nan"))
        safe = scan.safe_row(original, "invalid_row")
        self.assertEqual(set(safe), {"pid", "name", "script", "created_at", "kernel_status"})
        self.assertIsNone(safe["name"])
        self.assertIsNone(safe["script"])
        self.assertIsNone(safe["created_at"])

    def test_raw_classifier_does_not_query_kernel(self):
        with patch.object(guards, "process_inventory_raw", return_value=[row()]) as raw:
            with patch.object(guards, "coherent_inventory", return_value=[]) as reconcile:
                self.assertEqual(guards.process_inventory(), [])
                reconcile.assert_called_once_with(raw)

    def test_fresh_scan_failure_retains_first_scan_exit_evidence(self):
        raw = Mock(side_effect=[[row()], QueueError("PROCESS_SCAN_FAILED", "unavailable")])
        with self.assertRaises(QueueError) as raised:
            scan.coherent_inventory(raw, Mock(return_value=observation("exited")))
        self.assertEqual(raised.exception.process_diagnostics[0]["kernel_status"], "exited")


class GuardEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        # Permission enforcement itself is covered by the platform DACL test;
        # these tests isolate retention/order/failure behavior using private tmp.
        def private_folder(path):
            Path(path).mkdir(exist_ok=True)
            return Path(path)
        self.addCleanup(patch.stopall)
        patch.object(guards, "private_directory", side_effect=private_folder).start()

    def test_unique_events_are_retained_with_bounded_count(self):
        error = scan.scan_error("not retained", [scan.safe_row(row(), "open_failed")])
        names = []
        with patch.object(guards.time, "time_ns", return_value=123):
            for _ in range(20):
                names.append(guards.record_guard_failure(self.state, error))
        self.assertEqual(len(set(names)), 20)
        files = list((self.state / "guard-events").glob("guard-event-*.json"))
        self.assertEqual(len(files), 16)
        self.assertTrue(names[-1].is_file())

    def test_record_size_fields_and_row_count_are_bounded(self):
        error = QueueError("PROCESS_SCAN_FAILED", "private error message")
        rows = [row(pid=10 + index, script="\U0001f512" * 4096,
                    CommandLine="private argument", environment="private env",
                    kernel_status="alive") for index in range(100)]
        path = guards.record_guard_failure(self.state, error, rows)
        raw = path.read_bytes()
        event = json.loads(raw)
        self.assertLessEqual(len(raw), 65536)
        self.assertLessEqual(len(event["processes"]), 12)
        self.assertEqual(event["omitted_processes"] + len(event["processes"]), 100)
        self.assertNotIn(b"private", raw)
        for item in event["processes"]:
            self.assertEqual(set(item), {"pid", "name", "created_at", "script", "kernel_status"})

    def test_primary_current_event_is_committed_before_failed_pruning(self):
        error = QueueError("PROCESS_SCAN_FAILED", "not retained")
        for _ in range(16):
            guards.record_guard_failure(self.state, error, [])
        with patch.object(Path, "unlink", side_effect=PermissionError("synthetic prune denied")):
            with self.assertRaises(PermissionError):
                guards.record_guard_failure(self.state, error, [row()])
        files = list((self.state / "guard-events").glob("guard-event-*.json"))
        self.assertEqual(len(files), 17)
        self.assertEqual(sum(bool(json.loads(path.read_text())["processes"]) for path in files), 1)

    def test_diagnostic_write_failure_cannot_authorize_activation(self):
        activation = {"format": 1, "enabled": True, "source_root": str(guards.source_root()),
                      "expected_major": 33, "target_pid": 99, "target_created_at": 1000.123}
        guards.atomic_json(self.state / "activation.json", activation)
        with patch.object(guards, "process_inventory", return_value=[row()]):
            with patch.object(guards, "atomic_json", side_effect=OSError("synthetic write denied")):
                with self.assertRaises(OSError):
                    guards.validate_activation(self.state)

    def test_activation_records_primary_kernel_failure_without_sensitive_message(self):
        activation = {"format": 1, "enabled": True, "source_root": str(guards.source_root()),
                      "expected_major": 33, "target_pid": 99, "target_created_at": 1000.123}
        guards.atomic_json(self.state / "activation.json", activation)
        error = scan.scan_error("private message", [scan.safe_row(row(), "times_failed")])
        with patch.object(guards, "process_inventory", side_effect=error):
            with self.assertRaises(QueueError) as raised:
                guards.validate_activation(self.state)
        self.assertIs(raised.exception, error)
        files = list((self.state / "guard-events").glob("guard-event-*.json"))
        self.assertEqual(len(files), 1)
        event = json.loads(files[0].read_text())
        self.assertEqual(event["processes"][0]["kernel_status"], "times_failed")
        self.assertNotIn("private message", files[0].read_text())

    def test_failure_row_is_retained_ahead_of_more_than_twelve_healthy_rows(self):
        rows = [scan.safe_row(row(pid=10 + index), "alive") for index in range(16)]
        rows.append(scan.safe_row(row(pid=99), "open_failed"))
        error = scan.scan_error("unavailable", rows)
        path = guards.record_guard_failure(self.state, error)
        event = json.loads(path.read_text())
        self.assertEqual(event["processes"][0]["pid"], 99)
        self.assertEqual(event["processes"][0]["kernel_status"], "open_failed")

    def test_terminal_failure_precedes_many_initial_rescan_observations(self):
        rows = [scan.safe_row(row(pid=10 + index), "reused") for index in range(16)]
        rows.append(scan.safe_row(row(pid=99), "times_failed"))
        error = scan.scan_error("unavailable", rows)
        path = guards.record_guard_failure(self.state, error)
        event = json.loads(path.read_text())
        self.assertEqual(event["processes"][0]["pid"], 99)
        self.assertEqual(event["processes"][0]["kernel_status"], "times_failed")

    def test_nonowned_names_survive_pruning(self):
        error = QueueError("PROCESS_SCAN_FAILED", "test")
        folder = self.state / "guard-events"
        folder.mkdir()
        preserved = folder / "guard-event-manual.json"
        preserved.write_text("manual evidence")
        for _ in range(18):
            guards.record_guard_failure(self.state, error)
        self.assertEqual(preserved.read_text(), "manual evidence")

    def test_concurrent_removal_of_prune_victim_is_benign(self):
        error = QueueError("PROCESS_SCAN_FAILED", "test")
        for _ in range(16):
            guards.record_guard_failure(self.state, error)
        original = Path.unlink
        def concurrent_removal(path):
            original(path)
            raise FileNotFoundError("synthetic concurrent removal")
        with patch.object(Path, "unlink", side_effect=concurrent_removal, autospec=True):
            newest = guards.record_guard_failure(self.state, error)
        self.assertTrue(newest.is_file())
        self.assertEqual(len(list((self.state / "guard-events").glob("guard-event-*.json"))), 16)


class FakeKernel:
    def __init__(self, *, token=116444746001230000, wait=0x102,
                 open_error=None, times_ok=True, close_ok=True):
        self.token, self.wait = token, wait
        self.open_error, self.times_ok, self.close_ok = open_error, times_ok, close_ok
        self.closed, self.open_args, self.wait_args = [], [], []

    def OpenProcess(self, *args):
        self.open_args.append(args)
        if self.open_error is not None:
            ctypes.set_last_error(self.open_error)
            return None
        return 1234

    def GetProcessTimes(self, handle, creation, *unused):
        if not self.times_ok:
            return False
        from ctypes import wintypes
        value = ctypes.cast(creation, ctypes.POINTER(wintypes.FILETIME)).contents
        value.dwHighDateTime = self.token >> 32
        value.dwLowDateTime = self.token & 0xFFFFFFFF
        return True

    def WaitForSingleObject(self, *args):
        self.wait_args.append(args)
        return self.wait

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return self.close_ok


@unittest.skipUnless(os.name == "nt", "Windows process kernel API")
class KernelProbeTests(unittest.TestCase):
    def test_live_same_generation_is_alive_and_handle_is_closed(self):
        kernel = FakeKernel()
        self.assertEqual(scan.inspect_process(row(), kernel)["status"], "alive")
        self.assertEqual(kernel.open_args, [(0x101000, False, 10)])
        self.assertEqual(kernel.wait_args, [(1234, 0)])
        self.assertEqual(kernel.closed, [1234])

    def test_signalled_same_generation_is_positive_exit_proof(self):
        kernel = FakeKernel(wait=0)
        observed = scan.inspect_process(row(), kernel)
        self.assertEqual(observed, observation("exited"))
        self.assertEqual(kernel.closed, [1234])

    def test_pid_reuse_never_counts_as_exit_even_if_replacement_is_signalled(self):
        kernel = FakeKernel(token=116444746101230000, wait=0)
        self.assertEqual(scan.inspect_process(row(), kernel)["status"], "reused")
        self.assertEqual(kernel.wait_args, [])
        self.assertEqual(kernel.closed, [1234])

    def test_different_microsecond_within_same_millisecond_is_reused(self):
        kernel = FakeKernel(token=116444746001230010, wait=0)
        self.assertEqual(scan.inspect_process(row(), kernel)["status"], "reused")
        self.assertEqual(kernel.closed, [1234])

    def test_only_submicrosecond_kernel_digit_is_rounded_to_cim_precision(self):
        kernel = FakeKernel(token=116444746001230009)
        self.assertEqual(scan.inspect_process(row(), kernel)["status"], "alive")

    def test_absent_pid_requires_rescan(self):
        kernel = FakeKernel(open_error=87)
        self.assertEqual(scan.inspect_process(row(), kernel)["status"], "missing")
        self.assertEqual(kernel.closed, [])

    def test_access_denied_and_other_open_failure_fail_closed(self):
        for error in (5, 0, 6):
            with self.subTest(error=error):
                kernel = FakeKernel(open_error=error)
                with self.assertRaises(QueueError) as raised:
                    scan.inspect_process(row(), kernel)
                self.assertEqual(raised.exception.process_diagnostics[0]["kernel_status"], "open_failed")
                self.assertEqual(kernel.closed, [])

    def test_time_failure_and_wait_failure_release_handle_and_fail_closed(self):
        for kernel, status in ((FakeKernel(times_ok=False), "times_failed"),
                               (FakeKernel(wait=0xFFFFFFFF), "wait_failed"),
                               (FakeKernel(wait=0x80), "wait_failed"),
                               (FakeKernel(token=0), "invalid_kernel_timestamp")):
            with self.subTest(status=status):
                with self.assertRaises(QueueError) as raised:
                    scan.inspect_process(row(), kernel)
                self.assertEqual(raised.exception.process_diagnostics[0]["kernel_status"], status)
                self.assertEqual(kernel.closed, [1234])

    def test_handle_release_failure_is_not_success(self):
        kernel = FakeKernel(close_ok=False)
        with self.assertRaises(QueueError) as raised:
            scan.inspect_process(row(), kernel)
        self.assertEqual(raised.exception.process_diagnostics[0]["kernel_status"], "close_failed")


if __name__ == "__main__":
    unittest.main()
