"""An OS-owned dispatcher lease; process death releases ownership automatically.

Names include user and Windows session identity, and intentionally omit MCP
version and checkout path. Every release must use the same CAD instance name.
The lease does not prove that a dead dispatcher's outstanding COM call stopped.
"""

import ctypes
import hashlib
import math
import os
from pathlib import Path
import tempfile
import threading
import time


def process_identity(pid=None):
    """Return a PID plus kernel creation token, protecting against PID reuse."""
    pid = os.getpid() if pid is None else int(pid)
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Cannot inspect process identity")
        try:
            creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise OSError(ctypes.get_last_error(), "Cannot inspect process creation time")
            stamp = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
        created_at = (stamp - 116444736000000000) / 10000000
        return {"pid": pid, "creation_time": created_at, "created_at": created_at,
                "creation_token": str(stamp)}
    stat_path = Path("/proc") / str(pid) / "stat"
    if stat_path.exists():
        # comm may contain spaces/parentheses; fields after the last ')' start
        # with field 3. Linux starttime is field 22.
        fields = stat_path.read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
        created_at = btime + int(fields[19]) / os.sysconf("SC_CLK_TCK")
        return {"pid": pid, "creation_time": created_at, "created_at": created_at,
                "creation_token": boot + ":" + fields[19]}
    # No unverifiable fallback: failing closed is safer than PID-only ownership.
    raise OSError("Kernel process creation identity is unavailable")


def _scope():
    if os.name != "nt":
        return "uid=" + str(os.getuid())
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "Cannot determine dispatcher user scope")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buf, size.value, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "Cannot inspect user token")
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise OSError(ctypes.get_last_error(), "Cannot inspect user SID")
        try:
            user = sid_text.value
        finally:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise OSError(ctypes.get_last_error(), "Cannot determine desktop session")
    return user + ":session=" + str(session.value)


def user_session_identity():
    """Stable user/session identity; contains no version or runtime path."""
    return _scope()


def process_is_same_alive(pid, created_at, tolerance=0.002):
    """True=same process, False=proven absent/reused, None=unknown.

    CIM's timestamp serialization rounds to milliseconds, hence the small
    comparison tolerance. Access-denied and observation errors fail closed.
    """
    try:
        created_at = float(created_at)
        if isinstance(pid, bool) or int(pid) <= 0 or not math.isfinite(created_at) or created_at <= 0:
            return None
        if not math.isfinite(tolerance) or not 0 <= tolerance <= 0.01:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        actual = process_identity(pid)
    except OSError as exc:
        code = getattr(exc, "winerror", None) or exc.errno
        if os.name == "nt":
            return False if code == 87 else None
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except (OSError, ValueError):
            return None
        return None
    try:
        return abs(actual["created_at"] - created_at) <= tolerance
    except (TypeError, ValueError):
        return None


class ProcessLease:
    def __init__(self, name="solidworks-cad"):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A stable CAD lease name is required")
        self.name = name
        digest = hashlib.sha256((_scope() + ":" + name).encode("utf-8")).hexdigest()
        self.native_name = "Local\\SolidWorksMCP.CAD." + digest
        self._digest = digest
        self._handle = None
        self._thread = None
        self._pid = None
        self.abandoned = False

    @property
    def acquired(self):
        return self._handle is not None and self._pid == os.getpid()

    def acquire(self, timeout=0):
        if self._handle is not None:
            raise RuntimeError("ProcessLease is not reentrant")
        timeout = float(timeout)
        if not 0 <= timeout <= 86400:
            raise ValueError("Lease timeout must be between 0 and 86400 seconds")
        self.abandoned = False
        if os.name == "nt":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel.CreateMutexW(None, False, self.native_name)
            if not handle:
                raise OSError(ctypes.get_last_error(), "Cannot create CAD ownership mutex")
            outcome = kernel.WaitForSingleObject(handle, int(timeout * 1000))
            if outcome not in (0, 0x80):
                error = ctypes.get_last_error()
                kernel.CloseHandle(handle)
                if outcome == 0x102:
                    return False
                raise OSError(error, "Cannot acquire CAD ownership mutex")
            self.abandoned = outcome == 0x80
            self._handle = handle
        else:
            import fcntl
            folder = Path(tempfile.gettempdir()) / ("solidworks-mcp-leases-" + str(os.getuid()))
            folder.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(str(folder / (self._digest + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        return False
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            self._handle = fd
        self._thread = threading.get_ident()
        self._pid = os.getpid()
        return True

    def release(self):
        if self._handle is None:
            return
        if self._pid != os.getpid() or self._thread != threading.get_ident():
            raise RuntimeError("CAD ownership lease must be released by its acquiring thread")
        handle = self._handle
        if os.name == "nt":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
            kernel.ReleaseMutex.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            if not kernel.ReleaseMutex(handle):
                raise OSError(ctypes.get_last_error(), "Cannot release CAD ownership mutex")
            kernel.CloseHandle(handle)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)
        self._handle = None
        self._thread = None
        self._pid = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Another dispatcher owns this CAD instance")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
