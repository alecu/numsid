# Amiga module playback

How `numsid/amiga/` turns a MOD or MED file into sound. See
[architecture.md](architecture.md) for how the packages fit together.

```mermaid
flowchart TD
    file["module file<br/>PowerPacker ones unpacked first"] --> mod["<b>amiga/modfile.py</b><br/>MOD: samples, order list, patterns<br/>sample data laid out as chip RAM"]
    file --> medf["<b>amiga/medfile.py</b><br/>MED / OctaMED: blocks,<br/>samples and synth instruments"]
    mod --> pt["<b>amiga/protracker.py</b><br/>ProTracker 2.3D replay routine<br/>one tick per CIA timer interrupt"]
    medf --> medr["<b>amiga/med.py</b><br/>MED replay routine<br/>runs the synth tables too"]
    pt -- "register writes:<br/>location, length, period,<br/>volume, DMA on/off, LED" --> ch
    medr -- "the same register writes" --> ch

    subgraph paula ["amiga/paula.py"]
        ch["4 DMA channels, exact to the colour clock<br/>byte held for period colour clocks,<br/>block reload from the latches"] --> pan["panning<br/>1 + 4 left, 2 + 3 right"]
    end

    pan --> res["<b>resample.py</b><br/>709 kHz → 44.1 kHz<br/>with the A500's 4.4 kHz RC low-pass"]
    res --> led["LED filter<br/>2-pole 3.1 kHz, switched by the song"]
    led --> hp["5 Hz output high-pass"]
    hp --> lim["soft limiter above 90%"]
    lim --> out["live playback, WAV, video"]
```

* **Replayer (`amiga/protracker.py`)**: ProTracker 2.3D's routine, which most MODs
  were written for. It runs as a CIA interrupt (`1773447 / BPM` timer
  cycles, 50 Hz at 125 BPM), reads a row every `speed` ticks, and runs the
  effects in between. It writes Paula's registers the way the 68000 code
  does, and keeps its quirks:
  * sample offset (`9xx`) is applied twice, so retriggers start further in,
  * arpeggio on a finetune −1 sample reads past the period table,
  * the tremolo's ramp waveform looks at the vibrato position,
  * a BPM change takes effect one tick late, when the CIA timer reloads,
  * an instrument without a note swaps the sample in when the playing one
    ends,
  * `EFx` inverts sample bytes in memory as they play.

  The behaviour follows 8bitbubsy's [pt2-clone](https://github.com/8bitbubsy/pt2-clone),
  a faithful port of the original code. The period, vibrato and funk tables
  come from it too.
* **MED replayer (`amiga/med.py`)**: MED and OctaMED songs are made of
  blocks of any length, and their tempo is in MED's own units, which
  `med_bpm` converts. Notes are numbered from C-1. On 4-channel songs the
  ones above the third octave wrap down into it. Track and master volumes
  scale every note, instruments can have a hold/decay envelope (an
  instrument number without a note, MED's "hold" symbol, keeps a note
  going), and slides also run on a line's first tick unless the song asks
  for SoundTracker-style ones. Synthetic instruments are short looped
  waveforms driven by two small programs, a volume table and a waveform
  table. The replayer steps through both: set or ramp the volume, follow an
  envelope, pick a waveform, ramp the pitch, arpeggio, vibrato, wait and
  jump. A new waveform goes to Paula's latches, so it takes over when the
  current one ends. The semantics follow libopenmpt's MED loader and synth
  code, read as documentation; the code is new.
* **PowerPacker (`amiga/powerpacker.py`)**: many Amiga modules were
  crunched with PowerPacker. Its bit stream is read backwards, and the
  output is rebuilt from its last byte to its first, out of literal runs
  and copies of bytes already written. `PP20` files are unpacked before
  their format is detected.
* **Paula (`amiga/paula.py`)**: each channel plays its block from chip RAM, a byte
  per `period` colour clocks (3,546,895 Hz, PAL), and reloads the location
  and length latches when the block ends. That is how loops work, and how a
  sample swap happens. Period and volume take effect from the next byte.
  The output is rendered at the E clock (colour clock / 5), and every value
  is the exact average of the staircase over its five colour clocks,
  computed in integers. A naive colour-clock-stepped model checks it. The
  steps are kept as Paula makes them, so the images they produce around
  multiples of each sample's playback rate (the Amiga's crispness) are
  damped only by the analogue filters.
* **Output stage**: the A500's 360 Ω / 0.1 µF low-pass (4.4 kHz), or the
  A1200's 34 kHz one, is folded into the resampler's kernel, as the C64's RC
  filter is for the SID. The LED filter (Sallen-Key, 3.09 kHz, Q 0.66) runs
  at the output rate as a block-wise state-space IIR, so numpy does the
  work. It runs all the time, and `E0x` only switches its output in, so
  toggling it doesn't click. Component values are from the A500 and A1200
  schematics, as collected by pt2-clone. Peaks that would go past full
  scale (the output high-pass can lift a loud passage there) go through a
  soft limiter instead of clipping; below 90% of full scale the signal is
  untouched.

## Known gaps

* PAL timing only.
* Every MOD plays with ProTracker 2.3D semantics, including multichannel
  ones written in PC trackers.
* MED songs: MIDI instruments and packed samples stay silent, and the
  play-sequence commands of MMD2/MMD3 are ignored. Each track gets a Paula
  channel of its own, where OctaMED's 5-8 channel mode mixes two tracks per
  channel in software ([ISSUES.md](../ISSUES.md), M9).
* Other Amiga music formats (TFMX, Future Composer, Oktalyzer, the old
  `MED` format, XPK-packed files and the many DeliTracker players) are not
  supported (M8).
