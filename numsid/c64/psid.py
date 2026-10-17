"""PSID / RSID file parser.

Format reference: the HVSC "SID_file_format.txt" (versions 1-4).
All header words are big-endian; the optional embedded load address
(when the header's load address is 0) is little-endian, C64 style.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

PAL_CLOCK = 985248     # Hz, PAL C64 CPU/SID clock
NTSC_CLOCK = 1022727   # Hz, NTSC C64 CPU/SID clock

PAL_CYCLES_PER_FRAME = 63 * 312    # 19656 -> 50.125 Hz
NTSC_CYCLES_PER_FRAME = 65 * 263   # 17095 -> 59.826 Hz


class SIDFileError(ValueError):
    pass


@dataclass
class PSIDFile:
    magic: str                 # "PSID" or "RSID"
    version: int
    load_address: int
    init_address: int
    play_address: int          # 0 = the tune installs its own IRQ handler
    songs: int
    start_song: int            # 1-based
    speed: int                 # bit n set -> song n+1 uses CIA timing
    name: str
    author: str
    released: str
    flags: int = 0
    start_page: int = 0
    page_length: int = 0
    data: bytes = field(default=b"", repr=False)

    # ---- decoded flags -------------------------------------------------
    @property
    def is_rsid(self) -> bool:
        return self.magic == "RSID"

    @property
    def clock_flag(self) -> int:
        """0 = unknown, 1 = PAL, 2 = NTSC, 3 = both."""
        return (self.flags >> 2) & 3

    @property
    def sid_model_flag(self) -> int:
        """0 = unknown, 1 = 6581, 2 = 8580, 3 = both."""
        return (self.flags >> 4) & 3

    @property
    def is_pal(self) -> bool:
        return self.clock_flag != 2

    @property
    def clock(self) -> int:
        return PAL_CLOCK if self.is_pal else NTSC_CLOCK

    @property
    def preferred_model(self) -> str:
        return "8580" if self.sid_model_flag == 2 else "6581"

    @property
    def end_address(self) -> int:
        return self.load_address + len(self.data)

    def uses_cia(self, song: int) -> bool:
        """True if (1-based) `song` is timed by CIA 1 timer A instead of VBI."""
        bit = min(song - 1, 31)
        return bool((self.speed >> bit) & 1)

    def describe(self) -> str:
        model = {0: "unknown", 1: "6581", 2: "8580", 3: "6581/8580"}[self.sid_model_flag]
        clock = {0: "unknown", 1: "PAL", 2: "NTSC", 3: "PAL/NTSC"}[self.clock_flag]
        return (
            f"{self.name} - {self.author} ({self.released})\n"
            f"{self.magic} v{self.version}  load ${self.load_address:04X}-${self.end_address - 1:04X}  "
            f"init ${self.init_address:04X}  play ${self.play_address:04X}\n"
            f"songs {self.songs} (start {self.start_song})  clock {clock}  SID {model}"
        )


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


def parse(blob: bytes) -> PSIDFile:
    if len(blob) < 0x76:
        raise SIDFileError("file too short for a PSID header")
    magic = blob[0:4].decode("latin-1")
    if magic not in ("PSID", "RSID"):
        raise SIDFileError(f"bad magic {magic!r}")

    (version, data_offset, load, init, play, songs, start, speed) = struct.unpack(
        ">HHHHHHHI", blob[4:0x16])
    name, author, released = (_text(blob[o:o + 32]) for o in (0x16, 0x36, 0x56))

    flags = start_page = page_length = 0
    if version >= 2 and len(blob) >= 0x7C:
        flags, start_page, page_length = struct.unpack(">HBB", blob[0x76:0x7A])

    payload = blob[data_offset:]
    if load == 0:
        if len(payload) < 2:
            raise SIDFileError("missing embedded load address")
        load = payload[0] | (payload[1] << 8)
        payload = payload[2:]
    if load + len(payload) > 0x10000:
        raise SIDFileError("tune data runs past $FFFF")
    if init == 0:
        init = load

    return PSIDFile(magic, version, load, init, play, max(songs, 1), max(start, 1),
                    speed, name, author, released, flags, start_page, page_length,
                    bytes(payload))


def load(path: str | Path) -> PSIDFile:
    return parse(Path(path).read_bytes())
