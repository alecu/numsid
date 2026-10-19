# Testing

```bash
.venv/bin/python -m pytest -q
```

The ROM-booted game emulator adds actual OpenGL shader tests and an opt-in
Commando gameplay test using local ROM and game files. See
[the emulator guide](emulator.md#headless-runs-and-verification).

`tests/reference_sid.py` is a deliberately naive cycle-by-cycle model of the
oscillators, sync, ring modulation and envelopes. The vectorised engine is
checked against it cycle for cycle on random register sequences, and must
match exactly. Other tests cover the CPU (arithmetic, decimal mode, flags,
page-wrap bug, illegal opcodes, cycle counts), pitch accuracy, LFSR output,
filter modes, determinism, chunk-size independence, and all 19 Commando
subtunes.

For MODs, `tests/reference_paula.py` steps a Paula channel one colour clock
at a time, word fetches and length counter included. The renderer must match
it exactly on random register sequences. The replayer tests trace periods,
volumes and triggers tick by tick for each effect and quirk, and the flow
commands row by row. Other tests cover the file formats, pitch, panning, the
output filters and song length. `tests/test_med.py` builds small MMD0 and
MMD1 files and traces the MED replayer tick by tick: tempo units, note
numbering and octave wrap, track and master volumes, first-tick slides,
vibrato depth, flow commands, hold/decay and hold symbols, extra command
pages, and each synth table command. PowerPacker decrunching is checked by
round trips through a small encoder written for the test, and the soft
limiter by its shape.

Real modules were checked against libopenmpt 0.8.9 (`openmpt123` with its
Amiga resampler emulation, the A500 filter and hard panning), comparing
loudness envelopes in 25 ms frames and the level in five frequency bands
([ISSUES.md](../ISSUES.md), M1, has the details and a table):

* **ProTracker:** 27 of the 28 famous demo and game modules in the list
  play (Turrican II is TFMX). Over the first minute of each, the 25
  ProTracker ones keep the same timing as libopenmpt, with no drift, and
  their envelopes correlate at 0.91 to 1.00 (median 0.997). Every band
  matches within 0.7 dB, except in Agony, which switches on the LED filter.
* **MED:** all 198 MED and OctaMED songs that load from the collection
  play. 155 correlate at 0.90 or better as they are. With libopenmpt's own
  rounding of MED tick lengths emulated, 189 do (median 0.998), and none
  drifts.
* **`axelf.mod`:** with `--model A1200`, the spectrum matches within 0.7 dB
  up to 8 kHz. Above that, OpenMPT's interpolation smooths away the sample
  steps that numsid keeps.
