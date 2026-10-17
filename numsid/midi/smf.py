"""Standard MIDI File (SMF) parser.

Formats 0, 1 and 2, metrical (ticks per quarter note) and SMPTE timing,
running status, SysEx in both the F0 and the F7 "escape" forms, and the
RIFF "RMID" wrapper some Windows files come in.  All tracks are merged into
one list of events in time order, and the tempo map turns ticks into
seconds.

Real-world files are sloppy, so the parser is forgiving where the intent is
clear: a track whose length runs past the end of the file is read up to the
end, junk after a track is skipped, and a missing end-of-track is fine.
Each such repair is noted in `warnings`.
"""

from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TEMPO = 500_000          # microseconds per quarter note: 120 BPM

# meta event types
TEXT, COPYRIGHT, TRACK_NAME, INSTRUMENT, LYRIC, MARKER, CUE = 1, 2, 3, 4, 5, 6, 7
END_OF_TRACK, TEMPO, TIME_SIGNATURE, KEY_SIGNATURE = 0x2F, 0x51, 0x58, 0x59


class MidiFileError(ValueError):
    pass


class NotAMidiError(MidiFileError):
    pass


@dataclass(slots=True)
class Event:
    """One event.  `status` is the MIDI status byte (0x80-0xEF) for channel
    messages, 0xF0 for SysEx (`data` is the message without the leading F0,
    normally ending in F7), 0xF7 for an escaped raw sequence, and 0xFF for a
    meta event, whose type is `meta`."""
    tick: int
    track: int
    status: int
    data: bytes
    meta: int = -1

    @property
    def channel(self) -> int:
        """0..15 for a channel message."""
        return self.status & 0x0F

    @property
    def kind(self) -> int:
        """The message type for a channel message: 0x80 note off, 0x90 note
        on, 0xA0 key pressure, 0xB0 controller, 0xC0 program, 0xD0 channel
        pressure, 0xE0 pitch bend."""
        return self.status & 0xF0

    @property
    def is_note_on(self) -> bool:
        return self.status & 0xF0 == 0x90 and self.data[1] > 0

    @property
    def is_note_off(self) -> bool:
        k = self.status & 0xF0
        return k == 0x80 or (k == 0x90 and self.data[1] == 0)

    @property
    def text(self) -> str:
        return self.data.decode("latin-1").rstrip("\0")


@dataclass
class MidiFile:
    format: int
    division: int                # ticks per quarter note, or ticks per second for SMPTE
    smpte: bool
    events: list[Event] = field(repr=False)
    ntracks: int = 0
    filename: str = ""
    warnings: list[str] = field(default_factory=list)

    # ---- the interface the player and displays share with PSIDFile ---------
    magic = "MIDI"
    songs = 1
    start_song = 1
    author = ""
    released = ""

    def __post_init__(self) -> None:
        self._tempo_map()

    def _tempo_map(self) -> None:
        """Breakpoints for tick -> seconds: the tick, the time in seconds
        there, and the seconds per tick from there on."""
        if self.smpte:
            self._ticks, self._secs, self._rates = [0], [0.0], [1.0 / self.division]
            return
        ticks, secs, rates = [0], [0.0], [DEFAULT_TEMPO / 1e6 / self.division]
        for e in self.events:
            if e.meta == TEMPO and len(e.data) >= 3:
                tempo = int.from_bytes(e.data[:3], "big") or DEFAULT_TEMPO
                t = secs[-1] + (e.tick - ticks[-1]) * rates[-1]
                if e.tick == ticks[-1]:
                    rates[-1] = tempo / 1e6 / self.division
                else:
                    ticks.append(e.tick)
                    secs.append(t)
                    rates.append(tempo / 1e6 / self.division)
        self._ticks, self._secs, self._rates = ticks, secs, rates

    def seconds(self, tick: int) -> float:
        """Time in seconds at `tick`."""
        i = bisect.bisect_right(self._ticks, tick) - 1
        return self._secs[i] + (tick - self._ticks[i]) * self._rates[i]

    @property
    def tempos(self) -> list[float]:
        """Every tempo the song uses, in BPM, in order of appearance."""
        if self.smpte:
            return []
        return [60.0 / (r * self.division) for r in self._rates]

    @property
    def end_tick(self) -> int:
        return self.events[-1].tick if self.events else 0

    @property
    def length(self) -> float:
        """Seconds from the start to the last event (normally the last
        track's end-of-track)."""
        return self.seconds(self.end_tick)

    def texts(self, *metas: int) -> list[str]:
        """The text of the meta events of the given types, in file order,
        with blank ones left out."""
        return [e.text.strip() for e in self.events
                if e.meta in metas and e.text.strip()]

    @property
    def title(self) -> str:
        """The first track's name, the usual place for a song title."""
        for e in self.events:
            if e.meta == TRACK_NAME and e.track == 0 and e.text.strip():
                return e.text.strip()
        return ""

    @property
    def name(self) -> str:
        return Path(self.filename).stem if self.filename else (self.title or "MIDI file")

    @property
    def subtitle(self) -> str:
        return self.title

    def channel_events(self):
        return (e for e in self.events if e.status < 0xF0)

    def sysex(self) -> list[bytes]:
        return [e.data for e in self.events if e.status == 0xF0]

    def describe(self) -> str:
        from .detect import channel_summary, detect
        tempos = self.tempos
        timing = (f"SMPTE, {self.division} ticks/s" if self.smpte
                  else f"{self.division} ticks/quarter")
        bpm = "" if not tempos else (
            f", {tempos[0]:.0f} BPM" if len(set(round(t, 1) for t in tempos)) == 1
            else f", {min(tempos):.0f}-{max(tempos):.0f} BPM")
        m, s = divmod(self.length, 60)
        synth, why = detect(self)
        lines = [f"{self.name}" + (f" ({Path(self.filename).name})" if self.filename else ""),
                 f"Standard MIDI File, format {self.format}, {self.ntracks} tracks, {timing}{bpm}, "
                 f"{int(m)}:{s:04.1f}",
                 f"for {synth}: {why}"]
        for text in self.texts(TEXT, COPYRIGHT, TRACK_NAME)[:6]:
            lines.append(f"  \"{text}\"")
        lines += channel_summary(self, synth)
        lines += [f"warning: {w}" for w in self.warnings]
        return "\n".join(lines)


def _vlq(blob: bytes, i: int, end: int) -> tuple[int, int]:
    """A variable-length quantity at blob[i]: (value, index after it)."""
    value = 0
    for _ in range(4):
        if i >= end:
            raise MidiFileError("variable-length number runs off the end of the track")
        b = blob[i]
        i += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, i
    raise MidiFileError("variable-length number longer than 4 bytes")


DATA_BYTES = {0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xC0: 1, 0xD0: 1, 0xE0: 2}


def _track(blob: bytes, i: int, end: int, track: int, out: list[Event],
           warnings: list[str]) -> None:
    """Parse the events of one track chunk, blob[i:end], into `out`."""
    tick = 0
    running = 0
    while i < end:
        delta, i = _vlq(blob, i, end)
        tick += delta
        if i >= end:
            break
        status = blob[i]
        if status == 0xFF:                       # meta
            if i + 1 >= end:
                break
            meta = blob[i + 1]
            n, i = _vlq(blob, i + 2, end)
            out.append(Event(tick, track, 0xFF, bytes(blob[i:i + n]), meta))
            i += n
            if meta == END_OF_TRACK:
                return
        elif status in (0xF0, 0xF7):             # SysEx, or an escaped raw sequence
            n, i = _vlq(blob, i + 1, end)
            out.append(Event(tick, track, status, bytes(blob[i:i + n])))
            i += n
            running = 0
        elif status > 0xF7:                      # stray real-time byte: no data
            i += 1
        else:
            if status & 0x80:
                running = status
                i += 1
            elif not running:
                raise MidiFileError(f"track {track}: data byte {status:#04x} with no status")
            n = DATA_BYTES[running & 0xF0]
            if i + n > end:
                warnings.append(f"track {track}: last event cut short")
                return
            out.append(Event(tick, track, running, bytes(blob[i:i + n])))
            i += n
    warnings.append(f"track {track}: no end-of-track event")


def parse(blob: bytes, filename: str = "") -> MidiFile:
    if blob[:4] == b"RIFF" and blob[8:12] == b"RMID":
        at = blob.find(b"MThd", 12)
        if at < 0:
            raise MidiFileError("RIFF RMID file without MIDI data")
        blob = blob[at:]
    if blob[:4] != b"MThd":
        raise NotAMidiError("not a Standard MIDI File")
    if len(blob) < 14:
        raise MidiFileError("MIDI header cut short")
    hlen = struct.unpack(">I", blob[4:8])[0]
    fmt, ntracks, division = struct.unpack(">HHH", blob[8:14])
    if fmt > 2:
        raise MidiFileError(f"unknown SMF format {fmt}")
    smpte = bool(division & 0x8000)
    if smpte:
        fps = 256 - (division >> 8)
        division = (29.97 if fps == 29 else fps) * (division & 0xFF)
        division = round(division) or 1
    elif division == 0:
        raise MidiFileError("MIDI header says 0 ticks per quarter note")

    warnings: list[str] = []
    events: list[Event] = []
    i = 8 + hlen
    track = 0
    while track < ntracks and i + 8 <= len(blob):
        tag = blob[i:i + 4]
        n = struct.unpack(">I", blob[i + 4:i + 8])[0]
        i += 8
        if tag != b"MTrk":                       # unknown chunk: skip it
            i += n
            continue
        end = i + n
        if end > len(blob):
            warnings.append(f"track {track}: chunk runs {end - len(blob)} bytes past the end of the file")
            end = len(blob)
        _track(blob, i, end, track, events, warnings)
        i = end
        track += 1
    if track < ntracks:
        warnings.append(f"header says {ntracks} tracks, found {track}")
    if fmt == 2 and track > 1:
        warnings.append("format 2 (independent sequences): only the first one is played")
        events = [e for e in events if e.track == 0]
    events.sort(key=lambda e: e.tick)        # stable: track order, then file order
    return MidiFile(fmt, division, smpte, events, track, filename, warnings)


def load(path: str | Path) -> MidiFile:
    return parse(Path(path).read_bytes(), str(path))
