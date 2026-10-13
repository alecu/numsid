"""Visualiser: per-voice oscilloscopes and register state.

`Dashboard` draws everything for a given playback position into the current
GL target, at any size: it is used by the live `Visualizer` window and by
`record.py` for offline video/screenshot rendering.  The emulator renders
ahead of the sound card, so everything shown is looked up by the playback
position rather than taken from "now".
"""

from __future__ import annotations

import math

import numpy as np
import pyglet
from pyglet import shapes
from pyglet.gl import GL_TRIANGLES, glClearColor
from pyglet.window import key

from .player import Player
from .sid import SCOPE_SIZE

FONT = "Menlo"
BG = (15, 17, 26)
PANEL = (26, 29, 43)
GRID = (44, 48, 68)
TEXT = (220, 224, 236)
DIM = (130, 136, 160)
VOICE_COLORS = [(255, 122, 92), (104, 214, 142), (110, 160, 255)]
MUTED_COLOR = (74, 77, 88)       # trace colour of a muted voice
OUT_COLOR = (236, 236, 240)

WINDOW_LAYOUT = (980, 660)       # logical units of the live window
VIDEO_LAYOUT = (1280, 720)       # 16:9, for recordings
HELP = ("←/→ subtune    1 2 3 mute voice    SPACE pause    F filter on/off    "
        "M 6581/8580    ESC quit")

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
    """An oscilloscope trace drawn as a thick polyline (one quad per segment,
    extended by half the thickness so the joints overlap)."""

    def __init__(self, x, y, w, h, color, thickness, batch, group, points):
        self.y, self.h = y, h
        self.n = points
        self.color = tuple(color)
        self.half = thickness / 2
        self.xs = np.linspace(x, x + w, points)
        program = pyglet.graphics.get_default_shader()
        group = pyglet.graphics.ShaderGroup(program, order=group.order)
        self.count = 6 * (points - 1)
        self.vlist = program.vertex_list(
            self.count, GL_TRIANGLES, batch=batch, group=group,
            position=("f", np.zeros(3 * self.count).tolist()),
            colors=("Bn", (list(color) + [255]) * self.count))
        self.set(np.zeros(points))

    def set_color(self, color) -> None:
        color = tuple(color)
        if color != self.color:
            self.color = color
            self.vlist.colors[:] = (list(color) + [255]) * self.count

    def set(self, values: np.ndarray) -> None:
        """values: `n` samples in -1..1, left to right."""
        ys = self.y + self.h * 0.5 * (1.0 + np.clip(values, -1.0, 1.0) * 0.92)
        p = np.stack((self.xs, ys), axis=1)
        p0, p1 = p[:-1], p[1:]
        d = p1 - p0
        d /= np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-6)[:, None]
        d *= self.half
        nrm = np.stack((-d[:, 1], d[:, 0]), axis=1)
        p0 = p0 - d
        p1 = p1 + d
        a, b, c, e = p0 + nrm, p0 - nrm, p1 + nrm, p1 - nrm
        quads = np.stack((a, b, c, c, b, e), axis=1)            # (segments, 6, 2)
        buf = self.vlist.domain.attrib_name_buffers["position"]
        view = np.ctypeslib.as_array(buf.data)
        start = buf.count * self.vlist.start
        verts = view[start:start + 3 * self.count].reshape(-1, 6, 3)
        verts[:, :, :2] = quads
        verts[:, :, 2] = 0.0
        buf.invalidate_region(self.vlist.start, self.count)


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


def _fit(data: np.ndarray, n: int) -> np.ndarray:
    if len(data) == n:
        return data
    return np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)


class Dashboard:
    """All drawables, laid out in `layout` logical units and scaled to a
    canvas of `px_width` pixels."""

    def __init__(self, player: Player, px_width: int, layout=WINDOW_LAYOUT, *,
                 footer: str = HELP, progress: bool = False, max_points: int = 900,
                 line_width: float = 1.4):
        self.W, self.H = layout
        self.u = u = px_width / self.W
        self.batch = pyglet.graphics.Batch()
        self.bg = pyglet.graphics.Group(order=0)
        self.fg = pyglet.graphics.Group(order=1)
        self.top = pyglet.graphics.Group(order=2)
        self.span = int(player.sample_rate * 0.025)        # 25 ms of signal per scope
        self.keep = []                                      # keep static drawables alive
        W, H = self.W, self.H
        tune = player.tune

        self.title = self._label(tune.name, 24, H - 40, 18, bold=True)
        self.subtitle = self._label(f"{tune.author}  ·  {tune.released}", 24, H - 64, 11, DIM)
        self.status = self._label("", W - 24, H - 40, 12, anchor_x="right")
        self.status2 = self._label("", W - 24, H - 64, 10, DIM, anchor_x="right")
        self.progress = None
        if progress:
            self.keep.append(self._rect(16, H - 84, W - 32, 3, GRID, self.bg))
            self.progress = self._rect(16, H - 84, 0, 3, DIM, self.fg)

        header, footer_h, out_h, gap, info_w = 92, 30, 110, 10, 270
        panel_h = (H - header - footer_h - out_h - 4 * gap) / 3
        sx, sw = 16 + info_w, W - 32 - info_w - 12
        points = min(int(sw * u), max_points)
        thick = line_width * u
        self.voice_scopes, self.voice_labels, self.env_bars, self.env_tracks = [], [], [], []
        self.env_w = (info_w - 40) * u
        for i in range(3):
            y = H - header - (i + 1) * panel_h - i * gap
            col = VOICE_COLORS[i]
            self.keep.append(self._rect(16, y, W - 32, panel_h, PANEL, self.bg))
            self.keep.append(self._rect(sx, y + panel_h / 2, sw, 1, GRID, self.bg))
            self.voice_scopes.append(Scope(sx * u, (y + 8) * u, sw * u, (panel_h - 16) * u, col,
                                           thick, self.batch, self.fg, points))
            name = self._label(f"VOICE {i + 1}", 30, y + panel_h - 26, 13, col, bold=True)
            lines = [self._label("", 30, y + panel_h - 50 - 19 * k, 10) for k in range(4)]
            self.voice_labels.append((name, lines))
            self.env_tracks.append(self._rect(30, y + 12, info_w - 40, 6, GRID, self.bg))
            self.env_bars.append(self._rect(30, y + 12, 0, 6, col, self.fg))

        y = footer_h
        self.keep.append(self._rect(16, y, W - 32, out_h, PANEL, self.bg))
        self.keep.append(self._label("OUTPUT", 30, y + out_h - 26, 13, OUT_COLOR, bold=True))
        self.global_labels = [self._label("", 30, y + out_h - 50 - 19 * k, 10) for k in range(2)]
        self.out_scope = Scope(sx * u, (y + 6) * u, sw * u, (out_h - 12) * u, OUT_COLOR,
                               thick, self.batch, self.fg, points)
        self.keep.append(self._label(footer, W / 2, 10, 10, DIM, anchor_x="center"))
        self.shade = shapes.Rectangle(0, 0, W * u, H * u, color=(0, 0, 0), batch=self.batch,
                                      group=self.top)
        self.shade.opacity = 0

    def _label(self, text, x, y, size=11, color=TEXT, bold=False, anchor_x="left"):
        u = self.u
        return pyglet.text.Label(text, font_name=FONT, font_size=size * u, x=x * u, y=y * u,
                                 color=(*color, 255), weight="bold" if bold else "normal",
                                 anchor_x=anchor_x, batch=self.batch, group=self.fg)

    def _rect(self, x, y, w, h, color, group):
        u = self.u
        return shapes.Rectangle(x * u, y * u, w * u, h * u, color=color, batch=self.batch,
                                group=group)

    @staticmethod
    def _set(label, text):
        if label.text != text:
            label.text = text

    def update(self, player: Player, pos: float, *, status2: str = "", paused: bool = False,
               duration: float | None = None, brightness: float = 1.0) -> None:
        sid, tune = player.sid, player.tune
        self._set(self.status, f"song {player.song}/{tune.songs}   "
                               f"{int(pos) // 60:02d}:{int(pos) % 60:02d}"
                  + ("   PAUSED" if paused else ""))
        self._set(self.status2, status2)
        if self.progress is not None and duration:
            self.progress.width = (self.W - 32) * self.u * min(pos / duration, 1.0)
        self.shade.opacity = int(255 * (1.0 - max(0.0, min(1.0, brightness))))

        k = player.sample_index(pos)
        step = sid.scope_step
        voice_span = int(self.span * (tune.clock / step) / player.sample_rate)
        tap = player.cycle_at(pos) // step
        for i, scope in enumerate(self.voice_scopes):
            muted = sid.mute[i]
            scope.set_color(MUTED_COLOR if muted else VOICE_COLORS[i])
            scope.set(_fit(triggered_window(sid.scope[i], tap, voice_span, voice_span), scope.n))
            self.env_bars[i].visible = self.env_tracks[i].visible = not muted
        out = triggered_window(sid.scope_out, k, self.span, self.span // 2)
        self.out_scope.set(_fit(out * 2.0, self.out_scope.n))

        state = player.state_at(pos)
        if state:
            self._show_registers(player, *state)

    def _show_registers(self, player: Player, regs: bytes, envs) -> None:
        sid, clock = player.sid, player.tune.clock
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
            self._set(name, f"VOICE {i + 1}" + ("   [muted]" if sid.mute[i] else ""))
            texts = [f"{note_name(hz) if freq else '---':<10} {hz:8.1f} Hz",
                     f"{waves:<12} PW {100 * pw / 4095:5.1f}%",
                     f"A {ad >> 4:X} D {ad & 15:X} S {sr >> 4:X} R {sr & 15:X}   env {envs[i]:3d}",
                     flags or "-"]
            for label, text in zip(lines, texts):
                self._set(label, text)
            self.env_bars[i].width = self.env_w * envs[i] / 255
        fc = (regs[0x15] & 7) | regs[0x16] << 3
        res, routing, mode = regs[0x17] >> 4, regs[0x17] & 7, regs[0x18]
        modes = "".join(n for bit, n in ((0x10, "LP "), (0x20, "BP "), (0x40, "HP ")) if mode & bit)
        routed = "".join(str(v + 1) for v in range(3) if routing >> v & 1) or "-"
        texts = [f"VOL {mode & 15:<2d}  {modes or 'no filter'}" + ("  3OFF" if mode & 0x80 else ""),
                 f"FC {fc:<4d} {sid.f0[fc]:5.0f}Hz RES {res:X}  in {routed}"]
        for label, text in zip(self.global_labels, texts):
            self._set(label, text)

    def draw(self) -> None:
        self.batch.draw()


def chip_line(player: Player) -> str:
    tune = player.tune
    return (f"{tune.magic} · {'PAL' if tune.is_pal else 'NTSC'} · MOS {player.model} · "
            f"filter {'on' if player.sid.filter_enabled else 'off'}")


class Visualizer(pyglet.window.Window):
    """The live player window."""

    def __init__(self, player: Player, latency: float = 0.4):
        from .audio import LivePlayback
        w, h = WINDOW_LAYOUT
        try:
            config = pyglet.gl.Config(sample_buffers=1, samples=4, double_buffer=True)
            super().__init__(w, h, caption=f"pysid - {player.tune.name}", config=config)
        except pyglet.window.NoSuchConfigException:
            super().__init__(w, h, caption=f"pysid - {player.tune.name}")
        glClearColor(*(c / 255 for c in BG), 1.0)
        self.player = player
        self.live = LivePlayback(player, latency)
        # On HiDPI screens pyglet's "platform" mode hands us a larger canvas.
        self.dash = Dashboard(player, self.get_size()[0], WINDOW_LAYOUT, max_points=700)
        self.live.play()
        pyglet.clock.schedule_interval(self.update, 1 / 60)

    def update(self, dt: float) -> None:
        self.dash.update(self.player, self.live.position, paused=not self.live.media.playing,
                         status2=f"{chip_line(self.player)} · emulation "
                                 f"{100 * self.live.cpu_load():.0f}% CPU")

    def on_draw(self) -> None:
        self.clear()
        self.dash.draw()

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
