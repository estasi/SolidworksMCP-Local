"""Disposable synthetic client, started only by the Windows lifetime tests."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.coordination.client import QueueClient
from solidworks_mcp.coordination.protocol import QueueError

if __name__ == "__main__":
    if os.environ.get("SOLIDWORKS_MCP_TEST_BACKEND") != "1":
        raise SystemExit("Explicit synthetic opt-in required")
    print(json.dumps({"ready_pid": os.getpid()}), flush=True)
    # The parent assigns the installed SDK Job Object before releasing this
    # handshake, so no broker can race ahead of assignment in these tests.
    if sys.stdin.readline().strip() != "begin":
        raise SystemExit(2)
    try:
        client = QueueClient(Path(sys.argv[1]), backend="test",
                             auto_start=sys.argv[2] in {"autostart", "desktop"})
        if sys.argv[2] == "desktop":
            client.ensure_broker(desktop=True)
        status = client.request("status")
        endpoint = client._endpoint()
        print(json.dumps({"status": "connected", "owner_id": status["owner_id"],
                          "backend": status["backend"], "process": endpoint["process"]}), flush=True)
    except QueueError as exc:
        print(json.dumps({"status": "refused", "code": exc.code}), flush=True)
    # Keep the disposable frontend alive until its own Job Object is closed.
    sys.stdin.readline()
