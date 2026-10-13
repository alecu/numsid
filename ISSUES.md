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

| Fix | Effort | Unlocks |
|-----|--------|---------|
| B2: preset volume 15 for PSIDs | one line | 2 silent tunes, and the start click on 34 more |
| B1: follow CIA timer writes during play | small, in `C64.play_frame` | 6 tunes with wrong tempo, including #1 Wizball |
| F2 + F1: RSID environment | large: CIA/VIC interrupts, continuous CPU | 14 RSID tunes |

## Bugs

### B1. CIA timer changes made by the play routine are ignored

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
* **Fix:**
  * Re-read the latch after every play call. Like a real CIA in continuous
    mode, a value written during an interrupt takes effect after the period
    already running, unless force-load (`$DC0E` bit 4) is set.
  * Honour timer stop/start: while stopped, the countdown pauses.

### B2. SID volume starts at 0; PSID rips expect 15

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
* **Fix:** for PSID files, write `$0F` to `$D418` before calling init and
  settle at that level. Checked by rendering: this fixes Rambo, Parallax
  and the start clicks of Mutants and Hysteria. RSID files keep the
  real-hardware state.

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

**Summary:** 47 work, 32 work with a defect (mostly the B2 start click, which goes away with B2's fix), 21 are broken: 14 RSID files (F1, F2), 5 with wrong tempo (B1) and 2 silent (B2).

| # | Tune | Composer | Result | Notes |
|--:|------|----------|--------|-------|
| 1 | Wizball | Martin Galway | ❌ broken | wrong tempo (B1) |
| 2 | The Last Ninja | Ben Daglish & Anthony Lees | ⚠️ works, with defects | quiet intro by design; uses the pulse+triangle combined waveform a lot (approximate model); click at start (B2); heard 30 s |
| 3 | Sanxion | Rob Hubbard | ⚠️ works, with defects | click at start (B2); heard 30 s |
| 4 | Commando | Rob Hubbard | ✅ works | heard 30 s |
| 5 | International Karate | Rob Hubbard | ✅ works | heard 30 s |
| 6 | Cybernoid II | Jeroen Tel / Maniacs of Noise | ✅ works | heard 30 s |
| 7 | Delta | Rob Hubbard | ⚠️ works, with defects | click at start (B2); heard 30 s |
| 8 | Monty on the Run | Rob Hubbard | ⚠️ works, with defects | click at start (B2); heard 30 s |
| 9 | Cybernoid | Jeroen Tel / Maniacs of Noise | ✅ works | heard 30 s |
| 10 | Last Ninja 3 | Reyn Ouwehand | ⚠️ works, with defects | click at start (B2); heard 30 s |
| 11 | Rambo: First Blood Part II | Martin Galway | ❌ broken | silent (B2) |
| 12 | Last Ninja 2 | Matt Gray | ⚠️ works, with defects | click at start (B2); heard 30 s |
| 13 | Mutants | Fred Gray | ⚠️ works, with defects | quieter than most tunes (mostly triangle); verify by ear; click at start (B2); heard 30 s |
| 14 | Knucklebusters | Rob Hubbard | ⚠️ works, with defects | click at start (B2) |
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
| 26 | Lightforce | Rob Hubbard | ⚠️ works, with defects | click at start (B2) |
| 27 | Cobra | Ben Daglish | ✅ works | fades in from volume 0 by design |
| 28 | Robocop 3 | Jeroen Tel / Maniacs of Noise | ⚠️ works, with defects | click at start (B2) |
| 29 | Myth | Jeroen Tel / Maniacs of Noise | ⚠️ works, with defects | click at start (B2) |
| 30 | The Master of Magic | Rob Hubbard | ⚠️ works, with defects | click at start (B2) |
| 31 | Turbo Outrun | Jeroen Tel / Maniacs of Noise | ❌ broken | RSID, raster IRQ + NMI (F1) |
| 32 | Trap | Ben Daglish | ⚠️ works, with defects | click at start (B2) |
| 33 | Sweet | Johannes Bjerregaard | ⚠️ works, with defects | click at start (B2) |
| 34 | Stormlord | Johannes Bjerregaard & J. Tel | ❌ broken | RSID, raster IRQ + NMI (F1) |
| 35 | Spellbound | Rob Hubbard | ✅ works |  |
| 36 | Parallax | Martin Galway | ❌ broken | silent (B2) |
| 37 | IK+ | Rob Hubbard | ✅ works | uses combined waveforms a lot (approximate model) |
| 38 | Zamzara | Charles Deenen/Maniacs of Noise | ✅ works |  |
| 39 | Comic Bakery | Martin Galway | ✅ works |  |
| 40 | Yie Ar Kung Fu | Martin Galway | ✅ works |  |
| 41 | Hysteria | Fred Gray | ⚠️ works, with defects | quieter than most tunes; verify by ear; click at start (B2) |
| 42 | Defender of the Crown | Richard Joseph | ❌ broken | tempo too fast: timer stop/start ignored (B1) |
| 43 | Ocean Loader 3 | Peter Clarke | ❌ broken | tempo ~34% too fast (B1) |
| 44 | To Be on Top | Chris Hülsbeck | ❌ broken | RSID; crashes in missing KERNAL code (F1, F2) |
| 45 | Armalyte | Martin Walker | ✅ works |  |
| 46 | Mega Apocalypse | Rob Hubbard & Simon Nicol | ⚠️ works, with defects | click at start (B2) |
| 47 | Dulcedo Cogitationis | Chris Hülsbeck | ❌ broken | RSID, silent (F1) |
| 48 | Thing Bounces Back | Ben Daglish | ⚠️ works, with defects | uses combined waveforms (approximate model); click at start (B2) |
| 49 | Robocop | Jonathan Dunn | ❌ broken | RSID; crashes in missing KERNAL code (F1, F2) |
| 50 | Cooperation Demo | Kim Christensen (Future Freak) | ✅ works |  |
| 51 | Zoids | Rob Hubbard | ✅ works |  |
| 52 | Supremacy | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 53 | Compleeto! | Anders Andréen (ATOO) | ⚠️ works, with defects | click at start (B2) |
| 54 | Driller | Matt Gray | ⚠️ works, with defects | click at start (B2) |
| 55 | Times of Lore | Martin Galway | ❌ broken | plays at 0.6× tempo (B1) |
| 56 | One Man & His Droid | Rob Hubbard | ✅ works |  |
| 57 | Savage | Jeroen Tel / Maniacs of Noise | ❌ broken | RSID, silent (F1) |
| 58 | ACE II | Rob Hubbard | ⚠️ works, with defects | click at start (B2) |
| 59 | Thrust | Rob Hubbard | ⚠️ works, with defects | click at start (B2) |
| 60 | BMX Kidz | Rob Hubbard & Jori Olkkonen | ❌ broken | RSID, nearly silent (F1) |
| 61 | Kinetix | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 62 | Scroll Machine | Jori Olkkonen (Yip) | ✅ works |  |
| 63 | The Baby of Can Guru | Chris Hülsbeck | ⚠️ works, with defects | ~1.3% too fast (B1); click at start (B2) |
| 64 | Wizardry | Mike Alsop | ✅ works |  |
| 65 | Gauntlet III | Tim & Geoff Follin | ✅ works |  |
| 66 | Synth Sample | Georg Feil | ⚠️ works, with defects | PSID with its own IRQ handler; NTSC; `$D418` written 3×/frame; click at start (B2) |
| 67 | Grand Prix Circuit | Kris Hatlelid | ❌ broken | RSID, wrong timing (F1) |
| 68 | Nightdawn | Johannes Bjerregaard | ❌ broken | RSID, silent (F1) |
| 69 | Scumball | Tim Follin | ✅ works |  |
| 70 | R.I.S.K. | Wally Beben | ⚠️ works, with defects | click at start (B2) |
| 71 | Rocky Star | Kim Christensen (Future Freak) | ❌ broken | RSID, init never returns (F1) |
| 72 | Snare | Martin Walker | ✅ works |  |
| 73 | Bionic Commando | Tim Follin | ✅ works |  |
| 74 | Katakis | Chris Hülsbeck | ✅ works |  |
| 75 | Gerry the Germ | Rob Hubbard | ✅ works |  |
| 76 | Eliminator | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 77 | Bulldog | Ben Daglish | ⚠️ works, with defects | click at start (B2) |
| 78 | Ramparts | Mark Cooksey | ✅ works |  |
| 79 | Ark Pandora | Ben Daglish | ⚠️ works, with defects | click at start (B2) |
| 80 | Miami Vice | Martin Galway | ✅ works |  |
| 81 | Warhawk | Rob Hubbard | ⚠️ works, with defects | click at start (B2) |
| 82 | The Vikings | Ben Daglish | ⚠️ works, with defects | click at start (B2) |
| 83 | FAME (1) | Michael Hendriks | ✅ works |  |
| 84 | SYS4096 | Juha-Matti Hilpinen (AMJ) | ❌ broken | plays at 0.6× tempo (B1) |
| 85 | Batman (long) | Jens-Christian Huus | ✅ works |  |
| 86 | Mikie | Martin Galway | ✅ works |  |
| 87 | Zig Zag | Antony Crowther | ✅ works | 200 Hz CIA player; one long call per pattern is harmless |
| 88 | Battle Valley | Jeroen Tel / Maniacs of Noise | ⚠️ works, with defects | click at start (B2) |
| 89 | Chordian | Jens-Christian Huus | ✅ works |  |
| 90 | Combat Crazy | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 91 | Hawkeye | Jeroen Tel / Maniacs of Noise | ✅ works |  |
| 92 | Short Circuit | Martin Galway | ✅ works |  |
| 93 | DNA Warrior | Thomas Petersen & K.Christensen | ⚠️ works, with defects | click at start (B2) |
| 94 | Glider Rider | David Whittaker | ⚠️ works, with defects | click at start (B2) |
| 95 | Uridium | Steve Turner | ✅ works |  |
| 96 | Thunderforce | Johannes Bjerregaard | ⚠️ works, with defects | click at start (B2) |
| 97 | Kentilla | Rob Hubbard | ✅ works |  |
| 98 | Crazy Comets (remix) | Johannes Bjerregaard | ✅ works |  |
| 99 | Terra Cresta | Martin Galway | ✅ works |  |
| 100 | Tetris | Wally Beben | ❌ broken | RSID, wrong timing (F1) |
