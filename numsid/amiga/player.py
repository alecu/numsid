"""Amiga tracker modules: the ProTracker replay routine driving Paula."""

from __future__ import annotations

from pathlib import Path

from ..player import Player, Readout
from . import modfile, protracker
from .paula import Paula

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
