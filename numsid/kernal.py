"""A tiny replacement for the C64 KERNAL ROM ($E000-$FFFF).

The Commodore ROMs can't be distributed, and a SID player needs only a
handful of their entry points: the interrupt vectors and handlers, the
jiffy clock, the vector-restore routine, and harmless stand-ins for
everything else.  Unimplemented space is filled with RTS, so a call into
a routine we don't provide simply returns.

Entry points (real KERNAL addresses):

  $FF48  IRQ/BRK entry: push A, X, Y; JMP ($0316) on BRK else JMP ($0314)
  $EA31  default IRQ handler: update jiffy clock, scan keyboard (no-op),
         then fall through a NOP sled into ...
  $EA7E  acknowledge CIA 1 (LDA $DC0D), then
  $EA81  pull Y, X, A and RTI
  $FE43  NMI entry: SEI; JMP ($0318)
  $FE47  default NMI handler: acknowledge CIA 2, pull registers, RTI
  $FEBC  pull Y, X, A and RTI
  $FE66  BRK default: return from the interrupt
  $F69B  UDTIM: increment the jiffy clock at $A0-$A2
  $FD15  RESTOR: copy the default vectors to $0314-$0333
  $FF81-$FFF3  jump table into the above (or RTS)
"""

from __future__ import annotations

BASE = 0xE000

# default contents of $0314-$0333 (IRQ, BRK, NMI, then I/O vectors)
DEFAULT_VECTORS = [0xEA31, 0xFE66, 0xFE47] + [0xEA87] * 13

_ROUTINES = {
    # IRQ handler path; the sled lets tunes jump anywhere in $EA31-$EA7E
    0xEA31: [0x20, 0x9B, 0xF6,             # JSR UDTIM
             0x20, 0x87, 0xEA],            # JSR SCNKEY (RTS)
    0xEA7E: [0xAD, 0x0D, 0xDC],            # LDA $DC0D
    0xEA81: [0x68, 0xA8, 0x68, 0xAA, 0x68, 0x40],   # PLA TAY PLA TAX PLA RTI
    0xEA87: [0x60],                        # SCNKEY: nothing to scan
    # NMI
    0xFE43: [0x78, 0x6C, 0x18, 0x03],      # SEI : JMP ($0318)
    0xFE47: [0x48, 0x8A, 0x48, 0x98, 0x48, # PHA TXA PHA TYA PHA
             0xAD, 0x0D, 0xDD,             # LDA $DD0D (acknowledge)
             0x4C, 0xBC, 0xFE],            # JMP $FEBC
    0xFE66: [0x4C, 0x81, 0xEA],            # BRK: JMP $EA81
    0xFEBC: [0x68, 0xA8, 0x68, 0xAA, 0x68, 0x40],
    # IRQ/BRK entry
    0xFF48: [0x48, 0x8A, 0x48, 0x98, 0x48, # PHA TXA PHA TYA PHA
             0xBA, 0xBD, 0x04, 0x01,       # TSX : LDA $0104,X  (pushed P)
             0x29, 0x10,                   # AND #$10 (B flag)
             0xF0, 0x03,                   # BEQ +3
             0x6C, 0x16, 0x03,             # JMP ($0316)
             0x6C, 0x14, 0x03],            # JMP ($0314)
    # UDTIM: jiffy clock
    0xF69B: [0xE6, 0xA2, 0xD0, 0x06,       # INC $A2 : BNE done
             0xE6, 0xA1, 0xD0, 0x02,       # INC $A1 : BNE done
             0xE6, 0xA0,                   # INC $A0
             0x60],                        # done: RTS
    # RESTOR: copy 32 bytes of default vectors to $0314
    0xFD15: [0xA2, 0x1F,                   # LDX #$1F
             0xBD, 0x30, 0xFD,             # LDA $FD30,X
             0x9D, 0x14, 0x03,             # STA $0314,X
             0xCA, 0x10, 0xF7,             # DEX : BPL
             0x60],
    0xFD30: [b for v in DEFAULT_VECTORS for b in (v & 0xFF, v >> 8)],
    # GETIN / STOP stand-ins: no key, stop not pressed
    0xF13E: [0xA9, 0x00, 0x18, 0x60],      # LDA #0 : CLC : RTS
    0xF6ED: [0xA9, 0x01, 0x60],            # LDA #1 : RTS (Z clear)
}

# KERNAL jump table entries that go somewhere other than an RTS
_JUMPS = {0xFF8A: 0xFD15, 0xFF9F: 0xEA87, 0xFFE1: 0xF6ED, 0xFFE4: 0xF13E,
          0xFFEA: 0xF69B}


def build_kernal() -> bytes:
    rom = bytearray([0x60]) * 0x2000                  # RTS everywhere
    rom[0xEA37 - BASE:0xEA7E - BASE] = bytes([0xEA]) * (0xEA7E - 0xEA37)   # NOP sled
    for addr, code in _ROUTINES.items():
        rom[addr - BASE:addr - BASE + len(code)] = bytes(code)
    for addr in range(0xFF81, 0xFFF4, 3):             # the jump table
        target = _JUMPS.get(addr, 0xEA87)
        rom[addr - BASE:addr - BASE + 3] = bytes([0x4C, target & 0xFF, target >> 8])
    for vec, target in ((0xFFFA, 0xFE43), (0xFFFC, 0xFCE2), (0xFFFE, 0xFF48)):
        rom[vec - BASE] = target & 0xFF
        rom[vec - BASE + 1] = target >> 8
    return bytes(rom)


KERNAL = build_kernal()
