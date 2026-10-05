"""Channel routing for single-Gigaport and dual-Gigaport layouts.

Dual layout (ASIO4ALL aggregates two Gigaport eX units):
  Gigaport 1 (ch 1-8) — vibration only, each zone duplicated to a stereo pair:
    1,2 → head    (was single-unit ch 3)
    3,4 → upper   (was ch 4)
    5,6 → mid back (was Rs / torso mid)
    7,8 → legs    (was Ls)

  Gigaport 2 (ch 9-16) — audio only:
    9,10  → L/R headphones   (unit CH1&2)
    11,12 → L/R speakers     (unit CH3&4)
    Switch between the two pairs at runtime (inactive pair is silent).
    Open the audio unit as 8ch when possible — a 4ch WASAPI Speakers
    stream remaps the second pair onto physical CH5&6.
"""

from __future__ import annotations

from typing import Literal

import numpy as np

from audio.vibration_presets import SATORI_OUTPUT_CHANNELS

DUAL_GIGAPORT_CHANNELS = 16
AUDIO_GIGAPORT_OFFSET = 8
MIN_DUAL_CHANNELS = DUAL_GIGAPORT_CHANNELS

OutputLayout = Literal["single", "dual_asio4all", "dual_native", "vibration_only"]
SpeakerRoute = Literal["headphones", "secondary"]
# "zones"   = one body zone per stereo pair (ch1-2 head, 3-4 upper, 5-6 mid, 7-8 legs).
# "stereo"  = left channel to all left shakers (odd ch), right to all right shakers (even ch).
# "haptics" = Quake HapticDSP → 8ch direct (bypasses build_vibration_only_block).
VibrationMode = Literal["zones", "stereo", "haptics"]

VIBRATION_ONLY_CHANNELS = 8


def is_asio4all_name(name: str) -> bool:
    return "asio4all" in name.lower()


def output_channels_for_layout(layout: OutputLayout) -> int:
    if layout == "dual_asio4all":
        return DUAL_GIGAPORT_CHANNELS
    if layout in ("dual_native", "vibration_only"):
        return VIBRATION_ONLY_CHANNELS
    return SATORI_OUTPUT_CHANNELS


def build_vibration_only_block(
    vib: np.ndarray,
    *,
    channels: int = VIBRATION_ONLY_CHANNELS,
    mode: VibrationMode = "zones",
) -> np.ndarray:
    """Map 4-zone vibration to the 8 Gigaport outputs.

    vib columns: head, upper, legs, mid (same order as LiveStreamProcessor).
    head/legs are Left-derived, upper/mid are Right-derived (equal gain).

    mode="zones" (one body zone per stereo pair):
      ch 1,2 → head    ch 3,4 → upper back
      ch 5,6 → mid back ch 7,8 → legs
      Each zone drives both shakers of its row.

    mode="stereo" (plain left/right):
      odd  outputs ch 1,3,5,7 → Left channel  (all left shakers)
      even outputs ch 2,4,6,8 → Right channel (all right shakers)
    """
    n = len(vib)
    ch = max(1, int(channels))
    out = np.zeros((n, ch), dtype=np.float32)
    if n <= 0:
        return out

    vib = np.clip(vib[:n], -1.0, 1.0).astype(np.float32, copy=False)
    head, upper, legs, mid = vib[:, 0], vib[:, 1], vib[:, 2], vib[:, 3]

    if mode == "stereo":
        # head is the Left tactile band, upper is the Right tactile band.
        left_band, right_band = head, upper
        for c in range(ch):
            out[:, c] = left_band if c % 2 == 0 else right_band
        return out

    zone_pairs = ((head, head), (upper, upper), (mid, mid), (legs, legs))
    for pair_idx, (left, right) in enumerate(zone_pairs):
        out_l = pair_idx * 2
        out_r = pair_idx * 2 + 1
        if out_l < ch:
            out[:, out_l] = left
        if out_r < ch:
            out[:, out_r] = right

    return out


def build_output_block(
    audio: np.ndarray,
    vib: np.ndarray,
    *,
    layout: OutputLayout = "single",
    speaker_route: SpeakerRoute = "headphones",
) -> np.ndarray:
    """Assemble a multichannel output block from stereo audio and 4-zone vibration.

    audio: (n, 2) left/right at output sample rate
    vib:   (n, 4) head, upper, legs, mid
    """
    n = min(len(audio), len(vib))
    if n <= 0:
        ch = output_channels_for_layout(layout)
        return np.zeros((0, ch), dtype=np.float32)

    audio = np.clip(audio[:n], -1.0, 1.0).astype(np.float32, copy=False)
    vib = np.clip(vib[:n], -1.0, 1.0).astype(np.float32, copy=False)

    if layout == "dual_asio4all":
        out = np.zeros((n, DUAL_GIGAPORT_CHANNELS), dtype=np.float32)
        head, upper, legs, mid = vib[:, 0], vib[:, 1], vib[:, 2], vib[:, 3]
        out[:, 0] = head
        out[:, 1] = head
        out[:, 2] = upper
        out[:, 3] = upper
        out[:, 4] = mid
        out[:, 5] = mid
        out[:, 6] = legs
        out[:, 7] = legs
        if speaker_route == "secondary":
            out[:, 10:12] = audio
        else:
            out[:, 8:10] = audio
        return out

    out = np.zeros((n, SATORI_OUTPUT_CHANNELS), dtype=np.float32)
    out[:, 0:2] = audio
    out[:, 2:6] = vib
    return out
