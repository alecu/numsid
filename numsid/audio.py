"""Real-time output through pyglet's media player.

pyglet's audio worker thread pulls PCM from `PlayerSource.get_audio_data`,
which runs the emulator just in time.  The emulator renders several times
faster than real time, so that thread stays well ahead of the sound card.
"""

from __future__ import annotations

import threading
import time

import pyglet
from pyglet.media.codecs.base import AudioData, AudioFormat, StreamingSource
from pyglet.media.drivers.base import AbstractAudioPlayer

from .player import Player


def set_latency(seconds: float) -> None:
    """How much audio pyglet keeps queued ahead of the speakers.  Lower is
    snappier (mute/subtune keys), higher is more robust to hiccups."""
    AbstractAudioPlayer.audio_buffer_length = seconds


class PlayerSource(StreamingSource):
    def __init__(self, player: Player):
        self.player = player
        self.audio_format = AudioFormat(channels=player.channels, sample_size=16,
                                        sample_rate=player.sample_rate)
        self.lock = threading.RLock()     # guards the emulator against the UI thread
        self.render_time = 0.0            # seconds spent emulating (for the CPU meter)
        self.rendered = 0                 # samples handed to pyglet

    def is_precise(self) -> bool:
        return True

    def seek(self, timestamp: float) -> None:
        pass                              # an emulated tune can't be rewound cheaply

    def get_audio_data(self, num_bytes, compensation_time=0.0):
        n = int(num_bytes) // (2 * self.player.channels)
        if n <= 0:
            return None
        t0 = time.perf_counter()
        with self.lock:
            pcm = self.player.render_int16(n)
        self.render_time += time.perf_counter() - t0
        self.rendered += n
        data = pcm.tobytes()
        return AudioData(data, len(data))


class LivePlayback:
    """A Player hooked to a pyglet media player, with subtune switching."""

    def __init__(self, player: Player, latency: float = 0.4):
        set_latency(latency)
        self.player = player
        self.source = PlayerSource(player)
        self.media = pyglet.media.Player()
        self.media.queue(self.source)

    def play(self) -> None:
        self.media.play()

    def toggle_pause(self) -> None:
        if self.media.playing:
            self.media.pause()
        else:
            self.media.play()

    @property
    def position(self) -> float:
        """Seconds of the current song that have been heard."""
        return self.media.time

    def skip(self, direction: int) -> None:
        """Next or previous subtune (SID) or order position (MOD)."""
        with self.source.lock:
            self.player.skip(direction)
        self.media.seek(0.0)              # flush audio already queued for the old song

    def cpu_load(self) -> float:
        """Emulation time per second of audio produced."""
        if not self.source.rendered:
            return 0.0
        return self.source.render_time / (self.source.rendered / self.player.sample_rate)


def play_headless(player: Player, seconds: float | None = None, latency: float = 0.5) -> None:
    """Play without a window, printing progress; Ctrl-C stops."""
    live = LivePlayback(player, latency)
    live.play()
    print(f"Playing {player.title} - {player.subtitle} ({player.chip_line()}). "
          "Ctrl-C to stop.")
    try:
        while seconds is None or live.position < seconds:
            time.sleep(0.25)
            pos = live.position
            print(f"\r  {int(pos) // 60:02d}:{pos % 60:04.1f}   emulation load "
                  f"{100 * live.cpu_load():3.0f}%", end="", flush=True)
            pyglet.clock.tick()
    except KeyboardInterrupt:
        pass
    finally:
        print()
        live.media.pause()
        live.media.delete()
