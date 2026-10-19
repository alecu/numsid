# Architecture

The code is split by machine. `numsid/c64/` holds the C64: the 6502, the
machine around it, the SID and the SID player. `numsid/amiga/` holds the
Amiga: the MOD and MED loaders, PowerPacker decrunching, the ProTracker and
MED replayers, Paula and the module player.
What both share sits in `numsid/` itself: the `Player` base class, format
detection (`tunes.py`), resampling, audio output, the visualiser and video
recording.

* [C64 SID playback](c64.md): the 6502, the machine around it, and the SID.
* [Amiga module playback](amiga.md): the MOD and MED loaders, the
  ProTracker and MED replayers, and Paula.
* [Testing](testing.md): how the emulation is checked.

## Live playback

Scopes and register readouts follow what you are *hearing*: the emulator runs
ahead of the sound card, and the display looks up the state for the current
playback position.

## Video recording (`record.py`)

`--video` renders the visualiser offline on a fixed 60 fps timeline into an
offscreen 4× multisampled framebuffer. The emulator's own audio for that
timeline is encoded with it, so sync is exact and no frames drop. By default
it detects where the song's notes start repeating, records one full loop, and
fades out over the restart (Commando's main theme loops after 3:55.6). A
jingle that simply stops is recorded until its last note has died away. For a
MOD the replayer itself works out the length: the song is over when it
returns to a row it has already played (pattern loops aside), or when it hits
`F00` (`FFE` in a MED song).

## Performance

Rendering Commando costs about 0.13 s of CPU per second of audio offline, and
about 25–40% of one core during live playback with the visualiser open. A MOD
costs about 0.06 s of CPU per second of audio.
