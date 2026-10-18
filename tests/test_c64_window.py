"""Exercise real window input dispatch and resource cleanup without audio."""

import pytest

from numsid.c64.computer import Computer
from numsid.c64.roms import ROMs


def test_window_joystick_keyboard_pause_focus_and_close():
    import pyglet
    pyglet.options["headless"] = True
    try:
        from pyglet import gl
        probe = pyglet.window.Window(1, 1, visible=False,
                                     config=gl.Config(major_version=3, minor_version=3))
    except Exception as exc:
        pytest.skip(f"headless OpenGL context unavailable: {exc}")
    probe.close()
    from numsid.c64.window import C64Window, key
    machine = Computer(ROMs(bytes([0xEA]) * 8192, bytes([0xEA]) * 8192, bytes(4096)))
    window = C64Window(machine, scale=1, audio=False)
    try:
        assert window.renderer.crt
        window.on_key_press(key.F11, key.MOD_CTRL)
        assert not window.renderer.crt and not window.fullscreen
        window.on_key_press(key.F11, key.MOD_CTRL)
        assert window.renderer.crt
        window.on_key_press(key.RCTRL, 0)
        assert machine.read(0xDC00) == 0xEF
        window.on_key_release(key.RCTRL, 0)
        assert machine.read(0xDC00) == 0xFF
        window.on_key_press(key.RIGHT, 0)
        assert machine.read(0xDC00) == 0xF7
        window.on_key_press(key.F10, 0)
        assert machine.read(0xDC00) == 0xFF
        window.on_key_press(key.RIGHT, 0)
        assert machine.read(0xDC01) == 0xF7
        window.on_key_press(key.LSHIFT, 0)
        window.on_key_press(key.F2, 0)
        window.on_key_release(key.F2, 0)
        assert machine.inputs.keys == {"LSHIFT"}
        window.on_key_press(key.ESCAPE, 0)
        assert "STOP" in machine.inputs.keys and window.context is not None
        window.on_deactivate()
        assert not machine.inputs.keys
        assert machine.read(0xDC00) == machine.read(0xDC01) == 255
        before = machine.frame_number
        window.on_key_press(key.F9, 0)
        window.update(0.1)
        assert machine.frame_number == before
        window.on_key_press(key.F9, 0)
        window.update(window.frame_time)
        assert machine.frame_number == before + 1
    finally:
        window.close()
    assert window.renderer is None and window.context is None
