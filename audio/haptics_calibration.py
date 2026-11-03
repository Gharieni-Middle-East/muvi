"""Per-channel haptics calibration (same model as the Raspberry Pi muvi-haptics app).

    master_gain_db    overall haptic level, applied inside HapticDSP before its limiter
    channel_gain_db   8 per-transducer trims (dB)
    channel_enabled   8 on/off switches
    channel_labels    8 [body part, side] pairs, as confirmed during calibration

Stored as JSON outside the app folder, because online updates replace the whole app folder:
  frozen build (tablet)  C:\\kiosk_launcher\\haptics_calibration.json  (parent of the app folder)
  source run             <project>/haptics_calibration.json
"""

from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any

N_CHANNELS = 8
GAIN_MIN_DB, GAIN_MAX_DB = -30.0, 12.0
MASTER_MIN_DB, MASTER_MAX_DB = -20.0, 20.0

BODY_PARTS = ["Head", "Neck", "Shoulders", "Upper back", "Mid back", "Lower back", "Seat",
              "Legs", "Thighs", "Calves", "Feet", "Arms"]
SIDES = ["Left", "Right", "Center"]

# Matches the Gigaport pairs used everywhere else in the app (ch1/2 head ... ch7/8 legs).
DEFAULT_LABELS = [["Head", "Left"], ["Head", "Right"], ["Upper back", "Left"], ["Upper back", "Right"],
                  ["Mid back", "Left"], ["Mid back", "Right"], ["Legs", "Left"], ["Legs", "Right"]]

DEFAULTS: dict[str, Any] = {
    "master_gain_db": 0.0,
    "channel_gain_db": [0.0] * N_CHANNELS,
    "channel_enabled": [True] * N_CHANNELS,
    "channel_labels": DEFAULT_LABELS,
}

FILENAME = "haptics_calibration.json"


def calibration_path() -> str:
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(os.path.abspath(sys.executable))
        return os.path.join(os.path.dirname(app_dir), FILENAME)
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project, FILENAME)


def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def normalize(cal: dict[str, Any] | None) -> dict[str, Any]:
    """Return a complete, validated calibration dict (missing/bad fields fall back to defaults)."""
    out = copy.deepcopy(DEFAULTS)
    if not isinstance(cal, dict):
        return out
    out["master_gain_db"] = _clamp(cal.get("master_gain_db"), MASTER_MIN_DB, MASTER_MAX_DB, 0.0)
    gains = cal.get("channel_gain_db")
    if isinstance(gains, list):
        for i, g in enumerate(gains[:N_CHANNELS]):
            out["channel_gain_db"][i] = _clamp(g, GAIN_MIN_DB, GAIN_MAX_DB, 0.0)
    enabled = cal.get("channel_enabled")
    if isinstance(enabled, list):
        for i, e in enumerate(enabled[:N_CHANNELS]):
            out["channel_enabled"][i] = bool(e)
    labels = cal.get("channel_labels")
    if isinstance(labels, list):
        for i, lab in enumerate(labels[:N_CHANNELS]):
            if isinstance(lab, (list, tuple)) and len(lab) == 2:
                part, side = str(lab[0]), str(lab[1])
                if part in BODY_PARTS and side in SIDES:
                    out["channel_labels"][i] = [part, side]
    return out


def load() -> dict[str, Any]:
    try:
        with open(calibration_path(), encoding="utf-8") as f:
            return normalize(json.load(f))
    except (OSError, ValueError):
        return normalize(None)


def save(cal: dict[str, Any]) -> str:
    """Write atomically; returns the path written."""
    path = calibration_path()
    data = normalize(cal)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path
