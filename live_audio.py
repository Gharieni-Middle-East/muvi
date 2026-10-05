"""Start Live / Stop Live.

Modes (auto):
  1) Bluetooth connected in app → CABLE → Gigaport output (WASAPI/WDM-KS)
  2) Otherwise AUX if Behringer/USB interface present → Gigaport output (WASAPI/WDM-KS)
  3) Else CABLE / default capture

So a plugged-in Behringer no longer steals capture while the phone is on Bluetooth.
"""

from __future__ import annotations

import time
from typing import Any

from sounddevice import PortAudioError

from audio.gigaport_output import GigaportOutput
from audio.gigaport_routing import OutputLayout, SpeakerRoute, VibrationMode
from audio.output_devices import pick_output_devices
from audio.live_input import (
    DEFAULT_VIBE_SYNC_MS,
    LiveAudioEngine,
    find_gigaport_loopback_device,
    pick_aux_capture_device,
    pick_cable_capture_device,
    pick_default_capture_device,
)
from windows_cable import ensure_cable_default_playback

# Same as MainWindow.__init__
_gigaport = GigaportOutput()
_live_engine = LiveAudioEngine()

_vibration_base = 0.27
_vibration_muted = False
_zone_enabled: dict[str, bool] = {
    "head": True,
    "upper": True,
    "mid": True,
    "legs": True,
}


def _intensities_for_base(base: float) -> dict[str, float]:
    if _vibration_muted:
        return {"head": 0.0, "upper": 0.0, "mid": 0.0, "legs": 0.0}
    b = max(0.0, min(1.0, float(base)))
    return {
        "head": b if _zone_enabled["head"] else 0.0,
        "upper": b if _zone_enabled["upper"] else 0.0,
        "mid": b if _zone_enabled["mid"] else 0.0,
        "legs": b if _zone_enabled["legs"] else 0.0,
    }


def sync_zone_enabled(enabled: dict[str, bool] | None) -> None:
    if not enabled:
        return
    for key in _zone_enabled:
        if key in enabled:
            _zone_enabled[key] = bool(enabled[key])


def prepare_zone_intensities(
    base: float,
    zone_enabled: dict[str, bool] | None = None,
    *,
    vibration_muted: bool | None = None,
) -> dict[str, float]:
    """Sync UI zone flags and return per-zone gains for engine start."""
    global _vibration_base, _vibration_muted
    sync_zone_enabled(zone_enabled)
    if vibration_muted is not None:
        _vibration_muted = bool(vibration_muted)
    _vibration_base = max(0.0, min(1.0, float(base)))
    return _intensities_for_base(_vibration_base)


def apply_zone_intensities(base: float) -> None:
    global _vibration_base
    _vibration_base = max(0.0, min(1.0, float(base)))
    zi = _intensities_for_base(_vibration_base)
    _live_engine.set_zone_intensities(
        mid=zi["mid"],
        legs=zi["legs"],
        upper=zi["upper"],
        head=zi["head"],
    )


def get_zone_enabled() -> dict[str, bool]:
    return dict(_zone_enabled)


def set_vibration_muted(muted: bool) -> None:
    global _vibration_muted
    _vibration_muted = bool(muted)
    apply_zone_intensities(_vibration_base)


def get_vibration_muted() -> bool:
    return bool(_vibration_muted)

# Demo shares this engine so Start Live / Demo never fight over the output device.
try:
    import demo_audio as _demo_audio

    _demo_audio.bind_engine(_live_engine, _gigaport)
except Exception:  # noqa: BLE001
    pass


def _log_input_devices() -> None:
    """Print PortAudio inputs (once at Live start, for tablet debugging)."""
    try:
        import sounddevice as sd

        hostapis = sd.query_hostapis()
        print("[live_audio] PortAudio INPUT devices:", flush=True)
        for idx, d in enumerate(sd.query_devices()):
            if int(d["max_input_channels"]) <= 0:
                continue
            try:
                api = str(hostapis[int(d["hostapi"])]["name"])
            except Exception:  # noqa: BLE001
                api = "?"
            print(
                f"  [{idx}] {d['name']} — {api} in={d['max_input_channels']}",
                flush=True,
            )
        try:
            default_in = sd.default.device[0]
            print(f"[live_audio] default input index={default_in}", flush=True)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"[live_audio] input list failed: {exc}", flush=True)


def _pick_cable_or_default() -> tuple[dict[str, Any], str]:
    capture = pick_cable_capture_device()
    if capture is not None:
        return capture, "cable"

    capture = pick_default_capture_device()
    if capture is None:
        raise RuntimeError(
            "No capture device. Plug in Behringer (AUX) or install VB-Audio Virtual Cable (Bluetooth)."
        )
    name = str(capture.get("name", "")).lower()
    if "gigaport" in name or capture.get("is_gigaport"):
        raise RuntimeError(
            "No Behringer/CABLE capture found. Connect AUX interface or set up VB-Cable."
        )
    return capture, "cable"


def _pick_capture(
    *,
    vibration_overlay: bool,
    prefer_bluetooth: bool = False,
    log: bool = False,
) -> tuple[dict[str, Any], str]:
    """Return (capture_device, mode) where mode is 'aux' | 'cable' | 'overlay'.

    Priority:
      - Bluetooth connected → CABLE first (phone is streaming over BT)
      - No Bluetooth → AUX first if interface present, else CABLE
    """
    if log:
        _log_input_devices()

    if vibration_overlay:
        gigaport_lb = find_gigaport_loopback_device()
        if gigaport_lb is not None:
            return gigaport_lb, "overlay"

    aux = pick_aux_capture_device()

    if prefer_bluetooth:
        try:
            return _pick_cable_or_default()
        except RuntimeError:
            if aux is not None:
                return aux, "aux"
            raise

    if aux is not None:
        return aux, "aux"

    return _pick_cable_or_default()


def probe_live_input(
    *,
    prefer_bluetooth: bool = False,
    vibration_overlay: bool = False,
) -> dict[str, Any]:
    """Check whether Now Playing can start (AUX line-in or Bluetooth/CABLE only).

    Bare VB-Cable without an app Bluetooth session is NOT ready — otherwise the
    Play button stays enabled with nothing to capture.
    """
    not_ready_msg = "Connect AUX or Bluetooth to start."
    try:
        capture, mode = _pick_capture(
            vibration_overlay=vibration_overlay,
            prefer_bluetooth=prefer_bluetooth,
            log=False,
        )
    except RuntimeError as exc:
        return {
            "ready": False,
            "mode": None,
            "capture_name": None,
            "message": str(exc) or not_ready_msg,
        }

    # CABLE alone (no Bluetooth connected in the app) is not a valid Live source.
    if mode == "cable" and not prefer_bluetooth:
        return {
            "ready": False,
            "mode": None,
            "capture_name": str(capture.get("name") or ""),
            "message": not_ready_msg,
        }

    if mode not in ("aux", "cable"):
        return {
            "ready": False,
            "mode": None,
            "capture_name": str(capture.get("name") or ""),
            "message": not_ready_msg,
        }

    name = str(capture.get("name") or "")
    if mode == "aux":
        message = "AUX ready"
    else:
        message = "Bluetooth ready"

    return {
        "ready": True,
        "mode": mode,
        "capture_name": name,
        "message": message,
    }


def _pick_output_devices(
    *,
    vibration_index: int | None = None,
    audio_index: int | None = None,
    roles_flipped: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None, OutputLayout, dict[str, Any]]:
    """WASAPI/WDM-KS Gigaports only — no ASIO."""
    return pick_output_devices(
        vibration_index=vibration_index,
        audio_index=audio_index,
        roles_flipped=roles_flipped,
    )


def start_live_audio(
    *,
    volume: float = 0.85,
    mid: float = 1.0,
    legs: float = 1.0,
    upper: float = 1.0,
    head: float = 1.0,
    vibe_sync_ms: float = DEFAULT_VIBE_SYNC_MS,
    highpass_hz: float = 30.0,
    cutoff_hz: float = 180.0,
    vibration_overlay: bool = False,
    segmentation_id: str = "default",
    frequency_profile_id: str = "satori",
    synthetic_vibro: bool = False,
    synthetic_type_id: str = "sine",
    prefer_bluetooth: bool = False,
    speaker_route: SpeakerRoute = "headphones",
    vibration_mode: VibrationMode = "haptics",
    vibration_output_index: int | None = None,
    audio_output_index: int | None = None,
    roles_flipped: bool = False,
) -> dict[str, Any]:
    """Start Live: BT session → CABLE; else AUX if present; else CABLE."""
    # Release any previous Gigaport stream BEFORE probing — a held exclusive
    # lock makes check_output_settings fail on every endpoint (WDM-KS or WASAPI).
    if _live_engine.is_active:
        _live_engine.stop()
    _gigaport.stop()
    _gigaport.release_output_device()
    # Let Windows finish releasing the device before PortAudio probes again.
    time.sleep(0.2)

    capture, mode = _pick_capture(
        vibration_overlay=vibration_overlay,
        prefer_bluetooth=prefer_bluetooth,
        log=True,
    )

    cable_route: dict[str, Any] = {"ok": True, "skipped": True}
    if mode == "cable":
        # Only force CABLE as Windows default for Bluetooth phone routing.
        cable_route = ensure_cable_default_playback()
        if not cable_route.get("ok") and not cable_route.get("skipped"):
            print(f"[live_audio] warn: {cable_route.get('error')}")

    vib_dev, audio_dev, layout, pick_meta = _pick_output_devices(
        vibration_index=vibration_output_index,
        audio_index=audio_output_index,
        roles_flipped=roles_flipped,
    )
    output_index = vib_dev["index"]
    audio_output_index = audio_dev["index"] if audio_dev is not None else None
    output_name = (
        f"{vib_dev['name']} ({vib_dev.get('probed_channels', vib_dev['channels'])} ch, "
        f"{vib_dev['hostapi']}, #{output_index})"
    )
    if audio_dev is not None and audio_output_index is not None:
        output_name += (
            f" + Audio {audio_dev['name']} ({audio_dev['channels']} ch, "
            f"{audio_dev['hostapi']}, #{audio_output_index})"
        )

    print(
        f"[live_audio] mode={mode} layout={layout} speaker={speaker_route} "
        f"prefer_bt={prefer_bluetooth} "
        f"capture={capture.get('name')!r} "
        f"backend={capture.get('backend')} "
        f"output={output_name!r}",
        flush=True,
    )

    _live_engine.set_output_layout(layout)
    _live_engine.set_speaker_route(speaker_route)
    _live_engine.set_vibration_mode(vibration_mode)
    _live_engine.set_output_channel_plan(
        vibration_channels=int(
            vib_dev.get(
                "probed_channels",
                8 if layout in ("dual_native", "vibration_only") else 6,
            )
        ),
        audio_channels=int(audio_dev.get("probed_channels", 4)) if audio_dev else None,
    )
    _live_engine.set_volume(volume)
    _live_engine.set_vibe_sync_ms(vibe_sync_ms)
    _live_engine.set_zone_intensities(mid=mid, legs=legs, upper=upper, head=head)
    _live_engine.set_highpass_hz(highpass_hz)
    _live_engine.set_lowpass_hz(cutoff_hz)

    try:
        _live_engine.start(
            capture_device=capture,
            output_device_index=output_index,
            audio_output_device_index=audio_output_index,
            loopback=bool(capture.get("loopback")),
            capture_channels=int(capture.get("channels", 2)),
            segmentation_id=segmentation_id,
            frequency_profile_id=frequency_profile_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
            vibration_overlay=vibration_overlay,
        )
    except (PortAudioError, RuntimeError, OSError) as exc:
        raise RuntimeError(str(exc)) from exc

    return {
        "ok": True,
        "capture": str(capture.get("name")),
        "output_index": output_index,
        "output_name": output_name,
        "vibration_overlay": vibration_overlay,
        "mode": mode,
        "output_layout": layout,
        "audio_output_channels": int(audio_dev.get("probed_channels", audio_dev.get("channels", 0))) if audio_dev else 0,
        "speaker_route": speaker_route,
        "vibration_mode": vibration_mode,
        "pick_warnings": pick_meta.get("warnings") or [],
        "cable_default": cable_route.get("name"),
    }


def stop_live_audio() -> None:
    if _live_engine.is_active:
        _live_engine.stop()


def set_volume(value: float) -> None:
    _live_engine.set_volume(value)


def set_audio_muted(muted: bool) -> None:
    _live_engine.set_audio_muted(muted)


def set_vibration(value: float) -> None:
    apply_zone_intensities(value)


def set_zone_enabled(zone: str, enabled: bool) -> None:
    key = str(zone).strip().lower()
    if key not in _zone_enabled:
        return
    _zone_enabled[key] = bool(enabled)
    apply_zone_intensities(_vibration_base)


def set_highpass_hz(value: float) -> None:
    _live_engine.set_highpass_hz(value)


def set_cutoff_hz(value: float) -> None:
    """Bass cutoff = muvi vibe_lowpass_hz."""
    _live_engine.set_lowpass_hz(value)


def set_speaker_route(route: SpeakerRoute) -> None:
    _live_engine.set_speaker_route(route)


def set_vibration_mode(mode: VibrationMode) -> None:
    _live_engine.set_vibration_mode(mode)


def get_runtime_stats() -> dict[str, float]:
    return _live_engine.get_runtime_stats()


def get_audio_settings(
    *,
    active: bool = False,
    output_layout: str | None = None,
    vibration_index: int | None = None,
    audio_index: int | None = None,
    output_name: str | None = None,
    engine_error: str | None = None,
    roles_flipped: bool = False,
) -> dict[str, Any]:
    from audio.output_devices import build_audio_settings

    return build_audio_settings(
        active=active,
        output_layout=output_layout,
        vibration_index=vibration_index,
        audio_index=audio_index,
        output_name=output_name,
        engine_error=engine_error,
        roles_flipped=roles_flipped,
    )


def is_active() -> bool:
    return _live_engine.is_active
