"""The MT-32's sound generation: parts, notes, partials and the LA32.

A note plays a timbre: up to four partials, combined in two pairs by the
timbre's two structures.  A partial is either a synth wave (square or
sawtooth, shaped by the TVF, a resonant low-pass) or a PCM wave from the
sample ROM.  Each has three envelopes: pitch (P-ENV), filter (TVF) and
amplitude (TVA), plus a pitch LFO.  A pair is mixed, or ring modulated.

Partial parameters are the 58 bytes of the MT-32's SysEx timbre format,
documented in its owner's manual.  How the LA32 turns them into sound is
not documented, and this first version is a model, not an emulation:

* Synth partials are band-limited wavetables.  The TVF is applied in the
  frequency domain, harmonic by harmonic, with the response of a 2-pole
  resonant low-pass, and tables are cached by their shape.
* Envelope levels move in a log domain (0.6 dB per step of 100), so linear
  ramps of level give exponential decays.  Envelope times map 0-100 to
  about 1 ms - 20 s on a log scale.
* Structures 1/8 and 6/9 are treated alike (both mix their pair); 2 ring
  modulates the second partial with the first and plays both.
* The reverb is a stand-in for the BOSS chip (see `reverb`).

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
from .control import ControlROM, Timbre
from .pcm import MIDDLE_C_PITCH, PCMROM
from .reverb import Reverb

RATE = 32000
BLOCK = 128
PARTIALS = 32
RHYTHM = 8                                   # index of the rhythm part
DEFAULT_RESERVE = (3, 10, 6, 4, 3, 0, 0, 0, 6)
C4 = 261.6256

KEYFOLLOW = (-1, -0.5, -0.25, 0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1,
             1.25, 1.5, 2, 1, 1)

# structure -> (partial 1 kind, partial 2 kind, how they combine)
STRUCTURES = {
    1: ("S", "S", "mix"), 2: ("S", "S", "ring"), 3: ("P", "S", "mix"),
    4: ("P", "S", "ring"), 5: ("S", "P", "ring"), 6: ("P", "P", "mix"),
    7: ("P", "P", "ring"), 8: ("S", "S", "mix"), 9: ("P", "P", "mix"),
    10: ("S", "S", "ring only"), 11: ("P", "S", "ring only"),
    12: ("S", "P", "ring only"), 13: ("P", "P", "ring only"),
}

LEVEL_DB = 0.6                               # dB per envelope level step
MASTER = 0.4


def env_time(v: int) -> float:
    """Envelope time parameter 0-100 -> seconds."""
    return 0.0 if v <= 0 else 0.001 * 2.0 ** (v / 7.0)


def db(x: float) -> float:
    return 10.0 ** (x / 20.0)


def volume_db(v: int) -> float:
    """MIDI volume or expression 0-127 -> dB."""
    return -100.0 if v <= 0 else 40.0 * math.log10(v / 127.0)


class Envelope:
    """Straight-line segments: start at `start`, go through `stages`
    ((level, seconds) each), hold at the last one while the key is down
    unless `hold` is False, then release to `end` in `release` seconds."""

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
        self.target, self.left = self.release_to

    @property
    def finished(self) -> bool:
        return self.released and self.target is None

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


def wavetable(saw: bool, pw: int, f0: float, cutoff: float, resonance: int) -> np.ndarray:
    """One cycle of the filtered wave, band-limited for f0 at 32 kHz.  The
    shape is quantised (cutoff/f0 to 1/12 octave) so tables are shared."""
    harmonics = max(1, min(TABLE // 2 - 1, int(RATE / 2 / f0)))
    ratio = round(12 * math.log2(max(cutoff, 1.0) / f0))
    key = (saw, pw // 4 if not saw else 0, min(harmonics, 1 << harmonics.bit_length()),
           ratio, resonance)
    t = _tables.get(key)
    if t is not None:
        return t
    n = np.arange(1, harmonics + 1)
    if saw:
        amp = 1.0 / n
        phase = np.full(harmonics, -np.pi / 2)
    else:
        duty = 0.5 - 0.45 * min(pw, 100) / 100
        amp = 2 * np.sin(np.pi * n * duty) / (np.pi * n)
        phase = np.zeros(harmonics)
    x = n * 2.0 ** (-ratio / 12)                 # harmonic frequency / cutoff
    q = 0.7 + resonance * 0.3
    h = 1.0 / np.sqrt((1 - x * x) ** 2 + (x / q) ** 2)
    spec = np.zeros(TABLE // 2 + 1, dtype=complex)
    spec[1:harmonics + 1] = amp * h * np.exp(1j * phase)
    t = np.fft.irfft(spec, TABLE) * (TABLE / 2)
    t = np.append(t, t[0]).astype(np.float32)   # guard point for interpolation
    if len(_tables) > 20000:
        _tables.clear()
    _tables[key] = t
    return t


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
    done: bool = False

    @property
    def p(self) -> bytes:
        return self.params


@dataclass
class Note:
    part: int
    key: int                                     # the key played
    pitch_key: int                               # the key the partials follow
    velocity: int
    timbre: Timbre
    partials: list                               # Partial or None, four
    level_db: float = 0.0                        # rhythm key level etc
    pan: float | None = None                     # rhythm key pan, else the part's
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
    volume: int = 100
    expression: int = 127
    pan: int = 64
    bend: float = 0.0                            # -1..1
    bend_range: int = 12
    modulation: int = 0
    pedal: bool = False
    notes: list = field(default_factory=list)


class MT32:
    """The synthesiser, driven by MIDI messages and rendered on demand.

    `gm` makes it play General MIDI files: GM programs and drum keys go
    through `midi.maps`, and the pitch bend range is GM's 2 semitones."""

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
        for i, part in enumerate(self.parts[:8]):
            self._program(i, 0)
        self.parts[RHYTHM].bend_range = 0
        for part in self.parts:
            if self.gm:
                part.bend_range = 2
        self.t = 0
        self.reverb = Reverb()
        self.resampler.reset()
        self.out: deque = deque()
        self.out_len = 0

    # ---- MIDI -------------------------------------------------------------------
    def _program(self, part: int, program: int) -> None:
        p = self.parts[part]
        p.program = program
        p.timbre = self.rom.patch(self.maps.GM_TO_MT32[program] if self.gm else program)

    def message(self, status: int, data: bytes) -> None:
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
                p.pan = v
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

    def note_on(self, part: int, key: int, velocity: int) -> None:
        p = self.parts[part]
        level, pan, pitch_key, reverb = 0.0, None, key, True
        if part == RHYTHM:
            if self.gm:
                key2 = self.maps.gm_drum_key(key)
                if key2 is None:
                    return
                key = key2
            setup = self.rom.rhythm.get(key)
            if setup is None or setup.timbre is None:
                return
            timbre = self.rom.rhythm_timbres[setup.timbre]
            level = (setup.level - 100) * LEVEL_DB
            pan = setup.pan / 14
            pitch_key = 60
            reverb = setup.reverb
        else:
            timbre = p.timbre
        for n in p.notes:                        # a retriggered key cuts the old note
            if n.key == key and not n.released:
                n.held = n.sustained = False
                self._release(n)
        needed = timbre.active
        if not self._allocate(part, needed):
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
            number = b[5] + (128 if b[4] & 2 else 0)
            w = self.pcm.waves[number % len(self.pcm.waves)]
            pt.wave = self.pcm.wave(number)
            pt.looped = w.looped
            pt.wave_pitch = w.pitch
        # time key follow and velocity follow shorten envelopes for high keys
        # and hard hits
        def times(ts, keyf, velf=0):
            scale = 2.0 ** (-(key - 60) / 12 * keyf / 4 - (velocity - 64) / 64 * velf / 4)
            return [env_time(t) * scale for t in ts]
        sustain = timbre.sustain
        t = times(b[49:54], b[47], b[48])
        pt.tva = Envelope(0, list(zip(b[54:58], t[:4])), (0, t[4]), hold=sustain)
        t = times(b[32:37], b[31])
        pt.tvf = Envelope(0, list(zip(b[37:41], t[:4])), (0, t[4]), hold=sustain)
        t = times(b[11:15], b[10])
        lv = [v - 50 for v in b[15:20]]
        pt.penv = Envelope(lv[0], list(zip(lv[1:4], t[:3])), (lv[4], t[3]), hold=sustain)
        pt.velocity = velocity
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
        part_db = volume_db(part.volume) + volume_db(part.expression) + note.level_db
        sigs = []
        for i, pt in enumerate(note.partials):
            if pt is None or pt.done:
                sigs.append(None)
                continue
            sigs.append(self._partial_block(part, note, pt, n, dt, part_db))
        out = None
        for pair in (0, 1):
            s1, s2 = sigs[2 * pair], sigs[2 * pair + 1]
            how = STRUCTURES[note.timbre.structure[pair]][2]
            if how == "mix":
                outs = [s[0] for s in (s1, s2) if s is not None]
                y = None if not outs else outs[0] if len(outs) == 1 else outs[0] + outs[1]
            else:
                raw2 = None if s2 is None else s2[1]
                y = None
                if s1 is not None and raw2 is not None:
                    y = s1[0] * raw2
                if how == "ring" and s1 is not None:
                    y = s1[0] if y is None else y + s1[0]
            if y is not None:
                out = y if out is None else out + y
        if out is None:
            return None
        pan = note.pan if note.pan is not None else part.pan / 127
        return np.stack([out * math.cos(pan * math.pi / 2), out * math.sin(pan * math.pi / 2)])

    def _partial_block(self, part: Part, note: Note, pt: Partial, n: int, dt: float,
                       part_db: float):
        """(signal with its level applied, raw signal) for one partial."""
        b = pt.params
        a0, a1 = pt.tva.run(dt)
        if pt.tva.finished or (pt.tva.released and max(a0, a1) <= 0):
            pt.done = True
            return None
        f0_, f1_ = pt.tvf.run(dt)
        p0, p1 = pt.penv.run(dt)
        # pitch, in semitones from middle C
        kf = KEYFOLLOW[min(b[2], 16)]
        semis = (note.pitch_key - 60) * kf + (b[0] - 36) + (b[1] - 50) / 100
        semis += (p0 + p1) / 2 / 50 * b[8] * 1.2
        if b[3]:
            semis += part.bend * part.bend_range
        lfo_depth = b[21] / 100 * 0.5 + part.modulation / 127 * b[22] / 100
        if lfo_depth:
            rate = 0.2 * 2.0 ** (b[20] / 16)
            semis += lfo_depth * math.sin(2 * math.pi * pt.lfo_phase)
            pt.lfo_phase = (pt.lfo_phase + rate * dt) % 1.0
        freq = C4 * 2.0 ** (semis / 12)
        if pt.kind == "S":
            if freq >= RATE / 2:
                raw = np.zeros(n, dtype=np.float32)
            else:
                ckf = KEYFOLLOW[min(b[25], 14)]
                cutoff = b[23] + b[28] * (f0_ + f1_) / 200 + (note.pitch_key - 60) * ckf * 100 / 120
                cutoff_hz = 20.0 * 2.0 ** (min(max(cutoff, 0), 127) / 10)
                table = wavetable(bool(b[4] & 1), b[6], freq, cutoff_hz, b[24])
                inc = freq / RATE
                phase = (pt.pos + inc * np.arange(n)) % 1.0
                pt.pos = (pt.pos + inc * n) % 1.0
                x = phase * TABLE
                i = x.astype(np.int32)
                frac = (x - i).astype(np.float32)
                raw = table[i] + (table[i + 1] - table[i]) * frac
        else:
            step = 2.0 ** (semis / 12 + (pt.wave_pitch - MIDDLE_C_PITCH) / 4096)
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
        # level: partial level, velocity, key bias, envelope, part
        vel = note.velocity
        level = (b[41] - 100) * LEVEL_DB * 0.5 - b[42] / 100 * (127 - vel) / 127 * 30
        level += part_db
        g0 = 0.0 if a0 <= 0 else db(level + (a0 - 100) * LEVEL_DB)
        g1 = 0.0 if a1 <= 0 else db(level + (a1 - 100) * LEVEL_DB)
        gain = np.linspace(g0, g1, n, endpoint=False, dtype=np.float32) * MASTER
        return raw * gain, raw
