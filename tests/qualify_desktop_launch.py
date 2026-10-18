"""Explicit Windows desktop/SDK synthetic qualification; never access CAD.

Run separately from guarded unit tests: this deliberately uses existing Explorer
Shell COM and the installed SDK's real KILL_ON_JOB_CLOSE Job Object.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_queue_detach import WindowsDetachHarness
from solidworks_mcp.coordination.desktop_launch import physical_identity
from solidworks_mcp.coordination.journal import Journal
from solidworks_mcp.coordination.ownership import process_identity


class DesktopLifetimeQualification(WindowsDetachHarness):
    def test_real_desktop_broker_preserves_journal_and_survives_exact_sdk_job(self):
        import anyio
        import win32api
        import win32job
        client = self.client("desktop")
        client.state_dir.mkdir()
        journal = Journal(client.state_dir / "queue.sqlite3")
        cancelled = journal.enqueue("synthetic_operation", {}, "prelaunch-history", "desktop-test")
        journal.cancel(cancelled["id"])
        history_before = {"jobs": journal.list_jobs(), "events": journal.events()}
        before = physical_identity(client.state_dir / "queue.sqlite3")
        directory_before = physical_identity(client.state_dir)
        evidence = {"journal_before": before, "directory_before": directory_before}

        async def scenario():
            process, log, result = await self.sdk_frontend(client.state_dir, desktop=True)
            broker_handle = None
            try:
                self.assertEqual(result["status"], "connected")
                endpoint = client._endpoint()
                self.assertEqual(endpoint["launch"]["mode"], "desktop")
                self.assertEqual(endpoint["launch"]["journal_before"], before)
                self.assertEqual(endpoint["launch"]["state"], directory_before)
                helper = subprocess.run([endpoint["launch"]["helper_executable"], "-I", "-c",
                    "import pythoncom,win32gui,win32process; from PIL import ImageGrab; print('DEPENDENCIES_OK')"],
                    cwd=ROOT, capture_output=True, text=True, timeout=15, creationflags=0x08000000)
                (self.evidence / "helper-import-stdout.txt").write_text(helper.stdout, encoding="utf-8")
                (self.evidence / "helper-import-stderr.txt").write_text(helper.stderr, encoding="utf-8")
                self.assertEqual(helper.returncode, 0, "Validated helper interpreter cannot import runtime dependencies")
                self.assertEqual(helper.stdout.strip(), "DEPENDENCIES_OK")
                broker_handle = win32api.OpenProcess(0x1000, False, endpoint["process"]["pid"])
                self.assertTrue(win32job.IsProcessInJob(process._actual_helper_handle, process._job_object))
                self.assertFalse(win32job.IsProcessInJob(broker_handle, process._job_object))
                evidence.update({"actual_frontend_pid": process._actual_helper_pid,
                    "frontend_in_exact_sdk_job": True, "broker_in_exact_sdk_job": False,
                    "broker_process": endpoint["process"], "version": endpoint["version"],
                    "launch": endpoint["launch"], "owner_id": endpoint["owner_id"]})
                evidence["helper_pywin32_pillow_import"] = "PASS"
            finally:
                if broker_handle is not None:
                    broker_handle.Close()
                await self.close_sdk_job(process)
                log.close()
            self.assertEqual(client._endpoint()["process"], endpoint["process"])
            self.assertEqual(client.request("status")["owner_id"], endpoint["owner_id"])
            self.assertEqual(process_identity(endpoint["process"]["pid"]), endpoint["process"])
            self.assertEqual(physical_identity(client.state_dir / "queue.sqlite3"), before)
            self.assertEqual({"jobs": journal.list_jobs(), "events": journal.events()}, history_before)
            evidence.update({"actual_frontend_exited": True,
                "broker_survived_frontend_job_close": True, "journal_identity_preserved": True,
                "existing_journal_rows_unchanged": True})
        try:
            anyio.run(scenario)
            evidence["status"] = "PASS"
        except BaseException as exc:
            evidence.update({"status": "FAIL", "error_type": type(exc).__name__})
            raise
        finally:
            (self.evidence / "desktop-qualification.json").write_text(
                json.dumps(evidence, indent=2), encoding="utf-8")
            print("Desktop synthetic evidence: " + str(self.evidence), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] != ["--synthetic-only"]:
        raise SystemExit("Explicit --synthetic-only required; no native backend is permitted")
    os.environ["SOLIDWORKS_MCP_TEST_BACKEND"] = "1"
    unittest.main(argv=[sys.argv[0]], verbosity=2)
