"""The MT-32's reverb, as measured.

The MT-32 has four reverb modes (room, hall, plate, tap delay), a time of
0-7 and a level of 0-7; the factory setting is room, time 5, level 3.  The
reverb was measured the same way as the LA32 (see docs/mt32.md): its
impulse response was taken from Munt's reverb output streams and the
structure behind it worked out and fitted.  It is linear and time
invariant and has a mono input, the average of the two channels sent to
it.

Room, hall and plate are the same network with different delays:

    input -> one-pole lowpasses -> three allpasses (g = 0.5)
          -> three feedback delay lines, each with a one-pole lowpass in
             its loop, read out at one point for the left output and
             another for the right

The time setting only sets the loops' feedback.  Tap delay is a single
feedback delay line read out at d for the left and 2d for the right, so
the echoes go left, right, left...  The level scales the output; level 0
is not silent unless the time is 0 too.

Every delay is at least BLOCK samples, so a block's output depends only
on samples from earlier blocks and each block is computed with numpy
slices, no per-sample loop; the one-pole lowpasses are applied as short
FIR filters (their responses are gone to -300 dB in 48 samples).
"""

from __future__ import annotations

import numpy as np

ROOM, HALL, PLATE, TAP_DELAY = range(4)

BLOCK = 64                       # samples per step: no delay is shorter than this
MIN_DELAY = 1 << 30              # process() takes any length (kept for callers)
FIR_TAPS = 48

# per mode: input lowpasses, allpass delays, loop lowpass, and the three
# delay lines as (loop length, output gain, left tap, right tap)
NETWORKS = {
    ROOM: dict(pre=(0.40, 0.32), allpasses=(78, 729, 994), loop=0.374,
               lines=((2752, -0.400, 1263, 2648), (2040, -0.400, 2616, 1595),
                      (3629, -0.267, 2390, 577))),
    HALL: dict(pre=(0.36, 0.30), allpasses=(176, 809, 1324), loop=0.374,
               lines=((2619, -0.365, 3580, 2262), (3545, -0.365, 2722, 4494),
                      (4519, -0.243, 5480, 3236))),
    PLATE: dict(pre=(0.375,), allpasses=(157, 644, 969), loop=0.375,
                lines=((2839, -0.262, 835, 2245), (2259, -0.262, 2376, 1253),
                       (3539, -0.175, 1886, 118))),
}
ALLPASS_G = -0.5
# loop feedback by time; the shortest line of the three feeds back a little
# less at times 3-5
FEEDBACK = (0.25, 0.45, 0.60, 0.75, 0.80, 0.85, 0.90, 0.95)
FEEDBACK_SHORT = (0.25, 0.45, 0.60, 0.70, 0.75, 0.80, 0.90, 0.95)
# output gain by level (room, hall, plate; tap delay)
LEVEL = tuple(v / 14 for v in (1, 2, 3, 4, 5, 7, 10, 14))
TAP_LEVEL = tuple(v / 31 for v in (0.5, 1.5, 5, 8, 12, 16, 21, 31))
# tap delay: left tap by time (the right is at 2d - 2, the loop 2d - 3)
TAP_DELAYS = (402, 626, 962, 1490, 2258, 3474, 5282, 8002)
TAP_PRE, TAP_LOOP = 0.40, 0.408
TAP_GAIN = (-0.101,) + (-0.202,) * 7      # time 0 is quieter
TAP_FEEDBACK = (-0.686,) * 6 + (-0.634,) * 2


def _lowpass_fir(a: float) -> np.ndarray:
    """y = (1 - a) x + a y[-1], as an FIR."""
    return ((1 - a) * a ** np.arange(FIR_TAPS)).astype(np.float32)


class _Line:
    """A delay line's history: write a block, read any earlier stretch."""

    def __init__(self, length: int):
        size = 1
        while size < length + BLOCK:
            size *= 2
        self.buf = np.zeros(size, dtype=np.float32)
        self.mask = size - 1
        self.pos = 0                                 # where the next block goes

    def read(self, delay: int, n: int) -> np.ndarray:
        return self.buf[(self.pos - delay + np.arange(n)) & self.mask]

    def write(self, x: np.ndarray) -> None:
        self.buf[(self.pos + np.arange(len(x))) & self.mask] = x
        self.pos = (self.pos + len(x)) & self.mask


class _Lowpass:
    def __init__(self, a: float):
        self.fir = _lowpass_fir(a)
        self.hist = np.zeros(FIR_TAPS - 1, dtype=np.float32)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        h = np.concatenate([self.hist, x])
        self.hist = h[-(FIR_TAPS - 1):]
        return np.convolve(h, self.fir, "valid").astype(np.float32)


class Reverb:
    def __init__(self, mode: int = ROOM, time: int = 5, level: int = 3):
        self.set(mode, time, level)

    def set(self, mode: int, time: int, level: int) -> None:
        self.mode = min(max(mode, 0), 3)
        self.time, self.level = min(max(time, 0), 7), min(max(level, 0), 7)
        if self.mode == TAP_DELAY:
            d = TAP_DELAYS[self.time]
            self.pre = [_Lowpass(TAP_PRE)]
            self.allpasses = []
            # (loop length, feedback, output gain, left tap, right tap)
            self.lines = [(2 * d - 3, TAP_FEEDBACK[self.time], TAP_GAIN[self.time], d, 2 * d - 2)]
            self.loop_a = TAP_LOOP
            self.wet = TAP_LEVEL[self.level]
        else:
            net = NETWORKS[self.mode]
            self.pre = [_Lowpass(a) for a in net["pre"]]
            self.allpasses = [(d, _Line(d), _Line(d)) for d in net["allpasses"]]
            shortest = min(p for p, *_ in net["lines"])
            self.lines = [(p, -(FEEDBACK_SHORT if p == shortest else FEEDBACK)[self.time], c, dl, dr)
                          for p, c, dl, dr in net["lines"]]
            self.loop_a = net["loop"]
            self.wet = LEVEL[self.level]
        if self.time == 0 and self.level == 0:
            self.wet = 0.0                           # the one setting that turns it off
        # each line keeps its input-plus-feedback signal y and the lowpassed z
        self.y = [_Line(max(p, dl, dr)) for p, _, _, dl, dr in self.lines]
        self.z = [_Line(p) for p, *_ in self.lines]
        self.loop_lp = [_Lowpass(self.loop_a) for _ in self.lines]

    def process(self, left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The reverb's output (wet only) for a block of the send."""
        n = len(left)
        out_l = np.zeros(n, dtype=np.float32)
        out_r = np.zeros(n, dtype=np.float32)
        x = (np.asarray(left, dtype=np.float32) + np.asarray(right, dtype=np.float32)) * 0.5
        for s in range(0, n, BLOCK):
            l, r = self._step(x[s:s + BLOCK])
            out_l[s:s + BLOCK] = l
            out_r[s:s + BLOCK] = r
        return out_l * self.wet, out_r * self.wet

    def _step(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        n = len(x)
        for lp in self.pre:
            x = lp(x)
        for d, xin, yout in self.allpasses:
            y = -ALLPASS_G * x + xin.read(d, n) + ALLPASS_G * yout.read(d, n)
            xin.write(x)
            yout.write(y)
            x = y
        l = np.zeros(n, dtype=np.float32)
        r = np.zeros(n, dtype=np.float32)
        for (p, g, c, dl, dr), y_line, z_line, lp in zip(self.lines, self.y, self.z, self.loop_lp):
            y = x + g * z_line.read(p, n)
            y_line.write(y)
            z_line.write(lp(y))
            # (written first: a tap shorter than the line reads this block)
            l += c * y_line.read(dl + n, n)
            r += c * y_line.read(dr + n, n)
        return l, r
