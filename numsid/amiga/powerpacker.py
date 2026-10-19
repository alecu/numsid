"""PowerPacker ("PP20") decompression.

Many Amiga modules circulate crunched with Nico François' PowerPacker.  A
PP20 file is the magic, four "efficiency" bytes (the offset widths for the
four match classes), the compressed bit stream, and a trailer of four bytes:
the decrunched length (24-bit big-endian) and how many bits of padding to
skip.  The stream is read backwards from the end, each byte's bits taken
least significant first, and the output is likewise written from its last
byte towards its first: literal runs, and matches that copy bytes already
written (which lie *after* the write position).
"""

from __future__ import annotations

import numpy as np

MAGIC = b"PP20"


class PowerPackerError(ValueError):
    pass


def is_crunched(blob: bytes) -> bool:
    return blob[:4] == MAGIC


def decrunch(blob: bytes) -> bytes:
    if not is_crunched(blob) or len(blob) < 12:
        raise PowerPackerError("not a PowerPacker PP20 file")
    efficiency = blob[4:8]
    if any(not 1 <= e <= 16 for e in efficiency):
        raise PowerPackerError("bad PowerPacker efficiency table")
    size = int.from_bytes(blob[-4:-1], "big")
    skip = blob[-1]
    if not size:
        raise PowerPackerError("PowerPacker file decrunches to nothing")

    # The bit stream in reading order, as ASCII '0'/'1' so that a field of k
    # bits is one int(..., 2) on a slice.
    bits = np.unpackbits(np.frombuffer(blob[8:-4][::-1], np.uint8), bitorder="little")
    stream = (bits + ord("0")).astype(np.uint8).tobytes()
    end = len(stream)
    pos = skip

    def read(k: int) -> int:
        nonlocal pos
        if pos + k > end:
            raise PowerPackerError("PowerPacker data is truncated")
        v = int(stream[pos:pos + k], 2) if k else 0
        pos += k
        return v

    out = bytearray(size)
    o = size                                    # next byte goes to out[o - 1]
    while o > 0:
        if not read(1):                         # a literal run, then a match
            todo = 1
            while True:
                x = read(2)
                todo += x
                if x != 3:
                    break
            if todo > o:
                raise PowerPackerError("PowerPacker literal run overflows the output")
            for _ in range(todo):
                o -= 1
                out[o] = read(8)
            if o == 0:
                break
        x = read(2)
        offbits = efficiency[x]
        todo = x + 2
        if x == 3:
            if not read(1):
                offbits = 7
            offset = read(offbits)
            while True:
                x = read(3)
                todo += x
                if x != 7:
                    break
        else:
            offset = read(offbits)
        src = o + offset                        # the byte copied first
        if src >= size or todo > o:
            raise PowerPackerError("PowerPacker match points outside the output")
        if todo <= offset + 1:                  # source and target don't overlap
            out[o - todo:o] = out[src - todo + 1:src + 1]
            o -= todo
        else:
            for _ in range(todo):
                o -= 1
                out[o] = out[o + offset + 1]
    return bytes(out)
