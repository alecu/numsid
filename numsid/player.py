"""Glue: run a PSID tune on the emulated C64 and pull audio out of the SID."""

from __future__ import annotations

import bisect
import wave
from collections import deque
from pathlib import Path

import numpy as np

from . import psid
from .c64 import C64
from .sid import SID


class Player:
    def __init__(self, tune: psid.PSIDFile | str | Path, song: int | None = None, *,
                 sample_rate: int = 44100, model: str | None = None,
                 filter_enabled: bool = True, gain: float = 1.0, scope_rate: int = 0):
        self.tune = tune if isinstance(tune, psid.PSIDFile) else psid.load(tune)
        self.sample_rate = sample_rate
        self.model = model or self.tune.preferred_model
        self.filter_enabled = filter_enabled
        self.gain = gain
        self.scope_rate = scope_rate
        self.sid = SID(self.tune.clock, sample_rate, self.model,
                       filter_enabled=filter_enabled, scope_rate=scope_rate)
        self.select(song or self.tune.start_song)

    def set_model(self, model: str) -> None:
        """Swap in a different SID chip and restart the current song."""
        old = self.sid
        self.model = model
        self.sid = SID(self.tune.clock, self.sample_rate, model,
                       filter_enabled=old.filter_enabled, scope_rate=self.scope_rate)
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
        self.samples_played = 0
        self.first_sample = self.sid.resampler.k     # output index of our sample 0
        self.frames: deque = deque(maxlen=400)       # (cycle, registers, envelopes)

    @property
    def warnings(self) -> list[str]:
        return self.c64.warnings

    @property
    def seconds(self) -> float:
        return self.samples_played / self.sample_rate

    def render(self, n: int) -> np.ndarray:
        """Next n output samples as float32 in [-1, 1]."""
        sid = self.sid
        while sid.out_len < n:
            cycle = self.c64.play_frame()
            sid.advance(cycle)
            self.frames.append((cycle, bytes(sid.regs), tuple(v.env for v in sid.voices)))
        out = sid.take(n)
        if self.gain != 1.0:
            out *= self.gain
        np.clip(out, -1.0, 1.0, out=out)
        self.samples_played += n
        return out

    def render_int16(self, n: int) -> np.ndarray:
        return (self.render(n) * 32767.0).astype(np.int16)

    # ---- position bookkeeping for displays ------------------------------------
    def sample_index(self, seconds: float) -> int:
        """Output-sample index (as used by the SID's scope taps) heard at
        `seconds` into the current song."""
        return self.first_sample + int(seconds * self.sample_rate)

    def cycle_at(self, seconds: float) -> int:
        return self.sample_index(seconds) * self.tune.clock // self.sample_rate

    def state_at(self, seconds: float):
        """(registers, envelope levels) of the last frame rendered before the
        moment heard at `seconds`, or None."""
        cycle = self.cycle_at(seconds)
        frames = list(self.frames)
        i = bisect.bisect_right([f[0] for f in frames], cycle) - 1
        return frames[i][1:] if i >= 0 else None


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int) -> None:
    pcm = samples if samples.dtype == np.int16 else (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


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


def song_length(tune: psid.PSIDFile, song: int, max_seconds: float = 600.0,
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
    fps = tune.clock / machine.frame_cycles
    frames = []
    for _ in range(int(max_seconds * fps)):
        log.events = []
        machine.play_frame()
        frames.append(tuple(log.events))
    n = len(frames)
    window = min(int(window_seconds * fps), n // 3)
    notes = [i for i, f in enumerate(frames) if f]
    if not notes:
        return None
    first, last = notes[0], notes[-1]
    if n - last > 30 * fps:                       # long silence at the end: it stopped
        return (last / fps + tail, "end")
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
    return ((at + period) / fps, "loop")
