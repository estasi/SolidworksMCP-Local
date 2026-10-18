"""Passive Windows churn qualification using only short, owned Python children.

No CAD, MCP, queue state or user process is mutated. The raw CIM classifier is
run against the actual OS, then the corrected scanner must omit owned exited
objects while their Popen handles remain open. Evidence is never overwritten.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.coordination.ownership import process_identity
from solidworks_mcp.coordination.platform_guard import process_inventory_raw, legacy_processes
from solidworks_mcp.coordination.process_scan import coherent_inventory, inspect_process, safe_row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-only", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allowed-source", type=Path, required=True,
                        help="Currently legitimate MCP source path; observation only")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"format": 1, "passed": False, "native_calls_performed": False,
              "started_at": time.time(), "rounds": []}
    children = []
    owned = {}
    lock = threading.Lock()
    worker = None
    thread_errors = []
    try:
        if os.name != "nt":
            raise RuntimeError("WINDOWS_REQUIRED")
        python = getattr(sys, "_base_executable", sys.executable)
        def spawn():
            child = subprocess.Popen([python, "-I", "-S", "-c", "import time; time.sleep(0.06)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000, close_fds=True)
            token = process_identity(child.pid)["creation_token"]
            with lock:
                children.append(child)
                owned[child.pid] = token
        for _ in range(8):
            spawn()
        for child in children:
            if child.wait(timeout=5) != 0:
                raise RuntimeError("SYNTHETIC_CHILD_FAILED")
        def churn():
            try:
                for _ in range(40):
                    spawn()
                    time.sleep(.08)
            except Exception as exc:
                thread_errors.append(type(exc).__name__)
        worker = threading.Thread(target=churn, name="synthetic-process-churn")
        worker.start()
        for attempt in range(3):
            snapshot_evidence = []
            def raw():
                rows = process_inventory_raw()
                snapshot_evidence.append({"owned_exited_rows": [], "owned_live_rows": [],
                                          "selected_count": len(rows)})
                return rows
            def inspect(item):
                observation = inspect_process(item)
                with lock:
                    token = owned.get(item["pid"])
                if token:
                    if observation.get("creation_token") != token:
                        raise RuntimeError("OWNED_PROCESS_GENERATION_CHANGED")
                    if observation["status"] not in ("alive", "exited"):
                        raise RuntimeError("OWNED_CHILD_STATE_UNPROVEN")
                    key = "owned_exited_rows" if observation["status"] == "exited" else "owned_live_rows"
                    snapshot_evidence[-1][key].append(safe_row(item, observation["status"]))
                return observation
            corrected = coherent_inventory(raw, inspect)
            final_exited = {item["pid"] for item in snapshot_evidence[-1]["owned_exited_rows"]}
            if any(item["pid"] in final_exited for item in corrected):
                raise RuntimeError("EXITED_OWNED_PROCESS_RETAINED")
            final_live = {item["pid"] for item in snapshot_evidence[-1]["owned_live_rows"]}
            if not final_live.issubset({item["pid"] for item in corrected}):
                raise RuntimeError("LIVE_OWNED_PROCESS_DROPPED")
            report["rounds"].append({"round": attempt + 1,
                "raw_scans": snapshot_evidence, "corrected_owned_exited_rows": 0,
                "corrected_owned_live_rows": len(final_live),
                "legacy_count": len(legacy_processes(corrected, args.allowed_source))})
        worker.join(timeout=30)
        if worker.is_alive() or thread_errors:
            raise RuntimeError("SYNTHETIC_CHURN_FAILED")
        report["owned_children"] = len(children)
        report["observed_owned_exited_rows"] = sum(len(scan["owned_exited_rows"])
            for attempt in report["rounds"] for scan in attempt["raw_scans"])
        if report["observed_owned_exited_rows"] < 1:
            raise RuntimeError("EXITED_CIM_RACE_NOT_OBSERVED")
        report["passed"] = True
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        report["failure_code"] = getattr(exc, "code", str(exc))[:128]
    finally:
        # Do not terminate any process. Owned children are bounded short jobs;
        # release handles only once each child has actually finished.
        if worker:
            worker.join(timeout=30)
        for child in children:
            try:
                child.wait(timeout=30)
                child._handle.Close()
            except (OSError, subprocess.TimeoutExpired):
                report["passed"] = False
                report["cleanup_failed"] = True
        report["finished_at"] = time.time()
        path = args.output / "process-inventory-qualification.json"
        with path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2)
    print(json.dumps({"passed": report["passed"], "report": str(path),
                      "observed_owned_exited_rows": report.get("observed_owned_exited_rows", 0),
                      "native_calls_performed": False}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
