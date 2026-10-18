"""Durable, process-safe CAD queue. This module never imports or calls COM.

All state transitions use short SQLite IMMEDIATE transactions. The dispatcher
must hold its OS lease before calling ``recover_started`` or ``claim_next``.
An uncertain execution is sticky until an operator explicitly reconciles it.
"""

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid


TERMINAL_STATES = ("completed", "failed", "cancelled")
STATES = ("queued", "running", *TERMINAL_STATES, "recovery_required")
# Written atomically with claim, then replaced durably BEFORE any COM access.
# A crash before replacement proves that this job never entered native code.
UNSTARTED_NATIVE_CONTEXT = {"native_execution_not_started": True, "context_version": 1}


class JournalError(RuntimeError):
    """A queue invariant or admission limit was violated."""


class IdempotencyConflict(JournalError):
    """The request ID has already been used for a different request."""


class RecoveryRequired(JournalError):
    """A previous execution has an uncertain outcome."""


class Journal:
    SCHEMA_VERSION = 1

    def __init__(self, path, *, max_active_jobs=1000,
                 max_payload_bytes=4 * 1024 * 1024, max_total_jobs=100000):
        self.path = Path(path).expanduser().resolve()
        self.max_active_jobs = int(max_active_jobs)
        self.max_payload_bytes = int(max_payload_bytes)
        self.max_total_jobs = int(max_total_jobs)
        if min(self.max_active_jobs, self.max_payload_bytes, self.max_total_jobs) < 1:
            raise ValueError("Journal limits must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Pre-create with restrictive permissions; SQLite sidecars inherit them.
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, self.SCHEMA_VERSION):
                raise JournalError("Unsupported queue journal schema")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL UNIQUE,
                    client_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                      ('queued','running','completed','failed','cancelled','recovery_required')),
                    owner_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    queue_deadline REAL,
                    execution_timeout_sec REAL NOT NULL,
                    result_json TEXT,
                    execution_context_json TEXT,
                    reason TEXT,
                    recovery_note TEXT,
                    recovery_operator TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_state_seq ON jobs(state, seq);
                CREATE TABLE IF NOT EXISTS request_keys (
                    request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    job_id TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    at REAL NOT NULL,
                    event TEXT NOT NULL,
                    owner_id TEXT,
                    state TEXT
                );
                CREATE INDEX IF NOT EXISTS events_job_seq ON events(job_id, seq);
            """)
            conn.execute("PRAGMA user_version=1")

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _json(self, value):
        try:
            data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("Queue payload must be finite JSON") from exc
        if len(data.encode("utf-8")) > self.max_payload_bytes:
            raise JournalError("Queue payload exceeds configured size limit")
        return data

    @staticmethod
    def _identity(value, label):
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError(label + " must be a nonempty string of at most 256 characters")
        if any(ord(c) < 32 for c in value):
            raise ValueError(label + " must not contain control characters")
        return value

    @staticmethod
    def _duration(value, label, optional=False):
        if value is None and optional:
            return None
        if isinstance(value, bool):
            raise ValueError(label + " must be a positive finite duration")
        try:
            duration = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(label + " must be a positive finite duration") from exc
        if not math.isfinite(duration) or duration <= 0 or duration > 7 * 86400:
            raise ValueError(label + " must be within (0, 604800] seconds")
        return duration

    @staticmethod
    def _job(row):
        if row is None:
            return None
        job = dict(row)
        job["job_id"] = job["id"]
        job["queued_at"] = job["created_at"]
        job["arguments"] = json.loads(job.pop("arguments_json"))
        raw = job.pop("result_json")
        job["result"] = json.loads(raw) if raw is not None else None
        context = job.pop("execution_context_json")
        job["execution_context"] = json.loads(context) if context is not None else None
        job.pop("fingerprint", None)
        return job

    @staticmethod
    def _event(conn, job_id, event, owner_id=None, state=None):
        # Deliberately excludes arguments, scripts, results, exception text.
        conn.execute("INSERT INTO events(job_id,at,event,owner_id,state) VALUES(?,?,?,?,?)",
                     (job_id, time.time(), event, owner_id, state))

    @staticmethod
    def _row(conn, job_id):
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JournalError("Unknown job ID")
        return row

    @staticmethod
    def _check_owner(row, owner_id):
        if not owner_id or row["owner_id"] != owner_id:
            raise JournalError("Job execution belongs to another dispatcher owner")

    def enqueue(self, tool, arguments, request_id, client_id,
                queue_timeout_sec=3600, execution_timeout_sec=900):
        tool = self._identity(tool, "tool")
        request_id = self._identity(request_id, "request_id")
        client_id = self._identity(client_id, "client_id")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
        arguments_json = self._json(arguments)
        queue_timeout_sec = self._duration(queue_timeout_sec, "queue_timeout_sec", optional=True)
        execution_timeout_sec = self._duration(execution_timeout_sec, "execution_timeout_sec")
        fingerprint = hashlib.sha256(self._json({
            "tool": tool, "arguments": arguments,
            "queue_timeout_sec": queue_timeout_sec,
            "execution_timeout_sec": execution_timeout_sec,
        }).encode("utf-8")).hexdigest()
        with self._transaction() as conn:
            previous = conn.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise IdempotencyConflict("Request ID already belongs to a different request")
                return self._job(previous)
            tombstone = conn.execute("SELECT * FROM request_keys WHERE request_id=?", (request_id,)).fetchone()
            if tombstone is not None:
                if tombstone["fingerprint"] != fingerprint:
                    raise IdempotencyConflict("Request ID already belongs to a different archived request")
                raise JournalError("Request was already processed and archived; execution will not be repeated")
            self._expire_queued(conn)
            active = conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','recovery_required')").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM request_keys").fetchone()[0]
            if active >= self.max_active_jobs or total >= self.max_total_jobs:
                raise JournalError("Queue admission or retained request identity limit reached")
            now = time.time()
            job_id = str(uuid.uuid4())
            conn.execute("""INSERT INTO jobs
                (id,request_id,client_id,fingerprint,tool,arguments_json,state,
                 created_at,updated_at,queue_deadline,execution_timeout_sec)
                VALUES(?,?,?,?,?,?,'queued',?,?,?,?)""",
                (job_id, request_id, client_id, fingerprint, tool, arguments_json, now, now,
                 now + queue_timeout_sec if queue_timeout_sec else None, execution_timeout_sec))
            conn.execute("INSERT INTO request_keys(request_id,fingerprint,job_id) VALUES(?,?,?)", (request_id, fingerprint, job_id))
            self._event(conn, job_id, "enqueued", state="queued")
            return self._job(self._row(conn, job_id))

    def get(self, job_id):
        with self._connection() as conn:
            return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def get_by_request(self, request_id):
        with self._connection() as conn:
            return self._job(conn.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone())

    def list_jobs(self, limit=100, *, states=None):
        limit = max(1, min(int(limit), 1000))
        with self._connection() as conn:
            if states is not None:
                states = tuple(states)
                if not states or any(state not in STATES for state in states):
                    raise ValueError("Unknown or empty job state filter")
                query = "SELECT * FROM jobs WHERE state IN (" + ",".join("?" for _ in states) + ") ORDER BY seq DESC LIMIT ?"
                rows = conn.execute(query, (*states, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM jobs ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            return [self._job(row) for row in rows]

    def _expire_queued(self, conn):
        now = time.time()
        rows = conn.execute("SELECT id FROM jobs WHERE state='queued' AND queue_deadline<=?", (now,)).fetchall()
        for row in rows:
            conn.execute("UPDATE jobs SET state='cancelled',reason='queue_timeout',updated_at=?,finished_at=? WHERE id=?", (now, now, row["id"]))
            self._event(conn, row["id"], "queue_expired", state="cancelled")

    def claim_next(self, owner_id, *, execution_context=None):
        owner_id = self._identity(owner_id, "owner_id")
        if execution_context is not None and not isinstance(execution_context, dict):
            raise ValueError("Execution context must be a JSON object")
        initial_context = self._json(execution_context) if execution_context is not None else None
        with self._transaction() as conn:
            self._expire_queued(conn)
            # Global single-flight invariant is enforced even if a caller loses
            # or incorrectly omits the OS lease.
            if conn.execute("SELECT 1 FROM jobs WHERE state IN ('running','recovery_required') LIMIT 1").fetchone():
                return None
            row = conn.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                return None
            now = time.time()
            conn.execute("UPDATE jobs SET state='running',owner_id=?,started_at=?,updated_at=?,execution_context_json=? WHERE id=?",
                         (owner_id, now, now, initial_context, row["id"]))
            self._event(conn, row["id"], "claimed", owner_id, "running")
            return self._job(self._row(conn, row["id"]))

    def finish(self, job_id, owner_id, result, state="completed"):
        if state not in ("completed", "failed"):
            raise ValueError("finish state must be completed or failed")
        result_json = self._json(result)
        with self._transaction() as conn:
            row = self._row(conn, job_id)
            self._check_owner(row, owner_id)
            if row["state"] == "recovery_required":
                return self._late(conn, row, result_json, owner_id)
            if row["state"] != "running":
                raise JournalError("Only a running job can finish")
            now = time.time()
            conn.execute("UPDATE jobs SET state=?,result_json=?,updated_at=?,finished_at=? WHERE id=?", (state, result_json, now, now, job_id))
            self._event(conn, job_id, "finished", owner_id, state)
            return self._job(self._row(conn, job_id))

    def set_context(self, job_id, owner_id, context):
        """Persist the chosen CAD process/generation before executing any COM."""
        if not isinstance(context, dict):
            raise ValueError("Execution context must be a JSON object")
        context_json = self._json(context)
        with self._transaction() as conn:
            row = self._row(conn, job_id)
            self._check_owner(row, owner_id)
            if row["state"] != "running":
                raise JournalError("Execution context can only be bound to a running job")
            previous = row["execution_context_json"]
            if previous is not None and previous != self._json(UNSTARTED_NATIVE_CONTEXT):
                if previous != context_json:
                    raise JournalError("Execution context cannot change during a CAD operation")
                return self._job(row)
            conn.execute("UPDATE jobs SET execution_context_json=?,updated_at=? WHERE id=?", (context_json, time.time(), job_id))
            self._event(conn, job_id, "context_bound", owner_id, "running")
            return self._job(self._row(conn, job_id))

    def mark_recovery(self, job_id, owner_id, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4096:
            raise ValueError("Recovery reason must be nonempty and at most 4096 characters")
        with self._transaction() as conn:
            row = self._row(conn, job_id)
            self._check_owner(row, owner_id)
            if row["state"] == "recovery_required":
                return self._job(row)
            if row["state"] != "running":
                raise JournalError("Only a running job can require recovery")
            conn.execute("UPDATE jobs SET state='recovery_required',reason=?,updated_at=? WHERE id=?", (reason, time.time(), job_id))
            self._event(conn, job_id, "recovery_required", owner_id, "recovery_required")
            return self._job(self._row(conn, job_id))

    def _late(self, conn, row, result_json, owner_id):
        if row["result_json"] is not None:
            if row["result_json"] != result_json:
                raise JournalError("A different late result was already recorded")
            return self._job(row)
        conn.execute("UPDATE jobs SET result_json=?,updated_at=? WHERE id=?", (result_json, time.time(), row["id"]))
        self._event(conn, row["id"], "late_result", owner_id, "recovery_required")
        return self._job(self._row(conn, row["id"]))

    def record_late_result(self, job_id, owner_id, result):
        result_json = self._json(result)
        with self._transaction() as conn:
            row = self._row(conn, job_id)
            self._check_owner(row, owner_id)
            if row["state"] != "recovery_required":
                raise JournalError("Late results require a recovery gate")
            return self._late(conn, row, result_json, owner_id)

    def recover_started(self):
        """Call once after obtaining exclusive dispatcher ownership on startup."""
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE state='running' ORDER BY seq").fetchall()
            for row in rows:
                conn.execute("UPDATE jobs SET state='recovery_required',reason='dispatcher_interrupted',updated_at=? WHERE id=?", (time.time(), row["id"]))
                self._event(conn, row["id"], "startup_recovery", row["owner_id"], "recovery_required")
            self._expire_queued(conn)
            return [self._job(self._row(conn, row["id"])) for row in rows]

    def cancel(self, job_id):
        with self._transaction() as conn:
            row = self._row(conn, job_id)
            if row["state"] != "queued":
                raise JournalError("Only a queued job can be cancelled safely")
            now = time.time()
            conn.execute("UPDATE jobs SET state='cancelled',reason='client_cancelled',updated_at=?,finished_at=? WHERE id=?", (now, now, job_id))
            self._event(conn, job_id, "cancelled", state="cancelled")
            return self._job(self._row(conn, job_id))

    def status(self):
        with self._transaction() as conn:
            self._expire_queued(conn)
            counts = {state: 0 for state in STATES}
            counts.update({row["state"]: row["n"] for row in conn.execute("SELECT state,COUNT(*) AS n FROM jobs GROUP BY state")})
            recovery = [row["id"] for row in conn.execute("SELECT id FROM jobs WHERE state='recovery_required' ORDER BY seq")]
            running = [row["id"] for row in conn.execute("SELECT id FROM jobs WHERE state='running' ORDER BY seq")]
            return {"schema_version": self.SCHEMA_VERSION, "counts": counts,
                    "gate": "recovery_required" if recovery else "open",
                    "recovery_required": bool(recovery), "recovery_job_ids": recovery,
                    "running_job_ids": running, "total_jobs": sum(counts.values())}

    def acknowledge_recovery(self, note, operator, expected_job_ids, *, backend_idle_verified=False):
        """Reconcile uncertain jobs only after the broker independently proves idle.

        This flag is an internal trust boundary, never a client-supplied proof.
        The exact ID set prevents stale acknowledgements from clearing new gates.
        Acknowledged jobs become failed and are NEVER replayed automatically.
        """
        if backend_idle_verified is not True:
            raise RecoveryRequired("Backend idleness must be independently verified")
        operator = self._identity(operator, "operator")
        if not isinstance(note, str) or not note.strip() or len(note) > 4096:
            raise ValueError("A recovery note of at most 4096 characters is required")
        if not isinstance(expected_job_ids, (list, tuple, set)) or not expected_job_ids:
            raise ValueError("An explicit nonempty set of recovery job IDs is required")
        expected = set(expected_job_ids)
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
                raise RecoveryRequired("Cannot reconcile while a job is running")
            rows = conn.execute("SELECT * FROM jobs WHERE state='recovery_required' ORDER BY seq").fetchall()
            if expected != {row["id"] for row in rows}:
                raise RecoveryRequired("Recovery job set changed; inspect current state")
            now = time.time()
            for row in rows:
                conn.execute("UPDATE jobs SET state='failed',recovery_note=?,recovery_operator=?,updated_at=?,finished_at=? WHERE id=?", (note, operator, now, now, row["id"]))
                self._event(conn, row["id"], "recovery_acknowledged", operator, "failed")
            return [self._job(self._row(conn, row["id"])) for row in rows]

    def events(self, job_id=None, limit=100):
        limit = max(1, min(int(limit), 1000))
        with self._connection() as conn:
            if job_id is None:
                rows = conn.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM events WHERE job_id=? ORDER BY seq DESC LIMIT ?", (job_id, limit)).fetchall()
            return [dict(row) for row in reversed(rows)]

    def prune_terminal(self, *, older_than, keep_last=1000):
        """Explicit retention only. Active/recovery evidence is never deleted.

        Request identity tombstones survive pruning, so an archived request
        cannot replay a mutation. Admission counts these keys and fails closed
        at max_total_jobs; increasing that limit is an explicit admin action.
        The broker should expose pruning only as administrative maintenance.
        """
        if not isinstance(older_than, (int, float)) or not math.isfinite(older_than):
            raise ValueError("older_than must be a finite epoch timestamp")
        keep_last = max(0, int(keep_last))
        with self._transaction() as conn:
            rows = conn.execute("""SELECT id FROM jobs WHERE state IN ('completed','failed','cancelled')
                AND finished_at<? AND id NOT IN (SELECT id FROM jobs
                WHERE state IN ('completed','failed','cancelled') ORDER BY seq DESC LIMIT ?)""", (older_than, keep_last)).fetchall()
            for row in rows:
                conn.execute("DELETE FROM events WHERE job_id=?", (row["id"],))
                conn.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
            if rows:
                self._event(conn, None, "terminal_history_pruned")
            return len(rows)
