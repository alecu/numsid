"""OctaMED module (MMD0..MMD3) parser.

MED and OctaMED save "MMD" files: a 52-byte header of offsets, a song
structure, blocks (patterns, of any number of lines and tracks) and
instruments.  All numbers are big-endian and every structure is reached
through an absolute file offset.  MMD0 packs a note into 3 bytes (6-bit
note, 6-bit instrument, 4-bit command, 8-bit parameter); MMD1 and later use
4 bytes with 8-bit commands, and a block can carry extra command pages,
more effect columns.  MMD2 and MMD3 replace the single play sequence with
sections and play sequences, which are flattened here into one list of
blocks.

1-octave samples play as they are; the multi-octave IFF instruments hold
one sample per octave, each twice the length of the one before, and the
replayer picks the right one for a note.  16-bit, stereo and delta-coded
samples are reduced to what Paula can play (8-bit mono).  Synthetic
instruments are a set of short looped waveforms with two little programs,
a volume table and a waveform table, that the replayer steps through; a
hybrid is the same with a full sample as its first waveform.  Packed
samples and MIDI are not supported: their notes stay silent and the file
gets a warning.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .modfile import BLANK, ModFile, ModFileError, NotAModError, Sample

MAX_DMA_BYTES = 2 * 0xFFFF       # the longest block Paula plays in one go

# Instrument types (InstrHdr.type, low nibble) and how many octave samples each holds
SAMPLE, SYNTHETIC, HYBRID = 0, -1, -2
OCTAVE_SAMPLES = {0: 1, 1: 5, 2: 3, 3: 2, 4: 4, 5: 6, 6: 7}
S_16, STEREO, DELTA, PACKED = 0x10, 0x20, 0x40, 0x80

# For the multi-octave instruments, per MED octave (1, 2, ...): which of the
# octave samples plays (0 is the shortest) and the transpose it is played
# with, so that every octave lands in the range Paula plays well.  These are
# the maps libopenmpt uses, after OctaMED.
OCTAVE_MAP = {
    2: ([1, 1, 0, 0, 0, 0, 0, 0], [0, 0, -12, -12, -24, -36, -48, -60]),
    3: ([2, 2, 1, 1, 0, 0, 0, 0], [0, 0, -12, -12, -24, -36, -48, -60]),
    4: ([3, 3, 2, 2, 1, 0, 0, 0], [0, 0, -12, -12, -24, -36, -48, -60]),
    5: ([4, 3, 2, 1, 1, 0, 0, 0], [12, 0, -12, -24, -24, -36, -48, -60]),
    6: ([5, 4, 3, 2, 1, 0, 0, 0], [12, 0, -12, -24, -36, -48, -48, -60]),
    7: ([6, 5, 4, 3, 2, 1, 0, 0], [12, 0, -12, -24, -36, -48, -60, -72]),
}

# MMDSong.flags / flags2
FLAG_FILTERON, FLAG_VOLHEX, FLAG_STSLIDE, FLAG_8CHANNEL = 0x01, 0x10, 0x20, 0x40
FLAG2_BMASK, FLAG2_BPM, FLAG2_MIX = 0x1F, 0x20, 0x80


@dataclass
class MedSample(Sample):
    """A MED instrument as Paula plays it.  `octaves` lists (address,
    length, loop start, loop length) in bytes for each octave sample of a
    multi-octave instrument, shortest first; a 1-octave sample has one."""
    transpose: int = 0           # semitones added to every note
    hold: int = 0                # ticks at full volume (0: no hold/decay envelope)
    decay: int = 0               # volume units lost per tick after the hold
    octaves: list = field(default_factory=list)
    kind: str = "sample"
    # synthetic and hybrid instruments
    waveforms: list = field(default_factory=list)       # (address, length) in bytes
    volume_table: bytes = b""
    waveform_table: bytes = b""
    volume_speed: int = 1
    waveform_speed: int = 1


@dataclass
class MedFile(ModFile):
    """A MED song.  `patterns` holds the blocks: [block][line][track] ->
    (note, instrument, command, parameter), with lines and tracks as
    the block has them (missing tracks read as empty)."""
    version: int = 0             # 0..3 for MMD0..MMD3
    tempo: int = 33              # initial tempo, in the units the flags select
    speed: int = 6               # ticks per line ("secondary tempo")
    flags: int = 0
    flags2: int = 0
    transpose: int = 0           # song play transpose, semitones
    track_volumes: list = field(default_factory=list)   # 0..64 per track
    master_volume: int = 64
    # per block: saved by MED SoundStudio with a command extension table,
    # which means 7-bit volumes and notes that don't wrap down 9 octaves
    soundstudio: list = field(default_factory=list)
    # extra command pages (more effect columns): {block: [page][line][track] -> (command, parameter)}
    pages: dict = field(default_factory=dict)

    magic = "MED"

    @property
    def kind(self) -> str:
        return {0: "MED", 1: "OctaMED", 2: "OctaMED", 3: "OctaMED SoundStudio"}[self.version]

    @property
    def hex_volumes(self) -> bool:
        return bool(self.flags & FLAG_VOLHEX)

    @property
    def st_slides(self) -> bool:
        """SoundTracker-style slides: none on a line's first tick."""
        return bool(self.flags & FLAG_STSLIDE)

    @property
    def bpm_mode(self) -> bool:
        return bool(self.flags2 & FLAG2_BPM)

    @property
    def lines_per_beat(self) -> int:
        return 1 + (self.flags2 & FLAG2_BMASK)

    def describe(self) -> str:
        used = [(i, s) for i, s in enumerate(self.samples, 1) if s.length or s.kind != "sample"]
        lines = [f"{self.name} ({Path(self.filename).name})" if self.filename else self.name,
                 f"{self.kind} module ({self.tag}), {len(used)} instruments, "
                 f"{self.channels} tracks: {len(self.orders)} positions, "
                 f"{len(self.patterns)} blocks, {len(self.ram)} bytes of samples"]
        for i, s in used:
            extra = f"  loop {s.loop_start}+{s.loop_length}" if s.looped else ""
            if s.kind != "sample":
                extra += f"  [{s.kind}]"
            lines.append(f"  {i:2d} {s.name:22.22s} {s.length:7d}  vol {s.volume:2d}{extra}")
        return "\n".join(lines)


def is_med(blob: bytes) -> bool:
    return blob[:3] == b"MMD" and blob[3:4] in (b"0", b"1", b"2", b"3")


def parse(blob: bytes, filename: str = "") -> MedFile:
    if not is_med(blob):
        raise NotAModError("not an MMD file")
    version = blob[3] - ord("0")
    size = len(blob)

    def u8(o): return blob[o] if 0 <= o < size else _short()
    def u16(o): return struct.unpack_from(">H", blob, o)[0] if 0 <= o <= size - 2 else _short()
    def u32(o): return struct.unpack_from(">I", blob, o)[0] if 0 <= o <= size - 4 else _short()

    song = u32(8)
    block_array, sample_array, exp = u32(16), u32(24), u32(32)
    warnings: list[str] = []

    # ---- song --------------------------------------------------------------------
    info = [struct.unpack_from(">HHBBBb", blob, song + 8 * i) for i in range(63)] \
        if song + 504 <= size else _short()
    s = song + 504
    nblocks, songlen = u16(s), u16(s + 2)
    tempo, transpose, flags, flags2, speed = u16(s + 260), struct.unpack_from(">b", blob, s + 262)[0], \
        u8(s + 263), u8(s + 264), u8(s + 265)
    track_volumes = [min(v, 64) for v in blob[s + 266:s + 282]]
    master = min(u8(s + 282), 64) or 64
    nsamples = u8(s + 283)
    if nsamples > 63 or nblocks > 0x7FFF:
        raise ModFileError("damaged MMD song header")

    if version < 2:
        orders = list(blob[s + 4:s + 4 + min(songlen, 256)])
    else:
        playseqs, sections, trackvols = u32(s + 4), u32(s + 8), u32(s + 12)
        ntracks, npseqs = u16(s + 16), u16(s + 18)
        if trackvols:
            track_volumes = [min(v, 64) for v in blob[trackvols:trackvols + ntracks]]
        orders = []
        for i in range(songlen):
            sec = u16(sections + 2 * i)
            if sec >= npseqs:
                continue
            ps = u32(playseqs + 4 * sec)
            for j in range(u16(ps + 40)):
                b = u16(ps + 42 + 2 * j)
                if b < 0x8000:
                    orders.append(b)
            if u32(ps + 32):
                warnings.append("play sequence jump/stop commands are ignored")
    orders = [b for b in orders if b < nblocks]
    if not orders:
        raise ModFileError("the song has no blocks to play")

    # ---- blocks ------------------------------------------------------------------
    blocks, soundstudio, pages = [], [], {}
    for b in range(nblocks):
        o = u32(block_array + 4 * b)
        extended = False
        if version == 0:
            tracks, lines, data, width = u8(o), u8(o + 1) + 1, o + 2, 3
        else:
            tracks, lines, data, width = u16(o), u16(o + 2) + 1, o + 8, 4
            binfo = u32(o + 4)
            if binfo and binfo + 20 <= size:
                page_table, ext_table = u32(binfo + 12), u32(binfo + 16)
                extended = bool(ext_table) and ext_table + 4 <= size and 0 < u32(ext_table) < size
                if page_table and page_table + 4 <= size:
                    count = u16(page_table)
                    for k in range(count):
                        po = u32(page_table + 4 + 4 * k)
                        if not po or po + 2 * lines * tracks > size:
                            continue
                        cells = struct.unpack_from(f">{2 * lines * tracks}B", blob, po)
                        pages.setdefault(b, []).append(
                            [[(cells[2 * (line * tracks + t)], cells[2 * (line * tracks + t) + 1])
                              for t in range(tracks)] for line in range(lines)])
        soundstudio.append(extended)
        if data + lines * tracks * width > size:
            raise ModFileError(f"block {b} runs past the end of the file")
        rows = []
        for line in range(lines):
            p = data + line * tracks * width
            if width == 3:
                rows.append([((c0 & 0x3F),
                              (c1 >> 4) | (c0 & 0x80) >> 3 | (c0 & 0x40) >> 1,
                              c1 & 0x0F, c2)
                             for c0, c1, c2 in struct.iter_unpack("3B", blob[p:p + 3 * tracks])])
            else:
                rows.append([(n & 0x7F if n != 0x80 else 0x80, i & 0x3F, c, v)
                             for n, i, c, v in struct.iter_unpack("4B", blob[p:p + 4 * tracks])])
        blocks.append(rows)
    channels = max([4] + [len(rows[0]) for rows in blocks if rows])

    # ---- instruments -----------------------------------------------------------------
    names = [""] * 63
    ext = [(0, 0, 0, None, None, None, None)] * 63         # hold, decay, finetune, flags, loop
    song_name = ""
    if exp and exp + 52 <= size:
        exp_smp, n_ext, ext_size = u32(exp + 4), u16(exp + 8), u16(exp + 10)
        iinfo, n_info, info_size = u32(exp + 20), u16(exp + 24), u16(exp + 26)
        if exp_smp and ext_size >= 4:
            ext = []
            for i in range(63):
                if i < n_ext and exp_smp + (i + 1) * ext_size <= size:
                    e = blob[exp_smp + i * ext_size:exp_smp + (i + 1) * ext_size]
                    finetune = struct.unpack_from(">b", e, 3)[0]
                    iflags = e[5] if ext_size > 5 else None
                    loop = struct.unpack_from(">II", e, 10) if ext_size >= 18 else None
                    ext.append((e[0], e[1], finetune, iflags, loop, None, None))
                else:
                    ext.append((0, 0, 0, None, None, None, None))
        if iinfo and info_size:
            for i in range(min(n_info, 63)):
                raw = blob[iinfo + i * info_size:iinfo + i * info_size + min(info_size, 40)]
                names[i] = raw.split(b"\0")[0].decode("latin-1").strip()
        name_off, name_len = u32(exp + 44), u32(exp + 48)
        if name_off and name_len:
            song_name = blob[name_off:name_off + name_len].split(b"\0")[0].decode("latin-1").strip()

    offsets = [u32(sample_array + 4 * i) if sample_array else 0 for i in range(nsamples)]
    ram = bytearray(4)                                      # BLANK: a silent word
    samples: list[MedSample] = []
    skipped = set()
    long_samples = False
    for i in range(63):
        rep, replen, midi_ch, _, svol, strans = info[i]
        hold, decay, finetune, iflags, ext_loop = ext[i][:5]
        loop_start, loop_length = 2 * rep, 2 * replen if replen > 1 else 2
        if ext_loop and (ext_loop[0] or ext_loop[1]):
            loop_start, loop_length = ext_loop
        if iflags is not None and not iflags & 0x01:
            loop_length = 2                                 # loop switched off
        smp = MedSample(names[i], 0, finetune & 15, min(svol, 64), 0, 2,
                        transpose=strans, hold=hold, decay=decay)
        off = offsets[i] if i < nsamples else 0
        if midi_ch:
            smp.kind = "MIDI"
            skipped.add("MIDI")
        elif off:
            length, kind = u32(off), struct.unpack_from(">h", blob, off + 4)[0]
            data = blob[off + 6:off + 6 + length]
            if kind == SYNTHETIC or kind == HYBRID:
                smp.kind = "synth" if kind == SYNTHETIC else "hybrid"
                ram = _load_synth(blob, off, smp, ram, (loop_start, loop_length))
                if not smp.octaves:
                    skipped.add(f"damaged {smp.kind} instruments")
            elif kind < 0 or kind & PACKED or (kind & 0x0F) not in OCTAVE_SAMPLES:
                smp.kind = "unsupported"
                skipped.add(f"instrument type {kind}")
            else:
                if kind & STEREO:
                    data = data[:len(data) // 2]                # the left channel
                pcm = _to_8bit(data, bool(kind & S_16), bool(kind & DELTA))
                n = OCTAVE_SAMPLES[kind & 0x0F]
                unit = len(pcm) // ((1 << n) - 1)
                pos = 0
                for octave in range(n):
                    part = pcm[pos:pos + (unit << octave)]
                    pos += unit << octave
                    ls, ll = loop_start << octave, loop_length << octave if loop_length > 2 else 2
                    if ll > 2 and ls + ll > len(part):
                        ll = max(len(part) - ls, 0) & ~1
                        if ll <= 2:
                            ls, ll = 0, 2
                    if len(part) > MAX_DMA_BYTES or ls + ll > MAX_DMA_BYTES:
                        long_samples = True
                    address = len(ram)
                    ram += part
                    if len(part) & 1:
                        ram += b"\0"
                    smp.octaves.append((address, len(part), ls, ll))
                address, smp.length, smp.loop_start, smp.loop_length = smp.octaves[0]
                smp.address = address
                if n > 1:
                    smp.kind = f"{n}-octave"
        samples.append(smp)
    ram += bytes(4)
    if skipped:
        warnings.append("not supported, left silent: " + ", ".join(sorted(skipped)))
    if long_samples:
        warnings.append("samples over 128 KB are cut short, as Paula's DMA is")

    title = song_name or Path(filename).stem
    return MedFile(title, f"MMD{version}", channels, samples, orders, blocks, bytes(ram),
                   filename=filename, warnings=warnings, version=version, tempo=tempo,
                   speed=speed, flags=flags, flags2=flags2, transpose=transpose,
                   track_volumes=(track_volumes + [64] * channels)[:channels],
                   master_volume=master, soundstudio=soundstudio, pages=pages)


def _load_synth(blob: bytes, off: int, smp: MedSample, ram: bytearray, loop) -> bytearray:
    """Read a synthetic or hybrid instrument's tables and waveforms into
    `smp`, appending the waveforms to the RAM image."""
    synth = off + 6                                         # after the InstrHdr
    if synth + 272 > len(blob):
        return ram
    vol_len, wave_len, vol_speed, wave_speed, count = struct.unpack_from(">HHBBH", blob, synth + 8)
    if vol_len > 128 or wave_len > 128 or count > 64:
        return ram
    smp.volume_table = blob[synth + 16:synth + 16 + vol_len]
    smp.waveform_table = blob[synth + 144:synth + 144 + wave_len]
    smp.volume_speed, smp.waveform_speed = vol_speed, wave_speed
    if synth + 272 + 4 * count > len(blob):
        return ram
    for i, rel in enumerate(struct.unpack_from(f">{count}I", blob, synth + 272)):
        pos = off + rel
        if rel <= 6 + 272 or pos + 6 > len(blob):
            smp.waveforms.append((BLANK, 2))
            continue
        if smp.kind == "hybrid" and i == 0:                 # a whole sample
            length = struct.unpack_from(">I", blob, pos)[0]
            data = blob[pos + 6:pos + 6 + length]
        else:
            length = 2 * struct.unpack_from(">H", blob, pos)[0]
            data = blob[pos + 2:pos + 2 + length]
        address = len(ram)
        ram += data
        if len(data) & 1:
            ram += b"\0"
        smp.waveforms.append((address, len(data)))
    if smp.waveforms and smp.waveforms[0][1] >= 2:
        address, length = smp.waveforms[0]
        ls, ll = (0, length & ~1) if smp.kind == "synth" else loop
        if smp.kind == "hybrid" and (ll <= 2 or ls + ll > length):
            ls, ll = 0, 2
        smp.octaves = [(address, length, ls, ll)]
        smp.address, smp.length, smp.loop_start, smp.loop_length = smp.octaves[0]
    return ram


def _to_8bit(data: bytes, is16: bool, delta: bool) -> bytes:
    if is16:
        x = np.frombuffer(data[:len(data) & ~1], dtype=">i2")
        if delta:
            x = np.cumsum(x, dtype=np.int16)
        return (x >> 8).astype(np.int8).tobytes()
    if delta:
        return np.cumsum(np.frombuffer(data, np.int8), dtype=np.int8).tobytes()
    return bytes(data)


def _short():
    raise ModFileError("MMD file is truncated")


def load(path: str | Path) -> MedFile:
    return parse(Path(path).read_bytes(), str(path))
