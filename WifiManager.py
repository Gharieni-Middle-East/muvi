"""
Wi-Fi through Windows' built-in WinRT API (Windows.Devices.WiFi).

This is the same engine the Windows network screens use (e.g. the network button on
the Ctrl+Alt+Del screen), so:
  - the security type (WPA2 / WPA3 / mixed / WPA / open) is detected by Windows,
  - saved networks stay saved (nothing is deleted),
  - connect results are specific (wrong password, not available, timeout...).

All functions here are blocking - call them from a worker thread (wifi_service.py does).
Logs go to C:\\kiosk_launcher\\updates\\wifi.log on tablets.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

AVAILABLE = False
IMPORT_ERROR = ""
try:
    from winrt.windows.devices.wifi import WiFiAdapter, WiFiReconnectionKind
    from winrt.windows.security.credentials import PasswordCredential
    from winrt.windows.networking.connectivity import NetworkInformation
    import winrt.windows.foundation  # noqa: F401  (async/collection projections)
    import winrt.windows.foundation.collections  # noqa: F401
    AVAILABLE = True
except Exception as exc:  # noqa: BLE001 - macOS dev machines, or a build without winrt
    IMPORT_ERROR = repr(exc)

# WinRT enum values (compared as ints so we don't depend on Python member names).
_ACCESS_ALLOWED = 1                      # WiFiAccessStatus.Allowed
_STATUS_UNSPECIFIED, _STATUS_SUCCESS, _STATUS_ACCESS_REVOKED = 0, 1, 2
_STATUS_INVALID_CREDENTIAL, _STATUS_NOT_AVAILABLE = 3, 4
_STATUS_TIMEOUT, _STATUS_UNSUPPORTED_AUTH = 5, 6
_CONNECTIVITY = {0: "none", 1: "local", 2: "captive", 3: "internet"}  # NetworkConnectivityLevel

# NetworkAuthenticationType
_AUTH_NAMES = {
    0: "none", 1: "unknown", 2: "open", 3: "wep", 4: "wpa-enterprise", 5: "wpa-psk",
    6: "wpa-none", 7: "wpa2-enterprise", 8: "wpa2-psk", 9: "ihv", 10: "wpa3-enterprise-192",
    11: "wpa3-sae", 12: "owe", 13: "wpa3-enterprise",
}
_OPEN_AUTH = {0, 2, 12}                  # no password needed
_ENTERPRISE_AUTH = {4, 7, 10, 13}        # needs username + password (not supported yet)

# Result codes returned to the UI (wifi_service.py turns them into messages).
OK = "connected"
WRONG_PASSWORD = "wrong_password"
NOT_AVAILABLE = "not_available"
TIMED_OUT = "timed_out"
UNSUPPORTED = "unsupported"
FAILED = "failed"

CONNECTIVITY_PROBE_URL = "http://www.msftconnecttest.com/connecttest.txt"
CONNECTIVITY_PROBE_TEXT = "Microsoft Connect Test"


class WifiError(Exception):
    """Message is shown to the user."""


@dataclass
class Network:
    ssid: str
    signal_bars: int = 0
    rssi: float = -100.0
    auth: int = 1
    saved: bool = False
    connected: bool = False

    @property
    def auth_name(self) -> str:
        return _AUTH_NAMES.get(self.auth, str(self.auth))

    @property
    def secured(self) -> bool:
        return self.auth not in _OPEN_AUTH

    @property
    def enterprise(self) -> bool:
        return self.auth in _ENTERPRISE_AUTH


@dataclass
class ScanResult:
    networks: list = field(default_factory=list)
    current_ssid: str = ""
    internet: str = "none"   # internet / captive / local / none


@dataclass
class ConnectResult:
    code: str
    ssid: str
    internet: str = "none"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.code == OK


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(f"[WiFi] {message}")
    if not getattr(sys, "frozen", False):
        return
    try:
        folder = Path(sys.executable).resolve().parent.parent / "updates"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "wifi.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _com_init() -> None:
    """WinRT calls from a worker thread need COM initialised (MTA) on that thread."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED; S_FALSE if already
    except Exception:  # noqa: BLE001
        pass


def _run(coro):
    _com_init()
    return asyncio.run(coro)


def _require_available() -> None:
    if not AVAILABLE:
        raise WifiError("Wi-Fi is not available")


async def _get_adapter():
    access = await WiFiAdapter.request_access_async()
    if int(access) != _ACCESS_ALLOWED:
        _log(f"Wi-Fi access not allowed: {access}")
        raise WifiError("Wi-Fi is not available")
    adapters = await WiFiAdapter.find_all_adapters_async()
    if adapters is None or len(adapters) == 0:
        raise WifiError("No Wi-Fi adapter found")
    return adapters[0]


async def _current_ssid(adapter) -> str:
    try:
        profile = await adapter.network_adapter.get_connected_profile_async()
        if profile is None:
            return ""
        if profile.is_wlan_connection_profile:
            return profile.wlan_connection_profile_details.get_connected_ssid() or ""
        return profile.profile_name or ""
    except Exception as exc:  # noqa: BLE001
        _log(f"Could not read current network: {exc!r}")
        return ""


async def _try_enable_radio() -> bool:
    """Turn the Wi-Fi radio on if it's off (best effort)."""
    try:
        from winrt.windows.devices.radios import Radio
        await Radio.request_access_async()
        radios = await Radio.get_radios_async()
        for radio in radios:
            if int(radio.kind) == 1 and int(radio.state) != 1:   # RadioKind.WiFi, RadioState.On
                _log("Wi-Fi radio is off - turning it on")
                await radio.set_state_async(radio.state.__class__(1))
                return True
    except Exception as exc:  # noqa: BLE001
        _log(f"Could not turn on Wi-Fi radio: {exc!r}")
    return False


def saved_profiles() -> set:
    """Names of Wi-Fi networks saved on this tablet (netsh works in every Windows language)."""
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "profiles"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"netsh failed: {exc!r}")
        return set()
    try:
        text = out.decode("oem", errors="replace")
    except LookupError:
        text = out.decode("utf-8", errors="replace")
    names = set()
    for line in text.splitlines():
        # Profile lines look like "    All User Profile     : MyWifi" (label is localized).
        if line.startswith("    ") and " : " in line:
            name = line.split(" : ", 1)[1].strip()
            if name:
                names.add(name)
    return names


def internet_status(wait_seconds: float = 0.0) -> str:
    """'internet', 'captive' (needs a login page), 'local' or 'none'."""
    deadline = time.time() + wait_seconds
    while True:
        status = _probe_internet()
        if status == "internet" or time.time() >= deadline:
            return status
        time.sleep(1.5)


def _probe_internet() -> str:
    try:
        import requests
        resp = requests.get(CONNECTIVITY_PROBE_URL, timeout=5, allow_redirects=False)
        if resp.status_code == 200 and resp.text.strip() == CONNECTIVITY_PROBE_TEXT:
            return "internet"
        return "captive"   # something answered, but not the real page -> login portal
    except Exception:  # noqa: BLE001
        pass
    if AVAILABLE:
        try:
            profile = NetworkInformation.get_internet_connection_profile()
            if profile is not None:
                level = _CONNECTIVITY.get(int(profile.get_network_connectivity_level()), "none")
                return level
        except Exception:  # noqa: BLE001
            pass
    return "none"


# --------------------------------------------------------------------------- #
# public API (blocking)
# --------------------------------------------------------------------------- #

def scan() -> ScanResult:
    _require_available()

    async def _scan():
        adapter = await _get_adapter()
        await adapter.scan_async()
        report = adapter.network_report
        nets = list(report.available_networks) if report is not None else []
        if not nets and await _try_enable_radio():
            await asyncio.sleep(3)
            await adapter.scan_async()
            report = adapter.network_report
            nets = list(report.available_networks) if report is not None else []
        return nets, await _current_ssid(adapter)

    raw, current = _run(_scan())
    saved = saved_profiles()
    best = {}
    for net in raw:
        ssid = (net.ssid or "").strip()
        if not ssid:
            continue  # hidden network
        try:
            auth = int(net.security_settings.network_authentication_type)
        except Exception:  # noqa: BLE001
            auth = 1
        item = Network(
            ssid=ssid,
            signal_bars=int(net.signal_bars or 0),
            rssi=float(net.network_rssi_in_decibel_milliwatts or -100),
            auth=auth,
            saved=ssid in saved,
            connected=(ssid == current),
        )
        if ssid not in best or item.rssi > best[ssid].rssi:
            best[ssid] = item
    networks = sorted(
        best.values(),
        key=lambda n: (not n.connected, not n.saved, -n.signal_bars, -n.rssi, n.ssid.lower()),
    )
    internet = internet_status() if current else "none"
    _log(f"Scan: {len(networks)} networks, connected={current!r}, internet={internet}")
    return ScanResult(networks=networks, current_ssid=current, internet=internet)


def status() -> ScanResult:
    """Current network + internet state, without scanning."""
    if not AVAILABLE:
        return ScanResult(internet=internet_status())

    async def _status():
        adapter = await _get_adapter()
        return await _current_ssid(adapter)

    try:
        current = _run(_status())
    except Exception as exc:  # noqa: BLE001
        _log(f"Status failed: {exc!r}")
        current = ""
    return ScanResult(current_ssid=current, internet=internet_status())


def disconnect() -> str:
    """Disconnect from the current Wi-Fi (network stays saved). Returns the SSID still connected, if any."""
    _require_available()

    async def _disconnect():
        adapter = await _get_adapter()
        before = await _current_ssid(adapter)
        adapter.disconnect()
        for _ in range(10):                 # wait up to ~5 s for Windows to drop the link
            await asyncio.sleep(0.5)
            now = await _current_ssid(adapter)
            if not now:
                break
        return before, now

    before, now = _run(_disconnect())
    _log(f"Disconnect from {before!r}: {'still connected to ' + repr(now) if now else 'disconnected'}")
    return now


def connect(ssid: str, password: Optional[str] = None) -> ConnectResult:
    """Connect to `ssid`. Without a password Windows uses the saved profile (if any)."""
    _require_available()
    _log(f"Connecting to {ssid!r} ({'with' if password else 'without'} password)")

    async def _connect():
        adapter = await _get_adapter()
        await adapter.scan_async()
        report = adapter.network_report
        candidates = [n for n in (report.available_networks if report else []) if (n.ssid or "") == ssid]
        if not candidates:
            return NOT_AVAILABLE, "network not in scan results"
        network = max(candidates, key=lambda n: n.network_rssi_in_decibel_milliwatts or -100)
        kind = WiFiReconnectionKind(0)  # Automatic: reconnect after reboot
        if password:
            credential = PasswordCredential()
            credential.password = password
            method = getattr(adapter, "connect_with_password_credential_async", None)
            if method is not None:
                result = await method(network, kind, credential)
            else:
                result = await adapter.connect_async(network, kind, credential)
        else:
            result = await adapter.connect_async(network, kind)
        code = int(result.connection_status)
        mapping = {
            _STATUS_SUCCESS: OK,
            _STATUS_INVALID_CREDENTIAL: WRONG_PASSWORD,
            _STATUS_NOT_AVAILABLE: NOT_AVAILABLE,
            _STATUS_TIMEOUT: TIMED_OUT,
            _STATUS_UNSUPPORTED_AUTH: UNSUPPORTED,
        }
        auth = _AUTH_NAMES.get(int(network.security_settings.network_authentication_type), "?")
        return mapping.get(code, FAILED), f"status={code} auth={auth}"

    try:
        code, detail = _run(_connect())
    except WifiError:
        raise
    except Exception as exc:  # noqa: BLE001
        _log(f"Connect error: {exc!r}")
        return ConnectResult(FAILED, ssid, detail=repr(exc))

    internet = internet_status(wait_seconds=10) if code == OK else "none"
    _log(f"Connect {ssid!r}: {code} ({detail}), internet={internet}")
    return ConnectResult(code, ssid, internet=internet, detail=detail)
