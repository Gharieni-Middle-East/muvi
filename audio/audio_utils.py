"""Shared audio helpers."""

from __future__ import annotations

import numpy as np
from scipy.signal import firwin, resample_poly

TARGET_SR = 44100
# ~20 minutes at 44.1 kHz keeps memory reasonable on Raspberry Pi 4.
MAX_SAMPLES = TARGET_SR * 60 * 20


def ensure_stereo(audio: np.ndarray) -> np.ndarray:
    """Convert mono (1-D or Nx1) audio to stereo Nx2."""
    if audio.ndim == 1:
        return np.column_stack([audio, audio]).astype(np.float32)
    if audio.shape[1] == 1:
        return np.repeat(audio, 2, axis=1).astype(np.float32)
    if audio.shape[1] > 2:
        return audio[:, :2].astype(np.float32)
    return audio.astype(np.float32)


def to_mono(audio: np.ndarray) -> np.ndarray:
    if audio.ndim == 1:
        return audio.astype(np.float32)
    return audio.mean(axis=1).astype(np.float32)


def resample_if_needed(data: np.ndarray, src_sr: int, target_sr: int) -> np.ndarray:
    if src_sr == target_sr:
        return data.astype(np.float32)
    gcd = np.gcd(src_sr, target_sr)
    up = target_sr // gcd
    down = src_sr // gcd
    return resample_poly(data, up, down, axis=0).astype(np.float32)


class StreamResampler:
    """Polyphase resampler that keeps its filter state between blocks.

    `resample_if_needed` treats every block as an isolated signal, so on a live stream it rings
    at each block edge (audible clicks / transducer rattle every block) and rounds each block's
    length up, adding samples that make the output ring slowly overflow. This class resamples a
    continuous stream: same filter quality as resample_poly, no block edges, and the total
    output length tracks the exact rate ratio (no drift). Output length per call varies by
    one sample; latency is constant (half the filter length).
    """

    def __init__(self, src_sr: int, dst_sr: int, channels: int) -> None:
        g = int(np.gcd(int(src_sr), int(dst_sr)))
        self.src_sr, self.dst_sr, self.channels = int(src_sr), int(dst_sr), int(channels)
        self.up, self.down = self.dst_sr // g, self.src_sr // g
        self.passthrough = self.up == self.down
        if self.passthrough:
            return
        # Same design rule as scipy's resample_poly: Kaiser(5) low-pass at the narrower Nyquist.
        half = 10 * max(self.up, self.down)
        h = firwin(2 * half + 1, 1.0 / max(self.up, self.down), window=("kaiser", 5.0)) * self.up
        self.taps = int(np.ceil(len(h) / self.up))           # input samples per output sample
        h = np.concatenate([h, np.zeros(self.taps * self.up - len(h))])
        # polyphase table: output phase p uses h[p + j*up] against x[base - j]
        self.poly = h.reshape(self.taps, self.up).T.astype(np.float32)   # (up, taps)
        self.hist = np.zeros((self.taps - 1, self.channels), dtype=np.float32)
        self.n_in = 0       # input samples consumed so far
        self.m = 0          # next output sample index

    def reset(self) -> None:
        if not self.passthrough:
            self.hist[:] = 0.0
            self.n_in = 0
            self.m = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        """x: (n, channels) at src_sr -> (m, channels) at dst_sr."""
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 1:
            x = x.reshape(-1, 1)
        if self.passthrough or len(x) == 0:
            return x.astype(np.float32, copy=False)
        buf = np.concatenate([self.hist, x], axis=0)        # buf[i] is input index n_in-(taps-1)+i
        total = self.n_in + len(x)
        # outputs whose newest input has arrived: floor(m*down/up) <= total-1  <=>  m*down < total*up
        m_end = (total * self.up + self.down - 1) // self.down
        if m_end > self.m:
            ms = np.arange(self.m, m_end, dtype=np.int64)
            pos = ms * self.down
            base, phase = pos // self.up, pos % self.up
            idx = (base - (self.n_in - (self.taps - 1)))[:, None] - np.arange(self.taps)[None, :]
            y = np.einsum("mk,mkc->mc", self.poly[phase], buf[idx])
        else:
            y = np.zeros((0, self.channels), dtype=np.float32)
        self.m = max(self.m, m_end)
        self.n_in = total
        self.hist = buf[len(buf) - (self.taps - 1):]
        return y.astype(np.float32, copy=False)


def fit_frames(signal: np.ndarray, frames: int) -> np.ndarray:
    """Trim or zero-pad a 1-D block to an exact frame count."""
    flat = np.asarray(signal, dtype=np.float32).reshape(-1)
    if len(flat) == frames:
        return flat
    if len(flat) > frames:
        return flat[:frames]
    out = np.zeros(frames, dtype=np.float32)
    out[: len(flat)] = flat
    return out
