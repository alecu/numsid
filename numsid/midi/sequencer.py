"""Playing a MIDI file's events at sample-exact times.

The sequencer turns the merged event list into output-sample positions,
once, up front, and then hands a synth the events that fall before a given
sample.  A synth renders up to the next event, applies it, and carries on,
so every message lands on the sample it belongs to whatever the block size.
"""

from __future__ import annotations

import bisect

from .smf import Event, MidiFile


class Sequencer:
    def __init__(self, midi: MidiFile, sample_rate: int):
        self.midi = midi
        self.sample_rate = sample_rate
        # meta events only steer timing, which is already in the sample positions
        self.events: list[Event] = [e for e in midi.events if e.status != 0xFF]
        self.times: list[int] = [round(midi.seconds(e.tick) * sample_rate) for e in self.events]
        self.end = round(midi.length * sample_rate)
        self.index = 0

    @property
    def finished(self) -> bool:
        return self.index >= len(self.events)

    def next_time(self) -> int | None:
        """Sample position of the next event, or None at the end."""
        return None if self.finished else self.times[self.index]

    def take(self, until: int) -> list[tuple[int, Event]]:
        """(sample, event) for every remaining event before sample `until`."""
        j = bisect.bisect_left(self.times, until, self.index)
        out = list(zip(self.times[self.index:j], self.events[self.index:j]))
        self.index = j
        return out

    def seek(self, sample: int) -> list[Event]:
        """Jump to `sample`.  Returns the events skipped over that set up
        state (programs, controllers, pitch bend, SysEx), so a synth can
        apply them and sound right from there; notes are dropped."""
        j = bisect.bisect_left(self.times, sample)
        setup = [e for e in self.events[:j] if e.status >= 0xB0]
        self.index = j
        return setup
