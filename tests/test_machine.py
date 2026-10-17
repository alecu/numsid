"""The interrupt-driven machine (RSID), the replacement KERNAL and banking."""

import pytest

from numsid.c64.kernal import KERNAL
from numsid.c64.machine import C64
from numsid.c64.psid import PSIDFile

PAL = 985248


class NullSID:
    def write(self, reg, value, cycle):
        pass

    def read(self, reg, cycle):
        return 0


def tune(code: bytes, *, rsid: bool, load: int = 0x1000, play: int = 0) -> PSIDFile:
    return PSIDFile("RSID" if rsid else "PSID", 2, load, load, play, 1, 1, 0, "t", "", "",
                    0x14, 0, 0, code)


def run_seconds(machine: C64, seconds: float) -> int:
    """Init, then play for about `seconds`; returns the cycles elapsed."""
    machine.init()
    for _ in range(int(seconds * PAL / machine.frame_cycles)):
        machine.play_frame()
    return machine.cpu.clock


def counter(m: C64) -> int:
    return m.ram[0x2000] | (m.ram[0x2001] << 8)


# a handler body that counts calls in $2000/$2001
COUNT = bytes([0xEE, 0x00, 0x20, 0xD0, 0x03, 0xEE, 0x01, 0x20])   # INC $2000 : BNE +3 : INC $2001


def test_rsid_raster_irq_through_kernal_vector():
    code = bytes([
        0x78,                                   # SEI
        0xA9, 0x30, 0x8D, 0x14, 0x03,           # $0314 = $1030
        0xA9, 0x10, 0x8D, 0x15, 0x03,
        0xA9, 0x7F, 0x8D, 0x0D, 0xDC,           # CIA 1 interrupts off
        0xA9, 0x01, 0x8D, 0x1A, 0xD0,           # raster IRQ on
        0xA9, 0x30, 0x8D, 0x12, 0xD0,           # at line 48
        0x58, 0x60,                             # CLI : RTS
    ]).ljust(0x30, b"\xEA") + bytes([
        0xA9, 0x01, 0x8D, 0x19, 0xD0,           # $1030: acknowledge raster IRQ
    ]) + COUNT + bytes([0x4C, 0x81, 0xEA])      # JMP $EA81 (KERNAL exit)
    m = C64(tune(code, rsid=True), NullSID())
    cycles = run_seconds(m, 2)
    assert m.init_returned and not m.halted
    assert counter(m) == pytest.approx(cycles / (63 * 312), abs=1)     # once per frame


def test_rsid_own_vectors_cia_irq_and_endless_main_loop():
    code = bytes([
        0x78,
        0xA9, 0x35, 0x85, 0x01,                 # bank out BASIC and KERNAL
        0xA9, 0x40, 0x8D, 0xFE, 0xFF,           # $FFFE = $1040
        0xA9, 0x10, 0x8D, 0xFF, 0xFF,
        0xA9, 0x00, 0x8D, 0x04, 0xDC,           # timer A latch $2000
        0xA9, 0x20, 0x8D, 0x05, 0xDC,
        0xA9, 0x11, 0x8D, 0x0E, 0xDC,           # start + force load
        0xA9, 0x81, 0x8D, 0x0D, 0xDC,           # timer A interrupt on
        0x58,                                   # CLI
        0x4C, 0x24, 0x10,                       # $1024: JMP $1024 forever
    ]).ljust(0x40, b"\xEA") + bytes([
        0x48, 0xAD, 0x0D, 0xDC,                 # $1040: PHA : LDA $DC0D (acknowledge)
    ]) + COUNT + bytes([0x68, 0x40])            # PLA : RTI
    m = C64(tune(code, rsid=True), NullSID())
    cycles = run_seconds(m, 1)
    assert not m.init_returned                  # init never returns, yet the music runs
    assert counter(m) == pytest.approx(cycles / 0x2001, abs=2)


def test_rsid_cia2_nmi_at_sample_rate():
    code = bytes([
        0xA9, 0x30, 0x8D, 0x18, 0x03,           # $0318 = $1030 (KERNAL NMI vector)
        0xA9, 0x10, 0x8D, 0x19, 0x03,
        0xA9, 0x64, 0x8D, 0x04, 0xDD,           # CIA 2 timer A = 100 cycles
        0xA9, 0x00, 0x8D, 0x05, 0xDD,
        0xA9, 0x11, 0x8D, 0x0E, 0xDD,
        0xA9, 0x81, 0x8D, 0x0D, 0xDD,           # NMI on timer A
        0x60,
    ]).ljust(0x30, b"\xEA") + bytes([
        0x48, 0xAD, 0x0D, 0xDD,                 # $1030: PHA : LDA $DD0D (acknowledge)
    ]) + COUNT + bytes([0x68, 0x40])
    m = C64(tune(code, rsid=True), NullSID())
    cycles = run_seconds(m, 1)
    assert counter(m) == pytest.approx(cycles / 101, abs=3)


def test_psid_code_under_kernal_sees_ram():
    # init at $E000 reads its own data at $E100: the PSID bank rule ($35)
    # must hide the KERNAL ROM
    code = bytes([0xAD, 0x00, 0xE1, 0x8D, 0x00, 0x20, 0x60]).ljust(0x100, b"\x00") + b"\x42"
    m = C64(tune(code, rsid=False, load=0xE000, play=0xE006), NullSID())
    m.init()
    assert m.ram[0x2000] == 0x42


def test_writes_reach_ram_under_rom():
    m = C64(tune(b"\x60", rsid=False), NullSID())
    assert m.kernal_in and m.ram[0xFF48] == KERNAL[0x1F48]
    m.io_write(0xE100, 0x55)                   # KERNAL banked in: goes underneath
    assert m.ram[0xE100] == KERNAL[0x100]
    m.io_write(0x0001, 0x35)                    # bank it out
    assert m.ram[0xE100] == 0x55
    m.io_write(0x0001, 0x37)
    assert m.ram[0xE100] == KERNAL[0x100]


def test_psid_play_zero_uses_kernal_irq_path():
    # init installs a $0314 handler; play address 0 means "call it as an IRQ"
    code = bytes([
        0xA9, 0x10, 0x8D, 0x14, 0x03,           # $0314 = $1010
        0xA9, 0x10, 0x8D, 0x15, 0x03,
        0x60,
    ]).ljust(0x10, b"\xEA") + COUNT + bytes([0x4C, 0x31, 0xEA])   # ... JMP $EA31
    m = C64(tune(code, rsid=False, play=0), NullSID())
    run_seconds(m, 1)
    assert not m.warnings and counter(m) == pytest.approx(50, abs=1)
    assert m.ram[0xA2] > 0                      # the KERNAL handler ticked the jiffy clock


def test_rmw_acknowledge_of_raster_irq():
    # "INC $D019" acknowledges through the 6502's dummy write of the old
    # value; without it the IRQ would fire again right after RTI
    code = bytes([
        0x78,
        0xA9, 0x30, 0x8D, 0x14, 0x03, 0xA9, 0x10, 0x8D, 0x15, 0x03,   # $0314 = $1030
        0xA9, 0x7F, 0x8D, 0x0D, 0xDC,
        0xA9, 0x01, 0x8D, 0x1A, 0xD0,
        0x58, 0x60,
    ]).ljust(0x30, b"\xEA") + bytes([0xEE, 0x19, 0xD0]) + COUNT + bytes([0x4C, 0x81, 0xEA])
    m = C64(tune(code, rsid=True), NullSID())
    cycles = run_seconds(m, 1)
    assert counter(m) == pytest.approx(cycles / (63 * 312), abs=1)


def busy_loop_count(display_on: bool) -> int:
    # init sets the screen on/off, then counts loop iterations forever
    ctrl = 0x1B if display_on else 0x0B
    code = bytes([
        0xA9, ctrl, 0x8D, 0x11, 0xD0,           # LDA #ctrl : STA $D011
        0x78,                                   # SEI (no interrupts)
    ]) + COUNT + bytes([0x4C, 0x06, 0x10])      # $1006: count, JMP $1006
    m = C64(tune(code, rsid=True), NullSID())
    run_seconds(m, 0.5)
    return counter(m)


def test_bad_lines_steal_cpu_cycles():
    on, off = busy_loop_count(True), busy_loop_count(False)
    stolen = 1 - on / off                       # 25 bad lines x 43 cycles per frame
    assert stolen == pytest.approx(25 * 43 / (63 * 312), abs=0.005)
