"""End-to-end queue tests against owned synthetic broker subprocesses only.

No native backend, SOLIDWORKS instance, user model or active MCP is contacted.
Every broker uses a unique temporary state directory and explicit test opt-in.
"""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp.coordination.client import QueueClient
from solidworks_mcp.coordination.protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, QueueError, decode, encode, unwrap_result


def _independent_client(state_dir, index, barrier, output):
    os.environ["SOLIDWORKS_MCP_TEST_BACKEND"] = "1"
    client = QueueClient(state_dir, backend="test", auto_start=False)
    barrier.wait(15)
    jobs = [client.submit("synthetic_operation", {
        "label": f"client-{index}-operation-{i}", "increment": index + 1,
        "delay_before": .025, "delay_after": .025,
    }, request_id=f"client-{index}-request-{i}") for i in range(4)]
    completed = [client.wait(job["id"], timeout=15) for job in jobs]
    output.put({"pid": os.getpid(), "jobs": completed})


class BrokerProcessTests(unittest.TestCase):
    def setUp(self):
        self.previous_test_env = os.environ.get("SOLIDWORKS_MCP_TEST_BACKEND")
        os.environ["SOLIDWORKS_MCP_TEST_BACKEND"] = "1"
        self.tmp = tempfile.TemporaryDirectory(prefix="solidworks-queue-test-")
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.processes = []
        self.diagnostics = []
        self.client = QueueClient(self.state, backend="test", auto_start=False)
        self.addCleanup(self.cleanup_owned_resources)
        self.broker = self.start_broker()

    def cleanup_owned_resources(self):
        try:
            try:
                self.client.request("shutdown")
            except (OSError, QueueError):
                pass
            for process in self.processes:
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    # These exact Popen objects were created by this test with
                    # the synthetic backend; no unrelated process is touched.
                    process.terminate()
                    process.wait(timeout=5)
            for stream in self.diagnostics:
                stream.close()
            self.tmp.cleanup()
        finally:
            if self.previous_test_env is None:
                os.environ.pop("SOLIDWORKS_MCP_TEST_BACKEND", None)
            else:
                os.environ["SOLIDWORKS_MCP_TEST_BACKEND"] = self.previous_test_env

    def spawn_broker(self):
        diagnostics = (Path(self.tmp.name) / f"broker-{len(self.processes)}.log").open("ab")
        self.diagnostics.append(diagnostics)
        process = subprocess.Popen(
            [sys.executable, "-m", "solidworks_mcp.coordination.broker",
             "--backend", "test", "--state-dir", str(self.state)],
            cwd=ROOT, env={**os.environ, "SOLIDWORKS_MCP_TEST_BACKEND": "1"},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=diagnostics,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        self.processes.append(process)
        return process

    def start_broker(self):
        process = self.spawn_broker()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"Synthetic broker startup failed with exit code {process.returncode}")
            try:
                endpoint = self.client._endpoint()
                self.client._rpc(endpoint, "status")
                self.broker_pid = endpoint["process"]["pid"]
                return process
            except (OSError, QueueError):
                pass
            time.sleep(.05)
        self.fail("Synthetic broker readiness deadline exceeded")

    def eventually(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.025)
        self.fail("Expected queue state did not occur before test deadline")

    def get(self, job):
        return self.client.request("get", {"job_id": job["id"]})

    def model_value(self):
        path = self.state / "synthetic-model.json"
        return json.loads(path.read_text())["value"] if path.exists() else 0

    def submit(self, label, **arguments):
        return self.client.submit("synthetic_operation", {"label": label, **arguments}, request_id=label)

    def reconcile(self, job):
        accepted = self.client.request("recover", {"operator": "test-operator",
            "note": "Synthetic state inspected; backend has no external CAD process",
            "expected_job_ids": [job["id"]]})
        self.assertTrue(accepted["recovery_check_requested"])
        self.assertFalse(accepted["gate_released"])
        self.eventually(lambda: not self.client.request("status")["recovery_required"])

    def test_two_independent_clients_serialize_full_operations_fifo(self):
        ctx = multiprocessing.get_context("spawn")
        barrier, output = ctx.Barrier(2), ctx.Queue()
        children = [ctx.Process(target=_independent_client,
            args=(str(self.state), index, barrier, output)) for index in range(2)]
        try:
            for child in children:
                child.start()
            results = [output.get(timeout=25) for _ in children]
            for child in children:
                child.join(10)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(len({result["pid"] for result in results}), 2)
            jobs = sorted([job for result in results for job in result["jobs"]], key=lambda job: job["seq"])
            self.assertEqual(len(jobs), 8)
            self.assertTrue(all(job["state"] == "completed" for job in jobs))
            self.assertEqual(self.model_value(), 12)
            events = [json.loads(line) for line in (self.state / "synthetic-timeline.jsonl").read_text().splitlines()]
            self.assertEqual([event["kind"] for event in events], ["start", "end"] * 8)
            expected_labels = [job["request_id"].replace("request", "operation") for job in jobs]
            self.assertEqual([event["label"] for event in events if event["kind"] == "start"], expected_labels)
            self.assertEqual({event["pid"] for event in events}, {self.broker_pid})
            self.assertTrue(all(events[i]["at"] <= events[i + 1]["at"] for i in range(len(events) - 1)))
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                child.join(5)
            output.close()

    def test_async_submit_poll_and_control_status_responsive_during_execution(self):
        started = time.monotonic()
        job = self.submit("asynchronous", delay_before=.65, increment=3)
        self.assertLess(time.monotonic() - started, .5)
        self.eventually(lambda: self.get(job)["state"] == "running")
        started = time.monotonic()
        status = self.client.request("status")
        self.assertTrue(status["executor_busy"])
        self.assertLess(time.monotonic() - started, .4)
        response = unwrap_result(self.client.call_tool("get_job_result", {"job_id": job["id"], "timeout": 0}))
        self.assertEqual(response["data"]["state"], "running")
        self.assertEqual(self.client.wait(job["id"], timeout=5)["state"], "completed")
        self.assertEqual(self.model_value(), 3)

    def test_lost_admission_response_same_request_never_replays(self):
        endpoint = self.client._endpoint()
        payload = {"tool": "synthetic_operation", "arguments": {"label": "lost-response", "increment": 5},
            "request_id": "lost-response", "client_id": self.client.client_id,
            "queue_timeout_sec": 3600, "execution_timeout_sec": 300}
        with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=3) as connection:
            connection.sendall(encode({"protocol": PROTOCOL_VERSION, "token": endpoint["token"],
                "action": "submit", "payload": payload}))
            connection.shutdown(socket.SHUT_WR)
            # Deliberately discard the admission response.
        self.eventually(lambda: self.client.request("status")["total_jobs"] == 1)
        job = self.client.submit(payload["tool"], payload["arguments"], request_id=payload["request_id"])
        self.assertEqual(self.client.wait(job["id"], timeout=5)["state"], "completed")
        same = self.client.submit(payload["tool"], payload["arguments"], request_id=payload["request_id"])
        self.assertEqual(same["id"], job["id"])
        reconnected = QueueClient(self.state, backend="test", auto_start=False)
        same_again = reconnected.submit(payload["tool"], payload["arguments"], request_id=payload["request_id"])
        self.assertEqual(same_again["id"], job["id"])
        self.assertEqual(self.model_value(), 5)
        self.assertEqual(self.client.request("status")["total_jobs"], 1)

    def crash_scenario(self, after):
        kind = "crash_after" if after else "crash_before"
        job = self.submit(kind, delay_before=.2, increment=7, **{kind: True})
        following = self.submit("after-restart", increment=2)
        self.broker.wait(timeout=8)
        self.assertEqual(self.broker.returncode, 73 if after else 72)
        self.assertEqual(self.model_value(), 7 if after else 0)
        self.broker = self.start_broker()
        status = self.client.request("status")
        self.assertEqual(status["gate"], "recovery_required")
        self.assertEqual(status["recovery_job_ids"], [job["id"]])
        time.sleep(.15)
        self.assertEqual(self.get(following)["state"], "queued")
        self.assertEqual(self.model_value(), 7 if after else 0)
        self.reconcile(job)
        self.assertEqual(self.client.wait(following["id"], timeout=5)["state"], "completed")
        self.assertEqual(self.get(job)["state"], "failed")
        self.assertEqual(self.model_value(), 9 if after else 2)
        self.assertEqual(self.client.request("status")["total_jobs"], 2)

    def test_broker_crash_before_mutation_restart_requires_recovery(self):
        self.crash_scenario(after=False)

    def test_broker_crash_after_mutation_restart_never_replays_mutation(self):
        self.crash_scenario(after=True)

    def test_timeout_gate_remains_after_late_result_and_recovery_waits_for_idle(self):
        job = self.client.submit("synthetic_operation", {"label": "late", "increment": 4, "delay_after": .65},
            request_id="late", execution_timeout_sec=.12)
        following = self.submit("after-timeout", increment=2)
        self.eventually(lambda: self.get(job)["state"] == "recovery_required")
        self.assertTrue(self.client.request("status")["executor_busy"])
        with self.assertRaises(QueueError) as raised:
            self.client.request("recover", {"operator": "test", "note": "too early", "expected_job_ids": [job["id"]]})
        self.assertEqual(raised.exception.code, "EXECUTOR_BUSY")
        self.eventually(lambda: not self.client.request("status")["executor_busy"])
        self.assertEqual(self.get(job)["state"], "recovery_required")
        self.assertIsNotNone(self.get(job)["result"])
        self.assertEqual(self.get(following)["state"], "queued")
        self.assertEqual(self.model_value(), 4)
        self.reconcile(job)
        self.assertEqual(self.client.wait(following["id"], timeout=5)["state"], "completed")
        self.assertEqual(self.model_value(), 6)

    def test_cancelled_job_never_executes_and_verified_rollback_continues(self):
        blocker = self.submit("blocker", delay_before=.35, increment=2)
        self.eventually(lambda: self.get(blocker)["state"] == "running")
        cancelled = self.submit("cancelled", increment=100)
        self.assertEqual(self.client.request("cancel", {"job_id": cancelled["id"]})["state"], "cancelled")
        rollback = self.submit("rolled-back", increment=100, fail=True)
        after = self.submit("after-rollback", increment=3)
        self.assertEqual(self.client.wait(after["id"], timeout=5)["state"], "completed")
        self.assertEqual(self.get(rollback)["state"], "failed")
        self.assertEqual(self.get(cancelled)["state"], "cancelled")
        self.assertEqual(self.model_value(), 5)
        labels = [json.loads(line)["label"] for line in (self.state / "synthetic-timeline.jsonl").read_text().splitlines()]
        self.assertNotIn("cancelled", labels)
        self.assertFalse(self.client.request("status")["recovery_required"])

    def test_shutdown_finishes_active_operation_and_preserves_waiting_queue(self):
        active = self.submit("draining-active", delay_before=.4, increment=2)
        self.eventually(lambda: self.get(active)["state"] == "running")
        pending = self.submit("draining-pending", increment=3)
        drained = self.client.request("shutdown")
        self.assertTrue(drained["draining"])
        self.broker.wait(timeout=8)
        self.assertEqual(self.broker.returncode, 0)
        self.assertEqual(self.model_value(), 2)
        self.broker = self.start_broker()
        self.assertEqual(self.get(active)["state"], "completed")
        self.assertEqual(self.client.wait(pending["id"], timeout=5)["state"], "completed")
        self.assertEqual(self.model_value(), 5)
        self.assertFalse(self.client.request("status")["recovery_required"])

    def test_bad_token_oversized_requests_and_duplicate_broker_are_rejected(self):
        endpoint = self.client._endpoint()
        with self.assertRaises(QueueError) as raised:
            self.client._rpc({**endpoint, "token": "invalid-synthetic-token"}, "status")
        self.assertEqual(raised.exception.code, "UNAUTHORIZED")
        with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=5) as connection:
            oversized = b" " * (MAX_MESSAGE_BYTES + 1) + b"\n"
            connection.sendall(oversized)
            with connection.makefile("rb") as stream:
                response = decode(stream.readline(MAX_MESSAGE_BYTES + 2))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "MESSAGE_TOO_LARGE")
        duplicate = self.spawn_broker()
        duplicate.wait(timeout=10)
        self.assertEqual(duplicate.returncode, 1)
        self.assertEqual(self.client._endpoint()["process"]["pid"], self.broker_pid)
        self.assertEqual(self.client.request("status")["total_jobs"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
