"""A ROM-booted PAL C64, independent of the SID file playback protocol.

The CPU uses a fast banked bytearray. RAM hidden by ROM is kept separately;
VIC snapshots always expose that physical RAM, never the CPU's ROM overlay.
Video packets contain bytes and registers, with no CPU-generated pixels.
"""

from dataclasses import dataclass

import numpy as np

from .cpu6502 import CPU6502
from .collisions import latch_collisions
from .peripherals import Input, PortCIA
from .program import Program
from .roms import ROMs
from .sprites import Sprites
from .vic import VIC, BADLINE_CYCLES

PAL_CLOCK = 985248
WIDTH, HEIGHT = 384, 272
FIRST_LINE = 16


class SilentSID:
    """Register-only SID for fast, deterministic headless execution."""
    def __init__(self):
        self.regs = bytearray(32)

    def write(self, reg, value, cycle):
        self.regs[reg] = value

    def read(self, reg, cycle):
        return 255 if reg in (25, 26) else 0


@dataclass
class VideoFrame:
    # Every raster line has the VIC's 16 KiB bank, colour RAM and registers.
    memory: np.ndarray
    colors: np.ndarray
    registers: np.ndarray
    banks: np.ndarray
    number: int = 0
    sprite_rows: np.ndarray | None = None

    @classmethod
    def empty(cls):
        return cls(np.zeros((312, 16384), dtype=np.uint8),
                   np.zeros((312, 1024), dtype=np.uint8),
                   np.zeros((312, 64), dtype=np.uint8),
                   np.zeros(312, dtype=np.uint8))


class Computer:
    def __init__(self, roms: ROMs, sid=None):
        self.roms = roms
        self.memory = bytearray(65536)  # CPU view, with ROM overlays
        self._view = np.frombuffer(self.memory, dtype=np.uint8)
        self._hidden = {0xA000: bytearray(8192), 0xD000: bytearray(4096),
                        0xE000: bytearray(8192)}
        self._mapped = {addr: False for addr in self._hidden}
        self._hidden_views = {addr: np.frombuffer(hidden, dtype=np.uint8)
                              for addr, hidden in self._hidden.items()}
        self.color_ram = bytearray(1024)
        self._color_view = np.frombuffer(self.color_ram, dtype=np.uint8)
        self.inputs = Input()
        self.cia1 = PortCIA(self.inputs)
        self.cia2 = PortCIA()
        self.vic = VIC()
        self.vic.regs[:] = bytes(64)
        self.sprites = Sprites()
        self.sid = sid if sid is not None else SilentSID()
        self.port_latch = 0x37
        self.memory[0] = 0x2F
        self.cpu = CPU6502(self.memory, self.read, self.write, brk_halts=False)
        self._bank()
        self.cpu.sp = 0xFD
        self.cpu.pc = self.memory[0xFFFC] | self.memory[0xFFFD] << 8
        self.cpu.clock = 7
        self.frame_number = 0
        self._bad_after = 0
        self._nmi = False
        self._restore = False
        self.audio = np.empty(0, dtype=np.float32)

    def _bank(self):
        port = self.port_latch | (~self.memory[0] & 255)
        self.memory[1] = port
        self.io_visible = bool(port & 3) and bool(port & 4)
        roms = {0xA000: (port & 3 == 3, self.roms.basic),
                0xD000: (bool(port & 3) and not port & 4, self.roms.chargen),
                0xE000: (bool(port & 2), self.roms.kernal)}
        for start, (visible, rom) in roms.items():
            if visible == self._mapped[start]:
                continue
            end = start + len(rom)
            if visible:
                self._hidden[start][:] = self.memory[start:end]
                self.memory[start:end] = rom
            else:
                self.memory[start:end] = self._hidden[start]
            self._mapped[start] = visible

    def peek_ram(self, addr: int) -> int:
        if 0xA000 <= addr < 0xC000 and self._mapped[0xA000]:
            return self._hidden[0xA000][addr - 0xA000]
        if 0xD000 <= addr < 0xE000 and self._mapped[0xD000]:
            return self._hidden[0xD000][addr - 0xD000]
        if addr >= 0xE000 and self._mapped[0xE000]:
            return self._hidden[0xE000][addr - 0xE000]
        return self.memory[addr]

    def poke_ram(self, addr: int, value: int):
        for start, hidden in self._hidden.items():
            if self._mapped[start] and start <= addr < start + len(hidden):
                hidden[addr - start] = value
                return
        self.memory[addr] = value

    def vic_read(self, addr: int) -> int:
        addr &= 16383
        bank = self.cia2.vic_bank
        if not bank & 0x4000 and addr & 0x3000 == 0x1000:
            return self.roms.chargen[addr & 4095]
        return self.peek_ram(bank + addr)

    def read(self, addr: int) -> int:
        if not 0xD000 <= addr < 0xE000 or not self.io_visible:
            return self.memory[addr]
        now = self.cpu.clock
        if addr < 0xD400:
            return self.vic.read(addr, now)
        if addr < 0xD800:
            return self.sid.read(addr & 31, now)
        if addr < 0xDC00:
            return self.color_ram[addr & 1023] | 0xF0
        if addr < 0xDD00:
            return self.cia1.read(addr, now)
        if addr < 0xDE00:
            value = self.cia2.read(addr, now)
            if not self.cia2.irq and not self._restore:
                self._nmi = False
            return value
        return 255  # unconnected expansion port

    def write(self, addr: int, value: int):
        if addr < 2:
            if addr == 0:
                self.memory[0] = value
            else:
                self.port_latch = value
            self._bank()
        elif 0xD000 <= addr < 0xE000 and self.io_visible:
            now = self.cpu.clock
            if addr < 0xD400:
                self.vic.write(addr, value, now)
                self.cpu.until = now
            elif addr < 0xD800:
                self.sid.write(addr & 31, value, now)
            elif addr < 0xDC00:
                self.color_ram[addr & 1023] = value & 15
            elif addr < 0xDD00:
                self.cia1.write(addr, value, now)
                self.cpu.until = now
            elif addr < 0xDE00:
                self.cia2.write(addr, value, now)
                if not self.cia2.irq and not self._restore:
                    self._nmi = False
                self.cpu.until = now
        else:
            self.poke_ram(addr, value)

    def restore(self, pressed: bool):
        self._restore = pressed

    def _run_until(self, end: int):
        cpu, vic, c1, c2 = self.cpu, self.vic, self.cia1, self.cia2
        while cpu.clock < end:
            now = cpu.clock
            bad = vic.next_badline(self._bad_after)
            if bad is not None and bad <= now:
                self._bad_after = bad + 1
                cpu.clock = max(now, bad + BADLINE_CYCLES)
                continue
            c1.sync(now)
            c2.sync(now)
            vic.sync(now)
            nmi = c2.irq or self._restore
            if nmi and not self._nmi:
                self._nmi = True
                cpu.interrupt(0xFFFA)
                continue
            self._nmi = nmi
            cpu.irq_pending = c1.irq or vic.irq
            if cpu.irq_pending and not cpu.i:
                cpu.interrupt(0xFFFE)
                continue
            event = end
            for candidate in (c1.next_event(), c2.next_event(), vic.next_event(), bad):
                if candidate is not None and candidate < event:
                    event = candidate
            cpu.until = max(now + 1, event)
            cpu.run()

    def _capture(self, frame: VideoFrame, line: int):
        bank = self.cia2.vic_bank
        dest = frame.memory[line]
        dest[:] = self._view[bank:bank + 16384]
        # The VIC is wired to RAM, even when the CPU sees BASIC or KERNAL.
        for start, hidden in self._hidden_views.items():
            if self._mapped[start] and bank <= start < bank + 16384:
                offset = start - bank
                dest[offset:offset + len(hidden)] = hidden
        frame.colors[line] = self._color_view
        frame.registers[line] = self.vic.regs
        frame.banks[line] = bank >> 14
        if frame.sprite_rows is not None:
            frame.sprite_rows[line] = self.sprites.rows

    def _sprite_dma(self, start: int, cycle: int, sprites, rows):
        """PAL sprite fetch slots: 3..7 at cycles 1..10, 0..2 at 58..63.

        The latter fetch the next line. Keep these stalls at their bus
        positions; charging them all before the raster IRQ delays chained
        handlers enough to miss their next compare line.
        """
        for offset, n in enumerate(sprites):
            if rows[n] < 21:
                self._run_until(start + cycle + offset * 2)
                self.cpu.clock += 2

    def step_frame(self, *, capture: bool = True) -> VideoFrame | None:
        frame = VideoFrame.empty() if capture else None
        if frame is not None:
            frame.sprite_rows = np.full((312, 8), 255, dtype=np.uint8)
        start = self.frame_number * self.vic.frame
        for line in range(312):
            self._run_until(start + line * 63)
            self.sprites.begin_line()
            self._sprite_dma(start + line * 63, 0, range(3, 8), self.sprites.rows)
            # Sample after the raster interrupt has had time to enter its
            # handler, before the line's normal character/sprite output.
            self._run_until(start + line * 63 + 14)
            if frame is not None:
                self._capture(frame, line)
            if self.sprites.active:
                latch_collisions(self, line)
            self._run_until(start + line * 63 + 54)
            self.sprites.prepare_next_line(self.vic.regs, line)
            self._sprite_dma(start + line * 63, 57, range(3), self.sprites.next_rows)
            self._run_until(start + (line + 1) * 63)
        self.frame_number += 1
        if frame is not None:
            frame.number = self.frame_number
        if hasattr(self.sid, "advance"):
            self.sid.advance(self.frame_number * self.vic.frame)
            self.audio = self.sid.take(self.sid.out_len)
        return frame

    @property
    def ready(self) -> bool:
        return bytes((18, 5, 1, 4, 25, 46)) in self.memory[0x400:0x7E8]

    def boot(self, max_frames: int = 250):
        for _ in range(max_frames):
            self.step_frame(capture=False)
            if self.ready and self.memory[0xC6] == 0:
                # Finish the READY prompt and enter the keyboard input loop.
                for _ in range(5):
                    self.step_frame(capture=False)
                return
        raise RuntimeError(f"C64 did not reach BASIC READY in {max_frames} frames "
                           f"(PC=${self.cpu.pc:04X})")

    def type_command(self, text: str):
        data = text.upper().encode("ascii")
        if len(data) > 10:
            raise ValueError("KERNAL keyboard buffer holds at most ten characters")
        self.memory[0x277:0x277 + len(data)] = data
        self.memory[0xC6] = len(data)

    def load_program(self, program: Program, *, run: bool = True, entry: int | None = None):
        for addr, value in enumerate(program.data, program.address):
            self.poke_ram(addr, value)
        end = program.address + len(program.data)
        if program.address == 0x0801:
            for pointer, value in ((0x2B, program.address), (0x2D, end), (0x2F, end),
                                   (0x31, end), (0xAE, end)):
                self.memory[pointer:pointer + 2] = (value & 65535).to_bytes(2, "little")
            if run and entry is None:
                self.type_command("RUN\r")
        elif run and entry is None:
            raise ValueError("machine-code PRG: specify --entry (SYS address)")
        if entry is not None:
            if not 0 <= entry <= 65535:
                raise ValueError("entry address must be $0000..$FFFF")
            self.cpu.pc = entry
