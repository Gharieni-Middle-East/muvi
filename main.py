"""muve — standalone Windows app shell (pywebview).

Play button = Start Live / Stop Live (same as the old PyQt app).
Bluetooth Connect opens AudioPlaybackConnection (replaces APC).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

# "main.exe --watchdog --pid N": tiny process that restarts the app if it exits.
# Handled first so it stays small (no webview, no WinRT).
if "--watchdog" in sys.argv:
    import AppWatchdog

    raise SystemExit(AppWatchdog.run_watchdog(sys.argv))

# Frozen exe doubles as the audio worker (sys.executable is main.exe, not python).
# Must run before webview/WinRT imports — WinRT in-process breaks ASIO.
if "--engine-worker" in sys.argv:
    import engine_worker

    raise SystemExit(engine_worker.main())

import webview

import AppWatchdog
import UpdateManager
import device_name
from audio import haptics_calibration
from bt_audio import BluetoothAudioService
from battery_manager import BatteryManager
from engine_bridge import EngineBridge
from hardware_monitor import probe_hardware_status
from live_audio import probe_live_input
from media_session import MediaSessionService
from wifi_service import WifiService

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

# Wi-Fi and Bluetooth share one radio chip on the tablet, and Wi-Fi traffic makes the Bluetooth
# music choppy. So Wi-Fi is kept off while a phone is pairing, connecting or connected, and comes
# back WIFI_RESTORE_DELAY_S after the phone has gone (only if the app was the one that turned it off).
WIFI_RESTORE_DELAY_S = 15.0
WIFI_RETRY_S = 10.0  # don't hammer Windows if it refuses a radio change
WIFI_FLAG_NAME = "wifi_off_for_bluetooth.flag"

# Bluetooth link handling.
BT_STATUS_ERRORS_LOST = 3     # failed status checks in a row before a phone counts as gone
BT_WAIT_ACCEPT_S = 45.0       # phone hasn't accepted the audio link yet: keep asking this long
BT_RECONNECT_S = 120.0        # link dropped: try to get the same phone back this long
BT_REOPEN_EVERY_S = 10.0      # how often to re-open the link while waiting / reconnecting
BT_IDLE_ENGINE_STOP_S = 300.0  # Bluetooth Live keeps the Gigaports open this long without a phone
# Automatic (re)start of Live: first retry after 2 s, doubling up to 30 s.
LIVE_RETRY_MIN_S = 2.0
LIVE_RETRY_MAX_S = 30.0

# Windows can't send play/pause/skip back to a phone that streams to the tablet (its Bluetooth
# audio receiver has no remote-control channel), so with a phone connected the Now Playing
# Play/Pause button pauses on the tablet: headphones and vibration go silent, the phone keeps
# playing. Back/Next are hidden and the user controls the music on the phone.

# User settings that survive a restart (volume, vibration, ...). Calibration has its own file.
SETTINGS_NAME = "muvi_settings.json"
SETTINGS_SAVE_EVERY_S = 2.0
_SETTINGS_KEYS = (
    "volume", "vibration", "cutoff_hz", "vibration_mode", "speaker_route",
    "zone_enabled", "gigaport_roles_flipped",
)


def _launcher_file(name: str) -> str:
    """A file outside the app folder (replaced on every update), next to haptics_calibration.json:
    C:\\kiosk_launcher\\<name> on the tablet, the source folder when running from source."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))), name)
    return os.path.join(BASE_DIR, name)


def _wifi_flag_path() -> str:
    """Marker that the app switched Wi-Fi off, so a crash or restart can't leave the tablet offline."""
    return _launcher_file(WIFI_FLAG_NAME)


def _load_settings() -> dict[str, Any]:
    """Saved user settings, validated. Anything missing or broken falls back to the defaults."""
    try:
        with open(_launcher_file(SETTINGS_NAME), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}

    def _num(key: str, lo: float, hi: float) -> None:
        try:
            out[key] = max(lo, min(hi, float(raw[key])))
        except (KeyError, TypeError, ValueError):
            pass

    _num("volume", 0.0, 1.0)
    _num("vibration", 0.0, 1.0)
    _num("cutoff_hz", 40.0, 250.0)
    if raw.get("vibration_mode") in ("haptics", "zones", "stereo"):
        out["vibration_mode"] = raw["vibration_mode"]
    if raw.get("speaker_route") in ("headphones", "secondary"):
        out["speaker_route"] = raw["speaker_route"]
    zones = raw.get("zone_enabled")
    if isinstance(zones, dict):
        out["zone_enabled"] = {
            z: bool(zones.get(z, True)) for z in ("head", "upper", "mid", "legs")
        }
    if isinstance(raw.get("gigaport_roles_flipped"), bool):
        out["gigaport_roles_flipped"] = raw["gigaport_roles_flipped"]
    return out


# Set True to open the app fullscreen on the Windows tablet.
FULLSCREEN = True


class Api:
    """JS <-> Python bridge. Keep every return value JSON-serializable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {
            "playing": False,
            "volume": 0.70,
            "vibration": 0.27,
            "cutoff_hz": 200.0,  # ~76% bass cutoff (40–250 Hz)
            "highpass_hz": 200.0,
            "position": 0.0,
            "input_rms": 0.0,
            "zone_rms": {
                "head": 0.0, "upper": 0.0, "mid": 0.0, "legs": 0.0,
                "left": 0.0, "right": 0.0,
            },
            "zone_enabled": {
                "head": True, "upper": True, "mid": True, "legs": True,
            },
            "audio_muted": False,
            "vibration_muted": False,
            "track": {
                "title": "Live Bluetooth",
                "artist": "Phone audio",
                "album": "",
                "duration": 0.0,
                "artwork": "assets/artwork.svg",
            },
            "bluetooth": {
                "connected_id": None,
                "connecting_id": None,
                "scanning": False,
                "discovering": False,
                "pairing_id": None,
                # 'confirm' while the phone shows the pairing prompt, 'audio' while the new
                # phone's audio connection is looked up (then it connects by itself).
                "pair_stage": None,
                # Phone whose audio link isn't open: kind 'accept' = phone hasn't accepted it
                # yet, 'reconnect' = it dropped and the app is getting it back.
                "waiting_id": None,
                "waiting_kind": None,
                "forgetting": False,
                "error": None,
                "alert": None,
                "supported": False,
            },
            "engine_ok": False,
            "engine_error": None,
            "capture_name": None,
            "output_name": None,
            "live_mode": None,  # 'aux' | 'cable' | 'overlay' | 'demo' while Live is on
            "live_input": {
                "ready": False,
                "mode": None,
                "capture_name": None,
                "message": "Connect AUX or Bluetooth to start.",
            },
            "speaker_route": "headphones",  # 'headphones' | 'secondary' (dual Gigaport)
            "vibration_mode": "haptics",  # 'haptics' | 'zones' | 'stereo'
            "output_layout": None,  # 'single' | 'dual_native'
            "audio_output_channels": 0,  # channels on the audio (sound) device
            "vibration_output_index": None,
            "audio_output_index": None,
            "gigaport_roles_flipped": False,
            "play_busy": False,
            "media": {
                "ok": False,
                "can_seek": False,
                "can_prev": False,
                "can_next": False,
            },
            "demo": {
                "available": os.path.isfile(os.path.join(BASE_DIR, "assets", "demo.wav")),
                "error": None,
            },
            # Pi-style per-channel haptics calibration (audio/haptics_calibration.py).
            # Edits apply live; only Save writes them to disk.
            "haptic_cal": haptics_calibration.load(),
            "haptic_cal_dirty": False,
            "haptic_cal_error": None,
            "live_paused": False,  # Bluetooth Live paused on the tablet (audio + vibration silent)
            "calibrating": None,  # Gigaport channel 1-8 playing the calibration tone, or None
            "calibration_busy": False,
        }
        # Saved user settings (volume, vibration, modes, roles) from the last session.
        saved = _load_settings()
        self.state.update(saved)
        self.state["highpass_hz"] = self.state["cutoff_hz"]
        self.state["audio_muted"] = self.state["volume"] <= 0.0
        self.state["vibration_muted"] = self.state["vibration"] <= 0.0
        if saved:
            print(f"[app] restored settings: {sorted(saved)}")
        self._settings_dirty = False
        self._settings_saved_at = 0.0
        self._bt = BluetoothAudioService()
        self._media = MediaSessionService()
        # Real paired devices only — no mock phones in the list.
        self._devices: list[dict[str, Any]] = []
        self._nearby: list[dict[str, Any]] = []
        self.state["bluetooth"]["supported"] = self._bt.available
        if not self._bt.available:
            self.state["bluetooth"]["error"] = self._bt.last_error
        self._last_tick = time.monotonic()
        self._default_track = dict(self.state["track"])
        self._demo_track = {
            "title": "Demo",
            "artist": "MUVI test track",
            "album": "",
            "duration": 0.0,
            "artwork": "assets/artwork.svg",
        }
        self._live_started_at: float | None = None
        self._cal_started_engine = False  # calibration opened the outputs itself (silent)
        # Wi-Fi off while a phone uses Bluetooth (see WIFI_RESTORE_DELAY_S).
        self._wifi_off_for_bt = os.path.exists(_wifi_flag_path())  # survives a crash/restart
        self._wifi_restore_at: float | None = None
        self._wifi_policy_try_at = 0.0
        self._cal_keepalive_at = 0.0
        # Automatic Live start (Bluetooth session / recovery after a crash or unplug).
        self._live_retry_at = 0.0
        self._live_retry_delay = LIVE_RETRY_MIN_S
        self._live_recover = False   # AUX Live stopped by a fault: start it again
        self._autostart_busy = False
        self._last_stats: dict[str, float] = {}
        # Bluetooth link bookkeeping (see _refresh_bluetooth_link).
        self._bt_status_errors = 0
        self._bt_wait_deadline = 0.0
        self._bt_wait_check_at = 0.0
        self._bt_reopen_at = 0.0
        self._bt_reopen_busy = False
        self._bt_idle_since: float | None = None

        self._last_paired_refresh = 0.0
        self._paired_refresh_busy = False
        self._battery = BatteryManager()
        # Online updates (Settings -> Software update) and Wi-Fi (Wi-Fi screen).
        self._window: Any = None
        self._updater = UpdateManager.UpdateController(on_quit=self._quit_for_update)
        self._wifi = WifiService()
        self._update_notice = UpdateManager.take_update_notice()
        # Bluetooth name = Windows computer name (Settings -> Bluetooth name).
        self._device_name: dict[str, Any] = device_name.info()
        self._device_name_at = 0.0
        self._device_name_msg: str | None = None
        self._hardware_last_poll = 0.0
        self._hardware_cache: dict[str, Any] = {
            "gigaport_connected": False,
            "gigaport_count": 0,
            "gigaport_name": None,
            "codec_connected": False,
            "codec_name": None,
            "headphones_kind": None,
            "roles_flipped": False,
            "can_swap_gigaports": False,
            "battery_percent": None,
            "charging": False,
            "battery_available": False,
        }

        # Private so pywebview does not recurse into the engine object graph.
        self._engine = EngineBridge()
        self.state["engine_ok"] = self._engine.available
        self.state["engine_error"] = None if self._engine.available else self._engine.last_error
        print(
            "[app] engine ready"
            if self._engine.available
            else f"[app] engine unavailable: {self._engine.last_error}"
        )
        # Show already-paired phones without requiring Scan.
        if self._bt.available:
            self._schedule_paired_refresh(force=True)
        self._refresh_hardware_status(force=True)

    # ------------------------------------------------------------------ state --
    def get_state(self) -> dict[str, Any]:
        self._keep_calibration_tone_alive()
        self._refresh_bluetooth_link()
        self._check_engine_health()
        self._maybe_autostart_live()
        self._apply_wifi_policy()
        self._refresh_media()
        self._maybe_save_settings()
        with self._lock:
            if self.state["playing"] and self._engine.available and not self.state.get("live_paused"):
                stats = self._last_stats
                self.state["input_rms"] = float(stats.get("input_rms", 0.0))
                # One signal per shaker row (head, upper back, mid back, legs)
                # plus the stereo audio channels (left, right).
                self.state["zone_rms"] = {
                    "head": float(stats.get("rms_head", 0.0)),
                    "upper": float(stats.get("rms_upper_mid", 0.0)),
                    "mid": float(stats.get("rms_mid", 0.0)),
                    "legs": float(stats.get("rms_legs", 0.0)),
                    "left": float(stats.get("rms_audio_left", 0.0)),
                    "right": float(stats.get("rms_audio_right", 0.0)),
                }
            else:
                self.state["zone_rms"] = {
                    "head": 0.0, "upper": 0.0, "mid": 0.0, "legs": 0.0,
                    "left": 0.0, "right": 0.0,
                }
            self._refresh_live_input_probe()
            self._refresh_hardware_status()
            return self._snapshot()

    def _refresh_hardware_status(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._hardware_last_poll) < 2.0:
            return
        self._hardware_last_poll = now
        playing = bool(self.state.get("playing"))
        try:
            hw = probe_hardware_status(
                playing=playing,
                roles_flipped=bool(self.state.get("gigaport_roles_flipped")),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[app] hardware probe failed: {exc}")
            hw = {}
        percent = self._battery.get_battery_percent()
        self._hardware_cache = {
            "gigaport_connected": bool(hw.get("gigaport_connected")),
            "gigaport_count": int(hw.get("gigaport_count") or 0),
            "gigaport_name": hw.get("gigaport_name"),
            "codec_connected": bool(hw.get("codec_connected")),
            "codec_name": hw.get("codec_name"),
            "headphones_kind": hw.get("headphones_kind"),
            "roles_flipped": bool(hw.get("roles_flipped") or self.state.get("gigaport_roles_flipped")),
            "can_swap_gigaports": bool(hw.get("can_swap_gigaports")),
            "battery_percent": percent,
            "charging": bool(self._battery.is_charging()) if percent is not None else False,
            "battery_available": self._battery.is_available(),
        }

    def _refresh_live_input_probe(self) -> None:
        """Update whether Now Playing can start (AUX or Bluetooth/CABLE only)."""
        playing = bool(self.state.get("playing"))
        live_mode = self.state.get("live_mode")
        if playing and live_mode and live_mode != "demo":
            return
        prefer_bt = bool(self.state["bluetooth"].get("connected_id"))
        try:
            probe = probe_live_input(prefer_bluetooth=prefer_bt)
        except Exception as exc:  # noqa: BLE001
            probe = {
                "ready": False,
                "mode": None,
                "capture_name": None,
                "message": str(exc),
            }
        self.state["live_input"] = probe

    def _schedule_paired_refresh(self, *, force: bool = False) -> None:
        """Keep the paired-phones list current without a Scan button."""
        if not self._bt.available:
            return
        now = time.monotonic()
        with self._lock:
            busy = bool(
                self.state["bluetooth"].get("scanning")
                or self.state["bluetooth"].get("discovering")
                or self.state["bluetooth"].get("pairing_id")
                or self.state["bluetooth"].get("connecting_id")
                or self.state["bluetooth"].get("forgetting")
            )
            if self._paired_refresh_busy or busy:
                return
            if not force and (now - self._last_paired_refresh) < 6.0:
                return
            self._paired_refresh_busy = True
            self._last_paired_refresh = now

        def _work() -> None:
            try:
                devices = self._bt.list_devices()
                with self._lock:
                    bt = self.state["bluetooth"]
                    keep = {bt.get("connected_id"), bt.get("waiting_id")} - {None}
                    by_id = {d["id"]: d for d in devices}
                    for d in self._devices:
                        # Keep the live phone's entry (and its Connected / Reconnecting label).
                        if d.get("id") in keep:
                            by_id[d["id"]] = d
                    self._devices = list(by_id.values())
            except Exception as exc:  # noqa: BLE001
                print(f"[app] paired refresh failed: {exc}")
            finally:
                with self._lock:
                    self._paired_refresh_busy = False

        threading.Thread(target=_work, daemon=True, name="bt-paired-refresh").start()

    def _refresh_bluetooth_link(self) -> None:
        """Track the phone's audio link on every poll.

        connected  -> Live runs (started by _maybe_autostart_live).
        dropped    -> 'Reconnecting…': the link object is kept and re-opened every
                      BT_REOPEN_EVERY_S for BT_RECONNECT_S; the engine keeps running so the
                      music comes straight back.
        not accepted yet -> 'Waiting for phone…' (same retry, BT_WAIT_ACCEPT_S).
        A single failed status check no longer counts as a disconnect.
        """
        with self._lock:
            bt = self.state["bluetooth"]
            connected_id = bt.get("connected_id")
            connecting = bt.get("connecting_id")
            waiting_id = bt.get("waiting_id")
            playing = bool(self.state.get("playing"))
            live_mode = self.state.get("live_mode")
        if connecting:
            return
        now = time.monotonic()

        if connected_id:
            status = self._bt.get_status(connected_id)
            if status.get("connected"):
                self._bt_status_errors = 0
                return
            reason = status.get("reason") or "disconnected"
            if reason == "error":
                # A slow / failed WinRT call is not a disconnect: give it a few polls.
                self._bt_status_errors += 1
                if self._bt_status_errors < BT_STATUS_ERRORS_LOST:
                    return
            self._bt_status_errors = 0
            message = status.get("message") or "Bluetooth disconnected"
            print(f"[app] Bluetooth link lost ({reason}): {message}")
            if reason in ("closed", "waiting", "error"):
                self._start_bt_waiting(connected_id, "reconnect", from_connected=True)
            else:
                self._bt_link_gone(connected_id, message)
            return

        if waiting_id:
            if now < self._bt_wait_check_at:
                return
            self._bt_wait_check_at = now + 2.0
            status = self._bt.get_status(waiting_id)
            if status.get("connected"):
                print("[app] Bluetooth audio link open again")
                self._adopt_bt_connection(waiting_id)
                return
            reason = status.get("reason") or "disconnected"
            if reason in ("bluetooth_off", "device_gone", "no_connection", "state_error"):
                self._bt_link_gone(waiting_id, status.get("message") or "Phone disconnected")
                return
            if now >= self._bt_wait_deadline:
                with self._lock:
                    kind = self.state["bluetooth"].get("waiting_kind")
                msg = (
                    "The phone didn't accept the audio connection. On the phone, choose this "
                    "tablet as the audio device (or tap Connect again)."
                    if kind == "accept"
                    else "The phone didn't come back. Tap Connect to reconnect it."
                )
                print(f"[app] giving up on Bluetooth phone ({kind})")
                try:
                    self._bt.disconnect(waiting_id, forget=False)
                except Exception as exc:  # noqa: BLE001
                    print(f"[app] bt disconnect after timeout: {exc}")
                self._bt_link_gone(waiting_id, msg)
                return
            if now >= self._bt_reopen_at and not self._bt_reopen_busy:
                self._bt_reopen_at = now + BT_REOPEN_EVERY_S
                self._bt_reopen_busy = True
                threading.Thread(
                    target=self._bt_reopen_worker, args=(waiting_id,), daemon=True, name="bt-reopen"
                ).start()
            return

        # No phone at all. Bluetooth Live keeps the Gigaports open for a while (a phone that
        # comes back plays instantly), then lets them go.
        if playing and live_mode == "cable":
            if self._bt_idle_since is None:
                self._bt_idle_since = now
            elif now - self._bt_idle_since >= BT_IDLE_ENGINE_STOP_S:
                print("[app] no phone for 5 min - stopping Bluetooth Live")
                self._bt_idle_since = None
                self.stop_live()
        else:
            self._bt_idle_since = None

    def _start_bt_waiting(self, device_id: str, kind: str, *, from_connected: bool = False) -> None:
        """Show 'Waiting for phone…' / 'Reconnecting…' and retry opening the link."""
        now = time.monotonic()
        self._bt_wait_deadline = now + (BT_RECONNECT_S if kind == "reconnect" else BT_WAIT_ACCEPT_S)
        self._bt_wait_check_at = now + 2.0
        # A dropped link: give the phone a moment to come back by itself before re-opening.
        self._bt_reopen_at = now + (3.0 if from_connected else BT_REOPEN_EVERY_S)
        with self._lock:
            bt = self.state["bluetooth"]
            bt["connected_id"] = None
            bt["waiting_id"] = device_id
            bt["waiting_kind"] = kind
            bt["error"] = (
                "Phone disconnected - reconnecting… Keep the phone close."
                if kind == "reconnect"
                else "Waiting for the phone… If it asks, allow the audio connection on the phone."
            )
            for d in self._devices:
                if d.get("id") == device_id:
                    d["profile"] = "Reconnecting…" if kind == "reconnect" else "Waiting for phone…"

    def _adopt_bt_connection(self, device_id: str) -> None:
        with self._lock:
            bt = self.state["bluetooth"]
            bt["connected_id"] = device_id
            bt["waiting_id"] = None
            bt["waiting_kind"] = None
            bt["error"] = None
            self._mark_device_connected_locked(device_id)
        self._bt_status_errors = 0
        self._bt_idle_since = None
        self._live_retry_at = 0.0  # start / keep Live right away

    def _bt_link_gone(self, device_id: str, message: str) -> None:
        """Phone gone for good (Bluetooth off, unpaired, or it didn't come back)."""
        with self._lock:
            bt = self.state["bluetooth"]
            if bt.get("connected_id") == device_id:
                bt["connected_id"] = None
            if bt.get("waiting_id") == device_id:
                bt["waiting_id"] = None
                bt["waiting_kind"] = None
            bt["error"] = message
            for d in self._devices:
                if d.get("id") == device_id:
                    d["profile"] = "Paired - tap Connect"
                    d["audio_ready"] = False
        self._bt_idle_since = time.monotonic()
        self._schedule_paired_refresh(force=True)

    def _mark_device_connected_locked(self, device_id: str, old_id: str | None = None) -> None:
        for d in self._devices:
            if d.get("id") in (device_id, old_id):
                d["id"] = device_id
                d["paired"] = True
                d["audio_ready"] = True
                d["profile"] = "Connected · Audio ready"

    def _bt_reopen_worker(self, device_id: str) -> None:
        try:
            result = self._bt.connect(device_id)
            print(f"[app] re-open Bluetooth link: {result.get('status') or result.get('error')}")
            if result.get("ok"):
                with self._lock:
                    still = self.state["bluetooth"].get("waiting_id") == device_id
                if still:
                    self._adopt_bt_connection(str(result.get("device_id") or device_id))
        except Exception as exc:  # noqa: BLE001
            print(f"[app] re-open Bluetooth link failed: {exc}")
        finally:
            self._bt_reopen_busy = False

    # ------------------------------------------------- engine health / auto Live --
    def _check_engine_health(self) -> None:
        """Notice a crashed engine or a device that stopped (Gigaport unplugged) instead of
        showing 'playing' forever with no sound. Live is then restarted with backoff."""
        with self._lock:
            playing = bool(self.state.get("playing"))
            live_mode = self.state.get("live_mode")
            busy = bool(self.state.get("play_busy") or self.state.get("calibration_busy"))
        active = playing or live_mode == "calibration"
        if not active or busy or not self._engine.available:
            self._last_stats = {}
            return
        health = self._engine.health()
        if health.get("running") and health.get("streams_ok", True):
            self._last_stats = dict(health.get("stats") or {})
            return
        if not health.get("crashed") and "streams_ok" not in health and not playing:
            # Calibration engine already closed elsewhere: just tidy up, no error.
            with self._lock:
                self.state["calibrating"] = None
                self.state["live_mode"] = None
                self._cal_started_engine = False
            return
        if health.get("streams_ok") is False:
            why = (
                f"An audio device stopped ({health.get('error') or 'no response'}). "
                "Check that both Gigaports are plugged in."
            )
        else:
            why = health.get("error") or "The audio engine stopped."
        print(f"[app] engine fault in {live_mode}: {why}")
        try:
            self._engine.stop_live()
        except Exception as exc:  # noqa: BLE001
            print(f"[app] stop after engine fault: {exc}")
        now = time.monotonic()
        with self._lock:
            self._last_stats = {}
            self.state["playing"] = False
            self.state["input_rms"] = 0.0
            self.state["live_mode"] = None
            self._live_started_at = None
            if live_mode in ("cable", "aux"):
                self.state["engine_error"] = why + " Restarting…"
                # Bluetooth Live comes back by itself while the phone is connected; AUX needs this.
                self._live_recover = live_mode == "aux"
                self._live_retry_at = now + self._live_retry_delay
            elif live_mode == "demo":
                self.state["engine_error"] = why
                self.state["demo"]["error"] = why
            elif live_mode == "calibration":
                self.state["calibrating"] = None
                self.state["haptic_cal_error"] = why
                self._cal_started_engine = False

    def _maybe_autostart_live(self) -> None:
        """Start Live when a phone is connected (or an AUX session must recover). Runs in a
        thread so the UI poll never waits for the Gigaports; retries back off 2 s → 30 s."""
        with self._lock:
            bt = self.state["bluetooth"]
            phone = bool(bt.get("connected_id")) and not bt.get("connecting_id")
            recover = self._live_recover and not bt.get("connected_id") and not bt.get("waiting_id")
            playing = bool(self.state.get("playing"))
            live_mode = self.state.get("live_mode")
            busy = bool(self.state.get("play_busy") or self.state.get("calibration_busy"))
            needs = (not playing) or live_mode == "demo"
            if not (phone or recover) or not needs or busy or self._autostart_busy:
                return
            now = time.monotonic()
            if now < self._live_retry_at:
                return
            self._autostart_busy = True

        def _work() -> None:
            try:
                print("[app] starting Live " + ("(Bluetooth connected)" if phone else "(recovering)"))
                snap = self.start_live()
                ok = bool(snap.get("playing"))
            except Exception as exc:  # noqa: BLE001
                print(f"[app] auto-start Live failed: {exc}")
                ok = False
            with self._lock:
                if ok:
                    self._live_retry_delay = LIVE_RETRY_MIN_S
                    self._live_recover = False
                else:
                    self._live_retry_delay = min(LIVE_RETRY_MAX_S, self._live_retry_delay * 2)
                    self._live_retry_at = time.monotonic() + self._live_retry_delay
                    print(f"[app] Live didn't start - next try in {self._live_retry_delay:.0f}s")
                self._autostart_busy = False

        threading.Thread(target=_work, daemon=True, name="live-autostart").start()

    # ------------------------------------------------------------- saved settings --
    def _settings_changed(self) -> None:
        self._settings_dirty = True

    def _maybe_save_settings(self, force: bool = False) -> None:
        if not self._settings_dirty:
            return
        now = time.monotonic()
        if not force and now - self._settings_saved_at < SETTINGS_SAVE_EVERY_S:
            return
        self._settings_saved_at = now
        with self._lock:
            data = {k: self.state.get(k) for k in _SETTINGS_KEYS}
            data["zone_enabled"] = dict(self.state.get("zone_enabled") or {})
        path = _launcher_file(SETTINGS_NAME)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, path)
            self._settings_dirty = False
        except OSError as exc:
            print(f"[app] could not save settings to {path}: {exc}")

    def _refresh_media(self) -> None:
        """Pull SMTC metadata when Windows exposes it; else Live elapsed + device name."""
        with self._lock:
            is_demo = self.state.get("live_mode") == "demo" and self.state.get("playing")

        if is_demo:
            progress = self._engine.get_demo_progress()
            with self._lock:
                self.state["media"] = {
                    "ok": True,
                    "can_seek": True,
                    "can_prev": True,
                    "can_next": True,
                    "via": "demo",
                }
                duration = float(
                    progress.get("duration")
                    or self._demo_track.get("duration")
                    or 0.0
                )
                self.state["position"] = float(progress.get("position") or 0.0)
                self.state["track"] = {
                    "title": self._demo_track.get("title") or "Demo",
                    "artist": self._demo_track.get("artist") or "MUVI test track",
                    "album": self._demo_track.get("album") or "",
                    "duration": duration,
                    "artwork": self._demo_track.get("artwork") or "assets/artwork.svg",
                }
                if duration > 0:
                    self._demo_track["duration"] = duration
            return

        info = self._media.get_now_playing()
        with self._lock:
            connected_id = self.state["bluetooth"].get("connected_id")
            device_name = None
            if connected_id:
                for d in self._devices:
                    if d.get("id") == connected_id:
                        device_name = d.get("name")
                        break

            has_smtc = bool(info.get("ok") and (info.get("title") or info.get("duration")))
            self.state["media"] = {
                "ok": has_smtc,
                "can_seek": bool(info.get("can_seek")) and has_smtc,
                # Only a Windows media session can be skipped; a Bluetooth phone can't (see top).
                "can_prev": has_smtc and bool(info.get("can_prev")),
                "can_next": has_smtc and bool(info.get("can_next")),
                "via": info.get("via"),
            }

            if has_smtc:
                if info.get("title"):
                    self.state["track"]["title"] = info["title"]
                if info.get("artist"):
                    self.state["track"]["artist"] = info["artist"]
                if info.get("album"):
                    self.state["track"]["album"] = info["album"]
                self.state["track"]["duration"] = float(info.get("duration") or 0.0)
                self.state["position"] = float(info.get("position") or 0.0)
                return

            # No SMTC song metadata (typical for phone→PC Bluetooth).
            if self.state["playing"] and self._live_started_at is not None:
                elapsed = max(0.0, time.monotonic() - self._live_started_at)
                self.state["position"] = elapsed
                self.state["track"]["duration"] = 0.0  # unknown song length
                mode = self.state.get("live_mode")
                if mode == "aux":
                    self.state["track"]["title"] = "AUX Live"
                    self.state["track"]["artist"] = self.state.get("capture_name") or "Line-in"
                    self.state["track"]["album"] = ""
                elif connected_id:
                    self.state["track"]["title"] = "Bluetooth Live"
                    self.state["track"]["artist"] = device_name or "Phone"
                    self.state["track"]["album"] = ""
                else:
                    # Bluetooth Live kept open while the phone is away (see BT_IDLE_ENGINE_STOP_S).
                    waiting = bool(self.state["bluetooth"].get("waiting_id"))
                    self.state["track"]["title"] = "Reconnecting…" if waiting else "Phone disconnected"
                    self.state["track"]["artist"] = (
                        "Keep the phone close" if waiting else "Connect a phone to play"
                    )
                    self.state["track"]["album"] = ""
            elif connected_id:
                self.state["position"] = 0.0
                self.state["track"]["duration"] = 0.0
                self.state["track"]["title"] = "Ready"
                self.state["track"]["artist"] = device_name or "Phone connected"
                self.state["track"]["album"] = ""
            else:
                self.state["track"]["title"] = self._default_track["title"]
                self.state["track"]["artist"] = self._default_track["artist"]
                self.state["track"]["album"] = self._default_track["album"]
                self.state["track"]["duration"] = 0.0
                self.state["position"] = 0.0

    def _snapshot(self) -> dict[str, Any]:
        snap = dict(self.state)
        snap["track"] = dict(self.state["track"])
        snap["bluetooth"] = dict(self.state["bluetooth"])
        snap["media"] = dict(self.state.get("media") or {})
        snap["zone_rms"] = dict(self.state.get("zone_rms") or {})
        snap["zone_enabled"] = dict(self.state.get("zone_enabled") or {})
        snap["audio_muted"] = bool(self.state.get("audio_muted"))
        snap["vibration_muted"] = bool(self.state.get("vibration_muted"))
        snap["live_input"] = dict(self.state.get("live_input") or {})
        snap["demo"] = dict(self.state.get("demo") or {})
        snap["devices"] = [dict(d) for d in self._devices]
        snap["nearby_devices"] = [dict(d) for d in self._nearby]
        snap["hardware"] = dict(self._hardware_cache)
        snap["app_version"] = UpdateManager.current_version()
        snap["update"] = self._updater.snapshot()
        snap["update_notice"] = self._update_notice
        now = time.monotonic()
        if now - self._device_name_at > 5.0:
            self._device_name_at = now
            try:
                self._device_name = device_name.info()
            except Exception as exc:  # noqa: BLE001
                print(f"[app] device name: {exc}")
        snap["device_name"] = {**self._device_name, "message": self._device_name_msg}
        snap["wifi"] = self._wifi.snapshot()
        snap["wifi"]["phone_active"] = self._phone_active_locked()
        snap["wifi"]["off_for_bluetooth"] = bool(self._wifi_off_for_bt)
        cal = self.state["haptic_cal"]
        snap["haptic_cal"] = {
            "master_gain_db": cal["master_gain_db"],
            "channel_gain_db": list(cal["channel_gain_db"]),
            "channel_enabled": list(cal["channel_enabled"]),
            "channel_labels": [list(x) for x in cal["channel_labels"]],
            "parts": list(haptics_calibration.BODY_PARTS),
            "sides": list(haptics_calibration.SIDES),
        }
        return snap

    # -------------------------------------------------------- Start / Stop Live --
    def start_live(self) -> dict[str, Any]:
        """Same as old PyQt Start Live button."""
        with self._lock:
            if self.state["play_busy"]:
                return self._snapshot()
            self.state["play_busy"] = True
            was_demo = self.state.get("live_mode") == "demo" and self.state.get("playing")
            # The silent calibration engine has the Gigaports open. It used to make the engine
            # look "already running", so Bluetooth Live never really started.
            was_cal = self.state.get("live_mode") == "calibration" or self._cal_started_engine
            cal_channel = self.state.get("calibrating")

        try:
            if was_demo or was_cal:
                try:
                    self._engine.stop_live()
                except Exception:  # noqa: BLE001
                    pass
                if was_cal:
                    self._cal_started_engine = False
                    with self._lock:
                        self.state["live_mode"] = None

            if not self._engine.available:
                with self._lock:
                    msg = self._engine.last_error or "engine unavailable"
                    self.state["engine_error"] = msg
                    self.state["playing"] = False
                    self.state["play_busy"] = False
                    return self._snapshot()

            with self._lock:
                prefer_bt = bool(self.state["bluetooth"].get("connected_id"))
                self._refresh_live_input_probe()
                live_input = dict(self.state.get("live_input") or {})
            if not live_input.get("ready"):
                with self._lock:
                    self.state["engine_error"] = live_input.get("message")
                    self.state["playing"] = False
                    self.state["play_busy"] = False
                    if was_cal:
                        self.state["calibrating"] = None
                    return self._snapshot()

            result = self._engine.start_live(
                volume=self.state["volume"],
                vibration=self.state["vibration"],
                cutoff_hz=self.state["cutoff_hz"],
                prefer_bluetooth=bool(self.state["bluetooth"].get("connected_id")),
                speaker_route=self.state.get("speaker_route", "headphones"),
                vibration_mode=self.state.get("vibration_mode", "haptics"),
                vibration_output_index=self.state.get("vibration_output_index"),
                audio_output_index=self.state.get("audio_output_index"),
                roles_flipped=bool(self.state.get("gigaport_roles_flipped")),
                zone_enabled=dict(self.state.get("zone_enabled") or {}),
                audio_muted=bool(self.state.get("audio_muted")),
                vibration_muted=bool(self.state.get("vibration_muted")),
            )
            if result.get("ok"):
                self._cal_started_engine = False
                self._push_haptic_cal()
                if was_cal and cal_channel is not None:
                    # Calibration sheet still open: keep the tone going on the Live engine.
                    self._engine.haptic_test(cal_channel)
            with self._lock:
                self.state["playing"] = bool(result.get("ok"))
                self.state["capture_name"] = result.get("capture")
                self.state["output_name"] = result.get("output_name")
                self.state["output_layout"] = result.get("output_layout")
                self.state["audio_output_channels"] = int(result.get("audio_output_channels") or 0)
                self.state["live_mode"] = result.get("mode") if result.get("ok") else None
                self.state["engine_error"] = None if result.get("ok") else result.get("error")
                self.state["demo"]["error"] = None
                if result.get("ok"):
                    self._live_started_at = time.monotonic()
                    self.state["live_paused"] = False  # the new engine starts with the user's mutes
                else:
                    self._live_started_at = None
                    self.state["bluetooth"]["error"] = result.get("error")
                    if was_cal:
                        self.state["calibrating"] = None
                self.state["play_busy"] = False
                return self._snapshot()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state["playing"] = False
                self.state["engine_error"] = str(exc)
                self.state["play_busy"] = False
                self._live_started_at = None
                return self._snapshot()

    def stop_live(self) -> dict[str, Any]:
        """Same as old PyQt Stop Live button (also stops Demo)."""
        with self._lock:
            if self.state["play_busy"]:
                return self._snapshot()
            self.state["play_busy"] = True
            self._live_recover = False
        try:
            self._engine.stop_live()
            with self._lock:
                self.state["playing"] = False
                self.state["input_rms"] = 0.0
                self.state["live_mode"] = None
                self.state["play_busy"] = False
                self._live_started_at = None
                return self._snapshot()
        except Exception:  # noqa: BLE001
            with self._lock:
                self.state["playing"] = False
                self.state["live_mode"] = None
                self.state["play_busy"] = False
                self._live_started_at = None
                return self._snapshot()

    def toggle_play(self) -> dict[str, Any]:
        """Now Playing Play = Start Live / Stop Live.

        If Demo is playing, Play starts Live (replaces demo) instead of only stopping.
        While Bluetooth is connected, Live is owned by the BT session — Play/Stop
        does not stop it (disconnect does).
        """
        with self._lock:
            if self.state["play_busy"]:
                return self._snapshot()
            playing = self.state["playing"]
            is_demo = self.state.get("live_mode") == "demo"
            bt_connected = bool(self.state["bluetooth"].get("connected_id"))
        if bt_connected:
            if playing and not is_demo:
                # Live already runs for the phone: Play/Pause controls the phone's music.
                return self.media_play_pause()
            return self.start_live()
        if playing and is_demo:
            return self.start_live()
        if playing:
            return self.stop_live()
        return self.start_live()

    def start_demo(self) -> dict[str, Any]:
        """Play assets/demo.wav through the same Gigaport vibration path."""
        with self._lock:
            if self.state["play_busy"]:
                return self._snapshot()
            self.state["play_busy"] = True
            was_live = self.state.get("playing") and self.state.get("live_mode") != "demo"
            self._live_recover = False
            if self.state.get("live_mode") == "calibration" or self._cal_started_engine:
                # Demo replaces the silent calibration engine (the bridge restarts the worker).
                self._cal_started_engine = False
                self.state["live_mode"] = None

        try:
            if was_live:
                try:
                    self._engine.stop_live()
                except Exception:  # noqa: BLE001
                    pass

            if not self._engine.available:
                with self._lock:
                    msg = self._engine.last_error or "engine unavailable"
                    self.state["engine_error"] = msg
                    self.state["demo"]["error"] = msg
                    self.state["playing"] = False
                    self.state["play_busy"] = False
                    return self._snapshot()

            result = self._engine.start_demo(
                volume=self.state["volume"],
                vibration=self.state["vibration"],
                cutoff_hz=self.state["cutoff_hz"],
                vibration_mode=self.state.get("vibration_mode", "haptics"),
                vibration_output_index=self.state.get("vibration_output_index"),
                audio_output_index=self.state.get("audio_output_index"),
                roles_flipped=bool(self.state.get("gigaport_roles_flipped")),
                zone_enabled=dict(self.state.get("zone_enabled") or {}),
                audio_muted=bool(self.state.get("audio_muted")),
                vibration_muted=bool(self.state.get("vibration_muted")),
            )
            if result.get("ok"):
                self._cal_started_engine = False
                self._push_haptic_cal()
            with self._lock:
                ok = bool(result.get("ok"))
                self.state["playing"] = ok
                self.state["capture_name"] = result.get("capture")
                self.state["output_name"] = result.get("output_name")
                self.state["output_layout"] = result.get("output_layout")
                self.state["audio_output_channels"] = int(result.get("audio_output_channels") or 0)
                self.state["live_mode"] = "demo" if ok else None
                self.state["engine_error"] = None if ok else result.get("error")
                self.state["demo"]["error"] = None if ok else result.get("error")
                self.state["demo"]["available"] = True
                if ok:
                    self._live_started_at = time.monotonic()
                    if result.get("title"):
                        self._demo_track["title"] = result["title"]
                    if result.get("artist"):
                        self._demo_track["artist"] = result["artist"]
                    if result.get("album"):
                        self._demo_track["album"] = result["album"]
                    if result.get("duration"):
                        self._demo_track["duration"] = float(result["duration"])
                    self.state["track"] = dict(self._demo_track)
                    self.state["position"] = 0.0
                    self.state["media"] = {
                        "ok": True,
                        "can_seek": True,
                        "can_prev": True,
                        "can_next": True,
                        "via": "demo",
                    }
                else:
                    self._live_started_at = None
                self.state["play_busy"] = False
                return self._snapshot()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state["playing"] = False
                self.state["live_mode"] = None
                self.state["engine_error"] = str(exc)
                self.state["demo"]["error"] = str(exc)
                self.state["play_busy"] = False
                self._live_started_at = None
                return self._snapshot()

    def toggle_demo_play(self) -> dict[str, Any]:
        """Demo tab Play = start/stop demo.wav."""
        with self._lock:
            if self.state["play_busy"]:
                return self._snapshot()
            playing_demo = self.state["playing"] and self.state.get("live_mode") == "demo"
        if playing_demo:
            return self.stop_live()
        return self.start_demo()

    def seek(self, seconds: float) -> dict[str, Any]:
        """Seek demo.wav or phone track via Windows media session."""
        with self._lock:
            is_demo = self.state.get("live_mode") == "demo" and self.state.get("playing")
        if is_demo:
            progress = self._engine.seek_demo(float(seconds))
            with self._lock:
                self.state["position"] = float(progress.get("position") or seconds)
                if progress.get("duration"):
                    self.state["track"]["duration"] = float(progress["duration"])
            self._refresh_media()
            with self._lock:
                return self._snapshot()

        result = self._media.seek(float(seconds))
        if result.get("ok"):
            with self._lock:
                self.state["position"] = max(0.0, float(seconds))
        self._refresh_media()
        with self._lock:
            return self._snapshot()

    def _push_mutes(self) -> None:
        """Send the effective mutes to the engine: user mutes, plus everything while paused."""
        with self._lock:
            paused = bool(self.state.get("live_paused"))
            audio = paused or bool(self.state.get("audio_muted"))
            vib = paused or bool(self.state.get("vibration_muted"))
        self._engine.set_audio_muted(audio)
        self._engine.set_vibration_muted(vib)

    def _clear_live_pause(self) -> None:
        """Touching a sound control (or Live restarting) ends a tablet pause."""
        with self._lock:
            was = bool(self.state.get("live_paused"))
            self.state["live_paused"] = False
        if was:
            self._push_mutes()

    def media_play_pause(self) -> dict[str, Any]:
        """Now Playing Play/Pause while a phone is connected: pause / resume on the tablet.

        The phone can't be paused from here (see the note at the top), so the headphones and
        the vibration are silenced instead; the engine keeps running, so resume is instant."""
        with self._lock:
            phone = bool(self.state["bluetooth"].get("connected_id"))
            live = bool(self.state.get("playing")) and self.state.get("live_mode") == "cable"
        if not phone or not live:
            return self.toggle_play()
        with self._lock:
            self.state["live_paused"] = not bool(self.state.get("live_paused"))
            paused = self.state["live_paused"]
        print(f"[app] Bluetooth Live {'paused' if paused else 'resumed'} on the tablet")
        self._push_mutes()
        with self._lock:
            return self._snapshot()

    def previous_track(self) -> dict[str, Any]:
        with self._lock:
            is_demo = self.state.get("live_mode") == "demo" and self.state.get("playing")
        if is_demo:
            return self.seek(0.0)
        self._media.previous()
        self._refresh_media()
        with self._lock:
            return self._snapshot()

    def next_track(self) -> dict[str, Any]:
        with self._lock:
            is_demo = self.state.get("live_mode") == "demo" and self.state.get("playing")
        if is_demo:
            return self.seek(0.0)
        self._media.next()
        self._refresh_media()
        with self._lock:
            return self._snapshot()

    # ---------------------------------------------------------------- controls --
    def set_volume(self, value: float) -> dict[str, Any]:
        self._clear_live_pause()
        vol = max(0.0, min(1.0, float(value)))
        mute = vol <= 0.0
        with self._lock:
            self.state["volume"] = vol
            self.state["audio_muted"] = mute
            self._settings_changed()
        self._engine.set_volume(vol)
        self._engine.set_audio_muted(mute)
        with self._lock:
            return self._snapshot()

    def set_audio_muted(self, muted: bool) -> dict[str, Any]:
        self._clear_live_pause()
        with self._lock:
            self.state["audio_muted"] = bool(muted)
        self._engine.set_audio_muted(bool(muted))
        with self._lock:
            return self._snapshot()

    def set_vibration(self, value: float) -> dict[str, Any]:
        self._clear_live_pause()
        vib = max(0.0, min(1.0, float(value)))
        mute = vib <= 0.0
        with self._lock:
            self.state["vibration"] = vib
            self.state["vibration_muted"] = mute
            self._settings_changed()
        self._engine.set_vibration(vib)
        self._engine.set_vibration_muted(mute)
        with self._lock:
            return self._snapshot()

    def set_vibration_muted(self, muted: bool) -> dict[str, Any]:
        self._clear_live_pause()
        with self._lock:
            self.state["vibration_muted"] = bool(muted)
        self._engine.set_vibration_muted(bool(muted))
        with self._lock:
            return self._snapshot()

    def set_zone_enabled(self, zone: str, enabled: bool) -> dict[str, Any]:
        self._clear_live_pause()
        key = str(zone).strip().lower()
        if key not in ("head", "upper", "mid", "legs"):
            with self._lock:
                return self._snapshot()
        with self._lock:
            self.state.setdefault("zone_enabled", {})[key] = bool(enabled)
            self._settings_changed()
            # Turning any zone back on clears master vibration mute.
            if enabled:
                self.state["vibration_muted"] = False
        self._engine.set_zone_enabled(key, bool(enabled))
        if enabled:
            self._engine.set_vibration_muted(False)
        with self._lock:
            return self._snapshot()

    def set_highpass_hz(self, value: float) -> dict[str, Any]:
        """UI Bass cutoff → vibration low-pass (muvi --cutoff)."""
        return self.set_cutoff_hz(value)

    def set_cutoff_hz(self, value: float) -> dict[str, Any]:
        with self._lock:
            hz = max(40.0, min(250.0, float(value)))
            self.state["cutoff_hz"] = hz
            self.state["highpass_hz"] = hz  # keep UI field in sync
            self._settings_changed()
        self._engine.set_cutoff_hz(hz)
        with self._lock:
            return self._snapshot()

    def set_speaker_route(self, route: str) -> dict[str, Any]:
        """Switch audio output between Gigaport 2 ch 1-2 (headphones) and ch 3-4 (speaker)."""
        route = str(route).strip().lower()
        if route not in ("headphones", "secondary"):
            route = "headphones"
        with self._lock:
            self.state["speaker_route"] = route
            self._settings_changed()
        self._engine.set_speaker_route(route)
        with self._lock:
            return self._snapshot()

    def set_vibration_mode(self, mode: str) -> dict[str, Any]:
        """Switch vibration: 'zones', 'stereo' (L/R), or 'haptics' (Quake DSP)."""
        mode = str(mode).strip().lower()
        if mode not in ("zones", "stereo", "haptics"):
            mode = "haptics"
        with self._lock:
            self.state["vibration_mode"] = mode
            self._settings_changed()
        self._engine.set_vibration_mode(mode)
        with self._lock:
            return self._snapshot()

    # ------------------------------------------------------ haptics calibration --
    def _push_haptic_cal(self) -> None:
        with self._lock:
            cal = haptics_calibration.normalize(self.state["haptic_cal"])
        self._engine.set_haptic_calibration(cal)

    def set_haptic_master(self, gain_db: float) -> dict[str, Any]:
        """Overall haptic level in dB (applied in the DSP before its limiter)."""
        with self._lock:
            cal = dict(self.state["haptic_cal"])
            cal["master_gain_db"] = gain_db
            self.state["haptic_cal"] = haptics_calibration.normalize(cal)
            self.state["haptic_cal_dirty"] = True
        self._push_haptic_cal()
        with self._lock:
            return self._snapshot()

    def set_haptic_channel(
        self,
        channel: int,
        gain_db: float | None = None,
        enabled: bool | None = None,
        part: str | None = None,
        side: str | None = None,
    ) -> dict[str, Any]:
        """Edit one Gigaport channel (1-8): trim in dB, on/off, body position."""
        try:
            i = int(channel) - 1
        except (TypeError, ValueError):
            i = -1
        with self._lock:
            if 0 <= i < haptics_calibration.N_CHANNELS:
                cal = haptics_calibration.normalize(self.state["haptic_cal"])
                if gain_db is not None:
                    cal["channel_gain_db"][i] = gain_db
                if enabled is not None:
                    cal["channel_enabled"][i] = bool(enabled)
                part_now, side_now = cal["channel_labels"][i]
                cal["channel_labels"][i] = [part or part_now, side or side_now]
                self.state["haptic_cal"] = haptics_calibration.normalize(cal)
                self.state["haptic_cal_dirty"] = True
        self._push_haptic_cal()
        with self._lock:
            return self._snapshot()

    def save_haptic_calibration(self) -> dict[str, Any]:
        with self._lock:
            cal = haptics_calibration.normalize(self.state["haptic_cal"])
            try:
                path = haptics_calibration.save(cal)
                self.state["haptic_cal_dirty"] = False
                self.state["haptic_cal_error"] = None
                print(f"[app] haptics calibration saved: {path}")
            except OSError as exc:
                self.state["haptic_cal_error"] = f"Could not save calibration: {exc}"
            return self._snapshot()

    def revert_haptic_calibration(self) -> dict[str, Any]:
        with self._lock:
            self.state["haptic_cal"] = haptics_calibration.load()
            self.state["haptic_cal_dirty"] = False
            self.state["haptic_cal_error"] = None
        self._push_haptic_cal()
        with self._lock:
            return self._snapshot()

    def haptic_test(self, channel: int | None = None) -> dict[str, Any]:
        """Calibration tone on Gigaport channel 1-8; None stops it.

        If nothing is playing, the Gigaport outputs are opened with silence (same path as
        Demo, nothing in the headphones) and closed again when calibration ends. If Live or
        Demo is playing, the tone replaces the vibration while it runs.
        """
        ch: int | None
        try:
            ch = int(channel) if channel is not None else None
        except (TypeError, ValueError):
            ch = None
        if ch is not None and not 1 <= ch <= haptics_calibration.N_CHANNELS:
            ch = None

        if ch is None:
            self._engine.haptic_test(None)
            stop_engine = self._cal_started_engine
            self._cal_started_engine = False
            if stop_engine:
                try:
                    self._engine.stop_live()
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self.state["calibrating"] = None
                if stop_engine:
                    self.state["live_mode"] = None
                return self._snapshot()

        with self._lock:
            busy = self.state["play_busy"] or self.state["calibration_busy"]
            playing = bool(self.state["playing"])
        if busy:
            with self._lock:
                return self._snapshot()

        if not playing and not self._engine.running:
            with self._lock:
                self.state["calibration_busy"] = True
            try:
                result = self._engine.start_demo(
                    volume=0.0,
                    vibration=self.state["vibration"],
                    cutoff_hz=self.state["cutoff_hz"],
                    vibration_mode=self.state.get("vibration_mode", "haptics"),
                    vibration_output_index=self.state.get("vibration_output_index"),
                    audio_output_index=self.state.get("audio_output_index"),
                    roles_flipped=bool(self.state.get("gigaport_roles_flipped")),
                    zone_enabled=dict(self.state.get("zone_enabled") or {}),
                    audio_muted=True,
                    vibration_muted=False,
                    silent=True,
                )
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            with self._lock:
                self.state["calibration_busy"] = False
                if not result.get("ok"):
                    self.state["haptic_cal_error"] = (
                        "Could not open the Gigaport for calibration: "
                        f"{result.get('error') or 'unknown error'}"
                    )
                    self.state["calibrating"] = None
                    return self._snapshot()
                self.state["haptic_cal_error"] = None
                self.state["live_mode"] = "calibration"
            self._cal_started_engine = True
            self._push_haptic_cal()

        self._engine.haptic_test(ch)
        self._cal_keepalive_at = time.monotonic()
        with self._lock:
            self.state["calibrating"] = ch
            return self._snapshot()

    def _keep_calibration_tone_alive(self) -> None:
        """The engine stops the tone ~10 s after the last request (e.g. if the UI died);
        renew it every few seconds while the calibration sheet is open."""
        with self._lock:
            ch = self.state.get("calibrating")
        if ch is None or time.monotonic() - self._cal_keepalive_at < 3.0:
            return
        self._cal_keepalive_at = time.monotonic()
        self._engine.haptic_test(ch)

    def set_output_devices(
        self,
        vibration_output_index: int | None = None,
        audio_output_index: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self.state["vibration_output_index"] = (
                int(vibration_output_index) if vibration_output_index is not None else None
            )
            self.state["audio_output_index"] = (
                int(audio_output_index) if audio_output_index is not None else None
            )
            return self._snapshot()

    def swap_gigaport_roles(self) -> dict[str, Any]:
        """Flip which Gigaport is vibration vs headphones (dual Gigaport only).

        If Live or Demo is already running, restart it automatically so the new
        roles apply without the user pressing Stop/Play.
        """
        with self._lock:
            self.state["gigaport_roles_flipped"] = not bool(
                self.state.get("gigaport_roles_flipped")
            )
            # Clear sticky manual indices so auto-pick + flip decide roles.
            self.state["vibration_output_index"] = None
            self.state["audio_output_index"] = None
            flipped = bool(self.state["gigaport_roles_flipped"])
            self._settings_changed()
            playing = bool(self.state.get("playing"))
            live_mode = self.state.get("live_mode")

        self._hardware_last_poll = 0.0
        self._refresh_hardware_status(force=True)
        print(f"[app] Gigaport roles flipped={flipped} restart={playing} mode={live_mode}")

        if playing:
            was_demo = live_mode == "demo"
            try:
                self._engine.stop_live()
            except Exception as exc:  # noqa: BLE001
                print(f"[app] swap stop failed: {exc}")
            with self._lock:
                self.state["playing"] = False
                self.state["live_mode"] = None
                self.state["play_busy"] = False
                self._live_started_at = None
            if was_demo:
                return self.start_demo()
            return self.start_live()

        with self._lock:
            return self._snapshot()

    def get_audio_settings(self) -> dict[str, Any]:
        with self._lock:
            playing = bool(self.state.get("playing"))
            output_layout = self.state.get("output_layout")
            vibration_output_index = self.state.get("vibration_output_index")
            audio_output_index = self.state.get("audio_output_index")
            output_name = self.state.get("output_name")
            engine_error = self.state.get("engine_error")
            roles_flipped = bool(self.state.get("gigaport_roles_flipped"))
        settings = self._engine.get_audio_settings(
            output_layout=output_layout,
            vibration_output_index=vibration_output_index,
            audio_output_index=audio_output_index,
            output_name=output_name,
            engine_error=engine_error,
            roles_flipped=roles_flipped,
        )
        settings["active"] = playing
        settings["selected_vibration_index"] = vibration_output_index
        settings["selected_audio_index"] = audio_output_index
        settings["roles_flipped"] = roles_flipped
        return settings

    def refresh_audio_devices(self) -> dict[str, Any]:
        try:
            probe_hardware_status(
                playing=False,
                force_refresh=True,
                roles_flipped=bool(self.state.get("gigaport_roles_flipped")),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[app] hardware refresh failed: {exc}")
        self._hardware_last_poll = 0.0
        self._refresh_hardware_status(force=True)
        return self.get_audio_settings()

    # --------------------------------------------------------------- bluetooth --
    def list_devices(self) -> list[dict[str, Any]]:
        if self._bt.available:
            devices = self._bt.list_devices()
            with self._lock:
                self._devices = devices
                return [dict(d) for d in self._devices]
        with self._lock:
            return []

    def scan_bluetooth(self) -> dict[str, Any]:
        """Scan = find nearby unpaired phones. Paired list refreshes automatically."""
        with self._lock:
            if (
                self.state["bluetooth"].get("scanning")
                or self.state["bluetooth"].get("discovering")
                or self.state["bluetooth"].get("pairing_id")
            ):
                return self._snapshot()
            if self._phone_active_locked():
                # A nearby scan keeps the radio busy for up to 40 s and makes the music choppy.
                self.state["bluetooth"]["error"] = (
                    "Disconnect the phone first to look for other phones."
                )
                return self._snapshot()
            self.state["bluetooth"]["scanning"] = True
            self.state["bluetooth"]["discovering"] = True
            self.state["bluetooth"]["error"] = None
            self._nearby = []
            snap = self._snapshot()

        def _on_nearby(devices: list[dict[str, Any]]) -> None:
            with self._lock:
                self._nearby = list(devices)

        def _work() -> None:
            nearby: list[dict[str, Any]] = []
            error: str | None = None
            try:
                if not self._bt.available:
                    error = self._bt.last_error or "Bluetooth unavailable"
                else:
                    # Keep paired list fresh while searching nearby.
                    try:
                        paired = self._bt.list_devices()
                        with self._lock:
                            connected_id = self.state["bluetooth"].get("connected_id")
                            by_id = {d["id"]: d for d in paired}
                            if connected_id:
                                for d in self._devices:
                                    if d.get("id") == connected_id and connected_id not in by_id:
                                        by_id[connected_id] = d
                            self._devices = list(by_id.values())
                    except Exception as exc:  # noqa: BLE001
                        print(f"[app] paired refresh during scan: {exc}")

                    nearby = self._bt.discover_nearby(progress_cb=_on_nearby)
                    if self._bt.last_error and not nearby:
                        error = self._bt.last_error
                    elif not nearby:
                        error = (
                            "No nearby phones found. Put the phone in pairing mode, "
                            "keep it close, then Scan again."
                        )
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                print("[app] scan_bluetooth failed:", exc)
            finally:
                with self._lock:
                    self._nearby = nearby or self._bt.get_partial_nearby()
                    self.state["bluetooth"]["scanning"] = False
                    self.state["bluetooth"]["discovering"] = False
                    self.state["bluetooth"]["error"] = error
                    self._last_paired_refresh = time.monotonic()

        threading.Thread(target=_work, daemon=True, name="bt-scan").start()
        return snap

    def discover_nearby(self) -> dict[str, Any]:
        """Alias — UI Scan button uses scan_bluetooth for nearby discovery."""
        return self.scan_bluetooth()

    def pair_device(self, device_id: str) -> dict[str, Any]:
        """Pair a nearby phone (CustomPairing CONFIRM_ONLY), then connect it straight away.

        One tap: Pair -> confirm on the phone -> the app finds the phone's audio connection and
        connects. (Before: Pair often had to be tapped twice and Connect was a third step.)
        """
        with self._lock:
            if self.state["bluetooth"].get("pairing_id") or self.state["bluetooth"].get("connecting_id"):
                return self._snapshot()
            self.state["bluetooth"]["pairing_id"] = device_id
            self.state["bluetooth"]["pair_stage"] = "confirm"
            self.state["bluetooth"]["error"] = None
            snap = self._snapshot()
        # Pairing is much slower while a nearby scan is still running.
        self._bt.cancel_discovery()

        def _work() -> None:
            error: str | None = None
            alert: str | None = None
            apc_id: str | None = None
            paired_ok = False
            try:
                if not self._bt.available:
                    error = self._bt.last_error or "Bluetooth unavailable"
                else:
                    result = self._bt.pair_device(device_id)
                    paired_ok = bool(result.get("ok"))
                    if not paired_ok:
                        error = result.get("error") or "Pairing wasn't confirmed, try again."
                        if result.get("alert") or str(result.get("status", "")).upper() in (
                            "FAILED",
                            "19",
                        ):
                            alert = error
                    else:
                        with self._lock:
                            self.state["bluetooth"]["pair_stage"] = "audio"
                            self.state["bluetooth"]["error"] = "Paired - getting the audio connection ready…"
                        apc_id = result.get("apc_id") or self._bt.resolve_apc_id(result.get("name"))
                        devices = self._bt.list_devices()
                        with self._lock:
                            self._devices = devices
                            self._nearby = [d for d in self._nearby if d.get("id") != device_id]
                            self._last_paired_refresh = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                print("[app] pair_device failed:", exc)
            finally:
                with self._lock:
                    self.state["bluetooth"]["pairing_id"] = None
                    self.state["bluetooth"]["pair_stage"] = None
                    self.state["bluetooth"]["error"] = error
                    self.state["bluetooth"]["alert"] = alert
                    if paired_ok and not error and not apc_id:
                        self.state["bluetooth"]["error"] = (
                            "Paired - tap Connect on the phone below."
                        )
            if paired_ok and apc_id:
                print(f"[app] paired - connecting {apc_id[-24:]}…")
                self.connect_device(apc_id)

        threading.Thread(target=_work, daemon=True, name="bt-pair").start()
        return snap

    def clear_bluetooth_alert(self) -> dict[str, Any]:
        with self._lock:
            self.state["bluetooth"]["alert"] = None
            return self._snapshot()

    def open_bluetooth_settings(self) -> dict[str, Any]:
        """Optional fallback: open Windows Bluetooth Settings."""
        if not self._bt.available:
            with self._lock:
                self.state["bluetooth"]["error"] = self._bt.last_error or "Bluetooth unavailable"
                return self._snapshot()
        result = self._bt.open_bluetooth_settings()
        with self._lock:
            if not result.get("ok"):
                self.state["bluetooth"]["error"] = result.get("error") or "Could not open Settings"
            else:
                self.state["bluetooth"]["error"] = (
                    "Opened Windows Bluetooth (fallback). Prefer Find nearby + Pair in the app."
                )
            return self._snapshot()

    def connect_device(self, device_id: str) -> dict[str, Any]:
        """Open the phone's AudioPlaybackConnection; Live then starts by itself."""
        with self._lock:
            if self.state["bluetooth"].get("connecting_id"):
                return self._snapshot()
            bt = self.state["bluetooth"]
            # One phone at a time: a second phone would also play into CABLE (two songs mixed).
            previous = [i for i in (bt.get("connected_id"), bt.get("waiting_id")) if i and i != device_id]
            bt["connecting_id"] = device_id
            bt["error"] = None
            snap = self._snapshot()
        self._bt.cancel_discovery()

        def _work() -> None:
            for old in previous:
                print(f"[app] switching phones - closing {old[-24:]}…")
                try:
                    self._bt.disconnect(old, forget=False)
                except Exception as exc:  # noqa: BLE001
                    print(f"[app] close previous phone: {exc}")
            with self._lock:
                bt = self.state["bluetooth"]
                for old in previous:
                    if bt.get("connected_id") == old:
                        bt["connected_id"] = None
                    if bt.get("waiting_id") == old:
                        bt["waiting_id"] = None
                        bt["waiting_kind"] = None
                    for d in self._devices:
                        if d.get("id") == old:
                            d["profile"] = "Paired - tap Connect"
                            d["audio_ready"] = False

            # Phone audio goes to the Windows default playback device; it must be CABLE Input
            # or the app hears nothing. This used to fail silently.
            route: dict[str, Any]
            try:
                from windows_cable import ensure_cable_default_playback

                route = ensure_cable_default_playback()
            except Exception as exc:  # noqa: BLE001
                route = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if not route.get("ok"):
                msg = (
                    "Audio routing failed: "
                    f"{route.get('error') or 'could not select CABLE Input'} "
                    "Check that VB-Audio Virtual Cable is installed, then tap Connect again."
                )
                print(f"[app] {msg}")
                with self._lock:
                    self.state["bluetooth"]["connecting_id"] = None
                    self.state["bluetooth"]["error"] = msg
                return

            connected = False
            waiting = False
            error: str | None = None
            resolved_id = device_id
            if self._bt.available:
                result = self._bt.connect(device_id)
                connected = bool(result.get("ok"))
                waiting = bool(result.get("waiting")) and not connected
                resolved_id = str(result.get("device_id") or device_id)
                if not connected and not waiting:
                    error = result.get("error") or f"Connect failed ({result.get('status') or 'unknown'})"
            else:
                error = self._bt.last_error or "Bluetooth unavailable"

            with self._lock:
                self.state["bluetooth"]["connecting_id"] = None
                if connected:
                    self.state["bluetooth"]["connected_id"] = resolved_id
                    self._mark_device_connected_locked(resolved_id, device_id)
                elif not waiting:
                    self.state["bluetooth"]["error"] = error
                else:
                    for d in self._devices:
                        if d.get("id") == device_id:
                            d["id"] = resolved_id
            if connected:
                self._bt_status_errors = 0
                self._bt_idle_since = None
                print("[app] Bluetooth connected - starting Live")
                try:
                    self.start_live()
                except Exception as exc:  # noqa: BLE001
                    print(f"[app] BT connect auto-start Live failed: {exc}")
            elif waiting:
                print("[app] phone hasn't accepted the audio link yet - waiting")
                self._start_bt_waiting(resolved_id, "accept")

        threading.Thread(target=_work, daemon=True, name="bt-connect").start()
        return snap

    def disconnect_device(self) -> dict[str, Any]:
        """Close audio link only — keeps Windows pairing. Also cancels 'Reconnecting…'."""
        self.stop_live()
        with self._lock:
            bt = self.state["bluetooth"]
            ids = [i for i in (bt.get("connected_id"), bt.get("waiting_id")) if i]
        if self._bt.available:
            for did in ids:
                self._bt.disconnect(did, forget=False)
        with self._lock:
            bt = self.state["bluetooth"]
            bt["connected_id"] = None
            bt["connecting_id"] = None
            bt["waiting_id"] = None
            bt["waiting_kind"] = None
            bt["error"] = None
            self.state["live_paused"] = False
            for d in self._devices:
                if d.get("id") in ids:
                    d["profile"] = "Paired - tap Connect"
                    d["audio_ready"] = False
            self._bt_idle_since = None
            return self._snapshot()

    def forget_device(self, device_id: str | None = None) -> dict[str, Any]:
        """Disconnect if needed, then real Windows unpair for one device."""
        with self._lock:
            if self.state["bluetooth"].get("forgetting"):
                return self._snapshot()
            did = device_id or self.state["bluetooth"].get("connected_id")
            if not did:
                self.state["bluetooth"]["error"] = "No device to forget."
                return self._snapshot()
            self.state["bluetooth"]["forgetting"] = True
            self.state["bluetooth"]["error"] = None
            connected = self.state["bluetooth"].get("connected_id")
            snap = self._snapshot()

        if connected and connected == did:
            self.stop_live()

        def _work() -> None:
            error: str | None = None
            ok = False
            try:
                if self._bt.available:
                    result = self._bt.forget_device(did)
                    ok = bool(result.get("ok"))
                    if not ok:
                        error = result.get("error") or "Forget failed"
                else:
                    error = self._bt.last_error or "Bluetooth unavailable"
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                print("[app] forget_device failed:", exc)
            finally:
                with self._lock:
                    self.state["bluetooth"]["forgetting"] = False
                    if connected == did:
                        self.state["bluetooth"]["connected_id"] = None
                        self.state["bluetooth"]["connecting_id"] = None
                    if self.state["bluetooth"].get("waiting_id") == did:
                        self.state["bluetooth"]["waiting_id"] = None
                        self.state["bluetooth"]["waiting_kind"] = None
                    self._devices = [d for d in self._devices if d.get("id") != did]
                    self._nearby = [d for d in self._nearby if d.get("id") != did]
                    self.state["bluetooth"]["error"] = (
                        error if not ok else "Device forgotten from Windows."
                    )
                    self._last_paired_refresh = 0.0
                self._schedule_paired_refresh(force=True)

        threading.Thread(target=_work, daemon=True, name="bt-forget").start()
        return snap

    def forget_all_devices(self) -> dict[str, Any]:
        """Real Windows unpair for all paired Bluetooth devices."""
        with self._lock:
            if self.state["bluetooth"].get("forgetting"):
                return self._snapshot()
            self.state["bluetooth"]["forgetting"] = True
            self.state["bluetooth"]["error"] = None
            snap = self._snapshot()

        self.stop_live()

        def _work() -> None:
            error: str | None = None
            msg = "All Bluetooth devices forgotten from Windows."
            try:
                if self._bt.available:
                    result = self._bt.forget_all()
                    n = int(result.get("forgotten") or 0)
                    total = int(result.get("total") or 0)
                    msg = f"Forgot {n} of {total} paired Bluetooth device(s) from Windows."
                    if result.get("errors"):
                        error = msg + " Some failed — check console."
                    else:
                        error = msg
                else:
                    error = self._bt.last_error or "Bluetooth unavailable"
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                print("[app] forget_all failed:", exc)
            finally:
                with self._lock:
                    self.state["bluetooth"]["forgetting"] = False
                    self.state["bluetooth"]["connected_id"] = None
                    self.state["bluetooth"]["connecting_id"] = None
                    self.state["bluetooth"]["waiting_id"] = None
                    self.state["bluetooth"]["waiting_kind"] = None
                    self._devices = []
                    self._nearby = []
                    self.state["bluetooth"]["error"] = error
                    self._last_paired_refresh = 0.0
                self._schedule_paired_refresh(force=True)

        threading.Thread(target=_work, daemon=True, name="bt-forget-all").start()
        return snap

    # ----------------------------------------- Software update (Settings) --
    def update_check(self) -> dict[str, Any]:
        self._updater.check()
        return self.get_state()

    def update_install(self) -> dict[str, Any]:
        self._updater.install()
        return self.get_state()

    def update_cancel(self) -> dict[str, Any]:
        self._updater.cancel()
        return self.get_state()

    def clear_update_notice(self) -> dict[str, Any]:
        self._update_notice = None
        return self.get_state()

    def _quit_for_update(self) -> None:
        """The apply script is waiting for this process to exit (called from a worker thread)."""
        self._maybe_save_settings(force=True)
        try:
            self._engine.stop_live()
        except Exception as exc:  # noqa: BLE001
            print(f"[update] stop engine: {exc}")
        try:
            if self._bt.available:
                self._bt.disconnect(forget=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[update] bt disconnect: {exc}")
        # If a background thread keeps the process alive, exit anyway.
        killer = threading.Timer(8.0, lambda: os._exit(0))
        killer.daemon = True
        killer.start()
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception as exc:  # noqa: BLE001
            print(f"[update] close window: {exc}")
            os._exit(0)

    # ---------------------------------------------------------- Bluetooth name --
    def set_bluetooth_name(self) -> dict[str, Any]:
        """Rename the tablet to "MuVi" (phones show this name). Needs the admin prompt."""
        result = device_name.request_rename()
        print(f"[app] Bluetooth name -> {device_name.TARGET_NAME}: {result}")
        self._device_name_msg = (
            None if result.get("ok") else result.get("error") or "Could not rename the tablet."
        )
        self._device_name_at = 0.0  # re-read soon (the rename takes a few seconds)
        return self.get_state()

    def restart_tablet(self) -> dict[str, Any]:
        """Restart Windows to apply the new Bluetooth name."""
        print("[app] restarting the tablet for the new Bluetooth name")
        self._maybe_save_settings(force=True)
        try:
            self._engine.stop_live()
            if self._bt.available:
                self._bt.disconnect(forget=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[app] before restart: {exc}")
        result = device_name.restart_tablet()
        if not result.get("ok"):
            self._device_name_msg = result.get("error") or "Could not restart. Restart the tablet by hand."
        return self.get_state()

    # ------------------------------------------------------------------- Wi-Fi --
    def wifi_scan(self) -> dict[str, Any]:
        self._wifi.scan()
        return self.get_state()

    def wifi_connect(self, ssid: str, password: str | None = None) -> dict[str, Any]:
        self._wifi.connect(str(ssid or ""), password or None)
        return self.get_state()

    def wifi_set_radio(self, on: bool) -> dict[str, Any]:
        """Wi-Fi on/off switch (Wi-Fi screen). Bluetooth is not affected.

        While a phone is pairing, connecting or connected, Wi-Fi stays off: turning it on is
        refused (the UI asks the user to disconnect the phone first)."""
        with self._lock:
            if on and self._phone_active_locked():
                return self._snapshot()
            if on:
                # The user wants it on now: no pending automatic restore needed any more.
                self._set_wifi_off_for_bt(False)
                self._wifi_restore_at = None
        self._wifi.set_radio(bool(on))
        with self._lock:
            return self._snapshot()

    # ------------------------------------------------- Wi-Fi off during Bluetooth --
    def _phone_active_locked(self) -> bool:
        bt = self.state["bluetooth"]
        return bool(
            bt.get("connected_id")
            or bt.get("connecting_id")
            or bt.get("pairing_id")
            or bt.get("waiting_id")  # waiting for / reconnecting a phone
        )

    def _set_wifi_off_for_bt(self, value: bool) -> None:
        self._wifi_off_for_bt = value
        path = _wifi_flag_path()
        try:
            if value:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(str(os.getpid()))
            elif os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            print(f"[app] could not update {path}: {exc}")

    def _apply_wifi_policy(self) -> None:
        """Runs on every poll; only acts when something changed.

        Phone active and Wi-Fi on  -> switch Wi-Fi off (also if it was turned on in Windows
                                      quick settings meanwhile).
        Phone gone and the app had turned Wi-Fi off -> switch it back on after the grace period.
        """
        now = time.monotonic()
        with self._lock:
            phone = self._phone_active_locked()
        w = self._wifi.state
        if not w.get("available") or w.get("radio_switching"):
            return
        radio_on = w.get("radio_on")  # True / False / None (not known yet)

        if phone:
            self._wifi_restore_at = None
            if radio_on is True and now >= self._wifi_policy_try_at:
                self._wifi_policy_try_at = now + WIFI_RETRY_S
                with self._lock:
                    self._set_wifi_off_for_bt(True)
                print("[app] phone using Bluetooth - switching Wi-Fi off")
                self._wifi.set_radio(False)
            return

        if not self._wifi_off_for_bt:
            return
        if radio_on is True:          # already back on (e.g. by the user in Windows)
            with self._lock:
                self._set_wifi_off_for_bt(False)
            self._wifi_restore_at = None
            return
        if radio_on is None:          # state not read yet after start-up
            return
        if self._wifi_restore_at is None:
            self._wifi_restore_at = now + WIFI_RESTORE_DELAY_S
            return
        if now >= self._wifi_restore_at and now >= self._wifi_policy_try_at:
            # Keep the marker until the radio is really on (cleared above), so a refused
            # attempt is retried every WIFI_RETRY_S.
            self._wifi_policy_try_at = now + WIFI_RETRY_S
            print("[app] no phone on Bluetooth any more - switching Wi-Fi back on")
            self._wifi.set_radio(True)

    def wifi_disconnect(self) -> dict[str, Any]:
        self._wifi.disconnect()
        return self.get_state()


def main() -> None:
    # Packaged app has no console: keep a log in C:\\kiosk_launcher\\updates\\app.log.
    from app_log import open_log_file

    log = open_log_file("app.log")
    if log is not None:
        sys.stdout = sys.stderr = log
    AppWatchdog.start_if_needed()   # kiosk session only: restart the app if it closes/crashes
    api = Api()
    window = webview.create_window(
        "muve",
        url=os.path.join(WEB_DIR, "index.html"),
        js_api=api,
        width=1920,
        height=1080,
        min_size=(1280, 720),
        fullscreen=FULLSCREEN,
        background_color="#0b0b0f",
    )
    api._window = window

    def _on_loaded() -> None:
        # The new version is up - tells the update script not to roll back.
        UpdateManager.mark_started()

    window.events.loaded += _on_loaded

    def _on_closing() -> None:
        api._maybe_save_settings(force=True)
        api._engine.stop_live()
        if api._bt.available:
            api._bt.disconnect(forget=False)

    window.events.closing += _on_closing
    webview.start()


if __name__ == "__main__":
    main()