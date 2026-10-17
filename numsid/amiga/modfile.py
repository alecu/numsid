"""Amiga tracker module (MOD) parser.

Covers the ProTracker family: 31-sample modules with a four-letter tag at
offset 1080 ("M.K." and friends, plus the PC-era multichannel "6CHN",
"8CHN", "xxCH" variants), and the original 15-sample SoundTracker layout,
which has no tag.  Header words are big-endian; lengths and loop points are
stored in 16-bit words.

The sample data is laid out in a chip RAM image the way a tracker loads it,
so the replayer and Paula work with addresses: a sample offset past the end
of a sample reads into the next one, as on the real machine.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

ROWS = 64
BLANK = 0                    # chip RAM address of a silent word for empty samples


class ModFileError(ValueError):
    pass


class NotAModError(ModFileError):
    pass


@dataclass
class Sample:
    name: str
    length: int              # bytes
    finetune: int            # 0..15; 8..15 mean -8..-1
    volume: int              # 0..64
    loop_start: int          # bytes
    loop_length: int         # bytes; 2 means "no loop"
    address: int = BLANK     # where the data sits in ModFile.ram

    @property
    def looped(self) -> bool:
        return self.loop_length > 2


@dataclass
class ModFile:
    title: str
    tag: str                 # "M.K.", "8CHN", ..., or "" for a 15-sample module
    channels: int
    samples: list[Sample]
    orders: list[int]        # the song: a pattern number for each position
    patterns: list = field(repr=False)   # [pattern][row][channel] -> (note word, command word)
    ram: bytes = field(default=b"", repr=False)   # chip RAM image holding the samples
    cia_period: int = 14187  # initial CIA timer value: 125 BPM, or SoundTracker's tempo
    filename: str = ""
    warnings: list[str] = field(default_factory=list)

    # ---- the interface the player and displays share with PSIDFile ---------
    magic = "MOD"
    songs = 1
    start_song = 1
    author = ""
    released = ""

    @property
    def name(self) -> str:
        return self.title or Path(self.filename).stem

    @property
    def kind(self) -> str:
        if not self.tag:
            return "SoundTracker"
        if self.tag in ("M.K.", "M!K!", "M&K!"):
            return "ProTracker"
        if self.tag.startswith("FLT"):
            return "StarTrekker"
        if self.channels != 4:
            return "multichannel"
        return f"{self.tag}"

    @property
    def subtitle(self) -> str:
        return (f"{self.kind} module  ·  {self.channels} channels  ·  "
                f"{len(self.orders)} positions")

    def describe(self) -> str:
        used = sum(s.length for s in self.samples)
        lines = [f"{self.name}" + (f" ({Path(self.filename).name})" if self.filename else ""),
                 f"{self.kind} module ({self.tag or 'no tag'}), {len(self.samples)} samples, "
                 f"{self.channels} channels: {len(self.orders)} positions, "
                 f"{len(self.patterns)} patterns, {used} bytes of samples"]
        for i, s in enumerate(self.samples, 1):
            if not s.length and not s.name:
                continue
            ft = s.finetune - 16 if s.finetune > 7 else s.finetune
            loop = (f"loop {s.loop_start}+{s.loop_length}" if s.looped else "")
            lines.append(f"  {i:2d} {s.name:<22.22} {s.length:6d}  vol {s.volume:2d}"
                         + (f"  ft {ft:+d}" if ft else "") + (f"  {loop}" if loop else ""))
        return "\n".join(lines)


def _tag_channels(tag: bytes) -> int | None:
    """Channel count announced by a 31-sample module's tag, or None."""
    t = tag.decode("latin-1")
    if t in ("M.K.", "M!K!", "M&K!", "FLT4", "4CHN", "N.T.", "NSMS", "LARD", "PATT", "EXO4",
             "FEST"):
        return 4
    if t in ("FLT8", "EXO8", "OCTA", "OKTA", "CD81"):
        return 8
    if t[1:] == "CHN" and t[0].isdigit() and t[0] != "0":
        return int(t[0])
    if t[2:] in ("CH", "CN") and t[:2].isdigit() and 10 <= int(t[:2]) <= 32:
        return int(t[:2])
    if t[:3] == "TDZ" and t[3] in "123":
        return int(t[3])
    return None


def _text(raw: bytes) -> str:
    text = raw.split(b"\0", 1)[0].decode("latin-1")
    return "".join(c if c.isprintable() else " " for c in text).rstrip()


def _plausible_15(blob: bytes) -> bool:
    """A 15-sample module has no tag, so check that its header makes sense."""
    if len(blob) < 600 + 1024:
        return False
    length = blob[470]
    if not 1 <= length <= 128 or max(blob[472:472 + length]) >= 64 or blob[471] > 220:
        return False
    for i in range(15):
        h = blob[20 + 30 * i:50 + 30 * i]
        if h[25] > 64 or h[24] > 15:              # volume, finetune
            return False
        if any(0 < c < 32 and c not in (10, 13) for c in h[:22]):
            return False
    return True


def parse(blob: bytes, filename: str = "") -> ModFile:
    tag = blob[1080:1084] if len(blob) >= 1084 else b""
    channels = _tag_channels(tag) if tag else None
    if channels is not None:
        nsamples, header = 31, 1084
        tag_text = tag.decode("latin-1")
    elif _plausible_15(blob):
        nsamples, header, channels, tag_text = 15, 600, 4, ""
    else:
        raise NotAModError("not a MOD: no known tag at offset 1080, and not a "
                           "15-sample SoundTracker module")
    warnings: list[str] = []
    title = _text(blob[:20])

    raw = []                     # name, length, finetune, volume, loop start, loop length
    for i in range(nsamples):
        h = blob[20 + 30 * i:50 + 30 * i]
        length, finetune, volume, repeat, replen = struct.unpack(">HBBHH", h[22:30])
        if nsamples == 15:       # SoundTracker: no finetune, loop start in bytes
            raw.append((_text(h[:22]), 2 * length, 0, volume, repeat, 2 * replen))
        else:
            raw.append((_text(h[:22]), 2 * length, finetune & 15, volume, 2 * repeat,
                        2 * replen))

    o = 20 + 30 * nsamples
    song_length, restart = blob[o], blob[o + 1]
    table = list(blob[o + 2:o + 130])
    if not 1 <= song_length <= 128:
        raise ModFileError(f"bad song length {song_length}")
    orders = table[:song_length]

    flt8 = tag_text == "FLT8"
    cell = 4 * channels
    block = ROWS * (4 * 4 if flt8 else cell)       # bytes per stored pattern
    count = max(table) + 1
    if flt8:
        count += count % 2
    samples_size = sum(r[1] for r in raw)
    if header + count * block > len(blob):
        # unused garbage in the order table beyond the song: count what is played
        count = max(orders) + 1
        if flt8:
            count += count % 2
    if header + count * block > len(blob):
        raise ModFileError("file too short for its patterns")
    words = struct.unpack(f">{count * block // 2}H", blob[header:header + count * block])
    stride = block // 2
    patterns = []
    if flt8:                       # StarTrekker: an 8-channel pattern is two 4-channel ones
        orders = [p // 2 for p in orders]
        for p in range(0, count, 2):
            rows = []
            for r in range(ROWS):
                cells = []
                for half in (p, p + 1):
                    base = half * stride + r * 8
                    cells += [(words[base + 2 * c], words[base + 2 * c + 1]) for c in range(4)]
                rows.append(tuple(cells))
            patterns.append(rows)
    else:
        for p in range(count):
            base = p * stride
            patterns.append([tuple((words[base + r * 2 * channels + 2 * c],
                                    words[base + r * 2 * channels + 2 * c + 1])
                                   for c in range(channels)) for r in range(ROWS)])

    if nsamples == 15:
        patterns = _convert_soundtracker(patterns, any(r[1] > 9999 for r in raw))

    # sample data, laid out in "chip RAM" after a silent word
    ram = bytearray(4)
    data_at = header + count * block
    if data_at + samples_size > len(blob):
        warnings.append(f"sample data truncated by {data_at + samples_size - len(blob)} bytes")
    samples = []
    for name, length, finetune, volume, loop_start, loop_length in raw:
        data = blob[data_at:data_at + length]
        data_at += length
        if nsamples == 15:
            # Ultimate SoundTracker plays only the loop of a looped sample
            if 0 < loop_start < length and loop_length < length:
                data = data[loop_start:]
                length -= loop_start
                loop_start = 0
            if loop_length > 2 and length > loop_start + loop_length:
                length = loop_start + loop_length
                data = data[:length]
        # clean-up as ProTracker clones do it
        volume = min(volume, 64)
        loop_length = max(loop_length, 2)
        if loop_start + loop_length > 0x1FFFE:
            loop_start, loop_length = 0, 2
        if length and loop_length > 2 and loop_start + loop_length > length:
            length = loop_start + loop_length          # loop runs past the end: pad it
        address = BLANK
        if length:
            address = len(ram)
            chunk = bytearray(data[:length].ljust(length, b"\0"))
            if loop_start + loop_length <= 2:
                chunk[0:2] = b"\0\0"   # as ProTracker does: the idle loop on it stays silent
            ram += chunk
        samples.append(Sample(name, length, finetune, volume, loop_start, loop_length, address))
    ram += bytes(4)

    cia_period = 1773447 // 125                    # CIA timer value for 125 BPM
    if nsamples == 15 and restart not in (0, 120):
        # Ultimate SoundTracker kept its tempo here, a CIA timer value of
        # (240 - x) * 122; 120 is the 50 Hz default.
        cia_period = (240 - restart) * 122
    return ModFile(title, tag_text, channels, samples, orders, patterns, bytes(ram),
                   cia_period, filename, warnings)


def _convert_soundtracker(patterns: list, late: bool) -> list:
    """Map the effects of the SoundTracker versions that wrote 15-sample
    modules onto their ProTracker equivalents, as pt2-clone does."""
    commands = {(cmd >> 8) & 15 for pattern in patterns for row in pattern for _, cmd in row}
    late = late or bool(commands & {0xC, 0xD, 0xE, 0xF})   # TJC SoundTracker II and later
    very_late = 0xF in commands                              # DFJ SoundTracker III and later

    def convert(cmd: int) -> int:
        ins, effect, param = cmd & 0xF000, (cmd >> 8) & 15, cmd & 0xFF
        if not late:
            if effect == 1:                   # arpeggio
                effect = 0
            elif effect == 2 and param & 0xF0:
                param >>= 4                   # pitch slide down
            elif effect == 2 and param & 0x0F:
                effect = 1                    # pitch slide up
        elif effect == 0xD:
            if very_late:
                param = 0                     # pattern break, without a parameter
            else:
                effect = 0xA                  # volume slide
        if (effect == 0xF and not param) or (effect == 0xE and param >> 4 in (0x8, 0xF)):
            effect = param = 0
        return ins | effect << 8 | param

    return [[tuple((note, convert(cmd)) for note, cmd in row) for row in pattern]
            for pattern in patterns]


def load(path: str | Path) -> ModFile:
    return parse(Path(path).read_bytes(), str(path))
