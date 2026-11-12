"""Aurasens programs: authored haptics played straight to the vibration Gigaport.

An Aurasens track is an AIFF file, 2 ch, 48 kHz, 24-bit. Every 24-bit sample holds:

    bits 23..8  headphone audio (int16)
    bits  7..0  one byte of a 16-bit data word: LEFT = high byte, RIGHT = low byte

The data word is a time-multiplexed stream of 32 slots (one slot per sample, 1500 Hz per slot):
bit 15 marks slot 1 (frame sync), bits 14..0 = 0x4000 + v * 16384 with v in [-1, 1).
Slots 1-30 are transducer channels, 31 = pump, 32 = valves (1-based, as used in the Programs
settings). See AURASENS_AUDIO_SPEC.md in the reverse-engineering project for the details.

Muvi has 8 transducers. PROGRAM_SLOT_MAP picks which Aurasens slot drives each Gigaport output
(output 1 = first entry). The program's own vibration replaces the live haptics DSP; the
calibration trims, on/off switches, zone switches and the Vibration slider still apply.

The file is never loaded whole: a 22-minute program is ~380 MB. It is memory-mapped and decoded
one block at a time, so playback, seeking and looping cost almost no RAM.
"""

from __future__ import annotations

import json
import mmap
import os
import struct
from typing import Any

import numpy as np

# Gigaport vibration output 1..8 <- Aurasens slot (1-based). Chosen by Gharieni (2026-11-12).
PROGRAM_SLOT_MAP: tuple[int, ...] = (1, 3, 14, 16, 23, 24, 27, 28)

AURASENS_SR = 48000
SLOTS = 32
_SILENCE = 0x4000


def _default_programs_dir() -> str:
    env = os.environ.get("MUVI_PROGRAMS_DIR")
    if env:
        return env
    if os.name == "nt":
        return r"C:\kiosk_launcher\programs"
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "programs")


PROGRAMS_DIR = _default_programs_dir()
MANIFEST_NAME = "programs.json"


def _ext80(b: bytes) -> float:
    exp = ((b[0] & 0x7F) << 8 | b[1]) - 16383
    mant = int.from_bytes(b[2:10], "big")
    return mant * 2.0 ** (exp - 63)


def read_aiff_header(path: str) -> dict[str, int]:
    """Return channels, bits, rate, frames and the byte offset of the sample data."""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"FORM" or head[8:12] not in (b"AIFF", b"AIFC"):
            raise ValueError(f"not an AIFF file: {path}")
        info: dict[str, int] = {}
        while True:
            ck = f.read(8)
            if len(ck) < 8:
                break
            cid, size = ck[:4], struct.unpack(">I", ck[4:])[0]
            pos = f.tell()
            if cid == b"COMM":
                b = f.read(18)
                info["channels"] = struct.unpack(">h", b[0:2])[0]
                info["frames"] = struct.unpack(">I", b[2:6])[0]
                info["bits"] = struct.unpack(">h", b[6:8])[0]
                info["rate"] = int(round(_ext80(b[8:18])))
            elif cid == b"SSND":
                offset = struct.unpack(">I", f.read(4))[0]
                info["data_offset"] = pos + 8 + offset
            f.seek(pos + size + (size & 1))
    if "data_offset" not in info or "frames" not in info:
        raise ValueError(f"AIFF without COMM/SSND: {path}")
    return info


def is_aurasens_track(path: str) -> bool:
    try:
        h = read_aiff_header(path)
    except (OSError, ValueError):
        return False
    return h.get("channels") == 2 and h.get("bits") == 24


class AurasensTrack:
    """Array-like source for LiveAudioEngine.start_from_pcm.

    len(track) = frames; track[a:b] -> float32 (n, 10): [audio L, audio R, vib out 1..8],
    all at 48 kHz. Vibration is linearly interpolated between the 1500 Hz slot samples, so a
    block decodes the same wherever it starts (seek, loop) and has no steps at block edges.
    """

    authored_channels = 8

    def __init__(self, path: str, slot_map: tuple[int, ...] | list[int] = PROGRAM_SLOT_MAP) -> None:
        info = read_aiff_header(path)
        if info.get("channels") != 2 or info.get("bits") != 24:
            raise ValueError(
                f"{os.path.basename(path)}: expected 2-channel 24-bit AIFF, "
                f"got {info.get('channels')} ch {info.get('bits')}-bit"
            )
        slots = [int(s) for s in slot_map]
        if len(slots) != self.authored_channels or not all(1 <= s <= SLOTS for s in slots):
            raise ValueError(f"slot map needs 8 slots between 1 and {SLOTS}: {slot_map!r}")
        self.path = path
        self.sample_rate = int(info.get("rate") or AURASENS_SR)
        self.slot_map = tuple(slots)
        self._slot0 = np.array([s - 1 for s in slots], dtype=np.int64)
        self._file = open(path, "rb")
        self._mm = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        avail = (len(self._mm) - info["data_offset"]) // 6
        self.frames = int(min(info["frames"], avail))
        self._raw = np.frombuffer(self._mm, dtype=np.uint8, count=self.frames * 6,
                                  offset=info["data_offset"]).reshape(self.frames, 6)
        # Frame index of every sync (bit 15 = high bit of LEFT's low byte). ~2M entries for 22 min.
        self._syncs = np.flatnonzero(self._raw[:, 2] & 0x80).astype(np.int64)
        if len(self._syncs) == 0:
            raise ValueError(f"{os.path.basename(path)}: no Aurasens sync found (not an Aurasens track?)")

    def close(self) -> None:
        raw, self._raw = getattr(self, "_raw", None), None
        del raw
        try:
            self._mm.close()
        except (AttributeError, BufferError, ValueError):
            pass
        try:
            self._file.close()
        except AttributeError:
            pass

    def __len__(self) -> int:
        return self.frames

    @property
    def duration(self) -> float:
        return self.frames / float(self.sample_rate)

    def _slots_of(self, idx: np.ndarray) -> np.ndarray:
        """Slot number (0-based) of each frame index, re-synced at every sync mark."""
        k = np.searchsorted(self._syncs, idx, side="right") - 1
        base = np.where(k >= 0, self._syncs[np.maximum(k, 0)], self._syncs[0] - SLOTS)
        return (idx - base) % SLOTS

    def __getitem__(self, key: slice) -> np.ndarray:
        if not isinstance(key, slice):
            raise TypeError("AurasensTrack supports slices only")
        a, b, step = key.indices(self.frames)
        if step != 1:
            raise ValueError("AurasensTrack supports step 1 only")
        n = max(0, b - a)
        out = np.zeros((n, 2 + self.authored_channels), dtype=np.float32)
        if n == 0:
            return out
        raw = self._raw
        blk = raw[a:b]
        # Audio: top 16 bits of each 24-bit big-endian sample.
        out[:, 0] = (blk[:, 0].astype(np.int16) << 8 | blk[:, 1]).astype(np.int16) / 32768.0
        out[:, 1] = (blk[:, 3].astype(np.int16) << 8 | blk[:, 4]).astype(np.int16) / 32768.0

        # Vibration: read a window one frame of slots wider on both sides, then interpolate.
        lo, hi = max(0, a - 2 * SLOTS), min(self.frames, b + 2 * SLOTS)
        win = raw[lo:hi]
        idx = np.arange(lo, hi, dtype=np.int64)
        slot = self._slots_of(idx)
        word = (win[:, 2].astype(np.int32) << 8 | win[:, 5]) & 0x7FFF
        val = (word - _SILENCE).astype(np.float32) / 16384.0
        t = np.arange(a, b, dtype=np.float64)
        for ch, s in enumerate(self._slot0):
            m = slot == s
            if not np.any(m):
                continue
            out[:, 2 + ch] = np.interp(t, idx[m].astype(np.float64), val[m])
        return out


# --------------------------------------------------------------------------- catalogue --

def load_catalogue(programs_dir: str | None = None) -> dict[str, Any]:
    """Read programs.json from the programs folder on the tablet.

    {
      "slot_map": [1, 3, 14, 16, 23, 24, 27, 28],          optional, overrides PROGRAM_SLOT_MAP
      "categories": [
        {"title": "WELLNESS - Elements", "programs": [
          {"id": "air", "title": "Air", "artist": "Debrie - Zeller",
           "file": "Air.aif", "artwork": "Air.jpg", "duration": 600.0}
        ]}
      ]
    }
    Missing files are left out, so a half-copied folder never shows a program that can't play.
    """
    root = programs_dir or PROGRAMS_DIR
    path = os.path.join(root, MANIFEST_NAME)
    result: dict[str, Any] = {"dir": root, "found": False, "categories": [], "slot_map": list(PROGRAM_SLOT_MAP)}
    if not os.path.isfile(path):
        return result
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        result["error"] = f"{MANIFEST_NAME}: {exc}"
        return result
    result["found"] = True
    sm = data.get("slot_map")
    if isinstance(sm, list) and len(sm) == 8 and all(isinstance(x, int) and 1 <= x <= SLOTS for x in sm):
        result["slot_map"] = sm
    seen: set[str] = set()
    for cat in data.get("categories") or []:
        items = []
        for p in cat.get("programs") or []:
            pid = str(p.get("id") or "").strip()
            fname = str(p.get("file") or "")
            fpath = os.path.join(root, fname)
            if not pid or pid in seen or not fname or not os.path.isfile(fpath):
                continue
            seen.add(pid)
            art = str(p.get("artwork") or "")
            items.append({
                "id": pid,
                "title": str(p.get("title") or pid),
                "artist": str(p.get("artist") or ""),
                "duration": float(p.get("duration") or 0.0),
                "path": fpath,
                "artwork_path": os.path.join(root, art) if art and os.path.isfile(os.path.join(root, art)) else "",
            })
        if items:
            result["categories"].append({"title": str(cat.get("title") or ""), "programs": items})
    return result
