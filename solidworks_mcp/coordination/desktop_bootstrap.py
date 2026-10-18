"""Fixed isolated-Python desktop entry point; no CAD before provenance checks."""
import json
import os
from pathlib import Path
import site
import sys


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "--ticket":
        return 78
    ticket = Path(sys.argv[2])
    value = json.loads(ticket.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[2]
    if Path(value["source_root"]).resolve() != root:
        return 78
    venv = Path(value["venv"]).resolve()
    packages = Path(value["site_packages"]).resolve()
    if packages != venv / "Lib" / "site-packages" or not (venv / "pyvenv.cfg").is_file():
        return 78
    site.addsitedir(str(packages))
    sys.path.insert(0, str(root))
    from solidworks_mcp.coordination.desktop_launch import validate_ticket
    from solidworks_mcp.coordination import launch
    from solidworks_mcp.coordination.platform_guard import atomic_json
    # Failure diagnostics go only to the already existing private state.
    log_path = Path(value["state"]["path"]) / "broker-start.log"
    with log_path.open("a", encoding="utf-8") as errors:
        sys.stderr = errors
        try:
            proof = validate_ticket(value, ticket)
            ticket.replace(ticket.with_suffix(".consumed.json"))
            # The base interpreter gives an unambiguous immediate Explorer
            # parent. Subsequent helpers must use the configured venv Python,
            # whose own startup installs pywin32/Pillow and other dependencies.
            interpreter = venv / "Scripts" / "python.exe"
            if not interpreter.is_file():
                raise RuntimeError("Configured helper interpreter is unavailable")
            sys.executable = str(interpreter)
            sys.prefix = sys.exec_prefix = str(venv)
            proof["helper_executable"] = str(interpreter)
            launch._accept_desktop_proof(proof)
            receipt = ticket.with_suffix(".receipt.json")
            atomic_json(receipt, launch.lifetime_proof())
            args = ["--backend", value["backend"], "--state-dir", value["state"]["path"]]
            return launch.main(args)
        except Exception as exc:
            print(getattr(exc, "code", type(exc).__name__), file=errors, flush=True)
            return 78


if __name__ == "__main__":
    raise SystemExit(main())
