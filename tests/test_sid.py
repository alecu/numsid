import random

import numpy as np
import pytest

from pysid.sid import SID, noise_table, NOISE_PERIOD, RATE_PERIOD
from reference_sid import run_reference, RefVoice


def render_capture(writes, n, chunks=4, seed=0):
    sid = SID(985248, 44100, "6581")
    sid.capture = []
    for t, reg, value in sorted(writes):
        sid.write(reg, value, t)
    rng = random.Random(seed)
    cuts = sorted(rng.sample(range(1, n), chunks - 1)) + [n]
    for c in cuts:                         # render in uneven pieces
        sid.advance(c)
    accs = [np.concatenate([span[i][0] for span in sid.capture]) for i in range(3)]
    envs = [np.concatenate([span[i][1] for span in sid.capture]) for i in range(3)]
    outs = [[span[i][2] for span in sid.capture] for i in range(3)]
    return sid, accs, envs, outs


def random_writes(rng, n, count, waveforms=(0, 1, 2, 4, 8), allow_sync=True, allow_test=True):
    writes = []
    for _ in range(count):
        t = rng.randrange(n)
        voice = rng.randrange(3)
        reg = rng.randrange(7)
        if reg == 4:
            value = rng.choice(waveforms) << 4 | rng.getrandbits(1)          # gate
            if allow_sync and rng.random() < 0.3:
                value |= 2
            if rng.random() < 0.3:
                value |= 4
            if allow_test and rng.random() < 0.1:
                value |= 8
        elif reg in (5, 6):
            value = rng.randrange(4) << 4 | rng.randrange(6)   # fast rates
            if reg == 6:
                value = rng.randrange(16) << 4 | rng.randrange(6)
        else:
            value = rng.randrange(256)
        writes.append((t, voice * 7 + reg, value))
    return writes


@pytest.mark.parametrize("seed", range(6))
def test_matches_cycle_reference(seed):
    rng = random.Random(seed)
    n = 45000
    writes = random_writes(rng, n, 160, allow_sync=seed % 2 == 0)
    ref = run_reference(writes, n)
    sid, accs, envs, outs = render_capture(writes, n, seed=seed)
    for i in range(3):
        ref_acc, ref_env, ref_wave = (np.array(x) for x in ref[i])
        bad = np.nonzero(accs[i] != ref_acc)[0]
        assert len(bad) == 0, f"voice {i} acc differs first at cycle {bad[0]}"
        bad = np.nonzero(envs[i] != ref_env)[0]
        assert len(bad) == 0, f"voice {i} env differs first at cycle {bad[0]}"
        expected = (ref_wave - sid.wave_zero) * ref_env
        pos = 0
        for span_out, span_len in zip(outs[i], [len(s[i][0]) for s in sid.capture]):
            if span_out is not None:
                np.testing.assert_array_equal(span_out, expected[pos:pos + span_len])
            else:
                assert not ref_env[pos:pos + span_len].any()
            pos += span_len


def test_envelope_adsr_bug_and_long_rates():
    """Slow rates, rate period lowered below the counter (wrap-around delay),
    sustain changes and release from mid-attack."""
    n = 400000
    w = [(0, 5, 0xA9), (0, 6, 0x5A), (10, 4, 0x11),          # slow attack
         (60000, 5, 0x09),                                  # attack period lowered mid-count
         (90000, 4, 0x10),                                  # release from attack
         (150000, 6, 0x00), (150010, 4, 0x11), (200000, 6, 0xF2),
         (300000, 4, 0x10)]
    ref = run_reference(w, n)
    _, accs, envs, _ = render_capture(w, n, chunks=7)
    np.testing.assert_array_equal(envs[0], np.array(ref[0][1]))


def test_noise_table_matches_lfsr():
    table = noise_table()
    v = RefVoice()
    for i in range(5000):
        assert int(table[i]) << 4 == v.noise(), i
        bit0 = ((v.shift >> 22) ^ (v.shift >> 17)) & 1
        v.shift = ((v.shift << 1) & 0x7FFFFF) | bit0
    assert len(table) == NOISE_PERIOD


def test_pitch():
    """A sawtooth at register value F must sound at F * clock / 2^24 Hz."""
    sid = SID(985248, 44100, "8580")
    freq = 0x1CD6                         # A-4 (440 Hz) in PAL note tables
    for reg, value in ((0, freq & 0xFF), (1, freq >> 8), (5, 0x00), (6, 0xF0),
                       (0x18, 0x0F), (4, 0x21)):
        sid.write(reg, value, 0)
    sid.advance(985248 * 2)
    x = sid.take(88200)[44100:].astype(np.float64)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    peak = np.argmax(spec[20:]) + 20
    # parabolic interpolation of the peak bin
    a, b, c = np.log(spec[peak - 1:peak + 2])
    hz = (peak + 0.5 * (a - c) / (a - 2 * b + c)) * 44100 / len(x)
    assert abs(hz - freq * 985248 / 2 ** 24) < 0.05


def test_rate_periods_table():
    assert RATE_PERIOD[0] == 9 and RATE_PERIOD[15] == 31251


def _filtered_noise_spectrum(mode, res=0, fc=768):
    sid = SID(985248, 44100, "6581")
    for reg, value in ((0, 0xFF), (1, 0xFF), (5, 0), (6, 0xF0), (0x15, fc & 7), (0x16, fc >> 3),
                       (0x17, res << 4 | 1), (0x18, mode | 0x0F), (4, 0x81)):
        sid.write(reg, value, 0)
    sid.advance(985248 * 2)
    x = sid.take(88200)[22050:22050 + 4410 * 12].astype(np.float64).reshape(12, 4410)
    spec = np.abs(np.fft.rfft(x * np.hanning(4410), axis=1)).mean(axis=0)
    f = np.fft.rfftfreq(4410, 1 / 44100)
    return lambda hz: 20 * np.log10(spec[np.argmin(abs(f - hz))] / spec.max())


def test_filter_modes():
    lp = _filtered_noise_spectrum(0x10)     # 6581 FC=768 -> ~1.6 kHz
    assert lp(8000) < lp(800) - 25
    bp = _filtered_noise_spectrum(0x20)
    assert bp(1600) > bp(200) + 10 and bp(1600) > bp(8000) + 10
    hp = _filtered_noise_spectrum(0x40)
    assert hp(200) < hp(8000) - 25
    resonant = _filtered_noise_spectrum(0x10, res=15)
    assert resonant(1600) - resonant(400) > lp(1600) - lp(400) + 3
