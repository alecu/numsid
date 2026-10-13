"""Offline rendering of the visualiser: PNG screenshots and MP4 videos.

Frames are drawn into an offscreen multisampled framebuffer on a fixed
timeline (frame n shows playback time n / fps), and the audio track is the
exact sample stream the emulator produced while those frames were drawn, so
picture and sound are in perfect sync with no dropped frames.  Encoding is
done by ffmpeg (H.264 + AAC, YouTube-friendly).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import pyglet
from pyglet.gl import (GL_COLOR_BUFFER_BIT, GL_DRAW_FRAMEBUFFER, GL_FRAMEBUFFER, GL_NEAREST,
                       GL_READ_FRAMEBUFFER, GL_RGBA, GL_RGBA8, GL_UNSIGNED_BYTE, GLubyte,
                       glBindFramebuffer, glBlitFramebuffer, glClear, glClearColor,
                       glReadPixels, glViewport)
from pyglet.image.buffer import Framebuffer, Renderbuffer
from pyglet.math import Mat4

from .gui import BG, VIDEO_LAYOUT, WINDOW_LAYOUT, Dashboard, chip_line
from .player import Player, write_wav

CREDIT = "pysid  ·  6502 CPU and SID chip emulated cycle by cycle in Python + numpy"


class Offscreen:
    """A hidden GL context plus an MSAA render target of the given size."""

    def __init__(self, width: int, height: int, samples: int = 4):
        self.window = pyglet.window.Window(width=64, height=64, visible=False)
        self.w, self.h = width, height
        self._msaa_rb = Renderbuffer(width, height, GL_RGBA8, samples=samples)
        self.msaa = Framebuffer()
        self.msaa.attach_renderbuffer(self._msaa_rb)
        self._plain_rb = Renderbuffer(width, height, GL_RGBA8)
        self.plain = Framebuffer()
        self.plain.attach_renderbuffer(self._plain_rb)
        self.pixels = (GLubyte * (width * height * 4))()

    def render(self, draw) -> memoryview:
        """Run `draw()` into the target; returns RGBA rows, bottom row first."""
        w, h = self.w, self.h
        self.window.switch_to()
        self.msaa.bind()
        glViewport(0, 0, w, h)
        self.window.projection = Mat4.orthogonal_projection(0, w, 0, h, -255, 255)
        glClearColor(*(c / 255 for c in BG), 1.0)
        glClear(GL_COLOR_BUFFER_BIT)
        draw()
        glBindFramebuffer(GL_READ_FRAMEBUFFER, self.msaa.id)
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, self.plain.id)
        glBlitFramebuffer(0, 0, w, h, 0, 0, w, h, GL_COLOR_BUFFER_BIT, GL_NEAREST)
        glBindFramebuffer(GL_READ_FRAMEBUFFER, self.plain.id)
        glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, self.pixels)
        glBindFramebuffer(GL_FRAMEBUFFER, 0)
        return memoryview(self.pixels)

    def close(self) -> None:
        self.window.close()


class _Timeline:
    """Keeps the emulator rendered just ahead of a playback position and
    collects the audio it produced."""

    def __init__(self, player: Player):
        self.player = player
        self.audio: list[np.ndarray] = []
        self.samples = 0

    def advance(self, seconds: float, lead: float = 0.05) -> None:
        target = int((seconds + lead) * self.player.sample_rate)
        if target > self.samples:
            self.audio.append(self.player.render(target - self.samples))
            self.samples = target

    def track(self, seconds: float) -> np.ndarray:
        n = int(seconds * self.player.sample_rate)
        self.advance(seconds, lead=0.0)
        return np.concatenate(self.audio)[:n]


def screenshot(player: Player, path: str | Path, at: float, *, width: int = 1960,
               layout=WINDOW_LAYOUT, footer: str | None = None) -> None:
    """Save a PNG of the visualiser `at` seconds into the current song."""
    h = round(width * layout[1] / layout[0])
    off = Offscreen(width, h)
    try:
        kwargs = {} if footer is None else {"footer": footer}
        dash = Dashboard(player, width, layout, max_points=1600, line_width=1.4, **kwargs)
        timeline = _Timeline(player)
        timeline.advance(at)
        dash.update(player, at, status2=chip_line(player))
        rgba = np.frombuffer(off.render(dash.draw), dtype=np.uint8).reshape(h, width, 4).copy()
    finally:
        off.close()
    rgba[..., 3] = 255                      # blending leaves partial alpha under text
    pyglet.image.ImageData(width, h, "RGBA", rgba.tobytes()).save(str(path))


def record_video(player: Player, path: str | Path, seconds: float, *, fps: int = 60,
                 size: tuple[int, int] = (1920, 1080), fade_out: float = 6.0,
                 fade_in: float = 0.6, crf: int = 16, log=print) -> None:
    """Render `seconds` of the current song with the visualiser to an MP4."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH (e.g. `brew install ffmpeg`)")
    w, h = size
    nframes = int(round(seconds * fps))
    off = Offscreen(w, h)
    dash = Dashboard(player, w, VIDEO_LAYOUT, footer=CREDIT, progress=True,
                     max_points=1600, line_width=1.6)
    timeline = _Timeline(player)
    tmpdir = Path(tempfile.mkdtemp(prefix="pysid-video-"))
    video_only = tmpdir / "video.mp4"
    wav = tmpdir / "audio.wav"
    colour = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
              "-color_range", "tv"]
    enc = subprocess.Popen(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
         "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
         "-vf", "vflip,scale=out_color_matrix=bt709:out_range=tv",
         "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
         *colour, str(video_only)],
        stdin=subprocess.PIPE)
    t0 = time.perf_counter()
    try:
        for f in range(nframes):
            t = f / fps
            timeline.advance(t)
            brightness = min(1.0, t / fade_in if fade_in else 1.0,
                             (seconds - t) / min(fade_out, 2.0) if fade_out else 1.0)
            dash.update(player, t, status2=chip_line(player), duration=seconds,
                        brightness=brightness)
            enc.stdin.write(off.render(dash.draw))
            if f % (fps * 5) == 0 and log:
                el = time.perf_counter() - t0
                eta = el / max(f, 1) * (nframes - f)
                log(f"\r  frame {f}/{nframes}  ({t:5.1f} s)  eta {eta:4.0f} s", end="", flush=True)
    finally:
        enc.stdin.close()
        enc.wait()
        off.close()
    if enc.returncode:
        raise RuntimeError("ffmpeg failed while encoding video")

    audio = timeline.track(nframes / fps).astype(np.float64)
    if fade_out:
        n = min(len(audio), int(fade_out * player.sample_rate))
        audio[-n:] *= 0.5 * (1 + np.cos(np.linspace(0, np.pi, n)))
    write_wav(wav, audio.astype(np.float32), player.sample_rate)
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(video_only), "-i", str(wav),
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "320k", "-ac", "2",
                    "-movflags", "+faststart", "-shortest", str(path)], check=True)
    shutil.rmtree(tmpdir, ignore_errors=True)
    if log:
        log(f"\r  wrote {path}: {nframes} frames, {nframes / fps:.1f} s, "
            f"in {time.perf_counter() - t0:.0f} s" + " " * 20)
