"""Playing a General MIDI file on an MT-32.

GM program numbers mean different instruments from the MT-32's factory
patches, so each GM program is sent to the closest MT-32 patch instead.
The choices go by the MT-32 patch names (see `mt32.control`); where the
MT-32 has nothing close, the nearest family member stands in.

Drums need less: the GM percussion map grew out of the MT-32's rhythm
setup, so keys 35-75 mostly mean the same thing on both.  The GM keys the
MT-32 leaves empty are moved to the nearest sound it does have, or dropped.

GM plays drums on channel 10 and melody on 1-16; the MT-32 listens to
channels 2-10 by default.  `channel_map` folds the file's melodic channels
onto the MT-32's eight parts, busiest first.
"""

from __future__ import annotations

from collections import Counter

# GM program -> MT-32 factory patch
GM_TO_MT32 = (
    0, 1, 2, 7, 3, 4, 16, 19,                  # pianos, harpsichord, clavinet
    22, 101, 23, 97, 104, 103, 102, 99,        # chromatic percussion
    8, 9, 10, 12, 13, 15, 87, 15,              # organs, accordion, harmonica
    59, 60, 61, 62, 61, 62, 62, 59,            # guitars
    64, 66, 67, 70, 68, 69, 28, 29,            # basses
    52, 53, 54, 56, 50, 51, 57, 112,           # solo strings, harp, timpani
    48, 49, 50, 49, 34, 34, 39, 122,           # ensembles, choir, orchestra hit
    88, 90, 94, 89, 92, 95, 24, 25,            # brass
    78, 79, 80, 81, 84, 85, 86, 82,            # reeds
    74, 72, 76, 77, 110, 107, 108, 109,        # pipes
    47, 44, 73, 111, 62, 39, 26, 30,           # synth leads
    32, 36, 33, 34, 35, 40, 46, 37,            # synth pads
    41, 36, 38, 37, 32, 43, 43, 42,            # synth effects
    63, 60, 105, 105, 99, 106, 52, 84,         # ethnic
    100, 115, 126, 120, 117, 113, 116, 119,    # percussive
    59, 111, 119, 124, 123, 116, 119, 114,     # sound effects
)

# GM drum keys the MT-32's rhythm setup leaves empty -> a key it plays, or
# None to drop the note
GM_DRUMS_TO_MT32 = {
    52: 49,      # Chinese cymbal -> crash
    53: 51,      # ride bell -> ride
    55: 49,      # splash -> crash
    57: 49,      # crash 2 -> crash
    58: None,    # vibraslap
    59: 51,      # ride 2 -> ride
    74: 73,      # long guiro -> quijada
    76: 75,      # high wood block -> claves
    77: 75,      # low wood block -> claves
    78: None,    # mute cuica
    79: None,    # open cuica
    80: None,    # mute triangle
    81: None,    # open triangle
}

MT32_RHYTHM_CHANNEL = 9           # 0-based: channel 10
MT32_PART_CHANNELS = range(1, 9)  # 0-based: channels 2-9


def gm_drum_key(key: int) -> int | None:
    return GM_DRUMS_TO_MT32.get(key, key)


def channel_map(midi) -> dict[int, int]:
    """0-based file channel -> 0-based MT-32 channel, for a GM file.  The
    drum channel stays on 10; melodic channels already on 2-9 keep their
    place, and the rest take the free parts, busiest first.  Channels that
    find no part are left out (their notes are dropped)."""
    notes = Counter(e.channel for e in midi.channel_events() if e.is_note_on)
    out = {MT32_RHYTHM_CHANNEL: MT32_RHYTHM_CHANNEL} if notes[MT32_RHYTHM_CHANNEL] else {}
    melodic = [c for c, _ in notes.most_common() if c != MT32_RHYTHM_CHANNEL]
    for c in melodic:
        if c in MT32_PART_CHANNELS:
            out[c] = c
    free = [c for c in MT32_PART_CHANNELS if c not in out.values()]
    for c in melodic:
        if c not in out and free:
            out[c] = free.pop(0)
    return out
