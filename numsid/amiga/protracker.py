"""ProTracker 2.3D replay routine.

On an Amiga a MOD is played by a replay routine that a CIA timer interrupt
calls 50 times a second (at 125 BPM).  Each call is a "tick": every
`speed` ticks it reads the next pattern row and triggers notes, and on the
ticks in between it runs the effects (slides, vibrato, arpeggio, ...).  All
it does to make sound is write Paula's registers, as the 6502 player code of
a SID tune writes the SID's.

This is a Python model of ProTracker 2.3D's routine, quirks included, which
is what most MODs were written against.  It follows the behaviour as
documented by 8bitbubsy's pt2-clone, a faithful port of the original 68000
code; the code here is new.  Some of the quirks it keeps:

* sample offset (9xx) is applied twice, so retriggers start further in,
* arpeggio on a finetune -1 sample reads past the period table,
* the tremolo's ramp waveform looks at the vibrato position,
* a BPM change (Fxx >= 32) takes effect one tick late, when the CIA timer
  next reloads,
* an instrument number without a note swaps the sample in when the
  playing one reaches its end (Paula picks up the new loop registers),
* EFx ("funk repeat") inverts bytes of the sample in memory as it plays.

E8x (Karplus-Strong) is ignored, as pt2-clone does by default.  F00 stops
the song; the voices keep playing whatever loops they are in.
"""

from __future__ import annotations

import numpy as np

from .modfile import BLANK, ROWS, ModFile

# Periods for the 36 notes C-1..B-3 at each finetune (0..7, then -8..-1),
# each row ended by a 0.  The 15 values after the table are what PT's code
# finds in memory there, which arpeggio on finetune -1 samples reads.
PERIODS = [
    856, 808, 762, 720, 678, 640, 604, 570, 538, 508, 480, 453,
    428, 404, 381, 360, 339, 320, 302, 285, 269, 254, 240, 226,
    214, 202, 190, 180, 170, 160, 151, 143, 135, 127, 120, 113, 0,
    850, 802, 757, 715, 674, 637, 601, 567, 535, 505, 477, 450,
    425, 401, 379, 357, 337, 318, 300, 284, 268, 253, 239, 225,
    213, 201, 189, 179, 169, 159, 150, 142, 134, 126, 119, 113, 0,
    844, 796, 752, 709, 670, 632, 597, 563, 532, 502, 474, 447,
    422, 398, 376, 355, 335, 316, 298, 282, 266, 251, 237, 224,
    211, 199, 188, 177, 167, 158, 149, 141, 133, 125, 118, 112, 0,
    838, 791, 746, 704, 665, 628, 592, 559, 528, 498, 470, 444,
    419, 395, 373, 352, 332, 314, 296, 280, 264, 249, 235, 222,
    209, 198, 187, 176, 166, 157, 148, 140, 132, 125, 118, 111, 0,
    832, 785, 741, 699, 660, 623, 588, 555, 524, 495, 467, 441,
    416, 392, 370, 350, 330, 312, 294, 278, 262, 247, 233, 220,
    208, 196, 185, 175, 165, 156, 147, 139, 131, 124, 117, 110, 0,
    826, 779, 736, 694, 655, 619, 584, 551, 520, 491, 463, 437,
    413, 390, 368, 347, 328, 309, 292, 276, 260, 245, 232, 219,
    206, 195, 184, 174, 164, 155, 146, 138, 130, 123, 116, 109, 0,
    820, 774, 730, 689, 651, 614, 580, 547, 516, 487, 460, 434,
    410, 387, 365, 345, 325, 307, 290, 274, 258, 244, 230, 217,
    205, 193, 183, 172, 163, 154, 145, 137, 129, 122, 115, 109, 0,
    814, 768, 725, 684, 646, 610, 575, 543, 513, 484, 457, 431,
    407, 384, 363, 342, 323, 305, 288, 272, 256, 242, 228, 216,
    204, 192, 181, 171, 161, 152, 144, 136, 128, 121, 114, 108, 0,
    907, 856, 808, 762, 720, 678, 640, 604, 570, 538, 508, 480,
    453, 428, 404, 381, 360, 339, 320, 302, 285, 269, 254, 240,
    226, 214, 202, 190, 180, 170, 160, 151, 143, 135, 127, 120, 0,
    900, 850, 802, 757, 715, 675, 636, 601, 567, 535, 505, 477,
    450, 425, 401, 379, 357, 337, 318, 300, 284, 268, 253, 238,
    225, 212, 200, 189, 179, 169, 159, 150, 142, 134, 126, 119, 0,
    894, 844, 796, 752, 709, 670, 632, 597, 563, 532, 502, 474,
    447, 422, 398, 376, 355, 335, 316, 298, 282, 266, 251, 237,
    223, 211, 199, 188, 177, 167, 158, 149, 141, 133, 125, 118, 0,
    887, 838, 791, 746, 704, 665, 628, 592, 559, 528, 498, 470,
    444, 419, 395, 373, 352, 332, 314, 296, 280, 264, 249, 235,
    222, 209, 198, 187, 176, 166, 157, 148, 140, 132, 125, 118, 0,
    881, 832, 785, 741, 699, 660, 623, 588, 555, 524, 494, 467,
    441, 416, 392, 370, 350, 330, 312, 294, 278, 262, 247, 233,
    220, 208, 196, 185, 175, 165, 156, 147, 139, 131, 123, 117, 0,
    875, 826, 779, 736, 694, 655, 619, 584, 551, 520, 491, 463,
    437, 413, 390, 368, 347, 328, 309, 292, 276, 260, 245, 232,
    219, 206, 195, 184, 174, 164, 155, 146, 138, 130, 123, 116, 0,
    868, 820, 774, 730, 689, 651, 614, 580, 547, 516, 487, 460,
    434, 410, 387, 365, 345, 325, 307, 290, 274, 258, 244, 230,
    217, 205, 193, 183, 172, 163, 154, 145, 137, 129, 122, 115, 0,
    862, 814, 768, 725, 684, 646, 610, 575, 543, 513, 484, 457,
    431, 407, 384, 363, 342, 323, 305, 288, 272, 256, 242, 228,
    216, 203, 192, 181, 171, 161, 152, 144, 136, 128, 121, 114, 0,
    774, 1800, 2314, 3087, 4113, 4627, 5400, 6426, 6940, 7713, 8739, 9253, 24625, 12851, 13365,
]
VIBRATO = [0, 24, 49, 74, 97, 120, 141, 161, 180, 197, 212, 224, 235, 244, 250, 253,
           255, 253, 250, 244, 235, 224, 212, 197, 180, 161, 141, 120, 97, 74, 49, 24]
FUNK = [0, 5, 6, 7, 8, 10, 11, 13, 16, 19, 22, 26, 32, 43, 64, 128]
NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]

PAL_CIA_CLOCK = 709379
DEFAULT_SPEED = 6


def _s16(x: int) -> int:
    """PT keeps periods in signed 16-bit words."""
    return ((x + 0x8000) & 0xFFFF) - 0x8000


def cia_period(bpm: int) -> int:
    """CIA timer value ProTracker uses for a BPM; a tick lasts one cycle more."""
    return 1773447 // bpm


def note_name(period: int) -> str:
    """Tracker note name (C-1 is period 856) of the nearest finetune-0 note."""
    if not period:
        return "---"
    best = min(range(36), key=lambda i: abs(PERIODS[i] - period))
    return f"{NOTE_NAMES[best % 12]}{best // 12 + 1}"


class _Voice:
    """A channel's replayer state (PT's n_* variables)."""

    def __init__(self, index: int):
        self.index = index
        self.note = 0            # n_note: instrument high nibble + period, as in the pattern
        self.cmd = 0             # n_cmd: instrument low nibble + effect + parameter
        self.start = BLANK       # sample start address
        self.length = 1          # words to play from start
        self.loopstart = BLANK
        self.replen = 1          # loop length in words
        self.wavestart = BLANK   # EFx position
        self.period = 0
        self.finetune = 0
        self.volume = 0
        self.toneportdirec = 0
        self.toneportspeed = 0
        self.wantedperiod = 0
        self.vibratocmd = 0
        self.vibratopos = 0
        self.tremolocmd = 0
        self.tremolopos = 0
        self.wavecontrol = 0
        self.glissfunk = 0
        self.sampleoffset = 0
        self.pattpos = 0         # E60 loop row
        self.loopcount = 0
        self.funkoffset = 0
        self.sample = 0          # last instrument number (for displays)


class NullPaula:
    """A Paula that ignores writes, for running the replayer without sound."""

    def __init__(self, mod: ModFile):
        self.ram = np.frombuffer(mod.ram, dtype=np.int8).copy()

    def write_location(self, ch, address): pass
    def write_length(self, ch, words): pass
    def write_period(self, ch, period): pass
    def write_volume(self, ch, volume): pass
    def dma(self, mask, on): pass
    def set_led(self, on): pass


class ProTracker:
    def __init__(self, mod: ModFile, hw, position: int = 0):
        self.mod = mod
        self.hw = hw
        self.ram = hw.ram                         # EFx writes to sample memory
        self.voices = [_Voice(i) for i in range(mod.channels)]
        self.speed = DEFAULT_SPEED
        self.tick_no = self.speed - 1             # so that the first tick reads a row
        self.pos = position % len(mod.orders)
        self.row = 0
        self.pbreak_pos = 0
        self.pos_jump = False
        self.pbreak_flag = False
        self.low_mask = 0xFF
        self.patt_del = 0
        self.patt_del2 = 0
        self.dmacon = 0
        self.cia_period = mod.cia_period
        self.led = False
        self.time = 0            # E-clock cycle of the next tick
        self.playing = True
        self.row_read = None     # (position, row) read on the last tick, if any
        self.now = (self.pos, 0)  # (position, row) playing after the last tick

    @property
    def bpm(self) -> int:
        return round(PAL_CIA_CLOCK * 2.5 / (self.cia_period + 1))

    # ---- the interrupt ----------------------------------------------------------------
    def tick(self) -> None:
        """Run the replay routine at `time`, then move `time` to the next
        interrupt.  The tick lasts by the timer value it started with: a new
        BPM only reaches the CIA at its next reload."""
        length = self.cia_period + 1
        self.row_read = None
        if self.playing:
            self._tick()
        self.time += length

    def _tick(self) -> None:
        self.tick_no += 1
        if self.tick_no >= self.speed:
            self.tick_no = 0
            if self.patt_del2 == 0:
                self.dmacon = 0
                self.row_read = self.now = (self.pos, self.row)
                pattern = self.mod.orders[self.pos]
                rows = self.mod.patterns[pattern] if pattern < len(self.mod.patterns) else None
                cells = rows[self.row] if rows else [(0, 0)] * len(self.voices)
                for v, cell in zip(self.voices, cells):
                    self._play_voice(v, cell)
                    self.hw.write_volume(v.index, v.volume)
                self._set_dma()
            else:
                for v in self.voices:
                    self._check_effects(v)
            self.row += 1
            if self.patt_del:
                self.patt_del2 = self.patt_del
                self.patt_del = 0
            if self.patt_del2:
                self.patt_del2 -= 1
                if self.patt_del2:
                    self.row -= 1
            if self.pbreak_flag:
                self.row = self.pbreak_pos
                self.pbreak_pos = 0
                self.pbreak_flag = False
            if self.row >= ROWS or self.pos_jump:
                self._next_position()
        else:
            for v in self.voices:
                self._check_effects(v)
            if self.pos_jump:
                self._next_position()

    def _next_position(self) -> None:
        self.row = self.pbreak_pos
        self.pbreak_pos = 0
        self.pos_jump = False
        self.pos = (self.pos + 1) & 127
        if self.pos >= len(self.mod.orders):
            self.pos = 0

    # ---- tick 0: read a row ------------------------------------------------------------
    def _play_voice(self, v: _Voice, cell) -> None:
        hw = self.hw
        if v.note == 0 and v.cmd == 0:
            hw.write_period(v.index, v.period)
        v.note, v.cmd = cell
        ins = (v.note >> 8 & 0xF0) | (v.cmd >> 12)
        if ins:
            v.sample = ins
            s = self.mod.samples[ins - 1] if ins <= len(self.mod.samples) else None
            if s is None or not s.length:         # empty slot: silence
                v.start = v.loopstart = v.wavestart = BLANK
                v.length = v.replen = 1
                v.finetune = s.finetune if s else 0
                v.volume = s.volume if s else 0
            else:
                v.start = s.address
                v.finetune = s.finetune
                v.volume = s.volume
                v.length = s.length >> 1
                v.replen = s.loop_length >> 1
                repeat = s.loop_start >> 1
                if repeat:
                    v.loopstart = v.wavestart = v.start + 2 * repeat
                    v.length = repeat + v.replen
                else:
                    v.loopstart = v.wavestart = v.start
        if v.note & 0xFFF:
            if v.cmd & 0xFF0 == 0xE50:
                v.finetune = v.cmd & 0xF
                self._set_period(v)
            else:
                effect = v.cmd >> 8 & 0xF
                if effect in (3, 5):
                    self._set_tone_porta(v)
                    self._check_more_effects(v)
                elif effect == 9:
                    self._check_more_effects(v)
                    self._set_period(v)
                else:
                    self._set_period(v)
        else:
            self._check_more_effects(v)

    def _set_period(self, v: _Voice) -> None:
        note = v.note & 0xFFF
        i = 0
        while note < PERIODS[i]:                  # PERIODS[36] is 0
            i += 1
        v.period = PERIODS[v.finetune * 37 + i]
        if v.cmd & 0xFF0 != 0xED0:                # not a note delay: trigger now
            hw = self.hw
            hw.dma(1 << v.index, False)
            if not v.wavecontrol & 0x04:
                v.vibratopos = 0
            if not v.wavecontrol & 0x40:
                v.tremolopos = 0
            hw.write_length(v.index, v.length)
            hw.write_location(v.index, v.start)
            hw.write_period(v.index, v.period)
            self.dmacon |= 1 << v.index
        self._check_more_effects(v)

    def _set_dma(self) -> None:
        hw = self.hw
        hw.dma(self.dmacon, True)
        for v in self.voices:                     # Paula takes these when the block ends
            hw.write_location(v.index, v.loopstart)
            hw.write_length(v.index, v.replen)

    def _check_more_effects(self, v: _Voice) -> None:
        effect = v.cmd >> 8 & 0xF
        if effect == 0x9:
            self._sample_offset(v)
        elif effect == 0xB:
            self.pos = ((v.cmd & 0xFF) - 1) & 0xFF    # B00 wraps round to position 0
            self.pbreak_pos = 0
            self.pos_jump = True
        elif effect == 0xD:
            p = v.cmd & 0xFF
            self.pbreak_pos = (p >> 4) * 10 + (p & 0xF)
            if self.pbreak_pos > 63:
                self.pbreak_pos = 0
            self.pos_jump = True
        elif effect == 0xE:
            self._e_commands(v)
        elif effect == 0xF:
            self._set_speed(v)
        elif effect == 0xC:
            v.volume = min(v.cmd & 0xFF, 64)
        else:
            self.hw.write_period(v.index, v.period)

    # ---- ticks 1..speed-1: effects ----------------------------------------------------------
    def _check_effects(self, v: _Voice) -> None:
        self._update_funk(v)
        effect = v.cmd >> 8 & 0xF
        if v.cmd & 0xFFF:
            if effect == 0x0:
                self._arpeggio(v)
            elif effect == 0x1:
                self._porta_up(v)
            elif effect == 0x2:
                self._porta_down(v)
            elif effect == 0x3:
                self._tone_portamento(v)
            elif effect == 0x4:
                self._vibrato(v)
            elif effect == 0x5:
                self._tone_port_no_change(v)
                self._volume_slide(v)
            elif effect == 0x6:
                self._vibrato2(v)
                self._volume_slide(v)
            elif effect == 0xE:
                self._e_commands(v)
            else:
                self.hw.write_period(v.index, v.period)
                if effect == 0x7:
                    self._tremolo(v)
                    return                        # tremolo writes its own volume
                if effect == 0xA:
                    self._volume_slide(v)
        self.hw.write_volume(v.index, v.volume)

    def _e_commands(self, v: _Voice) -> None:
        e, x = v.cmd >> 4 & 0xF, v.cmd & 0xF
        first = self.tick_no == 0
        if e == 0x0:
            if first:
                self.led = not x & 1
                self.hw.set_led(self.led)
        elif e == 0x1:
            if first:
                self.low_mask = 0x0F
                self._porta_up(v)
        elif e == 0x2:
            if first:
                self.low_mask = 0x0F
                self._porta_down(v)
        elif e == 0x3:
            v.glissfunk = (v.glissfunk & 0xF0) | x
        elif e == 0x4:
            v.wavecontrol = (v.wavecontrol & 0xF0) | x
        elif e == 0x5:
            v.finetune = x
        elif e == 0x6:
            if not first:
                return
            if x == 0:
                v.pattpos = self.row
                return
            if v.loopcount == 0:
                v.loopcount = x
            else:
                v.loopcount -= 1
                if v.loopcount == 0:
                    return
            self.pbreak_pos = v.pattpos
            self.pbreak_flag = True
        elif e == 0x7:
            v.wavecontrol = (x << 4) | (v.wavecontrol & 0x0F)
        elif e == 0x9:
            if x and not (first and v.note & 0xFFF) and self.tick_no % x == 0:
                self._retrigger(v)
        elif e == 0xA:
            if first:
                v.volume = min(v.volume + x, 64)
        elif e == 0xB:
            if first:
                v.volume = max(v.volume - x, 0)
        elif e == 0xC:
            if self.tick_no == x:
                v.volume = 0
        elif e == 0xD:
            if self.tick_no == x and v.note & 0xFFF:
                self._retrigger(v)
        elif e == 0xE:
            if first and self.patt_del2 == 0:
                self.patt_del = x + 1
        elif e == 0xF:
            if first:
                v.glissfunk = (x << 4) | (v.glissfunk & 0x0F)
                if x:
                    self._update_funk(v)

    def _retrigger(self, v: _Voice) -> None:
        hw = self.hw
        hw.dma(1 << v.index, False)
        hw.write_location(v.index, v.start)
        hw.write_length(v.index, v.length)
        hw.write_period(v.index, v.period)
        hw.dma(1 << v.index, True)
        hw.write_location(v.index, v.loopstart)
        hw.write_length(v.index, v.replen)

    def _set_speed(self, v: _Voice) -> None:
        p = v.cmd & 0xFF
        if p == 0:
            self.playing = False                  # F00: the song stops
        elif p < 32:
            self.speed = p
            self.tick_no = 0
        else:
            self.cia_period = cia_period(p)

    def _sample_offset(self, v: _Voice) -> None:
        if v.cmd & 0xFF:
            v.sampleoffset = v.cmd & 0xFF
        offset = v.sampleoffset << 7
        if offset < v.length:
            v.length -= offset
            v.start += offset << 1
        else:
            v.length = 1

    def _arpeggio(self, v: _Voice) -> None:
        step = self.tick_no % 3
        if step == 0:
            self.hw.write_period(v.index, v.period)
            return
        add = v.cmd >> 4 & 0xF if step == 1 else v.cmd & 0xF
        base = v.finetune * 37
        for i in range(37):
            if v.period >= PERIODS[base + i]:
                self.hw.write_period(v.index, PERIODS[base + i + add])
                return

    def _porta_up(self, v: _Voice) -> None:
        v.period = _s16(v.period - (v.cmd & 0xFF & self.low_mask))
        self.low_mask = 0xFF
        if v.period & 0xFFF < 113:                # (PT clamps after dropping the sign)
            v.period = _s16((v.period & 0xF000) | 113)
        self.hw.write_period(v.index, v.period & 0xFFF)

    def _porta_down(self, v: _Voice) -> None:
        v.period = _s16(v.period + (v.cmd & 0xFF & self.low_mask))
        self.low_mask = 0xFF
        if v.period & 0xFFF > 856:
            v.period = _s16((v.period & 0xF000) | 856)
        self.hw.write_period(v.index, v.period & 0xFFF)

    def _nearest_period(self, v: _Voice, period: int) -> int:
        """First table period at or below `period` (PT's search for 3xx and glissando)."""
        base = v.finetune * 37
        for i in range(37):
            if period >= PERIODS[base + i]:
                return i
        return 35

    def _set_tone_porta(self, v: _Voice) -> None:
        i = self._nearest_period(v, v.note & 0xFFF)
        if v.finetune & 8 and i > 0:
            i -= 1
        v.wantedperiod = PERIODS[v.finetune * 37 + i]
        v.toneportdirec = 0
        if v.period == v.wantedperiod:
            v.wantedperiod = 0
        elif v.period > v.wantedperiod:
            v.toneportdirec = 1

    def _tone_portamento(self, v: _Voice) -> None:
        if v.cmd & 0xFF:
            v.toneportspeed = v.cmd & 0xFF
            v.cmd &= 0xFF00
        self._tone_port_no_change(v)

    def _tone_port_no_change(self, v: _Voice) -> None:
        if v.wantedperiod <= 0:
            return
        if v.toneportdirec:
            v.period = _s16(v.period - v.toneportspeed)
            if v.period <= v.wantedperiod:
                v.period = v.wantedperiod
                v.wantedperiod = 0
        else:
            v.period = _s16(v.period + v.toneportspeed)
            if v.period >= v.wantedperiod:
                v.period = v.wantedperiod
                v.wantedperiod = 0
        if v.glissfunk & 0xF:                     # glissando: round to semitones
            self.hw.write_period(v.index,
                                 PERIODS[v.finetune * 37 + self._nearest_period(v, v.period)])
        else:
            self.hw.write_period(v.index, v.period & 0xFFFF)

    def _vibrato(self, v: _Voice) -> None:
        if v.cmd & 0x0F:
            v.vibratocmd = (v.vibratocmd & 0xF0) | (v.cmd & 0x0F)
        if v.cmd & 0xF0:
            v.vibratocmd = (v.cmd & 0xF0) | (v.vibratocmd & 0x0F)
        self._vibrato2(v)

    @staticmethod
    def _wave(pos: int, kind: int, ramp_pos: int) -> int:
        i = pos >> 2 & 0x1F
        if kind == 0:
            return VIBRATO[i]
        if kind == 1:
            return i << 3 if ramp_pos < 128 else 255 - (i << 3)
        return 255

    def _vibrato2(self, v: _Voice) -> None:
        depth = (self._wave(v.vibratopos, v.wavecontrol & 3, v.vibratopos)
                 * (v.vibratocmd & 0xF)) >> 7
        period = v.period + depth if v.vibratopos < 128 else v.period - depth
        self.hw.write_period(v.index, period & 0xFFFF)
        v.vibratopos = (v.vibratopos + ((v.vibratocmd >> 2) & 0x3C)) & 0xFF

    def _tremolo(self, v: _Voice) -> None:
        if v.cmd & 0x0F:
            v.tremolocmd = (v.tremolocmd & 0xF0) | (v.cmd & 0x0F)
        if v.cmd & 0xF0:
            v.tremolocmd = (v.cmd & 0xF0) | (v.tremolocmd & 0x0F)
        # PT bug: the ramp waveform's direction comes from the *vibrato* position
        depth = (self._wave(v.tremolopos, v.wavecontrol >> 4 & 3, v.vibratopos)
                 * (v.tremolocmd & 0xF)) >> 6
        if v.tremolopos < 128:
            volume = min(v.volume + depth, 64)
        else:
            volume = max(v.volume - depth, 0)
        self.hw.write_volume(v.index, volume)
        v.tremolopos = (v.tremolopos + ((v.tremolocmd >> 2) & 0x3C)) & 0xFF

    def _volume_slide(self, v: _Voice) -> None:
        p = v.cmd & 0xFF
        if p & 0xF0 == 0:
            v.volume = max(v.volume - (p & 0xF), 0)
        else:
            v.volume = min(v.volume + (p >> 4), 64)

    def _update_funk(self, v: _Voice) -> None:
        speed = v.glissfunk >> 4
        if not speed:
            return
        v.funkoffset += FUNK[speed]
        if v.funkoffset >= 128:
            v.funkoffset = 0
            v.wavestart += 1
            if v.wavestart >= v.loopstart + 2 * v.replen:
                v.wavestart = v.loopstart
            a = v.wavestart % len(self.ram)
            self.ram[a] = -1 - int(self.ram[a])


def seek(mod: ModFile, hw, position: int) -> ProTracker:
    """A replayer about to play `position`, with the speed, tempo and LED
    state the song has when it first gets there.  Falls back to a fresh
    start there if the song loops or ends without reaching it."""
    position %= len(mod.orders)
    scout = ProTracker(mod, NullPaula(mod))
    seen = set()
    while scout.playing:
        if scout.tick_no + 1 >= scout.speed and not scout.patt_del2:   # a row is next
            if scout.pos == position:
                pt = ProTracker(mod, hw, position)
                pt.row = scout.row
                pt.now = (position, scout.row)
                pt.speed, pt.tick_no = scout.speed, scout.speed - 1
                pt.cia_period = scout.cia_period
                pt.led = scout.led
                hw.set_led(pt.led)
                return pt
            key = (scout.pos, scout.row, tuple(v.loopcount for v in scout.voices))
            if key in seen:
                break
            seen.add(key)
        scout.tick()
    return ProTracker(mod, hw, position)


def song_length(mod: ModFile, position: int = 0, max_seconds: float = 3600.0,
                tail: float = 2.5) -> tuple[float, str] | None:
    """(seconds, "loop") until the song returns to a row it has already
    played (intro plus one pass of the loop), or (seconds, "end") when it
    stops (F00), plus `tail` for the last notes to ring.  None if neither
    happens within `max_seconds`.  A row counts as revisited only with the
    same pattern-loop counters, so E6x loops don't end the song early.
    Timed from order `position`."""
    hw = NullPaula(mod)
    pt = seek(mod, hw, position) if position else ProTracker(mod, hw)
    seen = set()
    end = max_seconds * PAL_CIA_CLOCK
    while pt.time < end:
        t = pt.time
        pt.tick()
        if not pt.playing:
            return (pt.time / PAL_CIA_CLOCK + tail, "end")
        if pt.row_read is not None:
            key = (pt.row_read, tuple(v.loopcount for v in pt.voices))
            if key in seen:
                return (t / PAL_CIA_CLOCK, "loop")
            seen.add(key)
    return None
