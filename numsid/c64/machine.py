"""The C64 around the SID: memory with ROM banking, VIC raster interrupts,
two CIAs, and the two ways tunes are driven.

PSID tunes follow the classic protocol: init(A = song - 1) once, then play
once per frame (or per CIA 1 timer A underflow for CIA-timed songs).
Before each call the processor port at $01 is set according to where the
routine lives, as the PSID specification asks: $37 normally, $36 for
$A000-$CFFF, $34 for $D000-$DFFF and $35 for $E000-$FFFF.

RSID tunes expect a real machine: the CPU runs continuously, and the
tune's own interrupts (CIA timers, VIC raster, CIA 2 NMIs) drive the music.
`_run_machine` runs the CPU up to the next device event, then raises IRQ
(level-triggered, honouring the I flag) or NMI (edge-triggered) as a real
6510 would.  Idle loops such as JMP * are skipped over.

A small replacement KERNAL (see kernal.py) is banked in at $E000-$FFFF
whenever HIRAM ($01 bit 1) is set; writes there always reach the RAM
underneath.  BASIC and character ROM are not provided (RAM shows through).
"""

from __future__ import annotations

from .cia import CIA
from .cpu6502 import CPU6502, CPUHalt, _Return
from .kernal import DEFAULT_VECTORS, KERNAL
from .psid import PSIDFile
from .vic import BADLINE_CYCLES, VIC

INIT_CYCLE_LIMIT = 5_000_000     # PSID init routines may decrunch/copy data
PLAY_CYCLE_LIMIT = 200_000       # a single play call must not hang the player
RSID_INIT_SLICE = 20_000         # RSID: run init this long before audio starts
TRAP = 0xFFF8                    # return address that ends a call / idles


def psid_bank(addr: int) -> int:
    """Processor-port value a PSID routine at `addr` is called with."""
    if addr < 0xA000:
        return 0x37
    if addr < 0xD000:
        return 0x36
    if addr < 0xE000:
        return 0x34
    return 0x35


class C64:
    def __init__(self, tune: PSIDFile, sid, song: int | None = None):
        self.tune = tune
        self.sid = sid
        self.song = song or tune.start_song
        if not 1 <= self.song <= tune.songs:
            raise ValueError(f"song {self.song} out of range 1..{tune.songs}")
        self.rsid = tune.is_rsid
        self.ram = bytearray(0x10000)      # what the CPU sees (ROM swapped in)
        self.under_rom = bytearray(0x2000)  # RAM at $E000-$FFFF while the KERNAL shows
        self.kernal_in = False
        self.cpu = CPU6502(self.ram, self.io_read, self.io_write)
        self.vic = VIC(tune.is_pal)
        self.cia1 = CIA()
        self.cia1.kernal_defaults(0x4025 if tune.is_pal else 0x4295)   # ~60 Hz system IRQ
        self.cia2 = CIA()
        self.cia_timed = tune.uses_cia(self.song) and not self.rsid
        self.frame_cycles = self.vic.frame
        self.play_address = tune.play_address
        self.next_play = 0
        self.halted = False
        self.warnings: list[str] = []
        # RSID machine state
        self.machine_running = False
        self.idle = False
        self.init_returned = False
        self.nmi_line = False
        self.badline_after = 0      # bad lines before this cycle are accounted for
        self._load()

    # ---- memory banking -------------------------------------------------
    def set_port(self, value: int) -> None:
        """Write the processor port at $01 (LORAM, HIRAM, CHAREN)."""
        self.ram[1] = value
        kernal = bool(value & 2)
        if kernal != self.kernal_in:
            if kernal:
                self.under_rom[:] = self.ram[0xE000:]
                self.ram[0xE000:] = KERNAL
            else:
                self.ram[0xE000:] = self.under_rom
            self.kernal_in = kernal

    def _io_visible(self) -> bool:
        port = self.ram[1]
        return bool(port & 4) and bool(port & 3)

    # ---- memory-mapped I/O ----------------------------------------------
    # The CPU calls io_read for reads in $D000-$DFFF, and io_write for writes
    # to $00/$01 and to $D000-$FFFF.

    def io_read(self, addr: int) -> int:
        if not self._io_visible():
            return self.ram[addr]           # RAM (no character ROM)
        now = self.cpu.clock
        if addr < 0xD400:
            return self.vic.read(addr, now)
        if addr < 0xD800:
            return self.sid.read(addr & 0x1F, now)
        if 0xDC00 <= addr <= 0xDCFF:
            return self.cia1.read(addr, now)
        if 0xDD00 <= addr <= 0xDDFF:
            value = self.cia2.read(addr, now)
            if not self.cia2.irq:
                self.nmi_line = False       # acknowledged: the next rise is a new NMI
            return value
        return self.ram[addr]               # colour RAM, I/O 1/2

    def io_write(self, addr: int, value: int) -> None:
        if addr < 2:
            if addr == 1:
                self.set_port(value)
            else:
                self.ram[0] = value
            return
        if addr < 0xD000:
            self.ram[addr] = value
            return
        if addr >= 0xE000:
            if self.kernal_in:
                self.under_rom[addr - 0xE000] = value
            else:
                self.ram[addr] = value
            return
        if not self._io_visible():
            self.ram[addr] = value
            return
        now = self.cpu.clock
        if addr < 0xD400:
            self.vic.write(addr, value, now)
            self._lines_changed()
        elif addr < 0xD800:
            self.sid.write(addr & 0x1F, value, now)
        elif 0xDC00 <= addr <= 0xDCFF:
            self.cia1.write(addr, value, now)
            self._lines_changed()
        elif 0xDD00 <= addr <= 0xDDFF:
            self.cia2.write(addr, value, now)
            if not self.cia2.irq:
                self.nmi_line = False
            self._lines_changed()
        else:
            self.ram[addr] = value

    def _lines_changed(self) -> None:
        """An interrupt source may have changed: let the scheduler look."""
        if self.machine_running:
            self.cpu.until = self.cpu.clock

    @property
    def cia_timer(self) -> int:
        """CIA 1 timer A latch (sets the play rate of CIA-timed songs)."""
        return self.cia1.ta.latch

    # ---- setup -------------------------------------------------------------
    def _load(self) -> None:
        tune, ram = self.tune, self.ram
        ram[0] = 0x2F
        ram[tune.load_address:tune.end_address] = tune.data      # all RAM for now
        ram[0x0314:0x0334] = bytes(b for v in DEFAULT_VECTORS for b in (v & 0xFF, v >> 8))
        ram[0x02A6] = 1 if tune.is_pal else 0     # KERNAL's PAL/NTSC flag
        self.set_port(0x37)                       # BASIC, KERNAL and I/O
        self.cpu.set_p(0x24)
        if not self.rsid:
            # PSID players have always started tunes with the volume up, and
            # many rips rely on it (they never write $D418 themselves).
            self.sid.write(0x18, 0x0F, 0)

    def init(self) -> None:
        """Run the init routine for the selected song, starting at the current clock."""
        if self.rsid:
            self._init_rsid()
            return
        start = self.cpu.clock
        self.set_port(psid_bank(self.tune.init_address))
        self._call(self.tune.init_address, INIT_CYCLE_LIMIT, "init", "jsr", a=self.song - 1)
        if self.cia_timed:
            # play runs on CIA 1 timer A underflows, as the timer is left by init
            self.frame_cycles = self.cia_timer + 1          # nominal period
            self.cia1.sync(self.cpu.clock)
            self.next_play = self._next_underflow()
        else:
            frames = (self.cpu.clock - start) // self.frame_cycles + 1
            self.next_play = start + frames * self.frame_cycles

    def _next_underflow(self) -> int:
        ta = self.cia1.ta
        if ta.running:
            return ta.next_underflow
        return self.cpu.clock + ta.remaining       # stopped: as if restarted now

    # ---- PSID protocol ------------------------------------------------------------
    def play_frame(self) -> int:
        """Advance to the next play call (PSID) or the next frame (RSID).
        Returns the clock at which the next call / frame is due."""
        if self.rsid:
            end = self.next_play + self.frame_cycles
            self._run_machine(end)
            self.next_play = end
            return end
        cpu = self.cpu
        due = self.next_play
        if cpu.clock < due:
            cpu.clock = due
        if self.cia_timed:
            # the underflow that triggers this call reloads the timer from
            # the latch as it is now; writes made by play apply to later periods
            self.cia1.sync(due)
        if not self.halted:
            if self.tune.play_address:
                self.set_port(psid_bank(self.tune.play_address))
                self._call(self.tune.play_address, PLAY_CYCLE_LIMIT, "play", "jsr")
            else:
                # the tune installed an interrupt handler: enter it like an IRQ
                # (through the KERNAL's $FF48 and ($0314) if it is banked in)
                ram = self.ram
                handler = ram[0xFFFE] | (ram[0xFFFF] << 8)
                self._call(handler, PLAY_CYCLE_LIMIT, "play", "irq")
        if self.cia_timed:
            self.next_play = self._next_underflow()
        else:
            self.next_play += self.frame_cycles
        return self.next_play

    def _call(self, addr: int, limit: int, what: str, mode: str, a: int | None = None) -> None:
        cpu = self.cpu
        cpu.sp = 0xFF
        try:
            if not cpu.call(addr, limit, a=a, mode=mode, trap=TRAP):
                self._warn(f"{what} routine did not return within {limit} cycles")
        except CPUHalt as exc:
            self._warn(f"{what}: {exc}; stopping the tune")
            self.halted = True

    # ---- RSID: a running machine ---------------------------------------------------
    def _init_rsid(self) -> None:
        cpu = self.cpu
        cpu.sp = 0xFF
        cpu.a = self.song - 1
        cpu.i = 0                                   # as after a SYS from BASIC
        cpu.trap = TRAP
        cpu.push((TRAP - 1) >> 8)
        cpu.push((TRAP - 1) & 0xFF)
        cpu.pc = self.tune.init_address
        # Many RSID inits never return (they become the main loop) and
        # others take a while; either way the machine just keeps running.
        self._run_machine(cpu.clock + RSID_INIT_SLICE)
        self.next_play = cpu.clock

    def _run_machine(self, end: int) -> None:
        cpu = self.cpu
        cia1, cia2, vic = self.cia1, self.cia2, self.vic
        self.machine_running = True
        try:
            while cpu.clock < end:
                now = cpu.clock
                bad = vic.next_badline(self.badline_after)
                if bad is not None and bad <= now:
                    # the VIC halts the CPU for the character fetches
                    self.badline_after = bad + 1
                    if not (self.idle or self.halted):
                        cpu.clock = max(now, bad + BADLINE_CYCLES)
                    continue
                cia1.sync(now)
                cia2.sync(now)
                vic.sync(now)
                nmi = cia2.irq
                if nmi and not self.nmi_line and not self.halted:
                    self.nmi_line = True
                    cpu.interrupt(0xFFFA)
                    self.idle = False
                    continue
                self.nmi_line = nmi
                irq = cia1.irq or vic.irq
                cpu.irq_pending = irq
                if irq and not cpu.i and not self.halted:
                    cpu.interrupt(0xFFFE)
                    self.idle = False
                    continue
                ev = end
                for t in (cia1.next_event(), cia2.next_event(), vic.next_event(), bad):
                    if t is not None and t < ev:
                        ev = t
                if ev <= now:
                    ev = now + 1
                if self.idle or self.halted:
                    cpu.clock = ev                  # nothing runs until the next event
                    continue
                cpu.until = ev
                try:
                    cpu.run()
                except _Return:                     # init returned, or an RTI to idle
                    self.idle = True
                    if not self.init_returned:
                        self.init_returned = True
                        cpu.i = 0                   # the player loop runs with IRQs on
                except CPUHalt as exc:
                    self._warn(f"{exc}; stopping the tune")
                    self.halted = True
        finally:
            self.machine_running = False

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)
