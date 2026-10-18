"""Independent broker bootstrap; verify lifetime before importing the executor.

Windows nested jobs can retain an outer desktop job. A broker must either have
no job or prove it was launched by the existing same-user Explorer, independently
of the MCP frontend. No attached fallback is permitted.
"""
from __future__ import annotations

import os
import sys

from .protocol import QueueError

EXIT_LIFETIME_UNVERIFIED = 78
_verified_desktop = None


def _accept_desktop_proof(proof):
    global _verified_desktop
    # Called only by the fixed bootstrap after validate_ticket has checked the
    # real parent, same-user desktop and physical journal. Not an env override.
    from .ownership import process_identity
    if proof.get("mode") != "desktop" or proof.get("process") != process_identity():
        raise QueueError("DESKTOP_LAUNCH_UNVERIFIED", "Invalid desktop process proof")
    _verified_desktop = dict(proof)


def lifetime_proof():
    from .ownership import process_identity
    retained = False
    if os.name == "nt":
        import win32api
        import win32job
        retained = bool(win32job.IsProcessInJob(win32api.GetCurrentProcess(), None))
    if _verified_desktop is not None:
        return {**_verified_desktop, "retained_os_job": retained}
    return {"mode": "no_job" if not retained else "unverified", "process": process_identity(),
            "retained_os_job": retained}


def require_independent_lifetime():
    if os.name != "nt":
        return
    try:
        import win32api
        import win32job
        attached = win32job.IsProcessInJob(win32api.GetCurrentProcess(), None)
    except Exception as exc:
        raise QueueError("BROKER_LIFETIME_UNVERIFIED",
                         "Cannot verify independent broker process lifetime") from exc
    if attached:
        if _verified_desktop is not None:
            from .desktop_launch import verify_explorer
            from .ownership import process_identity, user_session_identity
            if (_verified_desktop["process"] == process_identity()
                    and _verified_desktop["scope"] == user_session_identity()
                    and _verified_desktop["explorer"] == verify_explorer(os.getppid())):
                return
        raise QueueError("BROKER_PARENT_JOB_RETAINED",
                         "Broker remains in a job without verified desktop provenance; no queue or CAD was opened")


def main(argv=None):
    if "--desktop" in (argv if argv is not None else sys.argv[1:]):
        import argparse
        from .desktop_launch import launch_desktop
        from .platform_guard import default_state_dir, private_directory
        parser = argparse.ArgumentParser(description="Start an independent shared CAD dispatcher")
        parser.add_argument("--desktop", action="store_true")
        parser.add_argument("--backend", choices=["native", "test"], default="native")
        parser.add_argument("--state-dir")
        args = parser.parse_args(argv)
        from .client import QueueClient
        client = QueueClient(args.state_dir or default_state_dir(), backend=args.backend)
        try:
            endpoint = client.ensure_broker(desktop=True)
            print("Broker ready: " + endpoint["owner_id"])
            return 0
        except QueueError as exc:
            print(exc.code, file=sys.stderr)
            return EXIT_LIFETIME_UNVERIFIED
    try:
        require_independent_lifetime()
    except QueueError as exc:
        print(exc.code, file=sys.stderr)
        return EXIT_LIFETIME_UNVERIFIED
    # Deliberately deferred until the independent-lifetime check succeeds.
    from .broker import main as broker_main
    return broker_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
