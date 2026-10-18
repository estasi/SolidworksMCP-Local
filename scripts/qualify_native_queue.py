"""Opt-in SW2025 queue qualification on a NEW synthetic document.

Importing this module or requesting --help performs no CAD or process discovery.
The native test must be run later, after the active CAD work has finished and the
queued MCP release has been activated. It never activates a release, acknowledges
recovery, kills a process, or runs native crash/fault-injection scenarios.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import queue
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
DIMENSION_NAME = "D1@QueueQualificationBoss"
BASE_DEPTH_M = 0.020
INCREMENT_M = 0.001
SIDE_M = 0.020


class QualificationFailure(RuntimeError):
    def __init__(self, code, job=None):
        super().__init__(code)
        self.code = code
        self.job = job


def _imports():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from solidworks_mcp.coordination.client import QueueClient
    from solidworks_mcp.coordination.platform_guard import (
        atomic_json, default_state_dir, doctor, private_directory, validate_activation)
    from solidworks_mcp.coordination.protocol import unwrap_result
    return QueueClient, atomic_json, default_state_dir, doctor, private_directory, validate_activation, unwrap_result


def _summary(job, phase):
    context = job.get("execution_context") or {}
    return {"phase": phase, "job_id": job["id"], "request_id": job["request_id"],
            "seq": job["seq"], "state": job["state"],
            "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
            "native_target_pid": context.get("target_pid"),
            "native_target_created_at": context.get("target_created_at")}


def _operation(client, tool, arguments, request_id, phase, expected=None,
               trusted=False, timeout=180, allow_failed_read=False):
    """Use the same production admission route as QueueClient.call_tool.

    _coordination is the internal envelope produced by call_tool after removing
    public coordination metadata; no direct backend/automation entry is used.
    """
    *_, unwrap_result = _imports()
    arguments = dict(arguments)
    metadata = {}
    if expected is not None:
        metadata["expected_document"] = expected
    if trusted:
        metadata["trusted_python"] = True
    if metadata:
        arguments["_coordination"] = metadata
    job = client.submit(tool, arguments, request_id=request_id,
                        queue_timeout_sec=timeout, execution_timeout_sec=timeout)
    deadline = time.monotonic() + timeout + 30
    while job["state"] in {"queued", "running"} and time.monotonic() < deadline:
        job = client.wait(job["id"], timeout=min(10, max(.01, deadline - time.monotonic())))
    summary = _summary(job, phase)
    if job["state"] not in {"completed", "failed"}:
        # Keep the full returned failure in the private report, including late
        # native results that have correctly left the queue fenced.
        summary["native_result"] = job.get("result")
        summary["journal_reason"] = job.get("reason")
        raise QualificationFailure("NATIVE_JOB_" + job["state"].upper(), summary)
    result = unwrap_result(job["result"])
    summary["success"] = result.get("success") is True
    summary["error_name"] = result.get("error_name")
    if not summary["success"] and not allow_failed_read:
        summary["native_result"] = job.get("result")
        summary["journal_reason"] = job.get("reason")
        raise QualificationFailure("NATIVE_OPERATION_FAILED", summary)
    return result, summary


_SCRIPT_HELPERS = r'''
def require_ok(value, label):
    if not isinstance(value, dict) or value.get("success") is not True:
        raise RuntimeError(json.dumps({"code": "QUALIFICATION_" + label + "_FAILED",
                                      "operation": label, "native_result": value},
                                     ensure_ascii=False, default=str))
    return value

def rebuild_and_save():
    if not com_get(doc, "EditRebuild3"):
        raise RuntimeError("QUALIFICATION_REBUILD_FAILED")
    require_ok(automation.save_document(), "SAVE")

def snapshot():
    bodies = list(doc.GetBodies2(0, False) or [])
    if len(bodies) != 1:
        raise RuntimeError("QUALIFICATION_BODY_COUNT")
    bounds = list(com_get(bodies[0], "GetBodyBox"))
    mass = automation._body_mass_props(bodies[0], "m")
    dimension = doc.Parameter("D1@QueueQualificationBoss")
    if dimension is None or len(bounds) != 6 or not mass or "volume_exact" not in mass:
        raise RuntimeError("QUALIFICATION_METRICS_UNAVAILABLE")
    return {"depth_m": float(dimension.SystemValue), "body_count": len(bodies),
            "bbox_m": [float(x) for x in bounds], "volume_m3": float(mass["volume_exact"]),
            "solidworks_revision": str(com_get(sw, "RevisionNumber"))}
'''


def _seed_script():
    return _SCRIPT_HELPERS + r'''
if list(doc.GetBodies2(0, False) or []):
    raise RuntimeError("QUALIFICATION_TEMPLATE_HAS_EXISTING_BODIES")
if typed(doc.FeatureManager, "IFeatureManager") is None:
    raise RuntimeError("QUALIFICATION_TYPED_FEATURE_MANAGER_UNAVAILABLE")
profile = require_ok(automation.create_parametric_sketch(
    name="QueueQualificationProfile", plane="Front", unit="mm",
    entities=[
        {"id": "bottom", "type": "line", "start": [-10, -10], "end": [10, -10]},
        {"id": "right", "type": "line", "start": [10, -10], "end": [10, 10]},
        {"id": "top", "type": "line", "start": [10, 10], "end": [-10, 10]},
        {"id": "left", "type": "line", "start": [-10, 10], "end": [-10, -10]}],
    constraints=[
        {"type": "coincident", "entities": ["bottom.end", "right.start"]},
        {"type": "coincident", "entities": ["right.end", "top.start"]},
        {"type": "coincident", "entities": ["top.end", "left.start"]},
        {"type": "coincident", "entities": ["left.end", "bottom.start"]}],
    solve={"mode": "locked_trace", "target": "fully_defined"},
    validation={"require_closed": True, "closed_contours": 1},
    transaction={"rollback_on_failure": True}), "PROFILE")
if profile["data"]["status"] != "fully_defined":
    raise RuntimeError("QUALIFICATION_PROFILE_NOT_FULLY_DEFINED")
profile_exit = require_ok(automation.exit_sketch(), "PROFILE_EXIT")
if profile_exit["data"].get("verified") is not True:
    raise RuntimeError("QUALIFICATION_PROFILE_EXIT_UNVERIFIED")
extrusion = require_ok(automation.advanced_extrude(
    sketch_name=profile["data"]["sketch_name"], depth=20, unit="mm",
    feature_name="QueueQualificationBoss", end_condition="blind",
    auto_verify=True, auto_flags=False), "TYPED_EXTRUSION")
if (extrusion["data"].get("typed_feature_manager") is not True or
        extrusion["data"].get("feature_name") != "QueueQualificationBoss"):
    raise RuntimeError("QUALIFICATION_TYPED_FEATURE_IDENTITY_UNVERIFIED")
rebuild_and_save()
metrics = snapshot()
metrics["seed_profile_status"] = profile["data"]["status"]
metrics["seed_profile_name"] = profile["data"]["sketch_name"]
metrics["seed_profile_exit_verified"] = profile_exit["data"]["verified"]
metrics["seed_typed_feature_manager"] = extrusion["data"]["typed_feature_manager"]
result = json.dumps(metrics, allow_nan=False)
'''


def _increment_script(label, delay):
    return _SCRIPT_HELPERS + "\nimport time\n" + f'''
started_ns = time.monotonic_ns()
dimension = doc.Parameter({DIMENSION_NAME!r})
if dimension is None:
    raise RuntimeError("QUALIFICATION_DIMENSION_MISSING")
before = float(dimension.SystemValue)
time.sleep({float(delay)!r})
dimension.SystemValue = before + {INCREMENT_M!r}
rebuild_and_save()
metrics = snapshot()
if abs(metrics["depth_m"] - before - {INCREMENT_M!r}) > 1e-9:
    raise RuntimeError("QUALIFICATION_INCREMENT_READBACK")
metrics.update({{"label": {label!r}, "started_ns": started_ns,
                "ended_ns": time.monotonic_ns(), "clock": "broker_monotonic_ns",
                "before_m": before}})
result = json.dumps(metrics, allow_nan=False)
'''


def _metrics(result):
    raw = (result.get("data") or {}).get("result")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise QualificationFailure("INVALID_NATIVE_METRICS")
    return value


def _check_geometry(metrics, expected_depth):
    if metrics.get("body_count") != 1:
        raise QualificationFailure("NATIVE_BODY_COUNT_MISMATCH")
    if not str(metrics.get("solidworks_revision", "")).startswith("33."):
        raise QualificationFailure("NATIVE_VERSION_NOT_SW2025")
    if not math.isclose(float(metrics["depth_m"]), expected_depth, rel_tol=0, abs_tol=1e-9):
        raise QualificationFailure("NATIVE_FINAL_INCREMENT_COUNT_MISMATCH")
    bounds = metrics["bbox_m"]
    if len(bounds) != 6 or not all(math.isfinite(float(x)) for x in bounds):
        raise QualificationFailure("NATIVE_BBOX_UNAVAILABLE")
    actual_sides = sorted(float(bounds[i + 3]) - float(bounds[i]) for i in range(3))
    expected_sides = sorted([SIDE_M, SIDE_M, expected_depth])
    if any(not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-6)
           for actual, expected in zip(actual_sides, expected_sides)):
        raise QualificationFailure("NATIVE_BBOX_MISMATCH")
    expected_volume = SIDE_M * SIDE_M * expected_depth
    if not math.isclose(float(metrics["volume_m3"]), expected_volume, rel_tol=1e-5, abs_tol=1e-12):
        raise QualificationFailure("NATIVE_VOLUME_MISMATCH")


def _check_reopened_geometry(before, reopened):
    """Native save/reopen is compared at stated tolerances, never exact floats."""
    if (before["body_count"] != reopened["body_count"] or
            before["solidworks_revision"] != reopened["solidworks_revision"] or
            not math.isclose(float(before["depth_m"]), float(reopened["depth_m"]),
                             rel_tol=0, abs_tol=1e-9) or
            not math.isclose(float(before["volume_m3"]), float(reopened["volume_m3"]),
                             rel_tol=1e-5, abs_tol=1e-12) or
            len(before["bbox_m"]) != 6 or len(reopened["bbox_m"]) != 6 or
            any(not math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-6)
                for a, b in zip(before["bbox_m"], reopened["bbox_m"]))):
        raise QualificationFailure("REOPENED_GEOMETRY_CHANGED")


def _preservation_preflight(inventory):
    if inventory.get("strict") is not True:
        raise QualificationFailure("STRICT_DOCUMENT_INVENTORY_REQUIRED")
    session = inventory.get("session") or {}
    if not isinstance(session.get("user_control"), bool) or not isinstance(session.get("visible"), bool):
        raise QualificationFailure("APPLICATION_LIFETIME_STATE_UNAVAILABLE")
    if inventory["count"] == 0 and session["user_control"] is False:
        # Closing our only document could terminate an automation-owned SW
        # session. This harness never changes app ownership/preferences.
        raise QualificationFailure("EMPTY_AUTOMATION_OWNED_SESSION")


def _working_file_evidence(inventory):
    """Read saved working files outside COM; never save or rewrite them."""
    evidence = {}
    for document in inventory["documents"]:
        if not document["path"]:
            continue
        path = Path(document["path"])
        try:
            before = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            after = path.stat()
            fields = ("st_size", "st_mtime_ns", "st_dev", "st_ino")
            if any(getattr(before, field) != getattr(after, field) for field in fields):
                raise QualificationFailure("WORKING_FILE_CHANGED_DURING_READ")
            evidence[document["key"]] = {
                "path": str(path), "size": after.st_size, "mtime_ns": after.st_mtime_ns,
                "device": after.st_dev, "file_id": after.st_ino, "sha256": digest.hexdigest()}
        except OSError as exc:
            raise QualificationFailure("WORKING_FILE_EVIDENCE_UNAVAILABLE") from exc
    return evidence


def _check_document_preservation(before, after, synthetic_path):
    """Permit only the new test part; original dirty/configuration state is exact."""
    if before.get("strict") is not True or after.get("strict") is not True:
        raise QualificationFailure("STRICT_DOCUMENT_INVENTORY_REQUIRED")
    if before.get("session") != after.get("session"):
        raise QualificationFailure("APPLICATION_SESSION_STATE_CHANGED")
    original = {item["key"]: item for item in before["documents"]}
    current = {item["key"]: item for item in after["documents"]}
    synthetic_key = os.path.normcase(os.path.realpath(os.path.abspath(synthetic_path)))
    if (len(original) != before["count"] or len(current) != after["count"] or
            synthetic_key in original or set(current) != set(original) | {synthetic_key} or
            any(current.get(key) != item for key, item in original.items()) or
            current[synthetic_key]["type"] != 1 or
            (after.get("active_document") or {}).get("key") != synthetic_key):
        raise QualificationFailure("WORKING_DOCUMENT_STATE_CHANGED")


def _worker(index, run_id, expected, count, delay, timeout, barrier, stopped, output):
    QueueClient, *_ = _imports()
    client = QueueClient(auto_start=False)
    try:
        barrier.wait(timeout=30)
        for step in range(count):
            if stopped.is_set():
                break
            label = f"worker-{index}-operation-{step}"
            result, job = _operation(client, "execute_python",
                {"code": _increment_script(label, delay)}, f"{run_id}-{label}", label,
                expected=expected, trusted=True, timeout=timeout)
            output.put({"kind": "operation", "worker": index, "client_pid": os.getpid(),
                        "job": job, "metrics": _metrics(result)})
    except BaseException as exc:
        stopped.set()
        output.put({"kind": "failure", "worker": index, "client_pid": os.getpid(),
                    "code": getattr(exc, "code", type(exc).__name__),
                    "job": getattr(exc, "job", None)})
    finally:
        output.put({"kind": "done", "worker": index, "client_pid": os.getpid()})


def _two_clients(run_id, expected, count, delay, timeout, evidence, save):
    ctx = multiprocessing.get_context("spawn")
    barrier, stopped, output = ctx.Barrier(2), ctx.Event(), ctx.Queue()
    children = [ctx.Process(target=_worker,
        args=(index, run_id, expected, count, delay, timeout, barrier, stopped, output))
        for index in range(2)]
    done, operations, failures = set(), [], []
    try:
        for child in children:
            child.start()
        deadline = time.monotonic() + count * (timeout + 35) + 60
        while len(done) < 2:
            if time.monotonic() > deadline:
                raise QualificationFailure("NATIVE_CLIENT_OBSERVATION_TIMEOUT")
            try:
                message = output.get(timeout=.5)
            except queue.Empty:
                if any(child.exitcode not in (None, 0) for child in children):
                    raise QualificationFailure("NATIVE_CLIENT_EXITED_UNEXPECTEDLY")
                continue
            if message["kind"] == "done":
                done.add(message["worker"])
            elif message["kind"] == "failure":
                failures.append(message)
                if message.get("job"):
                    evidence["jobs"].append(message["job"])
                evidence["client_failures"] = failures
                save()
            else:
                operations.append(message)
                evidence["jobs"].append(message["job"])
                evidence["concurrent_operations"] = operations
                save()
        for child in children:
            child.join(timeout=10)
        if failures or any(child.exitcode != 0 for child in children):
            raise QualificationFailure("NATIVE_CONCURRENT_CLIENT_FAILED")
        if len(operations) != 2 * count or len({item["client_pid"] for item in operations}) != 2:
            raise QualificationFailure("TWO_NATIVE_CLIENTS_NOT_PROVEN")
        operations.sort(key=lambda item: item["job"]["seq"])
        for index, item in enumerate(operations):
            metrics = item["metrics"]
            expected_before = BASE_DEPTH_M + index * INCREMENT_M
            if not math.isclose(float(metrics["before_m"]), expected_before, rel_tol=0, abs_tol=1e-9):
                raise QualificationFailure("NATIVE_LOST_INCREMENT_OR_FIFO_VIOLATION")
            _check_geometry(metrics, expected_before + INCREMENT_M)
            if metrics["ended_ns"] < metrics["started_ns"]:
                raise QualificationFailure("NATIVE_CLOCK_MOVED_BACKWARDS")
            if index:
                prior = operations[index - 1]
                if prior["metrics"]["ended_ns"] > metrics["started_ns"]:
                    raise QualificationFailure("NATIVE_OPERATIONS_OVERLAPPED")
                if prior["job"]["finished_at"] > item["job"]["started_at"]:
                    raise QualificationFailure("NATIVE_JOURNAL_OPERATIONS_OVERLAPPED")
        evidence["checks"].update({"two_client_processes": True,
            "whole_operation_non_overlap": True, "fifo": True,
            "native_increment_count": 2 * count})
    finally:
        stopped.set()
        # Never terminate a client/broker/CAD process. A submitted native job may
        # still be executing; its durable timeout/recovery gate remains owner.
        for child in children:
            if child.pid is not None:
                child.join(timeout=1)
        output.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acknowledge-idle-session", action="store_true",
                        help="Confirm all current CAD work is finished and no one will edit CAD during this test")
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="New, nonexistent directory on a local unsynchronized fixed disk")
    parser.add_argument("--operations-per-process", type=int, default=3)
    parser.add_argument("--delay-sec", type=float, default=.25,
                        help="Deliberate delay between reading and writing the native dimension")
    parser.add_argument("--operation-timeout-sec", type=int, default=180)
    args = parser.parse_args(argv)
    if not args.acknowledge_idle_session:
        parser.error("--acknowledge-idle-session is required; no native actions were performed")
    if not 1 <= args.operations_per_process <= 20:
        parser.error("--operations-per-process must be between 1 and 20")
    if not math.isfinite(args.delay_sec) or not .05 <= args.delay_sec <= 2:
        parser.error("--delay-sec must be between .05 and 2 seconds")
    if not 30 <= args.operation_timeout_sec <= 600:
        parser.error("--operation-timeout-sec must be between 30 and 600")
    output = args.output_dir.expanduser().absolute()
    if output.exists():
        parser.error("--output-dir must be new and nonexistent; existing files are never overwritten")
    QueueClient, atomic_json, default_state_dir, doctor, private_directory, validate_activation, _ = _imports()
    evidence = {"format": 1, "qualification": "native_sw2025_queue_concurrency",
        "actual_native": False, "requested_backend": "native", "status": "not_started",
        "phase": "preflight", "run_id": uuid.uuid4().hex, "jobs": [], "checks": {},
        "fault_injection": "not_performed_native_faults_are_out_of_scope",
        "prior_user_documents": "never_closed_or_saved_by_this_harness",
        "active_document_policy": "leave_the_new_synthetic_test_document_active"}
    report = output / "native-queue-qualification.json"
    client = None
    output_owned = False

    def save():
        evidence["updated_at"] = time.time()
        atomic_json(report, evidence)

    def operation(tool, arguments, phase, expected=None, trusted=False, allow_failed_read=False):
        evidence["phase"] = phase
        save()
        try:
            result, job = _operation(client, tool, arguments,
                evidence["run_id"] + "-" + phase, phase, expected, trusted,
                args.operation_timeout_sec, allow_failed_read)
        except QualificationFailure as exc:
            if exc.job:
                evidence["jobs"].append(exc.job)
                evidence["actual_native"] = evidence["actual_native"] or bool(exc.job.get("native_target_pid"))
            raise
        evidence["jobs"].append(job)
        evidence["actual_native"] = evidence["actual_native"] or bool(job.get("native_target_pid"))
        save()
        return result

    try:
        # Exclusive reservation prevents a second test from sharing this output
        # directory even when both passed the earlier existence check.
        output.mkdir(parents=False, exist_ok=False)
        output_owned = True
        private_directory(output)
        save()
        inspection = doctor()  # Process discovery only, no COM.
        if inspection["legacy_clients"]:
            raise QualificationFailure("LEGACY_MCP_CLIENTS_STILL_RUNNING")
        activation = validate_activation(default_state_dir())
        if activation["expected_major"] != 33:
            raise QualificationFailure("ACTIVATION_IS_NOT_SW2025")
        client = QueueClient(auto_start=False)
        initial = client.request("status")
        if (initial["recovery_required"] or initial.get("fatal_gate") or
            initial["executor_busy"] or initial["counts"]["queued"] or not initial["accepting"]):
            raise QualificationFailure("NATIVE_QUEUE_NOT_IDLE_AND_OPEN")
        evidence["target_pid"] = activation["target_pid"]
        evidence["target_created_at"] = activation["target_created_at"]
        evidence["expected_major"] = 33
        evidence["broker_owner_id"] = initial["owner_id"]
        evidence["checks"]["initial_queue_idle_no_legacy_clients"] = True
        evidence["status"] = "running"
        prior = operation("list_open_documents", {"strict": True},
                          "working_document_inventory_before")["data"]
        evidence["working_documents_before"] = prior
        save()
        _preservation_preflight(prior)
        evidence["working_files_before"] = _working_file_evidence(prior)
        prior_info = prior.get("active_document")
        if prior_info is not None:
            identity = str(prior_info.get("path", "")) + "\0" + str(prior_info.get("title", ""))
            evidence["prior_document_identity_sha256"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        save()
        operation("create_new_part", {}, "create_synthetic_part")
        info = operation("get_document_info", {}, "new_document_identity")["data"]
        if info.get("type_code") != 1 or info.get("path") not in (None, "", "Not saved"):
            raise QualificationFailure("NEW_DOCUMENT_IDENTITY_UNEXPECTED")
        if not info.get("title"):
            raise QualificationFailure("NEW_DOCUMENT_TITLE_UNAVAILABLE")
        unsaved = {"title": info["title"], "allow_unsaved_document": True, "document_type": 1}
        model = output / ("queue-qualification-" + evidence["run_id"] + ".SLDPRT")
        if model.exists():
            raise QualificationFailure("SYNTHETIC_MODEL_ALREADY_EXISTS")
        operation("save_document", {"filepath": str(model)}, "initial_synthetic_save", expected=unsaved)
        expected = {"path": str(model), "document_type": 1}
        evidence["synthetic_model"] = str(model)  # Inside explicit user output directory.
        seeded = _metrics(operation("execute_python", {"code": _seed_script()},
            "seed_cube", expected=expected, trusted=True))
        _check_geometry(seeded, BASE_DEPTH_M)
        evidence["seed_metrics"] = seeded
        evidence["solidworks_revision"] = seeded["solidworks_revision"]
        evidence["phase"] = "two_native_client_processes"
        save()
        _two_clients(evidence["run_id"], expected, args.operations_per_process,
                     args.delay_sec, args.operation_timeout_sec, evidence, save)
        expected_depth = BASE_DEPTH_M + 2 * args.operations_per_process * INCREMENT_M
        verification_code = _SCRIPT_HELPERS + "\nresult = json.dumps(snapshot(), allow_nan=False)\n"
        final = _metrics(operation("execute_python", {"code": verification_code},
            "verify_final_geometry", expected=expected, trusted=True))
        _check_geometry(final, expected_depth)
        operation("close_document", {"save": True}, "close_only_synthetic_document", expected=expected)
        operation("open_document", {"filepath": str(model)}, "reopen_saved_synthetic_document")
        reopened = _metrics(operation("execute_python", {"code": verification_code},
            "verify_reopened_geometry", expected=expected, trusted=True))
        _check_geometry(reopened, expected_depth)
        _check_reopened_geometry(final, reopened)
        after = operation("list_open_documents", {"strict": True},
                          "working_document_inventory_after")["data"]
        evidence["working_documents_after"] = after
        save()
        _check_document_preservation(prior, after, model)
        # Read only the original saved files; the synthetic model is validated
        # separately by its native save/reopen geometry checks.
        files_after = _working_file_evidence(prior)
        evidence["working_files_after"] = files_after
        save()
        if files_after != evidence["working_files_before"]:
            raise QualificationFailure("WORKING_FILES_CHANGED")
        current = client.request("status")
        if current["owner_id"] != initial["owner_id"]:
            raise QualificationFailure("BROKER_CHANGED_DURING_NATIVE_TEST")
        if current["recovery_required"] or current["executor_busy"] or current["counts"]["queued"]:
            raise QualificationFailure("QUEUE_NOT_CLEAR_AFTER_NATIVE_TEST")
        if current["total_jobs"] != initial["total_jobs"] + len(evidence["jobs"]):
            raise QualificationFailure("OTHER_CLIENT_SUBMITTED_DURING_NATIVE_QUALIFICATION")
        evidence["final_metrics"] = final
        evidence["reopened_metrics"] = reopened
        evidence["checks"].update({"body_count": True, "bbox": True, "volume": True,
            "saved_reopen": True, "native_sw2025": True, "no_other_client_jobs": True,
            "working_document_states_preserved": True, "working_file_hashes_and_stats_preserved": True})
        evidence["status"] = "passed"
        evidence["phase"] = "complete"
        evidence["note"] = "Synthetic test document remains active. Prior document identities, dirty flags and configurations match; saved working-file hashes and stats match. Prior user documents were not closed or saved. Native crash recovery was not exercised."
        save()
        print(json.dumps({"status": "passed", "actual_native": True, "report": str(report)}, ensure_ascii=False))
        return 0
    except BaseException as exc:
        evidence["status"] = "failed"
        evidence["failure_code"] = getattr(exc, "code", type(exc).__name__)
        evidence["note"] = "Stopped without replay, process termination or recovery acknowledgement. Inspect the queue and synthetic document before resuming."
        if client is not None:
            try:
                state = client.request("status")
                evidence["queue_after_failure"] = {key: state.get(key) for key in
                    ("gate", "recovery_required", "recovery_job_ids", "executor_busy", "fatal_gate")}
            except Exception:
                evidence["queue_after_failure"] = "unavailable"
        if output_owned and output.is_dir():
            save()
        print(json.dumps({"status": "failed", "actual_native": evidence["actual_native"],
                          "code": evidence["failure_code"],
                          "report": str(report) if report.exists() else None}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
