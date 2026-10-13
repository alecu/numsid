# Known issues and pending features

Found by running the [HVSC Top 100](https://www.hvsc.c64.org/) (2005 vote
list, `Top100.lst`) through numsid in vote order. Each tune was checked
with an instrumented 6502 run (60 s of play calls, logging every I/O access
outside the SID) and an offline render (20 s). Every flagged tune was then
traced by hand to find the cause. Tunes #2-#13 were also played through the
speakers for 30 s each, before live playback was dropped from the survey.

Each entry says which tunes it affects, so the fixes can be prioritised by
how many popular tunes they unlock.

## Suggested order

| Fix | Effort | Unlocks | Status |
|-----|--------|---------|--------|
| B2: preset volume 15 for PSIDs | one line | 2 silent tunes, and the start click on 34 more | fixed |
| B1: follow CIA timer writes during play | small, in `C64.play_frame` | 6 tunes with wrong tempo, including #1 Wizball | fixed |
| F2 + F1: RSID environment | large: CIA/VIC interrupts, continuous CPU | 14 RSID tunes | open |

## Fixed bugs

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

## Pending features

### F1. RSID support: a real interrupt-driven C64

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
* **Needed:**
  * run the CPU continuously instead of call-and-return,
  * cycle-counted CIA1/CIA2 timers A/B with interrupt control registers
    (IRQ and NMI),
  * VIC raster counter and raster-compare IRQ, with `$D019` acknowledge,
  * IRQ/NMI entry through the real vectors, honouring the I flag,
  * a minimal KERNAL IRQ entry for tunes that use `$0314`.

### F2. More KERNAL ROM entry points

* **Affects:** #44 To Be on Top (JAM at `$EAA2`), #49 Robocop (BRK at
  `$EA7E`).
* **Cause:** without the Commodore ROMs, numsid only provides stubs for the
  two KERNAL IRQ exits `$EA31` and `$EA81`. These tunes jump to other
  points in the same handler: `$EA7E` acknowledges CIA1 and then exits, and
  the keyboard-scan path continues to `$EAA2`.
* **Fix:** provide stubs for the whole `$EA31-$EA89` IRQ path, the
  `$FF48` IRQ and `$FE43/$FE47` NMI entries, and the vectors they use. The
  Commodore ROMs can't be shipped; libsidplayfp has its own small
  replacement KERNAL for the same reason. Probably needed together with F1,
  since both affected tunes are RSID.

## Compatibility table

**Summary:** 86 work and 14 are broken. The broken ones are all RSID files (F1, F2). Results reflect the fixes for B1 and B2.

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
| 15 | Arkanoid | Martin Galway | ❌ broken | RSID (F1) |
| 16 | Crazy Comets | Rob Hubbard | ✅ works |  |
| 17 | Auf Wiedersehen Monty | Rob Hubbard & Ben Daglish | ✅ works |  |
| 18 | The Great Giana Sisters | Chris Hülsbeck | ❌ broken | RSID with CIA2 NMI samples (F1) |
| 19 | Ghouls 'n' Ghosts | Tim Follin | ✅ works | uses combined waveforms (approximate model) |
| 20 | Ghosts'n Goblins | Mark Cooksey | ✅ works |  |
| 21 | Nemesis the Warlock | Rob Hubbard | ✅ works |  |
| 22 | Green Beret | Martin Galway | ✅ works |  |
| 23 | The Way of the Exploding Fist | Neil Brennan | ❌ broken | RSID, init never returns (F1) |
| 24 | Platoon | Jonathan Dunn | ✅ works |  |
| 25 | R-type | Chris Hülsbeck & Ramiro Vaca | ✅ works |  |
| 26 | Lightforce | Rob Hubbard | ✅ works |  |
| 27 | Cobra | Ben Daglish | ✅ works | fades in from volume 0 by design |
| 28 | Robocop 3 | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 29 | Myth | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 30 | The Master of Magic | Rob Hubbard | ✅ works |  |
| 31 | Turbo Outrun | Jeroen Tel / Maniacs of Noise | ❌ broken | RSID, raster IRQ + NMI (F1) |
| 32 | Trap | Ben Daglish | ✅ works |  |
| 33 | Sweet | Johannes Bjerregaard | ✅ works |  |
| 34 | Stormlord | Johannes Bjerregaard & J. Tel | ❌ broken | RSID, raster IRQ + NMI (F1) |
| 35 | Spellbound | Rob Hubbard | ✅ works |  |
| 36 | Parallax | Martin Galway | ✅ works | fixed by B2 (was silent) |
| 37 | IK+ | Rob Hubbard | ✅ works | uses combined waveforms a lot (approximate model) |
| 38 | Zamzara | Charles Deenen/Maniacs of Noise | ✅ works |  |
| 39 | Comic Bakery | Martin Galway | ✅ works |  |
| 40 | Yie Ar Kung Fu | Martin Galway | ✅ works |  |
| 41 | Hysteria | Fred Gray | ✅ works | quieter than most tunes; verify by ear |
| 42 | Defender of the Crown | Richard Joseph | ✅ works | fixed by B1 (timer stop/start); length matches HVSC |
| 43 | Ocean Loader 3 | Peter Clarke | ✅ works | fixed by B1 (44.7 Hz); length matches HVSC |
| 44 | To Be on Top | Chris Hülsbeck | ❌ broken | RSID; crashes in missing KERNAL code (F1, F2) |
| 45 | Armalyte | Martin Walker | ✅ works |  |
| 46 | Mega Apocalypse | Rob Hubbard & Simon Nicol | ✅ works |  |
| 47 | Dulcedo Cogitationis | Chris Hülsbeck | ❌ broken | RSID, silent (F1) |
| 48 | Thing Bounces Back | Ben Daglish | ✅ works | uses combined waveforms (approximate model) |
| 49 | Robocop | Jonathan Dunn | ❌ broken | RSID; crashes in missing KERNAL code (F1, F2) |
| 50 | Cooperation Demo | Kim Christensen (Future Freak) | ✅ works |  |
| 51 | Zoids | Rob Hubbard | ✅ works |  |
| 52 | Supremacy | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 53 | Compleeto! | Anders Andréen (ATOO) | ✅ works |  |
| 54 | Driller | Matt Gray | ✅ works |  |
| 55 | Times of Lore | Martin Galway | ✅ works | fixed by B1 (2 calls per frame) |
| 56 | One Man & His Droid | Rob Hubbard | ✅ works |  |
| 57 | Savage | Jeroen Tel / Maniacs of Noise | ❌ broken | RSID, silent (F1) |
| 58 | ACE II | Rob Hubbard | ✅ works |  |
| 59 | Thrust | Rob Hubbard | ✅ works |  |
| 60 | BMX Kidz | Rob Hubbard & Jori Olkkonen | ❌ broken | RSID, nearly silent (F1) |
| 61 | Kinetix | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 62 | Scroll Machine | Jori Olkkonen (Yip) | ✅ works |  |
| 63 | The Baby of Can Guru | Chris Hülsbeck | ✅ works | fixed by B1 (59.2 Hz) |
| 64 | Wizardry | Mike Alsop | ✅ works |  |
| 65 | Gauntlet III | Tim & Geoff Follin | ✅ works |  |
| 66 | Synth Sample | Georg Feil | ✅ works | PSID with its own IRQ handler; NTSC; `$D418` written 3×/frame |
| 67 | Grand Prix Circuit | Kris Hatlelid | ❌ broken | RSID (F1) |
| 68 | Nightdawn | Johannes Bjerregaard | ❌ broken | RSID, silent (F1) |
| 69 | Scumball | Tim Follin | ✅ works |  |
| 70 | R.I.S.K. | Wally Beben | ✅ works |  |
| 71 | Rocky Star | Kim Christensen (Future Freak) | ❌ broken | RSID, init never returns (F1) |
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
| 100 | Tetris | Wally Beben | ❌ broken | RSID, wrong timing (F1) |
