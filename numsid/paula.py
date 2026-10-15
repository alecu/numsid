"""Amiga Paula sound chip and the A500 / A1200 audio output stage.

Paula plays 8-bit signed samples straight out of chip RAM by DMA.  Each
channel holds a byte for `period` colour clocks (3,546,895 Hz on a PAL
Amiga) and then outputs the next one, scaled by its volume (0..64).  A
channel plays the block its location and length registers pointed at when
its DMA started.  When the block runs out it reloads those registers, so a
replayer that has written a loop's address there in the meantime gets the
loop.  Channels 0 and 3 go to the left output, 1 and 2 to the right.

The output is a staircase whose steps fall on colour-clock boundaries.
Paula reads the period and the volume when it moves on to the next byte, so
changes to either start with the next byte.  The staircase is rendered at
the E clock (colour clock / 5, 709,379 Hz, the clock the CIA timers count),
and each rendered value is its exact sum over those five colour clocks,
computed in integers.  The two-stage resampler of the SID path then takes
it to the output rate, with the machine's fixed RC low-pass folded into its
kernel.  The "LED" filter is a 2-pole low-pass that software can switch in
(ProTracker's E0x).  It runs at the output rate, followed by the output
coupling high-pass.

The filters use the component values of the A500 (rev 6A) and A1200
(rev 1D4) boards, as collected by the pt2-clone project: a 360 ohm / 0.1 uF
low-pass (4.42 kHz) on the A500 and 680 ohm / 6.8 nF (34.4 kHz) on the
A1200, the same Sallen-Key LED filter on both (10 k / 6.8 nF / 3.9 nF:
3.09 kHz, Q 0.66), and a high-pass of about 5 Hz.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from .resample import Resampler
from .sid import SCOPE_SIZE

PAL_COLOR_CLOCK = 3546895
NTSC_COLOR_CLOCK = 3579545
BIN = 5                          # colour clocks per E-clock cycle (one rendered value)
MIN_PERIOD = 113                 # shorter periods outrun the DMA; clamped like PT clones do
MAX_SPAN = 1 << 16               # E-clock cycles per rendering batch

# fixed output low-pass and coupling high-pass, Hz
MODELS = {"A500": (4420.97, 5.128), "A1200": (34419.32, 5.319)}
LED_CUTOFF, LED_Q = 3090.53, 0.660


class Channel:
    """One DMA audio channel: the registers a program writes, and the DMA
    state that plays from them."""

    def __init__(self):
        self.lc = 0              # AUDxLC: location latch (byte address)
        self.len = 1             # AUDxLEN: length latch in words (0 means 65536)
        self.per = 0             # AUDxPER: colour clocks per byte
        self.vol = 0             # AUDxVOL: 0..64
        self.dma = False
        self.addr = 0            # the block being played: start address,
        self.size = 0            # length in bytes,
        self.pos = -1            # and index of the byte being output
        self.level = 0           # that byte times the volume it was output with
        self.next = 0            # colour clocks until the next byte starts

    @property
    def period(self) -> int:
        if self.per == 0:
            return 65536
        return max(self.per, MIN_PERIOD)

    def start(self) -> None:
        """DMA switched on: the latches are copied and the first byte starts now."""
        self.dma = True
        self.addr = self.lc
        self.size = 2 * (self.len or 65536)
        self.pos = -1
        self.next = 0

    def render(self, ram: np.ndarray, n: int) -> np.ndarray | None:
        """Advance n E-clock cycles.  Returns, for every cycle, the sum of
        byte x volume over its five colour clocks (int64), or None when the
        output is silent."""
        if not self.dma:
            return None
        span = BIN * n
        p = self.period
        s0 = self.next
        if s0 >= span:                            # no new byte in this span
            self.next = s0 - span
            return np.full(n, self.level * BIN, dtype=np.int64) if self.level else None
        k = (span - 1 - s0) // p + 1              # bytes that start in the span
        first = self.pos + 1
        avail = self.size - first
        if k <= avail:
            idx = np.arange(self.addr + first, self.addr + first + k)
            self.pos += k
        else:                                     # block ends: reload from the latches
            loop = 2 * (self.len or 65536)
            rest = k - avail
            j = np.arange(rest)
            if rest > loop:
                j %= loop
            idx = np.concatenate((np.arange(self.addr + first, self.addr + self.size),
                                  self.lc + j))
            self.addr, self.size, self.pos = self.lc, loop, (rest - 1) % loop
        vals = ram[idx % len(ram)].astype(np.int64) * self.vol
        prev = self.level
        self.level = int(vals[-1])
        self.next = s0 + k * p - span
        if not prev and not vals.any():
            return None
        # Bytes last at least MIN_PERIOD colour clocks, so at most one byte
        # boundary falls inside a cycle.  Each cycle gets five times the
        # level held at its start, and the cycles a boundary splits are
        # corrected by the part of them that already holds the new byte.
        levels = np.empty(k + 1, dtype=np.int64)
        levels[0] = prev                          # held from before this span
        levels[1:] = vals
        starts = s0 + p * np.arange(k, dtype=np.int64)   # where levels[1:] begin
        cycle = -(-starts // BIN)                 # first cycle starting at or after
        out = np.repeat(levels * BIN, np.diff(cycle, prepend=0, append=n))
        split = starts % BIN != 0
        if split.any():
            b = starts[split]
            j = b // BIN
            out[j] += (levels[1:][split] - levels[:-1][split]) * (BIN * (j + 1) - b)
        return out


class _Biquad:
    """A 2-pole IIR filter run a block at a time with matrix products: in
    each block the output is the zero-state response (a Toeplitz product)
    plus the response to the state carried in from the block before."""

    def __init__(self, b, a, block: int = 128):
        b0, b1, b2 = (x / a[0] for x in b)
        a1, a2 = a[1] / a[0], a[2] / a[0]
        # transposed direct form II as a state-space system
        A = np.array([[-a1, 1.0], [-a2, 0.0]])
        B = np.array([b1 - a1 * b0, b2 - a2 * b0])
        powers = [np.eye(2)]
        for _ in range(block):
            powers.append(A @ powers[-1])
        self.powers = np.array(powers)                  # A^j, j = 0..block
        self.pb = self.powers @ B                       # A^j B
        h = np.empty(block)
        h[0] = b0
        h[1:] = self.pb[:block - 1, 0]                  # impulse response
        i = np.arange(block)
        lag = i[:, None] - i[None, :]
        self.H = np.where(lag >= 0, h[np.clip(lag, 0, None)], 0.0)
        self.O = self.powers[:block, 0, :]              # C A^n
        self.block = block

    def run(self, x: np.ndarray, state: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        L = self.block
        nb = len(x) // L
        y = np.empty(len(x))
        s = state
        if nb:
            X = x[:nb * L].reshape(nb, L)
            zero_state = X @ self.H.T
            gathered = X @ self.pb[L - 1::-1]           # sum of A^(L-1-m) B x[m]
            AL = self.powers[L]
            starts = np.empty((nb, 2))
            for i in range(nb):
                starts[i] = s
                s = AL @ s + gathered[i]
            y[:nb * L] = (zero_state + starts @ self.O.T).ravel()
        r = len(x) - nb * L
        if r:
            xr = x[nb * L:]
            y[nb * L:] = self.H[:r, :r] @ xr + self.O[:r] @ s
            s = self.powers[r] @ s + xr @ self.pb[r - 1::-1]
        return y, s


def _lowpass_biquad(f0: float, q: float, fs: float) -> _Biquad:
    """Bilinear transform of a 2-pole low-pass, pre-warped at its cutoff."""
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2 * q)
    c = math.cos(w0)
    return _Biquad(((1 - c) / 2, 1 - c, (1 - c) / 2), (1 + alpha, -2 * c, 1 - alpha))


class Paula:
    def __init__(self, ram: bytes, sample_rate: int = 44100, model: str = "A500", *,
                 color_clock: int = PAL_COLOR_CLOCK, channels: int = 4,
                 separation: float = 1.0, filter_enabled: bool = True, scope_rate: int = 0):
        if model not in MODELS:
            raise ValueError(f"model must be one of {', '.join(MODELS)}")
        self.model = model
        self.color_clock = color_clock
        self.clock = color_clock // BIN           # E clock: rendering rate and unit of time
        self.fs = sample_rate
        self.filter_enabled = filter_enabled      # emulate the switchable LED filter
        self.ram0 = np.frombuffer(ram, dtype=np.int8)
        self.nchannels = channels
        lowpass, highpass = MODELS[model]
        self.resampler = Resampler(self.clock, sample_rate, channels=2,
                                   ext_lowpass=2 * math.pi * lowpass)
        self.led = _lowpass_biquad(LED_CUTOFF, LED_Q, sample_rate)
        self.hp_pole = math.exp(-2 * math.pi * highpass / sample_rate)
        self.mute = [False] * channels
        self.set_separation(separation)
        self.scope_step = max(1, self.clock // scope_rate) if scope_rate else 0
        self.scope = np.zeros((channels, SCOPE_SIZE), dtype=np.float32)
        self.scope_out = np.zeros(SCOPE_SIZE, dtype=np.float32)
        self.reset()

    def set_separation(self, separation: float) -> None:
        """Stereo width: 1 is the Amiga's hard left/right, 0 is mono."""
        s = min(max(separation, 0.0), 1.0)
        weight = 1.0 / math.ceil(self.nchannels / 2)
        near, far = weight * (1 + s) / 2, weight * (1 - s) / 2
        # channels 0 and 3 of every group of four are on the left
        self.gains = [(near, far) if i % 4 in (0, 3) else (far, near)
                      for i in range(self.nchannels)]

    def reset(self) -> None:
        self.ram = self.ram0.copy()               # replayers may write to sample memory
        self.voices = [Channel() for _ in range(self.nchannels)]
        self.t = 0
        self.led_on = False
        self.led_changes: deque = deque()         # (output sample index, on)
        self.led_active = False                   # LED state at the output position
        self.led_state = [np.zeros(2), np.zeros(2)]
        self.hp_state = [0.0, 0.0]
        self.resampler.reset()
        self.out: deque = deque()
        self.out_len = 0

    # ---- register interface (writes take effect at the current time) ---------
    def write_location(self, ch: int, address: int) -> None:
        self.voices[ch].lc = address

    def write_length(self, ch: int, words: int) -> None:
        self.voices[ch].len = words & 0xFFFF

    def write_period(self, ch: int, period: int) -> None:
        self.voices[ch].per = period & 0xFFFF       # used from the next byte on

    def write_volume(self, ch: int, volume: int) -> None:
        self.voices[ch].vol = min(volume & 0x7F, 64)

    def dma(self, mask: int, on: bool) -> None:
        for i, v in enumerate(self.voices):
            if mask >> i & 1:
                if not on:
                    v.dma = False
                elif not v.dma:
                    v.start()

    def set_led(self, on: bool) -> None:
        if on != self.led_on:
            self.led_on = on
            k = -((-self.t * self.fs) // self.clock)
            self.led_changes.append((k, on))

    # ---- rendering ------------------------------------------------------------------
    def advance(self, until: int) -> None:
        """Render up to (not including) E-clock cycle `until`."""
        while self.t < until:
            n = min(until - self.t, MAX_SPAN)
            self._render(n)
            self.t += n

    def take(self, n: int) -> np.ndarray:
        """Remove and return up to n rendered stereo frames, shape (n, 2)."""
        parts = []
        need = n
        while need > 0 and self.out:
            chunk = self.out[0]
            if len(chunk) <= need:
                parts.append(self.out.popleft())
                need -= len(chunk)
            else:
                parts.append(chunk[:need])
                self.out[0] = chunk[need:]
                need = 0
        self.out_len -= n - need
        if not parts:
            return np.zeros((0, 2), dtype=np.float32)
        return np.concatenate(parts)

    def _render(self, n: int) -> None:
        t0 = self.t
        step = self.scope_step
        if step:
            first = -t0 % step
            taps = range((t0 + first) // step, (t0 + n - 1) // step + 1)
            ring = np.arange(taps.start, taps.stop) % SCOPE_SIZE
        sides: list = [None, None]
        for i, v in enumerate(self.voices):
            sums = v.render(self.ram, n)
            if sums is None:
                if step:
                    self.scope[i, ring] = 0.0
                continue
            y = sums.astype(np.float32) * np.float32(1 / (BIN * 128 * 64))
            if step:
                self.scope[i, ring] = y[first::step]
            if self.mute[i]:
                continue
            for c, g in enumerate(self.gains[i]):
                if g:
                    sides[c] = y * g if sides[c] is None else sides[c] + y * g
        a, b = self.resampler.process(sides, n)
        if len(a):
            self._output(a, b)

    def _output(self, left: np.ndarray, right: np.ndarray) -> None:
        k0 = self.resampler.k - len(left)
        n = len(left)
        # where the LED filter is switched in, per output sample
        changes = self.led_changes
        led = None
        if self.led_active or (changes and changes[0][0] < k0 + n):
            led = np.full(n, self.led_active)
            while changes and changes[0][0] < k0 + n:
                k, on = changes.popleft()
                led[max(k - k0, 0):] = on
                self.led_active = on
        outs = []
        for c, x in enumerate((left, right)):
            x = x.astype(np.float64)
            if self.filter_enabled:
                f, self.led_state[c] = self.led.run(x, self.led_state[c])
                if led is not None:
                    x = np.where(led, f, x)
            # one-pole high-pass: y = x - lowpass(x), the low-pass as a
            # scaled cumulative sum (y[n] = p*y[n-1] + (1-p)*x[n])
            p = self.hp_pole
            y = np.empty_like(x)
            for s in range(0, n, 2048):
                seg = x[s:s + 2048]
                powers = p ** np.arange(1, len(seg) + 1)
                low = powers * (self.hp_state[c] + (1 - p) * np.cumsum(seg / powers))
                self.hp_state[c] = low[-1]
                y[s:s + 2048] = seg - low
            outs.append(y)
        chunk = np.stack(outs, axis=1).astype(np.float32)
        if self.scope_step:
            self.scope_out[np.arange(k0, k0 + n) % SCOPE_SIZE] = chunk.mean(axis=1)
        self.out.append(chunk)
        self.out_len += n
