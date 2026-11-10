"""Bluetooth audio receiver — APC Connect + in-app CustomPairing for new phones.

Paired path (unchanged, same as Audio Playback Connector):
  DeviceWatcher(AudioPlaybackConnection.GetDeviceSelector())
  → TryCreateFromId → StartAsync → OpenAsync

New-phone path (in-app, no Windows Settings):
  AssociationEndpoint DeviceWatcher (classic inquiry)
  → DeviceInformationCustomPairing with CONFIRM_ONLY
  → then use the paired APC Connect flow above

Requires (Windows):
    pip install winrt-Windows.Media.Audio winrt-Windows.Devices.Enumeration \\
                winrt-Windows.Devices.Radios winrt-Windows.Foundation \\
                winrt-Windows.Foundation.Collections winrt-Windows.System
"""

from __future__ import annotations

import asyncio
import sys
import threading
import traceback
from typing import Any, Callable

_IMPORT_ERROR: str | None = None
_RADIOS_OK = False
_LAUNCHER_OK = False
_PAIRING_OK = False
try:  # pragma: no cover - Windows-only import
    from winrt.windows.devices.enumeration import (
        DeviceInformation,
        DeviceInformationKind,
        DevicePairingKinds,
        DevicePairingResultStatus,
    )
    from winrt.windows.media.audio import (
        AudioPlaybackConnection,
        AudioPlaybackConnectionOpenResultStatus,
        AudioPlaybackConnectionState,
    )

    _WINRT_OK = True
    _PAIRING_OK = True
except Exception as exc:  # noqa: BLE001
    _WINRT_OK = False
    _IMPORT_ERROR = str(exc)
    AudioPlaybackConnectionState = None  # type: ignore[misc, assignment]
    DeviceInformationKind = None  # type: ignore[misc, assignment]
    DevicePairingKinds = None  # type: ignore[misc, assignment]
    DevicePairingResultStatus = None  # type: ignore[misc, assignment]

# Classic / LE Bluetooth Association Endpoint selectors.
# DevObjectType 5 = AssociationEndpoint. Unpaired inquiry AQS from Windows guidance:
# IsPaired=False OR IssueInquiry=True forces radio discovery of unpaired devices.
_BT_CLASSIC = "{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}"
_BT_LE = "{bb7bb05e-5972-42b5-94fc-76eaa7084d49}"
_NEARBY_UNPAIRED_CLASSIC = (
    "System.Devices.DevObjectType:=5 "
    f'AND System.Devices.Aep.ProtocolId:="{_BT_CLASSIC}" '
    "AND (System.Devices.Aep.IsPaired:=System.StructuredQueryType.Boolean#False "
    "OR System.Devices.Aep.Bluetooth.IssueInquiry:=System.StructuredQueryType.Boolean#True)"
)
_NEARBY_UNPAIRED_BLE = (
    "System.Devices.DevObjectType:=5 "
    f'AND System.Devices.Aep.ProtocolId:="{_BT_LE}" '
    "AND (System.Devices.Aep.IsPaired:=System.StructuredQueryType.Boolean#False "
    "OR System.Devices.Aep.Bluetooth.IssueInquiry:=System.StructuredQueryType.Boolean#True)"
)
_NEARBY_ALL_BT = (
    "System.Devices.DevObjectType:=5 AND ("
    f'System.Devices.Aep.ProtocolId:="{_BT_CLASSIC}" OR '
    f'System.Devices.Aep.ProtocolId:="{_BT_LE}"'
    ")"
)
_NEARBY_PROPERTIES = [
    "System.Devices.Aep.DeviceAddress",
    "System.Devices.Aep.IsConnected",
    "System.Devices.Aep.IsPaired",
    "System.Devices.Aep.CanPair",
    "System.Devices.Aep.SignalStrength",
    "System.Devices.Aep.IsPresent",
    "System.Devices.Aep.Bluetooth.Le.IsConnectable",
]

_BT_DEVICE_OK = False
try:  # pragma: no cover — unpaired selector helper
    from winrt.windows.devices.bluetooth import BluetoothDevice, BluetoothLEDevice

    _BT_DEVICE_OK = True
except Exception as exc:  # noqa: BLE001
    _BT_DEVICE_OK = False
    BluetoothDevice = None  # type: ignore[misc, assignment]
    BluetoothLEDevice = None  # type: ignore[misc, assignment]
    print(f"[bt_audio] Devices.Bluetooth optional import skipped: {exc}")

try:  # pragma: no cover
    from winrt.windows.foundation.collections import IVectorView  # noqa: F401
    from winrt.windows.devices.radios import Radio, RadioKind, RadioState

    _RADIOS_OK = True
except Exception as exc:  # noqa: BLE001
    _RADIOS_OK = False
    print(f"[bt_audio] radios optional import skipped: {exc}")

try:  # pragma: no cover — open Windows Bluetooth Settings
    from winrt.windows.foundation import Uri
    from winrt.windows.system import Launcher

    _LAUNCHER_OK = True
except Exception as exc:  # noqa: BLE001
    _LAUNCHER_OK = False
    print(f"[bt_audio] Launcher optional import skipped: {exc}")


class BluetoothAudioService:
    """APC Connect for paired phones + CustomPairing for nearby unpaired phones."""

    def __init__(self) -> None:
        self.available = _WINRT_OK and sys.platform == "win32"
        self.last_error: str | None = None if self.available else (
            _IMPORT_ERROR or "AudioPlaybackConnection requires Windows"
        )
        self._connections: dict[str, Any] = {}
        self._started: set[str] = set()
        self._opened_ids: set[str] = set()
        self._dropped_ids: set[str] = set()
        self._state_tokens: dict[str, Any] = {}
        self._state_handlers: dict[str, Any] = {}
        self._last_radio_on: bool | None = None
        self._device_meta: dict[str, dict[str, Any]] = {}
        self._nearby_meta: dict[str, dict[str, Any]] = {}
        self._partial_devices: list[dict[str, Any]] = []
        self._partial_nearby: list[dict[str, Any]] = []
        self._progress_cb: Callable[[list[dict[str, Any]]], None] | None = None
        self._nearby_progress_cb: Callable[[list[dict[str, Any]]], None] | None = None
        self._watcher_handlers: list[Any] = []
        self._pairing_handlers: list[Any] = []
        # Set to stop a running nearby scan early (Bluetooth inquiry disturbs a live audio link).
        self._discovery_cancel = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        if self.available:
            self._start_loop()

    # -------------------------------------------------------- async plumbing --
    def _start_loop(self) -> None:
        def _run_loop() -> None:
            assert self._loop is not None
            try:
                from winrt.runtime import ApartmentType, init_apartment

                init_apartment(ApartmentType.MTA)
                print("[bt_audio] COM apartment: MTA")
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] COM apartment init skipped: {exc}")
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=_run_loop, daemon=True, name="bt-winrt")
        self._thread.start()

    def _run(self, coro: Any, timeout: float = 20.0) -> Any:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    # --------------------------------------------------------------- devices --
    def list_devices(self, progress_cb: Callable[[list[dict[str, Any]]], None] | None = None) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            self.last_error = None
            self._progress_cb = progress_cb
            self._partial_devices = []
            return self._run(self._list_devices(), timeout=12.0)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            print("[bt_audio] list_devices failed:")
            traceback.print_exc()
            return list(self._partial_devices)
        finally:
            self._progress_cb = None

    def get_partial_devices(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self._partial_devices]

    def get_partial_nearby(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self._partial_nearby]

    def discover_nearby(
        self, progress_cb: Callable[[list[dict[str, Any]]], None] | None = None
    ) -> list[dict[str, Any]]:
        """Scan unpaired AssociationEndpoint devices for the in-app Pair UI."""
        if not self.available:
            return []
        try:
            self.last_error = None
            self._nearby_progress_cb = progress_cb
            self._partial_nearby = []
            self._discovery_cancel.clear()
            return self._run(self._discover_nearby(), timeout=50.0)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            print("[bt_audio] discover_nearby failed:")
            traceback.print_exc()
            return list(self._partial_nearby)
        finally:
            self._nearby_progress_cb = None

    def cancel_discovery(self) -> None:
        """Stop a running nearby scan at its next 1 s step (before pairing / connecting)."""
        self._discovery_cancel.set()

    def resolve_apc_id(self, name: str | None) -> str | None:
        """Audio (APC) id of a just-paired phone, found by name. Can take ~5-25 s after pairing."""
        if not self.available or not name:
            return None
        try:
            return self._run(self._resolve_apc_id_by_name(name), timeout=35.0)
        except Exception as exc:  # noqa: BLE001
            print(f"[bt_audio] resolve_apc_id failed: {type(exc).__name__}: {exc}")
            return None

    def pair_device(self, device_id: str) -> dict[str, Any]:
        """CustomPairing (CONFIRM_ONLY) for an unpaired AEP device (~30s timeout)."""
        if not self.available:
            return {"ok": False, "error": self.last_error or "unsupported"}
        if not _PAIRING_OK or DevicePairingKinds is None:
            return {
                "ok": False,
                "error": (
                    "CustomPairing unavailable — upgrade winrt-Windows.Devices.Enumeration "
                    "(need DevicePairingKinds / DeviceInformation.pairing.custom)."
                ),
            }
        try:
            return self._run(self._pair_device(device_id), timeout=45.0)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            print("[bt_audio] pair_device failed:")
            traceback.print_exc()
            return {"ok": False, "error": self.last_error}

    def open_bluetooth_settings(self) -> dict[str, Any]:
        """Optional fallback: open Windows Bluetooth settings."""
        if not self.available:
            return {"ok": False, "error": self.last_error or "unsupported"}
        if not _LAUNCHER_OK:
            return {
                "ok": False,
                "error": "Install winrt-Windows.System to open Bluetooth Settings",
            }
        try:
            self._run(self._open_bluetooth_settings(), timeout=8.0)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            print("[bt_audio] open_bluetooth_settings failed:")
            traceback.print_exc()
            return {"ok": False, "error": str(exc)}

    async def _open_bluetooth_settings(self) -> None:
        uri = Uri("ms-settings:bluetooth")
        ok = await Launcher.launch_uri_async(uri)
        print(f"[bt_audio] launched ms-settings:bluetooth → {ok}")

    def _emit_progress(self, devices: list[dict[str, Any]]) -> None:
        self._partial_devices = list(devices)
        cb = self._progress_cb
        if cb is None:
            return
        try:
            cb([dict(d) for d in devices])
        except Exception as exc:  # noqa: BLE001
            print(f"[bt_audio] progress_cb failed: {exc}")

    def _emit_nearby_progress(self, devices: list[dict[str, Any]]) -> None:
        self._partial_nearby = list(devices)
        cb = self._nearby_progress_cb
        if cb is None:
            return
        try:
            cb([dict(d) for d in devices])
        except Exception as exc:  # noqa: BLE001
            print(f"[bt_audio] nearby progress_cb failed: {exc}")

    @staticmethod
    def _apc_selector() -> str:
        return str(AudioPlaybackConnection.get_device_selector())

    def _association_endpoint_kind(self) -> Any | None:
        if DeviceInformationKind is None:
            return None
        for name in ("ASSOCIATION_ENDPOINT", "AssociationEndpoint"):
            val = getattr(DeviceInformationKind, name, None)
            if val is not None:
                return val
        try:
            return DeviceInformationKind(5)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _unbox_property(value: Any) -> Any:
        """DeviceInformation.Properties is an IMapView<str, IInspectable> — values
        come back as opaque boxed objects, not native Python types. Cast to
        IPropertyValue and call the getter matching its declared type. Without
        this, e.g. DeviceAddress prints as "<_winrt.Object object at 0x...>" and
        any truthy boxed object (like IsPaired) evaluates as True regardless of
        its real value — which is what caused unpaired devices to show as paired.
        """
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        try:
            from winrt.windows.foundation import IPropertyValue, PropertyType

            pv = value.as_(IPropertyValue) if hasattr(value, "as_") else None
            if pv is None:
                return None
            t = pv.type
            getter_name = {
                getattr(PropertyType, "STRING", object()): "get_string",
                getattr(PropertyType, "BOOLEAN", object()): "get_boolean",
                getattr(PropertyType, "UINT8", object()): "get_uint8",
                getattr(PropertyType, "UINT16", object()): "get_uint16",
                getattr(PropertyType, "UINT32", object()): "get_uint32",
                getattr(PropertyType, "UINT64", object()): "get_uint64",
                getattr(PropertyType, "INT16", object()): "get_int16",
                getattr(PropertyType, "INT32", object()): "get_int32",
                getattr(PropertyType, "INT64", object()): "get_int64",
                getattr(PropertyType, "DOUBLE", object()): "get_double",
                getattr(PropertyType, "SINGLE", object()): "get_single",
            }.get(t)
            if getter_name is None:
                # Unknown/complex type (e.g. array) — don't leak a raw object repr.
                return None
            getter = getattr(pv, getter_name, None)
            return getter() if getter is not None else None
        except Exception as exc:  # noqa: BLE001
            print(f"[bt_audio] property unbox failed: {type(exc).__name__}: {exc}")
            return None

    @classmethod
    def _prop(cls, info: Any, key: str, default: Any = None) -> Any:
        try:
            props = getattr(info, "properties", None)
            if props is None:
                return default
            raw = None
            found = False
            if key in props:
                raw = props[key]
                found = True
            else:
                getter = getattr(props, "get", None) or getattr(props, "lookup", None)
                if getter is not None:
                    try:
                        raw = getter(key)
                        found = True
                    except Exception:  # noqa: BLE001
                        return default
            if not found:
                return default
            unboxed = cls._unbox_property(raw)
            return unboxed if unboxed is not None else default
        except Exception:  # noqa: BLE001
            return default

    async def _bluetooth_radio_on(self) -> bool | None:
        if not _RADIOS_OK:
            return None
        try:
            radios = await Radio.get_radios_async()
            for radio in radios:
                if radio.kind == RadioKind.BLUETOOTH:
                    on = radio.state == RadioState.ON
                    if self._last_radio_on is not on:
                        print(f"[bt_audio] Bluetooth radio: {radio.state}")
                        self._last_radio_on = on
                    return on
        except Exception as exc:  # noqa: BLE001
            print(f"[bt_audio] radio check failed: {type(exc).__name__}: {exc}")
        return None

    def _info_to_device(self, info: Any) -> dict[str, Any] | None:
        try:
            device_id = str(info.id)
            name = str(info.name).strip() if info.name else "Bluetooth device"
            return {
                "id": device_id,
                "name": name,
                "kind": "phone",
                "profile": "Audio ready — tap Connect",
                "paired": True,
                "nearby": False,
                "audio_ready": True,
                "address": None,
                "rssi": None,
                "source": "apc",
            }
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return None

    def _info_to_nearby(self, info: Any) -> dict[str, Any] | None:
        try:
            device_id = str(info.id)
            name = str(info.name).strip() if info.name else ""
            address = self._prop(info, "System.Devices.Aep.DeviceAddress")
            # Log every hit so empty UI is diagnosable from the console.
            print(
                f"[bt_audio] nearby candidate id=…{device_id[-28:]} "
                f"name={name!r} addr={address!r} kind={getattr(info, 'kind', '?')}"
            )
            if not name and not address:
                # Completely anonymous inquiry ghost — keep with a placeholder so
                # the list is not empty when only address-less hits appear.
                name = "Unknown Bluetooth device"
            elif not name:
                name = f"Bluetooth {str(address)[-5:]}"

            paired = self._prop(info, "System.Devices.Aep.IsPaired")
            if paired is None:
                paired = bool(getattr(getattr(info, "pairing", None), "is_paired", False))
            can_pair = self._prop(info, "System.Devices.Aep.CanPair")
            if can_pair is None:
                can_pair = bool(getattr(getattr(info, "pairing", None), "can_pair", True))
            # Still show already-paired so discovery isn't a blank wall.
            if paired:
                profile = "Already paired — use Scan above, then Connect"
                can_pair = False
            elif can_pair:
                profile = "Nearby — tap Pair"
            else:
                profile = "Nearby (may not support pairing)"

            rssi = self._prop(info, "System.Devices.Aep.SignalStrength")
            kind = "phone"
            lower = name.lower()
            if any(
                t in lower
                for t in ("watch", "buds", "headphone", "headset", "speaker", "mouse", "keyboard")
            ):
                kind = "other"
            return {
                "id": device_id,
                "name": name,
                "kind": kind,
                "profile": profile,
                "paired": bool(paired),
                "nearby": True,
                "can_pair": bool(can_pair) and not bool(paired),
                "audio_ready": False,
                "address": str(address) if address else None,
                "rssi": int(rssi) if isinstance(rssi, (int, float)) else None,
                "source": "nearby",
            }
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return None

    def _create_aep_watcher(self, selector: str, props: list[str], kind: Any) -> Any | None:
        # Confirmed via check_watcher_api.py (getattr, not dir): this build exposes
        # create_watcher_with_kind_aqs_filter_and_additional_properties, and the
        # argument order matches the name literally — kind FIRST. Calling it as
        # (selector, props, kind) — kind last — is the bug that made this silently
        # return zero devices; keeping that order as a fallback only, in case a
        # future winrt build changes the order back.
        attempts = (
            ("create_watcher_with_kind_aqs_filter_and_additional_properties", (kind, selector, props)),
            ("create_watcher_with_kind_aqs_filter_and_additional_properties", (selector, props, kind)),
            ("create_watcher_aqs_filter_and_additional_properties_and_kind", (selector, props, kind)),
        )
        for name, args in attempts:
            fn = getattr(DeviceInformation, name, None)
            if fn is None:
                print(f"[bt_audio] {name}: missing")
                continue
            try:
                w = fn(*args)
                print(f"[bt_audio] OK AEP DeviceWatcher via {name}{args}")
                return w
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] {name}{args} failed: {type(exc).__name__}: {exc}")
        return None

    async def _enumerate_aep_watcher(
        self,
        selector: str,
        *,
        kind: Any,
        timeout: float,
        label: str,
    ) -> list[Any]:
        found: dict[str, Any] = {}
        tokens: list[tuple[str, Any]] = []
        # Keep handlers on self for the whole watch — pywinrt can GC locals.
        handlers: list[Any] = []

        def on_added(_sender: Any, info: Any) -> None:
            try:
                found[str(info.id)] = info
                print(f"[bt_audio] {label} + {getattr(info, 'name', '')!r}")
            except Exception:  # noqa: BLE001
                traceback.print_exc()

        def on_updated(_sender: Any, update: Any) -> None:
            try:
                existing = found.get(str(update.id))
                if existing is not None and hasattr(existing, "update"):
                    existing.update(update)
                    print(f"[bt_audio] {label} ~ updated {str(update.id)[-20:]}")
            except Exception:  # noqa: BLE001
                pass

        def on_enum_completed(sender: Any, _args: Any) -> None:
            try:
                print(
                    f"[bt_audio] {label}: EnumerationCompleted "
                    f"status={getattr(sender, 'status', '?')} found={len(found)}"
                )
            except Exception:  # noqa: BLE001
                print(f"[bt_audio] {label}: EnumerationCompleted found={len(found)}")

        print(f"[bt_audio] {label} selector={selector[:140]}…")
        watcher = self._create_aep_watcher(selector, list(_NEARBY_PROPERTIES), kind)
        if watcher is None:
            print(f"[bt_audio] {label}: AssociationEndpoint watcher unavailable — try find_all")
            try:
                fn = getattr(
                    DeviceInformation,
                    "find_all_async_with_kind_aqs_filter_and_additional_properties",
                    None,
                )
                if fn is not None:
                    infos = await fn(selector, list(_NEARBY_PROPERTIES), kind)
                    out = list(infos) if infos is not None else []
                    print(f"[bt_audio] {label} find_all kind → {len(out)}")
                    return out
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] {label} find_all kind failed: {exc}")
            return []

        handlers.extend([on_added, on_updated, on_enum_completed])
        self._watcher_handlers = handlers
        tokens.append(("added", watcher.add_added(on_added)))
        if hasattr(watcher, "add_updated"):
            tokens.append(("updated", watcher.add_updated(on_updated)))
        if hasattr(watcher, "add_enumeration_completed"):
            tokens.append(
                ("enumeration_completed", watcher.add_enumeration_completed(on_enum_completed))
            )

        try:
            watcher.start()
            try:
                st0 = getattr(watcher, "status", "?")
            except Exception:  # noqa: BLE001
                st0 = "?"
            print(f"[bt_audio] {label}: watching {timeout:.0f}s… status={st0}")
            elapsed = 0.0
            step = 1.0
            while elapsed < timeout:
                if self._discovery_cancel.is_set():
                    print(f"[bt_audio] {label}: cancelled")
                    break
                await asyncio.sleep(step)
                elapsed += step
                if int(elapsed) % 4 == 0:
                    try:
                        st = getattr(watcher, "status", "?")
                    except Exception:  # noqa: BLE001
                        st = "?"
                    print(f"[bt_audio] {label}: t={elapsed:.0f}s status={st} found={len(found)}")
            try:
                watcher.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] {label} stop: {exc}")
            await asyncio.sleep(0.35)
        finally:
            for event_name, token in tokens:
                try:
                    getattr(watcher, f"remove_{event_name}")(token)
                except Exception:  # noqa: BLE001
                    pass
            self._watcher_handlers = []

        print(f"[bt_audio] {label}: collected {len(found)} raw device(s)")
        return list(found.values())

    def _nearby_selectors(self) -> list[tuple[str, str, float, Any]]:
        """Build (label, selector, timeout, kind) passes for nearby discovery."""
        kind = self._association_endpoint_kind()
        container = None
        if DeviceInformationKind is not None:
            container = getattr(DeviceInformationKind, "ASSOCIATION_ENDPOINT_CONTAINER", None)
            if container is None:
                try:
                    container = DeviceInformationKind(6)
                except Exception:  # noqa: BLE001
                    container = None

        passes: list[tuple[str, str, float, Any]] = []
        # 1) Official unpaired classic selector (best for phones in pairing mode).
        if _BT_DEVICE_OK and BluetoothDevice is not None:
            try:
                sel = BluetoothDevice.get_device_selector_from_pairing_state(False)
                print(f"[bt_audio] BluetoothDevice unpaired selector: {sel[:160]}…")
                passes.append(("bt_unpaired", str(sel), 15.0, kind))
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] BluetoothDevice selector failed: {exc}")
        # (No Bluetooth LE pass: phones stream music over classic Bluetooth, and an LE scan
        # only adds 8 s of radio time that disturbs a live audio link.)

        # 2) Explicit classic unpaired+inquiry AQS (StackOverflow / MS pattern).
        passes.append(("classic_inquiry", _NEARBY_UNPAIRED_CLASSIC, 12.0, kind))
        # 3) Broad classic+BLE AEP (catches devices inquiry might skip).
        passes.append(("all_bt_aep", _NEARBY_ALL_BT, 6.0, kind))
        # 4) Endpoint containers sometimes surface discoverable phones better.
        if container is not None:
            passes.append(("aep_container", _NEARBY_ALL_BT, 5.0, container))
        return [(lab, sel, t, k) for lab, sel, t, k in passes if k is not None]

    async def _discover_nearby(self) -> list[dict[str, Any]]:
        radio_on = await self._bluetooth_radio_on()
        if radio_on is False:
            self.last_error = "Bluetooth is off. Turn it on, then Find nearby again."
            print("[bt_audio] radio off — nearby empty")
            return []

        kind = self._association_endpoint_kind()
        if kind is None:
            self.last_error = (
                "Nearby discovery unavailable (DeviceInformationKind.ASSOCIATION_ENDPOINT missing)."
            )
            return []

        devices: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_addrs: set[str] = set()

        def ingest(infos: list[Any], batch_label: str = "") -> None:
            before = len(devices)
            skipped_disabled = 0
            skipped_no_dev = 0
            skipped_dupe = 0
            for info in infos:
                # NOTE: IsEnabled is documented as only meaningful for
                # DeviceInformationKind.DeviceInterface — for AssociationEndpoint
                # devices (what we enumerate here) it's not a reliable signal and
                # was observed wiping out every device in a batch. Don't filter on it.
                dev = self._info_to_nearby(info)
                if not dev:
                    skipped_no_dev += 1
                    continue
                addr = (dev.get("address") or "").lower()
                if dev["id"] in seen_ids or (addr and addr in seen_addrs):
                    skipped_dupe += 1
                    continue
                seen_ids.add(dev["id"])
                if addr:
                    seen_addrs.add(addr)
                devices.append(dev)
            added = len(devices) - before
            print(
                f"[bt_audio] ingest[{batch_label}]: {len(infos)} raw → +{added} added "
                f"(no_dev={skipped_no_dev} dupe={skipped_dupe} disabled_skipped={skipped_disabled})"
            )
            self._emit_nearby_progress(devices)

        passes = self._nearby_selectors()
        print(f"[bt_audio] nearby discovery starting ({len(passes)} passes)")
        # Prefer the first strong unpaired inquiry pass alone (~15s) before falling
        # through — stacking everything takes too long when the first works.
        for label, selector, timeout, aep_kind in passes:
            if self._discovery_cancel.is_set():
                print("[bt_audio] nearby discovery cancelled")
                break
            if devices and label in ("all_bt_aep", "aep_container"):
                # Already have hits — skip broad fallbacks.
                continue
            try:
                infos = await self._enumerate_aep_watcher(
                    selector, kind=aep_kind, timeout=timeout, label=label
                )
                ingest(infos, batch_label=label)
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] {label} failed: {type(exc).__name__}: {exc}")
                traceback.print_exc()
            # If the first unpaired inquiry found devices, stop early.
            if devices and label in ("bt_unpaired", "classic_inquiry"):
                print(f"[bt_audio] early-stop after {label} with {len(devices)} device(s)")
                break

        # Prefer unpaired / pairable at top of the list.
        devices.sort(
            key=lambda d: (
                0 if d.get("can_pair") else 1,
                0 if not d.get("paired") else 1,
                str(d.get("name", "")).lower(),
            )
        )
        self._nearby_meta = {d["id"]: d for d in devices}
        print(f"[bt_audio] nearby list total={len(devices)}")
        if not devices:
            self.last_error = (
                "No nearby phones found. Put the phone in pairing / discoverable mode "
                "(Bluetooth settings open on the phone), keep it near the tablet, then Find nearby again."
            )
        else:
            self.last_error = None
        return devices

    async def _pair_device(self, device_id: str) -> dict[str, Any]:
        radio_on = await self._bluetooth_radio_on()
        if radio_on is False:
            return {"ok": False, "error": "Bluetooth is off. Turn it on and try again."}

        try:
            info = await DeviceInformation.create_from_id_async(device_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Device not found: {exc}"}
        if info is None:
            return {"ok": False, "error": "Device not found"}

        name = str(info.name).strip() if info.name else (
            (self._nearby_meta.get(device_id) or {}).get("name") or "Bluetooth device"
        )
        pairing = getattr(info, "pairing", None)
        if pairing is None:
            return {"ok": False, "error": "This device does not support pairing from the app"}

        if bool(getattr(pairing, "is_paired", False)):
            print(f"[bt_audio] already paired: {name!r}")
            return {
                "ok": True,
                "already_paired": True,
                "device_id": device_id,
                "name": name,
                "status": "ALREADY_PAIRED",
            }

        custom = getattr(pairing, "custom", None)
        if custom is None:
            return {
                "ok": False,
                "error": (
                    "DeviceInformationCustomPairing not available on this WinRT build. "
                    "Upgrade winrt-Windows.Devices.Enumeration."
                ),
            }

        token = None
        handler_holder: list[Any] = []

        def on_pairing_requested(_sender: Any, args: Any) -> None:
            try:
                kind = args.pairing_kind
                kind_name = _status_name(kind)
                print(f"[bt_audio] PairingRequested kind={kind_name}")
                if DevicePairingKinds is not None and kind == DevicePairingKinds.CONFIRM_ONLY:
                    args.accept()
                    print("[bt_audio] pairing accepted (CONFIRM_ONLY)")
                    return
                # Pin / password ceremonies are unexpected for phone A2DP — reject gracefully.
                print(f"[bt_audio] WARNING: unsupported pairing kind {kind_name} — not accepting")
            except Exception:  # noqa: BLE001
                traceback.print_exc()

        handler_holder.append(on_pairing_requested)
        self._pairing_handlers = handler_holder
        try:
            token = custom.add_pairing_requested(on_pairing_requested)
            print(f"[bt_audio] pairing {name!r} (CONFIRM_ONLY, timeout 30s)…")
            result = await asyncio.wait_for(
                custom.pair_async(DevicePairingKinds.CONFIRM_ONLY),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": "Pairing timed out. Confirm on the phone and try again.",
                "status": "TIMEOUT",
                "name": name,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Pairing failed: {exc}", "name": name}
        finally:
            if token is not None and hasattr(custom, "remove_pairing_requested"):
                try:
                    custom.remove_pairing_requested(token)
                except Exception:  # noqa: BLE001
                    pass
            self._pairing_handlers = []

        status = getattr(result, "status", None)
        status_name = _status_name(status)
        ok_statuses = set()
        if DevicePairingResultStatus is not None:
            for n in ("PAIRED", "ALREADY_PAIRED"):
                val = getattr(DevicePairingResultStatus, n, None)
                if val is not None:
                    ok_statuses.add(val)
        paired = status in ok_statuses if ok_statuses else status_name in ("PAIRED", "ALREADY_PAIRED")
        print(f"[bt_audio] pair result status={status_name} ok={paired}")

        if not paired:
            # status 19 = FAILED — often leftover pairing on the phone.
            is_failed = (
                status_name in ("FAILED", "19")
                or (DevicePairingResultStatus is not None
                    and status == getattr(DevicePairingResultStatus, "FAILED", None))
                or str(status).endswith(".FAILED")
                or str(getattr(status, "value", "")) == "19"
            )
            forget_phone_msg = (
                "Pairing failed (status 19).\n\n"
                "This phone was likely paired with this tablet before.\n"
                "On the phone: open Bluetooth settings → forget / unpair this PC "
                "(or tablet), then put the phone in pairing mode and try Pair again."
            )
            friendly = {
                "PAIRING_CANCELED": "Pairing wasn't confirmed. Accept on the phone / Windows prompt, then try again.",
                "AUTHENTICATION_TIMEOUT": "Pairing wasn't confirmed in time. Try again.",
                "AUTHENTICATION_FAILURE": "Pairing wasn't confirmed. Try again.",
                "AUTHENTICATION_NOT_ALLOWED": "Pairing wasn't allowed. Try again from the phone side.",
                "REJECTED_BY_HANDLER": "This phone asked for a PIN we don't support yet. Try another phone or pair from Windows once.",
                "REQUIRED_HANDLER_NOT_REGISTERED": "Pairing handler missing — restart the app and try again.",
                "NOT_READY_TO_PAIR": "Phone isn't ready to pair. Put it in pairing mode, then try again.",
                "FAILED": forget_phone_msg,
                "19": forget_phone_msg,
            }.get(status_name, forget_phone_msg if is_failed else f"Pairing wasn't confirmed ({status_name}). Try again.")
            return {
                "ok": False,
                "error": friendly,
                "status": status_name if status_name != "19" else "FAILED",
                "name": name,
                "alert": is_failed or status_name in ("FAILED", "19"),
            }

        # Return straight away: looking up the phone's audio (APC) id takes another 5-25 s and
        # used to run inside the 45 s pairing timeout, so a successful pairing was reported as
        # failed and the user had to tap Pair twice. The app now calls resolve_apc_id() itself.
        return {
            "ok": True,
            "already_paired": status_name == "ALREADY_PAIRED",
            "device_id": device_id,
            "paired_aep_id": device_id,
            "apc_id": None,
            "name": name,
            "status": status_name,
        }

    async def _resolve_apc_id_by_name(self, name: str | None) -> str | None:
        if not name:
            return None
        for attempt in range(5):
            try:
                infos = await self._enumerate_apc_watcher(timeout=3.0)
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] APC resolve after pair failed: {exc}")
                infos = []
            exact: list[tuple[str, str]] = []
            loose: list[tuple[str, str]] = []
            for info in infos:
                info_name = str(info.name).strip() if info.name else ""
                if self._norm_name(info_name) == self._norm_name(name) and info_name:
                    exact.append((str(info.id), info_name))
                elif self._names_match(info_name, name):
                    loose.append((str(info.id), info_name))
            # Exact name first. A partial match ("iPhone" vs "Ali's iPhone") only when it is the
            # only candidate, so a second paired phone with a similar name is never picked.
            pick = exact[0] if exact else (loose[0] if len(loose) == 1 else None)
            if pick is not None:
                print(f"[bt_audio] resolved APC id for {name!r} → {pick[1]!r}")
                return pick[0]
            if len(loose) > 1:
                print(f"[bt_audio] {len(loose)} phones look like {name!r} — not guessing")
            await asyncio.sleep(1.0 + attempt * 0.4)
        print(f"[bt_audio] no APC device yet for {name!r} — user should Scan")
        return None

    @staticmethod
    def _norm_name(a: str) -> str:
        return "".join(ch for ch in (a or "").lower() if ch.isalnum())

    @staticmethod
    def _names_match(a: str, b: str) -> bool:
        aa = "".join(ch for ch in a.lower() if ch.isalnum())
        bb = "".join(ch for ch in b.lower() if ch.isalnum())
        if not aa or not bb:
            return False
        return aa == bb or aa in bb or bb in aa

    async def _enumerate_apc_watcher(self, timeout: float = 4.0) -> list[Any]:
        """DeviceWatcher(AudioPlaybackConnection.GetDeviceSelector()) — same as APC."""
        selector = self._apc_selector()

        found: dict[str, Any] = {}
        tokens: list[tuple[str, Any]] = []
        handlers: list[Any] = []
        done = asyncio.Event()

        def on_added(_sender: Any, info: Any) -> None:
            try:
                found[str(info.id)] = info
            except Exception:  # noqa: BLE001
                traceback.print_exc()

        def on_removed(_sender: Any, update: Any) -> None:
            try:
                found.pop(str(update.id), None)
            except Exception:  # noqa: BLE001
                pass

        def on_enum_completed(_sender: Any, _args: Any) -> None:
            try:
                done.set()
            except Exception:  # noqa: BLE001
                pass

        # create_watcher_aqs_filter(selector) — Microsoft sample uses the 1-arg CreateWatcher.
        watcher = None
        for name in (
            "create_watcher_aqs_filter",
            "create_watcher",
        ):
            fn = getattr(DeviceInformation, name, None)
            if fn is None:
                continue
            try:
                watcher = fn(selector)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] {name} failed: {type(exc).__name__}: {exc}")

        if watcher is None:
            print("[bt_audio] falling back to find_all_async_aqs_filter for APC")
            try:
                infos = await DeviceInformation.find_all_async_aqs_filter(selector)
                return list(infos)
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] APC find_all failed: {exc}")
                return []

        handlers.extend([on_added, on_removed, on_enum_completed])
        self._watcher_handlers = handlers

        tokens.append(("added", watcher.add_added(on_added)))
        if hasattr(watcher, "add_removed"):
            tokens.append(("removed", watcher.add_removed(on_removed)))
        if hasattr(watcher, "add_enumeration_completed"):
            tokens.append(
                ("enumeration_completed", watcher.add_enumeration_completed(on_enum_completed))
            )

        try:
            watcher.start()
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            # Brief linger for late Added events.
            await asyncio.sleep(0.4)
            try:
                watcher.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] apc stop: {exc}")
            await asyncio.sleep(0.2)
        finally:
            for event_name, token in tokens:
                try:
                    getattr(watcher, f"remove_{event_name}")(token)
                except Exception:  # noqa: BLE001
                    pass
            self._watcher_handlers = []

        return list(found.values())

    async def _list_devices(self) -> list[dict[str, Any]]:
        radio_on = await self._bluetooth_radio_on()
        if radio_on is False:
            self.last_error = "Bluetooth is off. Turn it on, then Scan again."
            print("[bt_audio] radio off — returning empty device list")
            return []

        infos = await self._enumerate_apc_watcher(timeout=5.0)
        devices: list[dict[str, Any]] = []
        for info in infos:
            if getattr(info, "is_enabled", None) is False:
                continue
            dev = self._info_to_device(info)
            if dev:
                devices.append(dev)

        devices.sort(key=lambda d: str(d.get("name", "")).lower())
        self._device_meta = {d["id"]: d for d in devices}
        self._emit_progress(devices)
        if not devices:
            self.last_error = (
                "No paired audio phones found. "
                "Tap Find nearby to pair a phone in the app, then Scan again."
            )
        return devices

    # -------------------------------------------------------------- connect ---
    def connect(self, device_id: str) -> dict[str, Any]:
        if not self.available:
            return {"ok": False, "error": self.last_error or "unsupported"}
        try:
            return self._run(self._connect(device_id), timeout=90.0)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            print("[bt_audio] connect failed:")
            traceback.print_exc()
            return {"ok": False, "error": self.last_error}

    async def _connect(self, device_id: str) -> dict[str, Any]:
        """TryCreateFromId → StartAsync → OpenAsync (same as APC)."""
        self._dropped_ids.discard(device_id)
        meta = self._device_meta.get(device_id) or {}
        name = meta.get("name")

        if not name:
            try:
                info = await DeviceInformation.create_from_id_async(device_id)
                name = str(info.name) if info and info.name else None
            except Exception:  # noqa: BLE001
                pass

        conn = self._connections.get(device_id)
        if conn is None:
            conn = AudioPlaybackConnection.try_create_from_id(device_id)
            if conn is None and name:
                # After AEP CustomPairing the APC id often differs — match by name.
                apc_id = await self._resolve_apc_id_by_name(name)
                if apc_id:
                    device_id = apc_id
                    conn = AudioPlaybackConnection.try_create_from_id(device_id)
            if conn is None:
                return {
                    "ok": False,
                    "error": (
                        "Device has no audio playback connection yet. "
                        "Pair completed — wait a moment, press Scan, then Connect."
                    ),
                }
            self._connections[device_id] = conn

        self._watch_state(device_id, conn)

        if device_id not in self._started:
            await conn.start_async()
            self._started.add(device_id)

        result = await conn.open_async()
        status = result.status
        ok = status == AudioPlaybackConnectionOpenResultStatus.SUCCESS
        waiting = status == AudioPlaybackConnectionOpenResultStatus.REQUEST_TIMED_OUT
        try:
            if AudioPlaybackConnectionState is not None and conn.state == AudioPlaybackConnectionState.OPENED:
                ok = True
        except Exception:  # noqa: BLE001
            pass
        if ok:
            self._opened_ids.add(device_id)
            self._dropped_ids.discard(device_id)
            waiting = False
        # REQUEST_TIMED_OUT = the phone hasn't accepted the audio link yet. That is NOT connected:
        # the app shows "Waiting for phone…" and tries again instead of claiming audio is ready.
        return {
            "ok": ok,
            "waiting": waiting,
            "status": _status_name(status),
            "device_id": device_id,
            "name": name,
        }

    def _watch_state(self, device_id: str, conn: Any) -> None:
        if device_id in self._state_tokens:
            return
        if not hasattr(conn, "add_state_changed"):
            return

        def _on_state(sender: Any, _args: Any) -> None:
            try:
                state = sender.state
                name = _status_name(state)
                print(f"[bt_audio] StateChanged {device_id[-24:]}… → {name}")
                if AudioPlaybackConnectionState is None:
                    return
                if state == AudioPlaybackConnectionState.OPENED:
                    self._opened_ids.add(device_id)
                    self._dropped_ids.discard(device_id)
                elif state == AudioPlaybackConnectionState.CLOSED:
                    if device_id in self._opened_ids:
                        self._opened_ids.discard(device_id)
                        self._dropped_ids.add(device_id)
                        print(f"[bt_audio] link dropped (was open): {device_id[-24:]}…")
            except Exception:  # noqa: BLE001
                traceback.print_exc()

        try:
            # Strong ref — pywinrt GC otherwise drops the handler.
            self._state_handlers[device_id] = _on_state
            token = conn.add_state_changed(_on_state)
            self._state_tokens[device_id] = token
        except Exception as exc:  # noqa: BLE001
            print(f"[bt_audio] add_state_changed failed: {exc}")

    def get_status(self, device_id: str | None) -> dict[str, Any]:
        if not device_id:
            return {"connected": False, "reason": "none"}
        if not self.available:
            return {"connected": False, "reason": "unsupported"}
        try:
            return self._run(self._get_status(device_id), timeout=4.0)
        except Exception as exc:  # noqa: BLE001
            print(f"[bt_audio] get_status failed: {exc}")
            return {
                "connected": False,
                "reason": "error",
                "message": f"{type(exc).__name__}: {exc}",
            }

    async def _device_still_present(self, device_id: str) -> bool:
        try:
            info = await DeviceInformation.create_from_id_async(device_id)
            if info is None:
                return False
            if getattr(info, "is_enabled", None) is False:
                return False
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _get_status(self, device_id: str) -> dict[str, Any]:
        """connected=True only while the audio link is really OPENED.

        A link that drops (phone walked away, radio glitch) keeps its AudioPlaybackConnection
        (started, not closed), so the phone can reconnect and the app can re-open it without a
        new pairing / Connect. It is only closed when the phone can't come back by itself:
        Bluetooth off, device gone/unpaired, or a broken connection object.
        """
        radio_on = await self._bluetooth_radio_on()
        if radio_on is False:
            await self._disconnect(device_id, forget=False)
            return {
                "connected": False,
                "reason": "bluetooth_off",
                "message": "Bluetooth is off",
            }

        conn = self._connections.get(device_id)
        if conn is None:
            return {"connected": False, "reason": "no_connection", "message": "Not connected"}

        try:
            state = conn.state
        except Exception as exc:  # noqa: BLE001
            await self._disconnect(device_id, forget=False)
            return {
                "connected": False,
                "reason": "state_error",
                "message": str(exc),
            }

        if AudioPlaybackConnectionState is not None and state == AudioPlaybackConnectionState.OPENED:
            self._opened_ids.add(device_id)
            self._dropped_ids.discard(device_id)
            return {"connected": True, "state": "opened"}

        present = await self._device_still_present(device_id)
        if not present:
            await self._disconnect(device_id, forget=False)
            return {
                "connected": False,
                "reason": "device_gone",
                "message": "Device unavailable (phone Bluetooth off or unpaired)",
            }

        if device_id in self._dropped_ids or device_id in self._opened_ids:
            # Was playing, now closed: remember it (keep the connection object) until it re-opens.
            self._opened_ids.discard(device_id)
            self._dropped_ids.add(device_id)
            return {
                "connected": False,
                "reason": "closed",
                "message": "Phone disconnected",
            }

        # Started but never opened: the phone hasn't accepted the audio link (yet).
        return {
            "connected": False,
            "reason": "waiting",
            "waiting": True,
            "message": "Waiting for the phone to accept audio",
        }

    # ----------------------------------------------------------- disconnect ---
    def disconnect(self, device_id: str | None = None, *, forget: bool = False) -> dict[str, Any]:
        """Close AudioPlaybackConnection only (keeps Windows pairing by default)."""
        if not self.available:
            return {"ok": True}
        try:
            self._run(self._disconnect(device_id, forget=forget))
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return {"ok": False, "error": str(exc)}

    def forget_device(self, device_id: str) -> dict[str, Any]:
        """Close connection if open, then unpair from Windows (real forget)."""
        if not self.available:
            return {"ok": False, "error": self.last_error or "unsupported"}
        try:
            return self._run(self._forget_device(device_id), timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            print("[bt_audio] forget_device failed:")
            traceback.print_exc()
            return {"ok": False, "error": str(exc)}

    def forget_all(self) -> dict[str, Any]:
        """Unpair every paired Bluetooth device from Windows (real forget all)."""
        if not self.available:
            return {"ok": False, "error": self.last_error or "unsupported"}
        try:
            return self._run(self._forget_all(), timeout=90.0)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            print("[bt_audio] forget_all failed:")
            traceback.print_exc()
            return {"ok": False, "error": str(exc)}

    async def _disconnect(self, device_id: str | None, *, forget: bool = False) -> None:
        ids = [device_id] if device_id else list(self._connections.keys())
        for did in ids:
            if not did:
                continue
            conn = self._connections.pop(did, None)
            self._started.discard(did)
            self._opened_ids.discard(did)
            self._dropped_ids.discard(did)
            token = self._state_tokens.pop(did, None)
            self._state_handlers.pop(did, None)
            if conn is not None and token is not None and hasattr(conn, "remove_state_changed"):
                try:
                    conn.remove_state_changed(token)
                except Exception:  # noqa: BLE001
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            if forget:
                await self._unpair(did)

    async def _forget_device(self, device_id: str) -> dict[str, Any]:
        await self._disconnect(device_id, forget=False)
        ok = await self._unpair(device_id)
        # Also try to resolve APC meta / nearby alias ids by name if needed.
        return {"ok": ok, "device_id": device_id}

    async def _forget_all(self) -> dict[str, Any]:
        await self._disconnect(None, forget=False)
        ids = await self._list_paired_bluetooth_ids()
        print(f"[bt_audio] forget_all: {len(ids)} paired device id(s)")
        forgotten = 0
        errors: list[str] = []
        for did in ids:
            try:
                if await self._unpair(did):
                    forgotten += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{did[-20:]}:{exc}")
        self._device_meta.clear()
        self._nearby_meta.clear()
        return {
            "ok": True,
            "forgotten": forgotten,
            "total": len(ids),
            "errors": errors[:5],
        }

    async def _list_paired_bluetooth_ids(self) -> list[str]:
        """Collect Windows-paired Bluetooth device IDs (classic + LE + APC)."""
        seen: set[str] = set()
        selectors: list[tuple[str, str]] = []
        if _BT_DEVICE_OK and BluetoothDevice is not None:
            try:
                selectors.append(
                    ("bt_paired", str(BluetoothDevice.get_device_selector_from_pairing_state(True)))
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] bt paired selector failed: {exc}")
        if _BT_DEVICE_OK and BluetoothLEDevice is not None:
            try:
                selectors.append(
                    (
                        "ble_paired",
                        str(BluetoothLEDevice.get_device_selector_from_pairing_state(True)),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] ble paired selector failed: {exc}")
        try:
            selectors.append(("apc", self._apc_selector()))
        except Exception:  # noqa: BLE001
            pass

        kind = self._association_endpoint_kind()
        for label, selector in selectors:
            infos: list[Any] = []
            try:
                if kind is not None:
                    fn = getattr(
                        DeviceInformation,
                        "find_all_async_with_kind_aqs_filter_and_additional_properties",
                        None,
                    )
                    if fn is not None:
                        infos = list(await fn(selector, [], kind))
                if not infos:
                    infos = list(await DeviceInformation.find_all_async_aqs_filter(selector))
            except Exception as exc:  # noqa: BLE001
                print(f"[bt_audio] {label} enum for forget_all failed: {exc}")
                continue
            print(f"[bt_audio] {label}: {len(infos)} for forget_all")
            for info in infos:
                try:
                    did = str(info.id)
                    if did:
                        seen.add(did)
                except Exception:  # noqa: BLE001
                    pass
        # Include any ids we currently track.
        seen.update(self._connections.keys())
        seen.update(self._device_meta.keys())
        return sorted(seen)

    async def _unpair(self, device_id: str) -> bool:
        try:
            info = await DeviceInformation.create_from_id_async(device_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[bt_audio] unpair lookup failed: {exc}")
            return False
        pairing = getattr(info, "pairing", None) if info else None
        if pairing is None:
            print(f"[bt_audio] unpair: no pairing object for {device_id[-24:]}…")
            return False
        if not bool(getattr(pairing, "is_paired", False)):
            print(f"[bt_audio] unpair: already unpaired {device_id[-24:]}…")
            return True
        try:
            result = await pairing.unpair_async()
            status = _status_name(getattr(result, "status", result))
            print(f"[bt_audio] unpaired {device_id[-24:]}… status={status}")
            return status.upper() in ("UNPAIRED", "ALREADY_UNPAIRED", "SUCCESS")
        except Exception as exc:  # noqa: BLE001
            print(f"[bt_audio] unpair failed: {exc}")
            return False


def _status_name(status: Any) -> str:
    try:
        return str(status).split(".")[-1]
    except Exception:  # noqa: BLE001
        return str(status)