"""
Keep Muvi running on kiosk tablets: restart main.exe if it crashes or closes.

How it works
  - When main.exe starts in the kiosk session, it launches a second, lightweight copy
    of itself:  main.exe --watchdog --pid <app pid>   (no window, no webview, ~10 MB RAM).
  - The watchdog waits for the app to exit, then starts it again (2 s later).
    Restarted apps get MUVI_WATCHDOG_PID in their environment, so they don't
    start another watchdog - one watchdog per session.
  - If the app keeps dying (5 times in 5 minutes) it waits 60 s between restarts
    instead of hammering the tablet.

When it does NOT restart
  - Not the kiosk session: only runs when this Windows user's shell is
    launch_app.bat (so closing the app while logged in as admin really closes it).
  - An update is being installed (updates\\updating.flag, written by UpdateManager).
  - The app folder is gone/renamed (update swap or rollback in progress).
  - WATCHDOG_MODE = "off" below.

Logs: C:\\kiosk_launcher\\updates\\watchdog.log
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# "kiosk"  - only in the kiosk user's session (shell = launch_app.bat)  [default]
# "always" - also when started by hand (e.g. logged in as admin)
# "off"    - never restart
WATCHDOG_MODE = "kiosk"

WATCHDOG_ARG = "--watchdog"
ENV_MARKER = "MUVI_WATCHDOG_PID"
UPDATING_FLAG = "updating.flag"
UPDATING_FLAG_MAX_AGE = 15 * 60          # ignore a stale flag older than this
RESTART_DELAY = 2
BACKOFF_DELAY = 60
CRASH_WINDOW = 5 * 60
CRASH_LIMIT = 5

_spawned_watchdog = False


# --------------------------------------------------------------------------- #
# Paths / helpers (no webview/WinRT imports here - the watchdog process must stay tiny)
# --------------------------------------------------------------------------- #

def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_exe() -> Path:
    return Path(sys.executable).resolve()


def _updates_dir() -> Path:
    # main.exe lives in C:\kiosk_launcher\app -> C:\kiosk_launcher\updates
    return _app_exe().parent.parent / "updates"


def _log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [pid {os.getpid()}] {message}"
    try:
        _updates_dir().mkdir(parents=True, exist_ok=True)
        with (_updates_dir() / "watchdog.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _mode() -> str:
    return str(WATCHDOG_MODE).strip().lower()


def _is_kiosk_session() -> bool:
    """True when this Windows user's shell is the kiosk launcher (launch_app.bat)."""
    try:
        import winreg
    except ImportError:
        return False
    keys = (
        r"Software\Microsoft\Windows NT\CurrentVersion\Winlogon",
        r"Software\Microsoft\Windows\CurrentVersion\Policies\System",
    )
    for key_path in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Shell")
                if "launch_app" in str(value).lower() or "kiosk_launcher" in str(value).lower():
                    return True
        except OSError:
            continue
    return False


def update_in_progress() -> bool:
    flag = _updates_dir() / UPDATING_FLAG
    try:
        return flag.is_file() and (time.time() - flag.stat().st_mtime) < UPDATING_FLAG_MAX_AGE
    except OSError:
        return False


def set_updating(active: bool) -> None:
    """UpdateManager sets this before quitting for an update; the app clears it on start."""
    flag = _updates_dir() / UPDATING_FLAG
    try:
        if active:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text(str(os.getpid()), encoding="utf-8")
        else:
            flag.unlink(missing_ok=True)
    except OSError:
        pass


def is_supervised() -> bool:
    """True if a watchdog will restart this app when it exits."""
    return _spawned_watchdog or bool(os.environ.get(ENV_MARKER))


# --------------------------------------------------------------------------- #
# App side: start the watchdog once per session
# --------------------------------------------------------------------------- #

def start_if_needed() -> None:
    """Call early in main(). Starts the watchdog for this app if appropriate."""
    global _spawned_watchdog
    if not _is_frozen() or os.name != "nt":
        return
    mode = _mode()
    if mode == "off":
        return
    if os.environ.get(ENV_MARKER):
        return  # we were started by a watchdog - it is already watching us
    if mode != "always" and not _is_kiosk_session():
        print("[Watchdog] Not the kiosk session - not starting")
        return
    flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | 0x00000008  # DETACHED_PROCESS
    )
    try:
        subprocess.Popen(
            [str(_app_exe()), WATCHDOG_ARG, "--pid", str(os.getpid())],
            cwd=str(_app_exe().parent),
            creationflags=flags,
            close_fds=True,
        )
        _spawned_watchdog = True
        _log(f"Watchdog started for app pid {os.getpid()}")
    except OSError as exc:
        _log(f"Could not start watchdog: {exc}")


# --------------------------------------------------------------------------- #
# Watchdog side
# --------------------------------------------------------------------------- #

def _wait_for_pid(pid: int) -> None:
    """Block until process `pid` exits (Windows), or return at once if it's gone."""
    import ctypes
    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject(handle, INFINITE)
    finally:
        kernel32.CloseHandle(handle)


def run_watchdog(argv) -> int:
    """Entry point for `main.exe --watchdog --pid N` (called from the top of main.py)."""
    try:
        pid = int(argv[argv.index("--pid") + 1])
    except (ValueError, IndexError):
        return 2
    exe = _app_exe()
    _log(f"Watching app pid {pid} ({exe})")
    _wait_for_pid(pid)

    restarts: list[float] = []
    while True:
        if update_in_progress():
            _log("App exited for an update - watchdog stopping")
            return 0
        now = time.time()
        restarts = [t for t in restarts if now - t < CRASH_WINDOW]
        delay = BACKOFF_DELAY if len(restarts) >= CRASH_LIMIT else RESTART_DELAY
        _log(f"App is not running - restarting in {delay}s")
        time.sleep(delay)
        if update_in_progress():
            _log("Update started - watchdog stopping")
            return 0
        if not exe.is_file():
            _log("App executable is gone (update/rollback) - watchdog stopping")
            return 0

        env = dict(os.environ)
        env[ENV_MARKER] = str(os.getpid())
        try:
            proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
        except OSError as exc:
            _log(f"Restart failed: {exc}")
            restarts.append(time.time())
            continue
        restarts.append(time.time())
        _log(f"Restarted app (pid {proc.pid})")
        code = proc.wait()
        _log(f"App exited with code {code}")
