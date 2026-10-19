"""MED / OctaMED replay routine, driving Paula.

Like ProTracker, MED's player runs once per timer tick, reads a line every
`speed` ticks ("secondary tempo") and runs effects on the ticks between.
The differences that matter here: the tempo is set in MED's own units (see
`med_bpm`), blocks can have any number of lines, notes are numbered from
C-1 = 1 and those above the third octave wrap down into it on 4-channel
songs, track and master volumes scale every note, an instrument can have a
hold/decay volume envelope (an instrument number without a note, MED's
"hold" symbol, keeps the note going), and unless the song asks for
SoundTracker-style slides, slides run on a line's first tick as well.  The semantics follow
libopenmpt's reading of the format; the code is new.

Synthetic instruments run two small programs per note, stepped every
`speed` ticks: the volume table (set volume, ramp it, follow an envelope
read from a waveform, wait, jump) and the waveform table (pick the
waveform, ramp the pitch, arpeggio, vibrato, wait, jump).  A new waveform
is written to Paula's location and length registers, so it takes over when
the one playing reaches its end, as on the real machine.
"""

from __future__ import annotations

import math

from .medfile import FLAG2_MIX, FLAG_8CHANNEL, FLAG_FILTERON, OCTAVE_MAP, MedFile, MedSample
from .modfile import BLANK
from .protracker import PAL_CIA_CLOCK, PERIODS, VIBRATO, NullPaula

EIGHT_CHANNEL_BPM = (179, 164, 152, 141, 131, 123, 116, 110, 104, 99)
MIN_PERIOD, MAX_PERIOD = 113, 856 * 4
SINE = [round(127 * math.sin(2 * math.pi * i / 64)) for i in range(64)]
FULL = 16384                    # synth volume factors: 16384 is 1.0

# Synth table commands
SPD, WAI, CHD, CHU, EN1_VBD, EN2_VBS, RES, VWF, JWS, ARP, ARE, HLT, JMP, END = (
    0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xFA, 0xFC, 0xFD, 0xFB, 0xFE, 0xFF)


def _command_starts(table: bytes, volume: bool) -> list[int]:
    """Byte positions where commands start (jumps land on the first one at or
    after their target), skipping the arguments and arpeggio notes."""
    starts, pos = [], 0
    with_arg = {JMP, JWS, EN2_VBS, EN1_VBD, CHU, CHD, WAI, SPD} | ({VWF} if not volume else set())
    while pos < len(table):
        starts.append(pos)
        b = table[pos]
        pos += 1
        if b in with_arg:
            pos += 1
        elif b == ARP:
            while pos < len(table) and table[pos] < 0x80:
                pos += 1
            pos += 1                                # the byte that ends the list
    return starts


class _Script:
    """One of a synthetic note's two programs."""

    def __init__(self, table: bytes, speed: int, volume: bool):
        self.table = table
        self.volume = volume
        self.starts = _command_starts(table, volume)
        self.pos = 0
        self.speed = 1              # until the first step sets the table's own
        self.first_speed = speed
        self.steps_remain = 0
        self.wait = 0
        self.factor = FULL          # volume factor
        self.volume_step = 0
        self.period_add = 0
        self.period_step = 0
        self.envelope = None        # (waveform, looping) for EN1/EN2
        self.envelope_pos = 0

    def jump(self, target: int) -> None:
        self.pos = next((p for p in self.starts if p >= target), len(self.table))
        self.wait = 0


class _Synth:
    """A synthetic or hybrid note in progress."""

    def __init__(self, s: MedSample, hold: int):
        self.volume = _Script(s.volume_table, s.volume_speed, True)
        self.wave = _Script(s.waveform_table, s.waveform_speed, False)
        self.hold = hold if hold else None
        self.hold_started = False
        self.arpeggio: tuple = ()
        self.arp_pos = 0
        self.vibrato_speed = self.vibrato_depth = self.vibrato_pos = 0
        self.vibrato_env = None
        self.semitones = 0


def med_bpm(mod: MedFile, tempo: int) -> float:
    """Tick rate of a MED tempo value, as a ProTracker-style BPM (ticks last
    2.5 / BPM seconds)."""
    is8, mix = mod.flags & FLAG_8CHANNEL, mod.flags2 & FLAG2_MIX
    if mod.bpm_mode and not is8:
        return 111.5 if tempo < 7 else tempo * mod.lines_per_beat / 4
    if is8 and tempo > 0:
        return EIGHT_CHANNEL_BPM[min(tempo, 10) - 1]
    if not mix and 0 < tempo <= 10:                 # SoundTracker-compatible tempos
        return 6 * 1773447 / 14500 / tempo
    if mix and tempo < 8:
        return 157.86
    return max(tempo, 1) / 0.264


class _Voice:
    def __init__(self, index: int):
        self.index = index
        self.sample = 0          # instrument number (1-based)
        self.cmd = 0             # command << 8 | parameter, for displays
        self.effect = (0, 0)
        self.volume = 0
        self.finetune = 0
        self.note = 0            # note as a period-table index (0 is C-1), before wrapping
        self.period = 0
        self.octave = 0          # which octave sample of a multi-octave instrument
        self.wanted = 0          # tone portamento target
        self.porta_speed = 0
        self.vibrato = 0         # speed << 4 | depth
        self.vibrato_pos = 0
        self.tremolo = 0
        self.tremolo_pos = 0
        self.loop = (BLANK, 1)
        self.trigger_at = -1     # tick of a delayed note
        self.cut_at = -1
        self.retrigger = ()
        self.env_tick = -1       # ticks since the note started, for hold/decay; -1: none
        self.synth = None        # _Synth of a synthetic or hybrid note


class MED:
    def __init__(self, mod: MedFile, hw, position: int = 0, line: int = 0):
        self.mod = mod
        self.hw = hw
        self.voices = [_Voice(i) for i in range(mod.channels)]
        self.speed = min(max(mod.speed, 1), 32)
        self.tick_no = self.speed - 1
        self.tempo_bpm = med_bpm(mod, mod.tempo)
        self.pos = position % len(mod.orders)
        self.line = line
        self.started = False
        self.jump = None         # (position, line) to go to after this line
        self.repeat = 0          # pattern delay lines left
        self.loop_line = 0
        self.loopcount = 0
        self.fast = not mod.st_slides
        self.hardware = mod.version < 2 or not mod.flags2 & FLAG2_MIX
        self.led = bool(mod.flags & FLAG_FILTERON)
        hw.set_led(self.led)
        self._t = 0.0
        self.time = 0
        self.playing = True
        self.row_read = None
        self.now = (self.pos, line)

    @property
    def bpm(self) -> int:
        return round(self.tempo_bpm)

    def tick(self) -> None:
        """Run the replay routine at `time`, then move `time` to the next
        tick.  A tempo set on a line already times the tick that reads it."""
        self.row_read = None
        if self.playing:
            self._tick()
        self._t += PAL_CIA_CLOCK * 2.5 / self.tempo_bpm
        self.time = round(self._t)

    def _tick(self) -> None:
        self.tick_no += 1
        if self.tick_no >= self.speed:
            self.tick_no = 0
            if self.repeat:
                self.repeat -= 1
                for v in self.voices:
                    self._effects(v, 0)
                return
            self._advance()
            self._read_line()
        else:
            for v in self.voices:
                self._effects(v, self.tick_no)

    def _advance(self) -> None:
        if self.started:
            if self.jump is not None:
                self.pos, self.line = self.jump
                self.jump = None
            else:
                self.line += 1
                if self.line >= len(self._block()):
                    self.pos, self.line = self.pos + 1, 0
            if self.pos >= len(self.mod.orders):
                self.pos = 0
            self.line = min(self.line, len(self._block()) - 1)
        self.started = True

    def _block(self):
        return self.mod.patterns[self.mod.orders[self.pos]]

    # ---- a line's first tick ------------------------------------------------------------
    def _read_line(self) -> None:
        self.row_read = self.now = (self.pos, self.line)
        cells = self._block()[self.line]
        pages = self.mod.pages.get(self.mod.orders[self.pos], ())
        started = []
        for v in self.voices:
            note, ins, cmd, param = cells[v.index] if v.index < len(cells) else (0, 0, 0, 0)
            v.cmd = cmd << 8 | param
            v.effect = (cmd, param)
            v.trigger_at, v.cut_at, v.retrigger = -1, -1, ()
            if ins:
                if not note and ins == v.sample and v.env_tick >= 0:
                    v.env_tick = 0          # a hold symbol: the note goes on at full volume
                v.sample = ins
                s = self.mod.samples[ins - 1]
                v.volume, v.finetune = s.volume, s.finetune
            if cmd == 0x15:
                v.finetune = param & 15
            if note == 0x80:
                v.volume = 0
            elif note:
                index, octave = self._note_index(v, note)
                if cmd in (3, 5):
                    v.wanted = self._period(v, index)
                elif cmd == 0xF and param == 0xFD:     # set pitch, no new note
                    v.note = index
                    v.period = self._period(v, index)
                else:
                    v.note, v.octave = index, octave
                    v.period = self._period(v, index)
                    v.vibrato_pos = v.tremolo_pos = 0
                    delay = param >> 4 if cmd == 0x1F else 3 if (cmd, param) == (0xF, 0xF2) else 0
                    if delay:
                        v.trigger_at = delay
                    elif self._trigger(v, 0x100 * param if cmd == 0x19 else 0):
                        started.append(v)
            self._line_effects(v, cmd, param)
            for page in pages:                      # more effect columns
                more = page[self.line][v.index] if v.index < len(page[self.line]) else (0, 0)
                if any(more):
                    self._line_effects(v, *more)
                    if v.effect == (0, 0):          # the one that runs on every tick
                        v.effect = more
            self._effects(v, 0)
        mask = sum(1 << v.index for v in started)
        if mask:
            self.hw.dma(mask, True)
            for v in started:
                self.hw.write_location(v.index, v.loop[0])
                self.hw.write_length(v.index, v.loop[1])

    def _note_index(self, v: _Voice, note: int) -> tuple[int, int]:
        """Period-table index (0 is C-1) a note plays at with the current
        instrument, and the octave sample it uses.  Very high notes come
        down as in OctaMED: past B-7 by 9 octaves (not in blocks saved by
        MED SoundStudio), and any still past B-6 by whole octaves."""
        s = self.mod.samples[v.sample - 1] if v.sample else None
        q = note - 1 + self.mod.transpose - (24 if self.mod.version > 2 else 0)
        if q > 83 and not self._soundstudio():
            q -= 108
        elif q > 71:                                # above B-9: down by whole octaves
            q -= (q - 60) // 12 * 12
        q += s.transpose if s else 0
        if s and s.kind == "synth" and self.mod.version <= 2:
            q -= 24
        octave = 0
        if s and len(s.octaves) > 1:
            which, shift = OCTAVE_MAP[len(s.octaves)]
            o = min(max(q // 12, 0), 7)
            octave, q = which[o], q + shift[o]
        return q, octave

    def _soundstudio(self) -> bool:
        flags = self.mod.soundstudio
        block = self.mod.orders[self.pos]
        return block < len(flags) and flags[block]

    def _period(self, v: _Voice, q: int) -> int:
        """Period of a table index.  Notes above the top octave wrap down
        into it (on hardware-mixed songs, those past B-3); notes below C-1
        are the table's periods doubled."""
        limit = 36 if self.hardware else 48
        if q >= limit:
            q -= 12 * ((q - limit) // 12 + 1)
        down = up = 0
        while q < 0:
            q, down = q + 12, down + 1
        while q >= 36:
            q, up = q - 12, up + 1
        return (PERIODS[v.finetune * 37 + q] << down) >> up

    def _trigger(self, v: _Voice, offset: int = 0) -> bool:
        hw = self.hw
        s = self.mod.samples[v.sample - 1] if v.sample else None
        hw.dma(1 << v.index, False)
        synthetic = s is not None and s.kind in ("synth", "hybrid")
        v.env_tick = 0 if s and s.hold and not synthetic else -1
        v.synth = _Synth(s, s.hold) if synthetic and s.octaves else None
        if not s or not s.octaves:
            v.loop = (BLANK, 1)
            return False
        address, length, loop_start, loop_length = s.octaves[min(v.octave, len(s.octaves) - 1)]
        offset = min(offset, max(length - 2, 0))
        hw.write_location(v.index, address + offset)
        hw.write_length(v.index, min(max((length - offset + 1) // 2, 1), 0xFFFF))
        hw.write_period(v.index, v.period)
        v.loop = (address + loop_start, min(loop_length // 2, 0xFFFF)) if loop_length > 2 else (BLANK, 1)
        return True

    def _line_effects(self, v: _Voice, cmd: int, param: int) -> None:
        """Commands that act once, when the line is read."""
        mod = self.mod
        if cmd == 0x09 and 0 < param <= 0x20:
            self.speed = param
        elif cmd == 0x0B:
            self.jump = (param % len(mod.orders), 0)
        elif cmd == 0x0C:
            if not mod.hex_volumes:
                if param < 0x99:
                    v.volume = min((param >> 4) * 10 + (param & 15), 64)
            elif self._soundstudio():               # 7-bit volumes
                v.volume = ((param & 0x7F) + 1) // 2
            else:
                v.volume = min(param & 0x7F, 64)
        elif cmd == 0x0F:
            if param == 0:
                self.jump = (self.pos + 1, 0)
            elif param <= 0xF0:
                self.tempo_bpm = 112.0 if param < 3 else med_bpm(mod, param)
            elif param == 0xF1:                     # play twice: retrigger every 3 ticks
                v.retrigger = tuple(range(3, 64, 3))
            elif param == 0xF3:                     # three times: every 2 ticks
                v.retrigger = tuple(range(2, 64, 2))
            elif param in (0xF8, 0xF9):
                self.led = param == 0xF9
                self.hw.set_led(self.led)
            elif param == 0xFE:
                self.playing = False
            elif param == 0xFF:
                v.volume = 0
        elif cmd == 0x03 and param:
            v.porta_speed = param
        elif cmd == 0x04:                           # vibrato, twice ProTracker's depth
            self._set_vibrato(v, (param & 0xF0) | min((param & 0x0F) * 2, 0x0F))
        elif cmd == 0x14:                           # ProTracker-compatible vibrato
            self._set_vibrato(v, param)
        elif cmd == 0x07:
            if param & 0x0F:
                v.tremolo = (v.tremolo & 0xF0) | (param & 0x0F)
            if param & 0xF0:
                v.tremolo = (param & 0xF0) | (v.tremolo & 0x0F)
        elif cmd == 0x11:
            v.period = max(v.period - (param & 15), MIN_PERIOD)
        elif cmd == 0x12:
            v.period = min(v.period + (param & 15), MAX_PERIOD)
        elif cmd == 0x16:
            if not param:
                self.loop_line = self.line
            elif self.loopcount == 0:
                self.loopcount = param
                self.jump = (self.pos, self.loop_line)
            else:
                self.loopcount -= 1
                if self.loopcount:
                    self.jump = (self.pos, self.loop_line)
        elif cmd == 0x18:
            v.cut_at = param
        elif cmd == 0x1A:
            v.volume = min(v.volume + (param & 15), 64)
        elif cmd == 0x1B:
            v.volume = max(v.volume - (param & 15), 0)
        elif cmd == 0x1D:
            self.jump = (self.pos + 1, param)
        elif cmd == 0x1E:
            self.repeat = param & 15
        elif cmd == 0x0E and v.synth:
            v.synth.wave.jump(param)
            v.synth.wave.steps_remain = 0
        elif cmd == 0x1F and not param & 0xF0 and param & 15:
            v.retrigger = tuple(range(param & 15, 64, param & 15))

    @staticmethod
    def _set_vibrato(v: _Voice, param: int) -> None:
        if param & 0x0F:
            v.vibrato = (v.vibrato & 0xF0) | (param & 0x0F)
        if param & 0xF0:
            v.vibrato = (param & 0xF0) | (v.vibrato & 0x0F)

    # ---- every tick -----------------------------------------------------------------
    def _effects(self, v: _Voice, tick: int) -> None:
        hw = self.hw
        cmd, param = v.effect
        if tick and (tick == v.trigger_at or tick in v.retrigger):
            if self._trigger(v):
                hw.dma(1 << v.index, True)
                hw.write_location(v.index, v.loop[0])
                hw.write_length(v.index, v.loop[1])
        if tick == v.cut_at:
            v.volume = 0
        slide = tick > 0 or self.fast
        period = v.period
        if cmd == 0x00 and param:                   # arpeggio
            k = tick % 3
            add = 0 if k == 0 else param >> 4 if k == 1 else param & 15
            period = self._period(v, v.note + add) if add else v.period
        elif cmd == 0x01 and slide:
            v.period = period = max(v.period - param, MIN_PERIOD)
        elif cmd == 0x02 and slide:
            v.period = period = min(v.period + param, MAX_PERIOD)
        elif cmd in (0x03, 0x05) and slide and v.wanted:
            if v.period > v.wanted:
                v.period = max(v.period - v.porta_speed, v.wanted)
            else:
                v.period = min(v.period + v.porta_speed, v.wanted)
            if v.period == v.wanted:
                v.wanted = 0
            period = v.period
        elif cmd in (0x04, 0x06, 0x14) and tick:    # vibrato, as ProTracker's
            depth = VIBRATO[v.vibrato_pos >> 2 & 0x1F] * (v.vibrato & 0xF) >> 7
            period = v.period + depth if v.vibrato_pos < 128 else v.period - depth
            v.vibrato_pos = (v.vibrato_pos + (v.vibrato >> 2 & 0x3C)) & 0xFF
        if cmd in (0x05, 0x06, 0x0A, 0x0D) and slide:
            if param & 0xF0:
                v.volume = min(v.volume + (param >> 4), 64)
            else:
                v.volume = max(v.volume - (param & 15), 0)
        volume = v.volume
        if cmd == 0x07 and tick:                    # tremolo
            depth = VIBRATO[v.tremolo_pos >> 2 & 0x1F] * (v.tremolo & 0xF) >> 6
            volume = min(volume + depth, 64) if v.tremolo_pos < 128 else max(volume - depth, 0)
            v.tremolo_pos = (v.tremolo_pos + (v.tremolo >> 2 & 0x3C)) & 0xFF
        if v.synth:
            period, volume = self._synth_tick(v, period, volume)
        if period:
            hw.write_period(v.index, period)
        mod = self.mod
        if v.env_tick >= 0:
            volume = volume * self._envelope(v) >> 6
            v.env_tick += 1
        hw.write_volume(v.index, volume * mod.track_volumes[v.index] * mod.master_volume >> 12)

    # ---- synthetic instruments ---------------------------------------------------------
    def _synth_tick(self, v: _Voice, period: int, volume: int) -> tuple[int, int]:
        sy = v.synth
        s = self.mod.samples[v.sample - 1]
        if sy.hold is not None and sy.hold_started:     # hold over: on to the decay part
            if sy.hold == 0:
                sy.hold = None
                sy.volume.jump(s.decay)
            else:
                sy.hold -= 1
        for script in (sy.volume, sy.wave):
            self._step(v, sy, s, script)
        sy.hold_started = True
        if sy.arpeggio:
            sy.semitones = sy.arpeggio[sy.arp_pos]
            sy.arp_pos = (sy.arp_pos + 1) % len(sy.arpeggio)
        vibrato = 0
        if sy.vibrato_depth:
            at = sy.vibrato_pos // 16
            value = (SINE[(at * 2) % 64] if sy.vibrato_env is None
                     else self._waveform_byte(s, sy.vibrato_env, at))
            vibrato = value * sy.vibrato_depth // 256
            sy.vibrato_pos = (sy.vibrato_pos + sy.vibrato_speed) % 512
        if period:
            p = period * 2 ** (-sy.semitones / 12) if sy.semitones else period
            p += sy.volume.period_add + sy.wave.period_add + vibrato
            period = max(round(p), MIN_PERIOD) if p != period else period
        volume = volume * sy.volume.factor // FULL * sy.wave.factor // FULL
        return period, volume

    def _step(self, v: _Voice, sy: _Synth, s: MedSample, sc: _Script) -> None:
        """One tick of a table: every `speed` ticks it runs commands until one
        that ends the step (a volume or waveform, a wait, an arpeggio)."""
        if not sc.speed:
            return
        if sc.steps_remain:
            sc.steps_remain -= 1
            return
        sc.steps_remain = sc.speed - 1
        if sc.volume_step:
            sc.factor = min(max(sc.factor + sc.volume_step, 0), FULL)
        if sc.period_step:
            sc.period_add -= sc.period_step
        if sc.envelope is not None:
            w, looping = sc.envelope
            sc.factor = min(max((self._waveform_byte(s, w, sc.envelope_pos) + 128) * 64, 0), FULL)
            if sc.envelope_pos < 127:
                sc.envelope_pos += 1
            elif looping:
                sc.envelope_pos = 0
        if sc.wait:
            sc.wait -= 1
            return
        if sc.first_speed is not None:          # a speed of 0 runs this one step only
            sc.speed, sc.first_speed = sc.first_speed, None
            sc.steps_remain = max(sc.speed - 1, 0)
        table, jumps = sc.table, 0
        while sc.pos < len(table):
            b = table[sc.pos]
            sc.pos += 1
            arg = table[sc.pos] if sc.pos < len(table) else 0
            if b in (END, HLT):
                sc.pos = len(table)
                return
            if b == JMP:
                sc.jump(arg)
                jumps += 1
                if jumps > 10:
                    return
            elif b == ARE:
                pass
            elif b == ARP:
                notes = []
                while sc.pos < len(table) and table[sc.pos] < 0x80:
                    notes.append(table[sc.pos])
                    sc.pos += 1
                sc.pos += 1
                if notes:
                    sy.arpeggio, sy.arp_pos = tuple(notes), 0
                    return
            elif b == JWS:
                sc.pos += 1
                other = sy.wave if sc.volume else sy.volume
                other.jump(arg)
                other.steps_remain = 0
            elif b == VWF:
                if not sc.volume:
                    sc.pos += 1
                    sy.vibrato_env = arg
            elif b == RES:
                if not sc.volume:                   # reset the pitch ramp
                    sy.semitones = 0
                    sc.period_add = 0
                    return
            elif b in (EN2_VBS, EN1_VBD):
                sc.pos += 1
                if sc.volume:
                    sc.envelope, sc.envelope_pos = (arg & 0x3F, b == EN2_VBS), 0
                elif b == EN2_VBS:
                    sy.vibrato_speed = arg
                else:
                    sy.vibrato_depth = arg
            elif b in (CHU, CHD):
                sc.pos += 1
                step = arg if b == CHU else -arg
                if sc.volume:
                    sc.volume_step = step * 256
                else:
                    sc.period_step = step
            elif b == WAI:
                sc.pos += 1
                sc.wait = max(arg, 1) - 1
                return
            elif b == SPD:
                sc.pos += 1
                sc.speed = arg
            elif sc.volume:
                if b <= 64:
                    sc.factor = b * 256
                    return
            else:                                   # pick a waveform
                if b < len(s.waveforms):
                    address, length = s.waveforms[b]
                    v.loop = (address, max(length // 2, 1))
                    self.hw.write_location(v.index, v.loop[0])
                    self.hw.write_length(v.index, v.loop[1])
                return

    def _waveform_byte(self, s: MedSample, w: int, pos: int) -> int:
        if w >= len(s.waveforms):
            return 0
        address, length = s.waveforms[w]
        return int(self.hw.ram[address + pos]) if pos < length else 0

    def _envelope(self, v: _Voice) -> int:
        """Hold/decay level, 0..64: full for `hold` ticks, then down to
        silence by `decay` per tick."""
        s = self.mod.samples[v.sample - 1]
        t, top = v.env_tick, s.hold - 1
        end = s.hold + (64 // s.decay if s.decay else 0)
        if t <= top:
            return 64
        if t >= end:
            return 0
        return 64 * (end - t) // (end - top)


def seek(mod: MedFile, hw, position: int) -> MED:
    """A replayer about to play `position`, with the speed, tempo and filter
    state the song has when it first gets there."""
    position %= len(mod.orders)
    if not position:
        return MED(mod, hw)
    scout = MED(mod, NullPaula(mod))
    end = 3600 * PAL_CIA_CLOCK
    while scout.playing and scout.time < end:
        before = (scout.speed, scout.tempo_bpm, scout.led)
        scout.tick()
        if scout.row_read is not None and scout.row_read[0] == position:
            med = MED(mod, hw, position, scout.row_read[1])
            med.speed, med.tempo_bpm, med.led = before
            med.tick_no = med.speed - 1
            hw.set_led(med.led)
            return med
    return MED(mod, hw, position)


def song_length(mod: MedFile, position: int = 0, max_seconds: float = 3600.0,
                tail: float = 2.5) -> tuple[float, str] | None:
    """As protracker.song_length: (seconds, "loop") when the song gets back
    to a line it has played, (seconds, "end") when it stops (FFE), or None."""
    med = seek(mod, NullPaula(mod), position)
    seen = set()
    end = max_seconds * PAL_CIA_CLOCK
    while med.time < end:
        t = med.time
        med.tick()
        if not med.playing:
            return (med.time / PAL_CIA_CLOCK + tail, "end")
        if med.row_read is not None:
            key = (med.row_read, med.loopcount)
            if key in seen:
                return (t / PAL_CIA_CLOCK, "loop")
            seen.add(key)
    return None
