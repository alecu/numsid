"""Deliberately naive cycle-by-cycle SID voice model used as a test oracle.

It follows the clocking order and state machines described by reSID 0.16
literally (envelope, then oscillators, then sync, once per cycle) with no
vectorisation or event skipping, so the optimised engine in numsid.sid can be
checked against it sample for sample.  Slow: only for short test runs.
"""

from numsid.sid import RATE_PERIOD, SUSTAIN_LEVEL, ATTACK, DECAY_SUSTAIN, RELEASE

MASK24 = 0xFFFFFF


class RefVoice:
    def __init__(self):
        self.freq = self.pw = self.control = self.ad = self.sr = 0
        self.acc = 0
        self.prev_acc = 0
        self.msb_rising = False
        self.shift = 0x7FFFF8
        self.env = 0
        self.state = RELEASE
        self.rate_counter = 0
        self.rate_period = RATE_PERIOD[0]
        self.exp_counter = 0
        self.exp_period = 1
        self.hold_zero = True

    def write(self, reg, value):
        if reg == 0:
            self.freq = (self.freq & 0xFF00) | value
        elif reg == 1:
            self.freq = (self.freq & 0xFF) | (value << 8)
        elif reg == 2:
            self.pw = (self.pw & 0xF00) | value
        elif reg == 3:
            self.pw = (self.pw & 0xFF) | ((value & 0xF) << 8)
        elif reg == 4:
            test_next = value & 8
            if test_next:
                self.acc = 0
                self.shift = 0
            elif self.control & 8:
                self.shift = 0x7FFFF8
            gate_next = value & 1
            if not (self.control & 1) and gate_next:
                self.state = ATTACK
                self.rate_period = RATE_PERIOD[self.ad >> 4]
                self.hold_zero = False
            elif self.control & 1 and not gate_next:
                self.state = RELEASE
                self.rate_period = RATE_PERIOD[self.sr & 15]
            self.control = value
        elif reg == 5:
            self.ad = value
            if self.state == ATTACK:
                self.rate_period = RATE_PERIOD[value >> 4]
            elif self.state == DECAY_SUSTAIN:
                self.rate_period = RATE_PERIOD[value & 15]
        elif reg == 6:
            self.sr = value
            if self.state == RELEASE:
                self.rate_period = RATE_PERIOD[value & 15]

    def clock_env(self):
        self.rate_counter += 1
        if self.rate_counter & 0x8000:
            self.rate_counter = (self.rate_counter + 1) & 0x7FFF
        if self.rate_counter != self.rate_period:
            return
        self.rate_counter = 0
        if self.state == ATTACK or self._bump_exp():
            self.exp_counter = 0
            if self.hold_zero:
                return
            if self.state == ATTACK:
                self.env = (self.env + 1) & 0xFF
                if self.env == 0xFF:
                    self.state = DECAY_SUSTAIN
                    self.rate_period = RATE_PERIOD[self.ad & 15]
            elif self.state == DECAY_SUSTAIN:
                if self.env != SUSTAIN_LEVEL[self.sr >> 4]:
                    self.env = (self.env - 1) & 0xFF
            else:
                self.env = (self.env - 1) & 0xFF
            e = self.env
            if e == 0xFF:
                self.exp_period = 1
            elif e == 0x5D:
                self.exp_period = 2
            elif e == 0x36:
                self.exp_period = 4
            elif e == 0x1A:
                self.exp_period = 8
            elif e == 0x0E:
                self.exp_period = 16
            elif e == 0x06:
                self.exp_period = 30
            elif e == 0:
                self.exp_period = 1
                self.hold_zero = True

    def _bump_exp(self):
        self.exp_counter += 1
        return self.exp_counter == self.exp_period

    def clock_osc(self):
        if self.control & 8:
            self.msb_rising = False
            return
        prev = self.acc
        self.acc = (self.acc + self.freq) & MASK24
        self.msb_rising = not (prev & 0x800000) and bool(self.acc & 0x800000)
        if not (prev & 0x80000) and (self.acc & 0x80000):
            bit0 = ((self.shift >> 22) ^ (self.shift >> 17)) & 1
            self.shift = ((self.shift << 1) & 0x7FFFFF) | bit0

    def noise(self):
        s = self.shift
        return (((s >> 22) & 1) << 11 | ((s >> 20) & 1) << 10 | ((s >> 16) & 1) << 9
                | ((s >> 13) & 1) << 8 | ((s >> 11) & 1) << 7 | ((s >> 7) & 1) << 6
                | ((s >> 4) & 1) << 5 | ((s >> 2) & 1) << 4)

    def wave(self, src_acc):
        """Basic (non-combined) waveforms, AND-ed when several are selected."""
        wf = self.control >> 4
        a = self.acc
        parts = []
        if wf & 1:
            msb = (a ^ src_acc) if self.control & 4 else a
            parts.append(((a ^ MASK24) if msb & 0x800000 else a) >> 11 & 0xFFF)
        if wf & 2:
            parts.append(a >> 12)
        if wf & 4:
            parts.append(0xFFF if (self.control & 8) or (a >> 12) >= self.pw else 0)
        if wf & 8:
            parts.append(self.noise())
        if not parts:
            return 0
        out = parts[0]
        for p in parts[1:]:
            out &= p
        return out


def run_reference(writes, n):
    """writes: list of (cycle, reg, value) with reg 0..20.  Returns per voice
    lists of (acc, env, wave) for every cycle."""
    voices = [RefVoice(), RefVoice(), RefVoice()]
    writes = sorted(writes, key=lambda w: w[0])
    wi = 0
    trace = [([], [], []) for _ in range(3)]
    for c in range(n):
        while wi < len(writes) and writes[wi][0] <= c:
            _, reg, value = writes[wi]
            voices[reg // 7].write(reg % 7, value)
            wi += 1
        for v in voices:
            v.clock_env()
        for v in voices:
            v.clock_osc()
        for i, v in enumerate(voices):
            src = voices[(i - 1) % 3]
            dest = voices[(i + 1) % 3]
            if v.msb_rising and dest.control & 2 and not (v.control & 2 and src.msb_rising):
                dest.acc = 0
        for i, v in enumerate(voices):
            trace[i][0].append(v.acc)
            trace[i][1].append(v.env)
            trace[i][2].append(v.wave(voices[(i - 1) % 3].acc))
    return trace
