"""Opt-in native SW2025 qualification of two DISPOSABLE targeted documents.

Run from the activated .6 source and interpreter in an idle maintenance window.
Import/--help is COM-free. No activation, broker start, recovery acknowledgement,
process termination, or changes to existing working documents are performed.
Every attempt requires a new output directory; failures remain for inspection.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
import math
import multiprocessing
import ntpath
import os
from pathlib import Path
import queue
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import qualify_native_queue as base

BUILD = "2026.10.18.6"
OWNER_PIN_ENV = "SOLIDWORKS_MCP_EXPECTED_OWNER_ID"
Failure = base.QualificationFailure


def _key(path):
    return ntpath.normcase(ntpath.normpath(str(path)))


def _identity_code(target):
    return "import ntpath\n" + f"_qualification_target = {target!r}\n" + r'''
def qualification_identity():
    active = com_get(sw, "ActiveDoc")
    state = automation._strict_document_state(active)
    bound = automation._strict_document_state(doc)
    if (state != bound or
        ntpath.normcase(ntpath.normpath(state["path"])) != ntpath.normcase(ntpath.normpath(_qualification_target["path"])) or
        state["type"] != 1 or state["configuration"] != _qualification_target["configuration"]):
        raise RuntimeError("QUALIFICATION_WRONG_ACTIVE_DOCUMENT")
    return state
identity_before = qualification_identity()
'''


def _seed_code(target):
    return _identity_code(target) + base._seed_script() + r'''
metrics["identity_before"] = identity_before
metrics["identity_after"] = qualification_identity()
result = json.dumps(metrics, allow_nan=False)
'''


def _increment_code(target, label, delay):
    return _identity_code(target) + base._increment_script(label, delay) + r'''
metrics["identity_before"] = identity_before
metrics["identity_after"] = qualification_identity()
if metrics["identity_after"]["dirty"]:
    raise RuntimeError("QUALIFICATION_SAVE_LEFT_DIRTY_DOCUMENT")
result = json.dumps(metrics, allow_nan=False)
'''


def _verification_code(target):
    return _identity_code(target) + base._SCRIPT_HELPERS + r'''
metrics = snapshot()
metrics["identity_before"] = identity_before
metrics["identity_after"] = qualification_identity()
result = json.dumps(metrics, allow_nan=False)
'''


def _check_identity(metrics, target):
    for label in ("identity_before", "identity_after"):
        state = metrics.get(label) or {}
        if (_key(state.get("path", "")) != _key(target["path"]) or
                state.get("type") != 1 or state.get("configuration") != target["configuration"] or
                state.get("dirty") is not False):
            raise Failure("NATIVE_TARGET_IDENTITY_OR_CLEAN_STATE_MISMATCH")


def _plan_arguments(plan_id, expected_depth):
    # The public plan contract saves after success. Only owned disposable
    # parts are passed to this plan; its sole step reads geometry.
    return {"plan_id": plan_id,
        "operations": [{"op": "body_volume", "args": {"unit": "m"}}],
        "transaction": {"checkpoint_before": False, "rollback_on_failure": False},
        "invariants": {"solid_body_count": 1,
            "volume_m3": base.BASE_DEPTH_M**2 * expected_depth,
            "no_modal_dialog": True},
        "allow_unsaved_document": False}


def _check_plan(result, expected_depth):
    data = result.get("data") or {}
    selection = data.get("document_selection") or {}
    steps = data.get("steps") or []
    if (result.get("success") is not True or data.get("committed") is not True or
            data.get("invariants") != "passed" or
            (data.get("save_result") or {}).get("success") is not True or
            selection.get("selected") is not True or
            selection.get("document_type") != 1 or
            selection.get("configuration_verified") is not True or
            len(steps) != 1 or steps[0].get("success") is not True):
        raise Failure("ORDINARY_TARGETED_PLAN_NOT_COMMITTED")
    step = steps[0].get("data") or {}
    bodies = step.get("bodies") or []
    if (step.get("operation") != "body_volume" or step.get("unit") != "m" or
            len(bodies) != 1 or not isinstance(bodies[0].get("volume_exact"), (int, float)) or
            not math.isclose(bodies[0]["volume_exact"], base.BASE_DEPTH_M**2 * expected_depth,
                             abs_tol=1e-15, rel_tol=1e-8)):
        raise Failure("ORDINARY_TARGETED_PLAN_GEOMETRY_MISMATCH")


def _check_preserved(before, after, owned_paths, *, final=False):
    """Original states are exact; only explicitly owned synthetic additions vary."""
    if before.get("strict") is not True or after.get("strict") is not True:
        raise Failure("STRICT_DOCUMENT_INVENTORY_REQUIRED")
    old = {x["key"]: x for x in before["documents"]}
    current = {x["key"]: x for x in after["documents"]}
    owned = {_key(p) for p in owned_paths}
    if (len(old) != before["count"] or len(current) != after["count"] or
            before.get("session") != after.get("session") or set(old) & owned or
            set(current) != set(old) | owned or
            any(current.get(k) != v for k, v in old.items()) or
            any(current[k].get("type") != 1 or current[k].get("dirty") is not False for k in owned)):
        raise Failure("WORKING_DOCUMENT_STATE_CHANGED")
    if final and after.get("active_document") != before.get("active_document"):
        raise Failure("ORIGINAL_ACTIVE_DOCUMENT_NOT_RESTORED")


def _owned_target(path, output, owned):
    """Never derive cleanup authority from the current active document."""
    key = _key(path)
    root = output.resolve()
    candidate = Path(path).resolve()
    if (key not in owned or not candidate.is_relative_to(root) or
            candidate.parent != root or candidate.suffix.lower() != ".sldprt"):
        raise Failure("CLEANUP_PATH_NOT_OWNED")
    return dict(owned[key])


def _operation(client, tool, arguments, request_id, phase, *, target=None,
               expected=None, trusted=False, timeout=180):
    if target is not None and expected is not None:
        raise Failure("HARNESS_CONFLICTING_DOCUMENT_GUARDS")
    arguments = dict(arguments)
    metadata = {}
    if target is not None:
        metadata["target_document"] = target
    if expected is not None:
        metadata["expected_document"] = expected
    if trusted:
        metadata["trusted_python"] = True
    if metadata:
        arguments["_coordination"] = metadata
    job = client.submit(tool, arguments, request_id=request_id,
                        queue_timeout_sec=timeout, execution_timeout_sec=timeout)
    return _finish_job(client, job, phase, timeout)


def _finish_job(client, job, phase, timeout):
    deadline = time.monotonic() + timeout + 30
    while job["state"] in {"queued", "running"} and time.monotonic() < deadline:
        job = client.wait(job["id"], timeout=min(10, max(.01, deadline-time.monotonic())))
    summary = base._summary(job, phase)
    summary["queued_at"] = job.get("queued_at", job.get("created_at"))
    if job["state"] != "completed":
        summary.update(native_result=job.get("result"), journal_reason=job.get("reason"))
        raise Failure("NATIVE_JOB_" + job["state"].upper(), summary)
    *_, unwrap = base._imports()
    result = unwrap(job["result"])
    if result.get("success") is not True:
        summary["native_result"] = job.get("result")
        raise Failure("NATIVE_OPERATION_FAILED", summary)
    summary["success"] = True
    summary["document_selection"] = (result.get("data") or {}).get("document_selection")
    return result, summary


def _payload(response):
    return json.loads(next(item.text for item in response.content if item.type == "text"))


def _frontend_env(owner_id):
    return {**os.environ, OWNER_PIN_ENV: owner_id,
            "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}


def _exception_details(exc):
    """Preserve concrete native failures hidden by async context-manager groups."""
    leaves = []
    def visit(error, path):
        if isinstance(error, BaseExceptionGroup):
            for index, child in enumerate(error.exceptions):
                visit(child, path + [index])
        else:
            leaves.append({"type": type(error).__name__, "group_path": path,
                "code": getattr(error, "code", type(error).__name__),
                "job": getattr(error, "job", None),
                "qualification_failure": isinstance(error, Failure)})
    visit(exc, [])
    primary = next((x for x in leaves if x["qualification_failure"] and x["job"]),
                   next((x for x in leaves if x["qualification_failure"]), leaves[0]))
    return {"wrapper_type": type(exc).__name__, "code": primary["code"],
            "job": primary["job"], "leaves": leaves}


def _retain_job(evidence, job):
    """Keep one latest summary per durable job; events retain earlier evidence."""
    if not job:
        return
    jobs = evidence.setdefault("jobs", [])
    for index, old in enumerate(jobs):
        if old.get("job_id") == job.get("job_id"):
            if (old.get("state") in {"completed", "failed", "cancelled", "recovery_required"} and
                    job.get("state") in {"queued", "running"}):
                return
            jobs[index] = job
            break
    else:
        jobs.append(job)


def _record_client_event(evidence, message, operations):
    evidence.setdefault("client_events", []).append(message)
    if message["kind"] == "failure":
        _retain_job(evidence, message.get("job"))
        for leaf in (message.get("exception") or {}).get("leaves", []):
            _retain_job(evidence, leaf.get("job"))
    elif message["kind"] == "completed":
        _retain_job(evidence, message["job"])
        operations.append(message)


def _save_failure_evidence(evidence, save):
    """A diagnostic write failure must not replace the original CAD failure."""
    try:
        save()
    except BaseException as exc:
        details = _exception_details(exc)
        errors = evidence.setdefault("failure_evidence_write_errors", [])
        for error in errors:
            if (error["exception"]["code"], error["exception"]["wrapper_type"]) == (details["code"], details["wrapper_type"]):
                error["count"] += 1
                break
        else:
            errors.append({"exception": details, "count": 1})
        return False
    return True


def _observe_dispatched_jobs(client, scheduled, evidence, save, *, timeout,
                             drain=lambda: None, workers_closed=lambda: True,
                             not_submitted_ids=lambda: set()):
    """Only read the pinned journal after failure; never dispatch or recover CAD."""
    observation = {"read_only": True, "owner_pinned": False, "complete": False,
                   "native_outcomes_known": False,
                   "requests": {}, "errors": []}
    evidence["jobs_after_failure"] = observation
    owner = evidence["broker_owner_id"]
    if client.auto_start is not False or client.expected_owner_id != owner:
        observation["errors"].append({"code": "FAILURE_OBSERVER_NOT_PINNED"})
        _save_failure_evidence(evidence, save)
        return
    observation["owner_pinned"] = True
    deadline = time.monotonic() + timeout
    by_id = {item["request_id"]: item for item in scheduled}
    terminal = {"completed", "failed", "cancelled", "recovery_required"}
    while True:
        drain()
        for request_id, item in by_id.items():
            try:
                job = client.request("lookup", {"request_id": request_id})
            except Exception as exc:
                details = _exception_details(exc)
                if details["code"] == "JOB_NOT_FOUND":
                    # Closing a frontend does not prove that an in-flight
                    # broker handler has finished its durable admission.
                    admission = "not_submitted" if request_id in not_submitted_ids() else "not_observed"
                    observation["requests"][request_id] = {"admission": admission}
                    continue
                observation["errors"].append(details)
                _save_failure_evidence(evidence, save)
                return  # An owner/transport failure must not trigger a restart/retry.
            summary = base._summary(job, item["phase"])
            summary.update(queued_at=job.get("queued_at", job.get("created_at")),
                           native_result=job.get("result"), journal_reason=job.get("reason"))
            observation["requests"][request_id] = {"admission": "confirmed", "job": job}
            _retain_job(evidence, summary)
        closed = workers_closed()
        settled = all(x["admission"] == "not_submitted" or
                      (x["admission"] == "confirmed" and x["job"]["state"] in terminal)
                      for x in observation["requests"].values())
        observation["workers_closed"] = closed
        observation["complete"] = closed and settled and len(observation["requests"]) == len(by_id)
        observation["native_outcomes_known"] = observation["complete"] and all(
            x["admission"] == "not_submitted" or
            (x["admission"] == "confirmed" and x["job"]["state"] != "recovery_required")
            for x in observation["requests"].values())
        _save_failure_evidence(evidence, save)
        if observation["complete"]:
            return
        if time.monotonic() >= deadline:
            observation["errors"].append({"code": "FAILURE_OBSERVATION_DEADLINE", "outcome": "unknown_or_still_pending"})
            _save_failure_evidence(evidence, save)
            return
        time.sleep(.1)


def _stdio_worker(index, run_id, target, timeout, delay, commands, output, log_path, owner_id, stop):
    """Independent OS client process, each owning a real MCP stdio frontend."""
    # The production client's owner pin disables auto-start and rejects a
    # replacement endpoint on every request, including MCP frontend admission.
    os.environ[OWNER_PIN_ENV] = owner_id
    async def run():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        QueueClient, *_ = base._imports()
        control = QueueClient(auto_start=False)
        params = StdioServerParameters(command=sys.executable,
            args=["-u", str(ROOT / "solidworks_mcp_server.py")], cwd=str(ROOT),
            env=_frontend_env(owner_id))
        with open(log_path, "x", encoding="utf8") as log:
            async with stdio_client(params, errlog=log) as streams:
                async with ClientSession(*streams,
                        read_timeout_seconds=timedelta(seconds=timeout+60)) as session:
                    initialized = await session.initialize()
                    if initialized.serverInfo.version != BUILD or initialized.serverInfo.name != "SolidworksMCP":
                        raise Failure("MCP_HANDSHAKE_MISMATCH")
                    catalog = await session.list_tools()
                    python_tool = next(t for t in catalog.tools if t.name == "execute_python")
                    coordination = python_tool.inputSchema["properties"]["coordination"]["properties"]
                    if "target_document" not in coordination:
                        raise Failure("TARGET_DOCUMENT_NOT_ADVERTISED")
                    status = _payload(await session.call_tool("get_queue_status", {}))
                    if status.get("success") is not True or status["data"]["backend"] != "native":
                        raise Failure("MCP_NATIVE_QUEUE_UNAVAILABLE")
                    output.put({"kind": "ready", "worker": index, "client_pid": os.getpid(),
                        "version": initialized.serverInfo.version, "server_name": initialized.serverInfo.name,
                        "owner_id": status["data"]["owner_id"]})
                    while True:
                        command = await asyncio.to_thread(commands.get, True, timeout+90)
                        if command is None:
                            break
                        label = f"model-{index}-step-{command}"
                        request_id = run_id + "-" + label
                        if stop.is_set():
                            output.put({"kind": "not_submitted", "worker": index,
                                "client_pid": os.getpid(), "request_id": request_id,
                                "reason": "stopped_before_call_tool"})
                            break
                        answer = _payload(await session.call_tool("execute_python", {
                            "code": _increment_code(target, label, delay),
                            "coordination": {"target_document": target, "trusted_python": True,
                                "request_id": request_id, "wait_timeout_sec": 0,
                                "queue_timeout_sec": 2*timeout, "execution_timeout_sec": timeout}}))
                        # Resolve only this submitted ID, never repeat a mutation with a new ID.
                        job = control.request("lookup", {"request_id": request_id})
                        output.put({"kind": "admitted", "worker": index, "client_pid": os.getpid(),
                            "step": command, "job": base._summary(job, label), "mcp_reply": answer})
                        result, summary = await asyncio.to_thread(_finish_job, control, job, label, 2*timeout)
                        output.put({"kind": "completed", "worker": index, "client_pid": os.getpid(),
                            "step": command, "job": summary, "metrics": base._metrics(result)})
    try:
        asyncio.run(run())
    except BaseException as exc:
        stop.set()
        details = _exception_details(exc)
        output.put({"kind": "failure", "worker": index, "client_pid": os.getpid(),
                    "code": details["code"], "job": details["job"], "exception": details})
    finally:
        output.put({"kind": "closed", "worker": index, "client_pid": os.getpid()})


def _check_operations(operations, targets):
    if len(operations) != 6 or len({x["client_pid"] for x in operations}) != 2:
        raise Failure("TWO_CLIENT_SIX_OPERATION_PROOF_MISSING")
    ordered = sorted(operations, key=lambda x: x["job"]["seq"])
    if [x["worker"] for x in ordered] != [0, 1, 0, 1, 0, 1]:
        raise Failure("ALTERNATING_FIFO_NOT_PROVEN")
    overlap = False
    for index, item in enumerate(ordered):
        metrics, job = item["metrics"], item["job"]
        expected_before = base.BASE_DEPTH_M + (index//2)*base.INCREMENT_M
        _check_identity(metrics, targets[item["worker"]])
        base._check_geometry(metrics, expected_before + base.INCREMENT_M)
        if not math.isclose(metrics["before_m"], expected_before, abs_tol=1e-9, rel_tol=0):
            raise Failure("TARGETED_INCREMENT_LOST_OR_WRONG_MODEL")
        if metrics["ended_ns"] < metrics["started_ns"] or job["state"] != "completed":
            raise Failure("NATIVE_TIME_OR_JOB_INVALID")
        if not (job.get("document_selection") or {}).get("selected"):
            raise Failure("PRODUCTION_TARGET_SELECTION_NOT_PROVEN")
        if index:
            prior = ordered[index-1]
            if (prior["metrics"]["ended_ns"] > metrics["started_ns"] or
                    prior["job"]["finished_at"] > job["started_at"]):
                raise Failure("NATIVE_OPERATIONS_OVERLAPPED")
            overlap |= job["queued_at"] < prior["job"]["finished_at"]
    if not overlap:
        raise Failure("CONCURRENT_QUEUE_PRESSURE_NOT_OBSERVED")
    return ordered


def _two_stdio_clients(run_id, targets, timeout, delay, output_dir, evidence, save):
    ctx = multiprocessing.get_context("spawn")
    channel = ctx.Queue()
    commands = [ctx.Queue(), ctx.Queue()]
    stop = ctx.Event()
    children = [ctx.Process(target=_stdio_worker, args=(i, run_id, targets[i], timeout,
        delay, commands[i], channel, str(output_dir/f"mcp-client-{i}.log"),
        evidence["broker_owner_id"], stop)) for i in range(2)]
    pending = []
    operations = []
    original_failure = None
    def receive(kind, worker=None):
        deadline = time.monotonic()+2*timeout+90
        while time.monotonic() < deadline:
            for i, message in enumerate(pending):
                if message["kind"] == kind and (worker is None or message["worker"] == worker):
                    return pending.pop(i)
            try:
                message = channel.get(timeout=.5)
            except queue.Empty:
                if any(c.exitcode not in (None, 0) for c in children):
                    raise Failure("MCP_WORKER_EXITED")
                continue
            _record_client_event(evidence, message, operations)
            if message["kind"] == "failure":
                _save_failure_evidence(evidence, save)
                raise Failure(message["code"], message.get("job"))
            save()
            pending.append(message)
        raise Failure("MCP_WORKER_OBSERVATION_TIMEOUT")
    try:
        for child in children:
            child.start()
        ready = [receive("ready", i) for i in range(2)]
        if ({x["owner_id"] for x in ready} != {evidence["broker_owner_id"]} or
                len({x["client_pid"] for x in ready}) != 2):
            raise Failure("MCP_CLIENTS_DID_NOT_SHARE_BROKER")
        evidence["mcp_handshakes"] = ready
        for step in range(3):
            for worker in range(2):
                if stop.is_set():
                    # Consume the queued failure to retain its concrete cause.
                    receive("failure")
                    raise Failure("MCP_WORKER_STOPPED")
                phase = f"model-{worker}-step-{step}"
                evidence.setdefault("scheduled_requests", []).append({"worker": worker,
                    "step": step, "phase": phase, "request_id": run_id + "-" + phase})
                save()
                commands[worker].put(step)
                receive("admitted", worker)
            receive("completed", 0)
            receive("completed", 1)
        evidence["targeted_operations"] = _check_operations(operations, targets)
        for q in commands:
            q.put(None)
        for i in range(2):
            receive("closed", i)
        for child in children:
            child.join(20)
            if child.exitcode != 0:
                raise Failure("MCP_WORKER_DID_NOT_CLOSE")
        evidence["checks"].update(two_real_stdio_clients=True, exact_release_handshakes=True,
            six_alternating_targeted_operations=True, fifo=True, whole_operation_non_overlap=True,
            concurrent_queue_pressure=True, per_model_no_lost_increments=True)
        save()
    except BaseException as exc:
        original_failure = exc
        evidence["two_client_failure"] = _exception_details(exc)
        _save_failure_evidence(evidence, save)
        try:
            stop.set()
            for command in commands:
                command.put(None)
        except BaseException as stop_error:
            evidence["failure_stop_signal_error"] = _exception_details(stop_error)
        def drain():
            while True:
                try:
                    message = channel.get_nowait()
                except queue.Empty:
                    return
                _record_client_event(evidence, message, operations)
        def workers_closed():
            closed = {x["worker"] for x in evidence.get("client_events", []) if x["kind"] == "closed"}
            return all(index in closed or child.pid is None for index, child in enumerate(children))
        def not_submitted_ids():
            return {x["request_id"] for x in evidence.get("client_events", [])
                    if x["kind"] == "not_submitted" and x.get("reason") == "stopped_before_call_tool"}
        try:
            QueueClient, *_ = base._imports()
            observer = QueueClient(auto_start=False, expected_owner_id=evidence["broker_owner_id"])
            _observe_dispatched_jobs(observer, evidence.get("scheduled_requests", []), evidence,
                save, timeout=2*timeout+30, drain=drain, workers_closed=workers_closed,
                not_submitted_ids=not_submitted_ids)
        except BaseException as observation_error:
            evidence["failure_observer_error"] = _exception_details(observation_error)
        _save_failure_evidence(evidence, save)
        raise  # Observation never replaces the original failure or resumes CAD.
    finally:
        # Requests already admitted remain owned by the broker. Never terminate
        # workers/broker/CAD or force a cleanup after an uncertain native result.
        try:
            for q in commands:
                q.put(None)
            for child in children:
                if child.pid is not None:
                    child.join(1)
            for q in commands:
                q.close()
            channel.close()
        except BaseException as close_error:
            if original_failure is None:
                raise
            evidence["failure_handle_close_error"] = _exception_details(close_error)
            _save_failure_evidence(evidence, save)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acknowledge-idle-session", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--operation-timeout-sec", type=int, default=180)
    parser.add_argument("--delay-sec", type=float, default=.5)
    args = parser.parse_args(argv)
    if not args.acknowledge_idle_session:
        parser.error("--acknowledge-idle-session required; no native actions performed")
    if not 30 <= args.operation_timeout_sec <= 600 or not math.isfinite(args.delay_sec) or not .05 <= args.delay_sec <= 2:
        parser.error("Timeout must be 30..600 seconds and delay .05..2 seconds")
    output = args.output_dir.expanduser().absolute()
    if output.exists():
        parser.error("Output directory must be new; previous evidence is never overwritten")
    QueueClient, atomic_json, default_state_dir, doctor, private_directory, validate_activation, _ = base._imports()
    from solidworks_mcp import __version__
    report = output / "native-document-targets-qualification.json"
    evidence = {"format": 1, "qualification": "native_sw2025_two_document_targets", "build": BUILD,
        "status": "not_started", "actual_native": False, "run_id": uuid.uuid4().hex,
        "jobs": [], "checks": {}, "fault_injection": "not_performed", "owned_models": []}
    owned = {}
    client = None
    reserved = False
    previous_owner_pin = os.environ.get(OWNER_PIN_ENV)
    def save():
        evidence["updated_at"] = time.time()
        atomic_json(report, evidence)
    def operation(tool, arguments, phase, **kwargs):
        evidence["phase"] = phase
        save()
        try:
            result, job = _operation(client, tool, arguments, evidence["run_id"]+"-"+phase,
                                    phase, timeout=args.operation_timeout_sec, **kwargs)
        except Failure as exc:
            if exc.job:
                evidence["jobs"].append(exc.job)
                evidence["actual_native"] |= bool(exc.job.get("native_target_pid"))
            raise
        evidence["jobs"].append(job)
        evidence["actual_native"] |= bool(job.get("native_target_pid"))
        save()
        return result
    def inventory(phase):
        return operation("list_open_documents", {"strict": True}, phase)["data"]
    try:
        output.mkdir(parents=False, exist_ok=False)
        reserved = True
        private_directory(output)
        save()
        if __version__ != BUILD:
            raise Failure("HARNESS_SOURCE_VERSION_MISMATCH")
        if doctor()["legacy_clients"]:
            raise Failure("LEGACY_MCP_CLIENTS_STILL_RUNNING")
        activation = validate_activation(default_state_dir())
        if activation["expected_major"] != 33:
            raise Failure("ACTIVATION_IS_NOT_SW2025")
        client = QueueClient(auto_start=False)
        initial = client.request("status")
        endpoint = client._endpoint()
        if (initial["backend"] != "native" or initial["gate"] != "open" or
                initial["recovery_required"] or initial.get("fatal_gate") or initial["executor_busy"] or
                initial["counts"]["queued"] or not initial["accepting"]):
            raise Failure("NATIVE_QUEUE_NOT_IDLE_AND_OPEN")
        if endpoint.get("owner_id") != initial["owner_id"]:
            raise Failure("BROKER_CHANGED_DURING_PREFLIGHT")
        os.environ[OWNER_PIN_ENV] = initial["owner_id"]
        client = QueueClient(auto_start=False)
        # Fail before the first CAD inventory call if this source does not
        # support the real frontend's attach-only owner pin.
        if (getattr(client, "expected_owner_id", None) != initial["owner_id"] or
                client.auto_start is not False or client._endpoint() != endpoint):
            raise Failure("BROKER_OWNER_PIN_NOT_ENFORCED")
        evidence.update(status="running", broker_owner_id=initial["owner_id"],
            broker_process=endpoint["process"], target_pid=activation["target_pid"],
            target_created_at=activation["target_created_at"])
        prior = inventory("initial_inventory")
        evidence["initial_inventory"] = prior
        base._preservation_preflight(prior)
        active = prior.get("active_document")
        if prior["count"] and (not active or not active["path"] or active["dirty"]):
            raise Failure("INITIAL_ACTIVE_DOCUMENT_NOT_SAVED_AND_CLEAN")
        evidence["working_files_before"] = base._working_file_evidence(prior)
        save()
        if active:
            # Additional read-only safety preconditions before creating a new
            # window; these are not the target-selection acceptance oracle.
            code = '''from solidworks_mcp.coordination.document_target import _verify_no_running_command, _verify_edit_state
_verify_no_running_command(sw)
state = automation._strict_document_state(doc)
_verify_edit_state(doc, state)
result = json.dumps({"preflight_only": True})
'''
            operation("execute_python", {"code": code}, "initial_edit_safety",
                      expected={"path": active["path"], "document_type": active["type"]}, trusted=True)
        targets = []
        for index in range(2):
            operation("create_new_part", {}, f"create_model_{index}")
            info = operation("get_document_info", {}, f"new_model_{index}_identity")["data"]
            if info.get("type_code") != 1 or info.get("path") not in (None, "", "Not saved") or not info.get("title"):
                raise Failure("CREATED_DOCUMENT_IDENTITY_UNEXPECTED")
            path = output / f"target-{index}-{evidence['run_id']}.SLDPRT"
            if path.exists():
                raise Failure("OWNED_PATH_ALREADY_EXISTS")
            unsaved = {"title": info["title"], "allow_unsaved_document": True, "document_type": 1}
            operation("save_document", {"filepath": str(path)}, f"save_new_model_{index}", expected=unsaved)
            current = inventory(f"saved_model_{index}_inventory")
            state = current["active_document"]
            if _key(state["path"]) != _key(path) or state["type"] != 1 or not state["configuration"] or state["dirty"]:
                raise Failure("SAVED_TARGET_IDENTITY_MISMATCH")
            target = {"path": str(path), "document_type": 1, "configuration": state["configuration"]}
            targets.append(target)
            owned[_key(path)] = target
            evidence["owned_models"] = targets
            _check_preserved(prior, current, [x["path"] for x in targets])
            metrics = base._metrics(operation("execute_python", {"code": _seed_code(target)},
                                      f"seed_model_{index}", target=target, trusted=True))
            base._check_geometry(metrics, base.BASE_DEPTH_M)
            _check_identity(metrics, target)
            evidence.setdefault("seed_metrics", []).append(metrics)
        evidence["phase"] = "two_real_mcp_clients"
        save()
        _two_stdio_clients(evidence["run_id"], targets, args.operation_timeout_sec,
                           args.delay_sec, output, evidence, save)
        expected_depth = base.BASE_DEPTH_M + 3*base.INCREMENT_M
        final_metrics = []
        for index, target in enumerate(targets):
            path = target["path"]
            checked = _owned_target(path, output, owned)
            plan_id = evidence["run_id"] + f"-ordinary-plan-{index}"
            plan = operation("execute_cad_plan", _plan_arguments(plan_id, expected_depth),
                             f"ordinary_targeted_plan_{index}", target=checked)
            _check_plan(plan, expected_depth)
            evidence.setdefault("ordinary_targeted_plans", []).append({"target": checked, "result": plan})
            final = base._metrics(operation("execute_python", {"code": _verification_code(checked)},
                                          f"verify_model_{index}", target=checked, trusted=True))
            base._check_geometry(final, expected_depth)
            _check_identity(final, checked)
            operation("close_document", {"save": False}, f"close_owned_model_{index}", target=checked)
            operation("open_document", {"filepath": path}, f"reopen_owned_model_{index}")
            reopened = base._metrics(operation("execute_python", {"code": _verification_code(checked)},
                                             f"verify_reopened_model_{index}", target=checked, trusted=True))
            base._check_geometry(reopened, expected_depth)
            _check_identity(reopened, checked)
            base._check_reopened_geometry(final, reopened)
            final_metrics.append({"target": checked, "final": final, "reopened": reopened})
        before_cleanup = inventory("inventory_before_cleanup")
        _check_preserved(prior, before_cleanup, [x["path"] for x in targets])
        for index, target in enumerate(targets):
            checked = _owned_target(target["path"], output, owned)
            operation("close_document", {"save": False}, f"final_close_owned_model_{index}", target=checked)
        if active:
            original = {"path": active["path"], "document_type": active["type"]}
            if active["configuration"] is not None:
                original["configuration"] = active["configuration"]
            operation("get_document_info", {}, "restore_original_active_model", target=original)
        after = inventory("final_inventory")
        evidence["final_inventory"] = after
        _check_preserved(prior, after, [], final=True)
        evidence["working_files_after"] = base._working_file_evidence(prior)
        if evidence["working_files_after"] != evidence["working_files_before"]:
            raise Failure("WORKING_FILES_CHANGED")
        status = client.request("status")
        if (status["owner_id"] != initial["owner_id"] or client._endpoint()["process"] != endpoint["process"] or
                status["gate"] != "open" or status["recovery_required"] or status["executor_busy"] or status["counts"]["queued"]):
            raise Failure("BROKER_NOT_PRESERVED_AND_IDLE")
        if status["total_jobs"] != initial["total_jobs"]+len(evidence["jobs"]):
            raise Failure("UNEXPECTED_OTHER_CLIENT_JOB")
        if any(j.get("native_target_pid") != activation["target_pid"] or
               j.get("native_target_created_at") != activation["target_created_at"] for j in evidence["jobs"]):
            raise Failure("CAD_GENERATION_CHANGED")
        evidence["geometry"] = final_metrics
        evidence["checks"].update(native_SW2025=True, per_model_saved_reopened_geometry=True,
            two_ordinary_targeted_CAD_plans=True,
            all_owned_test_parts_closed=True, original_active_restored=True,
            original_document_states_preserved=True, original_file_hashes_stats_preserved=True,
            broker_survived_stdio_closure=True, same_CAD_generation=True, no_external_jobs=True)
        evidence.update(status="passed", phase="complete")
        save()
        print(json.dumps({"status": "passed", "actual_native": True, "report": str(report)}))
        return 0
    except BaseException as exc:
        details = _exception_details(exc)
        evidence.update(status="failed", failure_code=details["code"], failure_details=details,
            note="No replay, forced close, process termination or recovery acknowledgement. Inspect retained test parts and queue before resuming.")
        for leaf in details["leaves"]:
            _retain_job(evidence, leaf.get("job"))
        if client is not None:
            try:
                status = client.request("status")
                evidence["queue_after_failure"] = {k: status.get(k) for k in ("gate", "recovery_required", "executor_busy", "fatal_gate")}
            except Exception:
                evidence["queue_after_failure"] = "unavailable"
        if reserved:
            _save_failure_evidence(evidence, save)
        print(json.dumps({"status": "failed", "code": evidence["failure_code"],
                          "actual_native": evidence["actual_native"], "report": str(report) if reserved else None}))
        return 1
    finally:
        if previous_owner_pin is None:
            os.environ.pop(OWNER_PIN_ENV, None)
        else:
            os.environ[OWNER_PIN_ENV] = previous_owner_pin


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
