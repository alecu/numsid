"""SID tunes on the emulated C64: the player for displays and audio, and
song-length detection by watching the tune's note-ons."""

from __future__ import annotations

import math
from pathlib import Path

from ..player import Player, Readout
from . import psid
from .machine import C64
from .sid import SID

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
