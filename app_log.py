"""Log files for the packaged (windowed) app, which has no console.

C:\\kiosk_launcher\\updates\\app.log     UI process (Bluetooth, Wi-Fi, state changes)
C:\\kiosk_launcher\\updates\\engine.log  audio worker (devices, streams, underruns)

Both sit next to the launcher, so they survive app updates.
"""

from __future__ import annotations

import os
import sys

LOG_MAX_BYTES = 2 * 1024 * 1024


def open_log_file(name: str):
    """Packaged app: append logs to C:\\kiosk_launcher\\updates\\<name> (next to the launcher, so
    they survive updates). Rotates to <name>.1 above LOG_MAX_BYTES. None if not possible."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        import time as _time

        base = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        log_dir = os.path.join(base, "updates")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, name)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            old = path + ".1"
            if os.path.exists(old):
                os.remove(old)
            os.replace(path, old)
        fh = open(path, "a", encoding="utf-8", buffering=1, errors="replace")
        fh.write(f"\n===== {_time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} =====\n")
        return fh
    except Exception:  # noqa: BLE001
        return None
