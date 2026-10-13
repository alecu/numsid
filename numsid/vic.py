"""MOS 6569/6567 VIC-II, reduced to what music players use: the raster
counter and the raster-compare interrupt.

The beam position follows directly from the cycle count (63 cycles x 312
lines on PAL, 65 x 263 on NTSC); bad lines and sprite DMA, which steal CPU
cycles on a real machine, are not modelled.
"""

from __future__ import annotations


class VIC:
    def __init__(self, pal: bool = True):
        self.cycles_per_line = 63 if pal else 65
        self.lines = 312 if pal else 263
        self.frame = self.cycles_per_line * self.lines
        self.regs = bytearray(64)
        self.regs[0x11] = 0x1B
        self.compare = 0          # raster line that raises the interrupt
        self.enable = 0           # $D01A
        self.flags = 0            # $D019
        self.last = 0             # cycle up to which events are accounted for

    def raster(self, now: int) -> int:
        return (now // self.cycles_per_line) % self.lines

    def _hit_time(self, after: int) -> int | None:
        """First cycle > `after` at which the beam starts the compare line."""
        if self.compare >= self.lines:
            return None
        offset = self.compare * self.cycles_per_line
        k = (after - offset) // self.frame + 1
        return offset + k * self.frame

    def sync(self, now: int) -> None:
        if now > self.last:
            hit = self._hit_time(self.last)
            if hit is not None and hit <= now:
                self.flags |= 1
            self.last = now

    def next_event(self) -> int | None:
        if not self.enable & 1:
            return None
        return self._hit_time(self.last)

    @property
    def irq(self) -> bool:
        return bool(self.flags & self.enable & 0x0F)

    def read(self, reg: int, now: int) -> int:
        reg &= 0x3F
        self.sync(now)
        if reg == 0x11:
            return (self.regs[0x11] & 0x7F) | (0x80 if self.raster(now) > 0xFF else 0)
        if reg == 0x12:
            return self.raster(now) & 0xFF
        if reg == 0x19:
            return self.flags | 0x70 | (0x80 if self.irq else 0)
        if reg == 0x1A:
            return self.enable | 0xF0
        return self.regs[reg]

    def write(self, reg: int, value: int, now: int) -> None:
        reg &= 0x3F
        self.sync(now)
        self.regs[reg] = value
        if reg == 0x11:
            self.compare = (self.compare & 0xFF) | ((value & 0x80) << 1)
        elif reg == 0x12:
            self.compare = (self.compare & 0x100) | value
        elif reg == 0x19:
            self.flags &= ~value & 0x0F
        elif reg == 0x1A:
            self.enable = value & 0x0F
