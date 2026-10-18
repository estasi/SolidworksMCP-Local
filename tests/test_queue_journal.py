"""Durable queue and ownership acceptance tests: real independent processes.

Only Python's standard library is required. The production CAD package is not
imported, ensuring these tests cannot connect to or launch SOLIDWORKS.
"""

import importlib.util
from contextlib import closing
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid


def _load(name):
    path = Path(__file__).resolve().parents[1] / "solidworks_mcp" / "coordination" / (name + ".py")
    spec = importlib.util.spec_from_file_location("cad_queue_test_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


journal_module = _load("journal")
ownership_module = _load("ownership")
Journal = journal_module.Journal
JournalError = journal_module.JournalError
IdempotencyConflict = journal_module.IdempotencyConflict
RecoveryRequired = journal_module.RecoveryRequired
ProcessLease = ownership_module.ProcessLease


def _enqueue_same(path, barrier, out):
    journal = Journal(path)
    barrier.wait(10)
    job = journal.enqueue("build", {"steps": [1, 2]}, "shared-request", "client")
    out.put((os.getpid(), job["id"]))


def _consume(path, barrier, out):
    journal = Journal(path)
    owner = "worker-" + str(os.getpid())
    barrier.wait(10)
    completed = []
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = journal.claim_next(owner)
        if job is None:
            status = journal.status()
            if status["counts"]["queued"] == 0 and status["counts"]["running"] == 0:
                out.put((os.getpid(), completed))
                return
            time.sleep(0.005)
            continue
        started = time.monotonic_ns()
        # Give the second process time to contend while this whole operation
        # comprises multiple individually interruptible synthetic steps.
        for _ in range(3):
            time.sleep(0.006)
        ended = time.monotonic_ns()
        journal.finish(job["id"], owner, {"pid": os.getpid()})
        completed.append((job["seq"], started, ended))
        time.sleep(0.003)
    raise RuntimeError("Consumer deadline exceeded")


def _crash_running(path, pipe):
    journal = Journal(path)
    job = journal.claim_next("crashing-owner")
    journal.set_context(job["id"], "crashing-owner", {"target_pid": 12345, "target_created_at": 1.0})
    pipe.send(job["id"])
    pipe.close()
    os._exit(23)


def _lease_holder(name, pipe):
    lease = ProcessLease(name)
    pipe.send(lease.acquire())
    pipe.close()
    time.sleep(30)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "private" / "queue.sqlite3"
        self.journal = Journal(self.path)
        self.ctx = multiprocessing.get_context("spawn")

    def tearDown(self):
        self.tmp.cleanup()

    def enqueue(self, request="r1", **kwargs):
        return self.journal.enqueue("build", {"part": "synthetic"}, request, "client", **kwargs)

    def join_ok(self, process):
        process.join(25)
        if process.is_alive():
            process.terminate()
            process.join(5)
            self.fail("Child process exceeded deadline")
        self.assertEqual(process.exitcode, 0)

    def test_two_process_duplicate_admission_is_exactly_once(self):
        barrier, out = self.ctx.Barrier(2), self.ctx.Queue()
        children = [self.ctx.Process(target=_enqueue_same, args=(str(self.path), barrier, out)) for _ in range(2)]
        for child in children:
            child.start()
        results = [out.get(timeout=20) for _ in children]
        for child in children:
            self.join_ok(child)
        self.assertEqual(len({pid for pid, _ in results}), 2)
        self.assertEqual(len({job_id for _, job_id in results}), 1)
        self.assertEqual(self.journal.status()["total_jobs"], 1)
        self.assertEqual([event["event"] for event in self.journal.events()], ["enqueued"])
        out.close()

    def test_two_process_claims_fifo_and_entire_operations_never_overlap(self):
        original = [self.enqueue("request-" + str(i)) for i in range(16)]
        barrier, out = self.ctx.Barrier(2), self.ctx.Queue()
        children = [self.ctx.Process(target=_consume, args=(str(self.path), barrier, out)) for _ in range(2)]
        for child in children:
            child.start()
        results = [out.get(timeout=25) for _ in children]
        for child in children:
            self.join_ok(child)
        self.assertEqual(len({pid for pid, _ in results}), 2)
        operations = sorted([item for _, rows in results for item in rows])
        self.assertEqual([seq for seq, _, _ in operations], [job["seq"] for job in original])
        self.assertTrue(all(operations[i][2] <= operations[i + 1][1] for i in range(len(operations) - 1)))
        claimed = [event["job_id"] for event in self.journal.events(limit=1000) if event["event"] == "claimed"]
        self.assertEqual(claimed, [job["id"] for job in original])
        self.assertEqual(self.journal.status()["counts"]["completed"], 16)
        out.close()

    def test_process_death_persists_binding_and_blocks_next_execution(self):
        first, second = self.enqueue("first"), self.enqueue("second")
        receiving, sending = self.ctx.Pipe(duplex=False)
        child = self.ctx.Process(target=_crash_running, args=(str(self.path), sending))
        child.start()
        sending.close()
        self.assertTrue(receiving.poll(20))
        self.assertEqual(receiving.recv(), first["id"])
        child.join(10)
        self.assertEqual(child.exitcode, 23)
        receiving.close()
        restarted = Journal(self.path)
        recovered = restarted.recover_started()
        self.assertEqual([job["id"] for job in recovered], [first["id"]])
        self.assertEqual(recovered[0]["execution_context"], {"target_pid": 12345, "target_created_at": 1.0})
        self.assertIsNone(restarted.claim_next("new-owner"))
        self.assertEqual(restarted.get(second["id"])["state"], "queued")
        self.assertEqual(restarted.recover_started(), [])
        self.assertTrue(restarted.status()["recovery_required"])

    def test_timeout_late_result_remains_sticky_until_exact_verified_ack(self):
        job = self.enqueue()
        self.enqueue("next")
        self.journal.claim_next("owner")
        self.journal.mark_recovery(job["id"], "owner", "execution_timeout")
        result = self.journal.finish(job["id"], "owner", {"ok": True})
        self.assertEqual(result["state"], "recovery_required")
        self.assertIsNone(self.journal.claim_next("owner"))
        with self.assertRaises(RecoveryRequired):
            self.journal.acknowledge_recovery("inspected", "operator", [job["id"]])
        with self.assertRaises(RecoveryRequired):
            self.journal.acknowledge_recovery("inspected", "operator", ["stale-id"], backend_idle_verified=True)
        self.journal.acknowledge_recovery("inspected CAD idle and documents", "operator", [job["id"]], backend_idle_verified=True)
        self.assertEqual(self.journal.get(job["id"])["state"], "failed")
        self.assertIsNotNone(self.journal.claim_next("owner"))
        self.assertEqual(self.journal.get(job["id"])["result"], {"ok": True})

    def test_idempotency_survives_client_reconnect_and_rejects_semantic_changes(self):
        first = self.enqueue()
        self.assertEqual(self.enqueue()["id"], first["id"])
        reconnected = self.journal.enqueue("build", {"part": "synthetic"}, "r1", "reconnected-client")
        self.assertEqual(reconnected["id"], first["id"])
        self.assertEqual(reconnected["client_id"], "client")
        variants = [({"part": "other"}, "client", 900), ({"part": "synthetic"}, "client", 901)]
        for arguments, client, timeout in variants:
            with self.assertRaises(IdempotencyConflict):
                self.journal.enqueue("build", arguments, "r1", client, execution_timeout_sec=timeout)
        self.journal.claim_next("owner")
        self.journal.finish(first["id"], "owner", {"saved": True})
        self.assertEqual(self.enqueue()["state"], "completed")

    def test_owner_enforced_and_execution_binding_is_immutable(self):
        job = self.enqueue()
        self.journal.claim_next("owner")
        with self.assertRaises(JournalError):
            self.journal.finish(job["id"], "different-owner", {})
        with self.assertRaises(JournalError):
            self.journal.mark_recovery(job["id"], "different-owner", "bad")
        context = {"target_pid": 42, "target_created_at": 10.0}
        self.journal.set_context(job["id"], "owner", context)
        self.journal.set_context(job["id"], "owner", context)
        with self.assertRaises(JournalError):
            self.journal.set_context(job["id"], "owner", {"target_pid": 43})
        self.assertEqual(self.journal.get(job["id"])["execution_context"], context)

    def test_queue_expiration_and_cancel_never_interrupt_running(self):
        expired = self.enqueue("expired", queue_timeout_sec=0.001)
        live = self.enqueue("live")
        time.sleep(0.01)
        self.assertEqual(self.journal.claim_next("owner")["id"], live["id"])
        self.assertEqual(self.journal.get(expired["id"])["reason"], "queue_timeout")
        with self.assertRaises(JournalError):
            self.journal.cancel(live["id"])
        queued = self.enqueue("cancel")
        self.assertEqual(self.journal.cancel(queued["id"])["state"], "cancelled")

    def test_admission_payload_limits_and_finite_json(self):
        limited = Journal(self.path, max_active_jobs=1, max_payload_bytes=512)
        limited.enqueue("tool", {}, "one", "client")
        with self.assertRaises(JournalError):
            limited.enqueue("tool", {}, "two", "client")
        with self.assertRaises(JournalError):
            self.journal.enqueue("tool", {"blob": "x" * (4 * 1024 * 1024)}, "large", "client")
        with self.assertRaises(ValueError):
            self.journal.enqueue("tool", {"number": float("nan")}, "nan", "client")
        with self.assertRaises(ValueError):
            self.enqueue("bad-timeout", execution_timeout_sec=float("inf"))

    def test_audit_events_do_not_contain_scripts_results_or_error_text(self):
        marker = "sensitive-synthetic-script-content"
        job = self.journal.enqueue("python", {"script": marker}, "script", "client")
        self.journal.claim_next("owner")
        self.journal.mark_recovery(job["id"], "owner", marker)
        self.journal.record_late_result(job["id"], "owner", {"output": marker})
        self.assertNotIn(marker, json.dumps(self.journal.events()))
        self.assertIn(marker, json.dumps(self.journal.get(job["id"])))

    def test_explicit_retention_preserves_uncertain_jobs_and_request_tombstones(self):
        terminal = self.enqueue("terminal")
        self.journal.claim_next("owner")
        self.journal.finish(terminal["id"], "owner", {})
        uncertain = self.enqueue("uncertain")
        self.journal.claim_next("owner")
        self.journal.mark_recovery(uncertain["id"], "owner", "unknown")
        self.assertEqual(self.journal.prune_terminal(older_than=time.time() + 1, keep_last=0), 1)
        self.assertIsNotNone(self.journal.get(uncertain["id"]))
        self.assertIsNone(self.journal.get(terminal["id"]))
        with self.assertRaises(JournalError):
            self.enqueue("terminal")
        with self.assertRaises(IdempotencyConflict):
            self.journal.enqueue("different", {}, "terminal", "client")

    def test_sqlite_wal_integrity_and_schema_refusal(self):
        self.enqueue()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            conn.execute("PRAGMA user_version=999")
        with self.assertRaises(JournalError):
            Journal(self.path)


class OwnershipTests(unittest.TestCase):
    def test_two_process_lease_exclusion_and_release_after_process_death(self):
        ctx = multiprocessing.get_context("spawn")
        name = "synthetic-test-" + str(uuid.uuid4())
        receiving, sending = ctx.Pipe(duplex=False)
        child = ctx.Process(target=_lease_holder, args=(name, sending))
        child.start()
        sending.close()
        try:
            self.assertTrue(receiving.poll(20))
            self.assertTrue(receiving.recv())
            lease = ProcessLease(name)
            self.assertFalse(lease.acquire(timeout=0.05))
            timer = threading.Timer(0.15, child.terminate)
            timer.start()
            try:
                self.assertTrue(lease.acquire(timeout=5))
                if os.name == "nt":
                    self.assertTrue(lease.abandoned)
                lease.release()
            finally:
                timer.join(5)
        finally:
            if child.is_alive():
                child.terminate()
            child.join(5)
            receiving.close()

    def test_lease_release_requires_same_thread_and_not_reentrant(self):
        lease = ProcessLease("thread-test-" + str(uuid.uuid4()))
        self.assertTrue(lease.acquire())
        with self.assertRaises(RuntimeError):
            lease.acquire()
        errors = []
        thread = threading.Thread(target=lambda: self._foreign_release(lease, errors))
        thread.start()
        thread.join(5)
        self.assertEqual(errors, ["RuntimeError"])
        self.assertTrue(lease.acquired)
        lease.release()
        self.assertFalse(lease.acquired)

    @staticmethod
    def _foreign_release(lease, errors):
        try:
            lease.release()
        except Exception as exc:
            errors.append(type(exc).__name__)

    def test_process_creation_identity_is_stable(self):
        identity = ownership_module.process_identity()
        self.assertEqual(identity, ownership_module.process_identity(os.getpid()))
        self.assertEqual(identity["pid"], os.getpid())
        self.assertIsInstance(identity["created_at"], float)
        self.assertTrue(identity["creation_token"])
        self.assertEqual(ownership_module.user_session_identity(), ownership_module.user_session_identity())
        self.assertTrue(ownership_module.process_is_same_alive(os.getpid(), identity["created_at"]))
        self.assertFalse(ownership_module.process_is_same_alive(os.getpid(), identity["created_at"] - 10))
        for unknown_creation in (None, 0, float("nan"), float("inf")):
            self.assertIsNone(ownership_module.process_is_same_alive(os.getpid(), unknown_creation))


if __name__ == "__main__":
    unittest.main(verbosity=2)
