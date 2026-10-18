"""Interactive C64 window, keyboard/joystick input and queued SID audio."""

from collections import deque
import threading
import time

import numpy as np
import pyglet
from pyglet import gl
from pyglet.window import key

from .computer import WIDTH, HEIGHT, PAL_CLOCK
from .cpu6502 import CPUHalt
from .renderer import VICRenderer


def keymap():
    mapping = {getattr(key, "_" + c if c.isdigit() else c): c
               for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
    mapping.update({key.SPACE: "SPACE", key.ENTER: "RETURN", key.RETURN: "RETURN",
                    key.BACKSPACE: "DELETE", key.DELETE: "DELETE", key.HOME: "HOME",
                    key.LSHIFT: "LSHIFT", key.RSHIFT: "RSHIFT", key.LCTRL: "CTRL",
                    key.RCTRL: "CTRL", key.LALT: "COMMODORE", key.RALT: "COMMODORE",
                    key.F1: "F1", key.F3: "F3", key.F5: "F5", key.F7: "F7",
                    key.MINUS: "-", key.EQUAL: "=", key.BRACKETLEFT: "@",
                    key.BRACKETRIGHT: "*", key.SEMICOLON: ";", key.APOSTROPHE: ":",
                    key.COMMA: ",", key.PERIOD: ".", key.SLASH: "/", key.ESCAPE: "STOP",
                    key.GRAVE: "LEFTARROW", key.BACKSLASH: "POUND"})
    return mapping


def make_audio():
    from pyglet.media.codecs.base import AudioData, AudioFormat, StreamingSource
    from pyglet.media.drivers.base import AbstractAudioPlayer

    class Source(StreamingSource):
        def __init__(self):
            self.audio_format = AudioFormat(channels=1, sample_size=16, sample_rate=44100)
            self.queue = deque()
            self.lock = threading.Lock()
            self.pending = 0

        def is_precise(self):
            return True

        def append(self, samples):
            data = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
            with self.lock:
                self.queue.append(data)
                self.pending += len(data)
                # Bound latency if the audio device falls behind.
                while self.pending > 44100 // 3 and len(self.queue) > 1:
                    self.pending -= len(self.queue.popleft())

        def get_audio_data(self, num_bytes, compensation_time=0):
            count = max(2, int(num_bytes) & ~1)
            pieces, remaining = [], count
            with self.lock:
                while remaining and self.queue:
                    data = self.queue.popleft()
                    part = data[:remaining]
                    pieces.append(part)
                    self.pending -= len(part)
                    remaining -= len(part)
                    if len(part) < len(data):
                        self.queue.appendleft(data[len(part):])
            if remaining:
                pieces.append(bytes(remaining))
            return AudioData(b"".join(pieces), count)

        def clear(self):
            with self.lock:
                self.queue.clear()
                self.pending = 0

    AbstractAudioPlayer.audio_buffer_length = 0.06
    source = Source()
    player = pyglet.media.Player()
    player.queue(source)
    return player, source


class C64Window(pyglet.window.Window):
    def __init__(self, machine, scale=3, joystick=2, audio=True, frames=None, crt=True):
        super().__init__(WIDTH * scale, HEIGHT * scale, caption="numc64", resizable=True,
                         config=gl.Config(major_version=3, minor_version=3, double_buffer=True,
                                          red_size=8, green_size=8, blue_size=8),
                         vsync=True)
        self.machine = machine
        self.renderer = VICRenderer(machine.roms.chargen, crt=crt)
        self.joystick = joystick
        self.mapping = keymap()
        self.held = {}
        self.frame_time = machine.vic.frame / PAL_CLOCK
        self.accumulator = 0.0
        self.paused = False
        self.limit = frames
        self.played = 0
        self.elapsed = 0.0
        self.work = 0.0
        self.media, self.source = make_audio() if audio else (None, None)
        self._advance()
        if self.media:
            self.media.play()
        pyglet.clock.schedule_interval(self.update, self.frame_time)

    def _advance(self, capture=True):
        frame = self.machine.step_frame(capture=capture)
        if frame is not None:
            self.renderer.upload(frame)
        if self.source:
            self.source.append(self.machine.audio)
        self.played += 1

    def update(self, dt):
        if self.limit and self.played >= self.limit:
            self.close()
            return
        if self.paused:
            return
        self.accumulator = min(self.accumulator + dt, 3 * self.frame_time)
        start = time.perf_counter()
        try:
            steps = int(self.accumulator / self.frame_time)
            for step in range(steps):
                # When catching up, synthesize every frame's audio but only
                # capture/upload the frame that will actually be displayed.
                self._advance(capture=step == steps - 1)
                self.accumulator -= self.frame_time
                if self.limit and self.played >= self.limit:
                    self.close()
                    return
        except CPUHalt as exc:
            self.paused = True
            print(f"C64 halted: {exc}")
        self.work += time.perf_counter() - start
        self.elapsed += dt
        if self.elapsed >= 1:
            self.set_caption(f"numc64 · PAL · joystick {self.joystick} · "
                             f"{100 * self.work / self.elapsed:.0f}% emulation load · "
                             "F9 pause · F10 joystick · F11 fullscreen · F12 RESTORE")
            self.elapsed = self.work = 0.0

    def on_draw(self):
        if getattr(self, "renderer", None) is not None:
            self.renderer.draw(*self.get_framebuffer_size())

    def on_key_press(self, symbol, modifiers):
        if symbol in self.held:
            return
        if symbol == key.F9:
            self.paused = not self.paused
            if self.media:
                self.media.pause() if self.paused else self.media.play()
            return
        if symbol == key.F10:
            self.machine.inputs.release()
            self.held.clear()
            self.joystick = 3 - self.joystick
            return
        if symbol == key.F11:
            if modifiers & key.MOD_CTRL:
                self.renderer.crt = not self.renderer.crt
                return
            self.set_fullscreen(not self.fullscreen)
            return
        if symbol == key.F12:
            self.machine.restore(True)
            return
        directions = {key.UP: "up", key.DOWN: "down", key.LEFT: "left",
                      key.RIGHT: "right", key.RCTRL: "fire"}
        if symbol in directions:
            self.machine.inputs.joystick(directions[symbol], True, self.joystick)
            self.held[symbol] = ("joy", directions[symbol])
        else:
            names = (self.mapping[symbol],) if symbol in self.mapping else ()
            if symbol in (key.F2, key.F4, key.F6, key.F8):
                names = (f"F{int(key.symbol_string(symbol)[1:]) - 1}", "LSHIFT")
            for name in names:
                self.machine.inputs.key(name, True)
            self.held[symbol] = ("key", names)

    def on_key_release(self, symbol, modifiers):
        if symbol == key.F12:
            self.machine.restore(False)
        action, value = self.held.pop(symbol, (None, None))
        if action == "joy":
            self.machine.inputs.joystick(value, False, self.joystick)
        elif action == "key":
            for name in value:
                if not any(name in v for kind, v in self.held.values() if kind == "key"):
                    self.machine.inputs.key(name, False)

    def on_deactivate(self):
        if hasattr(self, "machine"):
            self.machine.inputs.release()
            self.machine.restore(False)
            self.held.clear()

    def close(self):
        pyglet.clock.unschedule(self.update)
        if getattr(self, "media", None):
            self.media.pause()
            self.media.delete()
            self.media = None
        if getattr(self, "renderer", None) is not None:
            self.switch_to()
            self.renderer.delete()
            self.renderer = None
        super().close()


def run(machine, **kwargs):
    window = C64Window(machine, **kwargs)
    print("Arrows: joystick · Right Ctrl: fire · Space: grenade / trainer · "
          "Esc: RUN/STOP · F9: pause · F10: joystick port · F11: fullscreen · "
          "Ctrl+F11: CRT · F12: RESTORE")
    pyglet.app.run(interval=window.frame_time)
    return window
