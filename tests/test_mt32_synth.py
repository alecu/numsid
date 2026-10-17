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
    env.release()
    env.run(0.1)
    assert env.value == pytest.approx(25)
    env.run(1)
    assert env.value == 0 and env.finished


def test_envelope_without_sustain_releases_by_itself():
    env = Envelope(0, [(100, 0.01)], (0, 0.01), hold=False)
    env.run(1)
    assert env.finished and env.value == 0


def test_wavetable_is_band_limited():
    for saw in (False, True):
        for f0 in (55.0, 440.0, 3000.0):
            t = wavetable(saw, 0, f0, 20000.0, 0)[:-1]
            spec = np.abs(np.fft.rfft(t))
            top = int(RATE / 2 / f0)
            assert spec[top + 1:].max() < 1e-6 * spec.max()


def test_wavetable_filter_and_resonance():
    open_ = np.abs(np.fft.rfft(wavetable(True, 0, 110.0, 20000.0, 0)[:-1]))
    closed = np.abs(np.fft.rfft(wavetable(True, 0, 110.0, 440.0, 0)[:-1]))
    assert closed[20] < open_[20] * 0.05           # well above the cutoff: cut
    res = np.abs(np.fft.rfft(wavetable(True, 0, 110.0, 440.0, 30)[:-1]))
    assert res[4] > closed[4] * 3                  # at the cutoff: resonant peak


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
