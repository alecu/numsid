"""Minimal C64 environment for running PSID tunes.

Provides 64 KiB of RAM, the SID at $D400-$D7FF (mirrored every 32 bytes),
a few VIC/CIA registers that players commonly touch, and the PSID calling
convention: init(A = song - 1) once, then play() every frame.
"""

from __future__ import annotations

from .cpu6502 import CPU6502, CPUHalt
from .psid import PSIDFile, PAL_CYCLES_PER_FRAME, NTSC_CYCLES_PER_FRAME

INIT_CYCLE_LIMIT = 5_000_000     # init routines may decrunch/copy data
PLAY_CYCLE_LIMIT = 200_000       # a single play call must not hang the player

# Stand-in for the KERNAL IRQ exits at $EA31 / $EA81 that IRQ-driven tunes
# jump to when done: restore Y, X, A and return from interrupt.
_KERNAL_IRQ_EXIT = bytes([0x68, 0xA8, 0x68, 0xAA, 0x68, 0x40])   # PLA TAY PLA TAX PLA RTI


class C64:
    def __init__(self, tune: PSIDFile, sid, song: int | None = None):
        self.tune = tune
        self.sid = sid
        self.song = song or tune.start_song
        if not 1 <= self.song <= tune.songs:
            raise ValueError(f"song {self.song} out of range 1..{tune.songs}")
        self.ram = bytearray(0x10000)
        self.cpu = CPU6502(self.ram, self.io_read, self.io_write)
        self.cia_timer = 0x4025 if tune.is_pal else 0x4295   # KERNAL default, ~60 Hz
        self.frame_cycles = PAL_CYCLES_PER_FRAME if tune.is_pal else NTSC_CYCLES_PER_FRAME
        self.play_address = tune.play_address
        self.next_play = 0
        self.halted = False
        self.warnings: list[str] = []
        self._load()

    # ---- memory-mapped I/O ----------------------------------------------
    # The 6510 port at $01 selects what appears at $D000-$DFFF: I/O when
    # CHAREN (bit 2) is set and LORAM/HIRAM aren't both clear, otherwise
    # character ROM (reads) or RAM.  We have no character ROM, so that case
    # reads RAM.

    def _io_visible(self) -> bool:
        port = self.ram[1]
        return bool(port & 4) and bool(port & 3)

    def io_read(self, addr: int) -> int:
        if not self._io_visible():
            return self.ram[addr]
        if 0xD400 <= addr <= 0xD7FF:
            return self.sid.read(addr & 0x1F, self.cpu.clock)
        line = (self.cpu.clock // 63) % 312
        if addr == 0xD012:           # VIC raster counter
            return line & 0xFF
        if addr == 0xD011:
            return 0x1B | (0x80 if line > 0xFF else 0)
        if addr == 0xDC04:
            return self.cia_timer & 0xFF
        if addr == 0xDC05:
            return self.cia_timer >> 8
        return self.ram[addr]

    def io_write(self, addr: int, value: int) -> None:
        if self._io_visible():
            if 0xD400 <= addr <= 0xD7FF:
                self.sid.write(addr & 0x1F, value, self.cpu.clock)
            elif addr == 0xDC04:
                self.cia_timer = (self.cia_timer & 0xFF00) | value
            elif addr == 0xDC05:
                self.cia_timer = (self.cia_timer & 0x00FF) | (value << 8)
        self.ram[addr] = value

    # ---- PSID driver ------------------------------------------------------
    def _load(self) -> None:
        tune, ram = self.tune, self.ram
        ram[0] = 0x2F
        ram[1] = 0x37                 # BASIC, KERNAL and I/O banked in
        ram[tune.load_address:tune.end_address] = tune.data
        for stub in (0xEA31, 0xEA81):
            if not (tune.load_address <= stub + 5 and stub < tune.end_address):
                ram[stub:stub + len(_KERNAL_IRQ_EXIT)] = _KERNAL_IRQ_EXIT
        ram[0x02A6] = 1 if tune.is_pal else 0     # KERNAL's PAL/NTSC flag
        self.cpu.set_p(0x24)

    def init(self) -> None:
        """Run the init routine for the selected song, starting at the current clock."""
        start = self.cpu.clock
        self._call(self.tune.init_address, INIT_CYCLE_LIMIT, "init", "jsr", a=self.song - 1)
        if self.tune.play_address == 0:
            # The tune installed its own interrupt handler; call that instead.
            ram = self.ram
            if ram[1] & 2:
                self.play_address = ram[0x0314] | (ram[0x0315] << 8)
            else:
                self.play_address = ram[0xFFFE] | (ram[0xFFFF] << 8)
        if self.tune.uses_cia(self.song):
            self.frame_cycles = max(self.cia_timer, 256) + 1
        frames = (self.cpu.clock - start) // self.frame_cycles + 1
        self.next_play = start + frames * self.frame_cycles

    def play_frame(self) -> int:
        """Call play once; returns the clock at which the next frame starts."""
        cpu = self.cpu
        if cpu.clock < self.next_play:
            cpu.clock = self.next_play
        if not self.halted:
            if self.tune.play_address:
                mode = "jsr"
            else:
                mode = "kernal_irq" if self.ram[1] & 2 else "irq"
            self._call(self.play_address, PLAY_CYCLE_LIMIT, "play", mode)
        self.next_play += self.frame_cycles
        return self.next_play

    def _call(self, addr: int, limit: int, what: str, mode: str, a: int | None = None) -> None:
        cpu = self.cpu
        cpu.sp = 0xFF
        try:
            if not cpu.call(addr, limit, a=a, mode=mode):
                self._warn(f"{what} routine did not return within {limit} cycles")
        except CPUHalt as exc:
            self._warn(f"{what}: {exc}; stopping the tune")
            self.halted = True

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)
