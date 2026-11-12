"""USB hardware presence for UI status icons (Gigaport vibration, headphones audio)."""

from __future__ import annotations

import re
import time
from typing import Any

import sounddevice as sd

from audio.output_devices import (
    _group_units,
    _hostapi_name,
    _physical_gigaport_unit_count,
    _prefer_openable,
    _second_gigaport_unit,
    is_audio_interface_name,
    is_gigaport_name,
    is_usable_output_hostapi,
    list_output_devices,
)

_last_refresh_ts = 0.0
_REFRESH_INTERVAL_SEC = 2.5


def _should_refresh_portaudio(playing: bool) -> bool:
    if playing:
        return False
    global _last_refresh_ts
    now = time.time()
    if (now - _last_refresh_ts) >= _REFRESH_INTERVAL_SEC:
        _last_refresh_ts = now
        return True
    return False


def _refresh_portaudio() -> None:
    """Force PortAudio to drop unplugged USB devices from the device list."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def _short_name(raw: str) -> str:
    return re.sub(r"\s*\[.*?\]\s*", "", str(raw)).strip()


def _scan() -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """Return (gigaport_units, codec_name) from currently enumerated outputs."""
    units: dict[str, list[dict[str, Any]]] = {}
    codec_name: str | None = None

    try:
        all_devs = list_output_devices()
        gigaports = [d for d in all_devs if d.get("is_gigaport")]
        units = _group_units(gigaports)
        for d in all_devs:
            if (
                not d.get("is_gigaport")
                and is_audio_interface_name(d["name"])
                and int(d.get("channels", 0)) >= 2
                and is_usable_output_hostapi(str(d.get("hostapi", "")))
            ):
                codec_name = _short_name(d["name"])
                break
    except Exception as exc:
        print(f"[hardware] list_output_devices failed: {exc}")

    # If preferred APIs show nothing, look for MME Gigaport names only
    # (USB-A tablets sometimes hide WASAPI until a stream opens).
    if not units:
        try:
            raw: list[dict[str, Any]] = []
            for idx, dev in enumerate(sd.query_devices()):
                out_ch = int(dev.get("max_output_channels", 0))
                if out_ch <= 0:
                    continue
                name = str(dev.get("name", ""))
                if not is_gigaport_name(name):
                    continue
                api = _hostapi_name(int(dev.get("hostapi", 0)))
                if "asio" in api.lower():
                    continue
                raw.append(
                    {
                        "index": idx,
                        "name": name,
                        "channels": out_ch,
                        "hostapi": api,
                        "is_gigaport": True,
                        "usable": True,
                    }
                )
            if raw:
                units = _group_units(raw)
        except Exception as exc:
            print(f"[hardware] MME fallback scan failed: {exc}")

    return units, codec_name


def probe_hardware_status(
    playing: bool = False,
    force_refresh: bool = False,
    roles_flipped: bool = False,
) -> dict[str, Any]:
    """Detect vibration Gigaport + headphones for sidebar / Settings icons.

    Green rules:
      - Gigaport green  → at least one physical Gigaport is enumerated
      - Headphones green → a second physical Gigaport, OR (one Gigaport) a USB codec
      - AUX green        → a USB codec (Behringer UFO202) is present
    One Gigaport alone never lights headphones.
    """
    if force_refresh or _should_refresh_portaudio(playing):
        _refresh_portaudio()

    units, codec_name = _scan()
    physical_count = _physical_gigaport_unit_count(units)

    vib_key: str | None = None
    vib: dict[str, Any] | None = None
    for key in sorted(units.keys()):
        best = _prefer_openable(units[key])
        if vib is None or int(best.get("channels", 0)) > int(vib.get("channels", 0)):
            vib = best
            vib_key = key

    gigaport_name = _short_name(vib["name"]) if vib else None

    headphones_name: str | None = None
    headphones_kind: str | None = None

    # Second Gigaport first (headphones + speakers); a USB codec (Behringer UFO202) is then
    # the AUX input. Only with one Gigaport does the codec play the music.
    if physical_count >= 2 and vib_key is not None:
        second = _second_gigaport_unit(units, vib_key)
        if second is not None:
            headphones_name = _short_name(second[1]["name"])
            headphones_kind = "gigaport"
    if headphones_name is None and codec_name is not None:
        headphones_name = codec_name
        headphones_kind = "codec"

    if roles_flipped and physical_count >= 2 and vib_key is not None:
        second = _second_gigaport_unit(units, vib_key)
        if second is not None:
            flipped = second[1]
            old_vib = gigaport_name
            gigaport_name = _short_name(flipped["name"])
            if headphones_kind == "gigaport":
                headphones_name = old_vib

    return {
        "gigaport_connected": physical_count > 0 and vib is not None,
        "gigaport_count": physical_count,
        "gigaport_name": gigaport_name,
        "codec_connected": headphones_name is not None,
        "codec_name": headphones_name,
        "headphones_kind": headphones_kind,
        "roles_flipped": bool(roles_flipped),
        "can_swap_gigaports": physical_count >= 2 and headphones_kind == "gigaport",
        # AUX line-in interface (phone on a 3.5 mm cable), e.g. Behringer UFO202.
        "aux_connected": codec_name is not None,
        "aux_name": codec_name,
    }
