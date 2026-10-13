import pytest

from numsid.cpu6502 import CPU6502, CPUHalt, OPCODES


def make(program, at=0x1000):
    mem = bytearray(0x10000)
    mem[at:at + len(program)] = bytes(program)
    cpu = CPU6502(mem, lambda a: mem[a], lambda a, v: mem.__setitem__(a, v))
    return cpu, mem


def run(program, **regs):
    cpu, mem = make(program + [0x60])
    for k, v in regs.items():
        setattr(cpu, k, v)
    assert cpu.call(0x1000, 100000)
    return cpu, mem


def test_all_opcodes_defined():
    assert sorted(OPCODES) == list(range(256))


def test_loop_and_cycles():
    # LDX #0; loop: TXA; STA $0200,X; INX; BNE loop
    cpu, mem = run([0xA2, 0x00, 0x8A, 0x9D, 0x00, 0x02, 0xE8, 0xD0, 0xF9])
    assert list(mem[0x200:0x300]) == list(range(256))
    # 2 + 256*(2+5+2+3) - 1 (last branch not taken) + 6 (RTS)
    assert cpu.clock == 2 + 256 * 12 - 1 + 6


@pytest.mark.parametrize("a,m,c,res,cout,v", [
    (0x50, 0x50, 0, 0xA0, 0, 1), (0xFF, 0x01, 0, 0x00, 1, 0), (0x80, 0x80, 0, 0x00, 1, 1),
    (0x7F, 0x00, 1, 0x80, 0, 1)])
def test_adc_binary(a, m, c, res, cout, v):
    cpu, _ = run([0x69, m], a=a, c=c)
    assert (cpu.a, cpu.c, cpu.v) == (res, cout, v)


@pytest.mark.parametrize("a,m,c,res,cout", [
    (0x09, 0x01, 0, 0x10, 0), (0x99, 0x01, 0, 0x00, 1), (0x58, 0x46, 1, 0x05, 1),
    (0x12, 0x34, 0, 0x46, 0)])
def test_adc_decimal(a, m, c, res, cout):
    cpu, _ = run([0xF8, 0x69, m, 0xD8], a=a, c=c)
    assert (cpu.a, cpu.c) == (res, cout)


@pytest.mark.parametrize("a,m,c,res,cout", [
    (0x00, 0x01, 1, 0x99, 0), (0x46, 0x12, 1, 0x34, 1), (0x40, 0x13, 1, 0x27, 1),
    (0x32, 0x02, 0, 0x29, 1)])
def test_sbc_decimal(a, m, c, res, cout):
    cpu, _ = run([0xF8, 0xE9, m, 0xD8], a=a, c=c)
    assert (cpu.a, cpu.c) == (res, cout)


def test_jmp_indirect_page_bug():
    cpu, mem = make([0x6C, 0xFF, 0x30])
    mem[0x30FF] = 0x00
    mem[0x3000] = 0x40          # high byte comes from $3000, not $3100
    mem[0x3100] = 0x50
    cpu.pc = 0x1000
    cpu.step()
    assert cpu.pc == 0x4000


def test_stack_php_plp_and_flags():
    # SEC; SED; PHP; CLC; CLD; PLP
    cpu, _ = run([0x38, 0xF8, 0x08, 0x18, 0xD8, 0x28])
    assert cpu.c == 1 and cpu.d == 1
    assert cpu.mem[0x1FF - 2] & 0x30 == 0x30         # B and bit 5 pushed set


def test_bit_sets_n_and_z_independently():
    cpu, mem = run([0x24, 0x10], a=0x01)
    mem[0x10] = 0xC0
    cpu.pc = 0x1000
    cpu.call(0x1000, 100)
    assert cpu.nf & 0x80 and cpu.v == 1 and cpu.zf == 0      # N=1, V=1, Z=1
    assert cpu.get_p() & 0xC2 == 0xC2


def test_branch_page_cross_cycles():
    cpu, _ = make([0xD0, 0x7F], at=0x10F0)   # BNE +127 crosses into $11xx
    cpu.pc = 0x10F0
    cpu.zf = 1
    cpu.step()
    assert cpu.pc == 0x10F2 + 0x7F and cpu.clock == 4


def test_illegal_lax_dcp_isc():
    # LAX $20; DCP $21; ISC $22
    cpu, mem = make([0xA7, 0x20, 0xC7, 0x21, 0xE7, 0x22, 0x60])
    mem[0x20], mem[0x21], mem[0x22] = 0x55, 0x56, 0x10
    cpu.c = 1
    assert cpu.call(0x1000, 100)
    assert cpu.x == 0x55
    assert mem[0x21] == 0x55                     # DCP: decremented, compared equal
    assert mem[0x22] == 0x11


def test_jam_raises():
    cpu, _ = make([0x02])
    with pytest.raises(CPUHalt):
        cpu.call(0x1000, 100)
