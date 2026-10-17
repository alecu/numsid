"""The PCM sample ROM and the table of waves that points into it.

Samples are 16-bit big-endian words in a logarithmic sign-magnitude
format: bit 15 is the sign, bits 14-8 the integer part of the log
magnitude and bits 5-0 its fraction, at 16 steps per octave, so the
range is 8 octaves (48 dB).  Bit 7 is always clear and bit 6 is set in
nine words out of ten; neither is part of the value.  The scale was found
by decoding with each candidate step size and keeping the one that gives
the smoothest waveforms (16 per octave, clearly ahead of 12 or 20).

The control ROM holds the wave table: 4 bytes per wave, 128 waves on the
MT-32 and 256 on the CM-32L (whose second bank is the sound effects in
the upper half of its 1 MB ROM).

  byte 0    start address, in 2048-word blocks
  byte 1    bit 7: looped; bits 6-4: length is 2048 << n words; bit 0: (not
            yet known)
  bytes 2-3 little-endian pitch: 0x5000 means the wave plays middle C at
            the 32 kHz sample rate, and each 0x1000 below that raises the
            wave's own pitch by an octave (so it must be slowed down)

The pitch rule was found by measuring the period of the looped waves: the
native frequency is 2^(13.03 - pitch/4096) Hz in every case checked.
These layouts were worked out from the ROM images themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .roms import RomError, RomInfo

BLOCK = 2048                 # words
SAMPLE_RATE = 32000
MIDDLE_C_PITCH = 0x5000

# table position, number of waves, per firmware family
TABLE = {"MT-32": (0x3000, 128), "MT-32 new": (0x8100, 128), "CM-32L": (0x8100, 256)}


@dataclass(frozen=True)
class Wave:
    start: int               # word address in the PCM ROM
    length: int              # words
    looped: bool
    pitch: int               # see the module docstring
    flag: bool               # byte 1 bit 0, meaning unknown

    @property
    def native_hz(self) -> float:
        """Frequency the wave sounds at when played at 32 kHz, for the
        looped waves whose loop is one or a few cycles."""
        return 261.6256 * 2.0 ** ((MIDDLE_C_PITCH - self.pitch) / 4096)


def decode(words: np.ndarray) -> np.ndarray:
    """Log sign-magnitude words -> float32 in [-1, 1].  The scale is offset
    so that the all-zero word, which fills the gaps between waves, is
    silence rather than a -48 dB DC level."""
    w = words.astype(np.int32)
    log = ((w >> 8) & 0x7F) + (w & 0x3F) / 64.0
    mag = (np.exp2(log / 16.0) - 1.0) / 255.0
    return np.where(w & 0x8000, -mag, mag).astype(np.float32)


def wave_table(control: bytes, info: RomInfo) -> list[Wave]:
    at, count = TABLE[info.model]
    if control[at:at + 4] != b"\x10\x10\x00\x50":
        raise RomError(f"PCM wave table not found at {at:#x}")
    out = []
    for k in range(count):
        a, b, lo, hi = control[at + 4 * k:at + 4 * k + 4]
        out.append(Wave(a * BLOCK, BLOCK << ((b >> 4) & 7), bool(b & 0x80), hi << 8 | lo,
                        bool(b & 1)))
    return out


class PCMROM:
    """The decoded sample ROM and its waves."""

    def __init__(self, pcm: bytes, control: bytes, info: RomInfo):
        self.samples = decode(np.frombuffer(pcm, dtype=">u2"))
        self.waves = wave_table(control, info)

    def wave(self, number: int) -> np.ndarray:
        w = self.waves[number % len(self.waves)]
        return self.samples[w.start:w.start + w.length]
