"""Band-limited decimation from the SID clock (~1 MHz) to an audio rate.

The SID is emulated at one sample per clock cycle, which is exact but far
above audio rates; naive decimation would alias badly (pulse and sawtooth
waves are full of harmonics).  We decimate in two stages:

1. an integer factor `d1` (8 for 44.1/48 kHz) with a short Kaiser FIR,
   evaluated in polyphase form,
2. a rational step to the output rate using a polyphase windowed-sinc table
   with one row per distinct fractional phase (1225 rows for PAL -> 44.1 kHz).

The C64's output RC low-pass (~16 kHz) is folded into the stage-2 kernel,
so it costs nothing extra.  Output sample k corresponds to SID cycle
k * clock / fs.  A channel can be passed as None when it is silent; while its
history is silent too, no arithmetic is done for it.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

DTYPE = np.float32


def _kaiser_beta(atten_db: float) -> float:
    if atten_db > 50:
        return 0.1102 * (atten_db - 8.7)
    if atten_db >= 21:
        return 0.5842 * (atten_db - 21) ** 0.4 + 0.07886 * (atten_db - 21)
    return 0.0


def _kaiser_taps(atten_db: float, transition: float) -> int:
    """Kaiser's length estimate; `transition` in radians/sample."""
    return int(math.ceil((atten_db - 7.95) / (2.285 * transition))) + 1


class Resampler:
    def __init__(self, clock: int, fs: int, channels: int = 2, *,
                 passband: float = 19_000.0, ext_lowpass: float | None = 100_000.0,
                 atten1: float = 90.0, atten2: float = 85.0):
        self.clock = clock
        self.fs = fs
        self.channels = channels
        self.d1 = d1 = max(1, int(clock // (2.5 * fs)))
        r1 = clock / d1
        passband = min(passband, 0.45 * fs)

        # ---- stage 1: FIR at the SID clock, keep every d1-th output --------
        stop1 = r1 - passband
        n1 = _kaiser_taps(atten1, 2 * math.pi * (stop1 - passband) / clock)
        n1 += n1 % 2
        fc1 = 0.5 * (passband + stop1) / clock
        t = np.arange(n1) - (n1 - 1) / 2
        h1 = 2 * fc1 * np.sinc(2 * fc1 * t) * np.kaiser(n1, _kaiser_beta(atten1))
        h1 /= h1.sum()
        self.n1 = n1
        nq = -(-n1 // d1)                        # polyphase rows of d1 taps
        hp = np.zeros(nq * d1)
        hp[:n1] = h1
        self.h1_poly = hp.reshape(nq, d1).astype(DTYPE)

        # ---- stage 2: polyphase windowed sinc, r1 -> fs ---------------------
        stop2 = fs - passband                    # aliases land above `passband`
        n2 = _kaiser_taps(atten2, 2 * math.pi * (stop2 - passband) / r1)
        n2 += n2 % 2
        half = n2 // 2
        fc2 = 0.5 * (passband + stop2) / r1      # cycles per stage-1 sample
        beta2 = _kaiser_beta(atten2)

        # kernel on a fine grid (1/64 of a stage-1 sample), optionally convolved
        # with the external one-pole low-pass (impulse response w*exp(-w*t))
        step = 1 / 64
        tail = 0
        if ext_lowpass:
            w = ext_lowpass / r1                 # rad per stage-1 sample
            tail = int(math.ceil(12 / w))        # e^-12: ~ -104 dB
        grid = np.arange(-half, half + tail + step, step)
        inside = np.abs(grid) <= half
        g = np.zeros_like(grid)
        tg = grid[inside]
        g[inside] = (2 * fc2 * np.sinc(2 * fc2 * tg)
                     * np.i0(beta2 * np.sqrt(np.clip(1 - (tg / half) ** 2, 0, None)))
                     / np.i0(beta2))
        if ext_lowpass:
            h = np.exp(-w * np.arange(0, tail + step, step))
            g = np.convolve(g, h / h.sum())[:len(grid)]
        self.ntaps = ntaps = n2 + tail
        self.lookahead = half

        # positions: output k sits at stage-1 index k * num / den
        num, den = clock, d1 * fs
        gcd = math.gcd(num, den)
        self.num, self.den = num // gcd, den // gcd
        self.phases = self.den if self.den <= 8192 else 4096
        frac = np.arange(self.phases) / self.phases
        # tap j weights x1[base + half - j], at offset tau = frac - half + j
        tau = frac[:, None] - half + np.arange(ntaps)[None, :]
        table = np.interp(tau, grid, g, left=0.0, right=0.0)
        table /= table.sum(axis=1, keepdims=True)           # exact unity DC gain
        # reversed, so a row lines up with the forward window
        # x1[base + half - (ntaps - 1) : base + half + 1]
        self.table = np.ascontiguousarray(table[:, ::-1], dtype=DTYPE)
        self.reset()

    def reset(self) -> None:
        pre = (self.n1 - 1) // 2          # centre stage-1 sample m on cycle d1*m
        self.buf1 = [np.zeros(pre, DTYPE) for _ in range(self.channels)]
        self.buf2 = [np.zeros(self.ntaps, DTYPE) for _ in range(self.channels)]
        self.quiet = [True] * self.channels   # buffered history is all zero
        self.s0 = -self.ntaps             # stage-1 index of buf2[c][0]
        self.k = 0                        # next output sample index

    def fill(self, c: int, value: float) -> None:
        """Make channel c's history a constant (used to start at a DC level)."""
        self.buf1[c][:] = value
        self.buf2[c][:] = value
        self.quiet[c] = value == 0

    def _stage1(self, c: int, x: np.ndarray | None, n: int) -> np.ndarray:
        d1 = self.d1
        nq = self.h1_poly.shape[0]
        total = len(self.buf1[c]) + n
        blocks = total // d1
        m = max(0, blocks - nq + 1)
        if x is None and self.quiet[c]:
            self.buf1[c] = np.zeros(total - m * d1, DTYPE)
            return np.zeros(m, DTYPE)
        buf = np.concatenate((self.buf1[c], np.zeros(n, DTYPE) if x is None else x))
        self.buf1[c] = buf[m * d1:]
        if m == 0:
            return buf[:0]
        xr = buf[:blocks * d1].reshape(blocks, d1)
        y = xr[:m] @ self.h1_poly[0]
        for q in range(1, nq):
            y += xr[q:q + m] @ self.h1_poly[q]
        return y

    def process(self, x: list) -> list[np.ndarray]:
        """Feed one array (or None for silence) per channel at the SID clock.
        Returns one float32 array per channel at the output rate, with as many
        samples as can be computed so far."""
        n = next((len(a) for a in x if a is not None), None)
        if n is None:
            raise ValueError("at least one channel must carry samples")
        x1 = [np.concatenate((self.buf2[c], self._stage1(c, x[c], n)))
              for c in range(self.channels)]
        s_end = self.s0 + len(x1[0])
        last = s_end - 1 - self.lookahead          # newest usable output position
        k_end = -((-(last + 1) * self.den) // self.num)
        out = []
        if k_end > self.k:
            ks = np.arange(self.k, k_end, dtype=np.int64)
            prod = ks * self.num
            base = prod // self.den
            phase = prod % self.den
            if self.phases != self.den:
                phase = phase * self.phases // self.den
            rows = base + self.lookahead - (self.ntaps - 1) - self.s0
            coeffs = None
            for c in range(self.channels):
                if x[c] is None and self.quiet[c]:
                    out.append(np.zeros(len(ks), DTYPE))
                    continue
                if coeffs is None:
                    coeffs = self.table[phase]
                win = sliding_window_view(x1[c], self.ntaps)[rows]
                out.append(np.einsum("kj,kj->k", win, coeffs))
            self.k = k_end
            keep = (k_end * self.num) // self.den + self.lookahead - (self.ntaps - 1)
            drop = keep - self.s0
            if drop > 0:
                x1 = [b[drop:] for b in x1]
                self.s0 += drop
        else:
            out = [np.zeros(0, DTYPE)] * self.channels
        self.buf2 = x1
        for c in range(self.channels):
            if x[c] is not None:
                self.quiet[c] = False
            elif not self.quiet[c] and not self.buf1[c].any() and not x1[c].any():
                self.quiet[c] = True
        return out
