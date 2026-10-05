"""Runs live_audio.py in a clean process (WinRT in UI process can break audio I/O).

stdout = JSON only (one object per line). All logs go to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

# Keep a private handle for JSON replies; divert all other prints to stderr.
_JSON_OUT = sys.stdout
sys.stdout = sys.stderr

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from demo_audio import (  # noqa: E402
    demo_track_info,
    get_demo_progress,
    seek_demo,
    start_demo_audio,
)
from live_audio import (  # noqa: E402
    get_audio_settings,
    get_runtime_stats,
    is_active,
    prepare_zone_intensities,
    set_cutoff_hz,
    set_speaker_route,
    set_vibration_mode,
    set_audio_muted,
    set_vibration,
    set_vibration_muted,
    set_volume,
    set_zone_enabled,
    start_live_audio,
    stop_live_audio,
)


def _reply(obj: dict[str, Any]) -> None:
    _JSON_OUT.write(json.dumps(obj) + "\n")
    _JSON_OUT.flush()


def main() -> int:
    _reply({"ok": True, "event": "ready"})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _reply({"ok": False, "error": f"bad json: {exc}"})
            continue

        cmd = str(msg.get("cmd", ""))
        try:
            if cmd == "start":
                v = float(msg.get("vibration", 0.27))
                zi = prepare_zone_intensities(
                    v,
                    msg.get("zone_enabled"),
                    vibration_muted=bool(msg.get("vibration_muted", False)),
                )
                result = start_live_audio(
                    volume=float(msg.get("volume", 0.70)),
                    mid=zi["mid"],
                    legs=zi["legs"],
                    upper=zi["upper"],
                    head=zi["head"],
                    cutoff_hz=float(msg.get("cutoff_hz", msg.get("highpass_hz", 200.0))),
                    vibration_overlay=False,
                    prefer_bluetooth=bool(msg.get("prefer_bluetooth", False)),
                    speaker_route=str(msg.get("speaker_route", "headphones")),
                    vibration_mode=str(msg.get("vibration_mode", "haptics")),
                    vibration_output_index=msg.get("vibration_output_index"),
                    audio_output_index=msg.get("audio_output_index"),
                    roles_flipped=bool(msg.get("roles_flipped", False)),
                )
                set_audio_muted(bool(msg.get("audio_muted", False)))
                print(
                    f"[engine_worker] START LIVE ok — capture={result.get('capture')!r} "
                    f"output={result.get('output_name')!r}"
                )
                _reply({**result, "event": "started"})

            elif cmd == "start_demo":
                # Stop any Live capture first — shared LiveAudioEngine / ASIO.
                stop_live_audio()
                v = float(msg.get("vibration", 0.27))
                zi = prepare_zone_intensities(
                    v,
                    msg.get("zone_enabled"),
                    vibration_muted=bool(msg.get("vibration_muted", False)),
                )
                result = start_demo_audio(
                    volume=float(msg.get("volume", 0.70)),
                    mid=zi["mid"],
                    legs=zi["legs"],
                    upper=zi["upper"],
                    head=zi["head"],
                    cutoff_hz=float(msg.get("cutoff_hz", msg.get("highpass_hz", 200.0))),
                    path=msg.get("path"),
                    vibration_mode=str(msg.get("vibration_mode", "haptics")),
                    vibration_output_index=msg.get("vibration_output_index"),
                    audio_output_index=msg.get("audio_output_index"),
                    roles_flipped=bool(msg.get("roles_flipped", False)),
                    loop=bool(msg.get("loop", True)),
                )
                set_audio_muted(bool(msg.get("audio_muted", False)))
                print(
                    f"[engine_worker] START DEMO ok — file={result.get('capture')!r} "
                    f"output={result.get('output_name')!r}"
                )
                _reply({**result, "event": "started"})

            elif cmd == "demo_info":
                _reply({"ok": True, "track": demo_track_info(msg.get("path"))})

            elif cmd == "seek_demo":
                progress = seek_demo(float(msg.get("seconds", 0.0)))
                _reply({"ok": True, **progress})

            elif cmd == "demo_progress":
                _reply({"ok": True, **get_demo_progress()})

            elif cmd == "stop":
                stop_live_audio()
                print("[engine_worker] STOP LIVE")
                _reply({"ok": True, "event": "stopped"})

            elif cmd == "set_volume":
                set_volume(float(msg.get("value", 0.70)))
                _reply({"ok": True})

            elif cmd == "set_audio_muted":
                set_audio_muted(bool(msg.get("muted", False)))
                _reply({"ok": True})

            elif cmd == "set_vibration":
                set_vibration(float(msg.get("value", 0.27)))
                _reply({"ok": True})

            elif cmd == "set_vibration_muted":
                set_vibration_muted(bool(msg.get("muted", False)))
                _reply({"ok": True})

            elif cmd == "set_zone_enabled":
                set_zone_enabled(
                    str(msg.get("zone", "")),
                    bool(msg.get("enabled", True)),
                )
                _reply({"ok": True})

            elif cmd == "set_cutoff_hz" or cmd == "set_highpass_hz":
                set_cutoff_hz(float(msg.get("value", 200.0)))
                _reply({"ok": True})

            elif cmd == "set_speaker_route":
                route = str(msg.get("route", "headphones"))
                if route not in ("headphones", "secondary"):
                    _reply({"ok": False, "error": f"bad speaker route: {route}"})
                else:
                    set_speaker_route(route)  # type: ignore[arg-type]
                    _reply({"ok": True, "speaker_route": route})

            elif cmd == "set_vibration_mode":
                vmode = str(msg.get("mode", "haptics"))
                if vmode not in ("zones", "stereo", "haptics"):
                    _reply({"ok": False, "error": f"bad vibration mode: {vmode}"})
                else:
                    set_vibration_mode(vmode)  # type: ignore[arg-type]
                    _reply({"ok": True, "vibration_mode": vmode})

            elif cmd == "audio_settings":
                settings = get_audio_settings(
                    active=is_active(),
                    output_layout=msg.get("output_layout"),
                    vibration_index=msg.get("vibration_output_index"),
                    audio_index=msg.get("audio_output_index"),
                    output_name=msg.get("output_name"),
                    engine_error=msg.get("engine_error"),
                    roles_flipped=bool(msg.get("roles_flipped", False)),
                )
                _reply({"ok": True, **settings})

            elif cmd == "stats":
                _reply({"ok": True, "stats": get_runtime_stats(), "running": is_active()})

            elif cmd == "quit":
                stop_live_audio()
                _reply({"ok": True, "event": "bye"})
                return 0

            else:
                _reply({"ok": False, "error": f"unknown cmd: {cmd}"})

        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            stop_live_audio()
            _reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
