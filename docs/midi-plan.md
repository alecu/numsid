# Plan: General MIDI and MT-32 playback

Status: milestones 1 and 2 are done and milestone 3 has a first version
(theme.mid plays); the rest is proposal. The goal is to play Standard MIDI Files the way numsid plays SIDs
and MODs: the sound hardware emulated in Python + numpy, with live
playback, `--wav`, `--video` and the visualiser. The first target is
`theme.mid` (The Secret of Monkey Island).

## What we have

### The ROMs

The ROM zip holds the MAME set. Checked by content, not by name:

| Files | What they are |
|-------|---------------|
| `mt32_ctrl_1_04/05/06/07.rom` (64 KB) | MT-32 "old" control ROMs, v1.04 to v1.07 (1987) |
| `mt32_ctrl_bluer.rom` (64 KB) | "Blue Ridge" modified MT-32 ROM, `verX.XX 30 Sep 88` |
| `mt32_ctrl_2_04/06/07.rom` (128 KB) | MT-32 "new" control ROMs, v2.04 to v2.07 |
| `cm32l_ctrl_1_00/1_02.rom`, `cm32ln_ctrl_1_00.rom` (64 KB) | CM-32L / LAPC-I and CM-32LN control ROMs |
| `mt32_pcm.rom` (512 KB) | MT-32 PCM sample ROM |
| `cm32l_pcm.rom` (1 MB) | CM-32L PCM ROM: the MT-32 ROM plus 512 KB of extra sound effects |
| `*_a.rom` / `*_b.rom`, `*_pcm_l/h.rom` | MAME's split chips: `_a`/`_b` interleave byte by byte into the full control ROM, `_l` + `_h` concatenate into the PCM ROM. Redundant; we only need the full images. |

So one control ROM plus one PCM ROM makes a working module. Default:
`mt32_ctrl_1_07.rom` + `mt32_pcm.rom`, the original MT-32 that the LucasArts
and Sierra soundtracks were written for. `--model CM-32L` would pick
`cm32l_ctrl_1_02.rom` + `cm32l_pcm.rom`.

### The MIDI files

All 14 are format 1, 192 ticks per quarter note, and all 14 are **General
MIDI**. Most are Quest Studios' GM conversions of the Monkey Island
soundtrack, and say "GENERAL MIDI VERSION" in a text event; their program
numbers read as sensible GM instruments (pan flute, marimba, drawbar organ,
acoustic bass). The rest are other GM arrangements: `melee` and `voodoo1`
from NoteWorthy Composer, `lechuck` (GM System On SysEx) and `stan1`
(Yamaha XG reset).

The Quest Studios files are easy to mistake for MT-32 files: they keep the
MT-32 channel layout (melody on 2-9, drums on 10, nothing on 1), and
`flove.mid` even carries the site's "FOR THE ROLAND MT-32 ... ONLY" banner.
But they say they are GM, and played as MT-32 patches their programs come
out as harpsichords and bells. So none of the samples is MT-32 native.

`theme.mid`: 15 tracks, 2:54 at 120 BPM, channels 2-8 and 10, 3,900 notes
of which 1,217 are drums, at most 16 notes sounding at once. Its GM
instruments are Pan Flute and Drawbar Organ (ch 2), Xylophone and Marimba
(3), Drawbar Organ and Acoustic Bass (4), Marimba (5), Acoustic Bass (6),
Synth Bass 1 (7), Blown Bottle (8), and GM drums.

That gives theme.mid two routes to the speakers:

* **On the emulated MT-32, through a GM-to-MT-32 map.** The MT-32's factory
  sounds include Panpipes, Marimba, Xylophone, electric organs and Acou
  Bass, and the GM drum map grew out of the MT-32's rhythm map, so the map
  loses little on this song. This is how games of the era played GM music
  on an MT-32, and it uses the ROMs we have.
* **On a General MIDI synth**, as the file was written. That needs a GM
  sound source: a SoundFont the user supplies (see General MIDI below).

## Where the code goes

Following the one-subpackage-per-machine convention:

```
numsid/midi/       shared by every MIDI synth
  smf.py           Standard MIDI File parser (formats 0/1, running status,
                   SysEx, tempo map), merged event list in ticks and seconds
  sequencer.py     plays the event list against a synth at sample-exact
                   times; song length, seeking, looping
  detect.py        MT-32 vs GM vs GS/XG, from SysEx, channel use and text
  maps.py          MT-32 <-> GM program and drum-key maps, for playing a
                   file on the "wrong" synth
  player.py        MidiPlayer(Player): readouts, mute, skip by measure
numsid/mt32/       the Roland MT-32 / CM-32L
  roms.py          find, identify (by SHA-1) and load ROMs; join MAME halves
  control.py       tables from the control ROM: timbres, patches, rhythm
                   setup, PCM sample list, envelope/LFO lookup tables
  pcm.py           unscramble and decode the PCM ROM
  la32.py          the LA32 partial generator (square/saw, PCM, ring mod)
  partial.py       TVA, TVF and TVP envelopes, LFO, pitch
  synth.py         parts, polyphony, partial reserve and allocation, the
                   MIDI and SysEx implementation, system memory
  reverb.py        the BOSS reverb chip: room, hall, plate, tap delay
numsid/gm/         a General MIDI synth (see below)
  sf2.py           SoundFont 2 loader
  sampler.py       voices: looped samples, envelopes, LFOs, filter
```

`tunes.py` grows a third branch: an `MThd` header opens a `MidiFile`, and
`open_player` picks the synth from `--model` (`MT-32`, `CM-32L`, `GM`) or,
by default, from `detect.py`. The `Player` contract stays as it is: the synth
is the `chip`, with `take()`, `out_len`, `clock` (32,000 Hz for the MT-32,
its native rate), a resampler, `mute` per part, and scope taps per part.

## The MT-32 engine

### High level, not CPU level

The C64 side runs the tune's own 6502 code. The MT-32's equivalent would be
emulating its Intel 8095 microcontroller running the control ROM, plus the
LA32 sound chip and the reverb chip at the bus level. MAME is part way there;
nobody has made it play right.

The plan is the high-level route instead, the same choice the Amiga side
made by reimplementing the ProTracker replayer: read the timbre and table
data out of the control ROM, and implement what the firmware does with it in
Python. This is how Munt (mt32emu) works, and Munt is accurate enough that
it is the reference everyone compares against. The control ROM still
matters: its tables decide every sound, and version differences (1.07 vs
2.07 vs CM-32L) come through.

### Clean room (decided 2026-10-17)

Munt is LGPL-2.1+, and porting it would have made `numsid/mt32` a
derivative work. Instead the MT-32 engine is written clean room: from the
ROM contents themselves, published descriptions of the hardware, and
measurements against renders. Munt's source is not read. Munt is used only
as a black-box test oracle (`mt32emu-smf2wav` renders to compare against),
never as a dependency. That is slower going, and early milestones will be
less accurate than Munt, but leaves numsid's licence unconstrained.

### Pieces, in build order

1. **ROMs.** Look in `roms/`, then `$NUMSID_ROMS`, then `--roms DIR`.
   Identify by SHA-1, so names don't matter, and join `_a`/`_b` and `_l`/`_h`
   halves when only those exist.
2. **Control ROM tables.** The 128 factory patches, the 4 timbre groups
   (A, B, the internal memory and rhythm), the 85-key rhythm setup, the PCM
   sample descriptors, and the lookup tables for pitch, envelope time and
   key follow. Offsets differ per ROM version, so `roms.py` carries a small
   table of offsets keyed by SHA-1. `--info` can then name the instruments,
   both the MT-32 timbre a patch number selects and, for a GM file, the
   MT-32 timbre the GM map sends it to.
3. **PCM ROM decode.** The MT-32's PCM ROM has its address and data lines
   scrambled on the board, and samples are stored in the LA32's own
   log-like format. Unscramble once at load and cache as int16/float32.
4. **LA32 partials.** Each note uses one to four partials, combined in
   pairs by one of 13 structures (two synths, synth + PCM, ring modulated,
   etc). A synth partial generates a square or sawtooth whose
   "filtering" happens inside the generator: cutoff and resonance shape the
   waveform directly through log-sine and exponent tables, so there is no
   recursive filter to run per sample. That makes it vectorisable over a
   block, the same way the SID oscillators are. PCM partials play ROM
   samples, looped or one-shot, at a pitch-dependent step.
5. **Envelopes and modulation.** TVA (amplitude), TVF (cutoff) and TVP
   (pitch) are 5-segment envelopes with ROM-defined rate tables, velocity
   and key follow, plus the LFO, pitch bend, modulation wheel, sustain
   pedal, aftertouch and volume/expression/pan controllers. Envelopes
   advance in steps, so a block is cut where a step changes and each piece
   is a linear ramp in the log domain: still vectorised.
6. **Parts and partials.** 8 melodic parts on MIDI channels 2-9 and the
   rhythm part on 10 by default, 32 partials shared between them with the
   partial reserve (3, 10, 6, 4, 3, 0, 0, 0, rhythm 6 by default) and the
   firmware's stealing order. Getting this exactly as the firmware does is
   what keeps busy passages from dropping the wrong notes.
7. **Mixing and DAC.** Per-partial pan, the reverb send, and the old MT-32's
   DAC quirk (the 1.x units lose the top bit, which makes loud passages
   distort in a recognisable way). The quirk is on for `MT-32` and off for
   `CM-32L`, with a switch, like `--no-digiboost`.
8. **Reverb.** The BOSS chip's room, hall, plate and tap delay modes, as
   delay-line networks. Feedback loops are longer than a block, so blocks of
   at most the shortest delay can be computed with numpy slices instead of
   per sample.
9. **SysEx.** Roland DT1 writes into patch, timbre, rhythm and system
   memory (custom timbres, partial reserve, channel assignment, reverb), the
   LCD message area, and reset. None of the sample files need it, but most
   Sierra and many LucasArts files in the wild do.

### Visualiser

Nine rows instead of three or four voices: parts 1-8 and rhythm, each with a
scope, the timbre name, the last note, partials in use and a level meter.
The chip lines show the 32-partial map, reverb mode, master volume, and the
MT-32's 20-character LCD, which shows what the firmware would (part and
volume, or a SysEx message).

## General MIDI

The MT-32 is not a GM device, so GM files need a second synth. The plan is a
SoundFont 2 sampler: the user puts an `.sf2` file in `roms/` (GeneralUser GS
is free and sounds good; FluidR3_GM is the common alternative), and numsid
plays GM through it. The engine is close to Paula with more per voice:
looped samples with interpolation, the volume and modulation envelopes,
two LFOs, a 2-pole resonant low-pass, pan, and the reverb/chorus sends.
Default SF2 modulators and GM drum channel 10 are handled; GS/XG reset
SysEx are accepted and treated as GM.

The low-pass is the one recursive part. The plan is to run it per sample
but across all active voices at once, so the Python loop runs once per
sample rather than once per voice, and to skip it on voices whose filter is
wide open. If that is too slow for live playback, the fallback is updating
the coefficients per small block and filtering each block with a closed form.

Without an SF2, GM files play on the emulated MT-32 through `maps.py`: GM
programs and drum keys mapped to the nearest MT-32 patch and rhythm key
(the approach ScummVM and Sierra used). How close it gets depends on
the song: theme.mid's instruments all have good MT-32 equivalents.

The same maps work the other way: `--model GM` on an MT-32 file maps MT-32
patches to GM programs, for comparison or for people without the ROMs.

## Media and the repository

ROMs and MIDI files stay out of git, like `sids/` and `mods/`. `.gitignore`
has `/roms/`, `/midi/` (anchored, so `numsid/midi/` is not caught),
`*.mid`, `*.rom` and `*.sf2`. Tests that need them
are skipped when they are missing, and synthetic SMF data built in the tests
covers the parser and sequencer without any media. The README gets a short
MIDI section on where to put the ROMs and how to play a file; the internals
go into `docs/midi.md` and `docs/mt32.md`, replacing this plan.

## Testing

* SMF parser and sequencer: hand-built files covering running status,
  tempo changes mid-note, format 0 vs 1, SysEx with `F7` continuation, and
  timing checked to the sample.
* ROM tables: timbre names and parameters for every control ROM version
  checked against known values.
* LA32: a naive per-sample reference model in `tests/`, like the SID and
  Paula ones, which the vectorised renderer must match exactly on random
  partial parameters.
* End to end: `theme.mid` compared with a Munt render (`mt32emu-smf2wav`,
  used only as a test oracle, never a dependency), by loudness-envelope
  correlation and spectrum, the way `axelf.mod` was checked against
  libopenmpt.

## Milestones

| # | Delivers | Audible result |
|---|----------|----------------|
| 1 | SMF parser, sequencer, detection, `--info` for MIDI files; media gitignored | **done**: `--info theme.mid` lists tracks, channels, GM instruments and length |
| 2 | ROM loading, control ROM tables, PCM decode, GM-to-MT-32 map | **done**: `--info` names the MT-32 timbres theme.mid would use |
| 3 | LA32 partials, envelopes, parts and partial allocation | **first version done**: theme.mid plays (live too), dry; the LA32 is modelled, to be refined against Munt renders |
| 4 | Reverb, DAC, live playback, the 9-part visualiser, `--video` | **theme.mid plays live on an MT-32** |
| 5 | SysEx, LCD, CM-32L | MT-32-native files with custom timbres |
| 6 | SoundFont 2 GM synth | theme.mid and the rest as authored, on a GM SoundFont |
| 7 | MT-32-to-GM map | MT-32-native files on the GM synth |

Milestones 2-4 and 6 are independent, so the SoundFont synth could come
first instead: it is much less work than the MT-32 and plays theme.mid as
written, but needs a SoundFont download. Each milestone lands as its own PR
with tests.

## Risks

* **Speed.** 32 partials at 32 kHz with per-step envelopes is more work than
  three SID voices. Vectorising each partial over a block should keep it
  within live playback; the partial count in use (usually 10-20) is what it
  costs, not the maximum.
* **Firmware detail.** The MT-32's character is in small firmware rules
  (partial stealing, envelope edge cases, the 1.x vs 2.x differences).
  The Munt comparison test is what catches these.
* **SF2 quality** depends on the user's SoundFont; numsid ships none.
