"""Ensure required WinRT packages are installed into THIS Python.

Call before importing bt_audio / media_session on Windows.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

_REQUIRED = [
    ("winrt.windows.foundation", "winrt-Windows.Foundation"),
    ("winrt.windows.foundation.collections", "winrt-Windows.Foundation.Collections"),
    ("winrt.windows.media.audio", "winrt-Windows.Media.Audio"),
    ("winrt.windows.media.control", "winrt-Windows.Media.Control"),
    ("winrt.windows.devices.enumeration", "winrt-Windows.Devices.Enumeration"),
    ("winrt.windows.devices.bluetooth", "winrt-Windows.Devices.Bluetooth"),
    ("winrt.windows.system", "winrt-Windows.System"),
    ("winrt.windows.devices.wifi", "winrt-Windows.Devices.WiFi"),
    ("winrt.windows.networking.connectivity", "winrt-Windows.Networking.Connectivity"),
    ("winrt.windows.security.credentials", "winrt-Windows.Security.Credentials"),
]


def _missing() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for mod, pkg in _REQUIRED:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append((mod, pkg))
    return out


def ensure_winrt() -> bool:
    """Return True if WinRT imports are OK (after optional auto-install)."""
    if sys.platform != "win32":
        return False

    missing = _missing()
    if not missing:
        print(f"[ensure_winrt] OK — {sys.executable}")
        return True

    pkgs = sorted({pkg for _, pkg in missing})
    print(f"[ensure_winrt] missing modules: {[m for m, _ in missing]}")
    print(f"[ensure_winrt] installing into: {sys.executable}")
    print(f"[ensure_winrt] packages: {pkgs}")
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "winrt-runtime",
        *pkgs,
    ]
    try:
        subprocess.check_call(cmd)
    except Exception as exc:  # noqa: BLE001
        print(f"[ensure_winrt] pip failed: {exc}")
        print(
            "[ensure_winrt] run manually:\n  "
            f'"{sys.executable}" -m pip install winrt-runtime '
            + " ".join(pkgs)
        )
        return False

    # Clear failed imports so retry works in this process.
    for name in list(sys.modules):
        if name == "winrt" or name.startswith("winrt."):
            del sys.modules[name]

    missing = _missing()
    if missing:
        print(f"[ensure_winrt] still missing after install: {[m for m, _ in missing]}")
        return False
    print("[ensure_winrt] install OK")
    return True


if __name__ == "__main__":
    ok = ensure_winrt()
    raise SystemExit(0 if ok else 1)
