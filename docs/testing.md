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
output filters and song length.

Checked against libopenmpt (through ffmpeg), the first 60 s of `axelf.mod`
have the same timing: the loudness envelopes of both sides correlate at 0.95
with zero lag. With `--model A1200` the spectrum matches within 0.7 dB up to
8 kHz. Above that, OpenMPT's interpolation smooths away the sample steps that
numsid keeps.
