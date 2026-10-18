"""Non-COM, process-isolated observation of the activated CAD window.

The watcher never sends keys, confirms dialogs, starts or kills SOLIDWORKS.
It records a fence event while a COM call is blocked; returning from that call
does not erase the event. Only this helper process is stopped on normal exit.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path, value):
    from .platform_guard import atomic_json
    atomic_json(path, value)


class NativeWatchdog:
    def __init__(self, activation, state_dir, policy=None):
        self.activation = dict(activation)
        self.policy = dict(policy or {})
        self.directory = Path(state_dir) / "native-watchdog" / uuid.uuid4().hex
        self.process = None
        self._log = None
        self._failure = None

    def start(self):
        self.directory.mkdir(parents=True)
        config = {
            "target_pid": self.activation["target_pid"],
            "target_created_at": self.activation["target_created_at"],
            "directory": str(self.directory),
            "interval_sec": min(.5, max(.05, float(self.policy.get("interval_sec", .1)))),
            "dialog_detail_wait_sec": .75,
            "capture_screenshot": self.policy.get("capture_screenshot", True) is not False,
        }
        _write(self.directory / "config.json", config)
        self._log = (self.directory / "worker.log").open("ab", buffering=0)
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "solidworks_mcp.coordination.native_watchdog",
                 str(self.directory / "config.json")],
                cwd=str(Path(__file__).resolve().parents[2]),
                stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.event():
                    raise RuntimeError("Target CAD UI is not ready; watchdog recorded a blocker")
                if _read(self.directory / "ready.json"):
                    return
                if self.process.poll() is not None:
                    raise RuntimeError("Native watchdog exited before readiness")
                time.sleep(.02)
            raise RuntimeError("Native watchdog did not complete UI preflight")
        except BaseException as exc:
            self._failure = {"code": "WATCHDOG_STARTUP_FAILED",
                             "error_type": type(exc).__name__}
            raise

    def stop(self):
        try:
            exited_early = self.process is not None and self.process.poll() is not None
            if exited_early and self.event() is None:
                self._failure = {"code": "WATCHDOG_EXITED_BEFORE_OPERATION_RETURN",
                                 "returncode": self.process.returncode}
            (self.directory / "stop").touch(exist_ok=True)
            if self.process is not None:
                if self.process.poll() is None:
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        # This PID is the helper we created, never SOLIDWORKS.
                        self.process.terminate()
                        self.process.wait(timeout=1)
                elif self.process.returncode and self.event() is None:
                    self._failure = {"code": "WATCHDOG_EXITED",
                                     "returncode": self.process.returncode}
        except Exception as exc:
            self._failure = {"code": "WATCHDOG_SHUTDOWN_FAILED",
                             "error_type": type(exc).__name__}
        finally:
            if self._log is not None:
                self._log.close()
                self._log = None

    def event(self):
        return _read(self.directory / "event.json") or self._failure


def inspect_target_windows(pid):
    """Read only nonblocking Win32 window metadata, never titles/COM."""
    import win32gui
    import win32process
    windows = []

    def collect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            _, owner_pid = win32process.GetWindowThreadProcessId(hwnd)
            if owner_pid == pid:
                rect = win32gui.GetWindowRect(hwnd)
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                windows.append({"hwnd": int(hwnd), "area": area,
                                "owner": int(win32gui.GetWindow(hwnd, 4)),
                                "enabled": bool(win32gui.IsWindowEnabled(hwnd))})
        return True

    win32gui.EnumWindows(collect, None)
    frames = [item for item in windows if not item["owner"] and item["area"] > 0]
    if not frames:
        return {"ready": False, "code": "TARGET_CAD_WINDOW_MISSING"}
    main = max(frames, key=lambda item: item["area"])
    popups = []
    for item in windows:
        if item["hwnd"] == main["hwnd"] or item["area"] == 0:
            continue
        owner, seen = item["owner"], set()
        while owner and owner not in seen:
            if owner == main["hwnd"]:
                popups.append(item["hwnd"])
                break
            seen.add(owner)
            owner = int(win32gui.GetWindow(owner, 4))
    # Enabled floating toolbars/palettes are normal. A disabled main frame is
    # the conservative modal condition; preserve popup IDs as evidence only.
    return {"ready": main["enabled"], "code": "UI_READY" if main["enabled"]
            else "TARGET_CAD_UI_BLOCKED", "main_window_hwnd": main["hwnd"],
            "popup_hwnds": popups, "target_pid": pid}


def capture_evidence(state, directory):
    """Capture the already-visible target frame; never focus or move a window."""
    hwnd = state.get("main_window_hwnd")
    if not hwnd:
        return {"captured": False, "reason": "target_window_unavailable"}
    try:
        import win32gui
        from PIL import ImageGrab
        bounds = win32gui.GetWindowRect(hwnd)
        picture = ImageGrab.grab(bbox=bounds, all_screens=True)
        picture.thumbnail((2000, 2000))
        target = Path(directory) / "ui-block.png"
        picture.save(target, "PNG")
        return {"captured": True, "path": str(target), "bounds": list(bounds),
                "method": "visible_target_frame_no_focus_change"}
    except Exception as exc:
        return {"captured": False, "error_type": type(exc).__name__}


def run(config_path):
    from .ownership import process_identity
    config = _read(config_path)
    if not config:
        return 4
    directory = Path(config["directory"])
    blocked_since = None
    ready_written = False
    try:
        while not (directory / "stop").exists():
            identity = process_identity(config["target_pid"])
            if abs(identity["created_at"] - config["target_created_at"]) > .002:
                raise RuntimeError("Target process generation changed")
            state = inspect_target_windows(config["target_pid"])
            if state["ready"]:
                blocked_since = None
                if not ready_written:
                    _write(directory / "ready.json", {"ready": True, "at": time.time(),
                                                      "target_pid": config["target_pid"]})
                    ready_written = True
            else:
                blocked_since = blocked_since or time.monotonic()
                if (not ready_written or time.monotonic() - blocked_since >=
                        config["dialog_detail_wait_sec"]):
                    _write(directory / "event.json", {**state, "at": time.time(),
                                                      "auto_recovery_attempted": False})
                    if config.get("capture_screenshot", True):
                        state["screenshot"] = capture_evidence(state, directory)
                        _write(directory / "event.json", {**state, "at": time.time(),
                                                          "auto_recovery_attempted": False})
                    return 2
            time.sleep(config["interval_sec"])
        return 0
    except Exception as exc:
        _write(directory / "event.json", {"code": "WATCHDOG_OBSERVATION_FAILED",
                                          "at": time.time(),
                                          "error_type": type(exc).__name__})
        return 3


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]) if len(sys.argv) == 2 else 4)
