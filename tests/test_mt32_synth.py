"""The MT-32 synthesiser: envelopes, wavetables, notes, parts and the player."""

from pathlib import Path

import numpy as np
import pytest

from numsid.mt32.synth import RATE, Envelope, wavetable

ROOT = Path(__file__).resolve().parent.parent
ROMS = ROOT / "roms"
THEME = ROOT / "midi" / "theme.mid"
needs_roms = pytest.mark.skipif(not (ROMS / "mt32_ctrl_1_07.rom").exists(),
                                reason="MT-32 ROMs not present in roms/")


def test_envelope_stages_hold_and_release():
    env = Envelope(0, [(100, 0.1), (50, 0.1)], (0, 0.2))
    assert env.run(0.05) == (0, pytest.approx(50))
    env.run(0.1)                                   # through the peak, halfway down
    assert env.value == pytest.approx(75)
    env.run(10)
    assert env.value == 50 and not env.finished    # holds while the key is down
    env.release()                                  # falls 100 steps in 0.2 s
    env.run(0.05)
    assert env.value == pytest.approx(25)
    env.run(1)
    assert env.value == 0 and env.finished


def test_envelope_without_sustain_releases_by_itself():
    env = Envelope(0, [(100, 0.01)], (0, 0.01), hold=False)
    env.run(1)
    assert env.finished and env.value == 0


def test_measured_curves():
    from numsid.mt32.synth import edge_samples, env_time, level_db, velocity_db
    assert level_db(100) == 0 and level_db(50) == pytest.approx(-14.3)
    assert velocity_db(50, 1) == 0                 # 50: no velocity sensitivity
    assert velocity_db(100, 64) == pytest.approx(-18.9)
    assert velocity_db(0, 127) < velocity_db(0, 1)  # below 50 it works backwards
    assert env_time(50) == pytest.approx(0.276)
    assert env_time(80) == pytest.approx(2 ** (10 / 8) * env_time(70), rel=0.05)
    assert edge_samples(100) == pytest.approx(8.4) and edge_samples(0) == pytest.approx(558.2)


def test_wavetable_is_band_limited():
    for saw in (False, True):
        for f0 in (55.0, 440.0, 3000.0):
            top = int(RATE / 2 / f0)
            t = wavetable(saw, 0.5, 8.4 * f0 / RATE, 0, top)[:-1]
            spec = np.abs(np.fft.rfft(t))
            assert spec[top + 1:].max() < 1e-6 * spec.max()


def test_wavetable_edges_and_resonance():
    # open: a square with sharp edges; closed: the edges fill the half
    # period, leaving a quieter sine
    square = wavetable(False, 0.5, 0.01, 0, 100)[:-1]
    assert np.abs(square).max() == pytest.approx(1, abs=0.1)
    closed = wavetable(False, 0.5, 1.0, 0, 100)[:-1]
    spec = np.abs(np.fft.rfft(closed))
    assert spec[3] < spec[1] * 0.01
    assert np.abs(closed).max() == pytest.approx(0.5 ** 1.9, rel=0.05)
    # resonance rings at the edge rate: half a cycle per edge length
    res = np.abs(np.fft.rfft(wavetable(False, 0.5, 0.05, 30, 100)[:-1]))
    plain = np.abs(np.fft.rfft(wavetable(False, 0.5, 0.05, 0, 100)[:-1]))
    assert res[9] > plain[9] * 2


@pytest.fixture(scope="module")
def rom():
    from numsid.mt32 import control, pcm, roms
    info, cblob, _, pblob = roms.find("MT-32", ROMS)
    return control.parse(cblob, info), pcm.PCMROM(pblob, cblob, info)


def _synth(rom, **kw):
    from numsid.mt32.synth import MT32
    return MT32(*rom, 44100, **kw)


@needs_roms
def test_note_sounds_and_dies_away(rom):
    s = _synth(rom, gm=True)
    s.message(0xC1, bytes([75]))                   # pan flute -> Panpipes
    s.message(0x91, bytes([60, 100]))
    s.advance(RATE // 2)
    on = s.take(s.out_len)
    assert np.sqrt(np.mean(on ** 2)) > 0.01
    s.message(0x81, bytes([60, 0]))
    s.advance(RATE * 30)
    s.take(s.out_len)
    assert not s.parts[0].notes                    # released and finished


@needs_roms
def test_drums_and_partial_limit(rom):
    from numsid.mt32.synth import PARTIALS
    s = _synth(rom, gm=True)
    s.message(0x99, bytes([36, 127]))
    s.advance(RATE // 4)
    assert np.abs(s.take(s.out_len)).max() > 0.05
    for key in range(40, 90):                      # far more notes than partials
        s.message(0x91, bytes([key, 100]))
        s.message(0x92, bytes([key, 100]))
    s.advance(256)
    assert sum(n.count for p in s.parts for n in p.notes) <= PARTIALS


@needs_roms
def test_sustain_pedal_holds_notes(rom):
    s = _synth(rom, gm=True)
    s.message(0xB1, bytes([64, 127]))
    s.message(0x91, bytes([60, 100]))
    s.message(0x81, bytes([60, 0]))
    s.advance(RATE)
    assert s.parts[0].notes and not s.parts[0].notes[0].released
    s.message(0xB1, bytes([64, 0]))
    assert s.parts[0].notes[0].released


def _sysex(addr, data):
    cs = (128 - (sum(addr) + sum(data)) % 128) % 128
    return bytes([0x41, 0x10, 0x16, 0x12, *addr, *data, cs, 0xF7])


@needs_roms
def test_sysex_writes_timbres_patches_and_system(rom):
    s = _synth(rom)
    name = b"TEST TONE "
    s.message(0xF0, _sysex([4, 0, 0], name))       # part 1's timbre temp: its name
    assert s.parts[0].timbre.name == "TEST TONE"
    s.message(0xF0, _sysex([3, 0, 16 + 1], [5]))   # part 2 plays timbre A-6
    assert s.parts[1].timbre.name == rom[0].bank_a[5].name
    s.message(0xF0, _sysex([0x10, 0, 2], [0]))     # reverb level 0
    assert s.reverb.level == 0
    s.message(0xF0, _sysex([0x10, 0, 0x16], [50]))  # master volume
    assert s.master_db == pytest.approx(-14.3)


@needs_roms
def test_native_files_use_the_patch_memory(rom):
    s = _synth(rom)
    s.message(0xC1, bytes([8]))
    assert s.parts[0].timbre.name == rom[0].patch(8).name
    s = _synth(rom, gm=True)
    s.message(0xC1, bytes([8]))                    # GM 8 is a celesta
    assert s.parts[0].timbre.name != rom[0].patch(8).name


@needs_roms
@pytest.mark.skipif(not THEME.exists(), reason="midi/theme.mid not present")
def test_theme_plays_and_is_deterministic():
    from numsid.tunes import open_player
    a = open_player(THEME).render(44100 * 8)
    p = open_player(THEME)
    b = np.concatenate([p.render(n) for n in (1000, 77777, 44100 * 8 - 78777)])
    assert np.array_equal(a, b)                    # chunk size doesn't matter
    assert np.abs(a[44100 * 4:]).max() > 0.05      # notes start at 3.5 s
    assert not a[:44100 * 3].any()
    p = open_player(THEME, 10)                     # start 10 s in
    assert p.readout(None).position == "0:10.0"
    assert np.abs(p.render(44100)).max() > 0.01


def test_reverb_tail_and_blocks():
    from numsid.mt32.reverb import HALL, ROOM, TAP_DELAY, Reverb
    for mode in (ROOM, HALL, TAP_DELAY):
        r = Reverb(mode, 5, 7)
        imp = np.zeros(128, dtype=np.float32)
        imp[0] = 1.0
        outs = [r.process(imp, imp)[0]]
        silence = np.zeros(128, dtype=np.float32)
        outs += [r.process(silence, silence)[0] for _ in range(400)]
        y = np.concatenate(outs)
        assert not y[:200].any()                       # no direct sound, delays first
        assert np.abs(y[3000:16000]).max() > 1e-3      # a tail
        assert np.abs(y[-4000:]).max() < np.abs(y[3000:16000]).max()   # that decays
    assert not Reverb(ROOM, 5, 0).process(imp, imp)[0].any()          # level 0: dry
