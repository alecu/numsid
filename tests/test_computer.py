"""The ROM-booted machine, distinct from SID file playback."""

import io
import tarfile
from zipfile import ZipFile

import pytest

from numsid.c64.collisions import latch_collisions
from numsid.c64.computer import Computer, VideoFrame
from numsid.c64.peripherals import Input, PortCIA
from numsid.c64.program import Program
from numsid.c64.roms import ROMs


@pytest.fixture
def machine():
    return Computer(ROMs(bytes([0xBA]) * 8192, bytes([0xEA]) * 8192, bytes([0xCC]) * 4096))


def test_pla_banking_ddr_and_ram_under_all_roms(machine):
    m = machine
    m.write(0xA123, 0x11)
    m.write(0xE123, 0x22)
    assert m.memory[0xA123] == 0xBA and m.memory[0xE123] == 0xEA
    m.write(1, 0x33)  # character ROM visible instead of I/O
    m.write(0xD123, 0x33)
    assert m.read(0xD123) == 0xCC
    m.write(1, 0x30)  # all RAM
    assert (m.memory[0xA123], m.read(0xD123), m.memory[0xE123]) == (0x11, 0x33, 0x22)
    m.write(0, 0)     # inputs pulled high: ROMs and I/O visible again
    assert m.memory[1] & 7 == 7
    assert m.memory[0xA123] == 0xBA and m.memory[0xE123] == 0xEA
    m.write(0, 0x2F)
    assert m.memory[0xA123] == 0x11


def test_cpu_stores_and_rmw_write_under_basic(machine):
    m = machine
    # LDA #$42; STA $A123; INC $A124; RTS (INC reads ROM, stores to RAM).
    m.memory[0x1000:0x1009] = bytes.fromhex("a9428d23a1ee24a160")
    assert m.cpu.call(0x1000, 100)
    m.write(1, 0x30)
    assert m.memory[0xA123] == 0x42
    assert m.memory[0xA124] == 0xBB


def test_cpu_brk_uses_irq_vector_and_pushes_pc_plus_two(machine):
    m = machine
    m.write(1, 0x30)
    m.memory[0xFFFE:] = bytes.fromhex("0020")
    m.memory[0x1000] = 0
    m.cpu.pc, m.cpu.sp, m.cpu.clock = 0x1000, 0xFF, 0
    m.cpu.step()
    assert m.cpu.pc == 0x2000 and m.cpu.clock == 7 and m.cpu.i
    assert m.memory[0x1FF:0x200] == b"\x10"
    assert m.memory[0x1FE] == 2 and m.memory[0x1FD] & 0x30 == 0x30


def test_vic_uses_ram_under_cpu_rom_and_character_rom_wiring(machine):
    m = machine
    m.write(0xA123, 0x42)
    m.cia2.write(2, 3, 0)
    m.cia2.write(0, 1, 0)  # VIC bank $8000
    assert m.cia2.vic_bank == 0x8000
    assert m.vic_read(0x2123) == 0x42
    assert m.vic_read(0x1123) == 0xCC
    frame = VideoFrame.empty()
    m._capture(frame, 60)
    assert frame.memory[60, 0x2123] == 0x42
    assert frame.banks[60] == 2
    # A later CPU bank switch must not alter the captured line.
    m.write(1, 0x30)
    m.write(0xA123, 0x55)
    assert frame.memory[60, 0x2123] == 0x42


def test_keyboard_matrix_both_directions_and_joysticks():
    inputs = Input()
    cia = PortCIA(inputs)
    inputs.key("SPACE")  # PA7 <-> PB4
    cia.write(2, 255, 0)
    cia.write(0, 0x7F, 0)
    assert cia.read(1, 0) == 0xEF
    inputs.key("SPACE", False)
    assert cia.read(1, 0) == 255
    cia.write(2, 0, 0)
    cia.write(3, 255, 0)
    cia.write(1, 0xEF, 0)
    inputs.key("SPACE")
    assert cia.read(0, 0) == 0x7F
    inputs.release()
    cia.write(3, 0, 0)
    inputs.joystick("fire", port=2)
    inputs.joystick("up", port=1)
    assert cia.read(0, 0) == 0xEF and cia.read(1, 0) == 0xFE
    inputs.release()
    assert cia.read(0, 0) == cia.read(1, 0) == 255


def test_sprite_collision_latches_and_interrupts(machine):
    m = machine
    m.cia2.write(2, 3, 0)
    m.cia2.write(0, 2, 0)  # bank $4000, RAM glyphs
    r = m.vic.regs
    r[0:4] = bytes((24, 51, 24, 51))
    r[0x11], r[0x16], r[0x18], r[0x15] = 0x1B, 8, 0x10, 3
    m.sprites.rows[:2] = 0
    m.sprites.active = 3
    m.memory[0x47F8:0x47FA] = bytes((0x30, 0x30))
    m.memory[0x4C00] = 0x80  # two sprites: one pixel at (24,51)
    m.memory[0x4400] = 1
    m.memory[0x4008] = 0x80  # foreground character at (24,51)
    m.vic.enable = 6
    latch_collisions(m, 51)
    assert m.vic.irq
    assert m.read(0xD01E) == 3 and m.read(0xD01F) == 3
    assert m.read(0xD01E) == m.read(0xD01F) == 0
    m.write(0xD019, 6)
    assert not m.vic.irq


def test_program_and_rom_archives_are_read_without_extraction(tmp_path):
    path = tmp_path / "program.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("../../game.prg", bytes.fromhex("0108a94260"))
    program = Program.load(path)
    assert program.address == 0x801 and program.data == bytes.fromhex("a94260")
    assert not (tmp_path.parent / "game.prg").exists()
    rom_path = tmp_path / "vice.tar.gz"
    with tarfile.open(rom_path, "w:gz") as archive:
        for name, size in (("basic-901226-01.bin", 8192),
                           ("kernal-901227-03.bin", 8192), ("chargen-901225-01.bin", 4096)):
            member = tarfile.TarInfo("vice-3.10/data/C64/" + name)
            member.size = size
            archive.addfile(member, io.BytesIO(bytes(size)))
    assert len(ROMs.load(rom_path).chargen) == 4096


def test_reject_truncated_or_overflowing_programs(tmp_path):
    path = tmp_path / "bad.prg"
    for data in (b"\x01", b"\xff\xff\x00\x01"):
        path.write_bytes(data)
        with pytest.raises(ValueError):
            Program.load(path)
