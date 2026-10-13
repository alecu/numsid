import numpy as np

from numsid.c64 import C64
from numsid.cia import CIA
from numsid.player import Player
from numsid.psid import PSIDFile


def make_tune(code: bytes, play_offset: int, *, cia: bool, rsid: bool = False,
              load: int = 0x1000) -> PSIDFile:
    """A one-song tune whose init is at `load` and play at `load + play_offset`."""
    return PSIDFile("RSID" if rsid else "PSID", 2, load, load, load + play_offset if play_offset >= 0 else 0,
                    1, 1, 1 if cia else 0, "test", "", "", 0x14, 0, 0, code)


class NullSID:
    def write(self, reg, value, cycle):
        pass

    def read(self, reg, cycle):
        return 0


# ---- the timer model ------------------------------------------------------------

def test_continuous_period_is_latch_plus_one():
    c = CIA()
    c.write(0x04, 0x63, 0)          # latch 99
    c.write(0x05, 0x00, 0)
    c.write(0x0E, 0x11, 0)          # start + force load
    assert c.ta.next_underflow == 100
    c.sync(1000)
    assert c.ta.next_underflow == 1100


def test_latch_written_while_running_applies_at_next_reload():
    c = CIA()
    c.write(0x04, 99, 0)
    c.write(0x05, 0, 0)
    c.write(0x0E, 0x11, 0)          # underflows at 100, 200, ...
    c.write(0x04, 199, 50)          # new latch mid-period
    assert c.ta.next_underflow == 100
    c.sync(100)                     # reload uses the new latch
    assert c.ta.next_underflow == 300


def test_stop_pauses_and_start_resumes():
    c = CIA()
    c.write(0x04, 99, 0)
    c.write(0x05, 0, 0)
    c.write(0x0E, 0x11, 0)
    c.write(0x0E, 0x00, 30)         # stop with 70 cycles to go
    assert c.read(0x04, 500) == 69
    c.write(0x0E, 0x01, 1000)       # resume: underflow 70 cycles later
    assert c.ta.next_underflow == 1070


def test_icr_flags_mask_and_clear_on_read():
    c = CIA()
    c.write(0x04, 9, 0)
    c.write(0x05, 0, 0)
    c.write(0x0E, 0x11, 0)
    c.sync(10)
    assert not c.irq                # flag set but masked
    c.write(0x0D, 0x81, 11)         # enable timer A interrupt
    assert c.irq
    assert c.read(0x0D, 12) == 0x81
    assert not c.irq and c.read(0x0D, 13) == 0


def test_one_shot_stops_after_underflow():
    c = CIA()
    c.write(0x04, 9, 0)
    c.write(0x05, 0, 0)
    c.write(0x0E, 0x19, 0)          # start, one-shot, force load
    c.sync(100)
    assert not c.ta.running and c.flags & 1


def test_timer_b_counts_timer_a_underflows():
    c = CIA()
    c.write(0x04, 9, 0)
    c.write(0x05, 0, 0)             # TA every 10 cycles
    c.write(0x06, 2, 0)
    c.write(0x07, 0, 0)             # TB = 3 TA underflows
    c.write(0x0F, 0x51, 0)          # start TB, cascade, force load
    c.write(0x0E, 0x11, 0)
    c.write(0x0D, 0x82, 0)
    assert c.next_event() == 30
    c.sync(29)
    assert not c.flags & 2
    c.sync(30)
    assert c.flags & 2


# ---- B1: CIA-timed PSIDs follow the timer as play reprograms it ----------------

def play_times(tune, calls=12):
    m = C64(tune, NullSID())
    m.init()
    times = []
    for _ in range(calls):
        times.append(m.next_play)
        m.play_frame()
    return np.diff(times)


def test_play_rewriting_the_latch_changes_the_rate():
    # init: RTS.  play: toggle the latch between $1000 and $2000.
    code = bytes([
        0x60,                                   # $1000 init: RTS
        0xEA, 0xEA, 0xEA,
        0xAD, 0x20, 0x10,                       # $1004 play: LDA $1020
        0x49, 0x30,                             #   EOR #$30  ($10 <-> $20)
        0x8D, 0x20, 0x10,                       #   STA $1020
        0x8D, 0x05, 0xDC,                       #   STA $DC05 (latch high)
        0xA9, 0x00, 0x8D, 0x04, 0xDC,           #   LDA #0 : STA $DC04
        0x60,                                   #   RTS
    ]).ljust(0x20, b"\xEA") + b"\x20"
    gaps = play_times(make_tune(code, 4, cia=True))
    # each new latch takes effect one period late, then periods alternate
    assert set(gaps[2:]) == {0x1001, 0x2001}
    assert all(a != b for a, b in zip(gaps[2:], gaps[3:]))


def test_stopping_the_timer_during_play_stretches_the_period():
    # play: stop timer A, busy-wait ~1000 cycles, restart it
    code = bytes([
        0x60, 0xEA, 0xEA, 0xEA,
        0xA9, 0x00, 0x8D, 0x0E, 0xDC,           # $1004: LDA #0 : STA $DC0E (stop)
        0xA2, 0xC8,                             #   LDX #200
        0xCA, 0xD0, 0xFD,                       #   DEX : BNE (5 cycles x 200)
        0xA9, 0x01, 0x8D, 0x0E, 0xDC,           #   LDA #1 : STA $DC0E (start)
        0x60,
    ])
    gaps = play_times(make_tune(code, 4, cia=True))
    stopped = gaps[3] - 0x4026                  # KERNAL default latch $4025
    assert 990 <= stopped <= 1020


# ---- B2: PSIDs start with the volume up ------------------------------------------

def test_psid_that_never_sets_volume_is_audible():
    # play: sawtooth on voice 1 with sustain 15, never touching $D418
    code = bytes([
        0x60, 0xEA, 0xEA, 0xEA,
        0xA9, 0x20, 0x8D, 0x01, 0xD4,           # $1004: freq hi
        0xA9, 0xF0, 0x8D, 0x06, 0xD4,           #   SR = $F0
        0xA9, 0x21, 0x8D, 0x04, 0xD4,           #   sawtooth + gate
        0x60,
    ])
    x = Player(make_tune(code, 4, cia=False)).render(22050)
    assert np.sqrt(np.mean(x[4410:] ** 2)) > 0.05
    # the first play call (and note) comes one frame after init; before it
    # there must be no thump from the volume being set
    assert np.abs(x[:800]).max() < 0.01


def test_volume_fade_in_from_zero_has_no_start_thump():
    # Like Cobra: play sets $D418 from a counter (0, 0, ..., 1, ...) under a
    # held sawtooth note.  The 6581's DC shift must not come out as a thump.
    code = bytes([
        0x60, 0xEA, 0xEA, 0xEA,
        0xA9, 0x20, 0x8D, 0x01, 0xD4,           # $1004: freq hi
        0xA9, 0xF0, 0x8D, 0x06, 0xD4,           #   SR = $F0
        0xA9, 0x21, 0x8D, 0x04, 0xD4,           #   sawtooth + gate
        0xAD, 0x40, 0x10,                       #   LDA $1040 (frame counter)
        0x4A, 0x4A, 0x4A,                       #   LSR x3: volume = frame / 8
        0x29, 0x0F, 0x8D, 0x18, 0xD4,           #   AND #15 : STA $D418
        0xEE, 0x40, 0x10,                       #   INC $1040
        0x60,
    ]).ljust(0x41, b"\x00")
    x = Player(make_tune(code, 4, cia=False)).render(44100 * 3).astype(np.float64)
    assert np.abs(x[:4410]).max() < 0.02           # first 100 ms: nearly silent
    rms = np.sqrt((x.reshape(30, -1) ** 2).mean(axis=1))
    assert rms[25] > 5 * rms[3]                     # and it does fade in
