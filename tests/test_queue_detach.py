"""Windows Job Object lifetime checks against owned synthetic brokers only.

Uses the installed MCP SDK's actual Job Object creation/assignment. Each test
retains its temporary evidence directory, including failure stderr and journal.
No native backend, activation, installed configuration or CAD is accessed.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp.coordination.client import QueueClient
from solidworks_mcp.coordination import launch
from solidworks_mcp.coordination.protocol import QueueError

ROOT = Path(__file__).resolve().parents[1]


class LaunchGuardTests(unittest.TestCase):
    def test_uncertain_lifetime_stops_before_importing_broker(self):
        fake_broker = Mock()
        with patch.object(launch, "require_independent_lifetime", side_effect=QueueError("UNCERTAIN", "test")), \
                patch.dict(sys.modules, {"solidworks_mcp.coordination.broker": fake_broker}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(launch.main(["--backend", "test"]), launch.EXIT_LIFETIME_UNVERIFIED)
        fake_broker.main.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows job API")
    def test_retained_or_unreadable_job_is_rejected(self):
        for failure in (True, OSError("unavailable")):
            with self.subTest(failure=type(failure).__name__), \
                    patch("win32job.IsProcessInJob", **({"side_effect": failure}
                        if isinstance(failure, Exception) else {"return_value": failure})):
                with self.assertRaises(QueueError):
                    launch.require_independent_lifetime()

    def test_creation_denial_has_no_attached_fallback(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"SOLIDWORKS_MCP_TEST_BACKEND": "1"}):
            client = QueueClient(Path(temporary) / "state", backend="test")
            with patch.object(client, "_endpoint", side_effect=QueueError("ABSENT", "test")), \
                    patch("solidworks_mcp.coordination.client.subprocess.Popen", side_effect=PermissionError("test")) as start:
                with self.assertRaises(QueueError) as stopped:
                    client.ensure_broker()
                self.assertEqual(stopped.exception.code, "BROKER_DETACH_FAILED")
                self.assertEqual(start.call_count, 1)
                if os.name == "nt":
                    self.assertTrue(start.call_args.kwargs["creationflags"] & 0x01000000)

    def test_direct_native_module_cannot_bypass_independent_lifetime_check(self):
        from solidworks_mcp.coordination import broker
        with patch.object(launch, "require_independent_lifetime", side_effect=QueueError("ATTACHED", "test")), \
                patch.object(broker, "Broker") as constructor, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(broker.main(["--backend", "native"]), 1)
        constructor.assert_not_called()


class WindowsDetachHarness(unittest.TestCase):
    def setUp(self):
        self.evidence = Path(tempfile.mkdtemp(prefix="swqueue-detach-evidence-"))
        self.environment = patch.dict(os.environ, {"SOLIDWORKS_MCP_TEST_BACKEND": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.clients = []
        self.addCleanup(self.stop_owned_brokers)

    def stop_owned_brokers(self):
        import win32api
        import win32event
        for client in self.clients:
            handle = None
            try:
                endpoint = client._endpoint()
                # Endpoint requires the same kernel creation token, and every
                # client here uses only this test's private synthetic state.
                handle = win32api.OpenProcess(0x00100000, False, endpoint["process"]["pid"])
                client.request("shutdown")
                self.assertEqual(win32event.WaitForSingleObject(handle, 10000), 0,
                                 "Owned synthetic broker did not shut down")
            except (QueueError, OSError):
                pass
            finally:
                if handle is not None:
                    handle.Close()

    def client(self, suffix):
        client = QueueClient(self.evidence / suffix, backend="test", auto_start=False)
        self.clients.append(client)
        return client

    async def sdk_frontend(self, state, *, allow_breakaway=False, autostart=True, desktop=False):
        import anyio
        import win32api
        import win32job
        from mcp.os.win32.utilities import create_windows_process
        log = (self.evidence / (state.name + "-frontend.log")).open("w", encoding="utf-8")
        try:
            process = await create_windows_process(sys.executable,
                [str(ROOT / "tests" / "queue_detach_helper.py"), str(state),
                 "desktop" if desktop else ("autostart" if autostart else "connect")], dict(os.environ), log, ROOT)
            self.assertIsNotNone(getattr(process, "_job_object", None), "SDK job assignment failed")
            data = b""
            with anyio.fail_after(15):
                while b"\n" not in data:
                    data += await process.stdout.receive(8192)
            ready = json.loads(data.split(b"\n", 1)[0])
            actual = win32api.OpenProcess(0x1000 | 0x00100000 | 0x100 | 0x1, False, ready["ready_pid"])
            process._actual_helper_handle = actual
            process._actual_helper_pid = ready["ready_pid"]
            if not win32job.IsProcessInJob(actual, process._job_object):
                win32job.AssignProcessToJobObject(process._job_object, actual)
            self.assertTrue(win32job.IsProcessInJob(actual, process._job_object),
                            "Actual helper must be in the exact SDK job before starting a broker")
            if allow_breakaway:
                job = process._job_object
                limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
                win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
            await process.stdin.send(b"begin\n")
            data = b""
            with anyio.fail_after(25):
                while b"\n" not in data:
                    data += await process.stdout.receive(8192)
            result = json.loads(data.split(b"\n", 1)[0])
            (self.evidence / (state.name + "-frontend-result.json")).write_text(
                json.dumps(result, indent=2), encoding="utf-8")
            return process, log, result
        except BaseException:
            if "process" in locals():
                await self.close_sdk_job(process)
            log.close()
            raise

    async def close_sdk_job(self, process):
        import anyio
        import win32api
        job = getattr(process, "_job_object", None)
        if job is not None:
            # Exercise exactly KILL_ON_JOB_CLOSE, only on our disposable SDK
            # frontend. A detached synthetic broker must survive this close.
            win32api.CloseHandle(job)
            process._job_object = None
        with anyio.fail_after(10):
            await process.wait()
        actual = getattr(process, "_actual_helper_handle", None)
        if actual is not None:
            import win32event
            self.assertEqual(win32event.WaitForSingleObject(actual, 5000), 0,
                             "Actual SDK frontend generation survived closing its own job")
            actual.Close()
            process._actual_helper_handle = None
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                await stream.aclose()


@unittest.skipUnless(os.name == "nt", "Actual Windows SDK Job Object integration")
class WindowsDetachIntegrationTests(WindowsDetachHarness):
    def test_sdk_restrictive_job_refuses_autostart_without_journal(self):
        import anyio
        client = self.client("restricted")
        async def scenario():
            process, log, result = await self.sdk_frontend(client.state_dir)
            try:
                self.assertEqual(result, {"status": "refused", "code": "BROKER_DETACH_FAILED"})
                self.assertFalse((client.state_dir / "queue.sqlite3").exists())
                self.assertFalse((client.state_dir / "endpoint.json").exists())
            finally:
                await self.close_sdk_job(process)
                log.close()
        anyio.run(scenario)

    def test_external_broker_survives_sdk_frontend_job_close(self):
        import anyio
        client = self.client("external")
        # Existing synthetic test backend deliberately permits test-host jobs.
        # This broker is external to the SDK frontend's job, while this test
        # makes no claim that it escaped the outer test runner's own job.
        diagnostics = (self.evidence / "external-broker.log").open("ab")
        process = subprocess.Popen([sys.executable, "-m", "solidworks_mcp.coordination.broker",
            "--backend", "test", "--state-dir", str(client.state_dir)], cwd=ROOT,
            env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=diagnostics, creationflags=0x08000000)
        self.addCleanup(diagnostics.close)
        # Added after the normal stop cleanup, so register a specific stop/wait
        # here to release the exact owned process before closing its log.
        def stop_external():
            with contextlib.suppress(QueueError, OSError):
                client.request("shutdown")
            process.wait(timeout=10)
        self.addCleanup(stop_external)
        deadline = time.monotonic() + 15
        while True:
            try:
                before = client.ensure_broker()
                break
            except QueueError:
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail("Owned synthetic external broker failed to start")
                time.sleep(.05)
        async def scenario():
            process, log, result = await self.sdk_frontend(client.state_dir, autostart=False)
            try:
                self.assertEqual(result["status"], "connected")
                self.assertEqual(result["process"], before["process"])
            finally:
                await self.close_sdk_job(process)
                log.close()
        anyio.run(scenario)
        self.assertEqual(client._endpoint()["process"], before["process"])
        self.assertEqual(client.request("status")["owner_id"], before["owner_id"])

    def test_permitted_breakaway_broker_survives_sdk_frontend_job_close(self):
        import anyio
        client = self.client("permitted")
        async def scenario():
            process, log, result = await self.sdk_frontend(client.state_dir, allow_breakaway=True)
            try:
                if result == {"status": "refused", "code": "BROKER_DETACH_FAILED"}:
                    diagnostic = (client.state_dir / "broker-start.log").read_text()
                    self.assertIn("BROKER_PARENT_JOB_RETAINED", diagnostic)
                    self.assertFalse((client.state_dir / "queue.sqlite3").exists())
                    self.skipTest("Outer test-host job retains the child; positive full breakaway requires an independent operator runner")
                self.assertEqual(result["status"], "connected")
                self.assertEqual(result["backend"], "test")
            finally:
                await self.close_sdk_job(process)
                log.close()
            self.assertEqual(client._endpoint()["process"], result["process"])
            self.assertEqual(client.request("status")["owner_id"], result["owner_id"])
        anyio.run(scenario)


if __name__ == "__main__":
    unittest.main()
