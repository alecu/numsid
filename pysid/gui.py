"""pyglet window: per-voice oscilloscopes and register state, in sync with
what is being heard (the emulator renders ahead; everything shown here is
looked up by the current playback position)."""

from __future__ import annotations

import math

import numpy as np
import pyglet
from pyglet import shapes
from pyglet.gl import GL_LINES, glClearColor
from pyglet.window import key

from .audio import LivePlayback
from .player import Player
from .sid import SCOPE_SIZE

WIDTH, HEIGHT = 980, 660
FONT = "Menlo"
BG = (15, 17, 26)
PANEL = (26, 29, 43)
GRID = (44, 48, 68)
TEXT = (220, 224, 236)
DIM = (130, 136, 160)
VOICE_COLORS = [(255, 122, 92), (104, 214, 142), (110, 160, 255)]
OUT_COLOR = (236, 236, 240)

NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]
WAVE_NAMES = [(1, "TRI"), (2, "SAW"), (4, "PUL"), (8, "NOI")]


def note_name(hz: float) -> str:
    if hz < 8:
        return "---"
    midi = 69 + 12 * math.log2(hz / 440.0)
    n = int(round(midi))
    cents = int(round((midi - n) * 100))
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1} {cents:+3d}c"


class Scope:
    """A line trace drawn as GL_LINES segments with the default shader."""

    def __init__(self, x, y, w, h, color, batch, group, points=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.n = points or min(int(w), 800)
        program = pyglet.graphics.get_default_shader()
        group = pyglet.graphics.ShaderGroup(program, order=group.order)
        count = 2 * (self.n - 1)
        self.xs = np.repeat(np.linspace(x, x + w, self.n), 2)[1:-1]
        self.vlist = program.vertex_list(
            count, GL_LINES, batch=batch, group=group,
            position=("f", [0.0] * (3 * count)),
            colors=("Bn", (list(color) + [255]) * count))
        self.set(np.zeros(self.n))

    def set(self, values: np.ndarray) -> None:
        """values: n samples in -1..1."""
        ys = self.y + self.h * 0.5 * (1.0 + np.clip(values, -1.0, 1.0) * 0.92)
        pos = np.zeros((len(self.xs), 3), dtype=np.float32)
        pos[:, 0] = self.xs
        pos[:, 1] = np.repeat(ys, 2)[1:-1]
        self.vlist.position[:] = pos.ravel().tolist()


def triggered_window(buf: np.ndarray, end: int, span: int, search: int) -> np.ndarray:
    """`span` samples from a ring buffer ending near absolute index `end`,
    shifted back to a rising zero crossing so periodic waves stand still."""
    idx = np.arange(end - span - search, end) % SCOPE_SIZE
    data = buf[idx]
    centre = 0.5 * (data.max() + data.min())
    seg = data[:search + 1] - centre
    rising = np.nonzero((seg[:-1] < 0) & (seg[1:] >= 0))[0]
    start = rising[-1] + 1 if len(rising) else search
    return data[start:start + span]


class Visualizer(pyglet.window.Window):
    def __init__(self, player: Player, latency: float = 0.4):
        super().__init__(WIDTH, HEIGHT, caption=f"pysid - {player.tune.name}")
        glClearColor(*(c / 255 for c in BG), 1.0)
        # Layout is in logical units of a WIDTH x HEIGHT window; on HiDPI
        # screens pyglet's "platform" mode hands us a larger pixel canvas.
        self.u = self.get_size()[0] / WIDTH
        self.player = player
        self.live = LivePlayback(player, latency)
        self.batch = pyglet.graphics.Batch()
        self.bg_group = pyglet.graphics.Group(order=0)
        self.fg_group = pyglet.graphics.Group(order=1)
        self.span = int(player.sample_rate * 0.025)        # 25 ms of signal per scope
        self._build()
        self.live.play()
        pyglet.clock.schedule_interval(self.update, 1 / 60)

    # ---- layout -----------------------------------------------------------
    def _label(self, text, x, y, size=11, color=TEXT, bold=False, anchor_x="left"):
        u = self.u
        return pyglet.text.Label(text, font_name=FONT, font_size=size * u, x=x * u, y=y * u,
                                 color=(*color, 255), weight="bold" if bold else "normal",
                                 anchor_x=anchor_x, batch=self.batch, group=self.fg_group)

    def _rect(self, x, y, w, h, color, group):
        u = self.u
        return shapes.Rectangle(x * u, y * u, w * u, h * u, color=color, batch=self.batch,
                                group=group)

    def _build(self) -> None:
        tune = self.player.tune
        b, bg, u = self.batch, self.bg_group, self.u
        self.title = self._label(f"{tune.name}", 24, HEIGHT - 40, 18, bold=True)
        self.subtitle = self._label(f"{tune.author}  ·  {tune.released}", 24, HEIGHT - 64, 11, DIM)
        self.status = self._label("", WIDTH - 24, HEIGHT - 40, 12, anchor_x="right")
        self.status2 = self._label("", WIDTH - 24, HEIGHT - 64, 10, DIM, anchor_x="right")

        top = HEIGHT - 90
        panel_h, gap = 138, 10
        info_w = 270
        self.panels = []
        self.voice_scopes = []
        self.voice_labels = []
        self.env_bars = []
        for i in range(3):
            y = top - (i + 1) * panel_h - i * gap
            self.panels.append(self._rect(16, y, WIDTH - 32, panel_h, PANEL, bg))
            sx, sw = 16 + info_w, WIDTH - 32 - info_w - 12
            self.panels.append(self._rect(sx, y + panel_h / 2, sw, 1, GRID, bg))
            self.voice_scopes.append(Scope(sx * u, (y + 8) * u, sw * u, (panel_h - 16) * u,
                                           VOICE_COLORS[i], b, self.fg_group))
            col = VOICE_COLORS[i]
            name = self._label(f"VOICE {i + 1}", 30, y + panel_h - 26, 13, col, bold=True)
            lines = [self._label("", 30, y + panel_h - 50 - 19 * k, 10) for k in range(4)]
            self.voice_labels.append((name, lines))
            self.panels.append(self._rect(30, y + 12, info_w - 40, 6, GRID, bg))
            self.env_bars.append(self._rect(30, y + 12, 0, 6, col, self.fg_group))

        y = top - 3 * panel_h - 3 * gap - 100
        self.panels.append(self._rect(16, y, WIDTH - 32, 100, PANEL, bg))
        self.out_title = self._label("OUTPUT", 30, y + 74, 13, OUT_COLOR, bold=True)
        self.global_labels = [self._label("", 30, y + 50 - 19 * k, 10) for k in range(2)]
        sx, sw = 16 + info_w, WIDTH - 32 - info_w - 12
        self.out_scope = Scope(sx * u, (y + 6) * u, sw * u, 88 * u, OUT_COLOR, b, self.fg_group)
        self.help = self._label("←/→ subtune    1 2 3 mute voice    SPACE pause    F filter on/off    "
                    "M 6581/8580    ESC quit", WIDTH / 2, 12, 10, DIM, anchor_x="center")

    # ---- per-frame update ---------------------------------------------------
    def update(self, dt: float) -> None:
        player, sid = self.player, self.player.sid
        pos = self.live.position
        tune = player.tune
        self.status.text = (f"song {player.song}/{tune.songs}   "
                            f"{int(pos) // 60:02d}:{int(pos) % 60:02d}"
                            + ("   PAUSED" if not self.live.media.playing else ""))
        self.status2.text = (f"{tune.magic} · {'PAL' if tune.is_pal else 'NTSC'} · "
                             f"MOS {player.model} · filter {'on' if sid.filter_enabled else 'off'}"
                             f" · emulation {100 * self.live.cpu_load():.0f}% CPU")

        k = player.sample_index(pos)
        cycle = player.cycle_at(pos)
        step = sid.scope_step
        span = self.span
        voice_span = int(span * (tune.clock / step) / player.sample_rate)
        for i in range(3):
            data = triggered_window(sid.scope[i], cycle // step, voice_span, voice_span)
            self.voice_scopes[i].set(self._fit(data, self.voice_scopes[i].n))
        out = triggered_window(sid.scope_out, k, span, span // 2)
        self.out_scope.set(self._fit(out * 2.0, self.out_scope.n))

        state = player.state_at(pos)
        if state:
            self._show_registers(*state)

    @staticmethod
    def _fit(data: np.ndarray, n: int) -> np.ndarray:
        if len(data) == n:
            return data
        return np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)

    def _show_registers(self, regs: bytes, envs) -> None:
        sid, clock = self.player.sid, self.player.tune.clock
        for i in range(3):
            r = regs[7 * i:7 * i + 7]
            freq = r[0] | r[1] << 8
            pw = r[2] | (r[3] & 15) << 8
            ctrl, ad, sr = r[4], r[5], r[6]
            hz = freq * clock / 16777216
            waves = "+".join(n for bit, n in WAVE_NAMES if ctrl >> 4 & bit) or "----"
            flags = " ".join(f for bit, f in ((1, "GATE"), (2, "SYNC"), (4, "RING"), (8, "TEST"))
                             if ctrl & bit)
            name, lines = self.voice_labels[i]
            name.text = f"VOICE {i + 1}" + ("   [muted]" if sid.mute[i] else "")
            texts = [f"{note_name(hz) if freq else '---':<10} {hz:8.1f} Hz",
                     f"{waves:<12} PW {100 * pw / 4095:5.1f}%",
                     f"A {ad >> 4:X} D {ad & 15:X} S {sr >> 4:X} R {sr & 15:X}   env {envs[i]:3d}",
                     flags or "-"]
            for label, text in zip(lines, texts):
                if label.text != text:
                    label.text = text
            self.env_bars[i].width = (270 - 40) * self.u * envs[i] / 255
        fc = (regs[0x15] & 7) | regs[0x16] << 3
        res, routing, mode = regs[0x17] >> 4, regs[0x17] & 7, regs[0x18]
        modes = "".join(n for bit, n in ((0x10, "LP "), (0x20, "BP "), (0x40, "HP ")) if mode & bit)
        routed = "".join(str(v + 1) for v in range(3) if routing >> v & 1) or "-"
        texts = [f"VOL {mode & 15:<2d}  {modes or 'no filter'}" + ("  3OFF" if mode & 0x80 else ""),
                 f"FC {fc:<4d} {sid.f0[fc]:5.0f}Hz RES {res:X}  in {routed}"]
        for label, text in zip(self.global_labels, texts):
            if label.text != text:
                label.text = text

    # ---- events -------------------------------------------------------------
    def on_draw(self) -> None:
        self.clear()
        self.batch.draw()

    def on_key_press(self, symbol, modifiers) -> None:
        player, sid = self.player, self.player.sid
        songs = player.tune.songs
        if symbol == key.ESCAPE:
            self.close()
        elif symbol in (key.RIGHT, key.UP, key.PLUS, key.NUM_ADD):
            self.live.select(player.song % songs + 1)
        elif symbol in (key.LEFT, key.DOWN, key.MINUS, key.NUM_SUBTRACT):
            self.live.select((player.song - 2) % songs + 1)
        elif symbol in (key._1, key._2, key._3):
            i = symbol - key._1
            with self.live.source.lock:
                sid.mute[i] = not sid.mute[i]
        elif symbol == key.SPACE:
            self.live.toggle_pause()
        elif symbol == key.F:
            with self.live.source.lock:
                sid.filter_enabled = not sid.filter_enabled
        elif symbol == key.M:
            with self.live.source.lock:
                player.set_model("8580" if player.model == "6581" else "6581")
            self.live.media.seek(0.0)

    def on_close(self) -> None:
        pyglet.clock.unschedule(self.update)
        self.live.media.pause()
        self.live.media.delete()
        super().on_close()
        pyglet.app.exit()


def run_gui(player: Player, latency: float = 0.4) -> None:
    Visualizer(player, latency)
    pyglet.app.run()
