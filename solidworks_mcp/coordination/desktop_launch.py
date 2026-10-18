"""Verified launch through the existing same-user Windows desktop Explorer.

The broker can remain in an OS desktop job. It must not be a child of an MCP
frontend job. No Shell.Application activation, process killing or CAD occurs.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .ownership import process_identity, user_session_identity
from .platform_guard import atomic_json, default_state_dir, private_directory, source_root
from .protocol import QueueError


def physical_identity(path):
    """Record the real existing object, including its volume/file identifier."""
    path = Path(path)
    import win32file
    handle = win32file.CreateFile(str(path), 0, 7, None, 3, 0x02000000, None)
    try:
        final = win32file.GetFinalPathNameByHandle(handle, 0)
        if final.startswith("\\\\?\\"):
            final = final[4:]
        resolved = Path(final)
        info = win32file.GetFileInformationByHandle(handle)
        file_index = (info[8] << 32) | info[9]
        stat = resolved.stat()
        if stat.st_ino != file_index or (stat.st_dev & 0xffffffff) != info[4]:
            raise QueueError("STATE_IDENTITY_CHANGED", "Physical object changed while inspecting its handle")
        return {"path": str(resolved), "device": stat.st_dev, "inode": stat.st_ino,
                "volume_serial": info[4], "file_index": file_index}
    finally:
        handle.Close()


def process_scope(pid):
    import win32api
    import win32security
    import win32ts
    handle = win32api.OpenProcess(0x1000, False, int(pid))
    try:
        token = win32security.OpenProcessToken(handle, 8)
        try:
            sid = win32security.ConvertSidToStringSid(
                win32security.GetTokenInformation(token, win32security.TokenUser)[0])
        finally:
            token.Close()
        return sid + ":session=" + str(win32ts.ProcessIdToSessionId(int(pid)))
    finally:
        handle.Close()


def verify_explorer(pid):
    import win32api
    import win32process
    handle = win32api.OpenProcess(0x1000, False, int(pid))
    try:
        executable = win32process.GetModuleFileNameEx(handle, 0)
    finally:
        handle.Close()
    expected = Path(os.environ["SystemRoot"]) / "explorer.exe"
    if os.path.normcase(str(Path(executable).resolve())) != os.path.normcase(str(expected.resolve())):
        raise QueueError("DESKTOP_LAUNCH_UNVERIFIED", "Desktop owner is not Windows Explorer")
    if process_scope(pid) != user_session_identity():
        raise QueueError("DESKTOP_SCOPE_MISMATCH", "Desktop Explorer belongs to another user or session")
    return process_identity(pid)


def _ticket(state_dir, backend, explorer):
    from solidworks_mcp import __version__
    state = physical_identity(state_dir)
    journal_path = Path(state["path"]) / "queue.sqlite3"
    journal = physical_identity(journal_path) if journal_path.exists() else None
    nonce = uuid.uuid4().hex
    folder = private_directory(Path(state["path"]) / "launch")
    path = folder / (nonce + ".json")
    source = source_root().resolve()
    venv = Path(sys.prefix).resolve()
    site_packages = venv / "Lib" / "site-packages"
    if not site_packages.is_dir():
        raise QueueError("DESKTOP_LAUNCH_UNVERIFIED", "Configured Python site-packages unavailable")
    value = {"format": 1, "nonce": nonce, "created_at": time.time(), "backend": backend,
             "source_root": str(source), "version": __version__, "state": state,
             "journal": journal, "scope": user_session_identity(), "explorer": explorer,
             "base_executable": str(Path(sys._base_executable).resolve()),
             "site_packages": str(site_packages), "venv": str(venv),
             "client": process_identity()}
    if backend == "native":
        canonical = default_state_dir()
        if canonical.resolve() != Path(state["path"]).resolve():
            raise QueueError("NONCANONICAL_STATE", "Desktop launch requires the existing canonical queue")
        # Preserve exactly the existing physical state under MSIX redirection.
        # This changes the bootstrap's environment only; no journal is copied.
        value["local_appdata"] = str(Path(state["path"]).parents[3])
    atomic_json(path, value)
    return path, value


def launch_desktop(state_dir, backend):
    """Launch a fixed bootstrap via the already running Explorer desktop view."""
    if os.name != "nt":
        raise QueueError("WINDOWS_REQUIRED", "Desktop launch requires Windows")
    if backend not in {"native", "test"}:
        raise QueueError("INVALID_REQUEST", "Unknown broker backend")
    if backend == "test" and os.environ.get("SOLIDWORKS_MCP_TEST_BACKEND") != "1":
        raise QueueError("TEST_BACKEND_DISABLED", "Synthetic launch requires explicit opt-in")
    import pythoncom
    import win32com.client
    import win32process
    from win32com.shell import shell
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    windows = desktop = provider = browser = view = dispatch = folder = app = None
    try:
        windows = win32com.client.dynamic.Dispatch(pythoncom.CoCreateInstance(
            "{9BA05972-F6A8-11CF-A442-00A0C90A8F39}", None,
            pythoncom.CLSCTX_ALL, pythoncom.IID_IDispatch))
        hwnd = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        desktop = windows.FindWindowSW(0, None, 8, hwnd, 1)
        if not hwnd.value:
            raise QueueError("DESKTOP_UNAVAILABLE", "Existing Windows desktop unavailable")
        explorer = verify_explorer(win32process.GetWindowThreadProcessId(hwnd.value)[1])
        provider = desktop._oleobj_.QueryInterface(pythoncom.IID_IServiceProvider)
        browser = provider.QueryService(shell.SID_STopLevelBrowser, shell.IID_IShellBrowser)
        view = browser.QueryActiveShellView()
        dispatch = view.GetItemObject(0, pythoncom.IID_IDispatch)
        folder = win32com.client.dynamic.Dispatch(dispatch)
        app = folder.Application
        ticket, value = _ticket(state_dir, backend, explorer)
        bootstrap = source_root() / "solidworks_mcp" / "coordination" / "desktop_bootstrap.py"
        app.ShellExecute(value["base_executable"],
            subprocess.list2cmdline(["-I", str(bootstrap), "--ticket", str(ticket)]),
            str(source_root()), "open", 0)
        return {"nonce": value["nonce"], "ticket": str(ticket), "explorer": explorer}
    except QueueError:
        raise
    except Exception as exc:
        raise QueueError("DESKTOP_LAUNCH_FAILED", "Verified desktop broker launch failed; no attached fallback") from exc
    finally:
        app = folder = dispatch = view = browser = provider = desktop = windows = None
        pythoncom.CoUninitialize()


def validate_ticket(value, ticket_path):
    """Validate all provenance and physical objects before opening the journal."""
    from solidworks_mcp import __version__
    if value.get("format") != 1 or not 0 <= time.time() - value["created_at"] <= 60:
        raise QueueError("DESKTOP_TICKET_EXPIRED", "Desktop launch ticket is expired or invalid")
    if value["version"] != __version__ or Path(value["source_root"]).resolve() != source_root().resolve():
        raise QueueError("RELEASE_MISMATCH", "Desktop bootstrap release differs from the request")
    if Path(value["base_executable"]).resolve() != Path(sys.executable).resolve():
        raise QueueError("DESKTOP_LAUNCH_UNVERIFIED", "Unexpected bootstrap interpreter")
    if user_session_identity() != value["scope"]:
        raise QueueError("DESKTOP_SCOPE_MISMATCH", "Bootstrap user or desktop session changed")
    explorer = verify_explorer(os.getppid())
    if explorer != value["explorer"]:
        raise QueueError("DESKTOP_LAUNCH_UNVERIFIED", "Bootstrap is not a child of the verified Explorer generation")
    if physical_identity(value["state"]["path"]) != value["state"]:
        raise QueueError("STATE_IDENTITY_CHANGED", "Physical queue directory changed during launch")
    expected_ticket = Path(value["state"]["path"]) / "launch" / (value["nonce"] + ".json")
    if Path(ticket_path).resolve() != expected_ticket.resolve():
        raise QueueError("DESKTOP_LAUNCH_UNVERIFIED", "Launch ticket location differs from its request")
    journal = Path(value["state"]["path"]) / "queue.sqlite3"
    if value["journal"] is not None and physical_identity(journal) != value["journal"]:
        raise QueueError("JOURNAL_IDENTITY_CHANGED", "Existing physical journal changed during launch")
    if value["backend"] == "native":
        os.environ["LOCALAPPDATA"] = value["local_appdata"]
        if default_state_dir().resolve() != Path(value["state"]["path"]).resolve():
            raise QueueError("NONCANONICAL_STATE", "Pinned physical state is not the canonical session queue")
    elif value["backend"] == "test":
        os.environ["SOLIDWORKS_MCP_TEST_BACKEND"] = "1"
    else:
        raise QueueError("INVALID_REQUEST", "Unknown bootstrap backend")
    return {"mode": "desktop", "nonce": value["nonce"], "explorer": explorer,
            "process": process_identity(), "scope": value["scope"],
            "state": value["state"], "journal_before": value["journal"]}
