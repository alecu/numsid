"""Reverb, standing in for the MT-32's BOSS reverb chip.

The MT-32 has four reverb modes (room, hall, plate, tap delay), a time of
0-7 and a level of 0-7; the factory setting is room, time 5, level 3.  The
chip itself is not emulated yet.  This is a Schroeder reverb per channel
(parallel feedback combs, then series allpasses) with delay lengths and
feedback chosen per mode, and tap delay as a plain feedback echo.  Its
input loses the bass below about 100 Hz and its level is set to sit about
10 dB under the dry sound at the factory setting, as Munt's does.

Every delay is longer than the block size, so a block's output only
depends on samples from earlier blocks and each block is computed with
numpy slices, no per-sample loop.
"""

from __future__ import annotations

import numpy as np

ROOM, HALL, PLATE, TAP_DELAY = range(4)

# comb and allpass delays in samples at 32 kHz, for the left channel; the
# right channel's are a little longer, for width
MODES = {
    ROOM:  dict(combs=(810, 862, 927, 984), allpasses=(403, 320), size=0.85),
    HALL:  dict(combs=(1213, 1297, 1381, 1459), allpasses=(531, 409), size=1.0),
    PLATE: dict(combs=(689, 733, 797, 853), allpasses=(347, 263), size=0.9),
}
SPREAD = 17
MIN_DELAY = 256                  # the block size must not exceed this
HIGHPASS = 200                   # samples averaged and taken away: no boom below ~100 Hz
WET = 0.40                       # wet level at reverb level 7 (Munt's reverb is about
                                 # 10 dB under the dry signal at the factory setting)


class _Delay:
    """A ring buffer read `length` samples behind the write position."""

    def __init__(self, length: int):
        self.buf = np.zeros(length, dtype=np.float32)
        self.pos = 0

    def read(self, n: int) -> np.ndarray:
        idx = (self.pos + np.arange(n)) % len(self.buf)
        return self.buf[idx]

    def write(self, x: np.ndarray) -> None:
        idx = (self.pos + np.arange(len(x))) % len(self.buf)
        self.buf[idx] = x
        self.pos = (self.pos + len(x)) % len(self.buf)


class Reverb:
    def __init__(self, mode: int = ROOM, time: int = 5, level: int = 3):
        self.set(mode, time, level)

    def set(self, mode: int, time: int, level: int) -> None:
        self.mode, self.time, self.level = mode, min(max(time, 0), 7), min(max(level, 0), 7)
        self.wet = self.level / 7 * WET
        self.hist = [np.zeros(HIGHPASS, dtype=np.float32) for _ in range(2)]
        if mode == TAP_DELAY:
            self.echo = [_Delay(3200 + 1600 * self.time + c * 800) for c in range(2)]
            self.feedback = 0.3 + 0.05 * self.time
            return
        m = MODES[mode]
        self.feedback = min(0.6 + 0.04 * self.time * m["size"] + 0.08, 0.93)
        self.combs = [[_Delay(d + c * SPREAD) for d in m["combs"]] for c in range(2)]
        self.allpasses = [[_Delay(d + c * SPREAD) for d in m["allpasses"]] for c in range(2)]

    def process(self, left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The reverb's output (wet only) for a block of at most MIN_DELAY."""
        n = len(left)
        assert n <= MIN_DELAY
        out = []
        for c, x in enumerate((left, right)):
            if not self.wet:
                out.append(np.zeros(n, dtype=np.float32))
                continue
            # take the bass out: less a moving average, centred
            x = x.astype(np.float32)
            h = np.concatenate([self.hist[c], x])
            self.hist[c] = h[-HIGHPASS:]
            c_ = np.cumsum(np.concatenate([[0.0], h]))
            mid = h[HIGHPASS // 2:HIGHPASS // 2 + len(x)]   # in step with the average
            x = mid - ((c_[HIGHPASS + 1:] - c_[1:len(x) + 1]) / HIGHPASS).astype(np.float32)
            x2 = x
            if self.mode == TAP_DELAY:
                d = self.echo[c]
                y = d.read(n)
                d.write(x2 + self.feedback * y)
                out.append(y * self.wet)
                continue
            acc = np.zeros(n, dtype=np.float32)
            for d in self.combs[c]:
                y = d.read(n)
                d.write(x2 * 0.25 + self.feedback * y)
                acc += y
            for d in self.allpasses[c]:
                buf = d.read(n)
                d.write(acc + 0.5 * buf)
                acc = buf - acc
            out.append(acc * self.wet)
        return out[0], out[1]
