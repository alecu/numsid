"""Glue: run a tune's engine and pull audio out of its sound chip.

Two kinds of tune are played the same way.  For a SID file the emulated
C64's 6502 runs the tune's own player code against the SID.  For an Amiga
MOD a ProTracker replay routine drives Paula.  Either way the engine
writes registers at known clock cycles, the chip renders at its own clock
and resamples to the output rate, and `Player` pulls audio out of it.  It
also remembers recent register states, so displays can show what is being
heard rather than what was last rendered.
"""

from __future__ import annotations

import bisect
import math
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import modfile, protracker, psid
from .c64 import C64
from .paula import Paula
from .sid import SID


class TuneError(ValueError):
    pass


def load_tune(path: str | Path):
    """A PSIDFile or a ModFile, by the file's contents."""
    blob = Path(path).read_bytes()
    if blob[:4] in (b"PSID", b"RSID"):
        return psid.parse(blob)
    try:
        return modfile.parse(blob, str(path))
    except modfile.NotAModError:
        raise TuneError(f"{path}: not a PSID/RSID file or a MOD") from None
    except modfile.ModFileError as exc:
        raise TuneError(f"{path}: {exc}") from None


def open_player(tune, song: int | None = None, **kwargs) -> "Player":
    """A player for a tune or a path.  `song` is the subtune of a SID file,
    or the starting order position of a MOD."""
    if isinstance(tune, (str, Path)):
        tune = load_tune(tune)
    separation = kwargs.pop("separation", None)        # MOD only
    digi_boost = kwargs.pop("digi_boost", None)        # SID only
    if isinstance(tune, modfile.ModFile):
        if separation is not None:
            kwargs["separation"] = separation
        return ModPlayer(tune, song or 0, **kwargs)
    if digi_boost is not None:
        kwargs["digi_boost"] = digi_boost
    return SIDPlayer(tune, song, **kwargs)


@dataclass
class Readout:
    """What displays show for one moment: a position, info lines and a level
    (0..1) per voice, and lines about the whole chip."""
    position: str
    voices: list | None = None
    levels: list | None = None
    chip: list | None = None


class Player:
    """Shared bookkeeping.  Subclasses provide `chip` (with take(),
    out_len, clock, resampler, mute, filter_enabled and the scope taps) and
    `_step()`, which runs the engine a little and renders the chip up to
    there."""

    channels = 1                 # audio channels in the output
    voice_label = "VOICE"
    info_lines = 4               # readout lines per voice
    models: tuple = ()
    skip_label = "subtune"
    credit = ""

    def __init__(self, sample_rate: int, gain: float, scope_rate: int):
        self.sample_rate = sample_rate
        self.gain = gain
        self.scope_rate = scope_rate

    @property
    def voices(self) -> int:
        return len(self.chip.mute)

    @property
    def clock(self) -> int:
        return self.chip.clock

    @property
    def title(self) -> str:
        return self.tune.name

    @property
    def help(self) -> str:
        keys = f"1-{self.voices}" if self.voices > 3 else " ".join("123"[:self.voices])
        return (f"←/→ {self.skip_label}    {keys} mute {self.voice_label.lower()}    "
                f"SPACE pause    F filter on/off    M {'/'.join(self.models)}    ESC quit")

    def _restart_timeline(self) -> None:
        self.samples_played = 0
        self.first_sample = self.chip.resampler.k    # output index of our sample 0
        self.frames: deque = deque(maxlen=400)       # (cycle, state)

    @property
    def seconds(self) -> float:
        return self.samples_played / self.sample_rate

    def render(self, n: int) -> np.ndarray:
        """Next n output samples as float32 in [-1, 1]: shape (n,) for mono,
        (n, channels) otherwise."""
        chip = self.chip
        while chip.out_len < n:
            self._step()
        out = chip.take(n)
        if self.gain != 1.0:
            out *= self.gain
        np.clip(out, -1.0, 1.0, out=out)
        self.samples_played += n
        return out

    def render_int16(self, n: int) -> np.ndarray:
        return (self.render(n) * 32767.0).astype(np.int16)

    def skip(self, direction: int) -> None:
        """Next (+1) or previous (-1) subtune or position."""
        raise NotImplementedError

    # ---- position bookkeeping for displays ------------------------------------
    def sample_index(self, seconds: float) -> int:
        """Output-sample index (as used by the chip's scope taps) heard at
        `seconds` into the current song."""
        return self.first_sample + int(seconds * self.sample_rate)

    def cycle_at(self, seconds: float) -> int:
        return self.sample_index(seconds) * self.clock // self.sample_rate

    def state_at(self, seconds: float):
        """State recorded by the last engine step before the moment heard
        at `seconds`, or None."""
        cycle = self.cycle_at(seconds)
        frames = list(self.frames)
        i = bisect.bisect_right([f[0] for f in frames], cycle) - 1
        return frames[i][1] if i >= 0 else None


# ---- C64 SID tunes -------------------------------------------------------------------

SID_NOTES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]
WAVE_NAMES = [(1, "TRI"), (2, "SAW"), (4, "PUL"), (8, "NOI")]


def note_name(hz: float) -> str:
    if hz < 8:
        return "---"
    midi = 69 + 12 * math.log2(hz / 440.0)
    n = int(round(midi))
    cents = int(round((midi - n) * 100))
    return f"{SID_NOTES[n % 12]}{n // 12 - 1} {cents:+3d}c"


class SIDPlayer(Player):
    """A PSID/RSID tune on the emulated C64."""

    models = ("6581", "8580")
    credit = "numsid  ·  6502 CPU and SID chip emulated cycle by cycle in Python + numpy"

    def __init__(self, tune: psid.PSIDFile | str | Path, song: int | None = None, *,
                 sample_rate: int = 44100, model: str | None = None,
                 filter_enabled: bool = True, gain: float = 1.0, scope_rate: int = 0,
                 digi_boost: bool = True):
        super().__init__(sample_rate, gain, scope_rate)
        self.tune = tune if isinstance(tune, psid.PSIDFile) else psid.load(tune)
        self.model = model or self.tune.preferred_model
        self.filter_enabled = filter_enabled
        self.digi_boost = digi_boost
        self.sid = SID(self.tune.clock, sample_rate, self.model,
                       filter_enabled=filter_enabled, scope_rate=scope_rate,
                       digi_boost=digi_boost)
        self.select(song or self.tune.start_song)

    @property
    def chip(self) -> SID:
        return self.sid

    @property
    def subtitle(self) -> str:
        return f"{self.tune.author}  ·  {self.tune.released}"

    def set_model(self, model: str) -> None:
        """Swap in a different SID chip and restart the current song."""
        old = self.sid
        self.model = model
        self.sid = SID(self.tune.clock, self.sample_rate, model,
                       filter_enabled=old.filter_enabled, scope_rate=self.scope_rate,
                       digi_boost=self.digi_boost)
        self.sid.mute = old.mute
        self.select(self.song)

    def select(self, song: int) -> None:
        """(Re)start the machine on the given 1-based subtune."""
        self.sid.reset()
        self.c64 = C64(self.tune, self.sid, song)
        self.song = song
        self.c64.init()
        self.sid.advance(self.c64.cpu.clock)
        self.sid.settle()
        self._restart_timeline()

    def skip(self, direction: int) -> None:
        self.select((self.song - 1 + direction) % self.tune.songs + 1)

    @property
    def warnings(self) -> list[str]:
        return self.c64.warnings

    def _step(self) -> None:
        sid = self.sid
        cycle = self.c64.play_frame()
        sid.advance(cycle)
        self.frames.append((cycle, (bytes(sid.regs), tuple(v.env for v in sid.voices))))

    def chip_line(self) -> str:
        tune = self.tune
        return (f"{tune.magic} · {'PAL' if tune.is_pal else 'NTSC'} · MOS {self.model} · "
                f"filter {'on' if self.sid.filter_enabled else 'off'}")

    def readout(self, state) -> Readout:
        position = f"song {self.song}/{self.tune.songs}"
        if state is None:
            return Readout(position)
        regs, envs = state
        sid, clock = self.sid, self.clock
        voices = []
        for i in range(3):
            r = regs[7 * i:7 * i + 7]
            freq = r[0] | r[1] << 8
            pw = r[2] | (r[3] & 15) << 8
            ctrl, ad, sr = r[4], r[5], r[6]
            hz = freq * clock / 16777216
            waves = "+".join(n for bit, n in WAVE_NAMES if ctrl >> 4 & bit) or "----"
            flags = " ".join(f for bit, f in ((1, "GATE"), (2, "SYNC"), (4, "RING"), (8, "TEST"))
                             if ctrl & bit)
            voices.append([f"{note_name(hz) if freq else '---':<10} {hz:8.1f} Hz",
                           f"{waves:<12} PW {100 * pw / 4095:5.1f}%",
                           f"A {ad >> 4:X} D {ad & 15:X} S {sr >> 4:X} R {sr & 15:X}   "
                           f"env {envs[i]:3d}",
                           flags or "-"])
        fc = (regs[0x15] & 7) | regs[0x16] << 3
        res, routing, mode = regs[0x17] >> 4, regs[0x17] & 7, regs[0x18]
        modes = "".join(n for bit, n in ((0x10, "LP "), (0x20, "BP "), (0x40, "HP "))
                        if mode & bit)
        routed = "".join(str(v + 1) for v in range(3) if routing >> v & 1) or "-"
        chip = [f"VOL {mode & 15:<2d}  {modes or 'no filter'}" + ("  3OFF" if mode & 0x80 else ""),
                f"FC {fc:<4d} {sid.f0[fc]:5.0f}Hz RES {res:X}  in {routed}"]
        return Readout(position, voices, [e / 255 for e in envs], chip)


# ---- Amiga MODs ----------------------------------------------------------------------

class ModPlayer(Player):
    """A tracker module, played by the ProTracker replay routine on Paula."""

    channels = 2
    voice_label = "CHANNEL"
    info_lines = 3
    models = ("A500", "A1200")
    skip_label = "position"
    credit = "numsid  ·  ProTracker replayer and Amiga Paula chip emulated in Python + numpy"

    def __init__(self, tune: modfile.ModFile | str | Path, position: int = 0, *,
                 sample_rate: int = 44100, model: str | None = None,
                 filter_enabled: bool = True, gain: float = 1.0, scope_rate: int = 0,
                 separation: float = 1.0):
        super().__init__(sample_rate, gain, scope_rate)
        self.tune = tune if isinstance(tune, modfile.ModFile) else modfile.load(tune)
        self.model = model or "A500"
        self.separation = separation
        self.paula = self._paula(filter_enabled)
        self.select(position)

    def _paula(self, filter_enabled: bool) -> Paula:
        return Paula(self.tune.ram, self.sample_rate, self.model, channels=self.tune.channels,
                     separation=self.separation, filter_enabled=filter_enabled,
                     scope_rate=self.scope_rate)

    @property
    def chip(self) -> Paula:
        return self.paula

    @property
    def subtitle(self) -> str:
        return self.tune.subtitle

    @property
    def warnings(self) -> list[str]:
        return self.tune.warnings

    @property
    def position(self) -> int:
        return self.replayer.now[0]

    def select(self, position: int) -> None:
        """(Re)start at an order position, with the speed and tempo the song
        has there."""
        self.paula.reset()
        if position:
            self.replayer = protracker.seek(self.tune, self.paula, position)
        else:
            self.replayer = protracker.ProTracker(self.tune, self.paula)
        self._restart_timeline()

    def skip(self, direction: int) -> None:
        self.select((self.position + direction) % len(self.tune.orders))

    def set_model(self, model: str) -> None:
        """Swap in the other Amiga's output stage and restart at the current position."""
        old = self.paula
        self.model = model
        self.paula = self._paula(old.filter_enabled)
        self.paula.mute = old.mute
        self.select(self.position)

    def _step(self) -> None:
        pt, paula = self.replayer, self.paula
        t = pt.time
        paula.advance(t)
        pt.tick()
        voices = tuple((hw.per, hw.vol if hw.dma else 0, v.sample, v.cmd & 0xFFF)
                       for hw, v in zip(paula.voices, pt.voices))
        self.frames.append((t, (pt.now, pt.speed, pt.bpm, pt.led, pt.playing, voices)))

    def chip_line(self) -> str:
        sep = f"stereo {100 * self.separation:.0f}%" if self.separation else "mono"
        return (f"{self.tune.tag or 'ST15'} · PAL · Amiga {self.model[1:]} · {sep} · "
                f"filter {'on' if self.paula.filter_enabled else 'off'}")

    def readout(self, state) -> Readout:
        orders = self.tune.orders
        if state is None:
            return Readout(f"pos {self.position:02d}/{len(orders):02d}")
        (pos, row), speed, bpm, led, playing, voices = state
        lines, levels = [], []
        for period, volume, sample, cmd in voices:
            hz = self.paula.color_clock / period if period else 0.0
            s = self.tune.samples[sample - 1] if 0 < sample <= len(self.tune.samples) else None
            lines.append([f"{protracker.note_name(period) if volume else '---':<4} "
                          f"per {period:4d}  {hz:7.0f} Hz",
                          f"{sample:02d} {s.name if s else '':.22}" if sample else "--",
                          f"vol {volume:2d}    fx {f'{cmd:03X}' if cmd else '---'}"])
            levels.append(volume / 64)
        chip = [f"POS {pos:02d}/{len(orders):02d}  PATTERN {orders[pos]:02d}  ROW {row:02d}",
                f"SPEED {speed}  BPM {bpm}  LED {'on' if led else 'off'}"
                + ("" if playing else "  STOPPED")]
        return Readout(f"pos {pos:02d}/{len(orders):02d}  row {row:02d}", lines, levels, chip)


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int) -> None:
    """16-bit WAV; samples shaped (n,) for mono or (n, channels)."""
    pcm = samples if samples.dtype == np.int16 else (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1 if pcm.ndim == 1 else pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


def song_length(tune, song: int | None = None, max_seconds: float = 600.0, **kwargs) \
        -> tuple[float, str] | None:
    """How long a song lasts: (seconds, "loop") for intro plus one pass of
    its loop, or (seconds, "end") for one that stops.  None if neither is
    found within `max_seconds`.  For a MOD, `song` is the starting order
    position, and the replayer's own bookkeeping gives an exact answer."""
    if isinstance(tune, modfile.ModFile):
        return protracker.song_length(tune, song or 0, max_seconds=max(max_seconds, 3600.0))
    return sid_song_length(tune, song or tune.start_song, max_seconds, **kwargs)


class _NoteLog:
    """SID stand-in that only records gate-on events (voice, pitch, waveform)."""

    def __init__(self):
        self.regs = bytearray(32)
        self.events: list = []

    def write(self, reg, value, cycle):
        if reg in (4, 11, 18) and value & 1 and not self.regs[reg] & 1:
            v = reg // 7
            self.events.append((v, self.regs[7 * v] | self.regs[7 * v + 1] << 8, value & 0xF0))
        self.regs[reg] = value

    def read(self, reg, cycle):
        return 0


def sid_song_length(tune: psid.PSIDFile, song: int, max_seconds: float = 600.0,
                min_seconds: float = 5.0, window_seconds: float = 60.0,
                tail: float = 2.5) -> tuple[float, str] | None:
    """How long a song lasts: (seconds, "loop") for a song that repeats --
    intro plus one pass of the loop -- or (seconds, "end") for one that stops
    playing notes (last note plus `tail` for its release).  None if neither
    is found within `max_seconds`.

    Runs only the CPU (no audio) and compares per-frame note-on events.  A
    reference stretch of `window_seconds` is taken at a few offsets (so a
    non-repeating intro doesn't hide the loop); the shortest period at which
    it recurs is the loop length, and walking backwards finds where the
    repetition begins."""
    log = _NoteLog()
    machine = C64(tune, log, song)
    machine.init()
    frames, times = [], []           # note events per play call, and its cycle
    start = machine.next_play
    end = start + int(max_seconds * tune.clock)
    while machine.next_play < end:
        times.append(machine.next_play)
        log.events = []
        machine.play_frame()
        frames.append(tuple(log.events))
    fps = len(frames) / max_seconds   # play calls per second (CIA tunes vary)
    seconds = lambda i: (times[min(i, len(times) - 1)] - start) / tune.clock
    n = len(frames)
    window = min(int(window_seconds * fps), n // 3)
    notes = [i for i, f in enumerate(frames) if f]
    if not notes:
        return None
    first, last = notes[0], notes[-1]
    if n - last > 30 * fps:                       # long silence at the end: it stopped
        return (seconds(last) + tail, "end")
    best = None
    for offset in (0, 10, 30):
        ref_at = first + int(offset * fps)
        ref = frames[ref_at:ref_at + window]
        if not any(ref):
            continue
        for period in range(int(min_seconds * fps), n - ref_at - window + 1):
            if best is not None and period >= best[0]:
                break
            if frames[ref_at + period:ref_at + period + window] == ref:
                best = (period, ref_at)
                break
    if best is None:
        return None
    period, at = best
    while at > 0 and frames[at - 1] == frames[at - 1 + period]:
        at -= 1
    return (seconds(at + period), "loop")
