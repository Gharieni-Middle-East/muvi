"""Real-time audio capture for Bluetooth / live input on Windows and other platforms."""

from __future__ import annotations

import collections
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import sounddevice as sd
from scipy.signal import butter, sosfilt, sosfilt_zi

from audio.audio_utils import TARGET_SR, ensure_stereo, fit_frames, resample_if_needed
from audio.soundcard_loopback import (
    SoundcardLoopbackThread,
    get_default_playback_speaker_id,
    list_speaker_loopback_devices,
    soundcard_available,
)
from audio.gigaport_routing import (
    OutputLayout,
    SpeakerRoute,
    VibrationMode,
    build_output_block,
    build_vibration_only_block,
    output_channels_for_layout,
)
from audio.haptics_dsp import HapticDSP
from audio.output_devices import wasapi_output_extra
from audio.vibration_presets import (
    SATORI_OUTPUT_CHANNELS,
)

# Vibration transducers map to Gigaport channels 2-5 (C, LFE, Ls, Rs).
GIGAPORT_VIBRATION_CHANNELS = (2, 3, 4, 5)
BLOCKSIZE = 2048
# Deep enough to smooth capture/output jitter without dropping vibration blocks.
RING_MAX_BLOCKS = 32
PROCESSING_SR = TARGET_SR

MUVI_HIGHPASS_HZ = 30.0
MUVI_LOWPASS_HZ = 180.0
MUVI_FILTER_ORDER = 4
MUVI_LIMIT_DB = -1.0
MUVI_MASTER_GAIN = 1.0
# Sync slider centre. Real-time capture can only DELAY vibration, so the slider
# shifts around a baseline delay: positive = vibration later, negative = earlier.
DEFAULT_VIBE_SYNC_MS = 0.0
# Baseline vibration delay (ms) applied at slider = 0, giving headroom to move earlier.
BASELINE_DELAY_MS = 150.0
# Hard ceiling on total vibration delay.
MAX_DELAY_MS = 4000.0


def _looks_like_bluetooth(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in ("bluetooth", "bt ", "hands-free", "hands free", "a2dp"))


def _is_hands_free(name: str) -> bool:
    lowered = name.lower()
    return any(
        token in lowered
        for token in (
            "hands-free",
            "hands free",
            "hands-free hf",
            "hf audio",
            "ag audio",
            "bthhfenum",
        )
    )


def _is_stereo_bluetooth(name: str) -> bool:
    lowered = name.lower()
    if _is_hands_free(name):
        return False
    return _looks_like_bluetooth(name) or any(
        token in lowered for token in ("stereo", "a2dp", "avrcp", "bthhfenum.sys,#2")
    )


def _is_loopback_name(name: str) -> bool:
    return "loopback" in name.lower()


def _wasapi_hostapi_index() -> int | None:
    for idx, api in enumerate(sd.query_hostapis()):
        if "wasapi" in str(api["name"]).lower():
            return idx
    return None


def _find_loopback_input_index(output_name: str) -> int | None:
    """Match a WASAPI output name to its [Loopback] input sibling, if PortAudio exposes one."""
    base = output_name.strip().lower()
    for idx, dev in enumerate(sd.query_devices()):
        if int(dev["max_input_channels"]) <= 0:
            continue
        name = str(dev["name"])
        if not _is_loopback_name(name):
            continue
        stem = name.lower().replace("[loopback]", "").replace("(loopback)", "").strip()
        if base in stem or stem in base:
            return idx
    return None


def list_capture_devices() -> list[dict[str, Any]]:
    """Return capture devices: stereo inputs and speaker loopback (not Hands-Free)."""
    devices: list[dict[str, Any]] = []
    seen_indices: set[int] = set()

    for idx, dev in enumerate(sd.query_devices()):
        if int(dev["max_input_channels"]) <= 0:
            continue
        name = str(dev["name"])
        if _is_hands_free(name):
            continue
        seen_indices.add(idx)
        devices.append(
            {
                "backend": "sounddevice",
                "index": idx,
                "name": name,
                "loopback": _is_loopback_name(name),
                "channels": min(2, int(dev["max_input_channels"])),
                "is_bluetooth": _looks_like_bluetooth(name),
                "is_hands_free": False,
                "is_stereo_bluetooth": _is_stereo_bluetooth(name),
                "hostapi": int(dev["hostapi"]),
            }
        )

    if sys.platform == "win32":
        wasapi_idx = _wasapi_hostapi_index()
        if wasapi_idx is not None:
            for idx, dev in enumerate(sd.query_devices()):
                if int(dev["hostapi"]) != wasapi_idx:
                    continue
                if int(dev["max_output_channels"]) <= 0:
                    continue
                name = str(dev["name"])
                if _is_loopback_name(name):
                    continue
                loopback_idx = _find_loopback_input_index(name)
                if loopback_idx is not None and loopback_idx not in seen_indices:
                    seen_indices.add(loopback_idx)
                    devices.append(
                        {
                            "backend": "sounddevice",
                            "index": loopback_idx,
                            "name": f"{name} [Loopback]",
                            "loopback": True,
                            "channels": min(2, int(sd.query_devices(loopback_idx)["max_input_channels"])),
                            "is_bluetooth": _looks_like_bluetooth(name),
                            "is_hands_free": False,
                            "is_stereo_bluetooth": _looks_like_bluetooth(name),
                            "hostapi": wasapi_idx,
                        }
                    )

        for sc_dev in list_speaker_loopback_devices():
            sc_name = sc_dev["name"].lower()
            if any(d.get("backend") == "soundcard" and d["name"].lower() == sc_name for d in devices):
                continue
            devices.append(
                {
                    **sc_dev,
                    "is_bluetooth": _looks_like_bluetooth(sc_dev["name"]),
                    "is_hands_free": False,
                    "is_stereo_bluetooth": _looks_like_bluetooth(sc_dev["name"]),
                }
            )

    devices.sort(
        key=lambda d: (
            not d.get("is_default_playback", False),
            not d.get("is_bluetooth", False),
            not d.get("loopback", False),
            d["name"].lower(),
        )
    )
    return devices


def pick_default_capture_device() -> dict[str, Any] | None:
    """Best default: Windows default playback speaker loopback."""
    devices = list_capture_devices()
    default_id = get_default_playback_speaker_id()
    if default_id:
        for dev in devices:
            if dev.get("soundcard_id") == default_id:
                return dev
            if default_id in str(dev.get("name", "")):
                return dev
    for dev in devices:
        if dev.get("is_default_playback"):
            return dev
    for dev in devices:
        if dev.get("is_bluetooth") and dev.get("backend") == "soundcard":
            return dev
    for dev in devices:
        if dev.get("loopback"):
            return dev
    return devices[0] if devices else None


def pick_cable_capture_device() -> dict[str, Any] | None:
    """VB-Cable path used with the old app: phone → CABLE Input → capture CABLE.

    Prefer CABLE Output / CABLE Input [Speaker Loopback]. Never Gigaport.
    """
    devices = list_capture_devices()

    def _name(dev: dict[str, Any]) -> str:
        return str(dev.get("name", "")).lower()

    def _is_gigaport(dev: dict[str, Any]) -> bool:
        return bool(dev.get("is_gigaport")) or "gigaport" in _name(dev)

    cableish = [d for d in devices if not _is_gigaport(d) and (
        "cable" in _name(d) or "vb-audio" in _name(d)
    )]

    # 1) CABLE Output (recording end) — what you hear after phone hits CABLE Input.
    for key in ("cable output", "vb-audio point"):
        for dev in cableish:
            if key in _name(dev):
                return dev

    # 2) CABLE Input speaker loopback (★ DEFAULT when Windows playback = CABLE Input).
    for dev in cableish:
        name = _name(dev)
        if "output" in name and "input" not in name:
            continue
        if dev.get("loopback") or "loopback" in name or "speaker" in name:
            return dev

    # 3) Any remaining CABLE / VB-Audio capture that is not a bare "CABLE In" mic.
    for dev in cableish:
        name = _name(dev)
        if name.startswith("cable in") and not (
            dev.get("loopback") or "loopback" in name or "speaker" in name
        ):
            continue
        return dev

    # 4) Windows default playback loopback if it is CABLE (old app Start Live default).
    default = pick_default_capture_device()
    if default is not None and not _is_gigaport(default):
        if "cable" in _name(default) or "vb-audio" in _name(default):
            return default

    return None


def pick_aux_capture_device() -> dict[str, Any] | None:
    """USB audio interface line-in (Behringer, etc.) — real input, not loopback/CABLE.

    Also accepts Windows default recording device when it is an external USB
    interface (many Behringers show up only as 'USB Audio CODEC').
    """
    keywords = (
        "behringer",
        "umc",
        "u-phoria",
        "uphoria",
        "xenyx",
        "focusrite",
        "scarlett",
        "m-audio",
        "m-track",
        "steinberg",
        "ur22",
        "ur12",
        "yamaha ag",
        "presonus",
        "audient",
        "line in",
        "line-in",
        "usb audio",
        "usb audio codec",
        "usb audio device",
        "usb audio 2.0",
    )
    builtin = (
        "realtek",
        "intel",
        "conexant",
        "nvidia",
        "amd ",
        "high definition audio",
        "headphones",
        "headset",
        "airpods",
        "hands-free",
        "stereo mix",
        "macbook",
        "imac",
        "built-in",
        "builtin",
        "internal",
        "facetime",
        "microsoft teams",
        "teams audio",
    )

    def _name(dev: dict[str, Any]) -> str:
        return str(dev.get("name", "")).lower()

    def _skip_name(name: str) -> bool:
        if "gigaport" in name:
            return True
        if "cable" in name or "vb-audio" in name:
            return True
        if "loopback" in name:
            return True
        if _is_hands_free(name):
            return True
        return False

    def _is_aux_name(name: str) -> bool:
        if _skip_name(name):
            return False
        if any(k in name for k in keywords):
            return True
        # External USB generic names (class-compliant Behringer, etc.)
        if "usb" in name and not any(b in name for b in builtin):
            return True
        return False

    def _score(name: str, backend: str, has_index: bool) -> int:
        score = 0
        if "behringer" in name:
            score += 30
        if any(k in name for k in ("umc", "u-phoria", "uphoria", "xenyx")):
            score += 15
        if "usb audio" in name or "usb audio codec" in name:
            score += 12
        if "line" in name:
            score += 8
        if "microphone" in name and "usb" in name:
            score += 6
        if backend == "sounddevice" and has_index:
            score += 5
        if any(b in name for b in builtin):
            score -= 20
        return score

    # --- Direct PortAudio scan (most reliable on Windows) ---
    scored: list[tuple[int, dict[str, Any]]] = []
    try:
        hostapis = sd.query_hostapis()
        for idx, raw in enumerate(sd.query_devices()):
            if int(raw["max_input_channels"]) <= 0:
                continue
            name = str(raw["name"])
            name_l = name.lower()
            if not _is_aux_name(name_l):
                continue
            try:
                api = str(hostapis[int(raw["hostapi"])]["name"])
            except Exception:  # noqa: BLE001
                api = ""
            # Prefer WASAPI / WDM-KS over MME for capture stability
            api_bonus = 0
            al = api.lower()
            if "wasapi" in al:
                api_bonus = 4
            elif "wdm" in al:
                api_bonus = 2
            elif "mme" in al:
                api_bonus = -2
            dev = {
                "backend": "sounddevice",
                "index": idx,
                "name": name,
                "loopback": False,
                "channels": min(2, int(raw["max_input_channels"])),
                "is_bluetooth": False,
                "is_hands_free": False,
                "hostapi": int(raw["hostapi"]),
                "hostapi_name": api,
            }
            scored.append((_score(name_l, "sounddevice", True) + api_bonus, dev))
    except Exception as exc:  # noqa: BLE001
        print(f"[pick_aux] PortAudio scan failed: {exc}")

    # --- Windows default recording device (what Sound Recorder uses) ---
    try:
        default_pair = sd.default.device
        default_in = default_pair[0] if default_pair is not None else None
        if isinstance(default_in, int) and default_in >= 0:
            raw = sd.query_devices(default_in)
            name = str(raw["name"])
            name_l = name.lower()
            if int(raw["max_input_channels"]) > 0 and not _skip_name(name_l):
                if _is_aux_name(name_l):
                    hostapis = sd.query_hostapis()
                    try:
                        api = str(hostapis[int(raw["hostapi"])]["name"])
                    except Exception:  # noqa: BLE001
                        api = ""
                    dev = {
                        "backend": "sounddevice",
                        "index": default_in,
                        "name": f"{name} [DEFAULT RECORD]",
                        "loopback": False,
                        "channels": min(2, int(raw["max_input_channels"])),
                        "is_bluetooth": False,
                        "is_hands_free": False,
                        "hostapi": int(raw["hostapi"]),
                        "hostapi_name": api,
                        "is_default_record": True,
                    }
                    scored.append((_score(name_l, "sounddevice", True) + 25, dev))
    except Exception as exc:  # noqa: BLE001
        print(f"[pick_aux] default input probe failed: {exc}")

    # --- Also check list_capture_devices() ---
    for dev in list_capture_devices():
        if dev.get("loopback") or _skip_name(_name(dev)):
            continue
        name_l = _name(dev)
        if not _is_aux_name(name_l):
            continue
        scored.append(
            (
                _score(name_l, str(dev.get("backend")), dev.get("index") is not None),
                dev,
            )
        )

    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], _name(t[1])))
    chosen = scored[0][1]
    return chosen


def find_gigaport_loopback_device() -> dict[str, Any] | None:
    """Find speaker loopback for Gigaport (for AudioPlaybackConnector overlay capture)."""
    for dev in list_capture_devices():
        if dev.get("is_gigaport"):
            return dev
    for dev in list_capture_devices():
        name = str(dev.get("name", "")).lower()
        if "gigaport" in name and dev.get("loopback"):
            return dev
    return None


@dataclass(frozen=True)
class InputStreamConfig:
    sample_rate: int
    channels: int
    extra_settings: Any | None
    latency: str


def _wasapi_auto_convert() -> Any | None:
    if sys.platform != "win32":
        return None
    try:
        return sd.WasapiSettings(auto_convert=True)
    except Exception:
        return None


def _enumerate_input_configs(device_index: int) -> list[InputStreamConfig]:
    """Build ordered list of input open attempts for a capture device."""
    dev = sd.query_devices(device_index)
    name = str(dev["name"])
    max_ch = max(1, int(dev["max_input_channels"]))
    is_hfp = _is_hands_free(name)
    on_wasapi = _wasapi_hostapi_index() == int(dev["hostapi"])

    if is_hfp:
        channel_candidates = [1, 2] if max_ch >= 2 else [1]
        rate_candidates = [16000, 8000, 44100, 48000]
        latency_candidates = ["low", "high"]
    else:
        channel_candidates = [min(2, max_ch)]
        if max_ch >= 2:
            channel_candidates.append(1)
        rate_candidates = [PROCESSING_SR, 48000, 16000, 96000, 8000]
        default_sr = int(dev.get("default_samplerate") or 0)
        if default_sr and default_sr not in rate_candidates:
            rate_candidates.insert(1, default_sr)
        latency_candidates = ["high", "medium", "low"]

    extra_candidates: list[Any | None] = [None]
    wasapi_extra = _wasapi_auto_convert()
    if on_wasapi and wasapi_extra is not None:
        extra_candidates.insert(0, wasapi_extra)

    configs: list[InputStreamConfig] = []
    seen: set[tuple[int, int, str, bool]] = set()
    for extra in extra_candidates:
        for channels in channel_candidates:
            for sample_rate in rate_candidates:
                for latency in latency_candidates:
                    key = (sample_rate, channels, latency, extra is not None)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        kwargs: dict[str, Any] = {
                            "device": device_index,
                            "channels": channels,
                            "samplerate": sample_rate,
                            "dtype": "float32",
                        }
                        if extra is not None:
                            kwargs["extra_settings"] = extra
                        sd.check_input_settings(**kwargs)
                    except Exception:
                        continue
                    configs.append(
                        InputStreamConfig(
                            sample_rate=sample_rate,
                            channels=channels,
                            extra_settings=extra,
                            latency=latency,
                        )
                    )

    if not configs:
        fallback_ch = 1 if is_hfp else min(2, max_ch)
        fallback_sr = 16000 if is_hfp else PROCESSING_SR
        configs.append(
            InputStreamConfig(
                sample_rate=fallback_sr,
                channels=fallback_ch,
                extra_settings=wasapi_extra,
                latency="high" if is_hfp else "low",
            )
        )
    return configs


def _pick_output_sample_rate(output_index: int, channels: int = SATORI_OUTPUT_CHANNELS) -> int:
    candidates = [PROCESSING_SR, 48000, 96000, 88200, 44100]
    dev = sd.query_devices(output_index)
    default_sr = int(dev.get("default_samplerate") or 0)
    if default_sr and default_sr not in candidates:
        candidates.insert(1, default_sr)

    for sr in candidates:
        try:
            sd.check_output_settings(
                device=output_index,
                channels=channels,
                samplerate=sr,
                dtype="float32",
            )
            return sr
        except Exception:
            continue
    return PROCESSING_SR


def _pick_shared_output_sample_rate(
    vibration_output_index: int,
    audio_output_index: int,
    vibration_channels: int,
    audio_channels: int,
) -> int:
    candidates = [PROCESSING_SR, 48000, 44100, 96000, 88200]
    for idx in (vibration_output_index, audio_output_index):
        dev = sd.query_devices(idx)
        default_sr = int(dev.get("default_samplerate") or 0)
        if default_sr and default_sr not in candidates:
            candidates.insert(1, default_sr)

    for sr in candidates:
        try:
            sd.check_output_settings(
                device=vibration_output_index,
                channels=vibration_channels,
                samplerate=sr,
                dtype="float32",
            )
            sd.check_output_settings(
                device=audio_output_index,
                channels=audio_channels,
                samplerate=sr,
                dtype="float32",
            )
            return sr
        except Exception:
            continue
    return PROCESSING_SR


def _validate_output_device(
    output_index: int,
    layout: OutputLayout = "single",
    required_channels: int | None = None,
) -> None:
    dev = sd.query_devices(output_index)
    channels = int(dev["max_output_channels"])
    need = int(required_channels) if required_channels is not None else output_channels_for_layout(layout)
    if channels < need:
        if layout == "dual_asio4all":
            raise RuntimeError(
                f"Output device '{dev['name']}' has only {channels} channel(s). "
                f"Dual Gigaport via ASIO4ALL needs {need} channels. "
                "In ASIO4ALL: enable both Gigaports and set 8 outputs each."
            )
        raise RuntimeError(
            f"Output device '{dev['name']}' has only {channels} channel(s). "
            f"Satori needs {need} channels (Gigaport). "
            "Select Gigaport in Output Device, not Intel Speakers."
        )


class LiveStreamProcessor:
    """muvi-style stereo -> tactile zones (head, upper, legs, mid)."""

    def __init__(
        self,
        sample_rate: int = PROCESSING_SR,
        segmentation_id: str = "default",
        frequency_profile_id: str = "satori",
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
    ) -> None:
        self.sample_rate = sample_rate
        self.zone_gain: dict[str, float] = {"head": 1.0, "upper_mid": 1.0, "legs": 1.0, "mid": 1.0}
        self._highpass_hz = MUVI_HIGHPASS_HZ
        self._lowpass_hz = MUVI_LOWPASS_HZ
        self._rebuild_filters()
        self._limit = 10.0 ** (MUVI_LIMIT_DB / 20.0)

    def _rebuild_filters(self) -> None:
        nyq = self.sample_rate * 0.5
        lp = max(2.0, min(float(self._lowpass_hz), nyq - 1.0))
        stages = [butter(MUVI_FILTER_ORDER, lp / nyq, btype="low", output="sos")]
        # Fixed high-pass removes DC/subsonic rumble (muvi vibe_highpass_hz).
        if float(self._highpass_hz) > 0.5:
            hp = max(1.0, min(float(self._highpass_hz), lp - 1.0))
            stages.insert(0, butter(MUVI_FILTER_ORDER, hp / nyq, btype="high", output="sos"))
        self._sos = np.vstack(stages)
        self._zi_left = sosfilt_zi(self._sos)
        self._zi_right = sosfilt_zi(self._sos)

    def set_lowpass_hz(self, hz: float) -> None:
        """Bass cutoff (muvi vibe_lowpass_hz): highest frequency sent to motors."""
        self._lowpass_hz = float(np.clip(hz, 40.0, 250.0))
        self._rebuild_filters()

    def set_highpass_hz(self, hz: float) -> None:
        """Optional subsonic cut; kept for compatibility. Prefer set_lowpass_hz."""
        self._highpass_hz = float(np.clip(hz, 0.0, 120.0))
        self._rebuild_filters()

    def update_presets(
        self,
        segmentation_id: str,
        frequency_profile_id: str,
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
    ) -> None:
        # Presets are intentionally ignored in muvi-style live mode.
        return

    def set_zone_gains(self, head: float, upper_mid: float, legs: float, mid: float) -> None:
        self.zone_gain = {
            "head": float(np.clip(head, 0.0, 3.0)),
            "upper_mid": float(np.clip(upper_mid, 0.0, 3.0)),
            "legs": float(np.clip(legs, 0.0, 3.0)),
            "mid": float(np.clip(mid, 0.0, 3.0)),
        }

    def process_block(self, stereo: np.ndarray) -> dict[str, Any]:
        stereo = ensure_stereo(stereo).astype(np.float32, copy=False)
        left, self._zi_left = sosfilt(self._sos, stereo[:, 0], zi=self._zi_left)
        right, self._zi_right = sosfilt(self._sos, stereo[:, 1], zi=self._zi_right)

        head = np.clip(left * MUVI_MASTER_GAIN * self.zone_gain["head"], -self._limit, self._limit)
        upper = np.clip(right * MUVI_MASTER_GAIN * self.zone_gain["upper_mid"], -self._limit, self._limit)
        legs = np.clip(left * MUVI_MASTER_GAIN * self.zone_gain["legs"], -self._limit, self._limit)
        mid = np.clip(right * MUVI_MASTER_GAIN * self.zone_gain["mid"], -self._limit, self._limit)
        bass_energy = float(np.sqrt(np.mean((0.5 * (left + right)) ** 2) + 1e-10))
        return {
            "head": head.astype(np.float32),
            "upper_mid": upper.astype(np.float32),
            "legs": legs.astype(np.float32),
            "mid": mid.astype(np.float32),
            "rms_head": float(np.sqrt(np.mean(head**2) + 1e-10)),
            "rms_upper_mid": float(np.sqrt(np.mean(upper**2) + 1e-10)),
            "rms_legs": float(np.sqrt(np.mean(legs**2) + 1e-10)),
            "rms_mid": float(np.sqrt(np.mean(mid**2) + 1e-10)),
            "bass_energy": bass_energy,
            "generated_freq_hz": 0.0,
        }


class LiveAudioEngine:
    """Capture live/Bluetooth audio and route it through the 6-channel Satori output."""

    def __init__(self) -> None:
        self.input_stream: sd.InputStream | None = None
        self.output_stream: sd.OutputStream | None = None
        self.audio_output_stream: sd.OutputStream | None = None
        self._loopback_thread: SoundcardLoopbackThread | None = None
        self._loopback_stop = threading.Event()
        self._file_thread: threading.Thread | None = None
        self._file_pcm: np.ndarray | None = None
        self._file_pos = 0
        self._file_lock = threading.Lock()
        self._file_loop = True
        self._capture_ring = np.zeros((0, 2), dtype=np.float32)
        self._capture_read_idx = 0
        # Differential-delay rings (full mode): audio + vibration aligned to a
        # common absolute source index so each stream can be delayed independently.
        self._src_ring = np.zeros((0, 2), dtype=np.float32)
        self._vib_ring = np.zeros((0, 4), dtype=np.float32)
        self._ring_base = 0
        self._abs_processed = 0
        self._out_pos = 0
        self.vibe_sync_ms = DEFAULT_VIBE_SYNC_MS
        self.processor = LiveStreamProcessor()
        self.capture_device_index: int | None = None
        self.output_device_index: int | None = None
        self.audio_output_device_index: int | None = None
        self.capture_channels = 2
        self.input_sample_rate = PROCESSING_SR
        self.output_sample_rate = PROCESSING_SR
        self.is_active = False
        self.vibration_overlay = False
        self.output_layout: OutputLayout = "single"
        self.speaker_route: SpeakerRoute = "headphones"
        self.vibration_mode: VibrationMode = "haptics"
        self._haptic_dsp: HapticDSP | None = None
        self.output_channel_count = SATORI_OUTPUT_CHANNELS
        self.vibration_output_channels: int | None = None
        self.audio_output_channel_count = 4
        self.volume = 0.85
        self.audio_muted = False
        self.highpass_hz = MUVI_HIGHPASS_HZ
        self.lowpass_hz = MUVI_LOWPASS_HZ
        self.intensity_mid = 1.0
        self.intensity_legs = 1.0
        self.intensity_upper = 1.0
        self.intensity_head = 1.0
        self.lock = threading.Lock()
        self._output_ring: collections.deque[np.ndarray] = collections.deque(maxlen=RING_MAX_BLOCKS)
        self._audio_output_ring: collections.deque[np.ndarray] = collections.deque(maxlen=RING_MAX_BLOCKS)
        self._current_block: np.ndarray | None = None
        self._current_offset = 0
        self._audio_current_block: np.ndarray | None = None
        self._audio_current_offset = 0
        self._ingest_error_count = 0
        self._cb_frames = 0
        self._cb_silent_frames = 0
        self._audio_cb_frames = 0
        self._audio_cb_silent_frames = 0
        self.stats: dict[str, float] = {
            "rms_legs": 0.0,
            "rms_mid": 0.0,
            "rms_upper_mid": 0.0,
            "rms_head": 0.0,
            "bass_energy": 0.0,
            "input_rms": 0.0,
            "rms_audio_left": 0.0,
            "rms_audio_right": 0.0,
            "generated_freq_hz": 0.0,
            "cpu_usage_estimate": 0.0,
            "progress_fraction": 0.0,
            "vibe_sync_ms": DEFAULT_VIBE_SYNC_MS,
            "vibe_lag_ms": BASELINE_DELAY_MS,
            "audio_lag_ms": BASELINE_DELAY_MS,
        }

    def set_vibration_overlay(self, enabled: bool) -> None:
        self.vibration_overlay = enabled
        if enabled:
            self.output_channel_count = 4
        else:
            self.output_channel_count = output_channels_for_layout(self.output_layout)

    def set_output_layout(self, layout: OutputLayout) -> None:
        if layout not in ("single", "dual_asio4all", "dual_native", "vibration_only"):
            raise ValueError(f"Unknown output layout: {layout}")
        self.output_layout = layout
        if not self.vibration_overlay:
            self.output_channel_count = output_channels_for_layout(layout)

    def set_speaker_route(self, route: SpeakerRoute) -> None:
        if route not in ("headphones", "secondary"):
            raise ValueError(f"Unknown speaker route: {route}")
        self.speaker_route = route

    def set_vibration_mode(self, mode: VibrationMode) -> None:
        if mode not in ("zones", "stereo", "haptics"):
            raise ValueError(f"Unknown vibration mode: {mode}")
        prev = self.vibration_mode
        self.vibration_mode = mode
        # Reset Quake filter state when entering/leaving haptics to avoid bleed.
        if mode == "haptics" or prev == "haptics":
            self._reset_haptic_dsp()

    def _reset_haptic_dsp(self) -> None:
        self._haptic_dsp = None

    def _ensure_haptic_dsp(self) -> HapticDSP:
        if self._haptic_dsp is None:
            self._haptic_dsp = HapticDSP(
                fs=PROCESSING_SR,
                mode="smart",
                n_out=8,
                block=BLOCKSIZE,
            )
        return self._haptic_dsp

    def set_output_channel_plan(
        self,
        *,
        vibration_channels: int,
        audio_channels: int | None = None,
    ) -> None:
        self.vibration_output_channels = max(1, int(vibration_channels))
        if audio_channels is not None:
            self.audio_output_channel_count = max(1, int(audio_channels))
        if not self.vibration_overlay:
            self.output_channel_count = self.vibration_output_channels

    def _effective_audio_gain(self) -> float:
        return 0.0 if self.audio_muted else float(self.volume)

    def _passthrough_speakers(self, capture: np.ndarray) -> np.ndarray:
        """Fast L/R path — no vibration processing, avoids speaker dropouts."""
        if capture.ndim == 1:
            capture = capture.reshape(-1, 1)
        if capture.shape[1] == 1:
            capture = np.repeat(capture, 2, axis=1)
        stereo = ensure_stereo(capture)
        if self.input_sample_rate != PROCESSING_SR:
            stereo = resample_if_needed(stereo, self.input_sample_rate, PROCESSING_SR)
        if self.output_sample_rate != PROCESSING_SR:
            stereo = resample_if_needed(stereo, PROCESSING_SR, self.output_sample_rate)
        return (stereo * self._effective_audio_gain()).astype(np.float32)

    def _render_haptics_only(
        self,
        capture: np.ndarray,
        *,
        already_processing_sr: bool = False,
    ) -> np.ndarray:
        """Quake HapticDSP → 8 Gigaport channels with per-zone intensity applied."""
        if not already_processing_sr:
            capture = self._stereo_capture_block(capture)
        else:
            capture = ensure_stereo(capture).astype(np.float32, copy=False)
        frames = len(capture)
        if frames <= 0:
            return np.zeros((0, 8), dtype=np.float32)

        dsp = self._ensure_haptic_dsp()
        # HapticDSP expects (2, N)
        out = dsp.process(capture.T)  # (8, N)
        vib = out.T.astype(np.float32, copy=False)  # (N, 8)

        # Zone gains map onto channel pairs: head, upper, mid, legs.
        gains = (
            self.intensity_head,
            self.intensity_upper,
            self.intensity_mid,
            self.intensity_legs,
        )
        for pair_idx, g in enumerate(gains):
            lo = pair_idx * 2
            hi = lo + 2
            if g != 1.0:
                vib[:, lo:hi] *= float(g)

        if self.output_sample_rate != PROCESSING_SR:
            vib = resample_if_needed(vib, PROCESSING_SR, self.output_sample_rate)

        head = vib[:, 0:2]
        upper = vib[:, 2:4]
        mid = vib[:, 4:6]
        legs = vib[:, 6:8]
        rms_head = float(np.sqrt(np.mean(head**2) + 1e-10))
        rms_upper = float(np.sqrt(np.mean(upper**2) + 1e-10))
        rms_mid = float(np.sqrt(np.mean(mid**2) + 1e-10))
        rms_legs = float(np.sqrt(np.mean(legs**2) + 1e-10))
        bass_energy = float(np.sqrt(np.mean(vib**2) + 1e-10))

        with self.lock:
            self.stats["rms_legs"] = rms_legs
            self.stats["rms_mid"] = rms_mid
            self.stats["rms_upper_mid"] = rms_upper
            self.stats["rms_head"] = rms_head
            self.stats["bass_energy"] = bass_energy
            self.stats["generated_freq_hz"] = 0.0

        return np.clip(vib, -1.0, 1.0)

    @staticmethod
    def _haptics_to_zones(vib8: np.ndarray) -> np.ndarray:
        """Collapse 8ch haptics pairs to 4-zone (head, upper, legs, mid) for legacy layouts."""
        n = len(vib8)
        vib = np.zeros((n, 4), dtype=np.float32)
        if n <= 0:
            return vib
        # Average L/R per row; zone column order matches LiveStreamProcessor.
        vib[:, 0] = 0.5 * (vib8[:, 0] + vib8[:, 1])  # head
        vib[:, 1] = 0.5 * (vib8[:, 2] + vib8[:, 3])  # upper
        vib[:, 2] = 0.5 * (vib8[:, 6] + vib8[:, 7])  # legs
        vib[:, 3] = 0.5 * (vib8[:, 4] + vib8[:, 5])  # mid
        return vib

    def _render_vibration_only(
        self,
        capture: np.ndarray,
        *,
        already_processing_sr: bool = False,
    ) -> np.ndarray:
        """Vibration zones only for Gigaport ch3-6 (muvi-style, not haptics)."""
        if not already_processing_sr:
            capture = self._stereo_capture_block(capture)
        else:
            capture = self._prepare_capture(capture)
        result = self.processor.process_block(capture)
        frames = len(capture)
        head = fit_frames(result["head"], frames)
        upper = fit_frames(result["upper_mid"], frames)
        legs = fit_frames(result["legs"], frames)
        mid = fit_frames(result["mid"], frames)

        vib = np.zeros((frames, 4), dtype=np.float32)
        vib[:, 0] = head
        vib[:, 1] = upper
        vib[:, 2] = legs
        vib[:, 3] = mid

        if self.output_sample_rate != PROCESSING_SR:
            vib = resample_if_needed(vib, PROCESSING_SR, self.output_sample_rate)

        with self.lock:
            self.stats["rms_legs"] = result["rms_legs"]
            self.stats["rms_mid"] = result["rms_mid"]
            self.stats["rms_upper_mid"] = result["rms_upper_mid"]
            self.stats["rms_head"] = result["rms_head"]
            self.stats["bass_energy"] = result["bass_energy"]
            self.stats["generated_freq_hz"] = result.get("generated_freq_hz", 0.0)

        return np.clip(vib, -1.0, 1.0)

    def set_vibe_sync_ms(self, ms: float) -> None:
        """Shift vibration timing vs audio. Signal is unchanged; only delay changes.

        Positive = vibration later (delay vibration). Negative = vibration earlier
        (achieved by delaying the audio instead). Both streams are app-controlled.
        """
        with self.lock:
            self.vibe_sync_ms = float(np.clip(ms, -4000.0, 4000.0))
            # Re-arm the delay buffers so the new offset takes effect cleanly.
            self._reset_capture_ring()
            audio_ms, vibe_ms = self._delays_ms(self.vibe_sync_ms)
            self.stats["vibe_sync_ms"] = self.vibe_sync_ms
            self.stats["vibe_lag_ms"] = vibe_ms
            self.stats["audio_lag_ms"] = audio_ms

    def _delays_ms(self, sync_ms: float) -> tuple[float, float]:
        """Return (audio_delay_ms, vibe_delay_ms) around the shared baseline."""
        vibe_ms = float(np.clip(BASELINE_DELAY_MS + max(0.0, sync_ms), 0.0, MAX_DELAY_MS))
        audio_ms = float(np.clip(BASELINE_DELAY_MS + max(0.0, -sync_ms), 0.0, MAX_DELAY_MS))
        return audio_ms, vibe_ms

    def _delays_samples(self, sync_ms: float) -> tuple[int, int]:
        audio_ms, vibe_ms = self._delays_ms(sync_ms)
        sr = self.output_sample_rate
        return (
            int(round(audio_ms * sr / 1000.0)),
            int(round(vibe_ms * sr / 1000.0)),
        )

    def _capture_lag_ms(self, sync_ms: float) -> float:
        # Vibration-only (overlay) delay. Positive sync = later, negative = earlier.
        return float(np.clip(BASELINE_DELAY_MS + sync_ms, 0.0, MAX_DELAY_MS))

    def _capture_lag_samples(self, sync_ms: float) -> int:
        return int(round(self._capture_lag_ms(sync_ms) * PROCESSING_SR / 1000.0))

    def _reset_capture_ring(self) -> None:
        self._capture_ring = np.zeros((0, 2), dtype=np.float32)
        self._capture_read_idx = 0
        self._src_ring = np.zeros((0, 2), dtype=np.float32)
        self._vib_ring = np.zeros((0, 4), dtype=np.float32)
        self._ring_base = 0
        self._abs_processed = 0
        self._out_pos = 0

    def _stereo_capture_block(self, capture: np.ndarray) -> np.ndarray:
        if capture.ndim == 1:
            capture = capture.reshape(-1, 1)
        if capture.shape[1] == 1:
            capture = np.repeat(capture, 2, axis=1)
        stereo = ensure_stereo(capture).astype(np.float32, copy=False)
        if self.input_sample_rate != PROCESSING_SR:
            stereo = resample_if_needed(stereo, self.input_sample_rate, PROCESSING_SR)
        return stereo

    def _push_vibration_block(self, vib: np.ndarray) -> None:
        with self.lock:
            # No manual dropping: the delay line controls latency and the deque
            # maxlen is the only (rarely hit) overflow guard. Keeps vibration smooth.
            self._output_ring.append(vib)

    def _ingest_capture(self, capture: np.ndarray) -> None:
        """Route captured audio to the correct pipeline (audio thread, no queue hop)."""
        if self.vibration_overlay:
            self._ingest_vibration_only(capture)
        else:
            self._ingest_dual(capture)

    def _ingest_vibration_only(self, capture: np.ndarray) -> None:
        """APC/overlay mode: app outputs vibration only (audio played externally)."""
        stereo = self._stereo_capture_block(capture)
        mono = stereo.mean(axis=1)
        input_level = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2) + 1e-10))

        if len(stereo) == 0:
            return

        vol = self._effective_audio_gain()
        left_level = vol * float(np.sqrt(np.mean(stereo[:, 0].astype(np.float32) ** 2) + 1e-10))
        right_level = vol * float(np.sqrt(np.mean(stereo[:, 1].astype(np.float32) ** 2) + 1e-10))

        with self.lock:
            sync_ms = self.vibe_sync_ms
            self.stats["input_rms"] = input_level
            self.stats["rms_audio_left"] = left_level
            self.stats["rms_audio_right"] = right_level

        delay = self._capture_lag_samples(sync_ms)
        lag_ms = self._capture_lag_ms(sync_ms)
        chunk_len = len(stereo)

        self._capture_ring = (
            stereo if len(self._capture_ring) == 0 else np.concatenate([self._capture_ring, stereo], axis=0)
        )

        # Reclaim memory once the consumed head grows large.
        if self._capture_read_idx > PROCESSING_SR * 5:
            self._capture_ring = self._capture_ring[self._capture_read_idx :]
            self._capture_read_idx = 0

        # Proper delay line: emit the OLDEST unread samples while always keeping
        # `delay` samples buffered at the tail. Larger delay => vibration later.
        while (len(self._capture_ring) - self._capture_read_idx) - chunk_len >= delay:
            chunk = self._capture_ring[self._capture_read_idx : self._capture_read_idx + chunk_len]
            self._capture_read_idx += chunk_len
            if self.vibration_mode == "haptics":
                vib8 = self._render_haptics_only(chunk, already_processing_sr=True)
                vib = self._haptics_to_zones(vib8)
            else:
                vib = self._render_vibration_only(chunk, already_processing_sr=True)
            if self.vibration_overlay:
                frames = len(vib)
                block = np.zeros((frames, 6), dtype=np.float32)
                block[:, 2:6] = vib[:, :4]
                self._push_vibration_block(block)
            else:
                self._push_vibration_block(vib)

        with self.lock:
            self.stats["vibe_sync_ms"] = sync_ms
            self.stats["vibe_lag_ms"] = lag_ms

    @staticmethod
    def _read_ring(arr: np.ndarray, base: int, start_abs: int, length: int) -> np.ndarray:
        """Read `length` samples starting at absolute index `start_abs`, zero-padded."""
        width = arr.shape[1] if arr.ndim == 2 else 1
        out = np.zeros((length, width), dtype=np.float32)
        if len(arr) == 0:
            return out
        rel = start_abs - base
        src_lo = max(0, rel)
        src_hi = min(len(arr), rel + length)
        if src_hi <= src_lo:
            return out
        dst_lo = src_lo - rel
        out[dst_lo : dst_lo + (src_hi - src_lo)] = arr[src_lo:src_hi]
        return out

    def _process_pair(self, capture: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
        """Return source-aligned (audio_stereo, vibration_4ch) at the output rate."""
        cap = self._stereo_capture_block(capture)
        if len(cap) == 0:
            return None
        result = self.processor.process_block(cap)
        m = len(cap)
        vib_ps = np.zeros((m, 4), dtype=np.float32)
        vib_ps[:, 0] = fit_frames(result["head"], m)
        vib_ps[:, 1] = fit_frames(result["upper_mid"], m)
        vib_ps[:, 2] = fit_frames(result["legs"], m)
        vib_ps[:, 3] = fit_frames(result["mid"], m)

        if self.output_sample_rate != PROCESSING_SR:
            audio_os = resample_if_needed(cap, PROCESSING_SR, self.output_sample_rate)
            vib_os = resample_if_needed(vib_ps, PROCESSING_SR, self.output_sample_rate)
        else:
            audio_os = cap
            vib_os = vib_ps

        n = min(len(audio_os), len(vib_os))
        audio_os = audio_os[:n].astype(np.float32)
        vib_os = np.clip(vib_os[:n], -1.0, 1.0).astype(np.float32)

        with self.lock:
            self.stats["rms_legs"] = result["rms_legs"]
            self.stats["rms_mid"] = result["rms_mid"]
            self.stats["rms_upper_mid"] = result["rms_upper_mid"]
            self.stats["rms_head"] = result["rms_head"]
            self.stats["bass_energy"] = result["bass_energy"]
            self.stats["generated_freq_hz"] = result.get("generated_freq_hz", 0.0)
        return audio_os, vib_os

    def _ingest_dual(self, capture: np.ndarray) -> None:
        """Full mode: same proven delay line as overlay, plus clean audio on ch1-2.

        Audio and vibration stay locked together (same timing). No separate dual-ring
        indexing — that path caused one-shot blips on ASIO.
        """
        stereo = self._stereo_capture_block(capture)
        if len(stereo) == 0:
            return
        mono = stereo.mean(axis=1)
        input_level = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2) + 1e-10))
        # Audio L/R reflect what actually reaches the speakers: capture × volume.
        vol = self._effective_audio_gain()
        left_level = vol * float(np.sqrt(np.mean(stereo[:, 0].astype(np.float32) ** 2) + 1e-10))
        right_level = vol * float(np.sqrt(np.mean(stereo[:, 1].astype(np.float32) ** 2) + 1e-10))

        with self.lock:
            sync_ms = self.vibe_sync_ms
            self.stats["input_rms"] = input_level
            self.stats["rms_audio_left"] = left_level
            self.stats["rms_audio_right"] = right_level

        delay = self._capture_lag_samples(sync_ms)
        lag_ms = self._capture_lag_ms(sync_ms)
        chunk_len = len(stereo)

        self._capture_ring = (
            stereo if len(self._capture_ring) == 0 else np.concatenate([self._capture_ring, stereo], axis=0)
        )
        if self._capture_read_idx > PROCESSING_SR * 5:
            self._capture_ring = self._capture_ring[self._capture_read_idx :]
            self._capture_read_idx = 0

        while (len(self._capture_ring) - self._capture_read_idx) - chunk_len >= delay:
            chunk = self._capture_ring[self._capture_read_idx : self._capture_read_idx + chunk_len]
            self._capture_read_idx += chunk_len
            if self.vibration_mode == "haptics":
                vib8 = self._render_haptics_only(chunk, already_processing_sr=True)
            else:
                vib8 = None
                vib = self._render_vibration_only(chunk, already_processing_sr=True)
            audio = chunk.astype(np.float32, copy=False) * self._effective_audio_gain()
            if self.output_sample_rate != PROCESSING_SR:
                audio = resample_if_needed(audio, PROCESSING_SR, self.output_sample_rate)
            if self.output_layout in ("dual_native", "vibration_only"):
                vib_ch = int(
                    self.vibration_output_channels
                    or output_channels_for_layout(self.output_layout)
                )
                if vib8 is not None:
                    n = min(len(audio), len(vib8))
                    vib_out = np.zeros((n, vib_ch), dtype=np.float32)
                    copy_ch = min(vib_ch, vib8.shape[1])
                    vib_out[:, :copy_ch] = vib8[:n, :copy_ch]
                else:
                    n = min(len(audio), len(vib))
                    vib_out = build_vibration_only_block(
                        vib[:n], channels=vib_ch, mode=self.vibration_mode
                    )
                self._push_vibration_block(vib_out)
                if (
                    self.output_layout == "dual_native"
                    and self.audio_output_device_index is not None
                ):
                    audio_ch = int(self.audio_output_channel_count)
                    audio_out = np.zeros((n, audio_ch), dtype=np.float32)
                    # Audio Gigaport jack map (1-based): CH1&2 headphones, CH3&4 speakers.
                    # Must open >=4 (ideally 8) channels — a 4ch Windows Speakers
                    # stream remaps "back" onto physical CH5&6.
                    if self.speaker_route == "secondary" and audio_ch >= 4:
                        audio_out[:, 2:4] = np.clip(audio[:n], -1.0, 1.0)
                    elif audio_ch >= 2:
                        audio_out[:, 0:2] = np.clip(audio[:n], -1.0, 1.0)
                    with self.lock:
                        self._audio_output_ring.append(audio_out)
            else:
                if vib8 is not None:
                    vib = self._haptics_to_zones(vib8)
                n = min(len(audio), len(vib))
                block = build_output_block(
                    audio[:n],
                    vib[:n],
                    layout=self.output_layout,
                    speaker_route=self.speaker_route,
                )
                self._push_vibration_block(block)

        with self.lock:
            self.stats["vibe_sync_ms"] = sync_ms
            self.stats["vibe_lag_ms"] = lag_ms
            self.stats["audio_lag_ms"] = lag_ms

    def _open_output_stream(self, output_device_index: int) -> sd.OutputStream:
        extra = wasapi_output_extra(output_device_index)
        if self.vibration_overlay:
            self.output_channel_count = 6
            return sd.OutputStream(
                device=output_device_index,
                channels=6,
                samplerate=self.output_sample_rate,
                dtype="float32",
                blocksize=BLOCKSIZE,
                callback=self._output_callback,
                latency="low",
                extra_settings=extra,
            )
        if self.vibration_output_channels is not None:
            ch = self.vibration_output_channels
        else:
            ch = output_channels_for_layout(self.output_layout)
        self.output_channel_count = ch
        return sd.OutputStream(
            device=output_device_index,
            channels=ch,
            samplerate=self.output_sample_rate,
            dtype="float32",
            blocksize=BLOCKSIZE,
            callback=self._output_callback,
            latency="low",
            extra_settings=extra,
        )

    def _open_audio_output_stream(self, audio_output_device_index: int) -> sd.OutputStream:
        extra = wasapi_output_extra(audio_output_device_index)
        return sd.OutputStream(
            device=audio_output_device_index,
            channels=self.audio_output_channel_count,
            samplerate=self.output_sample_rate,
            dtype="float32",
            blocksize=BLOCKSIZE,
            callback=self._audio_output_callback,
            latency="low",
            extra_settings=extra,
        )

    def set_volume(self, volume: float) -> None:
        self.volume = float(np.clip(volume, 0.0, 1.2))

    def set_audio_muted(self, muted: bool) -> None:
        self.audio_muted = bool(muted)

    def set_highpass_hz(self, hz: float) -> None:
        self.highpass_hz = float(np.clip(hz, 0.0, 120.0))
        self.processor.set_highpass_hz(self.highpass_hz)

    def set_lowpass_hz(self, hz: float) -> None:
        """Bass cutoff — muvi vibe_lowpass_hz / --cutoff."""
        self.lowpass_hz = float(np.clip(hz, 40.0, 250.0))
        self.processor.set_lowpass_hz(self.lowpass_hz)

    def set_zone_intensities(
        self,
        mid: float | None = None,
        legs: float | None = None,
        upper: float | None = None,
        head: float | None = None,
    ) -> None:
        if mid is not None:
            self.intensity_mid = float(np.clip(mid, 0.0, 3.0))
        if legs is not None:
            self.intensity_legs = float(np.clip(legs, 0.0, 3.0))
        if upper is not None:
            self.intensity_upper = float(np.clip(upper, 0.0, 3.0))
        if head is not None:
            self.intensity_head = float(np.clip(head, 0.0, 3.0))
        self.processor.set_zone_gains(
            head=self.intensity_head,
            upper_mid=self.intensity_upper,
            legs=self.intensity_legs,
            mid=self.intensity_mid,
        )

    def update_presets(
        self,
        segmentation_id: str,
        frequency_profile_id: str,
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
    ) -> None:
        self.processor.update_presets(
            segmentation_id,
            frequency_profile_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
        )

    def get_runtime_stats(self) -> dict[str, float]:
        with self.lock:
            return dict(self.stats)

    def start(
        self,
        capture_device: dict[str, Any],
        output_device_index: int,
        audio_output_device_index: int | None = None,
        *,
        loopback: bool = False,  # noqa: ARG002 - kept for API compatibility
        capture_channels: int = 2,  # noqa: ARG002 - probed per device
        segmentation_id: str = "default",
        frequency_profile_id: str = "satori",
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
        vibration_overlay: bool = False,
    ) -> None:
        # Full mode: app plays audio (ch1-2) + vibration (ch3-6) on Gigaport.
        # Overlay/APC mode: app plays vibration only (ch3-6); audio comes from APC.
        self.set_vibration_overlay(vibration_overlay)
        if self.output_layout == "dual_native":
            if audio_output_device_index is None:
                raise RuntimeError("dual_native layout requires an audio output device")
            _validate_output_device(output_device_index, "dual_native")
            _validate_output_device(
                audio_output_device_index,
                required_channels=max(1, int(self.audio_output_channel_count)),
            )
        else:
            _validate_output_device(output_device_index, self.output_layout)
        if capture_device.get("backend") == "soundcard":
            self._start_soundcard(
                capture_device,
                output_device_index,
                audio_output_device_index,
                segmentation_id,
                frequency_profile_id,
                synthetic_vibro,
                synthetic_type_id,
            )
            return
        if capture_device.get("is_hands_free"):
            raise RuntimeError(
                "Hands-Free Bluetooth cannot capture music on Windows. "
                "Pick a [Speaker Loopback] entry instead."
            )
        self._start_sounddevice(
            int(capture_device["index"]),
            output_device_index,
            audio_output_device_index,
            segmentation_id,
            frequency_profile_id,
            synthetic_vibro,
            synthetic_type_id,
        )

    def _start_soundcard(
        self,
        capture_device: dict[str, Any],
        output_device_index: int,
        audio_output_device_index: int | None,
        segmentation_id: str,
        frequency_profile_id: str,
        synthetic_vibro: bool,
        synthetic_type_id: str,
    ) -> None:
        if not soundcard_available():
            raise RuntimeError(
                "Speaker loopback requires the soundcard package.\n"
                "On the tablet run: pip install soundcard"
            )

        self.stop()
        self.output_device_index = output_device_index
        self.audio_output_device_index = audio_output_device_index
        if self.output_layout == "dual_native" and audio_output_device_index is not None:
            vib_ch = self.vibration_output_channels or output_channels_for_layout(self.output_layout)
            audio_ch = self.audio_output_channel_count
            self.output_sample_rate = _pick_shared_output_sample_rate(
                output_device_index,
                audio_output_device_index,
                vib_ch,
                audio_ch,
            )
        else:
            vib_ch = self.vibration_output_channels or output_channels_for_layout(self.output_layout)
            self.output_sample_rate = _pick_output_sample_rate(output_device_index, vib_ch)
        capture_rate = self.output_sample_rate
        soundcard_id = str(capture_device["soundcard_id"])

        self.input_sample_rate = capture_rate
        self.processor = LiveStreamProcessor(
            sample_rate=PROCESSING_SR,
            segmentation_id=segmentation_id,
            frequency_profile_id=frequency_profile_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
        )
        self.processor.set_highpass_hz(self.highpass_hz)
        self.processor.set_lowpass_hz(self.lowpass_hz)
        self.processor.set_zone_gains(
            head=self.intensity_head,
            upper_mid=self.intensity_upper,
            legs=self.intensity_legs,
            mid=self.intensity_mid,
        )

        with self.lock:
            self._output_ring.clear()
            self._audio_output_ring.clear()
            self._current_block = None
            self._current_offset = 0
            self._audio_current_block = None
            self._audio_current_offset = 0
        self._reset_capture_ring()

        self.output_stream = self._open_output_stream(output_device_index)
        if self.output_layout == "dual_native" and audio_output_device_index is not None:
            self.audio_output_stream = self._open_audio_output_stream(audio_output_device_index)
            self.audio_output_stream.start()
        self.output_stream.start()

        self._loopback_stop.clear()

        def on_capture(data: np.ndarray) -> None:
            try:
                self._ingest_capture(data)
            except Exception:
                return

        self._loopback_thread = SoundcardLoopbackThread(
            soundcard_id=soundcard_id,
            sample_rate=capture_rate,
            on_block=on_capture,
            stop_event=self._loopback_stop,
        )
        self._loopback_thread.start()
        self.is_active = True

    def _start_sounddevice(
        self,
        capture_device_index: int,
        output_device_index: int,
        audio_output_device_index: int | None,
        segmentation_id: str,
        frequency_profile_id: str,
        synthetic_vibro: bool,
        synthetic_type_id: str,
    ) -> None:
        if self.output_layout == "dual_native":
            if audio_output_device_index is None:
                raise RuntimeError("dual_native layout requires an audio output device")
            _validate_output_device(output_device_index, "dual_native")
            _validate_output_device(
                audio_output_device_index,
                required_channels=max(1, int(self.audio_output_channel_count)),
            )
        else:
            _validate_output_device(output_device_index, self.output_layout)
        self.stop()
        self.capture_device_index = capture_device_index
        self.output_device_index = output_device_index
        self.audio_output_device_index = audio_output_device_index
        if self.output_layout == "dual_native" and audio_output_device_index is not None:
            vib_ch = self.vibration_output_channels or output_channels_for_layout(self.output_layout)
            audio_ch = self.audio_output_channel_count
            self.output_sample_rate = _pick_shared_output_sample_rate(
                output_device_index,
                audio_output_device_index,
                vib_ch,
                audio_ch,
            )
        else:
            vib_ch = self.vibration_output_channels or output_channels_for_layout(self.output_layout)
            self.output_sample_rate = _pick_output_sample_rate(output_device_index, vib_ch)
        self.processor = LiveStreamProcessor(
            sample_rate=PROCESSING_SR,
            segmentation_id=segmentation_id,
            frequency_profile_id=frequency_profile_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
        )
        self.processor.set_highpass_hz(self.highpass_hz)
        self.processor.set_lowpass_hz(self.lowpass_hz)
        self.processor.set_zone_gains(
            head=self.intensity_head,
            upper_mid=self.intensity_upper,
            legs=self.intensity_legs,
            mid=self.intensity_mid,
        )

        with self.lock:
            self._output_ring.clear()
            self._audio_output_ring.clear()
            self._current_block = None
            self._current_offset = 0
            self._audio_current_block = None
            self._audio_current_offset = 0
        self._reset_capture_ring()

        self._loopback_stop.clear()

        input_configs = _enumerate_input_configs(capture_device_index)
        last_error: Exception | None = None

        for cfg in input_configs:
            self.capture_channels = cfg.channels
            self.input_sample_rate = cfg.sample_rate
            input_blocksize = max(
                512, int(BLOCKSIZE * self.input_sample_rate / self.output_sample_rate)
            )
            try:
                self.output_stream = self._open_output_stream(output_device_index)
                if self.output_layout == "dual_native" and audio_output_device_index is not None:
                    # Non-fatal: keep vibration going even if the audio unit fails.
                    try:
                        self.audio_output_stream = self._open_audio_output_stream(audio_output_device_index)
                    except Exception as audio_exc:  # noqa: BLE001
                        self.audio_output_stream = None
                        print(
                            f"[live_input] live audio stream FAILED (vibration continues): {audio_exc}",
                            flush=True,
                        )
                self.input_stream = sd.InputStream(
                    device=capture_device_index,
                    channels=cfg.channels,
                    samplerate=cfg.sample_rate,
                    dtype="float32",
                    blocksize=input_blocksize,
                    callback=self._input_callback,
                    latency=cfg.latency,
                    extra_settings=cfg.extra_settings,
                )
                self.output_stream.start()
                if self.audio_output_stream is not None:
                    self.audio_output_stream.start()
                self.input_stream.start()
                self.is_active = True
                return
            except Exception as exc:
                last_error = exc
                self.stop()
                # If the output (e.g. ASIO) is what failed, don't keep retrying inputs.
                err_text = str(exc).lower()
                if "asio" in err_text or "outputstream" in err_text or "output stream" in err_text:
                    raise RuntimeError(
                        f"Could not open Gigaport output: {exc}"
                    ) from exc

        device_name = sd.query_devices(capture_device_index)["name"]
        hint = ""
        if _is_hands_free(str(device_name)):
            hint = (
                f"\n\nThe device '{device_name}' is a Hands-Free (phone-call) profile. "
                "Windows often cannot capture music from it.\n"
                "On your phone: disconnect Hands-Free, connect 'Stereo' / 'Media audio' instead.\n"
                "Or pick a [Loopback] speaker entry in the app."
            )
        cause = f" Last error: {last_error}" if last_error else ""
        raise RuntimeError(
            f"Could not open capture/output after {len(input_configs)} attempts.{hint}{cause}"
        ) from last_error

    def start_from_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        output_device_index: int,
        audio_output_device_index: int | None = None,
        *,
        segmentation_id: str = "default",
        frequency_profile_id: str = "satori",
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
        loop: bool = True,
    ) -> None:
        """Play stereo PCM through the same vibration → Gigaport path as Live capture."""
        pcm = np.asarray(pcm, dtype=np.float32)
        if pcm.ndim == 1:
            pcm = np.repeat(pcm.reshape(-1, 1), 2, axis=1)
        elif pcm.shape[1] == 1:
            pcm = np.repeat(pcm, 2, axis=1)
        elif pcm.shape[1] > 2:
            pcm = pcm[:, :2]
        if len(pcm) == 0:
            raise RuntimeError("Demo audio is empty")

        self.set_vibration_overlay(False)
        if self.output_layout == "dual_native":
            if audio_output_device_index is None:
                raise RuntimeError("dual_native layout requires an audio output device")
            _validate_output_device(output_device_index, "dual_native")
            _validate_output_device(
                audio_output_device_index,
                required_channels=max(1, int(self.audio_output_channel_count)),
            )
        else:
            _validate_output_device(output_device_index, self.output_layout)
        self.stop()

        self.output_device_index = output_device_index
        self.audio_output_device_index = audio_output_device_index
        if self.output_layout == "dual_native" and audio_output_device_index is not None:
            vib_ch = self.vibration_output_channels or output_channels_for_layout(self.output_layout)
            audio_ch = self.audio_output_channel_count
            self.output_sample_rate = _pick_shared_output_sample_rate(
                output_device_index,
                audio_output_device_index,
                vib_ch,
                audio_ch,
            )
        else:
            vib_ch = self.vibration_output_channels or output_channels_for_layout(self.output_layout)
            self.output_sample_rate = _pick_output_sample_rate(output_device_index, vib_ch)
        self.input_sample_rate = int(sample_rate)
        self.capture_channels = 2
        self.processor = LiveStreamProcessor(
            sample_rate=PROCESSING_SR,
            segmentation_id=segmentation_id,
            frequency_profile_id=frequency_profile_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
        )
        self.processor.set_highpass_hz(self.highpass_hz)
        self.processor.set_lowpass_hz(self.lowpass_hz)
        self.processor.set_zone_gains(
            head=self.intensity_head,
            upper_mid=self.intensity_upper,
            legs=self.intensity_legs,
            mid=self.intensity_mid,
        )

        with self.lock:
            self._output_ring.clear()
            self._audio_output_ring.clear()
            self._current_block = None
            self._current_offset = 0
            self._audio_current_block = None
            self._audio_current_offset = 0
        self._reset_capture_ring()

        with self._file_lock:
            self._file_pcm = pcm
            self._file_pos = 0
            self._file_loop = bool(loop)

        self._loopback_stop.clear()
        self.output_stream = self._open_output_stream(output_device_index)
        print(
            f"[live_input] demo vibration stream: dev=#{output_device_index} "
            f"ch={self.output_channel_count} sr={self.output_sample_rate} "
            f"layout={self.output_layout}",
            flush=True,
        )
        if self.output_layout == "dual_native" and audio_output_device_index is not None:
            # Non-fatal: if the audio Gigaport can't open, keep vibration going
            # so at least one device always works.
            try:
                self.audio_output_stream = self._open_audio_output_stream(audio_output_device_index)
                self.audio_output_stream.start()
                print(
                    f"[live_input] demo audio stream: dev=#{audio_output_device_index} "
                    f"ch={self.audio_output_channel_count} sr={self.output_sample_rate}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                self.audio_output_stream = None
                print(
                    f"[live_input] demo audio stream FAILED (vibration continues): {exc}",
                    flush=True,
                )
        self.output_stream.start()
        self.is_active = True
        self._file_thread = threading.Thread(
            target=self._file_feed_loop,
            name="demo-file-feed",
            daemon=True,
        )
        self._file_thread.start()

    def seek_file(self, seconds: float) -> None:
        with self._file_lock:
            pcm = self._file_pcm
            if pcm is None or self.input_sample_rate <= 0:
                return
            frame = int(max(0.0, float(seconds)) * self.input_sample_rate)
            self._file_pos = min(frame, len(pcm) - 1)
        self._reset_capture_ring()
        with self.lock:
            self._output_ring.clear()
            self._current_block = None
            self._current_offset = 0

    def get_file_progress(self) -> dict[str, float]:
        with self._file_lock:
            pcm = self._file_pcm
            pos = self._file_pos
            sr = float(self.input_sample_rate or 1)
            if pcm is None or len(pcm) == 0:
                return {"position": 0.0, "duration": 0.0, "fraction": 0.0}
            duration = float(len(pcm)) / sr
            position = float(pos) / sr
            fraction = position / duration if duration > 0 else 0.0
            return {
                "position": position,
                "duration": duration,
                "fraction": max(0.0, min(1.0, fraction)),
            }

    def _file_feed_loop(self) -> None:
        """Clocked stereo feed into the same ingest path used by Live capture.

        Paced against a monotonic deadline so blocks are produced at exactly
        real-time speed. The old fixed `sleep(block * 0.92)` fed ~9% faster
        than the output consumed; once the output ring (deque maxlen) filled,
        every append silently dropped the oldest block — heard as periodic
        cutting after ~15 s of demo playback.
        """
        # Feed a short burst up-front: ~150 ms is absorbed by the vibe-sync
        # delay line, the rest becomes a standing cushion in the output ring
        # so feed-thread jitter can't underrun it.
        prefill_blocks = 10
        next_deadline = time.monotonic()

        while not self._loopback_stop.is_set() and self.is_active:
            with self._file_lock:
                pcm = self._file_pcm
                pos = self._file_pos
                loop = self._file_loop
            if pcm is None or len(pcm) == 0:
                break

            end = pos + BLOCKSIZE
            if end <= len(pcm):
                chunk = pcm[pos:end]
                new_pos = end
            elif loop:
                first = pcm[pos:]
                need = BLOCKSIZE - len(first)
                chunk = np.concatenate([first, pcm[:need]], axis=0)
                new_pos = need
            else:
                if pos >= len(pcm):
                    break
                chunk = pcm[pos:]
                new_pos = len(pcm)

            with self._file_lock:
                self._file_pos = new_pos
                total = float(len(pcm))
                frac = float(new_pos) / total if total > 0 else 0.0

            try:
                self._ingest_capture(chunk)
            except Exception as exc:  # noqa: BLE001
                if self._ingest_error_count < 3:
                    self._ingest_error_count += 1
                    import traceback

                    print(f"[live_input] demo ingest error: {exc}", flush=True)
                    traceback.print_exc()

            with self.lock:
                self.stats["progress_fraction"] = frac
                backlog = len(self._output_ring)

            if prefill_blocks > 0:
                prefill_blocks -= 1
                next_deadline = time.monotonic()
            else:
                # Safety valve: if the ring is more than half full (output
                # consuming slower than expected), hold off until it drains
                # rather than letting the deque drop blocks.
                while backlog >= RING_MAX_BLOCKS // 2:
                    if self._loopback_stop.wait(timeout=0.02) or not self.is_active:
                        break
                    with self.lock:
                        backlog = len(self._output_ring)
                    next_deadline = time.monotonic()
                if self._loopback_stop.is_set() or not self.is_active:
                    break

                next_deadline += float(len(chunk)) / float(max(1, self.input_sample_rate))
                wait_s = next_deadline - time.monotonic()
                if wait_s > 0:
                    if self._loopback_stop.wait(timeout=wait_s):
                        break
                else:
                    # Fell behind (system stall) — resync instead of bursting.
                    next_deadline = time.monotonic()

            if not loop and new_pos >= len(pcm):
                break

        self.is_active = False

    def stop(self) -> None:
        self.is_active = False
        self._loopback_stop.set()
        if self._file_thread is not None:
            self._file_thread.join(timeout=2.0)
            self._file_thread = None
        if self._loopback_thread is not None:
            self._loopback_thread.join(timeout=2.0)
            self._loopback_thread = None
        for stream_attr in ("input_stream", "output_stream", "audio_output_stream"):
            stream = getattr(self, stream_attr)
            if stream is None:
                continue
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            setattr(self, stream_attr, None)

        with self._file_lock:
            self._file_pcm = None
            self._file_pos = 0

        self._reset_capture_ring()
        with self.lock:
            self._output_ring.clear()
            self._audio_output_ring.clear()
            self._current_block = None
            self._current_offset = 0
            self._audio_current_block = None
            self._audio_current_offset = 0
            self.stats = {
                "rms_legs": 0.0,
                "rms_mid": 0.0,
                "rms_upper_mid": 0.0,
                "rms_head": 0.0,
                "bass_energy": 0.0,
                "input_rms": 0.0,
                "rms_audio_left": 0.0,
                "rms_audio_right": 0.0,
                "generated_freq_hz": 0.0,
                "cpu_usage_estimate": 0.0,
                "progress_fraction": 0.0,
                "vibe_sync_ms": self.vibe_sync_ms,
                "vibe_lag_ms": self._delays_ms(self.vibe_sync_ms)[1],
                "audio_lag_ms": self._delays_ms(self.vibe_sync_ms)[0],
            }

    def _prepare_capture(self, capture: np.ndarray) -> np.ndarray:
        """Pass L/R loopback through unchanged — vibration is derived from this signal."""
        capture = np.asarray(capture, dtype=np.float32)
        if capture.ndim == 1:
            capture = capture.reshape(-1, 1)
        return capture

    def _take_next_output_block_locked(self) -> np.ndarray | None:
        """Pop the next output block in order. Caller must already hold self.lock."""
        if not self._output_ring:
            return None
        return self._output_ring.popleft()

    def _take_next_audio_output_block_locked(self) -> np.ndarray | None:
        """Pop the next audio-only output block. Caller must already hold self.lock."""
        if not self._audio_output_ring:
            return None
        return self._audio_output_ring.popleft()

    def _input_callback(self, indata, frames, _time_info, _status) -> None:
        try:
            self._ingest_capture(np.asarray(indata, dtype=np.float32))
        except Exception:
            return

    def _output_callback(self, outdata, frames, _time_info, _status) -> None:
        needed = frames
        offset = 0
        ch_count = self.output_channel_count
        output = np.zeros((frames, ch_count), dtype=np.float32)

        while needed > 0:
            with self.lock:
                if self._current_block is None:
                    self._current_block = self._take_next_output_block_locked()
                    self._current_offset = 0

                if self._current_block is None:
                    break

                block = self._current_block
                start = self._current_offset
                available = len(block) - start
                take = min(needed, available)
                chunk = block[start : start + take]

            if take <= 0:
                break

            # Blocks are 6-ch (or 4-ch overlay); stream may be 8-ch — pad with silence.
            src_ch = min(chunk.shape[1], ch_count)
            output[offset : offset + take, :src_ch] = chunk[:, :src_ch]
            offset += take
            needed -= take

            with self.lock:
                self._current_offset += take
                if self._current_block is not None and self._current_offset >= len(self._current_block):
                    self._current_block = None
                    self._current_offset = 0

        outdata[:] = output
        # Diagnostics: how much of this callback was real audio vs silence.
        prev = self._cb_frames
        self._cb_frames += frames
        self._cb_silent_frames += (frames - offset)
        if prev // 48000 != self._cb_frames // 48000:
            # Per-channel peak proves whether ch 1-2 (indices 0-1) carry vibration.
            peaks = [
                float(np.max(np.abs(output[:, c]))) if output.shape[1] > c else 0.0
                for c in range(min(ch_count, 8))
            ]
            # Quiet by default — set MUVE_AUDIO_DEBUG=1 to print callback stats.
            if os.environ.get("MUVE_AUDIO_DEBUG"):
                peak_txt = " ".join(f"{i + 1}:{p:.3f}" for i, p in enumerate(peaks))
                print(
                    f"[live_input] vib cb: served={self._cb_frames} "
                    f"silent={self._cb_silent_frames} ring={len(self._output_ring)} "
                    f"peaks[{peak_txt}]",
                    flush=True,
                )

    def _audio_output_callback(self, outdata, frames, _time_info, _status) -> None:
        needed = frames
        offset = 0
        ch_count = self.audio_output_channel_count
        output = np.zeros((frames, ch_count), dtype=np.float32)

        while needed > 0:
            with self.lock:
                if self._audio_current_block is None:
                    self._audio_current_block = self._take_next_audio_output_block_locked()
                    self._audio_current_offset = 0

                if self._audio_current_block is None:
                    break

                block = self._audio_current_block
                start = self._audio_current_offset
                available = len(block) - start
                take = min(needed, available)
                chunk = block[start : start + take]

            if take <= 0:
                break

            src_ch = min(chunk.shape[1], ch_count)
            output[offset : offset + take, :src_ch] = chunk[:, :src_ch]
            offset += take
            needed -= take

            with self.lock:
                self._audio_current_offset += take
                if self._audio_current_block is not None and self._audio_current_offset >= len(self._audio_current_block):
                    self._audio_current_block = None
                    self._audio_current_offset = 0

        outdata[:] = output
        prev = self._audio_cb_frames
        self._audio_cb_frames += frames
        self._audio_cb_silent_frames += (frames - offset)
        if prev // 48000 != self._audio_cb_frames // 48000:
            if os.environ.get("MUVE_AUDIO_DEBUG"):
                print(
                    f"[live_input] audio cb: served={self._audio_cb_frames} "
                    f"silent={self._audio_cb_silent_frames} ring={len(self._audio_output_ring)}",
                    flush=True,
                )
