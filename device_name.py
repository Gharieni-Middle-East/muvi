"""The tablet's Bluetooth name (what phones show in their Bluetooth list).

Windows uses the computer name as the Bluetooth name, so "renaming the Bluetooth"
means renaming the PC. That needs administrator rights (the kiosk user is a standard
user), so the app starts Rename-Computer through the Windows administrator prompt
(UAC). The new name is used after the next restart.
"""

from __future__ import annotations

import socket
import sys
from typing import Any

TARGET_NAME = "MuVi"

_TCPIP = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"


def _reg_value(path: str, name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def info() -> dict[str, Any]:
    """{name: current name, next_name: name after restart, target, ok, restart_needed}."""
    current = _reg_value(_TCPIP, "Hostname") or socket.gethostname() or ""
    pending = _reg_value(_TCPIP, "NV Hostname") or current
    return {
        "name": current,
        "next_name": pending,
        "target": TARGET_NAME,
        "ok": current == TARGET_NAME,
        "restart_needed": pending != current and pending == TARGET_NAME,
        "supported": sys.platform == "win32",
    }


def request_rename() -> dict[str, Any]:
    """Show the Windows administrator prompt that renames the tablet to TARGET_NAME."""
    if sys.platform != "win32":
        return {"ok": False, "error": "Only on the Windows tablet."}
    import ctypes

    command = (
        f"-NoProfile -WindowStyle Hidden -Command "
        f"\"Rename-Computer -NewName '{TARGET_NAME}' -Force\""
    )
    # ShellExecute "runas" = UAC prompt. Returns > 32 when the elevated process started.
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", command, None, 0)
    if int(rc) <= 32:
        return {"ok": False, "error": "The administrator prompt was cancelled."}
    return {"ok": True}


def restart_tablet() -> dict[str, Any]:
    """Restart Windows so the new name is used."""
    if sys.platform != "win32":
        return {"ok": False, "error": "Only on the Windows tablet."}
    import subprocess

    try:
        subprocess.Popen(
            ["shutdown", "/r", "/t", "5", "/c", "Restarting to apply the Bluetooth name MuVi"],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
