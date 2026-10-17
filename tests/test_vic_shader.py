"""Exercise the actual GLSL decoder on a headless OpenGL context."""

import ctypes

import numpy as np
import pytest

from numsid.c64.computer import VideoFrame, WIDTH, HEIGHT

PALETTE = np.array(((0, 0, 0), (255, 255, 255), (129, 51, 56), (117, 206, 200),
                    (142, 60, 151), (86, 172, 77), (46, 44, 155), (237, 241, 113),
                    (142, 80, 41), (85, 56, 0), (196, 108, 113), (74, 74, 74),
                    (123, 123, 123), (169, 255, 159), (112, 109, 235), (178, 178, 178)))


@pytest.fixture(scope="module")
def render():
    import pyglet
    pyglet.options["headless"] = True
    try:
        from pyglet import gl
        window = pyglet.window.Window(WIDTH * 4, HEIGHT * 4, visible=False,
                                      config=gl.Config(major_version=3, minor_version=3,
                                                       red_size=8, green_size=8, blue_size=8))
    except Exception as exc:
        pytest.skip(f"headless OpenGL context unavailable: {exc}")
    # Shader compilation failures are test failures, rather than skips.
    from numsid.c64.renderer import VICRenderer
    chargen = bytearray(4096)
    chargen[8] = 0x80
    renderer = VICRenderer(chargen, crt=False)

    def draw(frame, size=(WIDTH, HEIGHT), *, crt=False, reuse=False):
        window.switch_to()
        if not reuse:
            renderer.upload(frame)
        renderer.crt = crt
        width, height = size
        renderer.draw(width, height)
        pixels = np.empty((height, width, 4), dtype=np.uint8)
        gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE,
                        pixels.ctypes.data_as(ctypes.c_void_p))
        return pixels[::-1, :, :3]

    yield draw
    renderer.delete()
    window.close()


def scene():
    f = VideoFrame.empty()
    f.banks[:] = 1  # bank $4000 has RAM at every VIC address
    f.registers[:, 0x11] = 0x1B
    f.registers[:, 0x16] = 8
    f.registers[:, 0x18] = 0x18  # screen $0400, glyphs/bitmap $2000
    f.registers[:, 0x20:0x25] = (0, 6, 3, 4, 5)
    return f


@pytest.mark.parametrize("mode,expected", [
    ("text", [2, 6, 6, 6, 6, 6, 6, 6]),
    ("multicolor text", [6, 6, 3, 3, 4, 4, 5, 5]),
    ("extended text", [2, 4, 4, 4, 4, 4, 4, 4]),
    ("bitmap", [2, 5, 5, 5, 5, 5, 5, 5]),
    ("multicolor bitmap", [6, 6, 2, 2, 3, 3, 5, 5]),
])
def test_all_five_display_modes(render, mode, expected):
    f = scene()
    f.memory[:, 0x400] = 1
    f.memory[:, 0x2008] = 0x80
    f.colors[:, 0] = 2
    if mode == "multicolor text":
        f.registers[:, 0x16] |= 16
        f.memory[:, 0x2008] = 0x1B
        f.colors[:, 0] = 13
    elif mode == "extended text":
        f.registers[:, 0x11] |= 64
        f.memory[:, 0x400] = 0x81
    elif "bitmap" in mode:
        f.registers[:, 0x11] |= 32
        f.memory[:, 0x400] = 0x23 if mode.startswith("multicolor") else 0x25
        f.memory[:, 0x2000] = 0x1B if mode.startswith("multicolor") else 0x80
        f.colors[:, 0] = 5
        if mode.startswith("multicolor"):
            f.registers[:, 0x16] |= 16
    pixels = render(f)
    np.testing.assert_array_equal(pixels[35, 32:40], PALETTE[expected])


def test_vic_character_rom_depends_on_bank(render):
    f = scene()
    f.registers[:, 0x18] = 0x14  # character base $1000
    f.memory[:, 0x400] = 1
    f.colors[:, 0] = 1
    f.banks[:] = 0
    assert (render(f)[35, 32] == PALETTE[1]).all()
    f.banks[:] = 1
    assert (render(f)[35, 32] == PALETTE[6]).all()


def test_raster_changes_and_fine_scroll(render):
    f = scene()
    f.registers[100:, 0x21] = 7
    f.memory[:, 0x400] = 1
    f.memory[:, 0x2008] = 0x80
    f.colors[:, 0] = 1
    f.registers[:, 0x16] |= 3
    pixels = render(f)
    assert (pixels[35, 32] == PALETTE[6]).all()
    assert (pixels[35, 35] == PALETTE[1]).all()
    assert (pixels[100 - 16, 100] == PALETTE[7]).all()
    assert (pixels[99 - 16, 100] == PALETTE[6]).all()


def test_sprite_priority_multicolor_and_expansion(render):
    f = scene()
    f.registers[:, 0:4] = (24, 51, 24, 51)
    f.registers[:, 0x15] = 3
    f.registers[:, 0x27:0x29] = (10, 5)
    f.memory[:, 0x7F8:0x7FA] = 0x30
    f.memory[:, 0xC00] = 0x80
    f.memory[:, 0x400] = 1
    f.memory[:, 0x2008] = 0x80
    f.colors[:, 0] = 1
    assert (render(f)[35, 32] == PALETTE[10]).all()  # sprite 0 wins
    f.registers[:, 0x1B] = 1  # sprite 0 behind foreground also hides sprite 1
    assert (render(f)[35, 32] == PALETTE[1]).all()
    f.registers[:, 0x1B] = 0
    f.registers[:, 0x15] = 1
    f.registers[:, 0x1C] = 1
    f.registers[:, 0x1D] = 1
    f.registers[:, 0x17] = 1
    f.registers[:, 0x25] = 7
    f.memory[:, 0xC00] = 0x60  # 01 shared colour, 10 own colour, 00 transparent
    pixels = render(f)
    np.testing.assert_array_equal(pixels[35, 32:40], PALETTE[[7] * 4 + [10] * 4])
    np.testing.assert_array_equal(pixels[36, 32:40], pixels[35, 32:40])


def test_24_row_38_column_border_and_display_disable(render):
    f = scene()
    f.registers[:, 0x20] = 2
    f.registers[:, 0x11] &= 0xF7
    f.registers[:, 0x16] &= 0xF7
    pixels = render(f)
    assert (pixels[35, 40] == PALETTE[2]).all()
    assert (pixels[40, 32] == PALETTE[2]).all()
    assert (pixels[40, 40] == PALETTE[6]).all()
    f.registers[:, 0x11] &= 0xEF
    np.testing.assert_array_equal(render(f), np.broadcast_to(PALETTE[2], (HEIGHT, WIDTH, 3)))


@pytest.mark.parametrize("size", [(901, 601), (1000, 700), (1200, 400), (201, 120)])
def test_fractional_scaling_fills_window_preserving_aspect_ratio(render, size):
    f = scene()
    f.registers[:, 0x20:0x25] = 1
    pixels = render(f, size)
    lit = np.any(pixels, axis=2)
    rows, cols = np.flatnonzero(lit.any(axis=1)), np.flatnonzero(lit.any(axis=0))
    width, height = size
    assert len(rows) == height or len(cols) == width
    assert abs(len(cols) * HEIGHT - len(rows) * WIDTH) <= max(WIDTH, HEIGHT)
    assert abs(int(cols[0]) - (width - int(cols[-1]) - 1)) <= 1
    assert abs(int(rows[0]) - (height - int(rows[-1]) - 1)) <= 1


@pytest.mark.parametrize("scale", [2, 3])
def test_crt_scanlines_and_toggle_without_redecoding_frame(render, scale):
    f = scene()
    f.registers[:, 0x20:0x25] = 1
    size = WIDTH * scale, HEIGHT * scale
    effected = render(f, size, crt=True)
    x, y = size[0] // 2, 100 * scale
    levels = effected[y:y + scale, x - 12:x + 12].mean(axis=(1, 2))
    assert np.ptp(levels) > 20
    raw = render(f, size, crt=False, reuse=True)
    np.testing.assert_array_equal(raw, np.full((*raw.shape[:2], 3), 255, dtype=np.uint8))
    # Native-size output cannot resolve the grille/scanline pattern: fade it.
    native = render(f, crt=True)
    levels = native[100:103, WIDTH // 2 - 12:WIDTH // 2 + 12].mean(axis=(1, 2))
    assert np.ptp(levels) < 2


def test_sprite_uses_latched_row_when_y_register_changes_after_start(render):
    f = scene()
    f.registers[:, 0] = 24
    f.registers[:, 1] = 1  # Y register now points behind the current raster.
    f.registers[:, 0x27] = 10
    f.memory[:, 0x7F8] = 0x30
    f.memory[:, 0xC00 + 3 * 4] = 0x80
    f.sprite_rows = np.full((312, 8), 255, dtype=np.uint8)
    f.sprite_rows[60, 0] = 4
    pixels = render(f)
    assert (pixels[60 - 16, 32] == PALETTE[10]).all()
    assert (pixels[61 - 16, 32] == PALETTE[6]).all()
