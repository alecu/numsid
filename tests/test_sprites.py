"""Sprite DMA starts at a raster comparison, rather than a Y range test."""

from numsid.c64.sprites import Sprites


def test_y_written_behind_beam_does_not_start_dma():
    s, regs = Sprites(), bytearray(64)
    regs[0x15] = 1
    regs[1] = 40
    s.prepare_next_line(regs, 41)
    s.begin_line()
    assert s.active == 0 and s.rows[0] == 255
    # Only the next actual Y comparison starts the row counter.
    s.prepare_next_line(regs, 40)
    s.begin_line()
    assert s.active == 1 and s.rows[0] == 0


def test_multiplexing_y_write_does_not_reposition_active_row_counter():
    s, regs = Sprites(), bytearray(64)
    regs[0x15], regs[1] = 1, 60
    s.prepare_next_line(regs, 60)
    for line in range(61, 82):
        s.begin_line()
        assert s.active == 1 and s.rows[0] == line - 61
        regs[1] = 10  # Move Y behind the beam while the old sprite is in flight.
        regs[0x15] = 0  # An already-running DMA still finishes its 21 rows.
        s.prepare_next_line(regs, line)
    s.begin_line()
    assert s.active == 0 and s.rows[0] == 255


def test_y_expansion_repeats_every_row_twice_and_restarts_at_next_match():
    s, regs = Sprites(), bytearray(64)
    regs[0x15], regs[0x17], regs[1] = 1, 1, 60
    s.prepare_next_line(regs, 60)
    for step in range(42):
        s.begin_line()
        assert s.rows[0] == step // 2
        s.prepare_next_line(regs, 61 + step)
    s.begin_line()
    assert not s.active
    # The hardware Y comparator uses the low eight raster bits.
    regs[1] = 10
    s.prepare_next_line(regs, 266)
    s.begin_line()
    assert s.rows[0] == 0
