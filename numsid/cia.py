"""MOS 6526 CIA: the two interval timers and the interrupt control register.

Timers are evaluated lazily: instead of counting every cycle we keep the
cycle of the next underflow and catch up (`sync`) when a register is
touched or an event is due.  Modelled:

* 16-bit latch; in continuous mode a timer underflows every latch+1 cycles
  and reloads from the latch, so a latch written while the timer runs takes
  effect at the next reload,
* writing the high byte of a stopped timer also loads the counter,
* control register: start/stop (the count pauses while stopped), one-shot,
  force load, and timer B counting timer A underflows (cascade),
* interrupt control: underflow flags, mask set/clear, reading clears the
  flags, and `irq` tells whether the chip is pulling its interrupt line
  (IRQ for CIA 1, NMI for CIA 2).

Not modelled: time-of-day clock, serial port, the CNT pin, and the one or
two cycle pipeline delays of the real chip.
"""

from __future__ import annotations


class Timer:
    __slots__ = ("latch", "running", "one_shot", "next_underflow", "remaining")

    def __init__(self):
        self.latch = 0xFFFF
        self.running = False
        self.one_shot = False
        self.next_underflow = 0          # absolute cycle (valid while running)
        self.remaining = 0x10000         # cycles to underflow (valid while stopped)

    def value(self, now: int) -> int:
        if self.running:
            return max(0, min(0xFFFF, self.next_underflow - now - 1))
        return (self.remaining - 1) & 0xFFFF

    def advance(self, now: int) -> int:
        """Process underflows up to and including cycle `now`; returns how many."""
        if not self.running or self.next_underflow > now:
            return 0
        if self.one_shot:
            self.running = False
            self.remaining = self.latch + 1
            return 1
        period = self.latch + 1
        n = (now - self.next_underflow) // period + 1
        self.next_underflow += n * period
        return n

    def start(self, now: int) -> None:
        if not self.running:
            self.running = True
            self.next_underflow = now + self.remaining

    def stop(self, now: int) -> None:
        if self.running:
            self.running = False
            self.remaining = max(1, self.next_underflow - now)

    def load(self, now: int) -> None:
        """Copy the latch into the counter (force load)."""
        if self.running:
            self.next_underflow = now + self.latch + 1
        else:
            self.remaining = self.latch + 1


class CIA:
    def __init__(self):
        self.ta = Timer()
        self.tb = Timer()
        self.cra = 0
        self.crb = 0
        self.mask = 0                    # interrupt enable bits
        self.flags = 0                   # interrupt flags (bit 0 TA, bit 1 TB)
        self.tb_cascade = False          # timer B counts timer A underflows
        self.tb_count = 0x10000          # cascade mode: TA underflows left
        self.regs = bytearray(16)        # last written values (ports etc.)

    # ---- time -----------------------------------------------------------
    def sync(self, now: int) -> None:
        n = self.ta.advance(now)
        if n:
            self.flags |= 1
            if self.tb_cascade and self.crb & 1:
                self.tb_count -= n
                if self.tb_count <= 0:
                    self.flags |= 2
                    if self.crb & 8:              # one-shot
                        self.crb &= ~1
                        self.tb_count = self.tb.latch + 1
                    else:
                        period = self.tb.latch + 1
                        self.tb_count = period - (-self.tb_count) % period
        if not self.tb_cascade and self.tb.advance(now):
            self.flags |= 2

    def next_event(self) -> int | None:
        """Cycle of the next underflow that can raise an interrupt."""
        t = None
        if self.mask & 1 and self.ta.running:
            t = self.ta.next_underflow
        if self.mask & 2:
            if self.tb_cascade:
                if self.crb & 1 and self.ta.running and not self.ta.one_shot:
                    tb = self.ta.next_underflow + (self.tb_count - 1) * (self.ta.latch + 1)
                    t = tb if t is None else min(t, tb)
            elif self.tb.running:
                t = self.tb.next_underflow if t is None else min(t, self.tb.next_underflow)
        return t

    @property
    def irq(self) -> bool:
        return bool(self.flags & self.mask)

    # ---- registers -------------------------------------------------------
    def read(self, reg: int, now: int) -> int:
        reg &= 0x0F
        self.sync(now)
        if reg == 0x04:
            return self.ta.value(now) & 0xFF
        if reg == 0x05:
            return self.ta.value(now) >> 8
        if reg == 0x06:
            return self._tb_value(now) & 0xFF
        if reg == 0x07:
            return self._tb_value(now) >> 8
        if reg == 0x0D:
            value = self.flags | (0x80 if self.flags & self.mask else 0)
            self.flags = 0
            return value
        if reg == 0x0E:
            return self.cra & 0xEF
        if reg == 0x0F:
            return self.crb & 0xEF
        if reg in (0x00, 0x01):
            return 0xFF                  # no keys pressed, no joystick
        return self.regs[reg]

    def _tb_value(self, now: int) -> int:
        if self.tb_cascade:
            return (self.tb_count - 1) & 0xFFFF
        return self.tb.value(now)

    def write(self, reg: int, value: int, now: int) -> None:
        reg &= 0x0F
        self.sync(now)
        self.regs[reg] = value
        if reg == 0x04:
            self.ta.latch = (self.ta.latch & 0xFF00) | value
        elif reg == 0x05:
            self.ta.latch = (self.ta.latch & 0x00FF) | (value << 8)
            if not self.ta.running:
                self.ta.remaining = self.ta.latch + 1
        elif reg == 0x06:
            self.tb.latch = (self.tb.latch & 0xFF00) | value
        elif reg == 0x07:
            self.tb.latch = (self.tb.latch & 0x00FF) | (value << 8)
            if not self.tb.running:
                self.tb.remaining = self.tb.latch + 1
                if self.tb_cascade and not self.crb & 1:
                    self.tb_count = self.tb.latch + 1
        elif reg == 0x0D:
            if value & 0x80:
                self.mask |= value & 0x1F
            else:
                self.mask &= ~value & 0x1F
        elif reg == 0x0E:
            self.cra = value
            self._control(self.ta, value, now)
        elif reg == 0x0F:
            self.crb = value
            cascade = (value & 0x60) == 0x40
            if cascade:
                self.tb.stop(now)
                if value & 0x10 or not self.tb_cascade:
                    self.tb_count = self.tb.latch + 1
            elif self.tb_cascade:
                self.tb.remaining = max(1, self.tb_count)
            self.tb_cascade = cascade
            if not cascade:
                self._control(self.tb, value, now)

    @staticmethod
    def _control(timer: Timer, value: int, now: int) -> None:
        timer.one_shot = bool(value & 0x08)
        if value & 1:
            timer.start(now)
        else:
            timer.stop(now)
        if value & 0x10:
            timer.load(now)

    # ---- power-on state ------------------------------------------------------
    def kernal_defaults(self, latch: int, now: int = 0) -> None:
        """CIA 1 as left by the KERNAL: timer A free-running at `latch` with
        its interrupt enabled (the 60 Hz system IRQ)."""
        self.ta.latch = latch
        self.ta.remaining = latch + 1
        self.mask = 0x01
        self.write(0x0E, 0x11, now)
