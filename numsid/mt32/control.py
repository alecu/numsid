"""Tables in the control ROM: the factory timbres and the rhythm setup.

A timbre is stored as in the MT-32's SysEx timbre format: 14 common bytes
(the name in 10, the two partial structures, the partial mute bits and the
envelope mode) and then 58 bytes of parameters for each partial.  The old
MT-32 firmware (1.x) stores banks A and B at a fixed 246 bytes per timbre,
all four partials included.  The later firmware (2.x, CM-32L) packs them:
only the partials whose mute bit is set are stored, and runs of FF filler
pad the banks where a record would cross a page.  The rhythm timbres are
packed in every version.

The rhythm setup gives each key from 24 to 108 a rhythm timbre (64 + its
index, or 127 for none), an output level, a pan position (0-14) and a
reverb switch, 4 bytes per key.  It sits in a different place in every
firmware build, so it is found by its shape: eleven empty keys, then the
bass drum on keys 35 and 36.

The factory patch memory is not a table: patch n plays timbre n of bank A
for n < 64 and timbre n - 64 of bank B above that.

These layouts were worked out from the ROM images themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

from .roms import RomInfo, RomError

COMMON = 14
PARTIAL = 58
FIXED = COMMON + 4 * PARTIAL                 # 246: an unpacked timbre
RHYTHM_KEYS = range(24, 109)
NO_TIMBRE = 127

# where banks A and the rhythm timbres start, per firmware family
LAYOUT = {
    "MT-32":     dict(a=0x8080, b=0xC080, packed=False, rhythm=0x323C, rhythm_timbres=30),
    "MT-32 new": dict(a=0x9500, b=None, packed=True, rhythm=0x8A00, rhythm_timbres=30),
    "CM-32L":    dict(a=0xB000, b=None, packed=True, rhythm=0x8A00, rhythm_timbres=63),
}


@dataclass
class Timbre:
    name: str
    common: bytes            # the 14 common bytes
    partials: list           # 58-byte parameter blocks, None for a muted partial

    @property
    def structure(self) -> tuple[int, int]:
        """How partials 1+2 and 3+4 combine: 1-13 each."""
        return self.common[10] + 1, self.common[11] + 1

    @property
    def mute(self) -> int:
        """Bit i set: partial i+1 plays."""
        return self.common[12] & 0x0F

    @property
    def sustain(self) -> bool:
        """False for "no sustain" timbres, which play each note out (drums)."""
        return not self.common[13] & 1

    @property
    def active(self) -> int:
        return bin(self.mute).count("1")


@dataclass
class RhythmKey:
    timbre: int              # index into the rhythm timbres, or None
    level: int               # 0-100
    pan: int                 # 0 (left) - 14 (right)
    reverb: bool


def _timbre(blob: bytes, at: int, packed: bool) -> tuple[Timbre, int]:
    """The timbre at blob[at]: (timbre, offset after it)."""
    if packed:
        while blob[at] == 0xFF:              # page filler
            at += 1
    common = bytes(blob[at:at + COMMON])
    name = common[:10]
    if not all(32 <= c < 127 for c in name):
        raise RomError(f"no timbre at {at:#x}")
    mute = common[12] & 0x0F
    partials, p = [], at + COMMON
    for i in range(4):
        if mute >> i & 1 or not packed:
            partials.append(bytes(blob[p:p + PARTIAL]) if mute >> i & 1 else None)
            p += PARTIAL
        else:
            partials.append(None)
    return Timbre(name.decode("ascii").rstrip(), common, partials), p


def _bank(blob: bytes, at: int, count: int, packed: bool) -> tuple[list[Timbre], int]:
    out = []
    for _ in range(count):
        t, at = _timbre(blob, at, packed)
        out.append(t)
    return out, at


def _rhythm_setup(blob: bytes) -> int:
    empty, bass = bytes([NO_TIMBRE, 100, 7]), bytes([64, 100, 7])
    i = 0
    while (i := blob.find(bass, i + 1)) >= 0:
        if blob[i + 4:i + 7] == bass and all(blob[i - 4 * k:i - 4 * k + 3] == empty
                                             for k in range(1, 12)):
            return i - 44
    raise RomError("rhythm setup not found in the control ROM")


@dataclass
class ControlROM:
    info: RomInfo
    bank_a: list[Timbre]
    bank_b: list[Timbre]
    rhythm_timbres: list[Timbre]
    rhythm: dict[int, RhythmKey]             # key -> setup

    def patch(self, program: int) -> Timbre:
        """The timbre a program change selects with the factory patches."""
        return self.bank_a[program] if program < 64 else self.bank_b[program - 64]


def parse(blob: bytes, info: RomInfo) -> ControlROM:
    lay = LAYOUT[info.model]
    a, end = _bank(blob, lay["a"], 64, lay["packed"])
    b, _ = _bank(blob, lay["b"] or end, 64, lay["packed"])
    r, _ = _bank(blob, lay["rhythm"], lay["rhythm_timbres"], True)
    at = _rhythm_setup(blob)
    rhythm = {}
    for k, key in enumerate(RHYTHM_KEYS):
        t, level, pan, reverb = blob[at + 4 * k:at + 4 * k + 4]
        rhythm[key] = RhythmKey(t - 64 if 64 <= t < 64 + len(r) else None, level, pan,
                                bool(reverb))
    return ControlROM(info, a, b, r, rhythm)


def factory_patch_names(directory=None) -> list[str] | None:
    """The 128 factory patch names from the MT-32 control ROM, or None
    when no ROM is found."""
    from . import roms
    try:
        info, blob, _, _ = roms.find("MT-32", directory)
    except RomError:
        return None
    rom = parse(blob, info)
    return [rom.patch(p).name for p in range(128)]
