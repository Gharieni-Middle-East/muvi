# muve (production app)

Same as old app. New UI + Bluetooth + optional AUX.

## Modes (automatic on Play)

### 1) Bluetooth (priority when connected in app)
- **Scan** — finds nearby unpaired phones (AssociationEndpoint inquiry)
- **Pair** — `DeviceInformationCustomPairing` with `CONFIRM_ONLY` (confirm on phone when prompted)
- **Paired phones** refresh automatically (no separate Scan for already-paired)
- **Connect** — `TryCreateFromId` → `StartAsync` → `OpenAsync` (APC flow)
- **Disconnect** closes the audio link only (pairing kept)
- **Forget** / **Forget all** call Windows `UnpairAsync` (real remove from Bluetooth)
- App sets default playback to **CABLE Input**, captures CABLE → **Gigaport ASIO**
- Song title / seek are not available for phone→PC Bluetooth (Windows limitation)
- While Live is on, the timer shows **session elapsed** (`LIVE`)
- **Prev / Next** may not control the phone on this path

Phone/Windows may still show a short confirm toast during Pair — that cannot be removed.

Console:
```
[windows_cable] default playback → 'CABLE Input'
[live_audio] mode=cable prefer_bt=True capture='…CABLE…' output='…ASIO…Gigaport…'
```

### 2) AUX (Behringer / USB interface)
- Disconnect Bluetooth in the app (or skip Connect)
- Plug phone AUX into Behringer → USB to tablet
- **Play** captures Behringer → **Gigaport ASIO**

Console:
```
[live_audio] mode=aux capture='…Behringer…' output='…ASIO…Gigaport…'
```

## 3) Demo tab
- Plays **`assets/demo.wav`** through the **same Live → Gigaport** path (audio ch1–2 + vibration ch3–6)
- Same volume / vibration / bass-cutoff controls as Now Playing
- Seek / restart work on the demo file
- Starting Demo stops Live (and vice versa) so ASIO isn’t contested

### Aurasens programs (Programs page)
- The programs are **not in the repo or the release zip** (about 4.5 GB, and Aurasens content).
  Copy the prepared `muvi-programs` folder onto the tablet as **`C:\kiosk_launcher\programs`**
  (`programs.json` + `<Title>.aif` + `<Title>.jpg`). The Programs page reads it each time it opens.
  Other folder: set `MUVI_PROGRAMS_DIR`.
- Each `.aif` is an original Aurasens track (2 ch, 48 kHz, 24-bit: 16-bit audio + 8-bit haptic
  data, 32 slots at 1500 Hz). `audio/aurasens_program.py` decodes it from disk block by block.
- Headphones/speakers get the audio. The **vibration Gigaport outputs 1–8 get Aurasens slots
  1, 3, 14, 16, 23, 24, 27, 28** (`PROGRAM_SLOT_MAP`; `"slot_map"` in `programs.json` overrides it).
  The haptics DSP is bypassed; calibration trims, on/off, zone switches and the Vibration slider still apply.


## 4) Haptics calibration (Settings)
Same model as the Raspberry Pi app: per-transducer level (dB), on/off and body position, plus an
overall haptic level. **Start calibration** pulses one Gigaport channel at a time (40 Hz,
0.8 s on / 0.4 s off) and shows where it should be felt; correct the position if it's elsewhere.
- Works with nothing playing (opens the Gigaport with silence) or over Live/Demo.
- Edits apply live; **Save** keeps them (Undo returns to the last save).
- Stored outside the app folder so updates keep it: `C:\kiosk_launcher\haptics_calibration.json`
  (source runs: `haptics_calibration.json` in the project folder).
- Applies to **Haptics** mode. The Vibration slider and zone switches still work on top of it.

**Easiest fix for WinRT errors on the tablet:** double-click `install_winrt.bat` in `D:\muvi_new`, wait for `SUCCESS`, then:

```bat
python main.py
```

`main.py` also auto-installs missing WinRT packages on startup into the same Python.

Manual:

```bat
cd D:\muvi_new
python -m pip install -r requirements.txt
python main.py
```

Always use `python -m pip` (not bare `pip`) so packages go into the same Python as `main.py`.

Close the old muve app before Play (ASIO exclusive).

**Output is always Gigaport — never set app output to VB-Cable.**


cd D:\muvi_new
python -c "import sys; print(sys.executable)"
python -m pip install --force-reinstall winrt-runtime winrt-Windows.Foundation.Collections winrt-Windows.Media.Control winrt-Windows.Foundation
python -c "import winrt.windows.foundation.collections; print('OK')"
python main.py




Disable the Windows Update service using Windows-Update-Blocker-main





pyinstaller --noconfirm --windowed --add-data "web;web" --add-data "assets;assets" main.py





@echo off
setlocal

timeout /t 15 /nobreak >nul
cd /d "%~dp0app"

"C:\Users\RLX-Satori\AppData\Local\Programs\Python\Python313\python.exe" ^
"%~dp0app\main.py" > "%~dp0startup_log.txt" 2>&1

endlocal



pyinstaller --noconfirm --windowed --add-data "web;web" --add-data "assets;assets" main.py