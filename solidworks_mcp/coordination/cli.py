"""Non-COM operations console. Nothing here kills CAD or rewrites MCP config."""
from __future__ import annotations

import argparse
import json
import os
import time

from .client import QueueClient
from .ownership import ProcessLease, process_identity
from .platform_guard import (atomic_json, default_state_dir, doctor, legacy_processes,
                             private_directory, process_inventory, source_root)
from .protocol import QueueError


def activate(args):
    if not args.acknowledge_idle_session:
        raise QueueError("IDLE_ACK_REQUIRED", "Finish CAD work and explicitly acknowledge the idle session")
    if args.expected_major != 33:
        raise QueueError("VERSION_NOT_QUALIFIED", "This deployment targets SolidWorks 2025")
    inventory = process_inventory()
    blockers = legacy_processes(inventory)
    if blockers:
        raise QueueError("LEGACY_CLIENTS_PRESENT", f"{len(blockers)} legacy MCP process(es) remain")
    target = next((p for p in inventory if p["name"].lower() == "sldworks.exe" and p["pid"] == args.target_pid), None)
    if target is None:
        raise QueueError("CAD_NOT_FOUND", "The selected SolidWorks process is absent")
    # Activation cannot retarget a live owner, including an idle broker with a
    # still-bound COM connection. Drain/shutdown it first.
    lease = ProcessLease("solidworks-cad")
    if not lease.acquire(timeout=0):
        raise QueueError("BROKER_RUNNING", "Drain and stop the shared broker before activation")
    try:
        state = private_directory(default_state_dir())
        identity = process_identity(target["pid"])
        activation = {"format": 1, "enabled": True, "target_pid": target["pid"],
                      "target_created_at": identity["created_at"],
                      "target_creation_token": identity["creation_token"], "expected_major": 33,
                      "source_root": str(source_root()), "activated_at": time.time()}
        atomic_json(state / "activation.json", activation)
    finally:
        lease.release()
    return {"activated": True, "target_pid": target["pid"], "native_verification_pending": True,
            "mcp_config_changed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("status")
    sub.add_parser("shutdown")
    command = sub.add_parser("activate")
    command.add_argument("--target-pid", required=True, type=int)
    command.add_argument("--expected-major", type=int, default=33)
    command.add_argument("--acknowledge-idle-session", action="store_true")
    command = sub.add_parser("recover")
    command.add_argument("--job-id", action="append", required=True)
    command.add_argument("--operator", required=True)
    command.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "activate":
            result = activate(args)
        else:
            client = QueueClient(auto_start=False)
            if args.command == "recover":
                result = client.request("recover", {"expected_job_ids": args.job_id,
                    "operator": args.operator, "note": args.note})
            else:
                result = client.request(args.command)
        print(json.dumps({"success": True, "data": result}, ensure_ascii=False, indent=2))
        return 0
    except (QueueError, OSError, ValueError) as exc:
        print(json.dumps({"success": False, "code": getattr(exc, "code", type(exc).__name__),
                          "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
