"""Force Windows default playback to VB-Cable (CABLE Input).

AudioPlaybackConnection / phone audio always go to the Windows default
playback device. Setting that to CABLE Input lets Play capture CABLE
without the user opening Sound settings.
"""

from __future__ import annotations

import sys
from typing import Any


def _is_cable_playback_name(name: str) -> bool:
    n = name.lower()
    if "gigaport" in n:
        return False
    # Apps play INTO the cable via the "CABLE Input" render endpoint.
    if "cable input" in n:
        return True
    if "cable in" in n and "output" not in n:
        return True
    if "vb-audio" in n and "cable" in n and "output" not in n:
        return True
    return False


def find_cable_playback_device() -> dict[str, str] | None:
    """Return {id, name} for the CABLE Input playback endpoint."""
    if sys.platform != "win32":
        return None
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception as exc:  # noqa: BLE001
        print(f"[windows_cable] pycaw unavailable: {exc}")
        return None

    candidates: list[dict[str, str]] = []
    try:
        for dev in AudioUtilities.GetAllDevices():
            name = str(getattr(dev, "FriendlyName", "") or "")
            dev_id = str(getattr(dev, "id", "") or "")
            if not name or not dev_id:
                continue
            if _is_cable_playback_name(name):
                candidates.append({"id": dev_id, "name": name, "active": _is_active(dev)})
    except Exception as exc:  # noqa: BLE001
        print(f"[windows_cable] GetAllDevices failed: {exc}")
        return None

    if not candidates:
        return None
    # GetAllDevices also lists disabled / unplugged / stale endpoints (e.g. left over from an
    # older VB-Cable install). Setting one of those as default sends the phone audio nowhere.
    candidates.sort(key=lambda c: (not c["active"], "cable input" not in c["name"].lower()))
    best = candidates[0]
    return {"id": best["id"], "name": best["name"]}


def _is_active(dev: Any) -> bool:
    state = getattr(dev, "state", None)
    if state is None:
        return True  # unknown: don't rule it out
    try:
        return int(getattr(state, "value", state)) == 1  # DEVICE_STATE_ACTIVE
    except (TypeError, ValueError):
        return "active" in str(state).lower()


def _set_default_endpoint(device_id: str) -> None:
    """IPolicyConfig.SetDefaultEndpoint (undocumented, Win7+)."""
    from ctypes import POINTER, c_int, c_void_p, c_wchar_p
    from comtypes import CLSCTX_ALL, GUID, COMMETHOD, HRESULT, IUnknown, CoCreateInstance

    # Full IPolicyConfig vtable up to SetDefaultEndpoint (index 10).
    class IPolicyConfig(IUnknown):
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "GetMixFormat",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["out"], POINTER(c_void_p), "ppDeviceFormat"),
            ),
            COMMETHOD(
                [], HRESULT, "GetDeviceFormat",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["in"], c_int, "bDefault"),
                (["out"], POINTER(c_void_p), "ppDeviceFormat"),
            ),
            COMMETHOD(
                [], HRESULT, "ResetDeviceFormat",
                (["in"], c_wchar_p, "pwstrDeviceId"),
            ),
            COMMETHOD(
                [], HRESULT, "SetDeviceFormat",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["in"], c_void_p, "pEndpointFormat"),
                (["in"], c_void_p, "pMixFormat"),
            ),
            COMMETHOD(
                [], HRESULT, "GetProcessingPeriod",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["in"], c_int, "bDefault"),
                (["out"], POINTER(c_void_p), "pmftDefaultPeriod"),
                (["out"], POINTER(c_void_p), "pmftMinimumPeriod"),
            ),
            COMMETHOD(
                [], HRESULT, "SetProcessingPeriod",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["in"], c_void_p, "pmftPeriod"),
            ),
            COMMETHOD(
                [], HRESULT, "GetShareMode",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["out"], POINTER(c_void_p), "pMode"),
            ),
            COMMETHOD(
                [], HRESULT, "SetShareMode",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["in"], c_void_p, "pMode"),
            ),
            COMMETHOD(
                [], HRESULT, "GetPropertyValue",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["in"], c_void_p, "key"),
                (["out"], POINTER(c_void_p), "pv"),
            ),
            COMMETHOD(
                [], HRESULT, "SetPropertyValue",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["in"], c_void_p, "key"),
                (["in"], c_void_p, "pv"),
            ),
            COMMETHOD(
                [], HRESULT, "SetDefaultEndpoint",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["in"], c_int, "role"),
            ),
            COMMETHOD(
                [], HRESULT, "SetEndpointVisibility",
                (["in"], c_wchar_p, "pwstrDeviceId"),
                (["in"], c_int, "bVisible"),
            ),
        ]

    CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
    policy = CoCreateInstance(CLSID_PolicyConfigClient, IPolicyConfig, CLSCTX_ALL)
    # eConsole=0, eMultimedia=1, eCommunications=2
    for role in (0, 1, 2):
        hr = policy.SetDefaultEndpoint(device_id, role)
        if hr not in (0, None) and int(hr) < 0:
            raise OSError(f"SetDefaultEndpoint role={role} failed hr={hr}")


def ensure_cable_default_playback() -> dict[str, Any]:
    """Set Windows default playback to CABLE Input. Call before Start Live."""
    if sys.platform != "win32":
        return {"ok": True, "skipped": True}

    cable = find_cable_playback_device()
    if cable is None:
        return {
            "ok": False,
            "error": "CABLE Input not found. Install VB-Audio Virtual Cable.",
        }

    try:
        _set_default_endpoint(cable["id"])
        print(f"[windows_cable] default playback → {cable['name']!r}")
        return {"ok": True, "name": cable["name"], "id": cable["id"]}
    except Exception as exc:  # noqa: BLE001
        print(f"[windows_cable] set default failed: {exc}")
        return {
            "ok": False,
            "error": f"Could not set default playback to CABLE: {exc}",
            "name": cable["name"],
        }
