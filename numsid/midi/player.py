"""MIDI files, played by the sequencer on an emulated MT-32."""

from __future__ import annotations

from pathlib import Path

from ..player import Player, Readout
from . import maps, smf
from .detect import GM, MT32, detect
from .sequencer import Sequencer

PART_NAMES = [f"PART {i}" for i in range(1, 9)] + ["RHYTHM"]


class MidiPlayer(Player):
    """A Standard MIDI File on the MT-32.  A General MIDI file is mapped
    onto the MT-32's sounds and parts (see `maps`)."""

    channels = 2
    voice_label = "PART"
    info_lines = 3
    skip_label = "10 s"
    credit = "numsid  ·  Roland MT-32 emulated in Python + numpy"

    def __init__(self, tune: smf.MidiFile | str | Path, start: float = 0.0, *,
                 sample_rate: int = 44100, model: str | None = None,
                 filter_enabled: bool = True, gain: float = 1.0, scope_rate: int = 0,
                 roms_dir: str | Path | None = None):
        super().__init__(sample_rate, gain, scope_rate)
        from ..mt32 import control, pcm, roms
        from ..mt32.synth import MT32
        self.tune = tune if isinstance(tune, smf.MidiFile) else smf.load(tune)
        self.model = model or "MT-32"
        self.models = ("MT-32", "CM-32L")
        cinfo, cblob, _, pblob = roms.find(self.model, roms_dir)
        self.rom_info = cinfo
        self.synth_for, _ = detect(self.tune)
        gm = self.synth_for != MT32
        cmap = maps.channel_map(self.tune) if gm else None
        self.synth = MT32(control.parse(cblob, cinfo), pcm.PCMROM(pblob, cblob, cinfo),
                          sample_rate, scope_rate=scope_rate, gm=gm,
                          channel_map=cmap)
        self.synth.reverb_enabled = filter_enabled
        self.select(start)

    @property
    def help(self) -> str:
        return super().help.replace("F filter", "F reverb")

    @property
    def chip(self):
        return self.synth

    @property
    def title(self) -> str:
        return self.tune.title or self.tune.name

    @property
    def subtitle(self) -> str:
        return Path(self.tune.filename).name if self.tune.filename else ""

    @property
    def warnings(self) -> list[str]:
        return self.tune.warnings

    def select(self, seconds: float) -> None:
        """(Re)start `seconds` into the song."""
        self.synth.reset()
        self.sequencer = Sequencer(self.tune, self.synth.clock)
        self.offset = 0
        if seconds > 0:
            for e in self.sequencer.seek(int(seconds * self.synth.clock)):
                if e.status < 0xF0:
                    self.synth.message(e.status, e.data)
            # the synth's clock restarts at 0 here, so shift the song to match
            self.offset = int(seconds * self.synth.clock)
            self.sequencer.times = [t - self.offset for t in self.sequencer.times]
        self._restart_timeline()

    @property
    def position(self) -> float:
        return (self.synth.t + self.offset) / self.synth.clock

    def skip(self, direction: int) -> None:
        self.select(max(0.0, self.position + 10 * direction))

    def set_model(self, model: str) -> None:
        mute = self.synth.mute
        at = self.position
        self.__init__(self.tune, at, sample_rate=self.sample_rate, model=model,
                      filter_enabled=self.synth.reverb_enabled, gain=self.gain,
                      scope_rate=self.scope_rate)
        self.synth.mute = mute

    def _step(self) -> None:
        seq, synth = self.sequencer, self.synth
        nxt = seq.next_time()
        until = synth.t + 4096 if nxt is None else max(nxt, synth.t)
        until = min(until, synth.t + 4096)
        synth.advance(until)
        for _, e in seq.take(until + 1):
            if e.status < 0xF0:
                synth.message(e.status, e.data)
        parts = tuple((p.timbre.name if i < 8 and p.timbre else "rhythm",
                       len([n for n in p.notes if not n.released]),
                       sum(n.count for n in p.notes),
                       max((n.key for n in p.notes if not n.released), default=None),
                       p.volume) for i, p in enumerate(synth.parts))
        self.frames.append((synth.t, (synth.t, parts)))

    def chip_line(self) -> str:
        mode = "GM mapped" if self.synth.gm else "native"
        reverb = "reverb on" if self.synth.reverb_enabled else "reverb off"
        return (f"Roland {self.rom_info.model.split()[0]} {self.rom_info.version} · {mode} · "
                f"{reverb} · 32 kHz")

    def readout(self, state) -> Readout:
        if state is None:
            pos = self.position
            return Readout(f"{int(pos) // 60}:{pos % 60:04.1f}")
        t, state = state
        pos = (t + self.offset) / self.synth.clock
        position = f"{int(pos) // 60}:{pos % 60:04.1f}"
        lines, levels = [], []
        names = "C C# D D# E F F# G G# A A# B".split()
        for name, held, partials, key, volume in state:
            note = f"{names[key % 12]}{key // 12 - 1}" if key is not None else "---"
            lines.append([f"{name}", f"{note:<4} keys {held:2d}", f"partials {partials:2d}  vol {volume:3d}"])
            levels.append(min(1.0, partials / 8))
        used = sum(s[2] for s in state)
        chip = [f"PARTIALS {used:2d}/32", f"file for {self.synth_for}"]
        return Readout(position, lines, levels, chip)
