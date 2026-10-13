# pysid

A Commodore 64 SID music player written in Python. The MOS 6502 CPU runs the
tune's own player code, and the MOS 6581/8580 SID chip is synthesised
cycle by cycle with numpy. No SID or C64 emulation libraries are used, only
`numpy` for the maths and `pyglet` for audio output and the window.

```
python -m pysid sids/Commando.sid
```

Playback is real time. Rendering Commando costs about 0.13 s of CPU per second
of audio offline, and about 25–40% of one core during live playback with the
visualiser open.

## Setup

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[test]'
```

(or `python -m venv .venv && .venv/bin/pip install -e '.[test]'`)

## Usage

```bash
.venv/bin/python -m pysid sids/Commando.sid                 # window + live audio
.venv/bin/python -m pysid sids/Commando.sid --song 2        # pick a subtune
.venv/bin/python -m pysid sids/Commando.sid --headless      # audio only, Ctrl-C stops
.venv/bin/python -m pysid sids/Commando.sid --wav commando.wav --seconds 240
.venv/bin/python -m pysid sids/Commando.sid --model 8580 --no-filter --gain 1.5
.venv/bin/python -m pysid sids/Commando.sid --info          # header only
```

Window keys: `←/→` subtune, `1 2 3` mute a voice, `SPACE` pause, `F` filter
on/off, `M` switch between 6581 and 8580, `ESC` quit. Scopes and register
readouts follow what you are *hearing*: the emulator runs ahead of the sound
card, and the display looks up the state for the current playback position.

## How it works

```
 .sid file ──► psid.py  ──► c64.py: 64K RAM, I/O map, init/play calls
                               │
                               ▼  cpu6502.py: every write to $D400-$D7FF is
                               │  tagged with the CPU cycle it happened on
                               ▼
              sid.py: render all cycles up to the frame end (≈1 MHz, numpy)
                 per voice: accumulator → waveform × envelope
                 mixer: direct path + filter input, × volume (per cycle)
                               │
                               ▼
              resample.py: 985 kHz → 123 kHz → 44.1 kHz, band-limited
                               │
                               ▼
              SID filter (state-variable, 12 dB/oct) → 16 Hz high-pass
                               │
                               ▼
              audio.py (pyglet stream) / player.write_wav / gui.py
```

* **CPU (`cpu6502.py`)**: every documented 6502 opcode plus the common
  "illegal" ones, NMOS decimal mode, cycle counts including page-crossing and
  branch penalties. Each opcode is a generated Python closure with its
  addressing mode inlined. Commando's player costs under 10 ms of Python per
  second of music.
* **Timing**: register writes carry their cycle timestamp, and the SID splits
  each voice's timeline exactly there. Gate toggles within a frame, the ADSR
  delay bug, and sync and ring-mod phase relationships therefore come out as
  on hardware.
* **Oscillators**: 24-bit phase accumulators computed for every cycle with
  numpy, with hard sync (including the "source synced on the same cycle"
  rule), ring modulation and the test bit. Noise is the real 23-bit LFSR,
  clocked by accumulator bit 19. Its whole 8,388,607-step period is
  precomputed in ~0.1 s using the identity b[n] = b[n−23·2ᵏ] ⊕ b[n−18·2ᵏ].
* **Envelope**: the full reSID-style state machine: 15-bit rate counter with
  wrap-around, exponential decay divider with its thresholds, sustain
  equality comparator, freeze at zero, and the 0xFF→0x00 flip. Instead of
  ticking every cycle, it jumps from one counter step to the next and
  expands the steps to per-cycle values.
* **Combined waveforms**: a pull-down model (wired-AND plus neighbour-bit
  leakage) with the parameters fitted by the libsidplayfp project.
* **Mixer and filter**: per-voice routing, voice-3-off, and volume applied per
  cycle, so `$D418` sample playback works. The 6581 DC offsets are modelled.
  Cutoff curves come from measurements of real 6581 and 8580 chips.
* **Output**: a two-stage polyphase resampler (Kaiser windowed sinc,
  ≥80 dB alias rejection, flat to 19 kHz) with the C64's ~16 kHz RC low-pass
  folded into its kernel, then the 16 Hz coupling high-pass.

Chip behaviour follows reSID 0.16 (Dag Lem), the reference for SID emulation,
as described in its documentation and comments. All code here is new.

## Tests

```bash
.venv/bin/python -m pytest -q
```

`tests/reference_sid.py` is a deliberately naive cycle-by-cycle model of the
oscillators, sync, ring modulation and envelopes. The vectorised engine is
checked against it cycle for cycle on random register sequences, and must
match exactly. Other tests cover the CPU (arithmetic, decimal mode, flags,
page-wrap bug, illegal opcodes, cycle counts), pitch accuracy, LFSR output,
filter modes, determinism, chunk-size independence, and all 19 Commando
subtunes.

## Limitations

* **PSID only.** RSID tunes that rely on real CIA/VIC interrupts, NMIs or
  KERNAL/BASIC ROM code won't play correctly. IRQ-style PSIDs (play address
  0) are supported through stub KERNAL IRQ exits.
* One SID chip (no 2SID/3SID tunes).
* The filter is linear: no 6581 distortion or chip-to-chip variation, and
  reSID 0.16's mild resonance range.
* The noise LFSR "write-back" when noise is combined with other waveforms is
  not modelled; that combination produces an AND, near silence.
