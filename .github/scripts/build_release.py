"""Build a Muvi release zip with PyInstaller.

Used by .github/workflows/release.yml, and can be run on a Windows PC from the repo
root to test the same build locally:

    python .github/scripts/build_release.py --tag v1.0.2 --lang en

Output (in ./release):
    Muvi-<version>-<lang>.zip         main.exe + _internal (web UI, assets, demo.wav) + version.txt
    Muvi-<version>-<lang>.zip.sha256  "<sha256>  <zip name>"
    notes.md                          this version's VERSION_HISTORY.md section

Same layout the old WIM updater produced for Muvi (one folder, windowed, entry main.py,
web/ and assets/ bundled), so tablets and USB images keep working unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

APP_NAME = "Muvi"
LANGUAGES = ("en",)

ROOT = Path(__file__).resolve().parents[2]
CONSTANTS_JS = ROOT / "web" / "constants" / "constants.js"
MAIN_PY = ROOT / "main.py"
HISTORY_FILE = ROOT / "VERSION_HISTORY.md"
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
OUT_DIR = ROOT / "release"

COLLECT_ALL = (
    "webview",       # pywebview (+ its Edge WebView2 backend)
    "clr_loader",
    "pythonnet",
    "numpy",
    "scipy",
    "sounddevice",
    "soundcard",
    "cffi",
    "certifi",
    "requests",
    "pycaw",
    "comtypes",
    "winrt",
)

HIDDEN_IMPORTS = (
    "clr",
    "_cffi_backend",
    "sounddevice",
    "soundcard",
    "pycaw",
    "comtypes",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.media.audio",
    "winrt.windows.media.control",
    "winrt.windows.devices.enumeration",
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.radios",
    "winrt.windows.devices.wifi",
    "winrt.windows.networking.connectivity",
    "winrt.windows.security.credentials",
    "winrt.windows.system",
)

TAG_RE = re.compile(r"^v?(?P<base>\d+\.\d+\.\d+)(?P<pre>-[0-9A-Za-z.-]+)?$")


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def parse_tag(tag: str) -> tuple[str, str, bool]:
    """Return (version, base_version, is_prerelease) for e.g. v1.0.2-beta.1."""
    match = TAG_RE.match(tag.strip())
    if not match:
        fail(f"Tag {tag!r} is not like v1.2.3 or v1.2.3-beta.1")
    base = match.group("base")
    pre = match.group("pre") or ""
    return base + pre, base, bool(pre)


def check_version(base_version: str) -> None:
    text = CONSTANTS_JS.read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', text)
    if not match:
        fail(f"APP_VERSION not found in {CONSTANTS_JS}")
    if match.group(1).strip() != base_version:
        fail(
            f"Tag version {base_version} does not match "
            f"APP_VERSION = {match.group(1)!r} in web/constants/constants.js. "
            "Update it, commit, then tag that commit."
        )
    print(f"Version check OK: {base_version}")


def force_fullscreen(text: str) -> str:
    text, count = re.subn(r"(?m)^(FULLSCREEN\s*=\s*)(True|False)", r"\g<1>True", text, count=1)
    if not count:
        fail("FULLSCREEN not found in main.py")
    print("Applied FULLSCREEN = True")
    return text


def importable(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def run_pyinstaller() -> Path:
    for path in (BUILD_DIR, DIST_DIR):
        shutil.rmtree(path, ignore_errors=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", "main",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        "--add-data", f"{ROOT / 'web'}{os.pathsep}web",
        "--add-data", f"{ROOT / 'assets'}{os.pathsep}assets",
        "--log-level", "INFO",
    ]
    for pkg in COLLECT_ALL:
        if importable(pkg):
            args += ["--collect-all", pkg]
        else:
            print(f"Skipping --collect-all {pkg} (not importable here)")
    for name in HIDDEN_IMPORTS:
        if importable(name):
            args += ["--hidden-import", name]
        else:
            print(f"Skipping --hidden-import {name} (not importable here)")
    args.append(str(MAIN_PY))

    print("PyInstaller:", subprocess.list2cmdline(args))
    subprocess.run(args, cwd=ROOT, check=True)

    app_dir = DIST_DIR / "main"
    for required in (app_dir / "main.exe", app_dir / "_internal" / "web" / "index.html",
                     app_dir / "_internal" / "web" / "constants" / "constants.js",
                     app_dir / "_internal" / "assets" / "demo.wav"):
        if not required.is_file():
            fail(f"Build output missing: {required}")
    return app_dir


def write_version_file(app_dir: Path, tag: str, lang: str) -> None:
    # Same format the USB updater writes (file_manager.format_version_file).
    (app_dir / "version.txt").write_text(
        f"app={APP_NAME}\nlanguage={lang}\nversion={tag}\n", encoding="utf-8", newline="\n"
    )


def make_zip(app_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(app_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(app_dir).as_posix())


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_notes(base_version: str) -> str:
    if HISTORY_FILE.is_file():
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        heading = re.compile(rf"^##\s*v?{re.escape(base_version)}\b")
        start = next((i for i, ln in enumerate(lines) if heading.match(ln)), None)
        if start is not None:
            body = []
            for ln in lines[start + 1:]:
                if ln.startswith("##"):
                    break
                body.append(ln)
            text = "\n".join(body).strip()
            if text:
                return text + "\n"
    return f"{APP_NAME} v{base_version}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="git tag, e.g. v1.0.2")
    parser.add_argument("--lang", default="en", choices=LANGUAGES)
    args = parser.parse_args()

    if os.name != "nt":
        fail("Build on Windows - PyInstaller cannot cross-compile main.exe")

    tag = args.tag.strip()
    version, base_version, prerelease = parse_tag(tag)
    check_version(base_version)

    # Patch main.py for the build only; always restore the working tree.
    original = MAIN_PY.read_bytes().decode("utf-8")
    MAIN_PY.write_bytes(force_fullscreen(original).encode("utf-8"))
    try:
        app_dir = run_pyinstaller()
    finally:
        MAIN_PY.write_bytes(original.encode("utf-8"))

    write_version_file(app_dir, tag, args.lang)

    shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True)
    zip_name = f"{APP_NAME}-{version}-{args.lang}.zip"
    zip_path = OUT_DIR / zip_name
    make_zip(app_dir, zip_path)
    checksum = sha256_of(zip_path)
    (OUT_DIR / f"{zip_name}.sha256").write_text(f"{checksum}  {zip_name}\n", encoding="utf-8", newline="\n")
    (OUT_DIR / "notes.md").write_text(release_notes(base_version), encoding="utf-8", newline="\n")

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"Built {zip_name} ({size_mb:.1f} MB) sha256={checksum} prerelease={prerelease}")


if __name__ == "__main__":
    main()
