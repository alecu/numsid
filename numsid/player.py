"""Glue: run a tune's engine and pull audio out of its sound chip.

Two kinds of tune are played the same way.  For a SID file the emulated
C64's 6502 runs the tune's own player code against the SID.  For an Amiga
MOD a ProTracker replay routine drives Paula.  Either way the engine
writes registers at known clock cycles, the chip renders at its own clock
and resamples to the output rate, and `Player` pulls audio out of it.  The machine-specific players live in
`c64.player` and `amiga.player`; `tunes` opens either kind.  It
also remembers recent register states, so displays can show what is being
heard rather than what was last rendered.
"""

from __future__ import annotations

import bisect
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCOPE_SIZE = 1 << 16             # samples kept per display tap


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


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int) -> None:
    """16-bit WAV; samples shaped (n,) for mono or (n, channels)."""
    pcm = samples if samples.dtype == np.int16 else (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1 if pcm.ndim == 1 else pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
