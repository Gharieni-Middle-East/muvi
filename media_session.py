"""Now Playing metadata + transport.

Primary: Windows SMTC when available.
Fallback: media keys (prev/next) + Live elapsed timer in the app UI.

Requires on Windows (same Python that runs main.py):
  python -m pip install winrt-runtime winrt-Windows.Media.Control ^
      winrt-Windows.Foundation.Collections winrt-Windows.Foundation
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import traceback
from datetime import timedelta
from typing import Any

_IMPORT_ERROR: str | None = None
_COLLECTIONS_OK = False
try:  # pragma: no cover - Windows-only
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus,
    )

    _WINRT_OK = True
except Exception as exc:  # noqa: BLE001
    _WINRT_OK = False
    _IMPORT_ERROR = str(exc)

if _WINRT_OK:
    try:
        import winrt.windows.foundation.collections  # noqa: F401

        _COLLECTIONS_OK = True
    except Exception as exc:  # noqa: BLE001
        _COLLECTIONS_OK = False
        print(
            f"[media_session] collections not installed ({exc}) — "
            "using get_current_session only"
        )


def _pip_hint() -> str:
    return (
        f'"{sys.executable}" -m pip install '
        "winrt-runtime winrt-Windows.Media.Control "
        "winrt-Windows.Foundation winrt-Windows.Foundation.Collections"
    )


def _seconds(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, timedelta):
        return max(0.0, float(value.total_seconds()))
    duration = getattr(value, "duration", None)
    if duration is not None:
        try:
            return max(0.0, float(duration) / 10_000_000.0)
        except (TypeError, ValueError):
            pass
    try:
        return max(0.0, float(value) / 10_000_000.0)
    except (TypeError, ValueError):
        return 0.0


def _send_media_key(vk: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        user32 = __import__("ctypes").windll.user32
        KEYEVENTF_EXTENDEDKEY = 0x0001
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[media_session] media key 0x{vk:02X} failed: {exc}")
        return False


VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3


class MediaSessionService:
    """Read SMTC when fully installed; never crash the UI poll loop."""

    def __init__(self) -> None:
        # SMTC works with get_current_session even without Foundation.Collections.
        self.available = _WINRT_OK and sys.platform == "win32"
        self._smtc_usable = self.available
        self.last_error: str | None = None if self._smtc_usable else (
            _IMPORT_ERROR or "SMTC packages incomplete"
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._manager: Any = None
        self._cache: dict[str, Any] = self._empty()
        self._cache_at = 0.0
        self._logged_empty = False
        self._disabled_reason: str | None = None

        if self._smtc_usable:
            self._start_loop()
            mode = "full" if _COLLECTIONS_OK else "current-session-only"
            print(f"[media_session] ready (SMTC {mode})")
        else:
            print(f"[media_session] SMTC unavailable: {self.last_error}")
            print(f"[media_session] run: {_pip_hint()}")
            print("[media_session] prev/next use Windows media keys when on Windows")

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "ok": False,
            "title": None,
            "artist": None,
            "album": None,
            "position": 0.0,
            "duration": 0.0,
            "playing": False,
            "can_seek": False,
            "can_prev": True,
            "can_next": True,
            "source": None,
            "via": None,
        }

    def _disable_smtc(self, reason: str) -> None:
        if self._disabled_reason == reason:
            return
        self._disabled_reason = reason
        self._smtc_usable = False
        self.last_error = reason
        print(f"[media_session] disabling SMTC: {reason}")
        print(f"[media_session] run: {_pip_hint()}")

    def _start_loop(self) -> None:
        self._loop = asyncio.new_event_loop()

        def _run_loop() -> None:
            asyncio.set_event_loop(self._loop)
            assert self._loop is not None
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run_loop, daemon=True, name="media-smtc")
        self._thread.start()

    def _run(self, coro: Any, timeout: float = 8.0) -> Any:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    async def _ensure_manager(self) -> Any:
        if self._manager is None:
            self._manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
            print("[media_session] SMTC manager acquired")
        return self._manager

    async def _list_sessions(self, manager: Any) -> list[Any]:
        """Use get_current_session; only call get_sessions if Collections is installed."""
        sessions: list[Any] = []
        current = None
        try:
            current = manager.get_current_session()
        except Exception as exc:  # noqa: BLE001
            print(f"[media_session] get_current_session failed: {exc}")

        if _COLLECTIONS_OK:
            try:
                raw = manager.get_sessions()
                sessions = list(raw or [])
            except ModuleNotFoundError as exc:
                print(f"[media_session] get_sessions skipped: {exc}")
                sessions = []
            except Exception as exc:  # noqa: BLE001
                print(f"[media_session] get_sessions failed: {exc}")
                sessions = []

        if not sessions and current is not None:
            sessions = [current]
        return sessions

    async def _pick_session(self) -> Any | None:
        manager = await self._ensure_manager()
        sessions = await self._list_sessions(manager)

        if not sessions:
            if not self._logged_empty:
                print(
                    "[media_session] no SMTC sessions "
                    "(normal for phone→PC Bluetooth — use Live timer + Prev/Next)"
                )
                self._logged_empty = True
            return None

        self._logged_empty = False
        scored: list[tuple[int, Any]] = []
        for session in sessions:
            score = 0
            try:
                playback = session.get_playback_info()
                if playback is not None and (
                    playback.playback_status
                    == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
                ):
                    score += 50
            except Exception:  # noqa: BLE001
                pass
            try:
                props = await session.try_get_media_properties_async()
                if props is not None:
                    if str(props.title or "").strip():
                        score += 30
                    if str(props.artist or "").strip():
                        score += 10
            except Exception:  # noqa: BLE001
                pass
            scored.append((score, session))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    async def _read_session(self, session: Any) -> dict[str, Any]:
        info: dict[str, Any] = self._empty()
        info["ok"] = True
        info["via"] = "smtc"
        info["source"] = str(getattr(session, "source_app_user_model_id", "") or "") or None

        try:
            props = await session.try_get_media_properties_async()
            if props is not None:
                info["title"] = str(props.title or "").strip() or None
                info["artist"] = str(props.artist or "").strip() or None
                info["album"] = str(props.album_title or "").strip() or None
        except Exception:  # noqa: BLE001
            traceback.print_exc()

        playing = False
        rate = 1.0
        try:
            playback = session.get_playback_info()
            if playback is not None:
                playing = (
                    playback.playback_status
                    == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
                )
                try:
                    rate = float(playback.playback_rate or 1.0) or 1.0
                except (TypeError, ValueError):
                    rate = 1.0
                controls = getattr(playback, "controls", None)
                if controls is not None:
                    info["can_seek"] = bool(
                        getattr(controls, "is_playback_position_enabled", False)
                    )
            info["playing"] = playing
        except Exception:  # noqa: BLE001
            traceback.print_exc()

        try:
            timeline = session.get_timeline_properties()
            if timeline is not None:
                position = _seconds(timeline.position)
                start = _seconds(timeline.start_time)
                end = _seconds(timeline.end_time)
                last_updated = timeline.last_updated_time
                last_sec = None
                if last_updated is not None:
                    try:
                        last_sec = float(last_updated.timestamp())
                    except Exception:  # noqa: BLE001
                        last_sec = None
                if playing and last_sec is not None:
                    position = position + max(0.0, time.time() - last_sec) * rate
                duration = max(0.0, end - start) or max(0.0, end)
                info["position"] = min(position, duration) if duration > 0 else position
                info["duration"] = duration
                if duration > 0:
                    info["can_seek"] = True
        except Exception:  # noqa: BLE001
            traceback.print_exc()

        return info

    async def _read(self) -> dict[str, Any]:
        session = await self._pick_session()
        if session is None:
            return self._empty()
        return await self._read_session(session)

    def get_now_playing(self, *, max_age: float = 0.4) -> dict[str, Any]:
        if not self._smtc_usable:
            return self._empty()

        now = time.monotonic()
        if now - self._cache_at < max_age:
            cached = dict(self._cache)
            if cached.get("playing") and cached.get("duration", 0) > 0:
                cached["position"] = min(
                    float(cached.get("duration", 0)),
                    float(cached.get("position", 0)) + (now - self._cache_at),
                )
            return cached
        try:
            info = self._run(self._read())
            self._cache = info
            self._cache_at = now
            self.last_error = None
            return dict(info)
        except ModuleNotFoundError as exc:
            self._disable_smtc(str(exc))
            return self._empty()
        except Exception as exc:  # noqa: BLE001
            # One-line log; do not dump traceback every poll.
            msg = f"{type(exc).__name__}: {exc}"
            if self.last_error != msg:
                print(f"[media_session] read failed: {msg}")
                self.last_error = msg
            if "foundation.collections" in str(exc).lower():
                self._disable_smtc(str(exc))
            return self._empty()

    def seek(self, seconds: float) -> dict[str, Any]:
        if not self._smtc_usable:
            return {"ok": False, "error": "No media timeline"}
        try:
            ok = self._run(self._seek(float(seconds)))
            self._cache_at = 0.0
            if ok:
                self._cache["position"] = float(seconds)
            return {"ok": bool(ok)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    async def _seek(self, seconds: float) -> bool:
        session = await self._pick_session()
        if session is None:
            return False
        ticks = int(max(0.0, seconds) * 10_000_000)
        return bool(await session.try_change_playback_position_async(ticks))

    def previous(self) -> dict[str, Any]:
        return self._transport("previous")

    def next(self) -> dict[str, Any]:
        return self._transport("next")

    def play_pause(self) -> dict[str, Any]:
        """Play / pause the music on the connected phone (AVRCP via Windows)."""
        return self._transport("play_pause")

    def _transport(self, action: str) -> dict[str, Any]:
        if self._smtc_usable:
            try:
                if bool(self._run(self._do_transport(action))):
                    self._cache_at = 0.0
                    return {"ok": True, "via": "smtc"}
            except Exception as exc:  # noqa: BLE001
                print(f"[media_session] SMTC {action} failed: {exc}")
        print(f"[media_session] {action}: no Windows media session - sending media key")

        vk = {
            "previous": VK_MEDIA_PREV_TRACK,
            "next": VK_MEDIA_NEXT_TRACK,
            "play_pause": VK_MEDIA_PLAY_PAUSE,
        }[action]
        key_ok = _send_media_key(vk)
        return {"ok": key_ok, "via": "media_key" if key_ok else None}

    async def _do_transport(self, action: str) -> bool:
        session = await self._pick_session()
        if session is None:
            return False
        if action == "previous":
            return bool(await session.try_skip_previous_async())
        if action == "next":
            return bool(await session.try_skip_next_async())
        if action == "play_pause":
            return bool(await session.try_toggle_play_pause_async())
        return False
