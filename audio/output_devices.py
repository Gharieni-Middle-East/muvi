"""Playback device discovery.

Windows: WASAPI / WDM-KS only (no ASIO). macOS: Core Audio. Linux: ALSA/JACK/Pulse.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Any

import sounddevice as sd

from audio.gigaport_routing import OutputLayout

# Host APIs safe for multichannel Gigaport output, per platform.
# Windows: WASAPI / WDM-KS only (no ASIO, MME, DirectSound).
# macOS:   Core Audio (the only native API; works with the Gigaport).
# Linux:   ALSA / JACK / PulseAudio.
if sys.platform == "darwin":
    _PREFERRED_HOSTAPIS = ("core audio", "coreaudio")
    _BLOCKED_HOSTAPIS: tuple[str, ...] = ()
elif sys.platform.startswith("linux"):
    _PREFERRED_HOSTAPIS = ("alsa", "jack", "pulse")
    _BLOCKED_HOSTAPIS = ()
else:  # win32 and anything else
    # WASAPI first: shared-mode opens reliably and actually drives the Gigaport.
    # WDM-KS is a fallback only — it often fails to open (PaErrorCode -9996) or
    # opens without producing a physical signal on this hardware.
    _PREFERRED_HOSTAPIS = ("wasapi", "wdm-ks")
    _BLOCKED_HOSTAPIS = ("asio", "mme", "directsound")


_LAST_TOPOLOGY_SIG: str | None = None


def _log_topology(
    all_devs: list[dict[str, Any]],
    units: dict[str, list[dict[str, Any]]],
    *,
    layout: str,
    vib: dict[str, Any] | None,
    audio: dict[str, Any] | None,
) -> None:
    """Print detected outputs + physical-unit grouping once per topology change.

    This makes single-vs-dual Gigaport detection debuggable on the tablet: paste
    the '[output_devices]' block to see why a layout was chosen.
    """
    global _LAST_TOPOLOGY_SIG
    try:
        sig = "|".join(
            f"{d['index']}:{d['name']}:{d['channels']}:{d['hostapi']}"
            for d in all_devs
        )
        sig += f"||{layout}:{vib['index'] if vib else None}:{audio['index'] if audio else None}"
        if sig == _LAST_TOPOLOGY_SIG:
            return
        _LAST_TOPOLOGY_SIG = sig
        print(f"[output_devices] platform={sys.platform} outputs:", flush=True)
        for d in all_devs:
            print(
                f"  #{d['index']} {d['name']!r} {d['channels']}ch {d['hostapi']} "
                f"gigaport={d.get('is_gigaport')} usable={d.get('usable')}",
                flush=True,
            )
        unit_desc = "; ".join(
            f"{k}->[{', '.join('#' + str(e['index']) for e in eps)}]"
            for k, eps in units.items()
        )
        print(f"[output_devices] gigaport units={len(units)}: {unit_desc}", flush=True)
        print(
            f"[output_devices] -> layout={layout} "
            f"vib=#{vib['index'] if vib else None} "
            f"audio=#{audio['index'] if audio else None}",
            flush=True,
        )
    except Exception:
        pass


def _hostapi_name(hostapi_index: int) -> str:
    try:
        return str(sd.query_hostapis()[int(hostapi_index)]["name"])
    except Exception:
        return "?"


def is_usable_output_hostapi(api_name: str) -> bool:
    lowered = api_name.lower()
    if any(b in lowered for b in _BLOCKED_HOSTAPIS):
        return False
    return any(p in lowered for p in _PREFERRED_HOSTAPIS)


def is_gigaport_name(name: str) -> bool:
    return "gigaport" in name.lower()


def is_audio_interface_name(name: str) -> bool:
    """A plain stereo USB audio interface usable as the audio (sound) output.

    Covers the Behringer U-Phono UFO202 and similar 2-channel USB codecs that
    can replace the 2nd Gigaport for headphone/line audio.
    """
    n = name.lower()
    return any(
        k in n
        for k in (
            "u-phono",
            "uphono",
            "u phono",
            "ufo202",
            "ufo-202",
            "behringer",
            # The UFO202 uses a generic USB-audio-class chip that Windows often
            # enumerates simply as "USB Audio CODEC".
            "usb audio codec",
        )
    )


def wasapi_output_extra(
    device_index: int,
    *,
    exclusive: bool = False,
) -> Any | None:
    if sys.platform != "win32":
        return None
    api = _hostapi_name(int(sd.query_devices(device_index)["hostapi"])).lower()
    if "wasapi" not in api:
        return None
    try:
        # Shared (exclusive=False) is the default — plays through the Windows mixer
        # and is what actually drives Gigaport transducers. Exclusive is only used
        # as a probe fallback when shared rejects multi-channel.
        return sd.WasapiSettings(exclusive=bool(exclusive), auto_convert=not exclusive)
    except Exception:
        return None


def refresh_portaudio() -> None:
    """Re-init PortAudio so USB endpoints reappear after unplug/replug or a stuck exclusive lock."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def probe_output_channels(
    device_index: int,
    sample_rate: int,
    candidates: tuple[int, ...],
) -> int | None:
    """Return the highest channel count that PortAudio will accept for this device.

    Tries several sample rates — Gigaport endpoints sometimes reject 44100 on
    WDM-KS while 48000 (or the device default) succeeds.
    On Windows WASAPI, also tries exclusive mode if shared rejects multi-channel.
    """
    rates: list[int] = []
    for rate in (int(sample_rate), 44100, 48000):
        if rate > 0 and rate not in rates:
            rates.append(rate)
    try:
        default_sr = int(sd.query_devices(device_index).get("default_samplerate") or 0)
        if default_sr > 0 and default_sr not in rates:
            rates.append(default_sr)
    except Exception:
        pass

    hostapi = ""
    try:
        hostapi = _hostapi_name(int(sd.query_devices(device_index)["hostapi"])).lower()
    except Exception:
        pass
    is_wdm = "wdm" in hostapi

    extras: list[Any | None] = [wasapi_output_extra(device_index, exclusive=False)]
    # Exclusive only as second pass — some Multi 8 endpoints reject shared 8ch.
    excl = wasapi_output_extra(device_index, exclusive=True)
    if excl is not None:
        extras.append(excl)

    for ch in candidates:
        for rate in rates:
            for extra in extras:
                try:
                    kwargs: dict[str, Any] = {
                        "device": device_index,
                        "channels": ch,
                        "samplerate": rate,
                        "dtype": "float32",
                    }
                    if extra is not None:
                        kwargs["extra_settings"] = extra
                    sd.check_output_settings(**kwargs)
                    return ch
                except Exception:
                    continue

    # Last resort: briefly open a real stream. Core Audio sometimes under-reports
    # max_output_channels (shows 2) while 8ch open still works.
    # Skip on Windows WDM-KS — open/close races leave the device busy and the
    # next real open fails (forces unplug/replug). WASAPI check_output_settings
    # above is the reliable path for Gigaport on Windows.
    if sys.platform == "win32" and is_wdm:
        return None

    for ch in candidates:
        if ch < 4:
            continue
        for rate in rates:
            try:
                stream = sd.OutputStream(
                    device=device_index,
                    channels=ch,
                    samplerate=rate,
                    dtype="float32",
                    blocksize=256,
                )
                stream.start()
                stream.stop()
                stream.close()
                return ch
            except Exception:
                continue
    return None


def list_output_devices(*, include_all: bool = False) -> list[dict[str, Any]]:
    """List playback devices. By default only WASAPI / WDM-KS (no ASIO/MME).

    Exception: Gigaport endpoints on MME are kept for discovery. Windows sometimes
    enumerates a second identical Gigaport on MME before WASAPI/WDM-KS shows it;
    without those entries dual-unit detection collapses to one device.
    """
    devices: list[dict[str, Any]] = []
    for idx, dev in enumerate(sd.query_devices()):
        out_ch = int(dev["max_output_channels"])
        if out_ch <= 0:
            continue
        name = str(dev["name"])
        api_name = _hostapi_name(int(dev["hostapi"]))
        usable = is_usable_output_hostapi(api_name)
        gigaport = is_gigaport_name(name)
        api_l = api_name.lower()
        mme_gigaport = gigaport and "mme" in api_l and "asio" not in api_l
        if not include_all and not usable and not mme_gigaport:
            continue
        devices.append(
            {
                "index": idx,
                "name": name,
                "channels": out_ch,
                "hostapi": api_name,
                "is_gigaport": gigaport,
                # MME Gigaports are discoverable but not preferred for open.
                "usable": usable or mme_gigaport,
                "default_sr": int(dev.get("default_samplerate") or 44100),
            }
        )

    def _sort_key(d: dict[str, Any]) -> tuple:
        api = str(d.get("hostapi", "")).lower()
        wdm = "wdm" in api
        wasapi = "wasapi" in api
        # WASAPI first: it goes through the Windows audio engine that is
        # actually wired to the device. WDM-KS on this Gigaport opens (and even
        # "plays") but produces no physical signal, and some endpoints fail to
        # open at all (PaErrorCode -9996). WDM-KS is kept only as a fallback.
        return (
            not d.get("is_gigaport", False),
            not wasapi,
            not wdm,
            -int(d.get("channels", 0)),
            d["name"].lower(),
        )

    devices.sort(key=_sort_key)
    return devices


def gigaport_unit_key(name: str) -> str:
    """Group Gigaport endpoints by physical USB unit.

    Windows enumerates a second identical device as '2- <name>' (often inside
    parentheses: 'Speakers (2- GIGAPORT eX)'), a third as '3- …', etc.
    Only that leading instance prefix counts — never digits inside CH5&6 / Multi 8.
    """
    m = re.search(r"(?:^|[\(\[])\s*(\d+)\s*-\s+", name)
    if m:
        return m.group(1)
    return "1"


def _is_wasapi_speakers(dev: dict[str, Any]) -> bool:
    api = str(dev.get("hostapi", "")).lower()
    name = str(dev.get("name", "")).lower()
    return "wasapi" in api and "speakers" in name


def _main_wasapi_speakers(dev: dict[str, Any]) -> bool:
    """Primary WASAPI Speakers render endpoint (not a CH1&2 stereo sub-out)."""
    return _is_wasapi_speakers(dev) and not _is_ch_pair_endpoint(dev)


def _is_ch_pair_endpoint(dev: dict[str, Any]) -> bool:
    """WDM-KS stereo pair endpoints (CH1&2 … CH7&8) — 4 per physical Gigaport."""
    return _ch_pair_slot(str(dev.get("name", ""))) is not None


def _ch_pair_slot(name: str) -> int | None:
    """Map a Line Out / CH-pair endpoint name to slot 0..3 (one Gigaport has 4)."""
    n = name.lower()
    if any(t in n for t in ("ch1&2", "ch182")) or re.search(r"ch\s*12\b", n):
        return 0
    if any(t in n for t in ("ch3&4", "ch384")) or re.search(r"ch\s*34\b", n):
        return 1
    if any(t in n for t in ("ch5&6", "ch586")) or re.search(r"ch\s*56\b", n):
        return 2
    if any(t in n for t in ("ch7&8", "ch788")) or re.search(r"ch\s*78\b", n):
        return 3
    return None


def _unique_ch_pairs(endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One endpoint per physical stereo pair — drop duplicate name encodings."""
    by_slot: dict[int, dict[str, Any]] = {}
    for d in endpoints:
        if not _is_ch_pair_endpoint(d):
            continue
        if not is_usable_output_hostapi(str(d.get("hostapi", ""))):
            continue
        slot = _ch_pair_slot(str(d.get("name", "")))
        if slot is None:
            continue
        prev = by_slot.get(slot)
        if prev is None or int(d.get("channels", 0)) > int(prev.get("channels", 0)):
            by_slot[slot] = d
    return [by_slot[k] for k in sorted(by_slot.keys())]


def _multichannel_wasapi_speakers(dev: dict[str, Any]) -> bool:
    """True for the main WASAPI render endpoint — not per-jack 2ch sub-outs."""
    return _is_wasapi_speakers(dev) and int(dev.get("channels", 0)) > 2


def _unit_key(dev: dict[str, Any]) -> str:
    """Physical-unit key for a Gigaport device, per platform.

    Windows exposes one physical Gigaport as several endpoints that share a
    name (WASAPI 'Speakers' + WDM-KS 'Line Out …'), told apart only by the
    '2- ' numeric prefix — so we group by that prefix. macOS Core Audio and
    Linux ALSA expose one device per physical unit, and two identical units get
    the *same* name, so there we key by the unique device index instead.
    """
    if sys.platform == "win32":
        return gigaport_unit_key(dev["name"])
    return str(dev["index"])


def _group_units(gigaports: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Map physical-unit key -> its endpoints (kept in best-first sort order).

    On Windows one physical Gigaport exposes WASAPI Speakers + WDM-KS Multi +
    CH-pair Line Outs. WASAPI names often get a '5- ' (etc.) instance prefix
    while WDM-KS Multi stays unprefixed — those MUST stay one unit. Splitting
    them made us open silent WDM-KS while the live WASAPI Speakers sat unused.

    Two physical boxes: two multichannel WASAPI Speakers with different
    instance prefixes, or >4 unique CH-pair Line Outs.
    """
    if sys.platform != "win32":
        return {str(d["index"]): [d] for d in gigaports}

    by_base: dict[str, list[dict[str, Any]]] = {}
    for d in gigaports:
        by_base.setdefault(gigaport_unit_key(d["name"]), []).append(d)

    speaker_bases = [
        base
        for base, endpoints in by_base.items()
        if any(_main_wasapi_speakers(d) for d in endpoints)
    ]

    if len(speaker_bases) >= 2:
        # Real dual Gigaport — only Speakers bases are units. Fold orphan
        # WDM-KS/MME buckets (different prefix, same USB box) onto the nearest
        # Speakers unit so we never open silent WDM-KS as "the other Gigaport".
        units: dict[str, list[dict[str, Any]]] = {base: [] for base in speaker_bases}
        speaker_rep: dict[str, dict[str, Any]] = {}
        for base in speaker_bases:
            for d in by_base.get(base, []):
                if _main_wasapi_speakers(d):
                    speaker_rep[base] = d
                    break
            units[base].extend(by_base.get(base, []))
        for base, endpoints in by_base.items():
            if base in units:
                continue
            for d in endpoints:
                if base == "1" and speaker_rep:
                    # Unprefixed WDM/MME almost always belongs to the first
                    # physical box (lowest Speakers index), not a '2-' unit.
                    best = min(
                        speaker_bases,
                        key=lambda k: int(speaker_rep.get(k, {"index": 10**9})["index"]),
                    )
                else:
                    best = min(
                        speaker_bases,
                        key=lambda k: abs(
                            int(speaker_rep.get(k, {"index": 0})["index"]) - int(d["index"])
                        ),
                    )
                units[best].append(d)
        return units

    if len(speaker_bases) == 1:
        # One WASAPI Speakers = one physical box. Fold WDM-KS / MME / CH-pairs
        # that got a different prefix into that same unit.
        main = speaker_bases[0]
        merged: list[dict[str, Any]] = []
        for endpoints in by_base.values():
            merged.extend(endpoints)
        return _split_merged_windows_units({main: merged})

    # No WASAPI Speakers yet — still one box if ≤4 CH-pair slots total.
    all_eps = [d for endpoints in by_base.values() for d in endpoints]
    if len(_unique_ch_pairs(all_eps)) <= 4:
        return _split_merged_windows_units({"1": all_eps})

    return _split_merged_windows_units(dict(by_base))


def _split_merged_windows_units(
    units: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Split a name-key bucket that swallowed a second Gigaport (no '2-' prefix).

    Speakers + Speakers Multi on the same prefix are ONE physical box — do not
    split them. Only CH-pair Line Out counts >4 indicate two merged boxes.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for key, endpoints in units.items():
        pairs = _unique_ch_pairs(endpoints)
        # One Gigaport has 4 stereo Line Out pairs; >4 unique slots ⇒ merged units.
        if len(pairs) > 4:
            pairs_sorted = sorted(pairs, key=lambda d: int(d["index"]))
            others = [d for d in endpoints if d not in pairs]
            chunks = [
                pairs_sorted[i : i + 4]
                for i in range(0, len(pairs_sorted), 4)
            ]
            for i, chunk in enumerate(chunks):
                nk = key if i == 0 else f"{key}:{chunk[0]['index']}"
                out[nk] = list(chunk)
                # Non-pair endpoints (Speakers / Multi) stay with the first unit.
                if i == 0 and others:
                    out[nk].extend(others)
            continue

        out[key] = endpoints
    return out


def _short_device_name(name: str) -> str:
    return re.sub(r"\s*\[.*?\]\s*", "", str(name)).strip()


def settings_display_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Usable outputs for Settings UI — one Gigaport endpoint per physical unit."""
    usable = [d for d in devices if d.get("usable")]
    gigaports = [d for d in usable if d.get("is_gigaport")]
    non_gig = [d for d in usable if not d.get("is_gigaport")]
    units = _group_units(gigaports)
    gig_display = [units[k][0] for k in sorted(units.keys())]
    non_gig.sort(key=lambda d: (-int(d.get("channels", 0)), str(d.get("name", "")).lower()))
    return gig_display + non_gig


def _device_label(dev: dict[str, Any]) -> str:
    return (
        f"{dev['name']} ({dev['channels']} ch, {dev['hostapi']}, #{dev['index']})"
    )


def _vibration_open_error(
    preferred: dict[str, Any],
    tried: list[dict[str, Any]],
) -> str:
    labels = ", ".join(_device_label(d) for d in tried[:6]) or _device_label(preferred)
    mac_hint = ""
    if sys.platform == "darwin":
        mac_hint = (
            "\n\nOn Mac, Core Audio often lists Gigaport as 2ch only. "
            "Open Audio MIDI Setup → GIGAPORT eX → set 8 output channels "
            "(or make an Aggregate Device). Full 8-shaker output is on the Windows tablet."
        )
    return (
        f"Cannot open vibration output on {_device_label(preferred)}\n\n"
        "PortAudio could not open any Gigaport endpoint with at least 4 channels.\n"
        "Tried: "
        f"{labels}"
        f"{mac_hint}\n\n"
        "Fix: fully quit Muvi (and any other app using the Gigaport), "
        "unplug/replug the Gigaport USB, then press Play again.\n"
        "On Windows: Sound → Gigaport → Advanced → turn off Exclusive Mode if set."
    )


def _prefer_openable(endpoints: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the best endpoint in a unit for opening (WASAPI Speakers / Multi first).

    Never prefer stereo CH-pair Line Outs (CH1&2 …) for the vibration stream —
    those report odd channel counts on WDM-KS and often fail to open as 8ch.
    """
    preferred = [
        d
        for d in endpoints
        if is_usable_output_hostapi(str(d.get("hostapi", "")))
    ]
    pool = preferred or list(endpoints)

    def _rank(d: dict[str, Any]) -> tuple:
        api = str(d.get("hostapi", "")).lower()
        wasapi = "wasapi" in api
        wdm = "wdm" in api
        ch_pair = _is_ch_pair_endpoint(d)
        speakers = _is_wasapi_speakers(d)
        name = str(d.get("name", "")).lower()
        multi = "multi" in name
        # WASAPI Speakers before anything WDM-KS, even Multi aggregates.
        return (
            ch_pair,           # never first: CH-pair Line Outs
            not wasapi,        # WASAPI before WDM-KS
            not speakers,      # WASAPI Speakers first
            not multi,         # then Multi / aggregate
            wdm,               # deprioritize WDM-KS
            -int(d.get("channels", 0)),
            int(d.get("index", 0)),
        )

    return sorted(pool, key=_rank)[0]


def _probe_candidates(
    endpoints: list[dict[str, Any]],
    *,
    wasapi_only: bool = False,
    allow_ch_pairs: bool = False,
    allow_wdm: bool = True,
    min_channels: int = 4,
) -> tuple[dict[str, Any], int] | None:
    """Probe endpoints in preference order; return first that opens with >= min_channels."""
    preferred = [
        d
        for d in endpoints
        if is_usable_output_hostapi(str(d.get("hostapi", "")))
    ]
    pool = preferred or list(endpoints)

    def _rank(d: dict[str, Any]) -> tuple:
        api = str(d.get("hostapi", "")).lower()
        return (
            _is_ch_pair_endpoint(d),
            "wasapi" not in api,
            not _is_wasapi_speakers(d),
            "wdm" in api,
            -int(d.get("channels", 0)),
            int(d.get("index", 0)),
        )

    for cand in sorted(pool, key=_rank):
        api = str(cand.get("hostapi", "")).lower()
        if wasapi_only and "wasapi" not in api:
            continue
        if not allow_wdm and "wdm" in api:
            continue
        if not allow_ch_pairs and _is_ch_pair_endpoint(cand):
            continue
        # Never accept stereo-only (2ch) — that selected Mac's 2ch 'GIGAPORT eX'
        # and then crashed in _validate_output_device asking for 8ch.
        ch = probe_output_channels(cand["index"], 44100, (8, 6, 4))
        if ch is None or ch < min_channels:
            continue
        return cand, ch
    return None


def _probe_unit_vibration(
    endpoints: list[dict[str, Any]],
) -> tuple[dict[str, Any], int] | None:
    """Try unit endpoints until one opens for vibration (>=4 ch).

    Pass order:
      1) WASAPI non-pair endpoints (drives real Gigaport transducers)
      2) WASAPI CH-pair Line Outs
      3) WDM-KS only as last resort — opens but often produces NO physical signal
    """
    hit = _probe_candidates(
        endpoints, wasapi_only=True, allow_ch_pairs=False, allow_wdm=False
    )
    if hit is not None:
        return hit
    hit = _probe_candidates(
        endpoints, wasapi_only=True, allow_ch_pairs=True, allow_wdm=False
    )
    if hit is not None:
        return hit
    # WDM-KS last — known to open silently on this hardware; only if WASAPI gone.
    hit = _probe_candidates(
        endpoints, wasapi_only=False, allow_ch_pairs=False, allow_wdm=True
    )
    if hit is not None:
        return hit
    return _probe_candidates(
        endpoints, wasapi_only=False, allow_ch_pairs=True, allow_wdm=True
    )


def _probe_vibration_with_retry(
    endpoints: list[dict[str, Any]],
    *,
    all_gigaport_endpoints: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int] | None:
    """Probe preferring WASAPI; refresh/retry before accepting silent WDM-KS.

    WDM-KS Multi can pass check_output_settings and still drive nothing on the
    shakers. Exhaust WASAPI (with PortAudio refresh) before falling back.
    """
    pools: list[list[dict[str, Any]]] = [list(endpoints)]
    if all_gigaport_endpoints:
        pools.append(list(all_gigaport_endpoints))

    wasapi_hit: tuple[dict[str, Any], int] | None = None
    any_hit: tuple[dict[str, Any], int] | None = None

    for attempt in range(3):
        for pool in pools:
            # WASAPI-only first every attempt.
            hit = _probe_candidates(
                pool, wasapi_only=True, allow_ch_pairs=False, allow_wdm=False
            )
            if hit is None:
                hit = _probe_candidates(
                    pool, wasapi_only=True, allow_ch_pairs=True, allow_wdm=False
                )
            if hit is not None:
                return hit

            # Remember a non-WASAPI fallback but keep retrying WASAPI.
            fallback = _probe_unit_vibration(pool)
            if fallback is not None:
                api = str(fallback[0].get("hostapi", "")).lower()
                if "wasapi" in api:
                    return fallback
                if any_hit is None:
                    any_hit = fallback

        if attempt >= 2:
            break

        time.sleep(0.2 * (attempt + 1))
        refresh_portaudio()
        refreshed = list_output_devices()
        refreshed_giga = [d for d in refreshed if d.get("is_gigaport")]
        if not refreshed_giga:
            continue

        names = {(str(d.get("name")), str(d.get("hostapi"))) for d in endpoints}
        rematched = [
            d
            for d in refreshed_giga
            if (str(d.get("name")), str(d.get("hostapi"))) in names
        ] or refreshed_giga
        pools = [rematched, refreshed_giga]

    return any_hit


def _physical_gigaport_unit_count(units: dict[str, list[dict[str, Any]]]) -> int:
    """Count real USB Gigaport boxes.

    macOS / Linux: `_group_units` already keys one entry per physical device
    (by PortAudio index), so ``len(units)`` is the count.

    Windows: one physical box can appear as many endpoints. Count units that
    have a multichannel WASAPI Speakers endpoint (same rule as `_group_units`
    dual split). CH-pair Line Outs alone never make a second box.
    """
    if not units:
        return 0

    if sys.platform != "win32":
        return len(units)

    units_with_speakers = sum(
        1
        for endpoints in units.values()
        if any(_main_wasapi_speakers(d) for d in endpoints)
    )
    if units_with_speakers >= 1:
        return units_with_speakers

    any_giga = any(
        d.get("is_gigaport") for endpoints in units.values() for d in endpoints
    )
    return 1 if any_giga else 0


def _second_gigaport_unit(
    units: dict[str, list[dict[str, Any]]],
    vib_unit: str | None,
) -> tuple[str, dict[str, Any]] | None:
    """Return (unit_key, best_endpoint) for a Gigaport that is not vibration."""
    if vib_unit is None or len(units) < 2:
        return None
    # Prefer another unit that has its own multichannel Speakers.
    for key in sorted(units.keys()):
        if key == vib_unit:
            continue
        if any(_multichannel_wasapi_speakers(d) for d in units[key]):
            return key, _prefer_openable(units[key])
    for key in sorted(units.keys()):
        if key == vib_unit:
            continue
        return key, _prefer_openable(units[key])
    return None


def resolve_output_topology(
    *,
    vibration_index: int | None = None,
    audio_index: int | None = None,
    roles_flipped: bool = False,
    include_mme: bool = False,
) -> dict[str, Any]:
    """Pick vibration/audio devices and layout without opening streams.

    Safe for UI status polling — does not probe PortAudio output settings.
    """
    all_devs = list_output_devices(include_all=include_mme)
    gigaports = [d for d in all_devs if d.get("is_gigaport")]
    units = _group_units(gigaports)
    physical_count = _physical_gigaport_unit_count(units)

    # --- vibration ---
    vib: dict[str, Any] | None = None
    vib_unit: str | None = None
    if vibration_index is not None:
        vib = next((d for d in all_devs if d["index"] == vibration_index), None)
        if vib is None:
            raise RuntimeError(f"Vibration output device #{vibration_index} not found")
        vib_unit = _unit_key(vib) if sys.platform == "win32" else str(vib["index"])
        for key, endpoints in units.items():
            if any(int(e["index"]) == int(vib["index"]) for e in endpoints):
                vib_unit = key
                vib = _prefer_openable(endpoints)
                break
    else:
        for key in sorted(units.keys()):
            best = _prefer_openable(units[key])
            if vib is None or int(best["channels"]) > int(vib["channels"]):
                vib = best
                vib_unit = key

    if vib is None:
        return {
            "vib": None,
            "audio": None,
            "layout": "single",
            "units": units,
            "gigaport_unit_count": 0,
            "physical_gigaport_count": 0,
            "vib_unit": None,
            "all_devs": all_devs,
        }

    # --- audio ---
    audio: dict[str, Any] | None = None
    if audio_index is not None:
        audio = next((d for d in all_devs if d["index"] == audio_index), None)
        if audio is None:
            raise RuntimeError(f"Audio output device #{audio_index} not found")
        for key, endpoints in units.items():
            if any(int(e["index"]) == int(audio["index"]) for e in endpoints):
                audio = _prefer_openable(endpoints)
                break
    else:
        codec_cands = [
            d
            for d in all_devs
            if not d.get("is_gigaport")
            and d["index"] != vib["index"]
            and int(d["channels"]) >= 2
            and is_audio_interface_name(d["name"])
            and is_usable_output_hostapi(str(d.get("hostapi", "")))
        ]

        def _codec_rank(d: dict[str, Any]) -> tuple:
            api = str(d.get("hostapi", "")).lower()
            return (
                "wasapi" not in api,
                "wdm" not in api,
                -int(d.get("channels", 0)),
                int(d.get("index", 0)),
            )

        audio = sorted(codec_cands, key=_codec_rank)[0] if codec_cands else None
        if audio is None and physical_count >= 2:
            second = _second_gigaport_unit(units, vib_unit)
            if second is not None:
                audio = second[1]

    if (
        roles_flipped
        and vib.get("is_gigaport")
        and vibration_index is None
        and audio_index is None
        and physical_count >= 2
    ):
        second = _second_gigaport_unit(units, vib_unit)
        if second is not None:
            new_vib_unit, new_vib = second
            if audio is not None and audio.get("is_gigaport"):
                audio = vib
            vib = new_vib
            vib_unit = new_vib_unit

    audio_unit: str | None = None
    if audio is not None and audio.get("is_gigaport"):
        for key, endpoints in units.items():
            if any(int(e["index"]) == int(audio["index"]) for e in endpoints):
                audio_unit = key
                break
        if audio_unit is None:
            audio_unit = _unit_key(audio)

    same_unit = (
        audio is not None
        and audio.get("is_gigaport")
        and audio_unit is not None
        and audio_unit == vib_unit
    )
    if audio is not None and not same_unit and audio["index"] != vib["index"]:
        layout: OutputLayout = "dual_native"
    elif same_unit:
        layout = "single"
    else:
        layout = "vibration_only"
        audio = None

    return {
        "vib": vib,
        "audio": audio,
        "layout": layout,
        "units": units,
        "gigaport_unit_count": len(units),
        "physical_gigaport_count": physical_count,
        "vib_unit": vib_unit,
        "all_devs": all_devs,
    }


def pick_output_devices(
    *,
    vibration_index: int | None = None,
    audio_index: int | None = None,
    roles_flipped: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None, OutputLayout, dict[str, Any]]:
    """Auto-detect vibration (+ optional audio) Gigaport outputs on WASAPI/WDM-KS.

    Two physical Gigaports are separated by their Windows unit prefix / Speakers
    endpoints so the audio device is never just another channel-pair of the
    vibration unit. `roles_flipped` swaps which Gigaport is vibration vs audio.
    """
    meta: dict[str, Any] = {"warnings": []}
    topo = resolve_output_topology(
        vibration_index=vibration_index,
        audio_index=audio_index,
        roles_flipped=roles_flipped,
    )
    vib = topo["vib"]
    audio = topo["audio"]
    layout = topo["layout"]
    units = topo["units"]
    vib_unit = topo["vib_unit"]
    all_devs = topo["all_devs"]

    if vib is None:
        apis = " / ".join(a.upper() if len(a) <= 6 else a.title() for a in _PREFERRED_HOSTAPIS)
        raise RuntimeError(
            f"No Gigaport output on {apis}.\n\n"
            "Connect the Gigaport(s) via USB and install the ESI driver.\n"
            "Open Settings to see detected devices — ASIO is not used."
        )

    meta["roles_flipped"] = bool(roles_flipped)
    meta["gigaport_unit_count"] = topo["physical_gigaport_count"]

    if layout == "dual_native" and audio is not None:
        # Probe vibration across all endpoints of its physical unit — the first
        # ranked pick can be a WDM-KS CH1&2 Line Out that fails as 8ch (common
        # when a USB codec is also present and Windows reorders endpoints).
        vib_endpoints = units.get(vib_unit or "", [vib])
        all_giga = [d for endpoints in units.values() for d in endpoints]
        probed = _probe_vibration_with_retry(
            vib_endpoints, all_gigaport_endpoints=all_giga
        )
        if probed is None:
            raise RuntimeError(_vibration_open_error(vib, vib_endpoints))
        vib, vib_ch = probed
        # Gigaport audio must open as 8ch when possible. A 4ch WASAPI Speakers
        # stream is often treated as front+surround, and Windows remaps the
        # "back" pair onto physical CH5&6 instead of CH3&4.
        if audio.get("is_gigaport"):
            audio_ch = probe_output_channels(audio["index"], 44100, (8, 4, 2))
        else:
            audio_ch = probe_output_channels(audio["index"], 44100, (4, 2))
        if audio_ch is None:
            raise RuntimeError(
                f"Cannot open audio output on {_device_label(audio)}\n\n"
                "Close other apps using that device, then try again."
            )
        if vib_ch < 8:
            meta["warnings"].append(
                f"Vibration device supports {vib_ch} ch (wanted 8) — zones may be limited"
            )
        if audio.get("is_gigaport") and audio_ch < 4:
            meta["warnings"].append(
                f"Audio Gigaport opened as {audio_ch} ch — Speakers route needs CH3&4"
            )
        vib = {**vib, "probed_channels": vib_ch}
        audio = {**audio, "probed_channels": audio_ch}
        _log_topology(all_devs, units, layout="dual_native", vib=vib, audio=audio)
        return vib, audio, "dual_native", meta

    if layout == "single":
        vib_endpoints = units.get(vib_unit or "", [vib])
        all_giga = [d for endpoints in units.values() for d in endpoints]
        probed = _probe_vibration_with_retry(
            vib_endpoints, all_gigaport_endpoints=all_giga
        )
        if probed is None:
            raise RuntimeError(_vibration_open_error(vib, vib_endpoints))
        vib, single_ch = probed
        if single_ch < 6:
            meta["warnings"].append(
                f"Single device supports {single_ch} ch (wanted 6) — using reduced layout"
            )
        vib = {**vib, "probed_channels": single_ch}
        _log_topology(all_devs, units, layout="single", vib=vib, audio=audio)
        return vib, audio, "single", meta

    # vibration_only
    vib_endpoints = units.get(vib_unit or "", [vib])
    all_giga = [d for endpoints in units.values() for d in endpoints]
    probed = _probe_vibration_with_retry(
        vib_endpoints, all_gigaport_endpoints=all_giga
    )
    if probed is None:
        raise RuntimeError(_vibration_open_error(vib, vib_endpoints))
    vib, vib_ch = probed
    if vib_ch < 8:
        meta["warnings"].append(
            f"Vibration device supports {vib_ch} ch (wanted 8) — zones may be limited"
        )
    vib = {**vib, "probed_channels": vib_ch}
    _log_topology(all_devs, units, layout="vibration_only", vib=vib, audio=None)
    return vib, None, "vibration_only", meta


def build_audio_settings(
    *,
    active: bool = False,
    output_layout: str | None = None,
    vibration_index: int | None = None,
    audio_index: int | None = None,
    output_name: str | None = None,
    engine_error: str | None = None,
    roles_flipped: bool = False,
) -> dict[str, Any]:
    """Snapshot for the Settings screen."""
    all_devices = list_output_devices(include_all=True)
    display_devs = settings_display_devices(all_devices)
    gigaports = [d for d in display_devs if d.get("is_gigaport")]
    try:
        picked_vib, picked_audio, layout, meta = pick_output_devices(
            vibration_index=vibration_index,
            audio_index=audio_index,
            roles_flipped=roles_flipped,
        )
        auto_ok = True
        auto_error = None
    except Exception as exc:
        picked_vib, picked_audio, layout, meta = None, None, "single", {"warnings": []}
        auto_ok = False
        auto_error = str(exc)

    sel_vib_idx = vibration_index
    sel_aud_idx = audio_index

    rows: list[dict[str, Any]] = []
    for d in display_devs:
        short = _short_device_name(d["name"])
        status = "ready"
        if active and picked_vib and d["index"] == picked_vib["index"]:
            status = "active"
        elif active and picked_audio and d["index"] == picked_audio["index"]:
            status = "active"
        elif vibration_index is not None and d["index"] == vibration_index:
            status = "selected"
        elif audio_index is not None and d["index"] == audio_index:
            status = "selected"
        rows.append({
            **d,
            "short_name": short,
            "status": status,
        })

    return {
        "backend": "wasapi/wdm-ks",
        "asio_enabled": False,
        "devices": rows,
        "gigaport_count": len(gigaports),
        "auto_pick_ok": auto_ok,
        "auto_pick_error": auto_error,
        "picked_vibration": picked_vib,
        "picked_audio": picked_audio,
        "selected_vibration_index": sel_vib_idx,
        "selected_audio_index": sel_aud_idx,
        "in_use_vibration_index": picked_vib["index"] if active and picked_vib else None,
        "in_use_audio_index": picked_audio["index"] if active and picked_audio else None,
        "output_layout": output_layout or layout,
        "active": active,
        "output_name": output_name,
        "engine_error": engine_error,
        "warnings": meta.get("warnings") or [],
        "roles_flipped": bool(roles_flipped),
        "can_swap_gigaports": len(gigaports) >= 2,
    }
