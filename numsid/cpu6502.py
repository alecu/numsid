"""MOS 6502 / 6510 CPU emulator in pure Python.

All 151 documented opcodes plus the commonly used "illegal" ones (LAX, SAX,
DCP, ISC, SLO, RLA, SRE, RRA, ANC, ALR, ARR, SBX, the multi-byte NOPs, ...)
are implemented, with NMOS decimal mode and cycle counts including page
crossing and taken-branch penalties.

For speed, every opcode is a dedicated closure generated from small source
templates, so one instruction costs one Python call with its addressing mode
inlined.  The processor status is kept unpacked: `c`, `v`, `d`, `i` are 0/1
and N/Z are evaluated lazily from `nf` (bit 7) and `zf` (Z set when zf == 0).

Memory is a flat 64 KiB bytearray.  Accesses that might hit I/O ($Dxxx)
are routed through the `io_read` / `io_write` callbacks; zero page, stack
and opcode fetches go straight to RAM.
"""

from __future__ import annotations


class CPUHalt(Exception):
    """Raised on JAM/KIL or BRK: the program went off the rails."""


class _Return(Exception):
    """Internal: the subroutine called by `call()` has returned."""


# (mnemonic, mode) -> {opcode: cycles}
# modes: imp acc imm zp zpx zpy abs absx absy ind indx indy rel
_TABLE = """
ADC imm 69 2 | zp 65 3 | zpx 75 4 | abs 6D 4 | absx 7D 4 | absy 79 4 | indx 61 6 | indy 71 5
AND imm 29 2 | zp 25 3 | zpx 35 4 | abs 2D 4 | absx 3D 4 | absy 39 4 | indx 21 6 | indy 31 5
ASL acc 0A 2 | zp 06 5 | zpx 16 6 | abs 0E 6 | absx 1E 7
BIT zp 24 3 | abs 2C 4
CMP imm C9 2 | zp C5 3 | zpx D5 4 | abs CD 4 | absx DD 4 | absy D9 4 | indx C1 6 | indy D1 5
CPX imm E0 2 | zp E4 3 | abs EC 4
CPY imm C0 2 | zp C4 3 | abs CC 4
DEC zp C6 5 | zpx D6 6 | abs CE 6 | absx DE 7
EOR imm 49 2 | zp 45 3 | zpx 55 4 | abs 4D 4 | absx 5D 4 | absy 59 4 | indx 41 6 | indy 51 5
INC zp E6 5 | zpx F6 6 | abs EE 6 | absx FE 7
LDA imm A9 2 | zp A5 3 | zpx B5 4 | abs AD 4 | absx BD 4 | absy B9 4 | indx A1 6 | indy B1 5
LDX imm A2 2 | zp A6 3 | zpy B6 4 | abs AE 4 | absy BE 4
LDY imm A0 2 | zp A4 3 | zpx B4 4 | abs AC 4 | absx BC 4
LSR acc 4A 2 | zp 46 5 | zpx 56 6 | abs 4E 6 | absx 5E 7
ORA imm 09 2 | zp 05 3 | zpx 15 4 | abs 0D 4 | absx 1D 4 | absy 19 4 | indx 01 6 | indy 11 5
ROL acc 2A 2 | zp 26 5 | zpx 36 6 | abs 2E 6 | absx 3E 7
ROR acc 6A 2 | zp 66 5 | zpx 76 6 | abs 6E 6 | absx 7E 7
SBC imm E9 2 | zp E5 3 | zpx F5 4 | abs ED 4 | absx FD 4 | absy F9 4 | indx E1 6 | indy F1 5
STA zp 85 3 | zpx 95 4 | abs 8D 4 | absx 9D 5 | absy 99 5 | indx 81 6 | indy 91 6
STX zp 86 3 | zpy 96 4 | abs 8E 4
STY zp 84 3 | zpx 94 4 | abs 8C 4
BPL rel 10 2
BMI rel 30 2
BVC rel 50 2
BVS rel 70 2
BCC rel 90 2
BCS rel B0 2
BNE rel D0 2
BEQ rel F0 2
JMP abs 4C 3 | ind 6C 5
JSR abs 20 6
RTS imp 60 6
RTI imp 40 6
BRK imp 00 7
PHA imp 48 3
PLA imp 68 4
PHP imp 08 3
PLP imp 28 4
CLC imp 18 2
SEC imp 38 2
CLI imp 58 2
SEI imp 78 2
CLV imp B8 2
CLD imp D8 2
SED imp F8 2
TAX imp AA 2
TXA imp 8A 2
TAY imp A8 2
TYA imp 98 2
TSX imp BA 2
TXS imp 9A 2
INX imp E8 2
INY imp C8 2
DEX imp CA 2
DEY imp 88 2
NOP imp EA 2 | imp 1A 2 | imp 3A 2 | imp 5A 2 | imp 7A 2 | imp DA 2 | imp FA 2
NOP imm 80 2 | imm 82 2 | imm 89 2 | imm C2 2 | imm E2 2
NOP zp 04 3 | zp 44 3 | zp 64 3 | zpx 14 4 | zpx 34 4 | zpx 54 4 | zpx 74 4 | zpx D4 4 | zpx F4 4
NOP abs 0C 4 | absx 1C 4 | absx 3C 4 | absx 5C 4 | absx 7C 4 | absx DC 4 | absx FC 4
SLO zp 07 5 | zpx 17 6 | abs 0F 6 | absx 1F 7 | absy 1B 7 | indx 03 8 | indy 13 8
RLA zp 27 5 | zpx 37 6 | abs 2F 6 | absx 3F 7 | absy 3B 7 | indx 23 8 | indy 33 8
SRE zp 47 5 | zpx 57 6 | abs 4F 6 | absx 5F 7 | absy 5B 7 | indx 43 8 | indy 53 8
RRA zp 67 5 | zpx 77 6 | abs 6F 6 | absx 7F 7 | absy 7B 7 | indx 63 8 | indy 73 8
DCP zp C7 5 | zpx D7 6 | abs CF 6 | absx DF 7 | absy DB 7 | indx C3 8 | indy D3 8
ISC zp E7 5 | zpx F7 6 | abs EF 6 | absx FF 7 | absy FB 7 | indx E3 8 | indy F3 8
LAX zp A7 3 | zpy B7 4 | abs AF 4 | absy BF 4 | indx A3 6 | indy B3 5 | imm AB 2
SAX zp 87 3 | zpy 97 4 | abs 8F 4 | indx 83 6
ANC imm 0B 2 | imm 2B 2
ALR imm 4B 2
ARR imm 6B 2
SBX imm CB 2
SBC imm EB 2
ANE imm 8B 2
LAS absy BB 4
SHA indy 93 6 | absy 9F 5
SHX absy 9E 5
SHY absx 9C 5
TAS absy 9B 5
KIL imp 02 2 | imp 12 2 | imp 22 2 | imp 32 2 | imp 42 2 | imp 52 2 | imp 62 2 | imp 72 2 | imp 92 2 | imp B2 2 | imp D2 2 | imp F2 2
"""

# Operand fetch / effective address computation.  Leaves `ea` (or `v` for
# immediate) and, for indexed modes, `base` + `idx` for the page-cross test.
_MODE = {
    "imp": [],
    "acc": [],
    "rel": [],
    "ind": [],
    "imm": ["pc = self.pc", "v = mem[pc]", "self.pc = (pc + 1) & 0xFFFF"],
    "zp": ["pc = self.pc", "ea = mem[pc]", "self.pc = (pc + 1) & 0xFFFF"],
    "zpx": ["pc = self.pc", "ea = (mem[pc] + self.x) & 0xFF", "self.pc = (pc + 1) & 0xFFFF"],
    "zpy": ["pc = self.pc", "ea = (mem[pc] + self.y) & 0xFF", "self.pc = (pc + 1) & 0xFFFF"],
    "abs": ["pc = self.pc", "ea = mem[pc] | (mem[(pc + 1) & 0xFFFF] << 8)",
            "self.pc = (pc + 2) & 0xFFFF"],
    "absx": ["pc = self.pc", "base = mem[pc] | (mem[(pc + 1) & 0xFFFF] << 8)",
             "self.pc = (pc + 2) & 0xFFFF", "idx = self.x", "ea = (base + idx) & 0xFFFF"],
    "absy": ["pc = self.pc", "base = mem[pc] | (mem[(pc + 1) & 0xFFFF] << 8)",
             "self.pc = (pc + 2) & 0xFFFF", "idx = self.y", "ea = (base + idx) & 0xFFFF"],
    "indx": ["pc = self.pc", "zp = (mem[pc] + self.x) & 0xFF", "self.pc = (pc + 1) & 0xFFFF",
             "ea = mem[zp] | (mem[(zp + 1) & 0xFF] << 8)"],
    "indy": ["pc = self.pc", "zp = mem[pc]", "self.pc = (pc + 1) & 0xFFFF",
             "base = mem[zp] | (mem[(zp + 1) & 0xFF] << 8)", "idx = self.y",
             "ea = (base + idx) & 0xFFFF"],
}
_ZERO_PAGE = {"zp", "zpx", "zpy"}
_INDEXED = {"absx", "absy", "indy"}

_SET_NZ = "self.zf = self.nf = {0}"

# Operations that only read their operand `v`.
_READ_OPS = {
    "LDA": ["self.a = self.zf = self.nf = v"],
    "LDX": ["self.x = self.zf = self.nf = v"],
    "LDY": ["self.y = self.zf = self.nf = v"],
    "LAX": ["self.a = self.x = self.zf = self.nf = v"],
    "AND": ["v &= self.a", "self.a = self.zf = self.nf = v"],
    "ORA": ["v |= self.a", "self.a = self.zf = self.nf = v"],
    "EOR": ["v ^= self.a", "self.a = self.zf = self.nf = v"],
    "ADC": ["adc(v)"],
    "SBC": ["sbc(v)"],
    "CMP": ["t = self.a - v", "self.c = 1 if t >= 0 else 0", "self.zf = self.nf = t & 0xFF"],
    "CPX": ["t = self.x - v", "self.c = 1 if t >= 0 else 0", "self.zf = self.nf = t & 0xFF"],
    "CPY": ["t = self.y - v", "self.c = 1 if t >= 0 else 0", "self.zf = self.nf = t & 0xFF"],
    "BIT": ["self.zf = self.a & v", "self.nf = v", "self.v = (v >> 6) & 1"],
    "NOP": [],
    "LAS": ["v &= self.sp", "self.a = self.x = self.sp = self.zf = self.nf = v"],
    # immediate-only illegal opcodes
    "ANC": ["v &= self.a", "self.a = self.zf = self.nf = v", "self.c = v >> 7"],
    "ALR": ["v &= self.a", "self.c = v & 1", "v >>= 1", "self.a = self.zf = self.nf = v"],
    "ARR": ["v = ((v & self.a) >> 1) | (self.c << 7)", "self.a = self.zf = self.nf = v",
            "self.c = (v >> 6) & 1", "self.v = ((v >> 6) ^ (v >> 5)) & 1"],
    "SBX": ["t = (self.a & self.x) - v", "self.c = 1 if t >= 0 else 0",
            "self.x = self.zf = self.nf = t & 0xFF"],
    "ANE": ["v &= (self.a | 0xEE) & self.x", "self.a = self.zf = self.nf = v"],
}

# Operations that store a value `v` computed from registers.
_WRITE_OPS = {
    "STA": ["v = self.a"],
    "STX": ["v = self.x"],
    "STY": ["v = self.y"],
    "SAX": ["v = self.a & self.x"],
    "SHA": ["v = self.a & self.x & (((base >> 8) + 1) & 0xFF)"],
    "SHX": ["v = self.x & (((base >> 8) + 1) & 0xFF)"],
    "SHY": ["v = self.y & (((base >> 8) + 1) & 0xFF)"],
    "TAS": ["self.sp = self.a & self.x", "v = self.sp & (((base >> 8) + 1) & 0xFF)"],
}

# Read-modify-write: read `v`, compute `w`, write `w` back.
_RMW_CORE = {
    "ASL": ["self.c = v >> 7", "w = (v << 1) & 0xFF"],
    "LSR": ["self.c = v & 1", "w = v >> 1"],
    "ROL": ["w = ((v << 1) | self.c) & 0xFF", "self.c = v >> 7"],
    "ROR": ["w = (v >> 1) | (self.c << 7)", "self.c = v & 1"],
    "INC": ["w = (v + 1) & 0xFF"],
    "DEC": ["w = (v - 1) & 0xFF"],
}
_RMW_OPS = {
    "ASL": _RMW_CORE["ASL"] + ["self.zf = self.nf = w"],
    "LSR": _RMW_CORE["LSR"] + ["self.zf = self.nf = w"],
    "ROL": _RMW_CORE["ROL"] + ["self.zf = self.nf = w"],
    "ROR": _RMW_CORE["ROR"] + ["self.zf = self.nf = w"],
    "INC": _RMW_CORE["INC"] + ["self.zf = self.nf = w"],
    "DEC": _RMW_CORE["DEC"] + ["self.zf = self.nf = w"],
    "SLO": _RMW_CORE["ASL"] + ["t = self.a | w", "self.a = self.zf = self.nf = t"],
    "RLA": _RMW_CORE["ROL"] + ["t = self.a & w", "self.a = self.zf = self.nf = t"],
    "SRE": _RMW_CORE["LSR"] + ["t = self.a ^ w", "self.a = self.zf = self.nf = t"],
    "RRA": _RMW_CORE["ROR"] + ["adc(w)"],
    "DCP": _RMW_CORE["DEC"] + ["t = self.a - w", "self.c = 1 if t >= 0 else 0",
                                "self.zf = self.nf = t & 0xFF"],
    "ISC": _RMW_CORE["INC"] + ["sbc(w)"],
}

_PUSH = ["mem[0x100 | self.sp] = {0}", "self.sp = (self.sp - 1) & 0xFF"]
_PULL = ["self.sp = (self.sp + 1) & 0xFF", "{0} = mem[0x100 | self.sp]"]


def _fmt(lines, arg):
    return [line.format(arg) for line in lines]


_BRANCH_COND = {
    "BPL": "not (self.nf & 0x80)", "BMI": "self.nf & 0x80",
    "BVC": "not self.v", "BVS": "self.v",
    "BCC": "not self.c", "BCS": "self.c",
    "BNE": "self.zf", "BEQ": "not self.zf",
}

_IMPLIED = {
    "CLC": ["self.c = 0"], "SEC": ["self.c = 1"],
    "CLI": ["self.i = 0"], "SEI": ["self.i = 1"],
    "CLD": ["self.d = 0"], "SED": ["self.d = 1"],
    "CLV": ["self.v = 0"],
    "TAX": ["self.x = self.zf = self.nf = self.a"],
    "TXA": ["self.a = self.zf = self.nf = self.x"],
    "TAY": ["self.y = self.zf = self.nf = self.a"],
    "TYA": ["self.a = self.zf = self.nf = self.y"],
    "TSX": ["self.x = self.zf = self.nf = self.sp"],
    "TXS": ["self.sp = self.x"],
    "INX": ["self.x = self.zf = self.nf = (self.x + 1) & 0xFF"],
    "INY": ["self.y = self.zf = self.nf = (self.y + 1) & 0xFF"],
    "DEX": ["self.x = self.zf = self.nf = (self.x - 1) & 0xFF"],
    "DEY": ["self.y = self.zf = self.nf = (self.y - 1) & 0xFF"],
    "NOP": [],
    "PHA": _fmt(_PUSH, "self.a"),
    "PHP": _fmt(_PUSH, "self.get_p() | 0x10"),
    "PLA": _fmt(_PULL, "v") + ["self.a = self.zf = self.nf = v"],
    "PLP": _fmt(_PULL, "v") + ["self.set_p(v)"],
    "RTS": _fmt(_PULL, "lo") + _fmt(_PULL, "hi") + [
        "self.pc = pc = (((hi << 8) | lo) + 1) & 0xFFFF",
        "if pc == self.trap: raise _Return"],
    "RTI": _fmt(_PULL, "v") + ["self.set_p(v)"] + _fmt(_PULL, "lo") + _fmt(_PULL, "hi") + [
        "self.pc = pc = (hi << 8) | lo",
        "if pc == self.trap: raise _Return"],
    "BRK": ["raise CPUHalt('BRK at $%04X' % ((self.pc - 1) & 0xFFFF))"],
    "KIL": ["raise CPUHalt('JAM opcode at $%04X' % ((self.pc - 1) & 0xFFFF))"],
}


def _read_value(mode):
    if mode in _ZERO_PAGE:
        return ["v = mem[ea]"]
    return ["v = io_read(ea) if (ea & 0xF000) == 0xD000 else mem[ea]"]


def _write_value(mode, var):
    if mode in _ZERO_PAGE:
        return [f"mem[ea] = {var}"]
    return [f"if (ea & 0xF000) == 0xD000: io_write(ea, {var})", f"else: mem[ea] = {var}"]


def _gen_source(opcode, mnem, mode, cycles):
    body = list(_MODE[mode])
    clock = f"self.clock += {cycles}"
    if mnem in _BRANCH_COND:
        body = [
            "pc = self.pc",
            f"if {_BRANCH_COND[mnem]}:",
            "    off = mem[pc]",
            "    npc = (pc + 1 + (off - 256 if off & 0x80 else off)) & 0xFFFF",
            "    self.pc = npc",
            "    self.clock += 4 if (npc ^ (pc + 1)) & 0xFF00 else 3",
            "else:",
            "    self.pc = (pc + 1) & 0xFFFF",
            "    self.clock += 2",
        ]
        clock = None
    elif mnem == "JMP" and mode == "abs":
        body = ["pc = self.pc", "self.pc = mem[pc] | (mem[(pc + 1) & 0xFFFF] << 8)"]
    elif mnem == "JMP":   # indirect, with the NMOS page-wrap bug
        body = ["pc = self.pc", "ptr = mem[pc] | (mem[(pc + 1) & 0xFFFF] << 8)",
                "self.pc = mem[ptr] | (mem[(ptr & 0xFF00) | ((ptr + 1) & 0xFF)] << 8)"]
    elif mnem == "JSR":
        body = ["pc = self.pc", "target = mem[pc] | (mem[(pc + 1) & 0xFFFF] << 8)",
                "ret = (pc + 1) & 0xFFFF"] + _fmt(_PUSH, "ret >> 8") + _fmt(_PUSH, "ret & 0xFF") + [
                "self.pc = target"]
    elif mode == "imp":
        body = [clock] + _IMPLIED[mnem]     # clock first: RTS/RTI may raise
        clock = None
    elif mode == "acc":
        body = ["v = self.a"] + _RMW_OPS[mnem] + ["self.a = w"]
    elif mnem in _READ_OPS:
        if mode != "imm":
            body += _read_value(mode)
        body += _READ_OPS[mnem]
        if mode in _INDEXED:
            clock = f"self.clock += {cycles} + (((base & 0xFF) + idx) >> 8)"
    elif mnem in _WRITE_OPS:
        body += [clock] + _WRITE_OPS[mnem] + _write_value(mode, "v")
        clock = None
    elif mnem in _RMW_OPS:
        body += _read_value(mode) + [clock] + _RMW_OPS[mnem] + _write_value(mode, "w")
        clock = None
    else:
        raise AssertionError(f"unhandled {mnem} {mode}")
    if clock:
        body.append(clock)
    lines = [f"def op_{opcode:02X}():"] + ["    " + line for line in body]
    if len(lines) == 1:
        lines.append("    pass")
    return "\n".join(lines)


def _parse_table():
    entries = []
    for line in _TABLE.strip().splitlines():
        mnem, rest = line.split(None, 1)
        for part in rest.split("|"):
            mode, opcode, cycles = part.split()
            entries.append((int(opcode, 16), mnem, mode, int(cycles)))
    return entries


_ENTRIES = _parse_table()
assert sorted(e[0] for e in _ENTRIES) == list(range(256)), "opcode table incomplete"

_FACTORY_SRC = "def _factory(self, mem, io_read, io_write, adc, sbc, CPUHalt, _Return):\n"
for _e in _ENTRIES:
    _FACTORY_SRC += "\n".join("    " + line for line in _gen_source(*_e).splitlines()) + "\n"
_FACTORY_SRC += "    return [" + ", ".join(f"op_{i:02X}" for i in range(256)) + "]\n"
_namespace: dict = {}
exec(compile(_FACTORY_SRC, "<6502-opcodes>", "exec"), _namespace)
_factory = _namespace["_factory"]

OPCODES = {e[0]: (e[1], e[2], e[3]) for e in _ENTRIES}


class CPU6502:
    __slots__ = ("mem", "a", "x", "y", "sp", "pc", "c", "zf", "nf", "v", "d", "i",
                 "clock", "trap", "ops")

    def __init__(self, mem: bytearray, io_read, io_write):
        self.mem = mem
        self.a = self.x = self.y = 0
        self.sp = 0xFF
        self.pc = 0
        self.c = self.v = self.d = 0
        self.i = 1
        self.zf = 1
        self.nf = 0
        self.clock = 0          # cycle counter (shared time base with the SID)
        self.trap = -1          # return address that ends call()
        self.ops = _factory(self, mem, io_read, io_write, self._adc, self._sbc,
                            CPUHalt, _Return)

    # ---- status register -----------------------------------------------
    def get_p(self) -> int:
        return ((self.nf & 0x80) | (self.v << 6) | 0x20 | (self.d << 3) | (self.i << 2)
                | (0 if self.zf else 2) | self.c)

    def set_p(self, p: int) -> None:
        self.nf = p & 0x80
        self.v = (p >> 6) & 1
        self.d = (p >> 3) & 1
        self.i = (p >> 2) & 1
        self.zf = 0 if p & 2 else 1
        self.c = p & 1

    p = property(get_p, set_p)

    # ---- arithmetic ------------------------------------------------------
    def _adc(self, m: int) -> None:
        a = self.a
        c = self.c
        if not self.d:
            t = a + m + c
            self.v = ((a ^ t) & (m ^ t) & 0x80) >> 7
            self.c = t >> 8
            self.a = self.zf = self.nf = t & 0xFF
            return
        # NMOS decimal mode (see 6502.org "Decimal Mode" by Bruce Clark)
        self.zf = (a + m + c) & 0xFF
        lo = (a & 0x0F) + (m & 0x0F) + c
        if lo >= 0x0A:
            lo = ((lo + 0x06) & 0x0F) + 0x10
        t = (a & 0xF0) + (m & 0xF0) + lo
        self.nf = t & 0x80
        s = (a & 0xF0) - ((a & 0x80) << 1) + (m & 0xF0) - ((m & 0x80) << 1) + lo
        self.v = 1 if s < -128 or s > 127 else 0
        if t >= 0xA0:
            t += 0x60
        self.c = 1 if t >= 0x100 else 0
        self.a = t & 0xFF

    def _sbc(self, m: int) -> None:
        a = self.a
        c = self.c
        t = a + (m ^ 0xFF) + c          # binary result defines all flags
        self.v = ((a ^ t) & ((m ^ 0xFF) ^ t) & 0x80) >> 7
        self.c = t >> 8
        self.zf = self.nf = t & 0xFF
        if not self.d:
            self.a = t & 0xFF
            return
        lo = (a & 0x0F) - (m & 0x0F) + c - 1
        if lo < 0:
            lo = ((lo - 0x06) & 0x0F) - 0x10
        r = (a & 0xF0) - (m & 0xF0) + lo
        if r < 0:
            r -= 0x60
        self.a = r & 0xFF

    # ---- execution -------------------------------------------------------
    def step(self) -> None:
        pc = self.pc
        self.pc = (pc + 1) & 0xFFFF
        self.ops[self.mem[pc]]()

    def push(self, value: int) -> None:
        self.mem[0x100 | self.sp] = value & 0xFF
        self.sp = (self.sp - 1) & 0xFF

    def call(self, addr: int, max_cycles: int, *, a: int | None = None,
             mode: str = "jsr", trap: int = 0xFFF8) -> bool:
        """Run the routine at `addr` until it returns or `max_cycles` elapse.

        mode "jsr": called like JSR, ends with RTS.
        mode "irq": entered like a hardware IRQ, ends with RTI.
        mode "kernal_irq": like "irq" but A, X, Y are pushed first, as the
        KERNAL entry at $FF48 does before JMP ($0314); such handlers exit
        through $EA31/$EA81, which pull them again before RTI.
        Returns True if the routine returned to the trap address.
        """
        if a is not None:
            self.a = a & 0xFF
        self.trap = trap
        if mode == "jsr":
            self.push((trap - 1) >> 8)
            self.push((trap - 1) & 0xFF)
        else:
            self.push(trap >> 8)
            self.push(trap & 0xFF)
            self.push(self.get_p() & ~0x10)
            self.i = 1
            if mode == "kernal_irq":
                self.push(self.a)
                self.push(self.x)
                self.push(self.y)
        self.pc = addr
        end = self.clock + max_cycles
        mem = self.mem
        ops = self.ops
        try:
            while self.clock < end:
                pc = self.pc
                self.pc = (pc + 1) & 0xFFFF
                ops[mem[pc]]()
        except _Return:
            return True
        return False
