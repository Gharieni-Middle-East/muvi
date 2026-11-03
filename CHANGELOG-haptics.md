# Changelog: Haptics ("Unlimited") brought in line with the Raspberry Pi app

- **Date:** 3 November 2026
- **Applies to:** muvi Windows tablet software (`muvi-update-feature`)
- **Backup of the code before these changes:** `~/Downloads/muvi-update-feature.backup-before-haptics-patch`

## Summary

The tablet already used the same haptics algorithm as the Raspberry Pi (`audio/haptics_dsp.py` is a port of the Pi's `dsp.py`). But five things *around* the algorithm made it behave differently. All five are fixed.

| # | Problem | Fix |
|---|---|---|
| 1 | At 48 kHz (Windows' usual setting), resampling each block on its own caused a click/rattle every block and slowly growing delay | Continuous resampler that keeps its state between blocks |
| 2 | The haptics output was **hard**-clipped | Soft clip, like the Pi |
| 3 | No per-transducer calibration (only 4 left/right pair levels) | Pi-style calibration: per-channel level, on/off, body position, overall level, a test tone and a step-by-step screen |
| 4 | The dual ASIO4ALL layout merged left and right | The 8 haptic channels go out unchanged |
| 5 | The DSP ran on 2048-sample blocks (Pi: 512), so it reacted slower | Runs in exact 512-sample blocks, with time constants that follow the real block length |

**Result**, measured on the same music recording, tablet vs Pi:

| Windows output rate | Before: correlation with Pi | After: correlation with Pi | Level difference after |
|---|---|---|---|
| 44.1 kHz | 0.976–0.995 | **1.000** | 0.0 dB |
| 48 kHz | 0.10–0.63 | **1.000** | 0.0 dB |

The only remaining difference is a fixed ~12 ms delay from the exact 512-sample blocks, which the app's vibration-sync delay already absorbs.

---

## 1. Continuous resampling (no block-edge clicks, no drift)

**Files:** `audio/audio_utils.py` (new `StreamResampler`), `audio/live_input.py` (`_rs()` helper and all live-path call sites)

### How the old code worked
The app processes audio at 44.1 kHz. When Windows runs the GigaPort at 48 kHz, every 2048-sample block was converted **on its own**:

```python
# audio/audio_utils.py (old, still used for non-live work)
def resample_if_needed(data, src_sr, target_sr):
    ...
    return resample_poly(data, up, down, axis=0).astype(np.float32)

# audio/live_input.py (old) – called once per block
if self.output_sample_rate != PROCESSING_SR:
    vib = resample_if_needed(vib, PROCESSING_SR, self.output_sample_rate)
```

`resample_poly` assumes silence before and after the block it's given. So:
- **The ends of every block were distorted.** On a clean 60 Hz test tone, sample-to-sample jumps of up to 0.44 appeared where 0.0085 is normal: a click roughly 21 times a second, felt as a rattle on the transducers. The headphone audio had the same problem.
- **Each block came out one sample too long** (2048 → 2049). Vibration was produced slightly faster than it was played, so the delay crept up by about 0.5 ms per second. After about 47 minutes the output buffer overflowed and a 46 ms block was dropped.

### How the new code works
A new class keeps the resampling filter running **continuously** across blocks. It remembers the last few input samples, so there are no block edges at all. It also tracks exactly how many output samples are owed, so the total length never drifts.

```python
# audio/audio_utils.py (new)
class StreamResampler:
    """Polyphase resampler that keeps its filter state between blocks."""
    def process(self, x):
        buf = np.concatenate([self.hist, x], axis=0)     # previous samples + new block
        total = self.n_in + len(x)
        m_end = (total * self.up + self.down - 1) // self.down   # outputs that are now complete
        ...
        self.hist = buf[len(buf) - (self.taps - 1):]   # remember the tail for the next block
```

```python
# audio/live_input.py (new) – one named stream per path, reset on every start
def _rs(self, name, data, src_sr, dst_sr):
    rs = self._resamplers.get(name) or StreamResampler(src_sr, dst_sr, channels)
    return rs.process(data)

if self.output_sample_rate != PROCESSING_SR:
    vib = self._rs("vib8", vib, PROCESSING_SR, self.output_sample_rate)
```

It's used everywhere the live stream changes rate: capture input (`cap_in`), haptics (`vib8`), zone vibration (`vib4`), headphone audio (`audio`), speaker passthrough (`pt_in`/`pt_out`) and `_process_pair`.

**Verified:**
- Feeding it random block sizes gives exactly the same output as one long call.
- It matches scipy's offline resampler to 4×10⁻⁷.
- Output length after 20 s and after 120 s is the same constant (+1 frame), so there's no drift.

---

## 2. Soft clip instead of hard clip

**File:** `audio/live_input.py` (`_render_haptics_only`)

### How the old code worked
After the DSP, the zone levels were multiplied in and the result was **hard-clipped**:

```python
# old
return np.clip(vib, -1.0, 1.0)
```

Anything over full scale got its top cut flat, which turns a bass wave into a harsh square-ish buzz. This is the same problem we fixed on the Pi. From the UI it could only happen with high zone gains, but the new calibration allows boosts up to +12 dB, so it had to be safe.

### How the new code works
```python
# new
vib = soft_clip(vib.astype(np.float32, copy=False), 0.99)
```

`soft_clip` (from `haptics_dsp.py`) is linear up to 75% of the ceiling, then bends smoothly towards 0.99 and never cuts the wave flat. This is exactly what the Pi does.

---

## 3. Pi-style haptics calibration

**New files:**
- `audio/haptics_calibration.py`: storage and validation
- `web/calibration.js`: the UI logic

**Changed files:**
- `audio/live_input.py`
- `live_audio.py`, `engine_worker.py`, `engine_bridge.py`: the message path from the UI to the audio engine
- `demo_audio.py`: silent mode
- `main.py`: state, API, keep-alive
- `web/index.html`, `web/styles.css`, `web/app.js`: card, sheet and mock API

### How the old code worked
The only level controls were the **Vibration** slider and the four zone switches. Both acted on left/right **pairs**:

```python
# old – audio/live_input.py
gains = (self.intensity_head, self.intensity_upper, self.intensity_mid, self.intensity_legs)
for pair_idx, g in enumerate(gains):
    vib[:, pair_idx * 2 : pair_idx * 2 + 2] *= float(g)
```

There was no way to:
- set left and right separately, or turn off a single transducer
- raise the overall haptic strength. The DSP always ran at 0 dB, while the Pi in use is set to +10 dB.
- find out which transducer is where.

### How the new code works

**Settings model** (saved as JSON), the same as on the Pi:

```python
# audio/haptics_calibration.py (new)
DEFAULTS = {
    "master_gain_db": 0.0,                # overall level, -20..+20 dB
    "channel_gain_db": [0.0] * 8,         # per transducer, -30..+12 dB
    "channel_enabled": [True] * 8,
    "channel_labels": [["Head", "Left"], ["Head", "Right"], ...],   # body position
}
```

The file lives at `C:\kiosk_launcher\haptics_calibration.json`, **outside** the app folder, because the online updater replaces the whole app folder.

**In the engine**, each channel's final gain is:
- calibration level × on/off × the existing Vibration slider and zone switch.

The overall level goes **inside** the DSP, before its limiter, so turning it up makes the vibration stronger without distortion. Gain changes fade across the block so they never click.

```python
# new – audio/live_input.py
target = np.array(
    [trims[c] * (1.0 if enabled[c] else 0.0) * float(zone[c // 2]) for c in range(8)],
    dtype=np.float32,
)
ramp = np.linspace(0.0, 1.0, frames, endpoint=False, dtype=np.float32)[:, None]
vib = vib * (self._haptic_prev_gain[None, :] + (target - self._haptic_prev_gain)[None, :] * ramp)
```

**Test tone:** a pulsed 40 Hz tone (0.8 s on, 0.4 s off, −12 dBFS) plays on one channel at that channel's level. It plays even if the channel is switched off, so it can always be found. It stops by itself 10 s after the UI stops renewing it, for example if the app crashes.

```python
# new – audio/live_input.py
if test_ch is not None:
    vib = self._haptic_test_tone(test_ch, frames)
    target = np.zeros(8, dtype=np.float32)
    target[test_ch] = trims[test_ch]
```

**Calibrating with no music playing:** `start_demo_audio(silent=True)` opens the GigaPort exactly as Demo does, but plays silence, so nothing is heard in the headphones. `main.py` closes it again when calibration ends. If Live or Demo is already playing, the tone simply plays over it.

**UI:**
- **Settings card:** overall level, then each channel's slider, Test button and on/off switch, plus Save and Undo.
- **Start calibration** opens a sheet that steps through channels 1–8. Each step shows the body part and side, a highlighted figure and a level slider, and lets you correct the position. A review at the end leads to **Save and finish**.

Changes apply immediately. Only **Save** writes them to the file; **Undo** goes back to the last save.

---

## 4. Dual ASIO4ALL keeps left and right

**Files:** `audio/gigaport_routing.py` (new `build_output_block_haptics8`), `audio/live_input.py` (`_ingest_dual`)

### How the old code worked
Only the `dual_native` and `vibration_only` layouts sent the 8 haptic channels out as they are. In `dual_asio4all` they were first averaged into 4 zones, so the stereo balance, one of the main features of smart mode, was lost:

```python
# old
if vib8 is not None:
    vib = self._haptics_to_zones(vib8)      # L and R averaged per row
block = build_output_block(audio[:n], vib[:n], layout=self.output_layout, ...)
```

### How the new code works
```python
# new
elif vib8 is not None and self.output_layout == "dual_asio4all":
    # ch1-8 = the 8 haptic channels as-is (keeps left/right), audio on ch9-12.
    block = build_output_block_haptics8(audio[:n], vib8[:n], speaker_route=self.speaker_route)
```

The `single` layout and overlay (APC) mode have only **4** vibration outputs on the hardware (ch3–6), so they still average left and right. That's a hardware limit, now noted in a code comment.

---

## 5. DSP timing identical to the Pi

**Files:** `audio/haptics_dsp.py`, `audio/live_input.py`

### How the old code worked
The DSP was called once per capture block, which is 2048 samples (46 ms), while the Pi uses 512 (11.6 ms). Its smoothing constants were calculated for 2048, so the attack and release times were right. But levels were only measured and updated once every 46 ms, so the vibration reacted less tightly to beats. The level-tracker drift, a fixed amount per block, also ran 4× slower than on the Pi.

```python
# old – haptics_dsp.py: constants fixed once, for the block size given at start
t = block / fs
self.c_att = np.exp(-t / COMP_ATTACK_S)
...
self.lvl_min = ... else self.lvl_min * MIN_DECAY      # same step, whatever the block length
```

### How the new code works
- **Exact 512-sample blocks.** The engine feeds the DSP exactly 512 samples at a time through a small buffer. That buffer starts with one block of silence, so every call still returns as many samples as it received: a fixed 11.6 ms delay, and vibration/audio alignment is unchanged.
- **Constants follow the real block length,** so even an odd-sized block behaves correctly.

```python
# new – haptics_dsp.py
def _set_coefs(self, n):
    t = n / self.fs
    self.c_att = np.exp(-t / COMP_ATTACK_S)
    ...
    steps = t / TRACKER_STEP_S                         # TRACKER_STEP_S = 512 / 44100
    self.min_decay, self.max_decay = MIN_DECAY ** steps, MAX_DECAY ** steps
```

```python
# new – live_input.py
self._haptic_in_fifo = np.concatenate([self._haptic_in_fifo, capture], axis=0)
n_full = len(self._haptic_in_fifo) // HAPTIC_BLOCK * HAPTIC_BLOCK
blocks = [dsp.process(self._haptic_in_fifo[i:i + HAPTIC_BLOCK].T).T
          for i in range(0, n_full, HAPTIC_BLOCK)]
```

The "Bass depth" tilt that came with the port is unchanged. It's neutral at the app's default setting of 200 Hz.

---

## Testing done (on a Mac, without Windows hardware)

- **Pi comparison:** tablet vs Pi on the same music, as in the table at the top. Correlation 1.000 and 0.0 dB difference at both 44.1 and 48 kHz.
- **Resampler:**
  - Block-size independent (difference 0).
  - Matches scipy's offline resampler.
  - No drift over 120 s.
- **Layouts:** correct channel counts for all four. `dual_asio4all` keeps left ≠ right on ch1–8, with audio on ch9–10.
- **Calibration:**
  - Trims −6/+6 dB measured exactly −6.0/+6.0 dB, and a switched-off channel is silent.
  - Overall +10 dB measured exactly +10 dB, with a clean peak of 0.68.
- **Test tone:** only the chosen channel plays, and it stops after its deadline.
- **`main.py` API**, run with stubbed Windows modules: value clamping, silent start and stop, Save, Undo.
- **Web UI**, run in a browser on the mock API: no JavaScript errors, and the whole flow (card, sheet, correct position, review, save) works.

## Still to test on the tablet

1. **Calibration with nothing playing:** each channel 1–8 pulses on the right transducer, and the headphones stay silent.
2. **Calibration during Live and during Demo:** the tone plays over the music, and the music returns after Exit.
3. **Save survives a restart:** `C:\kiosk_launcher\haptics_calibration.json` is written and reloaded.
4. **Clean output at 48 kHz:** with the GigaPort set to 48 kHz in Windows Sound settings, the haptics sound clean (no rattle).
5. **Windows volume:** lowering the Windows volume should not weaken the vibration. This is from the earlier review and still needs checking.

**Note on levels:** the Vibration slider defaults to 0.27, which is −11 dB, while the Pi in use has an overall level of +10 dB. Raise **Overall haptic level** in Settings if the tablet feels weaker than the Pi.
