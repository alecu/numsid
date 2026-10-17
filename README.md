# numsid

A Commodore 64 SID and Amiga MOD music player written in Python. For SID
tunes the MOS 6502 CPU runs the tune's own player code, and the MOS 6581/8580
SID chip is synthesised cycle by cycle with numpy. For Amiga tracker modules
a ProTracker 2.3D replay routine drives Paula, the Amiga's sound chip, which
is emulated with exact sample timing and the A500/A1200 output filters. No
emulation or tracker libraries are used, only `numpy` for the maths and
`pyglet` for audio output and the window.

[![Video: numsid playing Commando by Rob Hubbard](https://img.youtube.com/vi/QAMZvv-m03c/maxresdefault.jpg)](https://www.youtube.com/watch?v=QAMZvv-m03c)

▶ [Watch numsid play the whole Commando main theme on YouTube](https://www.youtube.com/watch?v=QAMZvv-m03c)
(rendered with `--video`).

```
python -m numsid sids/Commando.sid
```

![numsid playing Rob Hubbard's Commando: three voice oscilloscopes with
note, waveform and ADSR readouts, and the mixed output](docs/screenshot.png)

Playback is real time.

## Setup

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[test]'
```

(or `python -m venv .venv && .venv/bin/pip install -e '.[test]'`)

SID tunes are not included in this repository. Get `Commando.sid` (Rob Hubbard,
1985) from the [High Voltage SID Collection](https://www.hvsc.c64.org/), under
`MUSICIANS/H/Hubbard_Rob/`, and put it in `sids/`. Any other PSID file works
the same way. Tests that need the tune are skipped when it is missing.

MODs aren't included either. [The Mod Archive](https://modarchive.org/) has
thousands. The tests use `axelf.mod` (module
[#215379](https://modarchive.org/module.php?215379)) from `mods/` when it's
there.

## Usage

```bash
.venv/bin/python -m numsid sids/Commando.sid                 # window + live audio
.venv/bin/python -m numsid sids/Commando.sid --song 2        # pick a subtune
.venv/bin/python -m numsid sids/Commando.sid --headless      # audio only, Ctrl-C stops
.venv/bin/python -m numsid sids/Commando.sid --wav commando.wav --seconds 240
.venv/bin/python -m numsid sids/Commando.sid --model 8580 --no-filter --gain 1.5
.venv/bin/python -m numsid sids/Commando.sid --model 8580 --no-digiboost  # authentic C64C samples
.venv/bin/python -m numsid sids/Commando.sid --info          # header only
```

Window keys: `←/→` subtune, `1 2 3` mute a voice, `SPACE` pause, `F` filter
on/off, `M` switch between 6581 and 8580, `ESC` quit. A muted voice keeps its
scope, drawn in gray, and loses its envelope meter.

![Subtune 2: sawtooth lead on voice 2, detuned pulse on voice 1, voice 3
muted](docs/screenshot-song2.png)

### Amiga MODs

```bash
.venv/bin/python -m numsid mods/axelf.mod                    # window + live audio, stereo
.venv/bin/python -m numsid mods/axelf.mod --pos 12           # start at order position 12
.venv/bin/python -m numsid mods/axelf.mod --model A1200      # no 4.4 kHz output filter
.venv/bin/python -m numsid mods/axelf.mod --separation 30    # narrower stereo, for headphones
.venv/bin/python -m numsid mods/axelf.mod --info             # header and sample list
```

The file type is detected from the contents. ProTracker, NoiseTracker and
StarTrekker modules (`M.K.`, `FLT4` and the like) play, as do the original
15-sample SoundTracker ones and PC-era multichannel MODs (`6CHN`, `8CHN`,
`xxCH`). Output is stereo with the Amiga's hard left/right panning: channels
1 and 4 left, 2 and 3 right. In the window `←/→` step through the order list,
`1`-`4` mute channels, `F` makes the LED filter ignore the song's `E0x`
commands, and `M` switches between the A500 and A1200 output stages. The
readouts show each channel's note, period and sample rate, sample, volume and
effect, plus the song position, speed and BPM.

![numsid playing axelf.mod: four channel oscilloscopes with note, sample and
volume readouts, and the stereo output mixed down](docs/screenshot-mod.png)

### Videos and screenshots

```bash
.venv/bin/python -m numsid sids/Commando.sid --video commando.mp4     # 1080p60 MP4
.venv/bin/python -m numsid sids/Commando.sid --screenshot shot.png --at 52
```

`--video` renders offline, so sync is exact and no frames drop. By default it
records one full loop of the song and fades out over the restart; a jingle
that simply stops is recorded until its last note has died away. Use
`--seconds`, `--fps`, `--size 2560x1440` and `--fade` to override, and
`--mute 1,3` to silence voices. It needs `ffmpeg` on the PATH and encodes
H.264 + AAC (BT.709), ready for YouTube.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Limitations

* One SID chip (no 2SID/3SID tunes).
* No BASIC or character ROM, and only a stand-in KERNAL. Tunes that call
  real ROM routines get a harmless return.
* MODs play with PAL timing and ProTracker 2.3D semantics only.

[ISSUES.md](ISSUES.md) has the full list of known bugs and missing features,
with a compatibility table for the HVSC Top 100: all 100 now play, RSID files
included.

## How it works

* [Architecture](docs/architecture.md): how the code is laid out, video
  recording, performance.
* [C64 SID playback](docs/c64.md): the 6502, the machine, the SID chip.
* [Amiga MOD playback](docs/amiga.md): the ProTracker replayer and Paula.
* [Testing](docs/testing.md): reference models and how accuracy is checked.
