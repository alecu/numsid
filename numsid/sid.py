"""MOS 6581 / 8580 SID emulation in Python + numpy.

The chip is simulated at its native clock (one sample per CPU cycle, about
1 MHz).  Register writes carry the cycle at which the CPU made them and are
queued; `advance(t)` then renders everything up to cycle `t` with numpy,
splitting each voice's timeline only where *that* voice's registers change.

Per voice:
  * 24-bit phase accumulator, hard sync and ring modulation,
  * triangle / sawtooth / pulse / noise waveforms.  Noise is the real 23-bit
    LFSR (clocked by accumulator bit 19), looked up in a precomputed table of
    its full 2^23-1 period.  Combined waveforms use a pull-down model,
  * ADSR envelope: rate counter with 15-bit wrap-around ("ADSR delay bug"),
    exponential decay counter, sustain comparator, freeze at zero.  It is
    stepped event by event in Python and expanded to per-cycle values.

Mixer and filter: voices are routed either directly or through the filter,
volume is applied per cycle (so $D418 "digi" tricks work), the 6581 DC offsets
are modelled, and the 12 dB/oct state-variable filter runs at the output rate
with cutoff curves measured from real chips.  Finally the C64's output RC
filters (16 Hz high-pass, 16 kHz low-pass) are applied.

Timing and chip behaviour follow reSID 0.16 by Dag Lem, the de-facto
reference, but none of its code is used: this is a from-scratch Python model.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from .resample import Resampler

MASK24 = 0xFFFFFF
MAX_SPAN = 30000                 # cycles per numpy batch (keeps freq * n within int32)
SCOPE_SIZE = 1 << 16             # samples kept per display tap

# Envelope rate counter periods (cycles per step) for the 16 A/D/R settings
RATE_PERIOD = [9, 32, 63, 95, 149, 220, 267, 313, 392, 977, 1954, 3126, 3907, 11720,
               19532, 31251]
SUSTAIN_LEVEL = [i * 0x11 for i in range(16)]
# Exponential decay: counter values where the step divider changes
EXP_PERIOD_AT = [0] * 256
for _level, _period in ((0xFF, 1), (0x5D, 2), (0x36, 4), (0x1A, 8), (0x0E, 16), (0x06, 30),
                        (0x00, 1)):
    EXP_PERIOD_AT[_level] = _period

ATTACK, DECAY_SUSTAIN, RELEASE = 0, 1, 2

# Filter cutoff (FC register, 0..2047) to frequency in Hz, measured on real chips
# (point tables as published with reSID).  The 6581 has a step at FC=1024.
F0_POINTS_6581 = [
    (0, 220), (128, 230), (256, 250), (384, 300), (512, 420), (640, 780), (768, 1600),
    (832, 2300), (896, 3200), (960, 4300), (992, 5000), (1008, 5400), (1016, 5700),
    (1023, 6000), (1024, 4600), (1032, 4800), (1056, 5300), (1088, 6000), (1120, 6600),
    (1152, 7200), (1280, 9500), (1408, 12000), (1536, 14500), (1664, 16000),
    (1792, 17100), (1920, 17700), (2047, 18000)]
F0_POINTS_8580 = [
    (0, 0), (128, 800), (256, 1600), (384, 2500), (512, 3300), (640, 4100), (768, 4800),
    (896, 5600), (1024, 6500), (1152, 7500), (1280, 8400), (1408, 9200), (1536, 9800),
    (1664, 10500), (1792, 11000), (1920, 11700), (2047, 12500)]

# Combined waveform pull-down model: (threshold, pulse strength, distance to
# higher bits, distance to lower bits) for waveforms ST, PT, PS, PST.  These
# are the fitted parameters used by libsidplayfp (from samples by kevtris).
COMBINED_PARAMS = {
    "6581": {3: (0.862147212, 0.0, 10.8962431, 2.50848103),
             5: (0.932746708, 2.07508397, 1.03668225, 1.14876997),
             6: (0.860927045, 2.43506575, 0.908603609, 1.07907593),
             7: (0.741343081, 0.0452554375, 1.1439606, 1.05711341)},
    "8580": {3: (0.715788841, 0.0, 1.32999945, 2.2172699),
             5: (0.93500334, 1.05977178, 1.08629429, 1.43518543),
             6: (0.920648575, 0.943601072, 1.13034654, 1.41881108),
             7: (0.90921098, 0.979807794, 1.04885304, 1.48537755)},
}

_AR = np.arange(MAX_SPAN + 1, dtype=np.int32)


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

NOISE_PERIOD = (1 << 23) - 1
_noise_table: np.ndarray | None = None


def noise_table() -> np.ndarray:
    """Noise waveform output (top 8 of 12 bits) for every LFSR step.

    Index i is the state after i shifts from the reset value 0x7FFFF8.
    The LFSR feeds back bit22 ^ bit17, so its output bit sequence obeys
    b[n] = b[n-23] ^ b[n-18], and (squaring the characteristic polynomial
    over GF(2)) also b[n] = b[n-23*2^k] ^ b[n-18*2^k] for any k, which lets
    numpy generate the 8M-step period in a few dozen vector operations.
    """
    global _noise_table
    if _noise_table is not None:
        return _noise_table
    n = NOISE_PERIOD + 23
    b = np.zeros(n, dtype=np.uint8)
    for i in range(23):                       # state bit i lives at b[t + 22 - i]
        b[22 - i] = (0x7FFFF8 >> i) & 1
    size = 23
    while size < n:
        k = 0
        while 23 << (k + 1) <= size:
            k += 1
        step = min(18 << k, n - size)
        far, near = size - (23 << k), size - (18 << k)
        b[size:size + step] = b[far:far + step] ^ b[near:near + step]
        size += step
    assert np.array_equal(b[NOISE_PERIOD:NOISE_PERIOD + 23], b[:23])
    out = np.zeros(NOISE_PERIOD, dtype=np.uint8)
    for j, bit in enumerate((22, 20, 16, 13, 11, 7, 4, 2)):   # -> output bits 11..4
        out |= b[22 - bit:22 - bit + NOISE_PERIOD] << (7 - j)
    _noise_table = out
    return out


def _pulldown_table(threshold, pulse_strength, dist_hi, dist_lo) -> np.ndarray:
    """Combined waveforms: the selected waveform outputs are wired together,
    so a bit only stays high if neighbouring low bits don't drag it down."""
    dist = np.empty(25)
    dist[12] = 1.0
    for i in range(1, 13):
        dist[12 - i] = 1.0 / dist_hi ** i
        dist[12 + i] = 1.0 / dist_lo ** i
    bits = ((np.arange(4096)[:, None] >> np.arange(12)) & 1).astype(np.float64)
    out = np.zeros(4096, dtype=np.int64)
    for sb in range(12):
        others = np.array([cb for cb in range(12) if cb != sb])
        w = dist[sb - others + 12]
        pull = (((1.0 - bits[:, others]) * w).sum(axis=1) - pulse_strength) / w.sum()
        high = (bits[:, sb] > 0) & (1.0 - pull > threshold)
        out |= high.astype(np.int64) << sb
    return out


def _cutoff_table(points, fs: float) -> np.ndarray:
    fc = np.arange(2048)
    xs = np.array([p[0] for p in points], dtype=np.float64)
    ys = np.array([p[1] for p in points], dtype=np.float64)
    split = np.nonzero(np.diff(xs) == 1)[0]
    table = np.interp(fc, xs, ys)
    for i in split:                           # keep the 6581 discontinuity sharp
        if ys[i + 1] < ys[i]:
            lo = fc <= xs[i]
            table[lo] = np.interp(fc[lo], xs[:i + 1], ys[:i + 1])
            hi = fc >= xs[i + 1]
            table[hi] = np.interp(fc[hi], xs[i + 1:], ys[i + 1:])
    return np.minimum(table, 0.45 * fs)


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

class Voice:
    """Registers plus oscillator and envelope state of one SID voice."""

    def __init__(self):
        self.freq = self.pw = self.control = self.ad = self.sr = 0
        # oscillator
        self.acc = 0
        self.lfsr = 0          # index into noise_table(); 0 = reset state 0x7FFFF8
        self.test = 0          # test bit as seen by the oscillator at span start
        # envelope
        self.env = 0
        self.state = RELEASE
        self.rate_counter = 0
        self.rate_period = RATE_PERIOD[0]
        self.exp_counter = 0
        self.exp_period = 1
        self.hold_zero = True

    def write(self, reg: int, value: int) -> None:
        if reg == 0:
            self.freq = (self.freq & 0xFF00) | value
        elif reg == 1:
            self.freq = (self.freq & 0x00FF) | (value << 8)
        elif reg == 2:
            self.pw = (self.pw & 0xF00) | value
        elif reg == 3:
            self.pw = (self.pw & 0x0FF) | ((value & 0x0F) << 8)
        elif reg == 4:
            gate_was = self.control & 1
            self.control = value
            if value & 1 and not gate_was:
                self.state = ATTACK
                self.rate_period = RATE_PERIOD[self.ad >> 4]
                self.hold_zero = False
            elif gate_was and not value & 1:
                self.state = RELEASE
                self.rate_period = RATE_PERIOD[self.sr & 0x0F]
        elif reg == 5:
            self.ad = value
            if self.state == ATTACK:
                self.rate_period = RATE_PERIOD[value >> 4]
            elif self.state == DECAY_SUSTAIN:
                self.rate_period = RATE_PERIOD[value & 0x0F]
        elif reg == 6:
            self.sr = value
            if self.state == RELEASE:
                self.rate_period = RATE_PERIOD[value & 0x0F]

    def run_envelope(self, n: int, vals: list, lens: list) -> None:
        """Clock the envelope generator for n cycles with the current
        registers, appending (value, run length) pairs covering the n cycles.

        Rather than ticking every cycle, it jumps from one counter step to
        the next: the rate counter fires every `rate_period` cycles and, in
        decay/release, every `exp_period`-th firing steps the counter."""
        pos = 0
        run_start = 0
        env = cur = self.env
        while True:
            rp = self.rate_period
            rc = self.rate_counter
            # cycles until the rate counter next equals rp (it wraps at 2^15)
            d = rp - rc if rc < rp else 0x7FFF - rc + rp
            state = self.state
            frozen = self.hold_zero or (state == DECAY_SUSTAIN
                                        and env == SUSTAIN_LEVEL[self.sr >> 4])
            if not frozen:
                need = 1 if state == ATTACK else self.exp_period - self.exp_counter
                cycles = d + (need - 1) * rp
                if pos + cycles <= n:
                    pos += cycles
                    self.rate_counter = 0
                    self.exp_counter = 0
                    if state == ATTACK:
                        env = (env + 1) & 0xFF
                        if env == 0xFF:
                            self.state = DECAY_SUSTAIN
                            self.rate_period = RATE_PERIOD[self.ad & 0x0F]
                    else:
                        env = (env - 1) & 0xFF
                    period = EXP_PERIOD_AT[env]
                    if period:
                        self.exp_period = period
                        if env == 0:
                            self.hold_zero = True
                    at = pos - 1
                    if at > run_start:
                        vals.append(cur)
                        lens.append(at - run_start)
                        run_start = at
                    cur = env
                    continue
            # no further step within this call: just advance the counters
            r = n - pos
            if r < d:
                rc += r
                self.rate_counter = rc if rc <= 0x7FFF else rc - 0x7FFF
            else:
                ticks = 1 + (r - d) // rp
                self.rate_counter = (r - d) - (ticks - 1) * rp
                if state == ATTACK:
                    self.exp_counter = 0
                else:
                    self.exp_counter = (self.exp_counter + ticks) % self.exp_period
            break
        self.env = env
        if n > run_start:
            vals.append(cur)
            lens.append(n - run_start)


# ---------------------------------------------------------------------------
# The chip
# ---------------------------------------------------------------------------

class SID:
    def __init__(self, clock: int = 985248, sample_rate: int = 44100, model: str = "6581",
                 *, filter_enabled: bool = True, scope_rate: int = 0):
        if model not in ("6581", "8580"):
            raise ValueError("model must be '6581' or '8580'")
        self.clock = clock
        self.fs = sample_rate
        self.model = model
        self.filter_enabled = filter_enabled
        self.noise = noise_table()
        self.combined = {wf: _pulldown_table(*p) for wf, p in COMBINED_PARAMS[model].items()}
        if model == "6581":
            self.wave_zero = 0x380            # DC offset of the waveform DAC
            self.voice_dc = 0x800 * 0xFF      # DC offset of the envelope DAC
            self.mixer_dc = -0xFFF * 0xFF // 18
            self.f0 = _cutoff_table(F0_POINTS_6581, sample_rate)
        else:
            self.wave_zero = 0x800
            self.voice_dc = 0
            self.mixer_dc = 0
            self.f0 = _cutoff_table(F0_POINTS_8580, sample_rate)
        self.resampler = Resampler(clock, sample_rate, channels=2)
        # full scale: three voices at maximum amplitude and volume 15
        self.scale = 1.0 / (4095 * 255 * 3 * 15)
        hp = 100.0                            # C64 output high-pass, rad/s (~16 Hz)
        self.hp_pole = math.exp(-hp / sample_rate)
        self.mute = [False, False, False]
        self.regs = bytearray(32)             # last value written to each register
        # Optional display taps: ring buffers of recent samples, indexed by
        # absolute position (SID cycle // scope_step, output sample index).
        self.scope_step = max(1, clock // scope_rate) if scope_rate else 0
        self.scope = np.zeros((3, SCOPE_SIZE), dtype=np.float32)
        self.scope_out = np.zeros(SCOPE_SIZE, dtype=np.float32)
        self.capture: list | None = None      # testing hook
        self.reset()

    def reset(self) -> None:
        self.voices = [Voice(), Voice(), Voice()]
        self.t = 0
        self.pending: list[tuple[int, int, int]] = []
        self.fc = self.res = self.filt = self.mode = self.vol = 0
        self.voice3off = False
        self.resampler.reset()
        self.params = (0, 0, 0, 0)            # (fc, res, mode, vol) for output samples
        self.param_changes: deque = deque()   # (output sample index, params)
        self.ic1 = self.ic2 = 0.0             # filter integrator states
        self.hp_state = 0.0
        # Start-up DC compensation (see settle): offset subtracted from the
        # direct path, the level it is measured against, and until when it
        # keeps tracking volume/routing changes.
        self.dc_comp = 0.0
        self.dc_vol = 0
        self.dc_changes = 0
        self.dc_track_until = 0
        self.out: deque = deque()
        self.out_len = 0

    # ---- CPU interface ------------------------------------------------------
    def write(self, reg: int, value: int, cycle: int) -> None:
        self.pending.append((cycle, reg & 0x1F, value & 0xFF))
        self.regs[reg & 0x1F] = value & 0xFF

    def read(self, reg: int, cycle: int) -> int:
        if reg == 0x1B or reg == 0x1C:
            self.advance(cycle)
            if reg == 0x1C:
                return self.voices[2].env
            return self._osc3()
        if reg in (0x19, 0x1A):               # paddles: nothing connected
            return 0xFF
        return 0                              # write-only registers

    def settle(self, track_seconds: float = 1.0) -> None:
        """Pre-charge all filters to the chip's current DC level and drop any
        rendered output.  Called after a tune's init routine: setting the
        volume on a 6581 shifts its large DC offset, which would otherwise
        cause a loud thump on the C64's output capacitor at song start.

        Many tunes only set the volume in their first play calls (Cobra even
        writes 0 there and fades in), so for `track_seconds` afterwards the
        DC step caused by each volume change is absorbed into a constant
        offset.  After that (or as soon as rapid volume writes reveal sample
        playback) the offset is frozen and volume changes act naturally,
        which `$D418` sample playback relies on."""
        self.advance(self.t)
        routed = self.filt if self.filter_enabled else 0
        n_filt = bin(routed & 7).count("1")
        n_direct = 3 - n_filt - (1 if self.voice3off and not routed & 4 else 0)
        direct_dc = (self.mixer_dc + n_direct * self.voice_dc) * self.vol
        filt_dc = n_filt * self.voice_dc
        self.resampler.fill(0, direct_dc)
        self.resampler.fill(1, filt_dc)
        self.dc_comp = 0.0
        self.dc_vol = self.vol
        self.dc_changes = 0
        self.dc_track_until = self.t + int(track_seconds * self.clock)
        while self.param_changes:
            self.params = self.param_changes.popleft()[1]
        self.ic1, self.ic2 = 0.0, float(filt_dc)      # SVF at rest: low-pass = input
        lp, hp = self.params[2] & 1, (self.params[2] >> 2) & 1
        self.hp_state = direct_dc + (hp - lp) * filt_dc * self.params[3]
        self.out.clear()
        self.out_len = 0

    # ---- rendering --------------------------------------------------------------
    def advance(self, until: int) -> None:
        """Render the chip up to (not including) cycle `until`."""
        pending = self.pending
        while self.t < until:
            end = min(until, self.t + MAX_SPAN)
            i = 0
            while i < len(pending) and pending[i][0] < end:
                i += 1
            writes = pending[:i]
            del pending[:i]
            self._render(end - self.t, writes)
            self.t = end

    def take(self, n: int) -> np.ndarray:
        """Remove and return up to n rendered output samples (float32, about +-1)."""
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
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)

    def _render(self, n: int, writes) -> None:
        t0 = self.t
        voice_writes: list[list] = [[], [], []]
        global_writes = []
        for t, reg, value in writes:
            off = t - t0 if t > t0 else 0
            if reg < 21:
                voice_writes[reg // 7].append((off, reg % 7, value))
            elif reg < 25:
                global_writes.append((off, reg, value))

        # 1. per voice: register segments and envelope
        segs, envs = [], []
        for v, vw in zip(self.voices, voice_writes):
            vals, lens = [], []
            seg = []
            pos = 0
            for off, reg, value in vw:
                if off > pos:
                    seg.append((pos, off, v.freq, v.pw, v.control))
                    v.run_envelope(off - pos, vals, lens)
                    pos = off
                v.write(reg, value)
            if pos < n:
                seg.append((pos, n, v.freq, v.pw, v.control))
                v.run_envelope(n - pos, vals, lens)
            segs.append(seg)
            envs.append((vals, lens))

        # 2. oscillators, then hard sync (needs the source's accumulator)
        start = [(v.acc, v.lfsr, v.test) for v in self.voices]
        osc = [self._oscillator(start[i], segs[i], n) for i in range(3)]
        syncing = [i for i in range(3) if any(c & 2 and not c & 8 for *_, c in segs[i])]
        if syncing:
            order = [i for i in syncing if (i - 1) % 3 not in syncing]
            order += [i for i in syncing if i not in order]
            if len(order) == 3:
                order = order * 2               # sync loop through all voices: iterate
            for i in order:
                src = (i - 1) % 3
                acc_src = osc[src][0]
                prev = np.empty(n, dtype=np.int32)
                prev[0] = start[src][0]
                prev[1:] = acc_src[:-1]
                rising = (acc_src & 0x800000) > (prev & 0x800000)
                osc[i] = self._oscillator(start[i], segs[i], n, rising)
        for v, (_, _, final) in zip(self.voices, osc):
            v.acc, v.lfsr, v.test = final

        # 3. waveforms and envelopes -> voice outputs
        outs = []
        step = self.scope_step
        if step:
            first = -t0 % step                  # cycles in this span that land on the grid
            taps = range((t0 + first) // step, (t0 + n - 1) // step + 1)
            ring = np.arange(taps.start, taps.stop) % SCOPE_SIZE
        for i in range(3):
            vals, lens = envs[i]
            if not any(vals):
                outs.append(None)               # silent: only the DC offset remains
                if step:
                    self.scope[i, ring] = 0.0
                continue
            acc, lfsr_starts, _ = osc[i]
            wave = self._waveform(i, acc, osc[(i - 1) % 3][0], segs[i], lfsr_starts,
                                  start[i][0], n)
            env = np.repeat(np.array(vals, dtype=np.int32), lens) if len(vals) > 1 else vals[0]
            if step:
                e = env[first::step] if len(vals) > 1 else env
                self.scope[i, ring] = (wave[first::step] - 2048) * e * (1.0 / (2048 * 255))
            outs.append(None if self.mute[i] else (wave - self.wave_zero) * env)
        if self.capture is not None:          # testing hook: per-cycle voice state
            self.capture.append([(osc[i][0].copy(), np.repeat(np.array(envs[i][0]), envs[i][1]),
                                  None if outs[i] is None else outs[i].copy()) for i in range(3)])

        # 4. mixer (direct path and filter input), volume applied per cycle
        direct = np.empty(n, dtype=np.float32)
        filt_in = None
        pos = 0
        for off, reg, value in global_writes + [(n, None, 0)]:
            if off > pos:
                routed = self.filt & 7 if self.filter_enabled else 0
                if routed and filt_in is None:
                    filt_in = np.zeros(n, dtype=np.float32)
                self._mix(outs, pos, off, direct, filt_in, routed)
                pos = off
            if reg is not None:
                self._write_global(reg, value, t0 + off)
        a, b = self.resampler.process([direct, filt_in])
        if len(a):
            self._output(a, b)

    def _oscillator(self, start, segs, n, sync_reset=None):
        """Accumulator values for every cycle of the span.

        Returns (acc array, [LFSR index at each segment start], final state)."""
        acc, lfsr, test = start
        out = np.empty(n, dtype=np.int32)
        lfsr_starts = []
        for s, e, freq, _pw, control in segs:
            length = e - s
            if control & 8:                     # test bit: accumulator held at 0
                out[s:e] = 0
                acc = 0
                test = 8
                lfsr_starts.append(lfsr)
                continue
            if test:                            # test released: LFSR reset
                lfsr = 0
                test = 0
            lfsr_starts.append(lfsr)
            reset = None
            if sync_reset is not None and control & 2:
                reset = sync_reset[s:e]
                if not reset.any():
                    reset = None
            if reset is None:
                u = acc + freq * _AR[1:length + 1]
                out[s:e] = u & MASK24
                end = acc + freq * length
                # noise LFSR shifts whenever accumulator bit 19 goes high
                lfsr += ((end - 0x80000) >> 20) - ((acc - 0x80000) >> 20)
                acc = end & MASK24
            else:
                idx = _AR[:length]
                last = np.maximum.accumulate(np.where(reset, idx, -1))
                a = np.where(last >= 0, freq * (idx - last), acc + freq * (idx + 1)) & MASK24
                out[s:e] = a
                prev = np.empty(length, dtype=np.int32)
                prev[0] = acc
                prev[1:] = a[:-1]
                lfsr += int(np.count_nonzero((a & 0x80000) > (prev & 0x80000)))
                acc = int(a[-1])
            lfsr %= NOISE_PERIOD
        return out, lfsr_starts, (acc, lfsr, test)

    def _waveform(self, i, acc, acc_src, segs, lfsr_starts, acc_before, n):
        wave = np.empty(n, dtype=np.int32)
        for (s, e, _freq, pw, control), lfsr0 in zip(segs, lfsr_starts):
            wf = control >> 4
            if wf == 0:
                wave[s:e] = 0
                continue
            a = acc[s:e]
            test = control & 8
            tri = saw = pulse = noise = None
            if wf & 1:
                msb = (a ^ acc_src[s:e]) if control & 4 else a
                tri = (np.where(msb & 0x800000, a ^ MASK24, a) >> 11) & 0xFFF
            if wf & 2:
                saw = a >> 12
            if wf & 4:
                pulse = 0xFFF if test else ((a >> 12) >= pw) * 0xFFF
            if wf & 8:
                if test:
                    noise = 0
                else:
                    prev = np.empty(e - s, dtype=np.int32)
                    prev[0] = acc[s - 1] if s else acc_before
                    prev[1:] = a[:-1]
                    shifts = np.cumsum((a & 0x80000) > (prev & 0x80000))
                    noise = self.noise[(lfsr0 + shifts) % NOISE_PERIOD].astype(np.int32) << 4
            if wf == 1:
                w = tri
            elif wf == 2:
                w = saw
            elif wf == 4:
                w = pulse
            elif wf == 8:
                w = noise
            elif wf & 8:                        # noise combined with anything: AND
                w = noise
                for part in (tri, saw, pulse):
                    if part is not None:
                        w = w & part
            else:
                if wf == 3:
                    w = self.combined[3][tri & saw]
                elif wf == 5:
                    w = self.combined[5][tri] & pulse
                elif wf == 6:
                    w = self.combined[6][saw] & pulse
                else:
                    w = self.combined[7][tri & saw] & pulse
            wave[s:e] = w
        return wave

    def _mix(self, outs, s, e, direct, filt_in, routed):
        """Fill the direct and filter-input buffers for cycles s..e using the
        current routing and volume.  Every voice carries the envelope DAC's
        DC offset to wherever it is routed (0 on the 8580)."""
        dc_direct = self.mixer_dc
        sum_direct = sum_filter = None
        dc_filter = 0
        for i, out in enumerate(outs):
            if routed >> i & 1:
                dc_filter += self.voice_dc
                if out is not None:
                    sum_filter = out[s:e] if sum_filter is None else sum_filter + out[s:e]
            elif not (i == 2 and self.voice3off):
                dc_direct += self.voice_dc
                if out is not None:
                    sum_direct = out[s:e] if sum_direct is None else sum_direct + out[s:e]
        if self.vol != self.dc_vol and self.t + s < self.dc_track_until:
            # silent output level per volume step: the direct path's DC, plus
            # the routed voices' DC that the (inverting) low-pass passes
            lp = self.mode & 1 if routed else 0
            self.dc_comp += (dc_direct - lp * dc_filter) * (self.vol - self.dc_vol)
            self.dc_vol = self.vol
            self.dc_changes += 1
            if self.dc_changes > 16:          # rapid writes: sample playback, keep it
                self.dc_track_until = 0
        if sum_direct is None:
            direct[s:e] = dc_direct * self.vol - self.dc_comp
        else:
            direct[s:e] = (sum_direct + dc_direct) * self.vol - self.dc_comp
        if routed:
            filt_in[s:e] = dc_filter if sum_filter is None else sum_filter + dc_filter

    def _write_global(self, reg: int, value: int, cycle: int) -> None:
        if reg == 0x15:
            self.fc = (self.fc & 0x7F8) | (value & 7)
        elif reg == 0x16:
            self.fc = (self.fc & 7) | (value << 3)
        elif reg == 0x17:
            self.res = value >> 4
            self.filt = value & 0x0F
        else:
            self.mode = (value >> 4) & 7
            self.voice3off = bool(value & 0x80)
            self.vol = value & 0x0F
        # filter coefficients and volume of the filtered path take effect at
        # the first output sample at or after this cycle
        k = -((-cycle * self.fs) // self.clock)
        self.param_changes.append((k, (self.fc, self.res, self.mode, self.vol)))

    def _output(self, direct: np.ndarray, filt_in: np.ndarray) -> None:
        k0 = self.resampler.k - len(direct)
        out = direct.astype(np.float64)
        pos = 0
        changes = self.param_changes
        while pos < len(out):
            end = len(out)
            if changes and changes[0][0] < k0 + end:
                end = max(pos, changes[0][0] - k0)
            if end > pos:
                self._filter(filt_in, out, pos, end)
                pos = end
            if changes and changes[0][0] <= k0 + pos:
                self.params = changes.popleft()[1]

        # C64 output stage: one-pole high-pass at ~16 Hz (vectorised one-pole
        # recursion: y[n] = p*y[n-1] + (1-p)*x[n] via a scaled cumulative sum)
        p = self.hp_pole
        y = np.empty_like(out)
        for s in range(0, len(out), 2048):
            x = out[s:s + 2048]
            powers = p ** np.arange(1, len(x) + 1)
            low = powers * (self.hp_state + (1 - p) * np.cumsum(x / powers))
            self.hp_state = low[-1]
            y[s:s + 2048] = x - low
        y *= self.scale
        chunk = y.astype(np.float32)
        if self.scope_step:
            self.scope_out[np.arange(k0, k0 + len(chunk)) % SCOPE_SIZE] = chunk
        self.out.append(chunk)
        self.out_len += len(chunk)

    def _filter(self, x: np.ndarray, out: np.ndarray, s: int, e: int) -> None:
        """Run the state-variable filter over x[s:e], adding the selected
        outputs times volume to out[s:e]."""
        fc, res, mode, vol = self.params
        seg = x[s:e]
        ic1, ic2 = self.ic1, self.ic2
        if not seg.any() and abs(ic1) < 1.0 and abs(ic2) < 1.0:
            self.ic1 = self.ic2 = 0.0
            return
        # Topology-preserving (trapezoidal) SVF, stable up to Nyquist.
        # The SID filter inverts: low-pass and high-pass come out negated.
        g = math.tan(math.pi * self.f0[fc] / self.fs)
        k = 1.0 / (0.707 + res / 15.0)
        a1 = 1.0 / (1.0 + g * (g + k))
        a2 = g * a1
        a3 = g * a2
        lp, bp, hp = mode & 1, (mode >> 1) & 1, (mode >> 2) & 1
        c0, c1, c2 = -hp, bp + hp * k, hp - lp
        y = []
        append = y.append
        for v0 in seg.tolist():
            v3 = v0 - ic2
            v1 = a1 * ic1 + a2 * v3
            v2 = ic2 + a2 * ic1 + a3 * v3
            ic1 = 2.0 * v1 - ic1
            ic2 = 2.0 * v2 - ic2
            append(c0 * v0 + c1 * v1 + c2 * v2)
        self.ic1, self.ic2 = ic1, ic2
        if mode:
            out[s:e] += np.array(y) * vol

    def _osc3(self) -> int:
        v, src = self.voices[2], self.voices[1]
        a, wf, control = v.acc, v.control >> 4, v.control
        if not wf:
            return 0
        parts = []
        if wf & 1:
            msb = (a ^ src.acc) if control & 4 else a
            parts.append(((a ^ MASK24) if msb & 0x800000 else a) >> 11 & 0xFFF)
        if wf & 2:
            parts.append(a >> 12)
        if wf & 4:
            parts.append(0xFFF if control & 8 or (a >> 12) >= v.pw else 0)
        if wf & 8:
            parts.append(0 if control & 8 else int(self.noise[v.lfsr]) << 4)
        w = parts[0]
        for p in parts[1:]:
            w &= p
        return w >> 4
