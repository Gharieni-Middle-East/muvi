"""Live audio -> haptics DSP, based on the Aurasens "Quake" smart mode
(see AURASENS_AUDIO_SPEC.md, section 2.3), adapted to drive 8 analog transducer channels.

Ported from muvi-haptics/dsp.py.

Per block (512 samples, ~11 ms):
  1. Split L and R into 3 bands (low 20-50, mid 50-80, high 80-110 Hz): 6 band signals.
  2. Compress each band (4:1 above -20 dB, attack ~10 ms, release ~250 ms).
  3. Dynamic intensity: track the slow min/max of the (L+R) level, normalise to 0..1,
     and apply -3..+3 dB to the mid and high bands.
  4. Stereo balance: the L-R level difference (limited to +/-6 dB) is applied between L and R.
  5. Mix the 6 bands to 8 outputs (channel map + overall intensity), then a smooth limiter per
     output (ceiling -3 dBFS, fast attack, slow release) and a soft clip as a last safety net.
Every gain change is ramped across the block, so nothing clicks.

Compared with Aurasens' Quake (50:1 compression, +15 dB pre-gain, per-block peak normalisation to
full scale) this keeps headroom and dynamics: the Quake settings drove the transducers into
clipping and produced abrupt level jumps between blocks.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

SOURCES = ["L-low", "L-mid", "L-high", "R-low", "R-mid", "R-high"]
BANDS = [("low", 20.0, 50.0), ("mid", 50.0, 80.0), ("high", 80.0, 110.0)]

PRE_GAIN = 10 ** (6 / 20)  # +6 dB into the compressor
COMP_THRESHOLD_DB = -20.0
COMP_RATIO = 4.0
COMP_ATTACK_S = 0.010
COMP_RELEASE_S = 0.250
LIMIT_CEILING = 10 ** (-3 / 20)  # outputs stay below -3 dBFS before per-channel calibration
LIMIT_RELEASE_S = 0.300
MIN_DECAY, MAX_DECAY = 0.997869, 1.002131  # level min/max tracker drift (from the app)
EPS = 1e-10


def _db(rms):
    return 20.0 * np.log10(rms + EPS)


def soft_clip(x, ceiling):
    """Linear below 75% of the ceiling, then a smooth tanh knee that never exceeds the ceiling."""
    knee = 0.75 * ceiling
    a = np.abs(x)
    over = a > knee
    if not np.any(over):
        return x
    y = x.copy()
    room = ceiling - knee
    y[over] = np.sign(x[over]) * (knee + room * np.tanh((a[over] - knee) / room))
    return y


class HapticDSP:
    def __init__(self, fs, mode="smart", channel_map=None, master_gain_db=0.0, n_out=8, block=512):
        self.fs = fs
        self.mode = mode
        self.n_out = n_out
        if mode == "smart":
            self.sos = [
                signal.butter(3, (lo, hi), btype="bandpass", fs=fs, output="sos")
                for _, lo, hi in BANDS
            ]
            self.zi = [np.zeros((s.shape[0], 2, 2)) for s in self.sos]
        else:
            self.sos = [signal.butter(3, (20.0, 115.0), btype="bandpass", fs=fs, output="sos")]
            self.zi = [np.zeros((self.sos[0].shape[0], 2))]
        t = block / fs
        self.c_att = np.exp(-t / COMP_ATTACK_S)
        self.c_rel = np.exp(-t / COMP_RELEASE_S)
        self.c_lim_rel = np.exp(-t / LIMIT_RELEASE_S)
        self.gain_db = np.zeros(6)  # compressor state per band
        self.lvl_min, self.lvl_max = -60.0, -20.0
        self.prev_gain = np.zeros(6)  # start silent and ramp up
        self.lim = np.ones(n_out)  # limiter gain per output
        self.set_mix(channel_map or default_channel_map(n_out), master_gain_db)

    def set_mix(self, channel_map, master_gain_db=0.0):
        """channel_map: list of n_out lists of [source_name, gain_db]."""
        m = np.zeros((self.n_out, 6))
        for ch, entries in enumerate(channel_map[: self.n_out]):
            for src, gdb in entries:
                m[ch, SOURCES.index(src)] += 10 ** (gdb / 20)
        self.mix = m * 10 ** (master_gain_db / 20)

    def process(self, x):
        """x: float array (2, N) in [-1, 1]. Returns float32 array (n_out, N)."""
        n = x.shape[1]
        if self.mode == "smart":
            bands = []
            for i, sos in enumerate(self.sos):
                y, self.zi[i] = signal.sosfilt(sos, x, axis=-1, zi=self.zi[i])
                bands.append(y)
            # order: L-low, L-mid, L-high, R-low, R-mid, R-high
            y = np.stack(
                [
                    bands[0][0],
                    bands[1][0],
                    bands[2][0],
                    bands[0][1],
                    bands[1][1],
                    bands[2][1],
                ]
            )
        else:
            mono = 0.5 * (x[0] + x[1])
            b, self.zi[0] = signal.sosfilt(self.sos[0], mono, zi=self.zi[0])
            y = np.tile(b, (6, 1))
        y = y * PRE_GAIN

        # per-band compressor (4:1 above -20 dB), smoothed in dB with attack/release
        level = _db(np.sqrt(np.mean(y * y, axis=1)))
        over = np.maximum(level - COMP_THRESHOLD_DB, 0.0)
        target = -over * (1.0 - 1.0 / COMP_RATIO)
        coef = np.where(target < self.gain_db, self.c_att, self.c_rel)
        self.gain_db = self.gain_db * coef + target * (1.0 - coef)
        gain = 10 ** (self.gain_db / 20)

        if self.mode == "smart":
            # dynamic intensity from the overall (L+R)/2 level
            mid_lvl = float(
                np.clip(_db(np.sqrt(np.mean((0.5 * (x[0] + x[1])) ** 2))), -200.0, 0.0)
            )
            self.lvl_min = mid_lvl if mid_lvl < self.lvl_min else self.lvl_min * MIN_DECAY
            self.lvl_max = mid_lvl if mid_lvl > self.lvl_max else self.lvl_max * MAX_DECAY
            rng = max(self.lvl_max - self.lvl_min, 3.0)
            intensity = min(max((mid_lvl - self.lvl_min) / rng, 0.0), 1.0)
            g_int = 10 ** ((intensity * 6.0 - 3.0) / 20)
            gain[[1, 2, 4, 5]] *= g_int  # mid and high bands only
            # stereo balance: L - R level difference, limited to +/-6 dB
            bal = float(
                np.clip(
                    _db(np.sqrt(np.mean(x[0] ** 2))) - _db(np.sqrt(np.mean(x[1] ** 2))),
                    -6.0,
                    6.0,
                )
            )
            gain[:3] *= 10 ** (bal / 20)
            gain[3:] *= 10 ** (-bal / 20)

        ramp = np.linspace(0.0, 1.0, n, endpoint=False)[None, :]
        g = self.prev_gain[:, None] + (gain - self.prev_gain)[:, None] * ramp
        self.prev_gain = gain
        out = self.mix @ (y * g)

        # smooth limiter per output: fast attack, slow release, ramped (no block-edge steps)
        peak = np.max(np.abs(out), axis=1)
        want = np.minimum(1.0, LIMIT_CEILING / np.maximum(peak, EPS))
        new = np.where(want < self.lim, want, self.lim * self.c_lim_rel + want * (1.0 - self.c_lim_rel))
        lg = self.lim[:, None] + (new - self.lim)[:, None] * ramp
        self.lim = new
        out = soft_clip(out * lg, LIMIT_CEILING)  # catches the start of a block before the ramp lands
        return out.astype(np.float32)


def default_channel_map(n_out=8):
    """Pairs top to bottom: 1/2 head (high), 3/4 upper (mid),
    5/6 mid back (low), 7/8 legs (low) — maps onto muvi Gigaport pairs."""
    m = [
        [["L-high", 0.0]],
        [["R-high", 0.0]],
        [["L-mid", 0.0]],
        [["R-mid", 0.0]],
        [["L-low", 0.0]],
        [["R-low", 0.0]],
        [["L-low", 0.0]],
        [["R-low", 0.0]],
    ]
    return m[:n_out]
