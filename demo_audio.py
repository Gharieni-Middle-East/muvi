"""Demo track playback — same Live → Gigaport vibration path as Bluetooth.

Uses assets/demo.wav. Isolated from AUX/Bluetooth capture selection so those
modes stay unchanged; shares the worker's LiveAudioEngine instance.
"""

from __future__ import annotations

import os
import time
import wave
from typing import Any

import numpy as np
from sounddevice import PortAudioError

from audio.gigaport_output import GigaportOutput
from audio.live_input import DEFAULT_VIBE_SYNC_MS, LiveAudioEngine

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_WAV_PATH = os.path.join(_APP_DIR, "assets", "demo.wav")

# Shared with live_audio when both run in engine_worker (same process).
_engine: LiveAudioEngine | None = None
_gigaport: GigaportOutput | None = None
_demo_meta: dict[str, Any] = {
    "path": DEMO_WAV_PATH,
    "duration": 0.0,
    "title": "Demo",
    "artist": "MUVI test track",
    "album": "",
}


def bind_engine(engine: LiveAudioEngine, gigaport: GigaportOutput) -> None:
    """Attach the same engine/gigaport instances used by Start Live."""
    global _engine, _gigaport
    _engine = engine
    _gigaport = gigaport


def resolve_demo_path(path: str | None = None) -> str:
    candidate = path or DEMO_WAV_PATH
    if not os.path.isfile(candidate):
        raise FileNotFoundError(
            f"Demo file not found: {candidate}\n"
            "Place demo.wav in new/assets/demo.wav"
        )
    return candidate


def load_wav_stereo(path: str) -> tuple[np.ndarray, int]:
    """Load a PCM WAV as float32 stereo (n, 2)."""
    with wave.open(path, "rb") as wf:
        channels = int(wf.getnchannels())
        width = int(wf.getsampwidth())
        sr = int(wf.getframerate())
        nframes = int(wf.getnframes())
        raw = wf.readframes(nframes)

    if width == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        data = (data - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 3:
        # 24-bit packed little-endian
        a = np.frombuffer(raw, dtype=np.uint8).astype(np.int32)
        if len(a) % 3:
            a = a[: len(a) - (len(a) % 3)]
        b = a.reshape(-1, 3)
        signed = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)).astype(np.int32)
        signed = np.where(signed >= 0x800000, signed - 0x1000000, signed)
        data = signed.astype(np.float32) / 8388608.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"Unsupported WAV sample width: {width}")

    if channels <= 0:
        raise RuntimeError("Invalid WAV channel count")
    if data.size % channels:
        data = data[: data.size - (data.size % channels)]
    frames = data.reshape(-1, channels)
    if channels == 1:
        stereo = np.repeat(frames, 2, axis=1)
    else:
        stereo = frames[:, :2]
    return stereo.astype(np.float32), sr


def demo_track_info(path: str | None = None) -> dict[str, Any]:
    resolved = resolve_demo_path(path)
    pcm, sr = load_wav_stereo(resolved)
    duration = float(len(pcm)) / float(sr) if sr > 0 else 0.0
    info = {
        "path": resolved,
        "duration": duration,
        "title": "Demo",
        "artist": "MUVI test track",
        "album": "",
        "artwork": "assets/artwork.svg",
    }
    _demo_meta.update(info)
    return dict(info)


def start_demo_audio(
    *,
    volume: float = 0.70,
    mid: float = 0.27,
    legs: float = 0.27,
    upper: float = 0.27,
    head: float = 0.27,
    vibe_sync_ms: float = DEFAULT_VIBE_SYNC_MS,
    highpass_hz: float = 30.0,
    cutoff_hz: float = 200.0,
    path: str | None = None,
    vibration_mode: str = "haptics",
    vibration_output_index: int | None = None,
    audio_output_index: int | None = None,
    roles_flipped: bool = False,
    loop: bool = True,
) -> dict[str, Any]:
    """Play demo.wav through LiveAudioEngine → Gigaport (same as Bluetooth Live)."""
    if _engine is None or _gigaport is None:
        raise RuntimeError("Demo engine not bound — start via engine_worker")

    resolved = resolve_demo_path(path)
    pcm, sr = load_wav_stereo(resolved)
    duration = float(len(pcm)) / float(sr) if sr > 0 else 0.0
    _demo_meta.update(
        {
            "path": resolved,
            "duration": duration,
            "title": "Demo",
            "artist": "MUVI test track",
            "album": "",
        }
    )

    from live_audio import _pick_output_devices

    # Release any previous stream BEFORE probing — same lock issue as live start.
    if _engine is not None and _engine.is_active:
        _engine.stop()
    _gigaport.stop()
    _gigaport.release_output_device()
    # Let Windows finish releasing the device before PortAudio probes again.
    time.sleep(0.2)

    output_dev, audio_dev, layout, _pick_meta = _pick_output_devices(
        vibration_index=vibration_output_index,
        audio_index=audio_output_index,
        roles_flipped=roles_flipped,
    )
    output_index = output_dev["index"]
    audio_output_index = audio_dev["index"] if audio_dev is not None else None
    output_name = (
        f"{output_dev['name']} ({output_dev.get('probed_channels', output_dev['channels'])} ch, "
        f"{output_dev['hostapi']}, #{output_index})"
    )
    if audio_dev is not None and audio_output_index is not None:
        output_name += (
            f" + Audio {audio_dev['name']} ({audio_dev['channels']} ch, "
            f"{audio_dev['hostapi']}, #{audio_output_index})"
        )

    _engine.set_output_layout(layout)
    _engine.set_vibration_mode(vibration_mode)  # type: ignore[arg-type]
    _engine.set_output_channel_plan(
        vibration_channels=int(
            output_dev.get(
                "probed_channels",
                8 if layout in ("dual_native", "vibration_only") else 6,
            )
        ),
        audio_channels=int(audio_dev.get("probed_channels", 4)) if audio_dev else None,
    )
    _engine.set_volume(volume)
    _engine.set_vibe_sync_ms(vibe_sync_ms)
    _engine.set_zone_intensities(mid=mid, legs=legs, upper=upper, head=head)
    _engine.set_highpass_hz(highpass_hz)
    _engine.set_lowpass_hz(cutoff_hz)

    print(
        f"[demo_audio] playing {resolved!r} ({duration:.1f}s @ {sr} Hz) → {output_name}",
        flush=True,
    )

    try:
        _engine.start_from_pcm(
            pcm,
            sr,
            output_index,
            audio_output_device_index=audio_output_index,
            loop=loop,
        )
    except (PortAudioError, RuntimeError, OSError) as exc:
        raise RuntimeError(str(exc)) from exc

    return {
        "ok": True,
        "capture": os.path.basename(resolved),
        "output_index": output_index,
        "output_name": output_name,
        "mode": "demo",
        "output_layout": layout,
        "audio_output_channels": int(audio_dev.get("probed_channels", audio_dev.get("channels", 0))) if audio_dev else 0,
        "vibration_mode": vibration_mode,
        "duration": duration,
        "path": resolved,
        "title": _demo_meta["title"],
        "artist": _demo_meta["artist"],
        "album": _demo_meta["album"],
    }


def stop_demo_audio() -> None:
    if _engine is not None and _engine.is_active:
        _engine.stop()


def seek_demo(seconds: float) -> dict[str, float]:
    if _engine is None:
        return {"position": 0.0, "duration": float(_demo_meta.get("duration") or 0.0)}
    _engine.seek_file(float(seconds))
    return get_demo_progress()


def get_demo_progress() -> dict[str, float]:
    if _engine is None:
        return {
            "position": 0.0,
            "duration": float(_demo_meta.get("duration") or 0.0),
            "fraction": 0.0,
        }
    progress = _engine.get_file_progress()
    if progress.get("duration", 0) <= 0 and _demo_meta.get("duration"):
        progress["duration"] = float(_demo_meta["duration"])
    return progress


def is_demo_active() -> bool:
    return bool(_engine is not None and _engine.is_active)
