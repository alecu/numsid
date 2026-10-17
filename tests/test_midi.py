"""Standard MIDI File parsing, timing, sequencing and synth detection."""

import struct
from pathlib import Path

import pytest

from numsid.__main__ import main
from numsid.midi import smf
from numsid.midi.detect import GM, MT32, XG, detect
from numsid.midi.sequencer import Sequencer
from numsid.tunes import TuneError, load_tune, open_player

THEME = Path(__file__).resolve().parent.parent / "midi" / "theme.mid"
needs_theme = pytest.mark.skipif(not THEME.exists(), reason="midi/theme.mid not present")


def vlq(n: int) -> bytes:
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def track(*events: tuple[int, bytes], end: bool = True) -> bytes:
    """An MTrk chunk from (delta, raw event bytes) pairs."""
    body = b"".join(vlq(d) + raw for d, raw in events)
    if end:
        body += b"\x00\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(body)) + body


def smf_bytes(*tracks: bytes, fmt: int = 1, division: int = 96) -> bytes:
    return b"MThd" + struct.pack(">IHHH", 6, fmt, len(tracks), division) + b"".join(tracks)


def tempo(us: int) -> bytes:
    return b"\xff\x51\x03" + us.to_bytes(3, "big")


def meta_text(kind: int, text: str) -> bytes:
    return bytes([0xFF, kind]) + vlq(len(text)) + text.encode()


def test_vlq_and_running_status():
    blob = smf_bytes(track((0, b"\x90\x3c\x40"), (200, b"\x3e\x40"),      # running status
                           (0x0FFFFFFF, b"\x80\x3c\x00")), fmt=0)
    m = smf.parse(blob)
    notes = [e for e in m.events if e.status < 0xF0]
    assert [(e.tick, e.status, e.data) for e in notes] == [
        (0, 0x90, b"\x3c\x40"), (200, 0x90, b"\x3e\x40"), (200 + 0x0FFFFFFF, 0x80, b"\x3c\x00")]
    assert not m.warnings


def test_note_on_velocity_zero_is_note_off():
    m = smf.parse(smf_bytes(track((0, b"\x91\x40\x50"), (10, b"\x40\x00"))))
    on, off = [e for e in m.events if e.status < 0xF0]
    assert on.is_note_on and not on.is_note_off
    assert off.is_note_off and not off.is_note_on
    assert on.channel == off.channel == 1


def test_tracks_merge_in_time_order_with_track_order_on_ties():
    blob = smf_bytes(track((0, tempo(500_000)), (96, b"\xc0\x05")),
                     track((0, b"\x91\x40\x40"), (96, b"\xc1\x07"), (1, b"\x81\x40\x00")))
    m = smf.parse(blob)
    seq = [(e.tick, e.track, e.status) for e in m.events if e.status != 0xFF]
    assert seq == [(0, 1, 0x91), (96, 0, 0xC0), (96, 1, 0xC1), (97, 1, 0x81)]
    assert m.ntracks == 2 and m.format == 1


def test_tempo_changes_mid_song():
    # 96 ticks/quarter: a quarter at 120 BPM (0.5 s), then 60 BPM from tick 96
    blob = smf_bytes(track((0, tempo(500_000)), (96, tempo(1_000_000)), (192, b"\x90\x3c\x40")))
    m = smf.parse(blob)
    assert m.seconds(96) == pytest.approx(0.5)
    assert m.seconds(192) == pytest.approx(1.5)
    assert m.seconds(288) == pytest.approx(2.5)
    assert m.tempos == pytest.approx([120.0, 60.0])
    assert m.length == pytest.approx(2.5)


def test_default_tempo_is_120_bpm():
    m = smf.parse(smf_bytes(track((480, b"\x90\x3c\x40")), division=480))
    assert m.seconds(480) == pytest.approx(0.5)


def test_smpte_division():
    division = ((256 - 25) << 8) | 40               # 25 fps, 40 ticks per frame
    m = smf.parse(smf_bytes(track((1000, b"\x90\x3c\x40")), division=division))
    assert m.smpte and m.seconds(1000) == pytest.approx(1.0)


def test_sysex_and_escape_forms():
    gm_on = b"\x7e\x7f\x09\x01\xf7"
    blob = smf_bytes(track((0, b"\xf0" + vlq(len(gm_on)) + gm_on),
                           (0, b"\xf7\x02\xf8\xfa"),              # escaped raw bytes
                           (0, b"\x90\x3c\x40")))
    m = smf.parse(blob)
    assert m.sysex() == [gm_on]
    assert [e.status for e in m.events if e.status != 0xFF] == [0xF0, 0xF7, 0x90]


def test_sysex_cancels_running_status():
    blob = smf_bytes(track((0, b"\x90\x3c\x40"), (0, b"\xf0\x01\xf7"), (0, b"\x3c\x00")))
    with pytest.raises(smf.MidiFileError):
        smf.parse(blob)


def test_meta_texts_and_title():
    blob = smf_bytes(track((0, meta_text(smf.TRACK_NAME, "Main Title")),
                           (0, meta_text(smf.TEXT, "by Someone"))),
                     track((0, meta_text(smf.TRACK_NAME, "Bass"))))
    m = smf.parse(blob, "song.mid")
    assert m.title == "Main Title"
    assert m.texts(smf.TEXT, smf.TRACK_NAME) == ["Main Title", "by Someone", "Bass"]
    assert m.name == "song"


def test_sloppy_files_are_repaired_with_warnings():
    good = track((0, b"\x90\x3c\x40"), (10, b"\x80\x3c\x00"))
    truncated = smf_bytes(good)[:-3]                 # track chunk runs off the end
    m = smf.parse(truncated)
    assert m.warnings and m.events[0].is_note_on
    no_end = smf.parse(smf_bytes(track((0, b"\x90\x3c\x40"), end=False)))
    assert any("end-of-track" in w for w in no_end.warnings)
    short = smf.parse(smf_bytes(good, good)[:-len(good)])    # header says 2, file has 1
    assert any("found 1" in w for w in short.warnings)


def test_unknown_chunks_are_skipped_and_rmid_unwrapped():
    inner = smf_bytes(b"XFIH\x00\x00\x00\x02ab" + track((0, b"\x90\x3c\x40")))
    inner = inner[:10] + struct.pack(">H", 1) + inner[12:]
    m = smf.parse(inner)
    assert m.ntracks == 1 and m.events[0].is_note_on
    rmid = b"RIFF" + struct.pack("<I", len(inner) + 12) + b"RMIDdata" + struct.pack("<I", len(inner)) + inner
    assert smf.parse(rmid).events[0].is_note_on


def test_not_a_midi_file():
    with pytest.raises(smf.NotAMidiError):
        smf.parse(b"PSID" + bytes(100))
    with pytest.raises(smf.MidiFileError):
        smf.parse(smf_bytes(track((0, b"\x90\x3c\x40")), fmt=3))


# ---- detection ---------------------------------------------------------------

def _song(*events, texts=()):
    t0 = track(*[(0, meta_text(smf.TEXT, t)) for t in texts], *[(0, e) for e in events])
    return smf.parse(smf_bytes(t0))


def test_detect_by_sysex():
    mt32_dt1 = b"\xf0\x0a\x41\x10\x16\x12\x10\x00\x16\x02\x58\xf7"
    xg_on = b"\xf0\x08\x43\x10\x4c\x00\x00\x7e\x00\xf7"
    gm_on = b"\xf0\x05\x7e\x7f\x09\x01\xf7"
    assert detect(_song(mt32_dt1, b"\x90\x3c\x40"))[0] == MT32
    assert detect(_song(xg_on, b"\x90\x3c\x40"))[0] == XG
    assert detect(_song(gm_on, b"\x99\x24\x40"))[0] == GM


def test_detect_by_channel_layout_and_text():
    mt32_layout = (b"\x91\x3c\x40", b"\x99\x24\x40")                 # channels 2 and 10
    assert detect(_song(*mt32_layout))[0] == MT32
    assert detect(_song(*mt32_layout, texts=["GENERAL MIDI VERSION"]))[0] == GM
    assert detect(_song(b"\x90\x3c\x40", b"\x99\x24\x40"))[0] == GM  # uses channel 1
    assert detect(_song(b"\x90\x3c\x40", texts=["for the Roland MT-32"]))[0] == MT32


# ---- sequencer ---------------------------------------------------------------

def test_sequencer_places_events_on_exact_samples():
    blob = smf_bytes(track((0, tempo(500_000)), (0, b"\xc0\x10"), (96, b"\x90\x3c\x40"),
                           (96, tempo(250_000)), (96, b"\x80\x3c\x00")))
    seq = Sequencer(smf.parse(blob), 32000)
    assert seq.times == [0, 16000, 40000]            # 0.5 s, then 0.5 + 0.5 + 0.25 s
    assert seq.next_time() == 0
    assert [t for t, _ in seq.take(16000)] == [0]
    assert [t for t, _ in seq.take(16001)] == [16000]
    assert seq.take(39999) == []
    assert [e.status for _, e in seq.take(10**9)] == [0x80]
    assert seq.finished and seq.next_time() is None


def test_sequencer_seek_keeps_setup_drops_notes():
    blob = smf_bytes(track((0, b"\xc0\x10"), (0, b"\x90\x3c\x40"), (10, b"\xb0\x07\x64"),
                           (10, b"\x80\x3c\x00"), (10, b"\x90\x3e\x40")))
    seq = Sequencer(smf.parse(blob), 1000)
    setup = seq.seek(seq.times[3])
    assert [e.status for e in setup] == [0xC0, 0xB0]
    assert [e.data for _, e in seq.take(10**9)] == [b"\x3c\x00", b"\x3e\x40"]


# ---- the command line ----------------------------------------------------------

def test_load_tune_and_info(tmp_path, capsys):
    path = tmp_path / "song.mid"
    path.write_bytes(smf_bytes(track((0, meta_text(smf.TRACK_NAME, "Song")), (0, b"\xc0\x49"),
                                     (0, b"\x90\x3c\x40"), (96, b"\x80\x3c\x00"))))
    assert isinstance(load_tune(path), smf.MidiFile)
    assert main([str(path), "--info"]) == 0
    out = capsys.readouterr().out
    assert "format 1, 1 tracks" in out and "73 Flute" in out and "General MIDI" in out
    from numsid.mt32.control import factory_patch_names
    wav = tmp_path / "x.wav"
    if factory_patch_names():                      # the ROMs are there: it plays
        assert main([str(path), "--wav", str(wav), "--seconds", "1"]) == 0 and wav.exists()
    else:
        assert main([str(path), "--wav", str(wav)]) == 1
        with pytest.raises(TuneError):
            open_player(load_tune(path))


@needs_theme
def test_theme_mid():
    m = smf.load(THEME)
    assert (m.format, m.ntracks, m.division) == (1, 15, 192)
    assert m.length == pytest.approx(174.0, abs=0.5)
    assert detect(m)[0] == GM                        # a GM conversion, despite the MT-32 layout
    assert not m.warnings


def test_mt32_info_names_patches_when_roms_are_present(tmp_path, capsys):
    from numsid.mt32.control import factory_patch_names
    path = tmp_path / "mt32.mid"
    path.write_bytes(smf_bytes(track((0, b"\xc1\x10"), (0, b"\x91\x3c\x40"), (0, b"\x99\x24\x40"))))
    assert main([str(path), "--info"]) == 0
    out = capsys.readouterr().out
    assert "for MT-32" in out
    assert ("16 Harpsi 1" if factory_patch_names() else "patch 16") in out


def test_gm_to_mt32_maps():
    from numsid.midi import maps
    assert len(maps.GM_TO_MT32) == 128 and all(0 <= p < 128 for p in maps.GM_TO_MT32)
    assert maps.GM_TO_MT32[75] == 77 and maps.GM_TO_MT32[12] == 104   # pan flute, marimba
    assert maps.gm_drum_key(36) == 36 and maps.gm_drum_key(57) == 49
    assert maps.gm_drum_key(81) is None


def test_channel_map_folds_gm_channels_onto_mt32_parts():
    from numsid.midi.maps import channel_map
    notes = [bytes([0x90 | ch, 60, 64]) for ch in (0, 0, 0, 1, 2, 9, 10, 11, 12, 13, 14, 15)]
    m = smf.parse(smf_bytes(track(*[(0, n) for n in notes])))
    cmap = channel_map(m)
    assert cmap[9] == 9 and cmap[1] == 1 and cmap[2] == 2     # already on MT-32 parts: kept
    assert cmap[0] == 3                                       # busiest takes the first free part
    assert sorted(cmap.values()) == list(range(1, 10))        # parts 2-9 and rhythm, once each
    assert 15 not in cmap                                     # one channel too many: dropped
