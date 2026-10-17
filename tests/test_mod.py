import random
import struct
from pathlib import Path

import numpy as np
import pytest

from numsid.amiga import modfile, protracker
from numsid.amiga.modfile import ModFileError
from numsid.amiga.paula import BIN, Channel, _lowpass_biquad
from numsid.amiga.player import ModPlayer
from numsid.amiga.protracker import PERIODS, NullPaula, ProTracker
from numsid.tunes import load_tune, open_player, song_length
from reference_paula import RefChannel

AXELF = Path(__file__).resolve().parent.parent / "mods" / "axelf.mod"
needs_axelf = pytest.mark.skipif(not AXELF.exists(), reason="mods/axelf.mod not present")

SQUARE = bytes([100] * 16 + [156] * 16)          # one cycle: +100, -100 (int8)


def make_mod(cells: dict, samples: list, orders=(0,), *, tag: bytes = b"M.K.",
             channels: int = 4, title: bytes = b"test") -> bytes:
    """A 31-sample module.  cells: {(pattern, row, channel): (period, sample,
    effect, param)}; samples: (data, volume, finetune, loop start, loop
    length) with byte lengths."""
    head = title.ljust(20, b"\0")
    for i in range(31):
        if i < len(samples):
            data, vol, ft, ls, ll = samples[i]
            head += f"s{i + 1}".encode().ljust(22, b"\0")
            head += struct.pack(">HBBHH", len(data) // 2, ft & 15, vol, ls // 2, max(ll // 2, 1))
        else:
            head += bytes(22) + struct.pack(">HBBHH", 0, 0, 0, 0, 1)
    table = list(orders) + [0] * (128 - len(orders))
    head += bytes([len(orders), 127]) + bytes(table) + tag
    npat = max(orders) + 1
    pats = bytearray(npat * 64 * channels * 4)
    for (p, r, c), (period, ins, eff, par) in cells.items():
        o = ((p * 64 + r) * channels + c) * 4
        pats[o:o + 4] = bytes([(ins & 0xF0) | period >> 8, period & 0xFF,
                               (ins & 15) << 4 | eff, par])
    return head + bytes(pats) + b"".join(s[0] for s in samples)


class Recorder(NullPaula):
    """Records what the replayer writes, per tick."""

    def __init__(self, mod):
        super().__init__(mod)
        self.tick = 0
        self.per, self.vol, self.lc, self.len = {}, {}, {}, {}
        self.triggers = []
        self.led = None

    def write_period(self, ch, period):
        self.per[ch] = period & 0xFFFF

    def write_volume(self, ch, volume):
        self.vol[ch] = volume

    def write_location(self, ch, address):
        self.lc[ch] = address

    def write_length(self, ch, words):
        self.len[ch] = words

    def dma(self, mask, on):
        if on:
            self.triggers += [(self.tick, ch, self.lc.get(ch)) for ch in range(4) if mask >> ch & 1]

    def set_led(self, on):
        self.led = on


def trace(blob: bytes, ticks: int, ch: int = 0):
    mod = modfile.parse(blob)
    hw = Recorder(mod)
    pt = ProTracker(mod, hw)
    out = []
    for i in range(ticks):
        hw.tick = i
        pt.tick()
        out.append((hw.per.get(ch), hw.vol.get(ch)))
    return out, hw, pt


def one_row(*cells, samples=None, more=None):
    """A module whose pattern 0 row 0 holds `cells` (channel 0, 1, ...)."""
    d = {(0, 0, c): cell for c, cell in enumerate(cells)}
    d.update(more or {})
    return make_mod(d, samples or [(SQUARE, 64, 0, 0, 32)])


# ---- file format --------------------------------------------------------------------

def test_parse_31_sample_module():
    blob = make_mod({(0, 0, 0): (428, 1, 0, 0)},
                    [(bytes([9, 9]) + bytes(98), 48, 0x0F, 0, 2), (SQUARE, 64, 3, 0, 32)],
                    orders=(0, 0))
    m = modfile.parse(blob)
    assert (m.title, m.tag, m.channels, m.orders, len(m.patterns)) == ("test", "M.K.", 4, [0, 0], 1)
    s1, s2 = m.samples[:2]
    assert (s1.length, s1.volume, s1.finetune, s1.looped) == (100, 48, 15, False)
    assert (s2.length, s2.looped, s2.loop_start, s2.loop_length) == (32, True, 0, 32)
    ram = np.frombuffer(m.ram, np.int8)
    assert list(ram[s1.address:s1.address + 2]) == [0, 0]       # idle loop made silent
    assert bytes(ram[s2.address:s2.address + 32].tobytes()) == SQUARE
    assert m.patterns[0][0][0] == (428, 1 << 12)
    assert len(m.samples) == 31 and not m.samples[5].length


def test_parse_multichannel_and_errors():
    m = modfile.parse(make_mod({(0, 3, 5): (214, 1, 0xC, 0x20)}, [(SQUARE, 64, 0, 0, 32)],
                               tag=b"6CHN", channels=6))
    assert m.channels == 6 and m.patterns[0][3][5] == (214, 1 << 12 | 0xC20)
    with pytest.raises(ModFileError):
        modfile.parse(bytes(range(256)) * 10)
    with pytest.raises(ValueError):
        load_tune(Path(__file__))                 # a Python file is neither SID nor MOD


def test_loop_running_past_the_end_is_padded():
    m = modfile.parse(make_mod({}, [(SQUARE, 64, 0, 16, 32)]))
    s = m.samples[0]
    assert (s.length, s.loop_start, s.loop_length) == (48, 16, 32)
    ram = np.frombuffer(m.ram, np.int8)
    assert not ram[s.address + 32:s.address + 48].any()


def make_mod15(cells: dict, samples: list, orders=(0,), tempo: int = 120) -> bytes:
    """A 15-sample SoundTracker module; loop starts are in bytes."""
    head = b"st".ljust(20, b"\0")
    for i in range(15):
        if i < len(samples):
            data, vol, ls, ll = samples[i]
            head += b"x".ljust(22, b"\0") + struct.pack(">HBBHH", len(data) // 2, 0, vol, ls,
                                                         max(ll // 2, 1))
        else:
            head += bytes(22) + struct.pack(">HBBHH", 0, 0, 0, 0, 1)
    head += bytes([len(orders), tempo]) + bytes(list(orders) + [0] * (128 - len(orders)))
    pats = bytearray((max(orders) + 1) * 1024)
    for (p, r, c), (period, ins, eff, par) in cells.items():
        o = (p * 64 + r) * 16 + c * 4
        pats[o:o + 4] = bytes([period >> 8, period & 0xFF, ins << 4 | eff, par])
    return head + bytes(pats) + b"".join(s[0] for s in samples)


def test_soundtracker_15_samples():
    data = bytes(range(64))
    blob = make_mod15({(0, 0, 0): (428, 1, 1, 0x37)}, [(data, 64, 16, 32)], tempo=100)
    m = modfile.parse(blob)
    assert (m.tag, len(m.samples), m.channels) == ("", 15, 4)
    s = m.samples[0]
    # only the loop of a looped sample is kept, starting at the loop
    assert (s.length, s.loop_start, s.loop_length) == (32, 0, 32)
    assert m.ram[s.address:s.address + 32] == data[16:48]
    assert m.patterns[0][0][0][1] & 0xFFF == 0x037          # old 1xy arpeggio -> 0xy
    assert m.cia_period == (240 - 100) * 122


# ---- Paula ------------------------------------------------------------------------------

def test_paula_channel_matches_reference():
    """Random register sequences, rendered by the vectorised channel and by a
    colour-clock-stepped model, must agree exactly."""
    for seed in range(60):
        rnd = random.Random(seed)
        ram = np.array([rnd.randrange(-128, 128) for _ in range(600)], dtype=np.int8)
        ch, ref = Channel(), RefChannel()
        got, want = [], []
        for _ in range(rnd.randrange(3, 10)):
            for _ in range(rnd.randrange(0, 4)):
                what = rnd.choice(["lc", "len", "per", "vol", "on", "off", "retrig"])
                if what == "lc":
                    ch.lc = ref.lc = rnd.randrange(0, 500, 2)
                elif what == "len":
                    ch.len = ref.len = rnd.choice([1, 2, 3, 7, 40, 0])
                elif what == "per":
                    ch.per = ref.per = rnd.choice([113, 124, 200, 214, 428, 1000, 0, 50, 117])
                elif what == "vol":
                    ch.vol = ref.vol = rnd.randrange(0, 65)
                elif what == "on":
                    if not ch.dma:
                        ch.start()
                    ref.start()
                elif what == "off":
                    ch.dma = False
                    ref.stop()
                else:
                    ch.dma = False
                    ref.stop()
                    ch.start()
                    ref.start()
            n = rnd.choice([1, 3, 17, 60, 211, 1000])
            r = ch.render(ram, n)
            got += [0] * n if r is None else list(r)
            want += ref.run(ram, n)
        assert got == want, f"seed {seed}"


def test_led_filter_blocks_match_direct_recursion():
    bq = _lowpass_biquad(3090.53, 0.66, 44100)
    x = np.random.default_rng(1).standard_normal(1000)
    w0 = 2 * np.pi * 3090.53 / 44100
    al, c = np.sin(w0) / (2 * 0.66), np.cos(w0)
    b = np.array([(1 - c) / 2, 1 - c, (1 - c) / 2]) / (1 + al)
    a1, a2 = -2 * c / (1 + al), (1 - al) / (1 + al)
    want, x1, x2, y1, y2 = [], 0.0, 0.0, 0.0, 0.0
    for v in x:
        y = b[0] * v + b[1] * x1 + b[2] * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, v, y1, y
        want.append(y)
    state, got = np.zeros(2), []
    for part in (x[:300], x[300:301], x[301:]):
        y, state = bq.run(part, state)
        got.append(y)
    np.testing.assert_allclose(np.concatenate(got), want, atol=1e-12)


def _spectrum(x, rate=44100):
    f = np.fft.rfftfreq(len(x), 1 / rate)
    return f, np.abs(np.fft.rfft(x * np.hanning(len(x))))


def test_pitch_and_hard_panning():
    # C-2 (period 428) on a 32-byte one-cycle loop: 3546895 / 428 / 32 Hz
    p = ModPlayer(modfile.parse(one_row((428, 1, 0, 0))))
    x = p.render(44100 * 3)[44100:]
    assert x.shape == (88200, 2)
    f, mag = _spectrum(x[:, 0].astype(np.float64))
    assert f[np.argmax(mag)] == pytest.approx(3546895 / 428 / 32, abs=0.6)
    assert np.abs(x[:, 1]).max() < 1e-6                 # channel 1 is hard left
    assert 0.3 < np.sqrt(np.mean(x[:, 0] ** 2)) < 0.6
    # the right side: channels 2 and 3 (0-based 1, 2)
    p = ModPlayer(modfile.parse(one_row((0, 0, 0, 0), (428, 1, 0, 0))))
    x = p.render(44100)
    assert np.abs(x[:, 0]).max() < 1e-6 and np.abs(x[:, 1]).max() > 0.2


def test_separation_zero_is_mono():
    p = ModPlayer(modfile.parse(one_row((428, 1, 0, 0))), separation=0.0)
    x = p.render(22050)
    np.testing.assert_allclose(x[:, 0], x[:, 1])


def test_output_filters():
    def hf(blob, **kw):
        x = ModPlayer(modfile.parse(blob), **kw).render(44100 * 2)[22050:, 0]
        f, mag = _spectrum(x.astype(np.float64))
        return (mag[f > 6000] ** 2).sum() / (mag ** 2).sum()
    plain = one_row((214, 1, 0, 0))
    led = one_row((214, 1, 0xE, 0x00))                   # E00: LED filter on
    a500, a1200 = hf(plain), hf(plain, model="A1200")
    assert a1200 > 3 * a500                              # the A500's 4.4 kHz RC low-pass
    assert hf(led) < a500 / 3                            # the LED filter on top
    assert hf(led, filter_enabled=False) == pytest.approx(a500, rel=1e-6)


def test_render_is_deterministic_and_chunk_independent():
    blob = make_mod({(0, 0, 0): (428, 1, 4, 0x48), (0, 0, 1): (285, 1, 0, 0x37),
                     (0, 8, 2): (214, 1, 0xA, 0x02), (0, 16, 3): (170, 1, 0xE, 0x00)},
                    [(SQUARE, 64, 0, 0, 32)])
    whole = ModPlayer(modfile.parse(blob)).render(30000)
    p = ModPlayer(modfile.parse(blob))
    parts = np.concatenate([p.render(n) for n in (1, 999, 7000, 22000)])
    np.testing.assert_array_equal(whole, parts)
    np.testing.assert_array_equal(whole, ModPlayer(modfile.parse(blob)).render(30000))


# ---- the replayer ---------------------------------------------------------------------

def test_tick_timing_and_tempo_change_one_tick_late():
    blob = one_row((428, 1, 0xF, 150))
    mod = modfile.parse(blob)
    pt = ProTracker(mod, NullPaula(mod))
    times = []
    for _ in range(4):
        times.append(pt.time)
        pt.tick()
    steps = np.diff(times + [pt.time])
    assert steps[0] == 1773447 // 125 + 1                # the F96 tick keeps the old rate
    assert list(steps[1:]) == [1773447 // 150 + 1] * 3


def test_arpeggio():
    out, *_ = trace(one_row((428, 1, 0x0, 0x37)), 6)
    assert [p for p, _ in out] == [428, 360, 285, 428, 360, 285]


def test_arpeggio_on_finetune_minus_one_reads_past_the_table():
    out, *_ = trace(one_row((113, 1, 0x0, 0xF0), samples=[(SQUARE, 64, 15, 0, 32)]), 2)
    assert out[1][0] == PERIODS[15 * 37 + 35 + 15]       # an overflow value


def test_portamento_clamps():
    out, *_ = trace(one_row((120, 1, 0x1, 0x05)), 6)
    assert [p for p, _ in out] == [120, 115, 113, 113, 113, 113]
    # C-1 on a finetune +1 sample is period 850
    out, *_ = trace(one_row((856, 1, 0x2, 0x04), samples=[(SQUARE, 64, 1, 0, 32)]), 4)
    assert [p for p, _ in out] == [850, 854, 856, 856]


def test_tone_portamento_reaches_its_note():
    blob = make_mod({(0, 0, 0): (428, 1, 0, 0), (0, 1, 0): (404, 0, 3, 0x08)},
                    [(SQUARE, 64, 0, 0, 32)])
    out, hw, _ = trace(blob, 12)
    assert [p for p, _ in out[6:]] == [428, 420, 412, 404, 404, 404]
    assert len(hw.triggers) == 1                         # 3xx doesn't retrigger


def test_vibrato_follows_the_sine_table():
    out, *_ = trace(one_row((428, 1, 4, 0x48)), 6)       # speed 4, depth 8
    pos = [0, 16, 32, 48, 64]                            # position advances 4 * speed
    want = [428 + (protracker.VIBRATO[(q >> 2) & 31] * 8 >> 7) for q in pos]
    assert [p for p, _ in out[1:]] == want


def test_volume_commands():
    out, *_ = trace(one_row((428, 1, 0xA, 0x02)), 6)     # slide down 2 per tick
    assert [v for _, v in out] == [64, 62, 60, 58, 56, 54]
    out, *_ = trace(one_row((428, 1, 0xC, 0x50)), 2)     # Cxx clamps at 64
    assert out[0][1] == 64
    out, *_ = trace(one_row((428, 1, 0xE, 0xC2)), 4)     # note cut on tick 2
    assert [v for _, v in out] == [64, 64, 0, 0]
    out, *_ = trace(one_row((428, 1, 0xE, 0xB5)), 3)     # fine slide, tick 0 only
    assert [v for _, v in out] == [59, 59, 59]
    out, _, pt = trace(one_row((428, 1, 0x7, 0x84), samples=[(SQUARE, 32, 0, 0, 32)]), 3)
    assert [v for _, v in out] == [32, 32, 32 + (180 * 4 >> 6)]    # tremolo: sine * depth
    assert pt.voices[0].volume == 32                     # ... around the kept volume


def test_note_delay_and_retrigger():
    _, hw, _ = trace(one_row((428, 1, 0xE, 0xD3)), 6)
    assert [t for t, ch, _ in hw.triggers] == [3]
    _, hw, _ = trace(one_row((428, 1, 0xE, 0x92)), 6)
    assert [t for t, ch, _ in hw.triggers] == [0, 2, 4]


def test_sample_offset_is_applied_twice():
    data = bytes(1024)
    blob = make_mod({(0, 0, 0): (428, 1, 9, 0x01), (0, 1, 0): (0, 0, 0xE, 0x91)},
                    [(data, 64, 0, 0, 2)])
    _, hw, pt = trace(blob, 7)
    start = modfile.parse(blob).samples[0].address
    assert hw.triggers[0][2] == start + 256             # the note: offset once
    assert hw.triggers[1][2] == start + 512             # a retrigger: offset twice


def test_instrument_alone_swaps_the_sample_at_its_end():
    blob = make_mod({(0, 0, 0): (428, 1, 0, 0), (0, 1, 0): (0, 2, 0, 0)},
                    [(SQUARE, 64, 0, 0, 32), (bytes(range(32)), 20, 0, 0, 32)])
    mod = modfile.parse(blob)
    out, hw, _ = trace(blob, 7)
    assert len(hw.triggers) == 1                         # no new trigger
    assert hw.lc[0] == mod.samples[1].address            # but Paula's latches point at it
    assert out[6][1] == 20                               # and its volume is set


def test_flow_commands():
    def rows(blob, ticks):
        mod = modfile.parse(blob)
        pt = ProTracker(mod, NullPaula(mod))
        seen = []
        for _ in range(ticks):
            pt.tick()
            if pt.row_read:
                seen.append(pt.row_read)
        return seen
    s = [(SQUARE, 64, 0, 0, 32)]
    # Dxx: pattern break to row 10 of the next position (decimal parameter)
    seen = rows(make_mod({(0, 1, 0): (0, 0, 0xD, 0x10)}, s, orders=(0, 1)), 18)
    assert seen == [(0, 0), (0, 1), (1, 10)]
    # Bxx: jump to position 2
    seen = rows(make_mod({(0, 0, 0): (0, 0, 0xB, 0x02)}, s, orders=(0, 0, 1)), 12)
    assert seen == [(0, 0), (2, 0)]
    # E6x: rows 1-2 play three times
    seen = rows(make_mod({(0, 1, 0): (0, 0, 0xE, 0x60), (0, 2, 0): (0, 0, 0xE, 0x62)}, s), 6 * 8)
    assert [r for _, r in seen] == [0, 1, 2, 1, 2, 1, 2, 3]
    # EEx: the row lasts (x + 1) times as long
    mod = modfile.parse(make_mod({(0, 0, 0): (0, 0, 0xE, 0xE2)}, s))
    pt = ProTracker(mod, NullPaula(mod))
    for _ in range(18):
        pt.tick()
        assert pt.row_read in (None, (0, 0))
    pt.tick()
    assert pt.row_read == (0, 1)
    # Fxx < 32: speed
    seen = rows(make_mod({(0, 0, 0): (0, 0, 0xF, 0x02)}, s), 6)
    assert seen == [(0, 0), (0, 1), (0, 2)]


def test_led_filter_command_and_funk_repeat():
    _, hw, pt = trace(one_row((428, 1, 0xE, 0x00)), 2)
    assert hw.led is True and pt.led
    blob = one_row((428, 1, 0xE, 0xFF))                  # EFF: fastest funk
    mod = modfile.parse(blob)
    _, hw, _ = trace(blob, 6)
    a = mod.samples[0].address
    assert (hw.ram[a:a + 32] != np.frombuffer(SQUARE, np.int8)).any()


def test_song_length_loop_and_end():
    s = [(SQUARE, 64, 0, 0, 32)]
    tick = (1773447 // 125 + 1) / 709379
    # positions 0, 1, then B01 jumps back to position 1: intro + one loop
    blob = make_mod({(1, 63, 0): (0, 0, 0xB, 0x01)}, s, orders=(0, 1))
    assert song_length(modfile.parse(blob)) == (pytest.approx(128 * 6 * tick), "loop")
    blob = make_mod({(0, 20, 0): (0, 0, 0xF, 0x00)}, s)  # F00 stops the song
    length, kind = song_length(modfile.parse(blob))
    assert kind == "end" and length == pytest.approx(21 * 6 * tick - 5 * tick + 2.5, abs=1e-6)
    # pattern loops don't count as the song repeating
    blob = make_mod({(0, 1, 0): (0, 0, 0xE, 0x60), (0, 2, 0): (0, 0, 0xE, 0x62)}, s)
    assert song_length(modfile.parse(blob)) == (pytest.approx(68 * 6 * tick), "loop")


def test_seek_keeps_speed_and_tempo():
    s = [(SQUARE, 64, 0, 0, 32)]
    blob = make_mod({(0, 0, 0): (0, 0, 0xF, 0x03), (0, 1, 0): (0, 0, 0xF, 150)}, s,
                    orders=(0, 1, 1))
    p = ModPlayer(modfile.parse(blob), 2)
    assert (p.replayer.pos, p.replayer.speed, p.replayer.bpm) == (2, 3, 150)
    p.render(4410)
    assert p.position == 2
    p.skip(+1)
    assert p.position == 0


def test_open_player_and_readout():
    p = open_player(modfile.parse(one_row((428, 1, 0, 0))), scope_rate=44100)
    assert isinstance(p, ModPlayer) and p.voices == 4 and p.channels == 2
    p.render(44100)
    ro = p.readout(p.state_at(0.5))
    assert ro.position.startswith("pos 00/01")
    assert ro.voices[0][0].startswith("C-2") and ro.levels[0] == 1.0
    assert "SPEED 6  BPM 125" in ro.chip[1]
    p.set_model("A1200")
    assert p.model == "A1200" and p.paula.model == "A1200"


# ---- axelf.mod ----------------------------------------------------------------------------

@needs_axelf
def test_axelf_header():
    m = load_tune(AXELF)
    assert (m.title, m.tag, len(m.samples), len(m.orders), len(m.patterns)) == \
        ("datafetch3", "", 15, 83, 33)
    assert [s.name for s in m.samples[:3]] == ["hallbrass", "digdug", "nice"]
    assert not m.warnings


@needs_axelf
def test_axelf_length_and_render():
    m = load_tune(AXELF)
    length, kind = song_length(m)
    assert kind == "loop" and length == pytest.approx(83 * 64 * 6 * 14188 / 709379)
    x = ModPlayer(m).render(44100 * 8)
    assert x.shape == (44100 * 8, 2) and np.isfinite(x).all()
    rms = np.sqrt(np.mean(x[44100:] ** 2, axis=0))
    assert (0.03 < rms).all() and (rms < 0.4).all()
    assert np.abs(x).max() < 0.99
    assert np.abs(x[44100:].mean(axis=0)).max() < 0.01
