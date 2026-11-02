"""
Online updates for Muvi from GitHub Releases (public repo, no token, no update code).

Flow (Settings -> "Check for updates" -> "Update now"):
  1. check      GET /releases/latest (or all releases on the beta channel),
                pick Muvi-<version>-<lang>.zip.
  2. download   stream the zip to C:\\kiosk_launcher\\updates\\, verify SHA256SUMS.txt.
  3. extract    unzip to C:\\kiosk_launcher\\app_new\\ and sanity-check it.
  4. apply      write a small PowerShell script, start it hidden, quit the app.
                The script waits for this process to exit, stops any leftover main.exe
                from the app folder (audio engine worker, watchdog), then:
                  app -> app_prev, app_new -> app, starts app\\main.exe,
                  waits for the new app to write updates\\started.flag,
                  and rolls back to app_prev if it doesn't within 90 s.

Layout on the tablet (frozen build) - same as BrainGym:
  C:\\kiosk_launcher\\launch_app.bat      (unchanged)
  C:\\kiosk_launcher\\app\\main.exe        (current version)
  C:\\kiosk_launcher\\app_prev\\           (previous version, kept for rollback)
  C:\\kiosk_launcher\\updates\\            (downloads, apply script, logs)
  C:\\kiosk_launcher\\update_channel.txt  (optional: "beta" to also get pre-releases)

The UI polls UpdateController.snapshot(); all work runs in background threads.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import requests

APP_NAME = "Muvi"
UPDATE_ENABLED = True
UPDATE_RELEASES_REPO = "Gharieni-Middle-East/muvi-releases"
DEFAULT_LANGUAGE = "en"
GITHUB_API = "https://api.github.com"
CHECKSUM_ASSET = "SHA256SUMS.txt"
VERSION_FILE = "version.txt"
STARTED_FLAG = "started.flag"
UPDATE_NOTE = "last_update.json"   # {"from": old, "to": new} - read once by the next start
HEALTH_TIMEOUT_SECONDS = 90

# Muvi keeps nothing in its app folder between versions (demo.wav ships inside the build).
PRESERVE_DIRS: tuple = ()
PRESERVE_FILES: tuple = ()

_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


# --------------------------------------------------------------------------- #
# Versions and paths
# --------------------------------------------------------------------------- #

def normalize_version(tag: str) -> str:
    return (tag or "").strip().lstrip("vV")


def version_key(tag: str):
    """Sortable key for 1.2.3 / 1.2.3-beta.1 (a final release sorts after its betas)."""
    match = _TAG_RE.match((tag or "").strip())
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    nums = (int(major), int(minor), int(patch))
    if pre is None:
        return nums + ((1,),)
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return nums + ((0,) + parts,)


def is_newer(candidate: str, current: str) -> bool:
    cand, cur = version_key(candidate), version_key(current)
    if cand is None:
        return False
    if cur is None:
        return True
    return cand > cur


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Folder with main.exe (frozen) or the project root (source run)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def launcher_dir() -> Path:
    """C:\\kiosk_launcher on tablets (parent of the app folder)."""
    return app_dir().parent


def updates_dir() -> Path:
    return launcher_dir() / "updates"


def _read_version_file() -> dict:
    data = {}
    path = app_dir() / VERSION_FILE
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                data[key.strip().lower()] = value.strip()
    except OSError:
        pass
    return data




def _source_version() -> str:
    """APP_VERSION from web/constants/constants.js (shown in the sidebar)."""
    for base in (Path(getattr(sys, "_MEIPASS", "")), Path(__file__).resolve().parent):
        path = base / "web" / "constants" / "constants.js"
        try:
            match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if match:
            return match.group(1).strip()
    return "0.0.0"


def current_version() -> str:
    """Full installed version, e.g. 1.0.2 or 1.0.2-beta.1 (version.txt from the build when present)."""
    return normalize_version(_read_version_file().get("version") or _source_version())


def build_language() -> str:
    return (_read_version_file().get("language") or DEFAULT_LANGUAGE or "en").strip().lower()


def _read_text_any(path: Path) -> str:
    """Read a small text file saved by Notepad (UTF-8 with/without BOM, UTF-16, ANSI)."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        return text.replace("\ufeff", "").replace("\x00", "")
    return ""


def update_channel() -> str:
    """'beta' if update_channel.txt (next to launch_app.bat or main.exe) says beta, else 'stable'."""
    value = os.environ.get("BRAINGYM_UPDATE_CHANNEL", "")
    if not value:
        for folder in (launcher_dir(), app_dir()):
            # Also accept Notepad's hidden double extension (update_channel.txt.txt).
            for name in ("update_channel.txt", "update_channel.txt.txt", "update_channel"):
                path = folder / name
                if path.is_file():
                    try:
                        value = _read_text_any(path)
                    except OSError:
                        value = ""
                    break
            if value:
                break
    return "beta" if "beta" in value.strip().lower() else "stable"


def can_install() -> bool:
    """Installing only works in the built app on Windows (not when running main.py)."""
    return is_frozen() and os.name == "nt"


def _log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(f"[Update] {message}")
    try:
        updates_dir().mkdir(parents=True, exist_ok=True)
        with (updates_dir() / "update.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass




def mark_started() -> None:
    """Called once the window has loaded; the apply script waits for this flag."""
    if not is_frozen():
        return
    try:
        import AppWatchdog
        AppWatchdog.set_updating(False)   # this version is up - restarts allowed again
    except Exception:  # noqa: BLE001
        pass
    try:
        updates_dir().mkdir(parents=True, exist_ok=True)
        (updates_dir() / STARTED_FLAG).write_text(current_version(), encoding="utf-8")
    except OSError as exc:
        print(f"[Update] Could not write started flag: {exc}")


def _write_update_note(new_version: str) -> None:
    try:
        updates_dir().mkdir(parents=True, exist_ok=True)
        (updates_dir() / UPDATE_NOTE).write_text(
            json.dumps({"from": current_version(), "to": new_version}), encoding="utf-8"
        )
    except OSError as exc:
        _log(f"Could not write update note: {exc}")


def take_update_notice() -> Optional[dict]:
    """
    Called once at start-up. Returns {"kind": "updated"|"failed", "version": ...} when this
    start follows an update, else None. The note is deleted so it is shown only once.
    """
    if not is_frozen():
        return None
    path = updates_dir() / UPDATE_NOTE
    if not path.is_file():
        return None
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        note = {}
    try:
        path.unlink()
    except OSError:
        pass
    target = normalize_version(str(note.get("to") or ""))
    if not target:
        return None
    if current_version() == target:
        _log(f"Started after update to {target}")
        return {"kind": "updated", "version": target}
    _log(f"Update to {target} did not stay installed (running {current_version()})")
    return {"kind": "failed", "version": target}


# --------------------------------------------------------------------------- #
# GitHub releases
# --------------------------------------------------------------------------- #

class UpdateError(Exception):
    """Message is shown to the user."""


NO_INTERNET = "No internet connection"


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    notes: str
    zip_name: str
    zip_url: str
    zip_size: int
    checksum_url: str
    prerelease: bool


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{APP_NAME}/{current_version()}",
    })
    return s


def _get_json(session: requests.Session, url: str):
    try:
        resp = session.get(url, timeout=(10, 20))
    except requests.ConnectionError:
        raise UpdateError(NO_INTERNET)
    except requests.Timeout:
        raise UpdateError(NO_INTERNET)
    except requests.RequestException as exc:
        raise UpdateError(f"Network error: {exc}")
    if resp.status_code == 404:
        return None
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        raise UpdateError("Too many checks - try again in an hour")
    if resp.status_code != 200:
        raise UpdateError(f"Update server error ({resp.status_code})")
    return resp.json()


def _release_from_json(data: dict, language: str) -> Optional[ReleaseInfo]:
    tag = data.get("tag_name", "")
    if data.get("draft") or version_key(tag) is None:
        return None
    version = normalize_version(tag)
    zip_name = f"{APP_NAME}-{version}-{language}.zip"
    assets = {a.get("name"): a for a in data.get("assets", [])}
    zip_asset = assets.get(zip_name)
    sums_asset = assets.get(CHECKSUM_ASSET)
    if not zip_asset or not sums_asset:
        return None
    return ReleaseInfo(
        version=version,
        tag=tag,
        notes=(data.get("body") or "").strip(),
        zip_name=zip_name,
        zip_url=zip_asset["browser_download_url"],
        zip_size=int(zip_asset.get("size") or 0),
        checksum_url=sums_asset["browser_download_url"],
        prerelease=bool(data.get("prerelease")),
    )


def find_update() -> Optional[ReleaseInfo]:
    """Return the newest release for this tablet's language if it is newer, else None."""
    if not UPDATE_ENABLED:
        raise UpdateError("Updates are disabled")
    language = build_language()
    channel = update_channel()
    session = _session()
    base = f"{GITHUB_API}/repos/{UPDATE_RELEASES_REPO}/releases"
    _log(f"Checking for updates: current={current_version()} lang={language} channel={channel} "
         f"(app={app_dir()}, channel file in {launcher_dir()})")

    if channel == "beta":
        data = _get_json(session, f"{base}?per_page=30") or []
        candidates = [r for r in (_release_from_json(d, language) for d in data) if r]
        best = max(candidates, key=lambda r: version_key(r.tag), default=None)
    else:
        data = _get_json(session, f"{base}/latest")
        best = _release_from_json(data, language) if data else None

    if best is None or not is_newer(best.tag, current_version()):
        _log("Up to date")
        return None
    _log(f"Update available: {best.version} ({best.zip_name}, {best.zip_size} bytes)")
    return best


ProgressFn = Callable[[str, int], None]   # (phase, percent)


def _expected_checksum(session: requests.Session, release: ReleaseInfo) -> str:
    try:
        resp = session.get(release.checksum_url, timeout=(10, 30))
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise UpdateError(f"Could not download checksums: {exc}")
    for line in resp.text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == release.zip_name:
            return parts[0].lower()
    raise UpdateError("Checksum missing for this update")


def _download(session, release, dest: Path, progress: ProgressFn, cancel: threading.Event) -> str:
    part = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha256()
    done = 0
    try:
        with session.get(release.zip_url, stream=True, timeout=(10, 60)) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or release.zip_size or 0)
            with part.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if cancel.is_set():
                        raise UpdateError("Cancelled")
                    if not chunk:
                        continue
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if total:
                        progress("download", min(99, done * 100 // total))
    except requests.RequestException as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Download failed: {exc}")
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    progress("download", 100)
    return digest.hexdigest()


def _safe_extract(zip_path: Path, dest: Path, progress: ProgressFn, cancel: threading.Event) -> None:
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        for index, member in enumerate(members):
            if cancel.is_set():
                raise UpdateError("Cancelled")
            target = (dest / member.filename).resolve()
            if dest_resolved not in target.parents and target != dest_resolved:
                raise UpdateError("Update package is invalid")
            zf.extract(member, dest)
            if index % 50 == 0:
                progress("extract", index * 100 // max(1, len(members)))
    progress("extract", 100)


def prepare_update(release: ReleaseInfo, progress: ProgressFn, cancel: threading.Event) -> Path:
    """Download + verify + extract into kiosk_launcher\\app_new. Returns that folder."""
    if not can_install():
        raise UpdateError("Install only works in the installed app on the tablet")

    updates = updates_dir()
    updates.mkdir(parents=True, exist_ok=True)
    new_dir = launcher_dir() / f"{app_dir().name}_new"

    needed = max(release.zip_size, 50 * 1024 * 1024) * 4  # zip + extracted + margin
    free = shutil.disk_usage(str(launcher_dir())).free
    if free < needed:
        raise UpdateError(f"Not enough disk space ({free // (1024 * 1024)} MB free)")

    # Remove leftovers from earlier attempts.
    for old in updates.glob(f"{APP_NAME}-*.zip*"):
        old.unlink(missing_ok=True)
    shutil.rmtree(new_dir, ignore_errors=True)

    session = _session()
    expected = _expected_checksum(session, release)
    zip_path = updates / release.zip_name
    _log(f"Downloading {release.zip_url}")
    actual = _download(session, release, zip_path, progress, cancel)
    if actual != expected:
        zip_path.unlink(missing_ok=True)
        _log(f"Checksum mismatch: expected {expected} got {actual}")
        raise UpdateError("Download was corrupted - please try again")
    _log("Checksum OK")

    try:
        _safe_extract(zip_path, new_dir, progress, cancel)
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(new_dir, ignore_errors=True)
        raise UpdateError(f"Could not unpack update: {exc}")
    except UpdateError:
        shutil.rmtree(new_dir, ignore_errors=True)
        raise
    finally:
        zip_path.unlink(missing_ok=True)

    if not (new_dir / "main.exe").is_file() or not (new_dir / "_internal").is_dir():
        shutil.rmtree(new_dir, ignore_errors=True)
        raise UpdateError("Update package is incomplete")
    _log(f"Update {release.version} ready in {new_dir}")
    return new_dir


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ps_array(values) -> str:
    return "@(" + ", ".join(_ps_quote(v) for v in values) + ")"


_APPLY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$oldPid   = __PID__
$base     = __BASE__
$version  = __VERSION__
$keepDirs  = __KEEP_DIRS__
$keepFiles = __KEEP_FILES__
$name  = __APPNAME__
$app   = Join-Path $base $name
$new   = Join-Path $base ($name + '_new')
$prev  = Join-Path $base ($name + '_prev')
$fail  = Join-Path $base ($name + '_failed')
$upd   = Join-Path $base 'updates'
$log   = Join-Path $upd 'apply.log'
$flag  = Join-Path $upd 'started.flag'

function Log($m) { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
function Retry([scriptblock]$action, [string]$what) {
  for ($i = 1; $i -le 15; $i++) {
    try { & $action; return } catch { if ($i -eq 15) { throw "$what failed: $_" }; Start-Sleep -Seconds 1 }
  }
}
function Stop-AppProcesses {
  Get-Process -Name 'main' -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and ($_.Path -like "$app*") } |
    Stop-Process -Force -ErrorAction SilentlyContinue
}
function Start-App { Start-Process -FilePath (Join-Path $app 'main.exe') -WorkingDirectory $app }
function Move-Kept($from, $to) {
  foreach ($rel in $keepDirs) {
    $src = Join-Path $from $rel
    if (Test-Path -LiteralPath $src) {
      $dst = Join-Path $to $rel
      if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Recurse -Force }
      New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
      Retry { Move-Item -LiteralPath $src -Destination $dst } "move $rel"
      Log "moved $rel"
    }
  }
}

Log "---- apply $version (waiting for pid $oldPid)"
try { Wait-Process -Id $oldPid -Timeout 60 -ErrorAction SilentlyContinue } catch {}
if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
  Log "old app still running - stopping it"
  Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
Stop-AppProcesses

$movedOut = $false
try {
  if (-not (Test-Path -LiteralPath (Join-Path $new 'main.exe'))) { throw "$new is missing main.exe" }
  if (Test-Path -LiteralPath $prev) { Retry { Remove-Item -LiteralPath $prev -Recurse -Force } 'remove app_prev' }
  Retry { Rename-Item -LiteralPath $app -NewName ($name + '_prev') } 'rename app -> prev'
  $movedOut = $true
  Move-Kept $prev $new
  foreach ($rel in $keepFiles) {
    $src = Join-Path $prev $rel
    if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination (Join-Path $new $rel) -Force; Log "copied $rel" }
  }
  Retry { Rename-Item -LiteralPath $new -NewName $name } 'rename new -> app'
  Log "swap done"
} catch {
  Log "SWAP FAILED: $_"
  if ($movedOut -and -not (Test-Path -LiteralPath $app)) {
    try { Move-Kept $new $prev } catch { Log "restore kept files failed: $_" }
    try { Retry { Rename-Item -LiteralPath $prev -NewName $name } 'restore app' } catch { Log "$_" }
  }
  Start-App
  exit 1
}

Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue
Start-App
Log "started new version, waiting for health flag"
$deadline = (Get-Date).AddSeconds(__HEALTH_TIMEOUT__)
while ((Get-Date) -lt $deadline) {
  if (Test-Path -LiteralPath $flag) {
    $got = ([string](Get-Content -LiteralPath $flag -Raw -ErrorAction SilentlyContinue)).Trim()
    if ($got -eq $version) { Log "new version $version is running - done"; exit 0 }
  }
  Start-Sleep -Seconds 2
}

Log "new version did not report in - rolling back"
Set-Content -LiteralPath (Join-Path $upd 'updating.flag') -Value 'rollback'   # stop the new watchdog restarting it
Stop-AppProcesses
Start-Sleep -Seconds 2
try {
  if (Test-Path -LiteralPath $fail) { Remove-Item -LiteralPath $fail -Recurse -Force }
  Retry { Rename-Item -LiteralPath $app -NewName ($name + '_failed') } 'rename app -> failed'
  Move-Kept $fail $prev
  Retry { Rename-Item -LiteralPath $prev -NewName $name } 'rename prev -> app'
  Log "rolled back"
} catch { Log "ROLLBACK FAILED: $_" }
Start-App
exit 2
"""


def start_apply(release: ReleaseInfo) -> None:
    """Write and launch the swap script. Caller must quit the app right after."""
    if not can_install():
        raise UpdateError("Install only works in the installed app on the tablet")
    updates = updates_dir()
    updates.mkdir(parents=True, exist_ok=True)
    script = (
        _APPLY_SCRIPT
        .replace("__PID__", str(os.getpid()))
        .replace("__BASE__", _ps_quote(str(launcher_dir())))
        .replace("__APPNAME__", _ps_quote(app_dir().name))
        .replace("__VERSION__", _ps_quote(release.version))
        .replace("__KEEP_DIRS__", _ps_array(PRESERVE_DIRS))
        .replace("__KEEP_FILES__", _ps_array(PRESERVE_FILES))
        .replace("__HEALTH_TIMEOUT__", str(HEALTH_TIMEOUT_SECONDS))
    )
    script_path = updates / "apply_update.ps1"
    # UTF-8 with BOM so Windows PowerShell 5.1 reads non-ASCII paths correctly.
    script_path.write_text(script, encoding="utf-8-sig")

    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    args = [
        str(powershell), "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
        "-File", str(script_path),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    _log(f"Starting apply script for {release.version}")
    subprocess.Popen(args, creationflags=flags, close_fds=True, cwd=str(updates))




# --------------------------------------------------------------------------- #
# Controller (thread based; the web UI polls snapshot())
# --------------------------------------------------------------------------- #

class UpdateController:
    """
    States: idle, checking, up_to_date, available, downloading, installing, offline, error.
    snapshot() -> {"state", "percent", "message", "version", "available", "notes", "can_install"}
    `on_quit` is called (from a worker thread) when the apply script is running and the app
    must exit so the folders can be swapped.
    """

    def __init__(self, on_quit: Callable[[], None]) -> None:
        self._on_quit = on_quit
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self.state = "idle"
        self.percent = 0
        self.message = ""
        self.release: Optional[ReleaseInfo] = None

    @property
    def busy(self) -> bool:
        return self.state in ("checking", "downloading", "installing")

    def _set(self, state: str, *, percent: int = 0, message: str = "") -> None:
        with self._lock:
            self.state, self.percent, self.message = state, percent, message

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            release = self.release
            return {
                "state": self.state,
                "percent": self.percent,
                "message": self.message,
                "version": current_version(),
                "available": release.version if release else None,
                "notes": (release.notes[:600] if release else ""),
                "can_install": can_install(),
            }

    def _thread(self, target, name: str) -> None:
        def run():
            try:
                target()
            except UpdateError as exc:
                self._failed(str(exc))
            except Exception as exc:  # noqa: BLE001 - never crash the kiosk app
                _log(f"Unexpected error: {exc!r}")
                self._failed(f"Update failed: {exc}")
        threading.Thread(target=run, name=name, daemon=True).start()

    def _failed(self, message: str) -> None:
        _log(f"Failed: {message}")
        if message == NO_INTERNET:
            self._set("offline", message=message)
        elif message == "Cancelled":
            self._set("available" if self.release else "idle")
        else:
            self._set("error", message=message)

    def check(self) -> None:
        if self.busy:
            return
        self._set("checking")

        def job():
            release = find_update()
            with self._lock:
                self.release = release
            if release is None:
                self._set("up_to_date")
            else:
                self._set("available")
        self._thread(job, "update-check")

    def install(self) -> None:
        if self.busy or self.release is None:
            return
        release = self.release
        self._cancel.clear()
        self._set("downloading", percent=0)

        def progress(phase: str, percent: int) -> None:
            if self.state == "downloading":
                # Download = 0-90 %, unpacking = 90-100 %.
                value = percent * 90 // 100 if phase == "download" else 90 + percent // 10
                self._set("downloading", percent=value)

        def job():
            prepare_update(release, progress, self._cancel)
            self._set("installing")
            try:
                import AppWatchdog
                AppWatchdog.set_updating(True)    # watchdog must not restart the old app
                _write_update_note(release.version)
                start_apply(release)
            except Exception:
                try:
                    import AppWatchdog
                    AppWatchdog.set_updating(False)
                except Exception:  # noqa: BLE001
                    pass
                raise
            time.sleep(1.0)   # let the UI show "Installing" before the window closes
            self._on_quit()
        self._thread(job, "update-install")

    def cancel(self) -> None:
        if self.state == "downloading":
            self._cancel.set()
