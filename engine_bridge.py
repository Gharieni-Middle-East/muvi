"""Start Live bridge — spawns engine_worker in a clean process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import Any


class EngineBridge:
    def __init__(self) -> None:
        self.available = True
        self.last_error: str | None = None
        self.capture_name: str | None = None
        self.output_index: int | None = None
        self.output_name: str | None = None
        self.output_layout: str | None = None
        self.vibration_output_index: int | None = None
        self.audio_output_index: int | None = None
        self._running = False
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._worker_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "engine_worker.py"
        )

    @property
    def running(self) -> bool:
        return self._running

    def _ensure_worker(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if getattr(sys, "frozen", False):
            # PyInstaller build: sys.executable is main.exe; re-exec with a flag
            # that main.py intercepts and routes to engine_worker.main().
            # Do NOT spawn main.py as a script here — that would start another UI.
            cmd = [sys.executable, "--engine-worker"]
            stderr = subprocess.DEVNULL  # windowed exe has no console to inherit
        else:
            # Dev: spawn engine_worker.py directly (keeps WinRT out of the audio process).
            cmd = [sys.executable, "-u", self._worker_path]
            stderr = None  # inherit — show worker logs in the same console
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            cwd=os.path.dirname(self._worker_path),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        ready = self._read()
        if not ready.get("ok"):
            raise RuntimeError(ready.get("error") or "engine worker failed to start")
        print("[engine_bridge] worker ready (separate process, no WinRT)")

    def _read(self) -> dict[str, Any]:
        """Read next JSON line from worker. Skip log noise if any leaks to stdout."""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:
                code = self._proc.poll()
                raise RuntimeError(f"engine worker exited (code={code})")
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                print(f"[engine_bridge] worker stdout (ignored): {line}")
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                print(f"[engine_bridge] bad JSON (ignored): {line[:120]}")
                continue

    @staticmethod
    def _friendly_start_error(err: str) -> str:
        low = err.lower()
        if "worker exited" in low or "readline" in low or "nonetype" in low:
            return (
                "Audio device did not respond in time (timed out). "
                "Unplug/replug the Gigaport(s), then try again. "
                f"({err})"
            )
        return err

    def _force_kill_proc(self) -> None:
        """Hard-kill the worker without nulling self._proc.

        Used by the start watchdog: killing the process makes the pending
        readline() return EOF so _read() raises instead of hanging forever.
        """
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def _send(self, msg: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        self._ensure_worker()
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()
        if timeout is None:
            return self._read()
        # Watchdog: if the worker stalls opening a device, kill it so the UI
        # gets a clear error instead of an endless "loading" spinner.
        watchdog = threading.Timer(timeout, self._force_kill_proc)
        watchdog.daemon = True
        watchdog.start()
        try:
            return self._read()
        finally:
            watchdog.cancel()

    def start_live(
        self,
        volume: float = 0.70,
        vibration: float = 0.27,
        cutoff_hz: float = 200.0,
        highpass_hz: float | None = None,
        prefer_bluetooth: bool = False,
        speaker_route: str = "headphones",
        vibration_mode: str = "haptics",
        vibration_output_index: int | None = None,
        audio_output_index: int | None = None,
        roles_flipped: bool = False,
        zone_enabled: dict[str, bool] | None = None,
        audio_muted: bool = False,
        vibration_muted: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return {
                    "ok": True,
                    "capture": self.capture_name,
                    "output_index": self.output_index,
                    "output_name": self.output_name,
                }
            try:
                # Fresh worker each Start Live for a clean audio backend state.
                self._kill_worker()
                if highpass_hz is not None:
                    cutoff_hz = float(highpass_hz)
                result = self._send(
                    {
                        "cmd": "start",
                        "volume": float(volume),
                        "vibration": float(vibration),
                        "cutoff_hz": float(cutoff_hz),
                        "prefer_bluetooth": bool(prefer_bluetooth),
                        "speaker_route": str(speaker_route),
                        "vibration_mode": str(vibration_mode),
                        "vibration_output_index": vibration_output_index,
                        "audio_output_index": audio_output_index,
                        "roles_flipped": bool(roles_flipped),
                        "zone_enabled": zone_enabled,
                        "audio_muted": audio_muted,
                        "vibration_muted": vibration_muted,
                    },
                    timeout=20.0,
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self._running = False
                self._kill_worker()
                return {"ok": False, "error": self._friendly_start_error(str(exc))}

            if not result.get("ok"):
                err = str(result.get("error") or "start failed")
                self.last_error = err
                self._running = False
                return {"ok": False, "error": err}

            self.capture_name = result.get("capture")
            self.output_index = result.get("output_index")
            self.output_name = result.get("output_name")
            self.output_layout = result.get("output_layout")
            self.vibration_output_index = vibration_output_index
            self.audio_output_index = audio_output_index
            self._running = True
            self.last_error = None
            mode = result.get("mode")
            print(
                f"[engine_bridge] START LIVE ok — mode={mode} "
                f"capture={self.capture_name!r} output={self.output_name}"
            )
            return {
                "ok": True,
                "capture": self.capture_name,
                "output_index": self.output_index,
                "output_name": self.output_name,
                "mode": mode,
                "output_layout": result.get("output_layout"),
                "audio_output_channels": result.get("audio_output_channels"),
                "speaker_route": result.get("speaker_route"),
                "vibration_mode": result.get("vibration_mode"),
            }

    def start_demo(
        self,
        volume: float = 0.70,
        vibration: float = 0.27,
        cutoff_hz: float = 200.0,
        highpass_hz: float | None = None,
        path: str | None = None,
        loop: bool = True,
        vibration_mode: str = "haptics",
        vibration_output_index: int | None = None,
        audio_output_index: int | None = None,
        roles_flipped: bool = False,
        zone_enabled: dict[str, bool] | None = None,
        audio_muted: bool = False,
        vibration_muted: bool = False,
    ) -> dict[str, Any]:
        """Play assets/demo.wav through the same Gigaport vibration path."""
        with self._lock:
            try:
                self._kill_worker()
                if highpass_hz is not None:
                    cutoff_hz = float(highpass_hz)
                msg: dict[str, Any] = {
                    "cmd": "start_demo",
                    "volume": float(volume),
                    "vibration": float(vibration),
                    "cutoff_hz": float(cutoff_hz),
                    "vibration_mode": str(vibration_mode),
                    "vibration_output_index": vibration_output_index,
                    "audio_output_index": audio_output_index,
                    "roles_flipped": bool(roles_flipped),
                    "loop": bool(loop),
                    "zone_enabled": zone_enabled,
                    "audio_muted": audio_muted,
                    "vibration_muted": vibration_muted,
                }
                if path:
                    msg["path"] = path
                result = self._send(msg, timeout=20.0)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self._running = False
                self._kill_worker()
                return {"ok": False, "error": self._friendly_start_error(str(exc))}

            if not result.get("ok"):
                err = str(result.get("error") or "demo start failed")
                self.last_error = err
                self._running = False
                return {"ok": False, "error": err}

            self.capture_name = result.get("capture")
            self.output_index = result.get("output_index")
            self.output_name = result.get("output_name")
            self.output_layout = result.get("output_layout")
            self.vibration_output_index = vibration_output_index
            self.audio_output_index = audio_output_index
            self._running = True
            self.last_error = None
            print(
                f"[engine_bridge] START DEMO ok — file={self.capture_name!r} "
                f"output={self.output_name}"
            )
            return {
                "ok": True,
                "capture": self.capture_name,
                "output_index": self.output_index,
                "output_name": self.output_name,
                "mode": "demo",
                "output_layout": result.get("output_layout"),
                "audio_output_channels": result.get("audio_output_channels"),
                "vibration_mode": result.get("vibration_mode"),
                "duration": result.get("duration"),
                "title": result.get("title"),
                "artist": result.get("artist"),
                "album": result.get("album"),
                "path": result.get("path"),
            }

    def get_demo_progress(self) -> dict[str, float]:
        with self._lock:
            if not self._running:
                return {"position": 0.0, "duration": 0.0, "fraction": 0.0}
            try:
                result = self._send({"cmd": "demo_progress"})
                return {
                    "position": float(result.get("position") or 0.0),
                    "duration": float(result.get("duration") or 0.0),
                    "fraction": float(result.get("fraction") or 0.0),
                }
            except Exception:  # noqa: BLE001
                return {"position": 0.0, "duration": 0.0, "fraction": 0.0}

    def seek_demo(self, seconds: float) -> dict[str, float]:
        with self._lock:
            if not self._running:
                return {"position": 0.0, "duration": 0.0, "fraction": 0.0}
            try:
                result = self._send({"cmd": "seek_demo", "seconds": float(seconds)})
                return {
                    "position": float(result.get("position") or 0.0),
                    "duration": float(result.get("duration") or 0.0),
                    "fraction": float(result.get("fraction") or 0.0),
                }
            except Exception:  # noqa: BLE001
                return {"position": 0.0, "duration": 0.0, "fraction": 0.0}

    def start(self) -> dict[str, Any]:
        return self.start_live()

    def stop_live(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._send({"cmd": "stop"})
                except Exception:  # noqa: BLE001
                    pass
            self._kill_worker()
            self._running = False
            print("[engine_bridge] STOP LIVE")

    def stop(self) -> None:
        self.stop_live()

    def _kill_worker(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                proc.stdin.flush()
                proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def set_volume(self, value: float) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                self._send({"cmd": "set_volume", "value": float(value)})
            except Exception:  # noqa: BLE001
                pass

    def set_audio_muted(self, muted: bool) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                self._send({"cmd": "set_audio_muted", "muted": bool(muted)})
            except Exception:  # noqa: BLE001
                pass

    def set_vibration(self, value: float) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                self._send({"cmd": "set_vibration", "value": float(value)})
            except Exception:  # noqa: BLE001
                pass

    def set_vibration_muted(self, muted: bool) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                self._send({"cmd": "set_vibration_muted", "muted": bool(muted)})
            except Exception:  # noqa: BLE001
                pass

    def set_zone_enabled(self, zone: str, enabled: bool) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                self._send({
                    "cmd": "set_zone_enabled",
                    "zone": str(zone),
                    "enabled": bool(enabled),
                })
            except Exception:  # noqa: BLE001
                pass

    def set_highpass_hz(self, value: float) -> None:
        # Back-compat: UI used to call this; now it means bass cutoff (low-pass).
        self.set_cutoff_hz(value)

    def set_cutoff_hz(self, value: float) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                self._send({"cmd": "set_cutoff_hz", "value": float(value)})
            except Exception:  # noqa: BLE001
                pass

    def set_speaker_route(self, route: str) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                self._send({"cmd": "set_speaker_route", "route": str(route)})
            except Exception:  # noqa: BLE001
                pass

    def set_vibration_mode(self, mode: str) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                self._send({"cmd": "set_vibration_mode", "mode": str(mode)})
            except Exception:  # noqa: BLE001
                pass

    def get_audio_settings(
        self,
        *,
        output_layout: str | None = None,
        vibration_output_index: int | None = None,
        audio_output_index: int | None = None,
        output_name: str | None = None,
        engine_error: str | None = None,
        roles_flipped: bool = False,
    ) -> dict[str, Any]:
        # Only talk to the worker when it's already running — and under the
        # same lock as every other _send so a concurrent Settings poll can never
        # interleave with Start Live / Demo on the shared stdin/stdout pipe.
        with self._lock:
            worker_alive = (
                self._running
                and self._proc is not None
                and self._proc.poll() is None
            )
            if worker_alive:
                try:
                    result = self._send(
                        {
                            "cmd": "audio_settings",
                            "output_layout": output_layout,
                            "vibration_output_index": vibration_output_index,
                            "audio_output_index": audio_output_index,
                            "output_name": output_name,
                            "engine_error": engine_error,
                            "roles_flipped": bool(roles_flipped),
                        },
                        timeout=8.0,
                    )
                    if result.get("ok"):
                        return result
                except Exception:  # noqa: BLE001
                    pass
        # Idle (or worker query failed): enumerate in-process. No worker spawn.
        try:
            from audio.output_devices import build_audio_settings

            return build_audio_settings(
                active=self._running,
                output_layout=output_layout,
                vibration_index=vibration_output_index,
                audio_index=audio_output_index,
                output_name=output_name,
                engine_error=engine_error,
                roles_flipped=roles_flipped,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc), "devices": []}

    def get_runtime_stats(self) -> dict[str, float]:
        with self._lock:
            if not self._running:
                return {}
            try:
                result = self._send({"cmd": "stats"})
                stats = result.get("stats") or {}
                return {k: float(v) for k, v in stats.items()}
            except Exception:  # noqa: BLE001
                return {}
