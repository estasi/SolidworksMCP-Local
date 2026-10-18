"""Reconcile CIM classifications with kernel process objects; never touch COM.

CIM reports creation times at microsecond precision. Compare its UTC FILETIME
token at that precision, then inspect liveness on the *same* open handle. A PID
alone, an inaccessible process, or an elapsed delay is never proof of exit.
"""
from __future__ import annotations

import ctypes
import math
import os

from .protocol import QueueError

_FILETIME_EPOCH = 116444736000000000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102


def safe_row(row, kernel_status="not_checked"):
    """Bounded diagnostic projection: deliberately excludes command lines."""
    row = row if isinstance(row, dict) else {}
    stamp = row.get("created_at")
    stamp = stamp if (isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
                      and 0 < stamp < 1e11 and math.isfinite(stamp)) else None
    pid = row.get("pid")
    return {"pid": pid if type(pid) is int and 0 < pid <= 0xFFFFFFFF else None,
            "name": row.get("name") if isinstance(row.get("name"), str)
                    and len(row["name"]) <= 128 else None,
            "created_at": stamp,
            "script": row.get("script") if isinstance(row.get("script"), str)
                      and len(row["script"]) <= 4096 else None,
            "kernel_status": kernel_status if isinstance(kernel_status, str)
                             and kernel_status.isascii() and len(kernel_status) <= 64
                             else "invalid_status"}


def scan_error(message, rows, code="PROCESS_SCAN_FAILED"):
    error = QueueError(code, message)
    error.process_diagnostics = rows
    return error


def validate_row(row):
    safe = safe_row(row)
    if (not isinstance(row, dict) or safe["pid"] is None or not safe["name"]
            or safe["created_at"] is None
            or (row.get("script") is not None and safe["script"] is None)):
        raise scan_error("Invalid process inventory row", [safe_row(row, "invalid_row")])
    # The raw classifier promises integer milliseconds. Reject malformed input
    # instead of broadening generation matching with an arbitrary tolerance.
    milliseconds = safe["created_at"] * 1000
    if not math.isfinite(milliseconds) or abs(milliseconds - round(milliseconds)) > 0.001:
        raise scan_error("Invalid process creation timestamp", [safe_row(row, "invalid_timestamp")])
    token = row.get("creation_cim_token")
    if (not isinstance(token, str) or not token.isascii() or not token.isdecimal()
            or len(token) > 20 or int(token) < _FILETIME_EPOCH or int(token) % 10
            or (int(token) - _FILETIME_EPOCH) // 10000 != round(milliseconds)):
        raise scan_error("Invalid CIM creation token", [safe_row(row, "invalid_timestamp")])
    return int(token)


def _kernel_api():
    if os.name != "nt":
        raise QueueError("WINDOWS_REQUIRED", "Native SolidWorks requires Windows")
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def inspect_process(row, kernel=None):
    """Return alive/exited/missing/reused, or fail closed on observation errors.

    SYNCHRONIZE + QUERY_LIMITED_INFORMATION is read-only. Every acquired handle
    is closed, including mismatch and error paths. The returned creation token
    identifies the kernel object that was actually observed.
    """
    expected_token = validate_row(row)
    kernel = _kernel_api() if kernel is None else kernel
    from ctypes import wintypes
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, row["pid"])
    if not handle:
        code = ctypes.get_last_error()
        if code == 87:  # PID absent at the point of OpenProcess; requires rescan.
            return {"status": "missing"}
        raise scan_error("Cannot inspect classified process", [safe_row(row, "open_failed")])
    try:
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise scan_error("Cannot inspect process creation time", [safe_row(row, "times_failed")])
        token = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        if token < _FILETIME_EPOCH:
            raise scan_error("Invalid kernel process creation time", [safe_row(row, "invalid_kernel_timestamp")])
        # CIM/DMTF truncates the sub-microsecond FILETIME digit. Do not broaden
        # this to a millisecond tolerance: a later object requires a rescan.
        if token // 10 != expected_token // 10:
            return {"status": "reused", "creation_token": str(token)}
        wait = kernel.WaitForSingleObject(handle, 0)
        if wait not in (_WAIT_OBJECT_0, _WAIT_TIMEOUT):
            raise scan_error("Cannot establish process liveness", [safe_row(row, "wait_failed")])
        return {"status": "exited" if wait == _WAIT_OBJECT_0 else "alive",
                "creation_token": str(token)}
    finally:
        if not kernel.CloseHandle(handle):
            raise scan_error("Cannot release process inspection handle", [safe_row(row, "close_failed")])


def coherent_inventory(raw_inventory, inspector=inspect_process):
    """At most two complete CIM snapshots; never silently drop unstable rows.

    An exited object in the first snapshot forces a fresh classification. Only
    an independently proven exited object may be omitted from the final scan.
    Missing/reused PIDs in the final scan fail closed even if they look benign.
    """
    diagnostics = []
    for attempt in range(2):
        try:
            rows = raw_inventory()
        except QueueError as exc:
            exc.process_diagnostics = diagnostics + getattr(exc, "process_diagnostics", [])
            raise
        if not isinstance(rows, list):
            raise scan_error("Invalid process inventory", diagnostics)
        selected, refresh, seen = [], False, set()
        for row in rows:
            try:
                validate_row(row)
                if row["pid"] in seen:
                    raise scan_error("Duplicate process inventory PID", [safe_row(row, "duplicate_pid")])
                seen.add(row["pid"])
                if row["name"].lower() == "sldworks.exe":
                    # Existing activation/COM guards separately pin CAD identity.
                    selected.append(row)
                    continue
                observed = inspector(row)
            except QueueError as exc:
                exc.process_diagnostics = diagnostics + getattr(exc, "process_diagnostics", [])
                raise
            status = observed.get("status")
            diagnostics.append({**safe_row(row, status), "scan": attempt + 1})
            if status == "alive":
                selected.append({**row, "kernel_status": status,
                                 "creation_token": observed["creation_token"]})
            elif status == "exited":
                refresh = True
            elif status in ("missing", "reused"):
                if attempt:
                    raise scan_error("Process inventory remains unstable after one rescan", diagnostics)
                refresh = True
            else:
                raise scan_error("Invalid kernel process observation", diagnostics)
        if attempt or not refresh:
            return selected
    raise AssertionError("Unreachable bounded process scan")
