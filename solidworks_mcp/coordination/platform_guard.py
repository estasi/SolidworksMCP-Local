"""Local deployment gates. These checks never attach to COM or close a process."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import stat
import re
import time
import uuid
from pathlib import Path

from .protocol import QueueError
from .process_scan import coherent_inventory, safe_row


def default_state_dir():
    if os.name == "nt":
        import hashlib
        from .ownership import user_session_identity
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise QueueError("LOCAL_STATE_UNAVAILABLE", "LOCALAPPDATA is not set")
        session = hashlib.sha256(user_session_identity().encode("utf-8")).hexdigest()[:16]
        # The named mutex is scoped to a desktop session. The journal/endpoint
        # must have the identical scope; RDP sessions cannot recover each other.
        return Path(base) / "SolidworksMCP" / "coordination" / "v1" / session
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "solidworks-mcp/v1"


def source_root():
    return Path(__file__).resolve().parents[2]


def private_directory(path):
    path = Path(path).absolute()
    def redirected(item):
        return item.is_symlink() or (item.exists() and bool(
            getattr(item.stat(follow_symlinks=False), "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))
    if any(redirected(p) for p in [path, *path.parents]):
        raise QueueError("UNSAFE_STATE_DIRECTORY", "State directory cannot use symbolic links")
    if os.name == "nt":
        if str(path).startswith("\\\\") or "yandexdisk" in str(path).lower():
            raise QueueError("UNSAFE_STATE_DIRECTORY", "Use an unsynchronized local state directory")
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))
        if drive_type != 3:
            raise QueueError("UNSAFE_STATE_DIRECTORY", "State directory must be on a fixed local drive")
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import win32api
        import win32con
        import win32security
        import ntsecuritycon
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        system = win32security.ConvertStringSidToSid("S-1-5-18")
        # Set an exact protected DACL, rather than grant:r (which preserves
        # pre-existing explicit grants to other users). Reprotect old files too.
        for item in [path, *path.rglob("*")]:
            if redirected(item):
                raise QueueError("UNSAFE_STATE_DIRECTORY", "State contains a redirected path")
            acl = win32security.ACL()
            flags = (win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE) if item.is_dir() else 0
            for principal in (sid, system):
                acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags,
                                         ntsecuritycon.FILE_ALL_ACCESS, principal)
            win32security.SetNamedSecurityInfo(str(item), win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, acl, None)
    else:
        os.chmod(path, 0o700)
    return path


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".queue-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (FileNotFoundError, ValueError) as exc:
        raise QueueError("NOT_CONFIGURED", "Queue configuration is absent or invalid") from exc


def process_inventory_raw():
    """Run the raw CIM classifier; expose no command lines and make no COM calls.

    This deliberately remains separate so its PowerShell parsing can be tested
    against synthetic CIM rows without accidentally querying real kernel PIDs.
    Production callers must use process_inventory() for liveness reconciliation.
    """
    if os.name != "nt":
        raise QueueError("WINDOWS_REQUIRED", "Native SolidWorks requires Windows")
    script = r'''
$ErrorActionPreference = 'Stop'
$pythonName = '^(?:python(w)?(\d+(\.\d+)*)?|pyw?)\.exe$'
$moduleLaunch = '(?:^|\s)"?-m"?\s*"?solidworks_mcp(?:\.server|_server)"?(?:\s|$)'
$inlineServer = '(?:from\s+|import\s+)solidworks_mcp(?:\.server|_server)(?:\s|[.;]|$)'
$items = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -ieq 'SLDWORKS.exe' -or
    ($_.Name -match $pythonName -and
     ([string]::IsNullOrWhiteSpace($_.CommandLine) -or
      $_.CommandLine -match 'solidworks_mcp_server\.py' -or
      $_.CommandLine -match $moduleLaunch -or
      $_.CommandLine -match $inlineServer))
} | ForEach-Object {
    $scriptPath = $null
    # Module and inline entry points do not reveal the imported source path.
    # Never infer an allowlisted source from a .py argument elsewhere in them.
    if ($_.Name -ine 'SLDWORKS.exe' -and
        $_.CommandLine -notmatch $moduleLaunch -and
        $_.CommandLine -notmatch $inlineServer) {
        if ($_.CommandLine -match '"([^"\r\n]*solidworks_mcp_server\.py)"') {
            $scriptPath = $matches[1]
        } elseif ($_.CommandLine -match '(\S*solidworks_mcp_server\.py)') {
            $scriptPath = $matches[1]
        }
    }
    [pscustomobject]@{pid=[int]$_.ProcessId; name=$_.Name;
        script=$scriptPath; created_at=([DateTimeOffset]$_.CreationDate).ToUnixTimeMilliseconds()/1000.0;
        creation_cim_token=(([DateTimeOffset]$_.CreationDate).UtcDateTime.ToFileTimeUtc()).ToString([Globalization.CultureInfo]::InvariantCulture)}
})
ConvertTo-Json -InputObject $items -Compress
'''
    try:
        run = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, encoding="utf-8", timeout=15,
                             creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QueueError("PROCESS_SCAN_FAILED", "Cannot complete process inventory") from exc
    if run.returncode:
        raise QueueError("PROCESS_SCAN_FAILED", "Cannot prove that legacy CAD clients are absent")
    try:
        data = json.loads(run.stdout or "[]")
        return data if isinstance(data, list) else [data]
    except ValueError as exc:
        raise QueueError("PROCESS_SCAN_FAILED", "Invalid process inventory") from exc


def process_inventory():
    """Classify then reconcile selected Python rows with bounded kernel checks."""
    return coherent_inventory(process_inventory_raw)


def record_guard_failure(state_dir, error, inventory=None):
    """Retain bounded private evidence before old evidence is pruned.

    Retention: at most 16 uniquely named guard-event JSON files, each <=64 KiB,
    under the supplied canonical private state directory's guard-events folder.
    Only selected PID/name/creation/script/kernel-status fields are retained;
    command lines, environment values, exception messages and tracebacks are not.
    Any write/prune error remains a guard failure and cannot authorize CAD work.
    """
    folder = private_directory(Path(state_dir) / "guard-events")
    rows = getattr(error, "process_diagnostics", None)
    if rows is None:
        rows = inventory or []
    # Put the actual failure/unstable row before healthy earlier observations.
    # Otherwise a busy desktop with >12 valid frontends would hide the offender.
    def priority(item):
        status = item.get("kernel_status")
        return (2 if status in ("alive", "not_checked", "exited")
                else 1 if status in ("missing", "reused") else 0)
    relevant = sorted((item for item in reversed(rows) if isinstance(item, dict)), key=priority)
    projected = [safe_row(item, item.get("kernel_status", "not_checked")) for item in relevant[:12]]
    event = {"format": 1, "recorded_at": time.time(),
             "code": str(getattr(error, "code", "PROCESS_SCAN_FAILED"))[:64],
             "processes": projected, "omitted_processes": max(0, len(rows) - len(projected))}
    while len(json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 65536:
        event["processes"].pop()
        event["omitted_processes"] += 1
    current = folder / (f"guard-event-{time.time_ns():020d}-{uuid.uuid4().hex}.json")
    atomic_json(current, event)
    # Commit the current primary evidence before pruning. Concurrent pruning may
    # remove an older name after enumeration; other failures remain visible.
    owned_name = re.compile(r"guard-event-[0-9]{20}-[0-9a-f]{32}\.json\Z")
    existing = []
    for item in folder.glob("guard-event-*.json"):
        if not owned_name.fullmatch(item.name):
            continue
        try:
            info = item.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            existing.append(item)
    existing.sort(key=lambda item: item.name)
    victims = [item for item in existing if item != current][:max(0, len(existing) - 16)]
    for item in victims:
        try:
            item.unlink()
        except FileNotFoundError:
            pass
    return current


def legacy_processes(inventory=None, allowed_source=None):
    allowed = os.path.normcase(str(Path(allowed_source or source_root()) / "solidworks_mcp_server.py"))
    result = []
    for process in process_inventory() if inventory is None else inventory:
        if process["name"].lower() == "sldworks.exe":
            continue
        script = process.get("script")
        if not script or not os.path.isabs(script) or os.path.normcase(os.path.abspath(script)) != allowed:
            result.append({"pid": process["pid"], "kind": "legacy_mcp"})
    return result


def validate_activation(state_dir):
    activation = read_json(Path(state_dir) / "activation.json")
    if activation.get("format") != 1 or activation.get("enabled") is not True:
        raise QueueError("NOT_ACTIVATED", "Native CAD queue has not been activated")
    if os.path.normcase(activation.get("source_root", "")) != os.path.normcase(str(source_root())):
        raise QueueError("RELEASE_MISMATCH", "Activation belongs to another release")
    if activation.get("expected_major") != 33:
        raise QueueError("VERSION_NOT_QUALIFIED", "This release targets SolidWorks 2025 only")
    try:
        inventory = process_inventory()
    except QueueError as exc:
        record_guard_failure(state_dir, exc)
        raise
    blockers = legacy_processes(inventory)
    if blockers:
        error = QueueError("LEGACY_CLIENTS_PRESENT",
                           f"{len(blockers)} legacy MCP process(es) must be shut down normally")
        blocked_pids = {item["pid"] for item in blockers}
        record_guard_failure(state_dir, error, [item for item in inventory if item["pid"] in blocked_pids])
        raise error
    target = next((p for p in inventory if p["name"].lower() == "sldworks.exe"
                   and p["pid"] == activation.get("target_pid")), None)
    if not target or abs(target["created_at"] - float(activation.get("target_created_at", 0))) > .1:
        raise QueueError("CAD_INSTANCE_CHANGED", "Activated CAD process is absent or has restarted")
    return activation


def doctor():
    inventory = process_inventory()
    return {"legacy_clients": legacy_processes(inventory),
            "solidworks_instances": [p for p in inventory if p["name"].lower() == "sldworks.exe"],
            "source_root": str(source_root()), "state_dir": str(default_state_dir()),
            "native_calls_performed": False}
