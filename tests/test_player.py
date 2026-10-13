from pathlib import Path

import numpy as np
import pytest

from pysid import psid
from pysid.player import Player, write_wav

TUNE = Path(__file__).resolve().parent.parent / "sids" / "Commando.sid"
pytestmark = pytest.mark.skipif(not TUNE.exists(), reason="sids/Commando.sid not present")


def test_header():
    t = psid.load(TUNE)
    assert (t.magic, t.version, t.load_address, t.init_address, t.play_address) == \
        ("PSID", 2, 0x5000, 0x5FB2, 0x5012)
    assert (t.songs, t.start_song, t.name, t.author) == (19, 1, "Commando", "Rob Hubbard")
    assert t.is_pal and t.preferred_model == "6581"


def test_render_levels_and_determinism():
    a = Player(TUNE, 1).render(44100 * 3)
    b = Player(TUNE, 1).render(44100 * 3)
    assert np.array_equal(a, b)
    assert not np.isnan(a).any()
    rms = np.sqrt(np.mean(a[44100:] ** 2))
    assert 0.03 < rms < 0.5
    assert np.abs(a).max() < 0.99
    assert abs(a[44100:].mean()) < 0.01          # DC removed by the output high-pass
    assert np.abs(a[:200]).max() < 0.05           # no power-on thump


def test_chunk_size_does_not_change_output():
    whole = Player(TUNE, 1).render(30000)
    p = Player(TUNE, 1)
    parts = np.concatenate([p.render(n) for n in (1, 999, 7000, 22000)])
    np.testing.assert_array_equal(whole, parts)


@pytest.mark.parametrize("song", range(1, 20))
def test_every_subtune_runs(song):
    p = Player(TUNE, song)
    x = p.render(44100)
    assert not p.warnings
    assert np.isfinite(x).all()


def test_8580_and_rates(tmp_path):
    for rate in (32000, 48000):
        x = Player(TUNE, 1, sample_rate=rate, model="8580").render(rate)
        assert len(x) == rate and np.sqrt(np.mean(x ** 2)) > 0.01
    write_wav(tmp_path / "x.wav", x, 48000)
    assert (tmp_path / "x.wav").stat().st_size == 44 + 2 * 48000


def test_song_length():
    from pysid.player import song_length
    tune = psid.load(TUNE)
    length, kind = song_length(tune, 2, max_seconds=200)
    assert kind == "loop" and length == pytest.approx(61.3, abs=0.1)
    # the main theme: its first frame differs from the loop's, so the
    # repetition must be found from a later offset and walked back
    length, kind = song_length(tune, 1, max_seconds=330)
    assert kind == "loop" and length == pytest.approx(235.6, abs=0.1)
    length, kind = song_length(tune, 3, max_seconds=60)       # a jingle that stops
    assert kind == "end" and length == pytest.approx(240 / 50.125 + 2.5, abs=0.05)
