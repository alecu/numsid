"""MOS 6569/6567 VIC-II, reduced to what music players need: the raster
counter, the raster-compare interrupt, and the "bad lines" on which the VIC
halts the CPU to fetch screen data.

The beam position follows directly from the cycle count (63 cycles x 312
lines on PAL, 65 x 263 on NTSC).  Sprite DMA, which also steals cycles, is
not modelled.
"""

from __future__ import annotations

# A bad line halts the CPU from about cycle 12 of the line for 40 cycles of
# character fetches plus the 3 cycles the CPU is warned in advance.
BADLINE_START = 12
BADLINE_CYCLES = 43


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

    def next_badline(self, after: int) -> int | None:
        """Cycle at which the first bad line at or after `after` halts the
        CPU, or None while the display is switched off ($D011 bit 4).

        Bad lines are the visible lines $30-$F7 whose low three bits equal
        the vertical scroll ($D011 bits 0-2)."""
        ctrl = self.regs[0x11]
        if not ctrl & 0x10:
            return None
        cpl = self.cycles_per_line
        first = 0x30 + ((ctrl & 7) - 0x30) % 8
        frame_start = after - after % self.frame
        pos = after - frame_start - BADLINE_START
        line = max(first, -(-pos // cpl))
        line = first + -(-(line - first) // 8) * 8
        if line > 0xF7:
            return frame_start + self.frame + first * cpl + BADLINE_START
        return frame_start + line * cpl + BADLINE_START

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
        if reg in (0x1E, 0x1F):
            value = self.regs[reg]
            self.regs[reg] = 0
            return value
        if reg == 0x16:
            return self.regs[reg] | 0xC0
        if reg == 0x18:
            return self.regs[reg] | 1
        if 0x20 <= reg <= 0x2E:
            return self.regs[reg] | 0xF0
        if reg >= 0x2F:
            return 0xFF
        return self.regs[reg]

    def write(self, reg: int, value: int, now: int) -> None:
        reg &= 0x3F
        self.sync(now)
        if reg in (0x1E, 0x1F) or reg >= 0x2F:
            return
        self.regs[reg] = value
        if reg == 0x11:
            self.compare = (self.compare & 0xFF) | ((value & 0x80) << 1)
        elif reg == 0x12:
            self.compare = (self.compare & 0x100) | value
        elif reg == 0x19:
            self.flags &= ~value & 0x0F
        elif reg == 0x1A:
            self.enable = value & 0x0F
