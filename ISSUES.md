# Known issues and pending features

Found by running the [HVSC Top 100](https://www.hvsc.c64.org/) (2005 vote
list, `Top100.lst`) through numsid in vote order. Each tune was checked
with an instrumented 6502 run (60 s of play calls, logging every I/O access
outside the SID) and an offline render (20 s). Every flagged tune was then
traced by hand to find the cause. Tunes #2-#13 were also played through the
speakers for 30 s each, before live playback was dropped from the survey.

Each entry says which tunes it affects, so the fixes can be prioritised by
how many popular tunes they unlock.

The Amiga entries (M1-M9) come from comparing real modules with libopenmpt:
a list of famous demo and game modules, every PowerPacked one, and about
200 MED songs from a large collection (M1). Their table is at the end.

## Suggested order

| Fix | Effort | Unlocks | Status |
|-----|--------|---------|--------|
| B2: preset volume 15 for PSIDs | one line | 2 silent tunes, and the start click on 34 more | fixed |
| B1: follow CIA timer writes during play | small, in `C64.play_frame` | 6 tunes with wrong tempo, including #1 Wizball | fixed |
| F2 + F1: RSID environment | large: CIA/VIC interrupts, continuous CPU | 14 RSID tunes | done |
| B3: read-modify-write dummy write | small, in the CPU | BMX Kidz, Nightdawn (garbled music) | fixed |
| B4: cycle-exact I/O timing | small, in the CPU | Savage (hung after 50 s) | fixed |
| F3: 8580 digi boost | small | `$D418` samples on the 8580 | done |
| O1: VIC bad lines | medium | RSID busy-loop timing | done (sprite DMA open) |
| O2: speed of sample-playing RSIDs | medium | headroom for live playback | open |
| M1: survey of real MODs | medium | confidence in the ProTracker replayer | done |
| M4: soft limiter instead of hard clipping | small | 8 of the top modules clipped | fixed |
| M5: PowerPacker | small | 24 packed files, Shadow of the Beast among them | done |
| M6: MED and OctaMED | large | 198 songs, Cannon Fodder and Xenon 2 among them | done |
| M7: MED synth instruments | medium | 16 songs | done |
| M8: other Amiga formats (TFMX, ...) | large, each | Turrican II and about 174 more TFMX files | open |

## Fixed

How the fixes were verified:
* Every Top 100 tune was rendered (10 s) before and after the fixes. 40 are
  bit-identical. 49 differ only in their first second, where the start-up
  thumps were removed. 11 changed: the 8 fixed tunes, Zig Zag and Short
  Circuit (CIA-timed, see below), and RSID #67, which is still broken.
* For the CIA-timed tunes, the song length numsid detects (where the notes
  start repeating, or the last note) now matches the HVSC Songlengths
  database: Wizball 214.9 s vs 214, SYS4096 168.6 vs 168, Ocean Loader 3
  169.6 vs 167, Defender of the Crown 59.8 vs 58, Zig Zag 237.1 vs 235,
  Short Circuit 239.5 vs 241. The "end" readings include 2.5 s for the last
  note's release.
* Across all PSIDs, 55 of the 68 tunes with a detectable length match
  HVSC within 3%. The other 13 are 3-10% off. Those are mostly VBI-timed,
  so their rate can't drift; the difference comes from how a loop point or
  an ending is defined.

### B1. CIA timer changes made by the play routine are ignored (fixed)

* **Affects:** #1 Wizball, #42 Defender of the Crown, #43 Ocean Loader 3,
  #55 Times of Lore, #63 The Baby of Can Guru, #84 SYS4096.
* **Symptom:** wrong tempo:
  * Wizball runs at about a third of the correct speed.
  * Times of Lore and SYS4096 run at 0.6× speed.
  * Ocean Loader 3 is about 34% too fast.
  * Defender of the Crown is about 20% too fast.
  * The Baby of Can Guru is about 1.3% too fast.
* **Cause:** for CIA-timed songs numsid reads the timer latch
  (`$DC04/$DC05`) once, after init, and calls play at that fixed rate.
  Wizball's init leaves the KERNAL's 60 Hz default. Its play routine then
  reprograms the latch on every call, cycling through `$0BCB`, `$1B15`,
  `$142D` and `$11B7`. Those sum to 19,652 cycles, about one PAL frame, so
  the player runs four times per frame at uneven intervals.
* **Other cases:**
  * Times of Lore (also Galway) alternates `$203D` and `$2C8B`, and
    SYS4096 alternates `$236F` and `$2957`. Each pair sums to one PAL
    frame: two calls per frame.
  * Ocean Loader 3's init also keeps the 60 Hz default. Every play call
    then writes `$56` to the latch high byte, giving `$5625` (44.7 Hz).
  * The Baby of Can Guru writes `$4100` (59.2 Hz) instead of the default
    `$4025` (60.0 Hz).
  * Defender of the Crown sets a 394 Hz timer (`$09C4`) in init. Its play
    routine stops the timer on entry (`$DC0E=$00`) and restarts it on
    exit (`$81`). Each period therefore stretches by the play routine's
    run time (about 20% on average), which numsid doesn't model.
* **Fix:** a model of the 6526 timers (`numsid/cia.py`) now schedules
  CIA-timed play calls from timer A's actual next underflow. Writes made by
  play behave as on the chip:
  * a new latch takes effect at the next reload,
  * force load (`$DC0E` bit 4) applies immediately,
  * stopping the timer pauses the countdown.

### B2. SID volume starts at 0; PSID rips expect 15 (fixed)

* **Affects:** #11 Rambo: First Blood Part II, #36 Parallax (both Martin
  Galway).
* **Symptom:** completely silent output, although the tune writes notes
  every frame.
* **Cause:** these rips never write `$D418`. Rambo's song 1 relocates the
  music driver, whose init clears SID registers 0-23 but not 24, the
  volume. The game set the volume elsewhere, and PSID players have
  traditionally reset the SID with volume 15 for exactly such rips. numsid
  starts at 0, as a powered-up chip does. With the volume preset to 15 both
  tunes play at normal levels (checked by rendering).
* **Second symptom: a loud click at song start.** 34 more PSIDs set the
  volume only in their first play call, not in init:
  * #1 Wizball, #2 The Last Ninja, #3 Sanxion, #7 Delta, #8 Monty on the
    Run, #10 Last Ninja 3, #12 Last Ninja 2, #13 Mutants, #14
    Knucklebusters, #26 Lightforce, #28 Robocop 3, #29 Myth, #30 The Master
    of Magic, #32 Trap, #33 Sweet, #41 Hysteria, #46 Mega Apocalypse,
  * #48 Thing Bounces Back, #53 Compleeto!, #54 Driller, #58 ACE II, #59
    Thrust, #63 The Baby of Can Guru, #66 Synth Sample, #70 R.I.S.K., #77
    Bulldog, #79 Ark Pandora, #81 Warhawk, #82 The Vikings, #84 SYS4096,
    #88 Battle Valley, #93 DNA Warrior, #94 Glider Rider, #96 Thunderforce.

  numsid settles the 6581's large DC offset right after init, at volume 0.
  When play then sets volume 15, the DC level jumps and comes out as a
  thump of about half full scale; Mutants and Hysteria peak at 0.48 in the
  first 50 ms.
* **Fix:**
  * PSIDs start with `$D418=$0F`; RSIDs keep the real-hardware state.
  * For the first second after init, the DC step caused by each volume
    change is absorbed into a constant offset. That covers tunes like #27
    Cobra, which write 0 in their first play call and fade in.
  * The offset stops tracking after 16 changes, so `$D418` sample playback
    keeps its full effect.

### F1. RSID support: a real interrupt-driven C64 (implemented)

* **Affects (RSID files):** #15 Arkanoid, #18 The Great Giana Sisters,
  #23 The Way of the Exploding Fist, #31 Turbo Outrun, #34 Stormlord,
  #44 To Be on Top, #47 Dulcedo Cogitationis, #49 Robocop, #57 Savage,
  #60 BMX Kidz, #67 Grand Prix Circuit, #68 Nightdawn, #71 Rocky Star,
  #100 Tetris.
* **What they do:** numsid only drives the PSID protocol (init once, then
  call play once per frame). RSID tunes instead program the hardware and
  expect interrupts to run them. Tracing their init routines shows:
  * Four inits never return (Arkanoid, Exploding Fist, Turbo Outrun, Rocky
    Star). They run their own main loop and expect interrupts to drive the
    music.
  * Most program VIC raster interrupts (`$D012/$D01A`) and/or CIA2 timer
    NMIs (`$DD04-$DD0E`). The NMIs are usually for sampled drums.
  * Several bank out the ROMs (`$01=$35`) and install their own `$FFFE` /
    `$FFFA` vectors.
  * Grand Prix Circuit and Arkanoid drive their music from CIA1 timer A at
    custom rates.
* **What numsid does with them today:** it falls back to calling the
  installed IRQ handler once per frame.
  * Five come out silent: Dulcedo Cogitationis, Savage, Nightdawn, BMX
    Kidz, and To Be on Top (which also crashes, see F2).
  * Nine make some sound: Arkanoid, Exploding Fist, Grand Prix Circuit,
    Rocky Star, Tetris, and Robocop (until it crashes, see F2), plus Giana
    Sisters, Turbo Outrun and Stormlord only very quietly. All have the
    wrong timing and lack their sampled parts, so none count as working.
* **Implemented:**
  * RSID tunes now run on a continuously running CPU (`C64._run_machine`).
    It runs to the next device event, then takes interrupts: IRQ is
    level-triggered and waits for the I flag; NMI fires on the edge.
  * CIA 1 / CIA 2 timers and interrupt control registers (`cia.py`).
  * The VIC raster counter and raster-compare IRQ with `$D019`
    acknowledge (`vic.py`).
  * `JMP *` and branch-to-self idle loops are skipped over, so idling
    costs nothing.
* **Verified:**
  * Synthetic tests cover a raster IRQ through `$0314`/`$EA81`, and a
    CIA IRQ through the tune's own `$FFFE` with the ROMs banked out and an
    init that never returns. They also cover CIA 2 NMIs at 9.7 kHz, all at
    the exact rates.
  * All 14 RSIDs now play; none is silent or crashes. Their interrupts run
    at the rates they program: the sample NMIs of Turbo Outrun, To Be on
    Top, Dulcedo Cogitationis and Giana Sisters at 1.4-6.8 kHz, and
    Arkanoid's music IRQ at 52.05 Hz, as its timer latch implies.
  * Robocop's song length matches HVSC (191.5 vs 193 s).
  * PSIDs were unaffected: 85 render bit-identically to the previous
    version. #66 Synth Sample (play address 0) changed slightly because it
    now goes through the KERNAL's real IRQ entry.

### F2. More KERNAL ROM entry points (implemented)

* **Affects:** #44 To Be on Top (JAM at `$EAA2`), #49 Robocop (BRK at
  `$EA7E`).
* **Cause:** without the Commodore ROMs, numsid only provides stubs for the
  two KERNAL IRQ exits `$EA31` and `$EA81`. These tunes jump to other
  points in the same handler: `$EA7E` acknowledges CIA1 and then exits, and
  the keyboard-scan path continues to `$EAA2`.
* **Implemented:** `kernal.py` builds a replacement 8 KB KERNAL. It is
  banked in at `$E000` whenever HIRAM is set, and writes go to the RAM
  underneath. It provides:
  * the hardware vectors and the `$FF48` IRQ/BRK entry,
  * the `$EA31` handler (jiffy clock, then a NOP sled to `$EA7E`/`$EA81`),
  * the NMI path `$FE43`/`$FE47`/`$FEBC`,
  * `RESTOR`, `GETIN` and `STOP`, and a jump table where every other
    routine is a harmless `RTS`.

  PSIDs now get the bank values the PSID spec asks for (`$37/$36/$34/$35`
  by code address), so tunes loaded under the ROM still see their RAM. Both
  affected tunes play: Robocop as described under F1, and To Be on Top
  with its samples.

### B3. Read-modify-write instructions skipped their dummy write (fixed)

* **Affects:** #60 BMX Kidz, #68 Nightdawn, and #23 The Way of the
  Exploding Fist (other subtunes). Reported by ear: "sounds awful".
* **Cause:** these tunes acknowledge their raster interrupt with
  `INC $D019` (BMX Kidz, Exploding Fist) or `LSR $D019` (Nightdawn). A real
  6502 read-modify-write first writes the unmodified value back and then
  the result, and it is that first write (`$F1`) that clears the raster
  flag. numsid wrote only the result (`$F2` or `$78`), so the flag stayed
  set and the IRQ re-fired right after `RTI`. The music routine then ran
  about 12 times per frame (593 IRQs/s) instead of once.
* **Fix:** RMW instructions on I/O addresses perform both writes, on their
  real cycles. BMX Kidz now gets 50 IRQs/s, and as a side effect renders
  at 4.5× real time instead of 1.9×.

### B4. I/O accesses happened on the wrong cycle of an instruction (fixed)

* **Affects:** #57 Savage. Reported by ear: it hung after about 52 s,
  sustaining one note forever.
* **Cause:**
  * Savage restarts a drum sample by writing the new CIA 2 rate low byte,
    then high byte, sometimes while the previous sample is still playing.
  * At 49.08 s the timer underflows 3 cycles after the low-byte write. It
    reloads with a mix of old and new latch (`$000D`), and the NMIs come
    every 14 cycles.
  * Whether such a storm nests, sinking the stack until it overflows and
    the CPU runs into a JAM opcode, or just plays out the old sample in a
    few milliseconds depends on the exact cycle of the handler's
    `LDA $DD0D`.
  * numsid performed every absolute read on an instruction's *first*
    cycle, 3 cycles early, and every write 1 cycle late.
* **Fix:**
  * The CPU now does memory I/O on the real cycle: reads and writes on an
    instruction's last cycle; read-modify-write reads, writes back and
    writes on its last three.
  * Savage now plays its full length with a steady stack.
  * For the PSIDs, SID writes simply land one cycle earlier. Their loudness
    envelopes are unchanged: the median difference is 0.00 dB for all 86.

### F3. 8580 "digi boost" (done)

* **Reported:** samples missing in Savage and Turbo Outrun on the 8580.
* **Cause:** `$D418` sample playback works because the volume register
  scales the 6581's large DC offset. The 8580 has almost none, so on a real
  C64C these samples are nearly inaudible. numsid reproduced that
  faithfully.
* **Change:** like sidplayfp's digi boost, the 8580 now feeds a constant
  level into the mixer, equal to that of three silent 6581 voices.
  Samples then play at the same loudness as on the 6581 (Turbo Outrun
  0.084 vs 0.094 RMS). `--no-digiboost` restores the authentic
  near-silence.

### M1. Survey of real modules against libopenmpt (done)

* **What was run:**
  * the list of well-known demo and game modules below (the Amiga
    compatibility table),
  * every PowerPacked module,
  * all 199 MED and OctaMED songs in a large collection.

  Each was rendered by numsid and by libopenmpt 0.8.9 (`openmpt123` with
  its Amiga resampler emulation, the A500 filter and hard panning), and
  compared on:
  * the loudness envelope in 25 ms frames, floored at −70 dB (timing
    drift, and correlation over the first 30 s, or 60 s for the list),
  * the overall level,
  * the level in five frequency bands.
* **ProTracker modules:** 27 of the 28 modules in the list play (Turrican II is TFMX, M8). Over their first minute the 25 ProTracker and StarTrekker ones keep the same timing as libopenmpt with no drift, their envelopes correlate at 0.91 to 1.00 (median 0.997), the overall level matches within 0.12 dB and every band within 0.7 dB, except in Agony (below). The 14 PowerPacked ProTracker modules match as closely.
* **MED songs:** 198 of the 199 load (one file is truncated), and all of them play without errors. As numsid renders them, 134 correlate with libopenmpt at 0.95 or better and 155 at 0.90 or better (median 0.985). Most of the rest drift a little against libopenmpt because of how it rounds tick lengths (below); with that arithmetic emulated, 184 reach 0.95, 189 reach 0.90 (median 0.998), and none drifts. The lowest (0.83-0.90) are mostly dense, sustained mixes whose envelopes hardly move, where the correlation measures little more than how channels beat against each other. Seven of the nine lowest were checked track by track: every track matches at 0.88 or better, except one track of one MMD3 song (M9).
* **Bugs found and fixed on the way:**
  * MED files without an instrument-extension block had their loops
    switched off.
  * An instrument number without a note (MED's "hold" symbol) didn't keep
    a held note going, which cut notes short in songs with hold/decay
    instruments. Xmas 2DXtreme went from 0.25 to 0.93 on its worst track.
  * Notes past B-7 weren't brought down 9 octaves, as OctaMED does.
    MegaBall's title tune went from 0.77 to 0.96.
  * Extra command pages were ignored, and with them a speed change in a
    Bohemian Rhapsody remix (0.78 to 1.00).
* **Where numsid and libopenmpt differ by design.** None of these is a
  numsid bug; the comparisons allow for the first five.
  * **Panning:** libopenmpt pans MOD channels 25/75 by default. The Amiga
    pans them hard left/right, as numsid does. Comparisons use
    `--stereo 200`.
  * **DC:** libopenmpt keeps DC. Off-centre samples and synth waveforms
    leave an offset in its output that an Amiga's coupling capacitor
    (numsid's 5 Hz high-pass) removes. Both sides are high-passed at 20 Hz
    before comparing.
  * **MED filter flag:** libopenmpt ignores it, so it plays without the
    LED filter what OctaMED plays with it (about −10 dB above 4 kHz,
    −23 dB above 8 kHz). Those songs are compared with numsid's filter
    off.
  * **Song end:** libopenmpt ignores MED's end-of-song command `FFE` and
    plays a song once. numsid stops at `FFE` and otherwise loops. Game
    soundtracks often put several tunes in one file, separated by `FFE`.
    Comparisons stop at the song's end.
  * **Tick timing:** libopenmpt rounds MED tempo commands (`Fxx`) to a
    whole BPM, and renders every tick as a whole number of output samples,
    computed from a tempo held to 1/10000 BPM. Examples:
    * Peace 4 a Nation's `F1A` (98.48 BPM) plays at 98, 0.5% slow.
    * At the fast tempos of Hired Guns (up to 792 BPM, 3.2 ms ticks) the
      truncation makes it up to 0.65% fast.

    numsid keeps the exact rates, which an Amiga's CIA timer would give to
    within a cycle. The second set of MED figures above emulates
    libopenmpt's arithmetic.
  * **MED level:** libopenmpt plays 4-channel MED songs about 6 dB quieter
    than MODs. numsid gives both the same level, Paula's.
  * **LED filter strength:** Agony's intro, the one module in the list
    that switches the LED filter on (`E00`), is 4.5 dB duller above 8 kHz
    in numsid than in libopenmpt. numsid's filter is the A500's Sallen-Key
    circuit (3.09 kHz, Q 0.66). Which of the two is closer to a real
    machine is not settled.

### M4. MOD output clipped hard (fixed)

* **Affects:** 8 of the 24 playable modules in the survey list: Desert
  Dream 1a, Stardust Memories, Livin' Insanity, Lotus 2, Turrican, Pinball
  Fantasies, Pinball Dreams and Blood Money. Peaks reached 1.19 (Blood
  Money's intro), and Lotus 2 had 6,666 samples past full scale in its
  first minute.
* **Cause:** two loud channels on one side already reach full scale, and
  the output high-pass shifts a loud waveform off centre, which takes its
  peaks further. That happens on the real machine too, in its analogue
  stages, but numsid then clipped the result to ±1.0.
* **Not the cause:** numsid's level. It first looked 1.9 dB louder than
  libopenmpt, but that came from libopenmpt's default 25/75 panning; with
  hard panning on both, the levels match within 0.4 dB.
* **Fix:** a soft limiter in place of the hard clip. Below 90% of full
  scale the signal is untouched. Above it, a tanh curve whose slope is
  continuous at the knee squeezes the rest into the last tenth. None of
  the 8 modules has a hard-clipped sample now (Lotus 2 peaks at 0.993,
  Blood Money at 0.9994), and the match with libopenmpt is unchanged.

### M5. PowerPacker files (done)

* **Affects:** 27 files in the collection, among them Shadow of
  the Beast. 14 are ProTracker modules and 13 (3 of them in the old MED 2/3 format, which isn't supported) are MED songs.
* **Fix:** `numsid/powerpacker.py` unpacks `PP20` files before the format
  is detected. The bit stream is read backwards, and the output is rebuilt
  from its last byte to its first, out of literal runs and copies of bytes
  already written. All 27 unpack, in under 0.2 s each; the 14
  ProTracker ones match libopenmpt (which reads PP20 itself) with zero
  timing drift and levels within 0.25 dB. Tests round-trip data through a
  small encoder written for them.
* **Not supported:** XPK-packed files (`XPKF`) and encrypted PowerPacker
  files.

### M6. MED and OctaMED songs (done)

* **Affects:** 199 songs in the collection (139 MMD1, 48 MMD0, 6 MMD2, 5 MMD3), among
  them Cannon Fodder and Xenon 2's Megablast. 162 have 4 tracks, 31 have 8 and 5 have 7, 9 or 10.
* **Fix:** a loader for MMD0 to MMD3 (`numsid/medfile.py`) and a MED
  replay routine (`numsid/med.py`) that drives Paula like the ProTracker
  one. It covers:
  * MED's tempo units (`SPD`, SoundTracker-compatible, BPM mode and the
    8-channel tempos),
  * blocks of any length,
  * note numbering and the octave wrap of 4-channel songs,
  * multi-octave IFF instruments,
  * track and master volumes,
  * hex and decimal volumes,
  * first-tick slides,
  * hold/decay envelopes and hold symbols,
  * the effect commands.

  The semantics follow libopenmpt's MED loader.
* **Result:** see M1 and the table below. Cannon Fodder matches
  libopenmpt at 1.000 and Megablast at 0.983 over a minute, with zero
  timing drift.

### M7. MED synthetic and hybrid instruments (done)

* **Affects:** 16 songs, among them Civilization and Bart
  Simpson, which were nearly silent without them.
* **Fix:** each note runs the instrument's two tables, stepped every
  `speed` ticks: the volume table (set and ramp the volume, follow an
  envelope read from a waveform, wait, jump, hold then decay) and the
  waveform table (pick a waveform, ramp the pitch, arpeggio, vibrato,
  wait, jump). A new waveform goes to Paula's location and length latches,
  so it takes over when the current one ends, as on the real machine.
* **Result:** Bart Simpson matches libopenmpt at 0.963; Civilization's
  four tracks at 0.90-1.00 each.

## Open

### O1. VIC bad lines (done for RSIDs) and sprite DMA (open)

* **Done:** in the RSID machine the CPU is now halted for 43 cycles on each
  bad line (lines `$30-$F7` matching the vertical scroll, while the display
  is on), about 5.5% of all cycles, as on a real C64. A test checks that
  share. PSIDs are called as before.
* **Open:** sprite DMA, which steals a few more cycles per line while
  sprites are shown.
* Arkanoid's notes end at about 113 s against HVSC's 137 s. It was
  confirmed by ear to play correctly, so the remaining time is presumably
  a sample-only ending, which the note-based length detection can't see.
  (Savage's 52.3 s turned out to be the hang fixed by B4.)

### O2. Sample-playing RSIDs render at only about 2× real time

* **Affects:** Giana Sisters (2.3×), #68 Nightdawn (2.4×), To Be on Top
  and Dulcedo Cogitationis (about 2.5×). BMX Kidz improved to 4.5× once
  B3 removed its IRQ storm.
* **Cause:** a CIA 2 NMI at 4-7 kHz means thousands of interrupt entries
  and handler runs in Python per second. Each `$D418` write also starts a
  new mixer segment.
* **Fix ideas:** build the per-cycle volume as one array instead of a
  segment per write, and make the scheduler loop leaner.

### M2. MODs: everything plays with ProTracker 2.3D semantics

* **Affects:** multichannel modules (`6CHN`, `8CHN`, `xxCH`) written in PC
  trackers, and modules made in other Amiga trackers. FastTracker 2 differs
  from ProTracker in several effects (`9xx` past the sample end, `E6x`,
  `Fxx` at 32, `8xx` panning, which PT ignores).
* **SoundTracker:** 15-sample modules have their old effects mapped onto
  PT's, as pt2-clone does. The SoundTracker version is guessed from the
  effects a module uses, which can be wrong.

### M3. MODs: Paula details not modelled

* PAL only: an NTSC Amiga's colour clock is 3,579,545 Hz, and vblank-timed
  players run at 60 Hz there.
* The short delay between a DMA start and the first byte is not modelled,
  nor is the volume PWM.
* Periods below 113 are clamped, as in pt2-clone. On hardware the DMA can't
  fetch fast enough and words get repeated.
* `E8x` (Karplus-Strong) is ignored, as pt2-clone does by default.

### M8. Other Amiga music formats

* **Affects:** in the collection used for the survey, about 174 TFMX files
  (among them Turrican II, the one module of the top list that doesn't
  play), XPK-packed files (`XPKF`), Oktalyzer, Future Composer and
  SoundMon songs, the old pre-MMD `MED` format (3 PowerPacked files),
  executables with built-in replayers, and the many custom formats
  DeliTracker plays.
* **What it takes:** each is its own replay routine (TFMX is a large one,
  with its own macro language); XPK is a family of packers. They would
  slot in like the MED replayer, driving the same Paula.

### M9. MED: what the replayer doesn't do

* MIDI instruments and packed (ADPCM) samples stay silent, with a warning.
* The play-sequence stop/jump commands of MMD2/MMD3 are ignored, with a
  warning.
* Extra command pages (more effect columns) are read. Their one-off
  commands all act, but only one effect per track runs on every tick: the
  main column's, or the first page's when the main column is empty.
* OctaMED's 5-8 channel mode mixes two tracks into each Paula channel in
  software; numsid gives every track its own channel instead (as
  libopenmpt does), so those songs sound cleaner than on the machine.
* Songs made for OctaMED's software mixing (`MMD2`/`MMD3` with the mix
  flag) can ask for pitches that Paula can't play; numsid clamps periods
  at 113.
* MMD2/MMD3 track panning, echo and stereo separation settings are
  ignored: tracks are panned the Amiga way.
* The conversions follow libopenmpt's reading of the format: MED tempo
  to timer rate, the hold-symbol behaviour, the octave rules for very
  high notes. They have not been checked against OctaMED's own player
  code or a real Amiga.
* `FFE` stops the song, as in OctaMED. Game soundtracks that hold several
  tunes separated by `FFE` (Roadkill has 14) play only the first; `←/→`
  and `--pos` reach the others.
* **Not understood yet:** in one MMD3 song (Oasis, "Wonder Remix Wall"),
  track 3 plays a long one-shot square-wave sample, a new note every two
  lines. libopenmpt fades each note within its line, by 3-8 dB from note
  to note, with or without its Amiga resampler. numsid plays each one at
  the sample's own level. Nothing in the instrument's data (type, loop,
  extension fields) explains the fade. The other 197 MED songs show no
  such difference.

## Compatibility table: HVSC Top 100

**Summary:** 100 of 100 work. Results include all fixes up to B4/F3. Tunes marked as heard were played during the original survey, before the later fixes; Arkanoid, Savage, BMX Kidz and Nightdawn were confirmed by ear afterwards, on both chips.

| # | Tune | Composer | Result | Notes |
|--:|------|----------|--------|-------|
| 1 | Wizball | Martin Galway | ✅ works | fixed by B1 (4 calls per frame); length matches HVSC |
| 2 | The Last Ninja | Ben Daglish & Anthony Lees | ✅ works | quiet intro by design; uses the pulse+triangle combined waveform a lot (approximate model); heard 30 s in the survey |
| 3 | Sanxion | Rob Hubbard | ✅ works | heard 30 s in the survey |
| 4 | Commando | Rob Hubbard | ✅ works | heard 30 s in the survey |
| 5 | International Karate | Rob Hubbard | ✅ works | heard 30 s in the survey |
| 6 | Cybernoid II | Jeroen Tel / Maniacs of Noise | ✅ works | heard 30 s in the survey |
| 7 | Delta | Rob Hubbard | ✅ works | heard 30 s in the survey |
| 8 | Monty on the Run | Rob Hubbard | ✅ works | heard 30 s in the survey |
| 9 | Cybernoid | Jeroen Tel / Maniacs of Noise | ✅ works | heard 30 s in the survey |
| 10 | Last Ninja 3 | Reyn Ouwehand | ✅ works | heard 30 s in the survey |
| 11 | Rambo: First Blood Part II | Martin Galway | ✅ works | fixed by B2 (was silent) |
| 12 | Last Ninja 2 | Matt Gray | ✅ works | heard 30 s in the survey |
| 13 | Mutants | Fred Gray | ✅ works | quieter than most tunes (mostly triangle); verify by ear; heard 30 s in the survey |
| 14 | Knucklebusters | Rob Hubbard | ✅ works |  |
| 15 | Arkanoid | Martin Galway | ✅ works | RSID, plays (F1); confirmed by ear (6581 and 8580) |
| 16 | Crazy Comets | Rob Hubbard | ✅ works |  |
| 17 | Auf Wiedersehen Monty | Rob Hubbard & Ben Daglish | ✅ works |  |
| 18 | The Great Giana Sisters | Chris Hülsbeck | ✅ works | RSID, plays with NMI samples (F1) |
| 19 | Ghouls 'n' Ghosts | Tim Follin | ✅ works | uses combined waveforms (approximate model) |
| 20 | Ghosts'n Goblins | Mark Cooksey | ✅ works |  |
| 21 | Nemesis the Warlock | Rob Hubbard | ✅ works |  |
| 22 | Green Beret | Martin Galway | ✅ works |  |
| 23 | The Way of the Exploding Fist | Neil Brennan | ✅ works | RSID (F1); start song is a 3 s jingle; IRQ acknowledge fixed by B3 |
| 24 | Platoon | Jonathan Dunn | ✅ works |  |
| 25 | R-type | Chris Hülsbeck & Ramiro Vaca | ✅ works |  |
| 26 | Lightforce | Rob Hubbard | ✅ works |  |
| 27 | Cobra | Ben Daglish | ✅ works | fades in from volume 0 by design |
| 28 | Robocop 3 | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 29 | Myth | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 30 | The Master of Magic | Rob Hubbard | ✅ works |  |
| 31 | Turbo Outrun | Jeroen Tel / Maniacs of Noise | ✅ works | RSID, plays with NMI samples (F1) |
| 32 | Trap | Ben Daglish | ✅ works |  |
| 33 | Sweet | Johannes Bjerregaard | ✅ works |  |
| 34 | Stormlord | Johannes Bjerregaard & J. Tel | ✅ works | RSID, plays with NMI samples (F1) |
| 35 | Spellbound | Rob Hubbard | ✅ works |  |
| 36 | Parallax | Martin Galway | ✅ works | fixed by B2 (was silent) |
| 37 | IK+ | Rob Hubbard | ✅ works | uses combined waveforms a lot (approximate model) |
| 38 | Zamzara | Charles Deenen/Maniacs of Noise | ✅ works |  |
| 39 | Comic Bakery | Martin Galway | ✅ works |  |
| 40 | Yie Ar Kung Fu | Martin Galway | ✅ works |  |
| 41 | Hysteria | Fred Gray | ✅ works | quieter than most tunes; verify by ear |
| 42 | Defender of the Crown | Richard Joseph | ✅ works | fixed by B1 (timer stop/start); length matches HVSC |
| 43 | Ocean Loader 3 | Peter Clarke | ✅ works | fixed by B1 (44.7 Hz); length matches HVSC |
| 44 | To Be on Top | Chris Hülsbeck | ✅ works | RSID, plays with NMI samples (F1, F2) |
| 45 | Armalyte | Martin Walker | ✅ works |  |
| 46 | Mega Apocalypse | Rob Hubbard & Simon Nicol | ✅ works |  |
| 47 | Dulcedo Cogitationis | Chris Hülsbeck | ✅ works | RSID, plays with NMI samples (F1) |
| 48 | Thing Bounces Back | Ben Daglish | ✅ works | uses combined waveforms (approximate model) |
| 49 | Robocop | Jonathan Dunn | ✅ works | RSID (F1, F2); length matches HVSC |
| 50 | Cooperation Demo | Kim Christensen (Future Freak) | ✅ works |  |
| 51 | Zoids | Rob Hubbard | ✅ works |  |
| 52 | Supremacy | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 53 | Compleeto! | Anders Andréen (ATOO) | ✅ works |  |
| 54 | Driller | Matt Gray | ✅ works |  |
| 55 | Times of Lore | Martin Galway | ✅ works | fixed by B1 (2 calls per frame) |
| 56 | One Man & His Droid | Rob Hubbard | ✅ works |  |
| 57 | Savage | Jeroen Tel / Maniacs of Noise | ✅ works | RSID, plays with NMI samples (F1); used to hang after ~50 s, fixed by B4; confirmed by ear (6581 and 8580) |
| 58 | ACE II | Rob Hubbard | ✅ works |  |
| 59 | Thrust | Rob Hubbard | ✅ works |  |
| 60 | BMX Kidz | Rob Hubbard & Jori Olkkonen | ✅ works | RSID, plays with NMI samples (F1); garbled music fixed by B3; confirmed by ear (6581 and 8580) |
| 61 | Kinetix | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 62 | Scroll Machine | Jori Olkkonen (Yip) | ✅ works |  |
| 63 | The Baby of Can Guru | Chris Hülsbeck | ✅ works | fixed by B1 (59.2 Hz) |
| 64 | Wizardry | Mike Alsop | ✅ works |  |
| 65 | Gauntlet III | Tim & Geoff Follin | ✅ works |  |
| 66 | Synth Sample | Georg Feil | ✅ works | PSID with its own IRQ handler; NTSC; `$D418` written 3×/frame |
| 67 | Grand Prix Circuit | Kris Hatlelid | ✅ works | RSID, plays (F1) |
| 68 | Nightdawn | Johannes Bjerregaard | ✅ works | RSID, plays with NMI samples (F1); garbled music fixed by B3; confirmed by ear (6581 and 8580) |
| 69 | Scumball | Tim Follin | ✅ works |  |
| 70 | R.I.S.K. | Wally Beben | ✅ works |  |
| 71 | Rocky Star | Kim Christensen (Future Freak) | ✅ works | RSID, plays (F1) |
| 72 | Snare | Martin Walker | ✅ works |  |
| 73 | Bionic Commando | Tim Follin | ✅ works |  |
| 74 | Katakis | Chris Hülsbeck | ✅ works |  |
| 75 | Gerry the Germ | Rob Hubbard | ✅ works |  |
| 76 | Eliminator | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 77 | Bulldog | Ben Daglish | ✅ works |  |
| 78 | Ramparts | Mark Cooksey | ✅ works |  |
| 79 | Ark Pandora | Ben Daglish | ✅ works |  |
| 80 | Miami Vice | Martin Galway | ✅ works |  |
| 81 | Warhawk | Rob Hubbard | ✅ works |  |
| 82 | The Vikings | Ben Daglish | ✅ works |  |
| 83 | FAME (1) | Michael Hendriks | ✅ works |  |
| 84 | SYS4096 | Juha-Matti Hilpinen (AMJ) | ✅ works | fixed by B1 (2 calls per frame); length matches HVSC |
| 85 | Batman (long) | Jens-Christian Huus | ✅ works |  |
| 86 | Mikie | Martin Galway | ✅ works |  |
| 87 | Zig Zag | Antony Crowther | ✅ works | 200 Hz CIA player; length matches HVSC |
| 88 | Battle Valley | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 89 | Chordian | Jens-Christian Huus | ✅ works |  |
| 90 | Combat Crazy | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 91 | Hawkeye | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 92 | Short Circuit | Martin Galway | ✅ works | CIA-timed; length matches HVSC |
| 93 | DNA Warrior | Thomas Petersen & K.Christensen | ✅ works |  |
| 94 | Glider Rider | David Whittaker | ✅ works |  |
| 95 | Uridium | Steve Turner | ✅ works |  |
| 96 | Thunderforce | Johannes Bjerregaard | ✅ works |  |
| 97 | Kentilla | Rob Hubbard | ✅ works |  |
| 98 | Crazy Comets (remix) | Johannes Bjerregaard | ✅ works |  |
| 99 | Terra Cresta | Martin Galway | ✅ works |  |
| 100 | Tetris | Wally Beben | ✅ works | RSID, plays with NMI samples (F1) |

## Compatibility table: Amiga modules

**Summary:** 27 of 28 work. The list is the one the survey started from: famous demo and game modules. Match is the correlation of the loudness envelopes with libopenmpt's rendering over the first minute (M1).

| # | Module | Format | Result | Match with libopenmpt | Notes |
|---|--------|--------|--------|-----------------------|-------|
| 1 | Space Debris | ProTracker `M.K.` | ✅ works | 0.997, no drift (60 s) |  |
| 2 | Desert Dream 1a | ProTracker `M.K.` | ✅ works | 0.997, no drift (60 s) | clipped before M4 (peak 1.01) |
| 3 | Desert Dream 1b | ProTracker `M.K.` | ✅ works | 0.979, no drift (60 s) |  |
| 4 | Desert Dream 1c | ProTracker `M.K.` | ✅ works | 0.999, no drift (60 s) |  |
| 5 | Desert Dream 1d | ProTracker `M.K.` | ✅ works | 0.998, no drift (47 s) |  |
| 6 | Desert Dream 1e | ProTracker `M.K.` | ✅ works | 1.000, no drift (16 s) |  |
| 7 | Cream of the Earth | ProTracker `M.K.` | ✅ works | 0.998, no drift (60 s) |  |
| 8 | Guitar Slinger | ProTracker `M.K.` | ✅ works | 0.995, no drift (60 s) |  |
| 9 | Physical Presence | ProTracker `M.K.` | ✅ works | 0.909, no drift (60 s) |  |
| 10 | 4Mat's Madness | ProTracker `M.K.` | ✅ works | 0.993, no drift (60 s) |  |
| 11 | Stardust Memories | ProTracker `M.K.` | ✅ works | 0.998, no drift (60 s) | clipped before M4 (peak 1.04) |
| 12 | Livin' Insanity | ProTracker `M.K.` | ✅ works | 0.997, no drift (60 s) | clipped before M4 (peak 1.03); ends after 189 s |
| 13 | Elysium | StarTrekker `FLT4` | ✅ works | 0.997, no drift (60 s) |  |
| 14 | Global Trash III | ProTracker `M.K.` | ✅ works | 0.974, no drift (60 s) |  |
| 15 | Lotus Turbo Challenge 2 (title) | ProTracker `M.K.` | ✅ works | 0.999, no drift (60 s) | clipped before M4 (peak 1.07) |
| 16 | Agony (intro) | ProTracker `M.K.` | ✅ works | 0.999, no drift (60 s) | uses the LED filter: duller above 8 kHz than libopenmpt (M1) |
| 17 | Turrican II (TFMX) | TFMX | ❌ not supported | | another replayer format (M8) |
| 18 | Turrican (MOD) | ProTracker `M.K.` | ✅ works | 0.998, no drift (60 s) | clipped before M4 (peak 1.01) |
| 19 | Gods (Into the Wonderful) | ProTracker `M.K.` | ✅ works | 0.997, no drift (60 s) | the same song as the other Gods entry, in a slightly different rip |
| 20 | Gods (in game) | ProTracker `M.K.` | ✅ works | 0.997, no drift (60 s) | the same song as the other Gods entry, in a slightly different rip |
| 21 | Pinball Fantasies 1 | ProTracker `M.K.` | ✅ works | 0.998, no drift (60 s) | clipped before M4 (peak 1.14) |
| 22 | Pinball Dreams 1 | ProTracker `M.K.` | ✅ works | 0.999, no drift (60 s) | clipped before M4 (peak 1.07) |
| 23 | Cannon Fodder | MED `MMD0` | ✅ works | 1.000, no drift (60 s) | plays since M6; ends after 150 s |
| 24 | Xenon 2 Megablast | OctaMED `MMD1` | ✅ works | 0.998, no drift (60 s) | plays since M6 |
| 25 | Shadow of the Beast | StarTrekker `FLT4` | ✅ works | 0.997, no drift (60 s) | PowerPacked, unpacked on load (M5); plays since M5 |
| 26 | Magic Pockets | ProTracker `M.K.` | ✅ works | 0.990, no drift (60 s) |  |
| 27 | Blood Money (intro) | ProTracker `M.K.` | ✅ works | 0.996, no drift (60 s) | clipped before M4 (peak 1.19) |
| 28 | Blood Money (in game) | ProTracker `M.K.` | ✅ works | 0.995, no drift (60 s) |  |
