"""
Wi-Fi state for the web UI (Settings -> Wi-Fi).

Wraps WifiManager (Windows WinRT Wi-Fi, blocking calls) in background threads and keeps a
small JSON-friendly state the UI polls through Api.get_state():

    {"available", "scanning", "networks": [...], "current_ssid", "internet",
     "connecting_ssid", "disconnecting", "message", "message_kind", "error_ssid",
     "radio_on", "radio_switching"}
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

import WifiManager

MESSAGES = {
    WifiManager.WRONG_PASSWORD: "Wrong password",
    WifiManager.NOT_AVAILABLE: "Network not available",
    WifiManager.TIMED_OUT: "Connection timed out",
    WifiManager.UNSUPPORTED: "Network type not supported",
    WifiManager.FAILED: "Could not connect",
}
INTERNET_TEXT = {
    "internet": "Internet OK",
    "captive": "Login page required",
    "local": "No internet access",
    "none": "No internet access",
}
STATUS_REFRESH_SECONDS = 20


class WifiService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._last_status = 0.0
        self.state: dict[str, Any] = {
            "available": WifiManager.AVAILABLE,
            "scanning": False,
            "scanned": False,
            "networks": [],
            "current_ssid": "",
            "internet": "none",
            "internet_text": INTERNET_TEXT["none"],
            "connecting_ssid": None,
            "disconnecting": False,
            "message": "" if WifiManager.AVAILABLE else "Wi-Fi is not available",
            "message_kind": "info" if WifiManager.AVAILABLE else "error",
            "error_ssid": None,
            "radio_on": None,         # Wi-Fi radio: True on, False off, None not known yet
            "radio_switching": False,
        }

    # ------------------------------------------------------------ helpers
    def snapshot(self) -> dict[str, Any]:
        self._maybe_refresh_status()
        with self._lock:
            snap = dict(self.state)
            snap["networks"] = [dict(n) for n in self.state["networks"]]
            return snap

    def _update(self, **values: Any) -> None:
        with self._lock:
            self.state.update(values)
            if "internet" in values:
                self.state["internet_text"] = INTERNET_TEXT.get(values["internet"], INTERNET_TEXT["none"])

    def _run(self, target, name: str) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        def work():
            try:
                target()
            except WifiManager.WifiError as exc:
                self._update(message=str(exc), message_kind="error")
            except Exception as exc:  # noqa: BLE001 - never crash the app
                WifiManager._log(f"Unexpected error: {exc!r}")
                self._update(message="Could not connect", message_kind="error")
            finally:
                with self._lock:
                    self._busy = False
                    self.state["scanning"] = False
                    self.state["connecting_ssid"] = None
                    self.state["disconnecting"] = False
        threading.Thread(target=work, name=name, daemon=True).start()
        return True

    @staticmethod
    def _network_dict(net: "WifiManager.Network") -> dict[str, Any]:
        return {
            "ssid": net.ssid,
            "bars": max(0, min(4, int(net.signal_bars))),
            "secured": net.secured,
            "enterprise": net.enterprise,
            "saved": net.saved,
            "connected": net.connected,
        }

    def _maybe_refresh_status(self) -> None:
        """Keep "Connected to X / Internet OK" fresh while the app runs (cheap, every 20 s)."""
        if not WifiManager.AVAILABLE or self._busy:
            return
        now = time.monotonic()
        if now - self._last_status < STATUS_REFRESH_SECONDS:
            return
        self._last_status = now

        def job():
            radio = WifiManager.radio_state()  # follows changes made in Windows quick settings
            if radio is not None and not self.state["radio_switching"]:
                self._update(radio_on=radio)
            if radio is False:
                self._update(current_ssid="", internet="none")
                return
            result = WifiManager.status()
            self._update(current_ssid=result.current_ssid, internet=result.internet)
        self._run(job, "wifi-status")

    # ------------------------------------------------------------ actions
    def scan(self) -> None:
        if not WifiManager.AVAILABLE:
            return
        if self.state["radio_on"] is False:
            # A scan would quietly switch the radio back on (WifiManager.scan), so don't.
            self._update(message="Wi-Fi is off. Turn it on to see the networks.", message_kind="info")
            return

        def job():
            result = WifiManager.scan()
            self._update(
                networks=[self._network_dict(n) for n in result.networks],
                current_ssid=result.current_ssid,
                internet=result.internet,
                scanned=True,
                message="" if result.networks else "No Wi-Fi networks found",
                message_kind="info",
            )
        if self._run(job, "wifi-scan"):
            self._update(scanning=True, message="", error_ssid=None)

    def connect(self, ssid: str, password: Optional[str] = None) -> None:
        if not WifiManager.AVAILABLE or not ssid:
            return

        def job():
            result = WifiManager.connect(ssid, password or None)
            if result.ok:
                self._update(
                    current_ssid=ssid,
                    internet=result.internet,
                    message=f"Connected to {ssid}",
                    message_kind="ok",
                    error_ssid=None,
                )
            else:
                self._update(
                    message=MESSAGES.get(result.code, "Could not connect"),
                    message_kind="error",
                    error_ssid=ssid,
                )
            # Refresh the list so "Saved" / "Connected" badges are right.
            scan = WifiManager.scan()
            self._update(networks=[self._network_dict(n) for n in scan.networks],
                         current_ssid=scan.current_ssid, internet=scan.internet)
        if self._run(job, "wifi-connect"):
            self._update(connecting_ssid=ssid, message="", error_ssid=None)

    def set_radio(self, on: bool) -> None:
        """Turn the Wi-Fi radio on or off. Runs on its own thread so a running status check
        can't swallow the request."""
        if not WifiManager.AVAILABLE:
            return
        with self._lock:
            if self.state["radio_switching"]:
                return
            self.state["radio_switching"] = True
            self.state["message"] = ""
            self.state["error_ssid"] = None

        def work():
            try:
                state = WifiManager.set_radio(on)
                if state:
                    self._update(
                        radio_on=True,
                        message="Wi-Fi is on. Windows reconnects to a saved network in a few seconds.",
                        message_kind="ok",
                    )
                    # Pick up the reconnect soon instead of waiting the full status interval.
                    self._last_status = time.monotonic() - STATUS_REFRESH_SECONDS + 8
                else:
                    self._update(
                        radio_on=False, current_ssid="", internet="none", networks=[], scanned=False,
                        message="Wi-Fi is off. Bluetooth now has the tablet's radio to itself.",
                        message_kind="info",
                    )
            except WifiManager.WifiError as exc:
                self._update(message=str(exc), message_kind="error")
            except Exception as exc:  # noqa: BLE001 - never crash the app
                WifiManager._log(f"Unexpected error switching Wi-Fi: {exc!r}")
                self._update(message="Could not switch Wi-Fi", message_kind="error")
            finally:
                self._update(radio_switching=False)
        threading.Thread(target=work, name="wifi-radio", daemon=True).start()

    def disconnect(self) -> None:
        if not WifiManager.AVAILABLE:
            return

        def job():
            still = WifiManager.disconnect()
            self._update(
                current_ssid=still,
                internet="none" if not still else self.state["internet"],
                message="" if not still else "Could not disconnect",
                message_kind="info" if not still else "error",
            )
            scan = WifiManager.scan()
            self._update(networks=[self._network_dict(n) for n in scan.networks],
                         current_ssid=scan.current_ssid, internet=scan.internet)
        if self._run(job, "wifi-disconnect"):
            self._update(disconnecting=True, message="")
