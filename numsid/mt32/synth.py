"""The MT-32's sound generation: parts, notes, partials and the LA32.

A note plays a timbre: up to four partials, combined in two pairs by the
timbre's two structures.  A partial is either a synth wave (square or
sawtooth, shaped by the TVF) or a PCM wave from the sample ROM.  Each has
three envelopes: pitch (P-ENV), filter (TVF) and amplitude (TVA), plus a
pitch LFO.  A pair is mixed, or ring modulated.

Partial parameters are the 58 bytes of the MT-32's SysEx timbre format,
documented in its owner's manual.  What the LA32 does with them is not
documented; the curves here were measured by playing test timbres (sent by
SysEx) through Munt as a black box and fitting what came out (see
docs/mt32.md).  The main findings:

* The TVF is not a filter.  A synth partial is a square wave whose edges
  are half cosines; the cutoff sets how long an edge takes (about 8
  samples at 100, 560 at 0, an octave per 16.4 steps).  Once the edges
  fill the half period the wave is a sine and its level falls with about
  the square of the shortfall.  The sawtooth is that square at half the pitch
  (edges and all, so its edges are twice as long) times a cosine at half
  the pitch.  Resonance lowers the square and adds
  a decaying cosine after every edge, with a period of two edge lengths.
  The wavetables are built from that description in the frequency domain,
  band-limited to the note, and cached.
* Levels add in dB.  Envelope levels are 0.376 dB a step and 0 is
  silence; the TVA level, part volume and expression
  share one curve, and velocity takes off 0.3 dB a step below 127 at full
  sensitivity (50 is none, below 50 inverts it).
* Envelope segments take a fixed time, about 3.6 ms * 2^(T/8), except the
  release, which falls at a fixed rate, and a TVA fall to sustain level 0,
  which carries on to silence.  Velocity shortens only the
  attack, key follow only the segments between attack and release.
* Panning is reversed (0 is hard right) and has eight steps; structures 8
  and 9 spread their two partials apart.

Control values (envelopes, LFO, pitch) update every BLOCK samples and are
interpolated in between.  The output is stereo at the MT-32's 32 kHz.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from ..player import SCOPE_SIZE
from ..resample import Resampler
from .control import COMMON, PARTIAL, ControlROM, RhythmKey, Timbre
from .pcm import MIDDLE_C_PITCH, PCMROM
from .reverb import Reverb

RATE = 32000
BLOCK = 128
PARTIALS = 32
RHYTHM = 8                                   # index of the rhythm part
DEFAULT_RESERVE = (3, 10, 6, 4, 3, 0, 0, 0, 6)
C4 = 261.6256
TIMBRE = COMMON + 4 * PARTIAL                # 246 bytes

# one partial at full level, hard panned, part volume 127: the level of
# Munt's default output
AMP = 0.475

# pitch key follow, 0-16 (15 and 16 are 1, 16 slightly detuned)
KEYFOLLOW = (-1, -0.5, -0.25, 0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1,
             1.25, 1.5, 2, 1, 1)
TVF_KEYFOLLOW = KEYFOLLOW[:15]

# structure -> (partial 1 kind, partial 2 kind, how they combine)
STRUCTURES = {
    1: ("S", "S", "mix"), 2: ("S", "S", "ring"), 3: ("P", "S", "mix"),
    4: ("P", "S", "ring"), 5: ("S", "P", "ring"), 6: ("P", "P", "mix"),
    7: ("P", "P", "ring"), 8: ("S", "S", "mix"), 9: ("P", "P", "mix"),
    10: ("S", "S", "ring only"), 11: ("P", "S", "ring only"),
    12: ("S", "P", "ring only"), 13: ("P", "P", "ring only"),
}
RING_WITH = 0.95                             # ring level next to partial 1
RING_ONLY = 1.6

# ---- measured curves ------------------------------------------------------------

# dB for a level of 0-100, every 5: the TVA level, part volume and expression
LEVEL_CURVE = (-54.2, -54.2, -46.5, -38.4, -32.8, -28.3, -24.9, -21.5, -18.8, -16.6, -14.3,
               -12.4, -10.5, -9.0, -7.5, -6.0, -4.5, -3.4, -2.3, -1.1, 0.0)
ENV_DB = 0.3765                              # dB per envelope level step
VELOCITY_DB = 0.3                            # dB per velocity step at full sensitivity
# TVA bias level 0-12: dB per octave beyond the bias point
TVA_BIAS = (-35.9, -26.4, -19.2, -13.9, -10.2, -7.5, -5.6, -3.8, -2.6, -1.9, -1.1, -0.4, 0.0)
# TVF bias level 0-14: octaves of edge length per octave beyond the bias point
TVF_BIAS = (-1, -1, -1, -0.68, -0.48, -0.24, -0.06, 0, 0.12, 0.24, 0.45, 0.68, 0.93, 3.3, 3.8)

# envelope time parameter -> seconds for a segment, measured to 70; above
# that the curve's doubling every 8 steps carries on
ENV_TIMES = ((0, 0.001), (10, 0.010), (20, 0.022), (30, 0.049), (40, 0.116), (50, 0.276),
             (60, 0.649), (70, 1.538))
RELEASE = 0.42                               # a release falls 100 steps in this share
TO_SILENCE = 0.85                            # a fall to sustain 0 reaches silence in this share

# cutoff -> edge length in samples (a half cosine from low to high)
EDGE = ((0, 558.2), (5, 450.2), (10, 363.3), (15, 294.5), (20, 235.6), (25, 190.7),
        (30, 154.3), (35, 124.8), (40, 99.6), (45, 81.3), (50, 65.9), (60, 42.1),
        (80, 18.2), (100, 8.4))
EDGE_STEPS_PER_OCTAVE = 16.4
CUTOFF_KEYFOLLOW = 15.0                      # cutoff steps per octave at key follow 1
CUTOFF_ENV = 0.67                            # cutoff steps per depth * level / 100
# resonance 0-30: level of the square, and the amplitude and decay time /
# edge length of the ringing after each edge
RES_POINTS = (0, 5, 10, 15, 18, 21, 24, 27, 30)
RES_SQUARE = (1.0, 0.914, 0.80, 0.735, 0.674, 0.645, 0.592, 0.567, 0.52)
RES_AMP = (0.0, 0.025, 0.039, 0.084, 0.125, 0.22, 0.36, 0.6, 0.9)
RES_DECAY = (0.5, 0.5, 0.5, 1.42, 1.42, 1.7, 2.2, 3.3, 4.3)


def env_time(v: int) -> float:
    """Envelope time parameter 0-100 -> seconds."""
    if v <= 70:
        xs, ys = zip(*ENV_TIMES)
        return float(np.interp(v, xs, ys))
    return 3.63e-3 * 2.0 ** (v / 8)


def level_db(v: float) -> float:
    """A level of 0-100 (TVA level, output level) -> dB."""
    v = min(max(v, 0.0), 100.0) / 5
    i = min(int(v), 19)
    return LEVEL_CURVE[i] + (LEVEL_CURVE[i + 1] - LEVEL_CURVE[i]) * (v - i)


def midi_db(v: int) -> float:
    """MIDI volume or expression 0-127 -> dB."""
    return -120.0 if v <= 0 else level_db(v * 100 / 127)


def velocity_db(sens: int, velocity: int) -> float:
    if sens >= 50:
        return -(127 - velocity) * VELOCITY_DB * (sens - 50) / 50
    return -(velocity - 1) * VELOCITY_DB * (50 - sens) / 50


def edge_samples(cutoff: float) -> float:
    """Cutoff steps (may run past 0-100) -> edge length in samples."""
    if cutoff < 0:
        return EDGE[0][1] * 2.0 ** (-cutoff / EDGE_STEPS_PER_OCTAVE)
    if cutoff > 100:
        return max(EDGE[-1][1] * 2.0 ** (-(cutoff - 100) / EDGE_STEPS_PER_OCTAVE), 1.0)
    xs, ws = zip(*EDGE)
    return 2.0 ** float(np.interp(cutoff, xs, np.log2(ws)))


def bias_octaves(point: int, key: int) -> float:
    """How many octaves `key` lies on the biased side of a bias point
    (0-63: below A1 + point, 64-127: above A1 + point - 64)."""
    if point < 64:
        return max(0, point + 33 - key) / 12
    return max(0, key - (point - 64 + 33)) / 12


def db(x: float) -> float:
    return 10.0 ** (x / 20.0)


def pan_gains(s: int) -> tuple[float, float]:
    """Pan step 0 (right) - 7 (left) -> (left, right) gains."""
    s = min(max(s, 0), 7)
    return s / 7, (7 - s) / 7


class Envelope:
    """Straight-line segments: start at `start`, go through `stages`
    ((level, seconds) each), hold at the last one while the key is down
    unless `hold` is False, then release towards `release[0]`.  The
    release moves at a fixed rate, 100 steps in `release[1]` seconds."""

    def __init__(self, start: float, stages: list, release: tuple, hold: bool = True):
        self.value = float(start)
        self.stages = deque(stages)
        self.release_to = release
        self.hold = hold
        self.released = False
        self.target, self.left = None, 0.0
        self._next()

    def _next(self) -> None:
        if self.stages:
            self.target, self.left = self.stages.popleft()
        elif self.hold and not self.released:
            self.target, self.left = None, 0.0
        elif not self.released:
            self.release()
        else:
            self.target, self.left = None, 0.0

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.stages.clear()
        level, full = self.release_to
        self.target, self.left = level, full * abs(self.value - level) / 100

    @property
    def finished(self) -> bool:
        return self.released and self.target is None

    @property
    def holding(self) -> bool:
        """Done moving while the key is down."""
        return self.target is None and not self.released

    def run(self, seconds: float) -> tuple[float, float]:
        """Advance; (value before, value after)."""
        v0 = self.value
        while seconds > 0 and self.target is not None:
            if self.left <= seconds:
                self.value = self.target
                seconds -= self.left
                self._next()
            else:
                self.value += (self.target - self.value) * seconds / self.left
                self.left -= seconds
                seconds = 0
        return v0, self.value


# ---- synth wavetables -----------------------------------------------------------

TABLE = 2048
_tables: dict = {}


def _kernel(x: np.ndarray) -> np.ndarray:
    """Spectrum of a half-cosine edge x periods of the harmonic long:
    cos(pi x) / (1 - 4x^2)."""
    d = 1 - 4 * x * x
    near = np.abs(d) < 1e-9
    out = np.cos(np.pi * x) / np.where(near, 1.0, d)
    return np.where(near, np.pi / 4, out)


def _segment(x: np.ndarray, length: float) -> np.ndarray:
    """Integral of exp(-i x s) for s from -length to 0."""
    small = np.abs(x) < 1e-9
    xs = np.where(small, 1.0, x)
    return np.where(small, length, (np.exp(1j * xs * length) - 1) / (1j * xs))


def _swell(om: np.ndarray, wr: float, length: float) -> np.ndarray:
    """Spectrum of the ringing as it swells in over an edge: a cosine at wr
    rising to its peak at the edge's end, under a raised-cosine window the
    edge's length (time 0 is the middle of the edge)."""
    b = np.pi / length

    def cos_part(k):
        return 0.5 * (_segment(om - k, length) + _segment(om + k, length))
    g = 0.5 * cos_part(wr) + 0.25 * (cos_part(wr + b) + cos_part(wr - b))
    return g * np.exp(-1j * om * length / 2)


def _square(m: np.ndarray, duty: float, w: float, res: int) -> np.ndarray:
    """Complex Fourier coefficients (harmonics m of its own period) of the
    LA32 square: +1 from 0 to `duty`, -1 after, half-cosine edges `w`
    periods long, and ringing after each edge for resonance."""
    lim = min(duty, 1 - duty)
    gain, w_edge = ((lim / w) ** 1.9, lim) if w > lim else (1.0, w)
    edges = 1 - np.exp(-2j * np.pi * m * duty)          # rising at 0, falling at duty
    base = float(np.interp(res, RES_POINTS, RES_SQUARE))
    c = 2 * edges / (2j * np.pi * m) * _kernel(m * w_edge) * base
    if res and w <= lim:                                # (no ringing once the edges fill it)
        amp = float(np.interp(res, RES_POINTS, RES_AMP))
        tau = float(np.interp(res, RES_POINTS, RES_DECAY)) * w
        wr = np.pi / w
        om = 2 * np.pi * m
        # the ringing swells in over the edge, then decays from its end
        h = 0.5 * (1 / (1 / tau + 1j * (om - wr)) + 1 / (1 / tau + 1j * (om + wr)))
        h = h * np.exp(-1j * om * w_edge / 2)
        h = h + _swell(om, wr, w_edge)
        c = c + amp * edges * h
    return c * gain


def wavetable(saw: bool, duty: float, w: float, res: int, harmonics: int) -> np.ndarray:
    """One cycle of a synth partial's wave, band-limited to `harmonics`.
    `w` is the edge length in periods of the note (quantised to 1/24
    octave so tables are shared)."""
    lw = round(24 * math.log2(max(w, 1e-4)))
    key = (saw, round(duty * 256), lw, res, harmonics)
    t = _tables.get(key)
    if t is not None:
        return t
    w = 2.0 ** (lw / 24)
    n = np.arange(1, harmonics + 1)
    if saw:
        # the square at half the pitch times a cosine at half the pitch:
        # harmonic n of the note gets half of each of its neighbours 2n +- 1
        c = _square(np.arange(1, 2 * harmonics + 2), duty, w, res)
        coef = 0.5 * (c[2 * n - 2] + c[2 * n])
    else:
        coef = _square(n, duty, w, res)
    spec = np.zeros(TABLE // 2 + 1, dtype=complex)
    spec[1:harmonics + 1] = coef
    t = np.fft.irfft(spec * TABLE, TABLE)
    t = np.append(t, t[0]).astype(np.float32)   # guard point for interpolation
    if len(_tables) > 20000:
        _tables.clear()
    _tables[key] = t
    return t


def harmonics_for(f0: float) -> int:
    """How many harmonics fit under 16 kHz, rounded down to 1/12 octave
    steps so nearby pitches share tables."""
    h = max(1, min(TABLE // 2 - 1, int(RATE / 2 / f0)))
    return max(1, min(h, int(2.0 ** (math.floor(12 * math.log2(h)) / 12))))


# ---- partials, notes, parts ----------------------------------------------------

@dataclass
class Partial:
    params: bytes
    kind: str                                    # "S" or "P"
    wave: np.ndarray | None = None               # PCM data
    looped: bool = False
    wave_pitch: int = MIDDLE_C_PITCH
    pos: float = 0.0                             # PCM position or synth phase (cycles)
    tva: Envelope = None
    tvf: Envelope = None
    penv: Envelope = None
    lfo_phase: float = 0.0
    level_db: float = 0.0                        # fixed part of the level
    cutoff: float = 0.0                          # fixed part of the cutoff
    env_depth: float = 0.0                       # cutoff steps at TVF envelope level 100
    pitch_depth: float = 0.0                     # cents per P-ENV step
    duty: float = 0.5
    sounding: bool = False                       # the TVA has risen above 0
    done: bool = False


@dataclass
class Note:
    part: int
    key: int                                     # the key played
    pitch_key: int                               # the key the partials follow
    velocity: int
    timbre: Timbre
    partials: list                               # Partial or None, four
    level_db: float = 0.0                        # rhythm key level
    pan: int | None = None                       # rhythm key pan 0-14, else the part's
    started: int = 0
    reverb: bool = True                          # sent to the reverb
    held: bool = True                            # key down
    sustained: bool = False                      # released while the pedal was down

    @property
    def released(self) -> bool:
        return not self.held and not self.sustained

    @property
    def count(self) -> int:
        return sum(p is not None and not p.done for p in self.partials)

    @property
    def finished(self) -> bool:
        return all(p is None or p.done for p in self.partials)


@dataclass
class Part:
    program: int = 0
    timbre: Timbre | None = None
    group: int = 0                               # timbre group and number
    number: int = 0
    volume: int = 102                            # the MT-32 starts at output level 80
    expression: int = 127
    pan: int = 64                                # MT-32 sense: 0 is right
    key_shift: int = 0
    reverb: bool = True
    bend: float = 0.0                            # -1..1
    bend_range: int = 12
    modulation: int = 0
    pedal: bool = False
    notes: list = field(default_factory=list)


def timbre_from_bytes(data: bytes) -> Timbre:
    """A timbre from its 246 SysEx bytes."""
    common = bytes(data[:COMMON])
    mute = common[12] & 0x0F
    partials = [bytes(data[COMMON + i * PARTIAL:COMMON + (i + 1) * PARTIAL]) if mute >> i & 1
                else None for i in range(4)]
    name = "".join(chr(c) if 32 <= c < 127 else " " for c in common[:10]).rstrip()
    return Timbre(name, common, partials)


def timbre_bytes(t: Timbre) -> bytearray:
    blank = bytes(PARTIAL)
    return bytearray(t.common + b"".join(p or blank for p in t.partials))


class MT32:
    """The synthesiser, driven by MIDI messages and rendered on demand.

    `gm` makes it play General MIDI files: GM programs and drum keys go
    through `midi.maps`, pan goes the GM way round and the pitch bend
    range is GM's 2 semitones."""

    def __init__(self, control: ControlROM, pcm: PCMROM, sample_rate: int = 44100, *,
                 scope_rate: int = 0, gm: bool = False, channel_map: dict | None = None):
        from ..midi import maps
        self.rom = control
        self.pcm = pcm
        self.gm = gm
        self.maps = maps
        self.clock = RATE
        self.fs = sample_rate
        self.resampler = Resampler(RATE, sample_rate, channels=2, passband=15_000.0,
                                   ext_lowpass=None)
        self.mute = [False] * 9
        self.reverb_enabled = True
        self.scope_step = 1 if scope_rate else 0
        self.scope = np.zeros((9, SCOPE_SIZE), dtype=np.float32)
        self.scope_out = np.zeros(SCOPE_SIZE, dtype=np.float32)
        if channel_map is None:
            channel_map = {c: c for c in range(1, 10)}
        # file channel -> part: MT-32 channels 2-9 are parts 1-8, 10 is rhythm
        # (all 0-based here)
        self.channel_part = {c: (RHYTHM if m == 9 else m - 1) for c, m in channel_map.items()
                             if 1 <= m <= 9}
        self.reset()

    @property
    def filter_enabled(self) -> bool:
        """The player's "filter" switch turns the reverb on and off."""
        return self.reverb_enabled

    @filter_enabled.setter
    def filter_enabled(self, on: bool) -> None:
        self.reverb_enabled = on

    def reset(self) -> None:
        self.parts = [Part() for _ in range(9)]
        self.temp = [bytearray(TIMBRE) for _ in range(8)]   # timbre temp area
        self.memory = [None] * 64                           # timbre memory (group 2)
        # patch memory: timbre group, number, key shift, fine tune, bend
        # range, assign mode, reverb switch, dummy
        self.patches = [bytes([p // 64, p % 64, 24, 50, 12, 0, 1, 0]) for p in range(128)]
        self.rhythm = dict(self.rom.rhythm)
        self.dirty: set = set()
        for i in range(8):
            self._program(i, 0)
        self.parts[RHYTHM].bend_range = 0
        for part in self.parts:
            if self.gm:
                part.bend_range = 2
        self.master_db = 0.0
        self.t = 0
        self.reverb = Reverb()
        self.resampler.reset()
        self.out: deque = deque()
        self.out_len = 0

    # ---- MIDI -------------------------------------------------------------------
    def _set_timbre(self, part: int, group: int, number: int) -> None:
        if group == 0:
            t = self.rom.bank_a[number % 64]
        elif group == 1:
            t = self.rom.bank_b[number % 64]
        elif group == 2:
            t = self.memory[number % 64] or self.rom.bank_a[number % 64]
        else:
            t = self.rom.rhythm_timbres[number % len(self.rom.rhythm_timbres)]
        p = self.parts[part]
        p.group, p.number, p.timbre = group, number, t
        self.temp[part] = timbre_bytes(t)

    def _program(self, part: int, program: int) -> None:
        p = self.parts[part]
        p.program = program
        if self.gm:
            program = self.maps.GM_TO_MT32[program]
            self._set_timbre(part, program // 64, program % 64)
            return
        group, number, shift, _, bend, _, reverb, _ = self.patches[program]
        self._set_timbre(part, group, number)
        p.key_shift = shift - 24
        p.bend_range = bend
        p.reverb = bool(reverb)

    def message(self, status: int, data: bytes) -> None:
        if status == 0xF0:
            self.sysex(data)
            return
        kind, channel = status & 0xF0, status & 0x0F
        part = self.channel_part.get(channel)
        if part is None:
            return
        p = self.parts[part]
        if kind == 0x90 and data[1]:
            self.note_on(part, data[0], data[1])
        elif kind == 0x80 or kind == 0x90:
            self.note_off(part, data[0])
        elif kind == 0xC0 and part != RHYTHM:
            self._program(part, data[0])
        elif kind == 0xE0:
            p.bend = ((data[1] << 7 | data[0]) - 8192) / 8192
        elif kind == 0xB0:
            cc, v = data
            if cc == 7:
                p.volume = v
            elif cc == 11:
                p.expression = v
            elif cc == 10:
                p.pan = 127 - v if self.gm else v
            elif cc == 1:
                p.modulation = v
            elif cc == 64:
                p.pedal = v >= 64
                if not p.pedal:
                    for n in p.notes:
                        if n.sustained:
                            n.sustained = False
                            self._release(n)
            elif cc in (120, 123, 124, 125, 126, 127):     # all sound / notes off
                for n in p.notes:
                    n.held = n.sustained = False
                    self._release(n)
            elif cc == 121:                                  # reset controllers
                p.bend, p.modulation, p.expression, p.pedal = 0.0, 0, 127, False

    # ---- SysEx --------------------------------------------------------------------
    def sysex(self, data: bytes) -> None:
        """A Roland DT1 (data set) message for the MT-32, without its F0:
        41 device 16 12, a 3-byte address, data, checksum, F7.  It writes
        the MT-32's memory map: patch temp (03 00 00), rhythm setup
        (03 01 10), timbre temp (04 00 00), patch memory (05 00 00),
        timbre memory (08 00 00) and the system area (10 00 00)."""
        if len(data) < 8 or data[0] != 0x41 or data[2] != 0x16 or data[3] != 0x12:
            return
        body = data[4:-1] if data[-1] == 0xF7 else data[4:]
        addr = body[0] << 14 | body[1] << 7 | body[2]
        for i, v in enumerate(body[3:-1]):           # the last byte is the checksum
            self._poke(addr + i, v)
        for part in self.dirty:
            self.parts[part].timbre = timbre_from_bytes(self.temp[part])
        self.dirty.clear()

    def _poke(self, addr: int, v: int) -> None:
        a = addr - (0x03 << 14)
        if 0 <= a < 8 * 16:
            self._patch_temp(*divmod(a, 16), v)
            return
        a = addr - (0x03 << 14 | 0x01 << 7 | 0x10)
        if 0 <= a < 85 * 4:
            self._rhythm_setup(a // 4 + 24, a % 4, v)
            return
        a = addr - (0x04 << 14)
        if 0 <= a < 8 * TIMBRE:
            part, off = divmod(a, TIMBRE)
            self.temp[part][off] = v
            self.dirty.add(part)
            return
        a = addr - (0x05 << 14)
        if 0 <= a < 128 * 8:
            n, off = divmod(a, 8)
            p = bytearray(self.patches[n])
            p[off] = v
            self.patches[n] = bytes(p)
            return
        a = addr - (0x08 << 14)
        if 0 <= a < 64 * 256:
            n, off = divmod(a, 256)
            if off < TIMBRE:
                cur = timbre_bytes(self.memory[n]) if self.memory[n] else bytearray(TIMBRE)
                cur[off] = v
                self.memory[n] = timbre_from_bytes(cur)
            return
        a = addr - (0x10 << 14)
        if 0 <= a <= 2:
            mode = [self.reverb.mode, self.reverb.time, self.reverb.level]
            mode[a] = v
            self.reverb.set(*mode)
        elif a == 0x16:
            self.master_db = level_db(v)

    def _patch_temp(self, part: int, off: int, v: int) -> None:
        p = self.parts[part]
        if off == 0:
            self._set_timbre(part, v, p.number)
        elif off == 1:
            self._set_timbre(part, p.group, v)
        elif off == 2:
            p.key_shift = v - 24
        elif off == 4:
            p.bend_range = v
        elif off == 6:
            p.reverb = bool(v)
        elif off == 8:
            p.volume = round(v * 127 / 100)
        elif off == 9:
            p.pan = v * 9                            # 0-14, onto the same eight steps

    def _rhythm_setup(self, key: int, off: int, v: int) -> None:
        r = self.rhythm.get(key)
        if r is None:
            return
        vals = [127 if r.timbre is None else 64 + r.timbre, r.level, r.pan, int(r.reverb)]
        vals[off] = v
        t = vals[0] - 64
        self.rhythm[key] = RhythmKey(t if 0 <= t < len(self.rom.rhythm_timbres) else None,
                                     vals[1], vals[2], bool(vals[3]))

    # ---- notes ----------------------------------------------------------------------
    def note_on(self, part: int, key: int, velocity: int) -> None:
        p = self.parts[part]
        level, pan, reverb = 0.0, None, p.reverb
        if part == RHYTHM:
            if self.gm:
                key2 = self.maps.gm_drum_key(key)
                if key2 is None:
                    return
                key = key2
            setup = self.rhythm.get(key)
            if setup is None or setup.timbre is None:
                return
            timbre = self.rom.rhythm_timbres[setup.timbre]
            level = level_db(setup.level)
            pan = setup.pan
            pitch_key = 60
            reverb = setup.reverb
        else:
            timbre = p.timbre
            pitch_key = min(max(key + p.key_shift, 0), 127)
        for n in p.notes:                        # a retriggered key cuts the old note
            if n.key == key and not n.released:
                n.held = n.sustained = False
                self._release(n)
        needed = timbre.active
        if not needed or not self._allocate(part, needed):
            return
        partials = []
        for i, params in enumerate(timbre.partials):
            if params is None:
                partials.append(None)
                continue
            kind = STRUCTURES[timbre.structure[i // 2]][i % 2]
            partials.append(self._partial(params, kind, timbre, pitch_key, velocity))
        p.notes.append(Note(part, key, pitch_key, velocity, timbre, partials, level, pan,
                            self.t, reverb))

    def note_off(self, part: int, key: int) -> None:
        p = self.parts[part]
        for n in p.notes:
            if n.key == key and n.held:
                n.held = False
                if p.pedal and part != RHYTHM:
                    n.sustained = True
                else:
                    self._release(n)

    def _release(self, note: Note) -> None:
        for pt in note.partials:
            if pt is not None:
                for env in (pt.tva, pt.tvf, pt.penv):
                    env.release()

    def _allocate(self, part: int, needed: int) -> bool:
        """Free partials for a new note, stealing as the firmware does in
        spirit: released notes first, then the oldest note of a part playing
        over its reserve, then this part's own oldest note."""
        def in_use():
            return sum(n.count for p in self.parts for n in p.notes)
        while PARTIALS - in_use() < needed:
            notes = [n for p in self.parts for n in p.notes]
            released = [n for n in notes if n.released]
            usage = [sum(n.count for n in p.notes) for p in self.parts]
            over = [n for n in notes if usage[n.part] > DEFAULT_RESERVE[n.part]]
            own = [n for n in self.parts[part].notes]
            for pool in (released, over, own):
                if pool:
                    victim = min(pool, key=lambda n: n.started)
                    self.parts[victim.part].notes.remove(victim)
                    break
            else:
                return False
        return True

    def _partial(self, b: bytes, kind: str, timbre: Timbre, key: int, velocity: int) -> Partial:
        pt = Partial(b, kind)
        if kind == "P":
            number = (b[5] + (128 if b[4] & 2 else 0)) % len(self.pcm.waves)
            w = self.pcm.waves[number]
            pt.wave = self.pcm.wave(number)
            pt.looped = w.looped
            pt.wave_pitch = w.pitch
        sustain = timbre.sustain

        def times(ts, keyf, velf=0):
            """Velocity follow shortens the first segment for hard hits, time
            key follow the ones after it for high keys (not the release)."""
            out = [env_time(t) for t in ts]
            if velf:
                out[0] *= 2.0 ** (-(velocity - 64) / 32 * 2.0 ** (velf - 4))
            if keyf:
                k = 2.0 ** (-(key - 60) / 16 * 2.0 ** (keyf - 4))
                out[1:-1] = [t * k for t in out[1:-1]]
            return out

        t = times(b[49:54], b[47], b[48])
        # a fall to sustain level 0 goes on to silence, quickly, like the release
        stages = list(zip(b[54:57], t[:3]))
        stages.append((b[57], t[3]) if b[57] else (-100, t[3] * TO_SILENCE))
        pt.tva = Envelope(0, stages, (-100, t[4] * RELEASE), hold=sustain)
        t = times(b[32:37], b[31])
        pt.tvf = Envelope(0, list(zip(b[37:41], t[:4])), (0, t[4] * RELEASE), hold=sustain)
        t = times(b[11:15], b[10])
        lv = [v - 50 for v in b[15:20]]
        pt.penv = Envelope(lv[0], list(zip(lv[1:4], t[:3])), (lv[4], t[3] * RELEASE),
                           hold=sustain)
        # the fixed part of the level: partial level, velocity, key bias
        pt.level_db = (level_db(b[41]) + velocity_db(b[42], velocity)
                       + TVA_BIAS[min(b[44], 12)] * bias_octaves(b[43], key)
                       + TVA_BIAS[min(b[46], 12)] * bias_octaves(b[45], key))
        # the fixed part of the cutoff: cutoff, key follow, bias; the coarse
        # pitch moves it as far as it moves the pitch
        semis = (key - 60) * TVF_KEYFOLLOW[min(b[25], 14)] + b[0] - 36
        pt.cutoff = (b[23] + semis / 12 * CUTOFF_KEYFOLLOW
                     + TVF_BIAS[min(b[27], 14)] * bias_octaves(b[26], key)
                     * EDGE_STEPS_PER_OCTAVE)
        pt.env_depth = CUTOFF_ENV * b[28] * (1 + 0.47 * b[29] / 100)
        if b[8]:
            pt.pitch_depth = (100 * 2.0 ** (b[8] - 10)
                              * (1 - min(b[9], 3) / 3 * (1 - velocity / 127)))
        pw = b[6] + (velocity - 64) / 63 * 50 * (min(b[7], 14) - 7) / 7
        pw = min(max(pw, 0), 100)
        pt.duty = 0.5 if pw <= 50 else 0.5 * 2.0 ** (-(pw - 50) / 25)
        return pt

    # ---- rendering --------------------------------------------------------------
    def advance(self, until: int) -> None:
        """Render up to (not including) sample `until` at 32 kHz."""
        while self.t < until:
            n = min(until - self.t, BLOCK)
            self._render(n)
            self.t += n

    def take(self, n: int) -> np.ndarray:
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
        left = np.zeros(n, dtype=np.float32)
        right = np.zeros(n, dtype=np.float32)
        send = np.zeros((2, n), dtype=np.float32)
        ring = np.arange(self.t, self.t + n) % SCOPE_SIZE if self.scope_step else None
        for i, part in enumerate(self.parts):
            mix = None
            for note in part.notes:
                y = self._note(part, note, n)
                if y is not None:
                    mix = y if mix is None else mix + y
                    if note.reverb and not self.mute[i]:
                        send += y
            part.notes = [nt for nt in part.notes if not nt.finished]
            if ring is not None:
                self.scope[i, ring] = 0.0 if mix is None else np.sum(mix, axis=0) * 0.5
            if mix is None or self.mute[i]:
                continue
            left += mix[0]
            right += mix[1]
        if self.reverb_enabled:
            wl, wr = self.reverb.process(send[0], send[1])
            left += wl
            right += wr
        a, b = self.resampler.process([left, right], n)
        if len(a):
            chunk = np.stack([a, b], axis=1).astype(np.float32)
            if self.scope_step:
                k0 = self.resampler.k - len(a)
                self.scope_out[np.arange(k0, k0 + len(a)) % SCOPE_SIZE] = chunk.mean(axis=1)
            self.out.append(chunk)
            self.out_len += len(a)

    def _note(self, part: Part, note: Note, n: int) -> np.ndarray | None:
        """A note's stereo output for the next n samples, shape (2, n)."""
        dt = n / RATE
        sigs = [None if pt is None or pt.done else self._partial_block(part, note, pt, n, dt)
                for pt in note.partials]
        if all(s is None for s in sigs):
            return None
        gain = AMP * db(midi_db(part.volume) + midi_db(part.expression) + note.level_db
                        + self.master_db)
        if note.pan is not None:
            s = note.pan >> 1
        else:
            s = min(max(part.pan, 0) // 18, 7)
        out = np.zeros((2, n), dtype=np.float32)
        for pair in (0, 1):
            s1, s2 = sigs[2 * pair], sigs[2 * pair + 1]
            structure = note.timbre.structure[pair]
            how = STRUCTURES[structure][2]
            if structure in (8, 9):
                # the pair's partials are spread apart
                outs = ((s1, 2 * s - 7), (s2, 2 * s + 1))
            elif how == "mix":
                y = s1 if s2 is None else s2 if s1 is None else s1 + s2
                outs = ((y, s),)
            elif s1 is None or s2 is None:
                outs = ((s2 if s1 is None else s1, s),)     # nothing to ring with
            else:
                y = s1 * s2 * (RING_WITH if how == "ring" else RING_ONLY)
                outs = ((y + s1 if how == "ring" else y, s),)
            for y, step in outs:
                if y is not None:
                    lg, rg = pan_gains(step)
                    out[0] += y * (lg * gain)
                    out[1] += y * (rg * gain)
        return out

    def _partial_block(self, part: Part, note: Note, pt: Partial, n: int, dt: float):
        """One partial's signal for the block, at its own level (1 = full)."""
        b = pt.params
        a0, a1 = pt.tva.run(dt)
        if a1 > 0:
            pt.sounding = True
        elif pt.tva.finished or a1 <= -100 or (
                pt.sounding and (pt.tva.target is None or pt.tva.target >= 0)):
            # silent at the end, or back at level 0 in an earlier segment
            pt.done = True
            return None
        f0_, f1_ = pt.tvf.run(dt)
        p0, p1 = pt.penv.run(dt)
        # pitch, in cents from middle C
        kf = KEYFOLLOW[min(b[2], 16)]
        cents = ((note.pitch_key - 60) * kf + (b[0] - 36)) * 100 + (b[1] - 50)
        if b[2] == 16:
            cents += 5
        cents += (p0 + p1) / 2 * pt.pitch_depth
        if b[3]:
            cents += part.bend * part.bend_range * 100
        lfo = 0.56 * (b[21] + b[22] * part.modulation / 127)
        if lfo:
            rate = 1.54 * 2.0 ** ((b[20] - 50) / 8)
            cents += lfo * math.sin(2 * math.pi * pt.lfo_phase)
            pt.lfo_phase = (pt.lfo_phase + rate * dt) % 1.0
        if pt.kind == "S":
            freq = C4 * 2.0 ** (cents / 1200)
            if freq >= RATE / 2:
                return None
            cutoff = pt.cutoff + pt.env_depth * (f0_ + f1_) / 200
            w = edge_samples(cutoff) * freq / RATE
            table = wavetable(bool(b[4] & 1), pt.duty, w, min(b[24], 30), harmonics_for(freq))
            inc = freq / RATE
            phase = (pt.pos + inc * np.arange(n)) % 1.0
            pt.pos = (pt.pos + inc * n) % 1.0
            x = phase * TABLE
            i = x.astype(np.int32)
            frac = (x - i).astype(np.float32)
            raw = table[i] + (table[i + 1] - table[i]) * frac
        else:
            step = 2.0 ** (cents / 1200 + (pt.wave_pitch - MIDDLE_C_PITCH) / 4096)
            step = max(math.floor(step * 1024), 1) / 1024    # the LA32's step resolution
            pos = pt.pos + step * np.arange(n)
            length = len(pt.wave)
            if pt.looped:
                pos %= length
            else:
                if pos[0] >= length - 1:
                    pt.done = True
                    return None
                pos = np.minimum(pos, length - 1.001)
            i = pos.astype(np.int32)
            frac = (pos - i).astype(np.float32)
            j = (i + 1) % length if pt.looped else i + 1
            raw = pt.wave[i] + (pt.wave[j] - pt.wave[i]) * frac
            pt.pos = pt.pos + step * n
            if pt.looped:
                pt.pos %= length
        g0 = 0.0 if a0 <= -100 else db(pt.level_db + (a0 - 100) * ENV_DB)
        g1 = 0.0 if a1 <= -100 else db(pt.level_db + (a1 - 100) * ENV_DB)
        return raw * np.linspace(g0, g1, n, endpoint=False, dtype=np.float32)
