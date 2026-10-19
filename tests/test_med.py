"""PowerPacker decrunching, the OctaMED loader and replayer, and the soft limiter."""
import random
import struct

import numpy as np
import pytest

from numsid import med, medfile, powerpacker
from numsid.modfile import BLANK, ModFile
from numsid.player import KNEE, ModPlayer, TuneError, load_tune, open_player, soft_limit, song_length
from numsid.protracker import PAL_CIA_CLOCK, PERIODS, VIBRATO, NullPaula

from test_mod import SQUARE, make_mod

EFFICIENCY = (9, 10, 11, 11)


# ---- PowerPacker ---------------------------------------------------------------------

def crunch(data: bytes, efficiency=EFFICIENCY) -> bytes:
    """A PP20 file for `data`: a greedy LZ77 encoder writing the bit stream
    the decruncher reads (the output is rebuilt from its last byte back)."""
    r = data[::-1]
    bits: list[int] = []

    def put(value, k):
        bits.extend((value >> (k - 1 - i)) & 1 for i in range(k))

    def put_run(n, step, top):          # n as a sum of `step`-bit fields, `top` = continue
        while n >= top:
            put(top, step)
            n -= top
        put(n, step)

    literals = []
    w = 0
    while w < len(r):
        best_len, best_dist = 0, 0
        for dist in range(1, min(w, 1 << efficiency[3]) + 1):
            n = 0
            while w + n < len(r) and n < 40 and r[w + n] == r[w + n - dist]:
                n += 1
            if n > best_len:
                best_len, best_dist = n, dist
        x = min(best_len, 5) - 2
        if best_len >= 2 and best_dist - 1 < 1 << efficiency[min(x, 3)]:
            if literals:
                put(0, 1)
                put_run(len(literals) - 1, 2, 3)
                for b in literals:
                    put(b, 8)
                literals = []
            else:
                put(1, 1)
            if best_len >= 5:
                put(3, 2)
                put(1, 1)                    # offset width from the efficiency table
                put(best_dist - 1, efficiency[3])
                put_run(best_len - 5, 3, 7)
            else:
                put(x, 2)
                put(best_dist - 1, efficiency[x])
            w += best_len
        else:
            literals.append(r[w])
            w += 1
    if literals:
        put(0, 1)
        put_run(len(literals) - 1, 2, 3)
        for b in literals:
            put(b, 8)
    skip = -len(bits) % 8
    bits = [0] * skip + bits
    stream = bytes(sum(bits[8 * j + i] << i for i in range(8)) for j in range(len(bits) // 8))
    return b"PP20" + bytes(efficiency) + stream[::-1] + len(data).to_bytes(3, "big") + bytes([skip])


@pytest.mark.parametrize("data", [
    b"x",
    b"hello, world",
    b"abcabcabcabcabcabcabcabcabcabc",
    bytes(300),                                          # long overlapping copies
    bytes(random.Random(1).randrange(4) for _ in range(3000)),
    bytes(random.Random(2).randrange(256) for _ in range(500)),
])
def test_powerpacker_round_trip(data):
    packed = crunch(data)
    assert powerpacker.is_crunched(packed)
    assert powerpacker.decrunch(packed) == data


def test_powerpacker_rejects_damage():
    packed = crunch(b"abcabcabcabcabc" * 20)
    with pytest.raises(powerpacker.PowerPackerError):
        powerpacker.decrunch(packed[:8] + packed[20:])           # bits missing
    with pytest.raises(powerpacker.PowerPackerError):
        powerpacker.decrunch(b"PP20" + bytes(4) + packed[8:])    # bad efficiency table
    with pytest.raises(powerpacker.PowerPackerError):
        powerpacker.decrunch(b"PP20")


def test_load_tune_decrunches_a_packed_mod(tmp_path):
    blob = make_mod({(0, 0, 0): (428, 1, 0, 0)}, [(SQUARE * 4, 64, 0, 0, 2)])
    path = tmp_path / "packed.mod"
    path.write_bytes(crunch(blob))
    mod = load_tune(path)
    assert isinstance(mod, ModFile) and mod.tag == "M.K."
    assert mod.patterns[0][0][0] == (428, 0x1000)
    path.write_bytes(crunch(blob)[:-30] + crunch(blob)[-4:])
    with pytest.raises(TuneError):
        load_tune(path)


# ---- MMD files -----------------------------------------------------------------------

def make_mmd(cells: dict, samples: list, orders=(0,), *, version: int = 0, lines: int = 64,
             tracks: int = 4, tempo: int = 33, speed: int = 6, flags: int = 0x10,
             flags2: int = 0, transpose: int = 0, track_volumes=None, master: int = 64,
             nblocks: int | None = None, pages: dict | None = None) -> bytes:
    """An MMD0/MMD1 file.  cells: {(block, line, track): (note, instrument,
    command, parameter)}; samples: (data, volume, transpose, loop start,
    loop length, hold, decay) with byte lengths (missing fields 0); pages
    (MMD1): {block: [{(line, track): (command, parameter)}, ...]}, extra
    effect columns.  The default flags select hex volumes."""
    nblocks = nblocks or max(orders) + 1
    samples = [tuple(s) + (0,) * (7 - len(s)) for s in samples]
    song = bytearray()
    for i in range(63):
        if i < len(samples):
            data, vol, strans, ls, ll, _, _ = samples[i]
            song += struct.pack(">HHBBBb", ls // 2, max(ll // 2, 1), 0, 0, vol, strans)
        else:
            song += struct.pack(">HHBBBb", 0, 1, 0, 0, 0, 0)
    song += struct.pack(">HH", nblocks, len(orders)) + bytes(orders).ljust(256, b"\0")
    tv = (track_volumes or [64] * tracks)[:16]
    song += struct.pack(">HbBBB", tempo, transpose, flags, flags2, speed)
    song += bytes(tv).ljust(16, b"\0") + bytes([master, len(samples)])
    assert len(song) == 788

    blocks = []
    for b in range(nblocks):
        if version == 0:
            body = bytearray([tracks, lines - 1])
        else:
            body = bytearray(struct.pack(">HHI", tracks, lines - 1, 0))
        for line in range(lines):
            for t in range(tracks):
                note, ins, cmd, par = cells.get((b, line, t), (0, 0, 0, 0))
                if version == 0:
                    body += bytes([note | (ins & 0x10) << 3 | (ins & 0x20) << 1,
                                   (ins & 15) << 4 | cmd, par])
                else:
                    body += bytes([note, ins, cmd, par])
        blocks.append(bytes(body))

    blob = bytearray(52)
    blob += song
    block_array = len(blob)
    blob += bytes(4 * nblocks)
    for b, body in enumerate(blocks):
        struct.pack_into(">I", blob, block_array + 4 * b, len(blob))
        blob += body
    for b, block_pages in (pages or {}).items():
        offsets = []
        for page in block_pages:
            offsets.append(len(blob))
            for line in range(lines):
                for t in range(tracks):
                    blob += bytes(page.get((line, t), (0, 0)))
        table = len(blob)
        blob += struct.pack(f">HH{len(offsets)}I", len(offsets), 0, *offsets)
        info = len(blob)
        blob += struct.pack(">5I", 0, 0, 0, table, 0) + bytes(16)
        block = struct.unpack_from(">I", blob, block_array + 4 * b)[0]
        struct.pack_into(">I", blob, block + 4, info)
    sample_array = len(blob)
    blob += bytes(4 * len(samples))
    for i, (data, *_rest) in enumerate(samples):
        struct.pack_into(">I", blob, sample_array + 4 * i, len(blob))
        if isinstance(data, Instrument):
            blob += data.blob
        else:
            blob += struct.pack(">Ih", len(data), 0) + data
        if len(blob) & 1:
            blob += b"\0"
    exp = 0
    if any(s[5] for s in samples):                       # hold/decay: InstrExt entries
        ext = len(blob)
        for s in samples:
            blob += bytes([s[5], s[6], 0, 0])
        exp = len(blob)
        blob += struct.pack(">IIHH", 0, ext, len(samples), 4) + bytes(80 - 12)
    blob[0:52] = (b"MMD" + bytes([48 + version]) + struct.pack(">II", len(blob), 52) + bytes(4)
                  + struct.pack(">I", block_array) + bytes(4) + struct.pack(">I", sample_array)
                  + bytes(4) + struct.pack(">I", exp) + bytes(16))
    return bytes(blob)


class Instrument:
    """A raw MMD instrument, header included (for synthetic ones)."""

    def __init__(self, blob: bytes):
        self.blob = blob

    def __len__(self):
        return len(self.blob)


def synth(waveforms, volume_table: bytes, waveform_table: bytes, speeds=(1, 1)) -> Instrument:
    """A synthetic instrument: looped waveforms and the two tables."""
    head = struct.pack(">B3xHHHHBBH", 0, 0, 0, len(volume_table), len(waveform_table), *speeds,
                       len(waveforms))
    head += volume_table.ljust(128, b"\xff") + waveform_table.ljust(128, b"\xff")
    offsets, body = [], b""
    base = 6 + len(head) + 4 * len(waveforms)
    for w in waveforms:
        offsets.append(base + len(body))
        body += struct.pack(">H", len(w) // 2) + w
    data = head + struct.pack(f">{len(waveforms)}I", *offsets) + body
    return Instrument(struct.pack(">Ih", len(data), -1) + data)


SAW = bytes(range(0, 256, 8))                        # 32 bytes
UP = bytes([60] * 16 + [196] * 16)


class Recorder(NullPaula):
    def __init__(self, mod):
        super().__init__(mod)
        self.per, self.vol, self.lc, self.len = {}, {}, {}, {}
        self.triggers = 0
        self.led = None

    def write_period(self, ch, period): self.per[ch] = period
    def write_volume(self, ch, volume): self.vol[ch] = volume
    def write_location(self, ch, address): self.lc[ch] = address
    def write_length(self, ch, words): self.len[ch] = words
    def set_led(self, on): self.led = on

    def dma(self, mask, on):
        if on and mask & 1:
            self.triggers += 1


def trace(blob: bytes, ticks: int, ch: int = 0):
    """(period, volume) written to a channel after each tick."""
    mod = medfile.parse(blob)
    hw = Recorder(mod)
    r = med.MED(mod, hw)
    out = []
    for _ in range(ticks):
        r.tick()
        out.append((hw.per.get(ch), hw.vol.get(ch)))
    return out, r, hw


def test_parse_mmd0():
    blob = make_mmd({(0, 0, 0): (13, 1, 0xC, 0x20), (0, 1, 3): (1, 33, 0xF, 0xFE)},
                    [(SQUARE, 48, 0, 0, 32)] + [(b"", 0)] * 31 + [(SQUARE * 2, 64, -12)],
                    orders=(0, 1, 0), speed=3, tempo=40)
    m = medfile.parse(blob, "song.med")
    assert (m.version, m.tag, m.channels, m.orders, m.speed, m.tempo) == (0, "MMD0", 4, [0, 1, 0], 3, 40)
    assert m.patterns[0][0][0] == (13, 1, 0xC, 0x20)
    assert m.patterns[0][1][3] == (1, 33, 0xF, 0xFE)        # instrument bits 4 and 5 from the note byte
    s = m.samples[0]
    assert (s.volume, s.loop_start, s.loop_length, s.looped) == (48, 0, 32, True)
    assert m.ram[s.address:s.address + s.length] == SQUARE
    assert m.samples[32].transpose == -12 and not m.samples[1].length
    assert "MMD0" in m.describe() and m.hex_volumes and not m.st_slides


def test_parse_mmd1_and_errors(tmp_path):
    m = medfile.parse(make_mmd({(0, 0, 5): (25, 2, 0x19, 0x04)}, [(SQUARE, 64), (SQUARE, 64)],
                               version=1, tracks=6, lines=16))
    assert (m.tag, m.channels, len(m.patterns[0])) == ("MMD1", 6, 16)
    assert m.patterns[0][0][5] == (25, 2, 0x19, 0x04)
    with pytest.raises(medfile.ModFileError):
        medfile.parse(make_mmd({}, [(SQUARE, 64)])[:900])
    with pytest.raises(medfile.NotAModError):
        medfile.parse(b"MMDX" + bytes(100))
    path = tmp_path / "x.med"
    path.write_bytes(make_mmd({}, [(SQUARE, 64)])[:1000])
    with pytest.raises(TuneError):
        load_tune(path)


def test_tempo_units():
    m = medfile.parse(make_mmd({}, [(SQUARE, 64)]))
    assert med.med_bpm(m, 33) == pytest.approx(125.0)            # SPD tempo: 33 is 125 BPM
    assert med.med_bpm(m, 6) == pytest.approx(6 * 1773447 / 14500 / 6)   # SoundTracker-compatible
    bpm = medfile.parse(make_mmd({}, [(SQUARE, 64)], flags2=0x20 | 3))  # BPM mode, 4 lines/beat
    assert med.med_bpm(bpm, 120) == pytest.approx(120.0)
    eight = medfile.parse(make_mmd({}, [(SQUARE, 64)], flags=0x50))
    assert med.med_bpm(eight, 1) == 179


def test_tick_timing_speed_and_tempo_command():
    blob = make_mmd({(0, 0, 0): (13, 1, 0x9, 3), (0, 1, 0): (0, 0, 0xF, 66)}, [(SQUARE, 64)], speed=6)
    mod = medfile.parse(blob)
    r = med.MED(mod, NullPaula(mod))
    times = []
    for _ in range(8):
        r.tick()
        times.append(r.time)
    tick33 = PAL_CIA_CLOCK * 2.5 / 125
    assert r.speed == 3
    assert times[2] == round(3 * tick33)                 # line 0 lasts 3 ticks at tempo 33
    assert times[3] - times[2] == pytest.approx(tick33 / 2, abs=1)   # tempo 66 from line 1's first tick


def test_notes_wrap_and_transpose():
    cells = {(0, 0, 0): (1, 1, 0, 0), (0, 1, 0): (13, 1, 0, 0), (0, 2, 0): (37, 1, 0, 0),
             (0, 3, 0): (49, 1, 0, 0), (0, 4, 0): (1, 2, 0, 0)}
    out, _, _ = trace(make_mmd(cells, [(SQUARE, 64), (SQUARE, 64, 12)], speed=1), 5)
    periods = [p for p, _ in out]
    # C-1, C-2, then C-4 and C-5 both wrap down to C-3; sample transpose +12 makes C-1 a C-2
    assert periods == [856, 428, 214, 214, 428]
    out, _, _ = trace(make_mmd({(0, 0, 0): (13, 1, 0, 0)}, [(SQUARE, 64)], transpose=-1, speed=1), 1)
    assert out[0][0] == PERIODS[11]                      # play transpose: B-1


def test_notes_past_b7_come_back_nine_octaves_down():
    cells = {(0, 0, 0): (106, 1, 0, 0), (0, 1, 0): (80, 1, 0, 0)}
    out, _, _ = trace(make_mmd(cells, [(SQUARE, 64)], version=1, speed=1), 2)
    # 106 is A-9: 9 octaves down it is A-0, three semitones below C-1;
    # 80 (G-7) only wraps into the third octave
    assert [p for p, _ in out] == [PERIODS[9] << 1, PERIODS[31]]


def test_volumes_hex_bcd_track_and_master():
    cells = {(0, 0, 0): (13, 1, 0xC, 0x20)}
    assert trace(make_mmd(cells, [(SQUARE, 64)]), 1)[0][0][1] == 32
    assert trace(make_mmd(cells, [(SQUARE, 64)], flags=0), 1)[0][0][1] == 20    # decimal
    out, _, _ = trace(make_mmd({(0, 0, 0): (13, 1, 0, 0)}, [(SQUARE, 64)],
                               track_volumes=[32, 64, 64, 64], master=32), 1)
    assert out[0][1] == 16


def test_slides_run_on_the_first_tick_unless_soundtracker_style():
    cells = {(0, 0, 0): (13, 1, 0xC, 0x20), (0, 1, 0): (0, 0, 0xD, 0x02)}
    fast, _, _ = trace(make_mmd(cells, [(SQUARE, 64)], speed=3), 6)
    assert [v for _, v in fast[3:]] == [30, 28, 26]
    st, _, _ = trace(make_mmd(cells, [(SQUARE, 64)], speed=3, flags=0x30), 6)
    assert [v for _, v in st[3:]] == [32, 30, 28]
    up, _, _ = trace(make_mmd({(0, 0, 0): (13, 1, 0x1, 4)}, [(SQUARE, 64)], speed=3), 3)
    assert [p for p, _ in up] == [424, 420, 416]


def test_arpeggio_and_vibrato():
    out, _, _ = trace(make_mmd({(0, 0, 0): (13, 1, 0x0, 0x47)}, [(SQUARE, 64)], speed=6), 6)
    assert [p for p, _ in out] == [428, PERIODS[16], PERIODS[19]] * 2
    for cmd, depth in ((0x4, 8), (0x14, 4)):           # 04 is twice as deep as ProTracker
        out, _, _ = trace(make_mmd({(0, 0, 0): (13, 1, cmd, 0x44)}, [(SQUARE, 64)], version=1,
                                   speed=6), 6)
        want = [428] + [428 + (VIBRATO[(16 * k >> 2) & 31] * depth >> 7) for k in range(5)]
        assert [p for p, _ in out] == want


def test_flow_commands_and_end_of_song():
    cells = {(0, 0, 0): (13, 1, 0, 0), (0, 1, 0): (0, 0, 0xF, 0x00),          # break
             (1, 0, 0): (13, 1, 0xB, 3), (3, 0, 0): (0, 0, 0xF, 0xFE)}        # jump, end
    mod = medfile.parse(make_mmd(cells, [(SQUARE, 64)], orders=(0, 1, 2, 3), lines=8, speed=1))
    r = med.MED(mod, NullPaula(mod))
    lines = []
    while r.playing:
        r.tick()
        lines.append(r.row_read)
    assert lines == [(0, 0), (0, 1), (1, 0), (3, 0)]
    assert med.song_length(mod) == (pytest.approx(4 * PAL_CIA_CLOCK * 2.5 / 125 / PAL_CIA_CLOCK + 2.5),
                                    "end")


def test_extra_command_pages():
    cells = {(0, 0, 0): (13, 1, 0, 0), (0, 0, 1): (13, 1, 0x1, 2)}
    pages = {0: [{(0, 0): (0x09, 3), (0, 1): (0x0C, 0x20)}]}
    out, r, _ = trace(make_mmd(cells, [(SQUARE, 64)], version=1, pages=pages, speed=6), 3, ch=1)
    assert r.speed == 3                                      # line command from the page
    assert [v for _, v in out] == [32, 32, 32]               # volume set alongside the main column's slide
    assert [p for p, _ in out] == [426, 424, 422]
    cells = {(0, 0, 0): (13, 1, 0, 0)}
    pages = {0: [{(0, 0): (0x0D, 0x02)}]}                    # main column empty: the page's slide runs
    out, _, _ = trace(make_mmd(cells, [(SQUARE, 64)], version=1, pages=pages, speed=3), 3)
    assert [v for _, v in out] == [62, 60, 58]


def test_one_shot_sample_idles_on_a_silent_word():
    _, r, hw = trace(make_mmd({(0, 0, 0): (13, 1, 0, 0)}, [(SQUARE, 64)]), 1)
    assert hw.triggers == 1 and (hw.lc[0], hw.len[0]) == (BLANK, 1)
    _, r, hw = trace(make_mmd({(0, 0, 0): (13, 1, 0, 0)}, [(SQUARE * 2, 64, 0, 32, 32)]), 1)
    s = r.mod.samples[0]
    assert (hw.lc[0], hw.len[0]) == (s.address + 32, 16)


def test_hold_and_decay_envelope():
    out, _, _ = trace(make_mmd({(0, 0, 0): (13, 1, 0, 0)}, [(SQUARE * 8, 64, 0, 0, 0, 3, 16)],
                               speed=1), 9)
    # full for 3 ticks, then down to silence over 64 / 16 = 4 more
    assert [v for _, v in out] == [64, 64, 64, 51, 38, 25, 12, 0, 0]


def test_hold_symbol_keeps_the_note_at_full_volume():
    sample = [(SQUARE * 8, 64, 0, 0, 0, 3, 16)]
    held = {(0, 0, 0): (13, 1, 0, 0), (0, 1, 0): (0, 1, 0, 0)}     # instrument alone: hold on
    out, _, _ = trace(make_mmd(held, sample, speed=4), 8)
    assert [v for _, v in out] == [64, 64, 64, 51, 64, 64, 64, 51]
    out, _, _ = trace(make_mmd({(0, 0, 0): (13, 1, 0, 0)}, sample, speed=4), 8)
    assert [v for _, v in out] == [64, 64, 64, 51, 38, 25, 12, 0]


def test_player_seek_and_length(tmp_path):
    cells = {(b, 0, 0): (13 + b, 1, 0, 0) for b in range(3)}
    cells[(1, 0, 1)] = (0, 0, 0x9, 2)
    path = tmp_path / "t.med"
    path.write_bytes(make_mmd(cells, [(SQUARE * 64, 64, 0, 0, len(SQUARE) * 64)],
                              orders=(0, 1, 2), lines=4))
    p = open_player(path)
    assert isinstance(p, ModPlayer) and p.tune.tag == "MMD0"
    x = p.render(44100)
    assert np.abs(x).max() > 0.1
    p.select(2)
    assert p.replayer.speed == 2 and p.replayer.now == (2, 0)
    length, kind = song_length(p.tune)
    assert kind == "loop" and length == pytest.approx((4 * 6 + 4 * 2 + 4 * 2) * 2.5 / 125, abs=1e-3)


# ---- synthetic instruments ---------------------------------------------------------------

def test_parse_synth_instrument():
    m = medfile.parse(make_mmd({}, [(synth([SAW, UP], b"\x20\xff", b"\x01\xff", (2, 3)), 64)]))
    s = m.samples[0]
    assert (s.kind, len(s.waveforms), s.volume_speed, s.waveform_speed) == ("synth", 2, 2, 3)
    assert s.volume_table == b"\x20\xff" and s.waveform_table == b"\x01\xff"
    (a0, n0), (a1, n1) = s.waveforms
    assert m.ram[a0:a0 + n0] == SAW and m.ram[a1:a1 + n1] == UP
    assert s.octaves == [(a0, 32, 0, 32)] and not m.warnings


def synth_trace(volume_table, waveform_table, ticks, note=37, hold=0, decay=0, speeds=(1, 1)):
    cells = {(0, 0, 0): (note, 1, 0, 0)}
    blob = make_mmd(cells, [(synth([SAW, UP], volume_table, waveform_table, speeds), 64, 0, 0, 0,
                             hold, decay)], speed=ticks)
    mod = medfile.parse(blob)
    hw = Recorder(mod)
    r = med.MED(mod, hw)
    out = []
    for _ in range(ticks):
        r.tick()
        out.append((hw.per.get(0), hw.vol.get(0), hw.lc.get(0)))
    return out, mod.samples[0]


def test_synth_volume_table():
    out, _ = synth_trace(b"\x20\xf1\x02\x30\xff", b"\xff", 5)
    assert [v for _, v, _ in out] == [32, 32, 32, 48, 48]            # set, wait 2 steps, set
    out, _ = synth_trace(b"\x10\xf3\x04\xff", b"\xff", 5)
    assert [v for _, v, _ in out] == [16, 16, 20, 24, 28]            # CHU: up 4 a step
    out, _ = synth_trace(b"\x10\xf3\x04\xff", b"\xff", 5, speeds=(2, 1))
    assert [v for _, v, _ in out] == [16, 16, 16, 16, 20]            # steps every 2 ticks


def test_synth_waveforms_and_arpeggio():
    out, s = synth_trace(b"\x40\xff", b"\x01\xf1\x02\x00\xff", 4)
    (a0, _), (a1, _) = s.waveforms
    assert [lc for _, _, lc in out] == [a1, a1, a1, a0]     # the loop registers: Paula switches at the end
    out, _ = synth_trace(b"\x40\xff", b"\xfc\x00\x0c\xfd\xff", 4)
    assert [p for p, _, _ in out] == [428, 214, 428, 214]  # C-4 on a synth is C-2; arpeggio 0, +12


def test_synth_hold_jumps_to_the_decay_part():
    out, _ = synth_trace(b"\x40\xff\x10\xff", b"\xff", 5, hold=2, decay=2)
    assert [v for _, v, _ in out] == [64, 64, 64, 16, 16]


# ---- output limiter ------------------------------------------------------------------

def test_soft_limit():
    x = np.linspace(-3, 3, 6001).astype(np.float32)
    y = soft_limit(x.copy())
    below = np.abs(x) <= KNEE
    np.testing.assert_array_equal(y[below], x[below])
    assert np.abs(y).max() <= 1.0 and np.all(np.diff(y) >= 0)
    assert np.abs(y[np.abs(x) < 1.5]).max() < 1.0
    step = 1e-4
    k = np.float32(KNEE + step)
    assert soft_limit(np.array([k]))[0] == pytest.approx(KNEE + step, abs=1e-6)   # slope 1 at the knee
