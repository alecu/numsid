"""Which synth a MIDI file was written for.

SysEx says it outright when there is any: a Roland DT1 message addressed to
the MT-32 (model 16h), a GM System On, a GS reset or an XG System On.
Failing that, channel use gives an MT-32 file away: its factory setup puts
the melodic parts on channels 2-9 and the rhythm part on 10, so a file that
plays drums on 10, nothing on 1 and nothing above 10 may well have been
written for one.  That is only a hint, though: General MIDI conversions of
MT-32 soundtracks often keep the channel layout, and say so in a text event
("GENERAL MIDI VERSION"), so what the text says is checked first.
Everything else is taken as General MIDI.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

MT32, GM, GS, XG = "MT-32", "General MIDI", "Roland GS", "Yamaha XG"

GM_PROGRAMS = (
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano", "Honky-tonk Piano",
    "Electric Piano 1", "Electric Piano 2", "Harpsichord", "Clavinet",
    "Celesta", "Glockenspiel", "Music Box", "Vibraphone",
    "Marimba", "Xylophone", "Tubular Bells", "Dulcimer",
    "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ",
    "Reed Organ", "Accordion", "Harmonica", "Tango Accordion",
    "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)", "Electric Guitar (jazz)",
    "Electric Guitar (clean)", "Electric Guitar (muted)", "Overdriven Guitar",
    "Distortion Guitar", "Guitar Harmonics",
    "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)", "Fretless Bass",
    "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
    "Violin", "Viola", "Cello", "Contrabass",
    "Tremolo Strings", "Pizzicato Strings", "Orchestral Harp", "Timpani",
    "String Ensemble 1", "String Ensemble 2", "Synth Strings 1", "Synth Strings 2",
    "Choir Aahs", "Voice Oohs", "Synth Voice", "Orchestra Hit",
    "Trumpet", "Trombone", "Tuba", "Muted Trumpet",
    "French Horn", "Brass Section", "Synth Brass 1", "Synth Brass 2",
    "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax",
    "Oboe", "English Horn", "Bassoon", "Clarinet",
    "Piccolo", "Flute", "Recorder", "Pan Flute",
    "Blown Bottle", "Shakuhachi", "Whistle", "Ocarina",
    "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)",
    "Lead 5 (charang)", "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass + lead)",
    "Pad 1 (new age)", "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)",
    "Pad 5 (bowed)", "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)",
    "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)",
    "FX 5 (brightness)", "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
    "Sitar", "Banjo", "Shamisen", "Koto",
    "Kalimba", "Bagpipe", "Fiddle", "Shanai",
    "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock",
    "Taiko Drum", "Melodic Tom", "Synth Drum", "Reverse Cymbal",
    "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet",
    "Telephone Ring", "Helicopter", "Applause", "Gunshot",
)

_MT32_TEXT = re.compile(r"MT-?32|LAPC|CM-?32|CM-?64", re.I)
_GM_TEXT = re.compile(r"GENERAL MIDI|\bGM\b", re.I)


def _sysex_kind(data: bytes) -> str | None:
    """The synth a SysEx message (without its F0) is aimed at, if it says."""
    if len(data) >= 4 and data[0] == 0x41 and data[2] == 0x16 and data[3] == 0x12:
        return MT32                                # Roland, MT-32 model, DT1
    if data[:4] == b"\x7e\x7f\x09\x01":
        return GM                                  # GM System On (any device id)
    if len(data) >= 4 and data[0] == 0x7E and data[2:4] == b"\x09\x01":
        return GM
    if len(data) >= 7 and data[0] == 0x41 and data[2] == 0x42 and data[3] == 0x12 \
            and data[4:7] == b"\x40\x00\x7f":
        return GS                                  # GS reset
    if len(data) >= 6 and data[0] == 0x43 and data[1] & 0xF0 == 0x10 and data[2] == 0x4C \
            and data[3:6] == b"\x00\x00\x7e":
        return XG                                  # XG System On
    return None


def notes_per_channel(midi) -> Counter:
    """Note-ons per MIDI channel, numbered 1-16."""
    return Counter(e.channel + 1 for e in midi.channel_events() if e.is_note_on)


def detect(midi) -> tuple[str, str]:
    """(synth, the reason in a few words)."""
    kinds = [k for k in map(_sysex_kind, midi.sysex()) if k]
    if MT32 in kinds:
        return MT32, "MT-32 SysEx"
    for kind, why in ((XG, "XG System On"), (GS, "GS reset"), (GM, "GM System On")):
        if kind in kinds:
            return kind, why
    from .smf import COPYRIGHT, TEXT, TRACK_NAME
    texts = midi.texts(TEXT, COPYRIGHT, TRACK_NAME)
    if any(_GM_TEXT.search(t) for t in texts):
        return GM, "the text says General MIDI"
    if any(_MT32_TEXT.search(t) for t in texts):
        return MT32, "the text names an MT-32"
    notes = notes_per_channel(midi)
    if notes[10] and not notes[1] and not any(notes[c] for c in range(11, 17)):
        return MT32, "drums on channel 10, melody on 2-9 only"
    return GM, "no MT-32 signs"


def channel_summary(midi, synth: str) -> list[str]:
    """A line per channel that plays notes: note count and programs used."""
    notes = notes_per_channel(midi)
    programs: dict[int, list[int]] = defaultdict(list)
    for e in midi.channel_events():
        if e.kind == 0xC0 and e.data[0] not in programs[e.channel + 1]:
            programs[e.channel + 1].append(e.data[0])
    drums = 10
    names = None
    if synth == MT32:
        from ..mt32.control import factory_patch_names
        names = factory_patch_names()
    lines = []
    for ch in sorted(notes):
        if ch == drums:
            what = "rhythm" if synth == MT32 else "drums"
        elif synth == MT32:
            what = ", ".join(f"{p} {names[p]}" if names else f"patch {p}"
                             for p in programs[ch]) or "patch (default)"
        else:
            what = ", ".join(f"{p} {GM_PROGRAMS[p]}" for p in programs[ch]) or "0 (default)"
        lines.append(f"  ch {ch:2d}  {notes[ch]:5d} notes  {what}")
    return lines
