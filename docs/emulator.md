# PAL C64 emulator

`python -m numsid c64` (also `python -m numsid.c64` or the installed `numc64`
command) boots a C64 with user-supplied BASIC, KERNAL and character ROMs.
It runs the original ROM reset sequence, memory test, BASIC interpreter,
keyboard scanner and interrupt handlers. The existing SID music player
keeps its separate PSID/RSID execution environment.

## Running Commando

```bash
.venv/bin/python -m numsid c64 ~/Downloads/Commando*.zip
```

ROM discovery checks `./roms` and `~/Downloads/vice-*.tar.*`. Use `--roms PATH`
to select a VICE tarball or a directory containing the three C64 ROMs.
The archive reader selects the standard C64 ROM revisions, checks their
sizes, and reads the three members without extracting files. No ROMs or
game data are downloaded or shipped with numsid.

PRGs load into physical RAM after the ROM has reached `READY.`. BASIC
program pointers are updated and `RUN` is entered through the KERNAL's
keyboard buffer. `--no-run` leaves the program loaded at BASIC; a program
without a BASIC stub needs `--entry 0x1234` to select its machine-code address.

The downloaded `Commando (1985)(Elite)[cr HF][t +7 HF].zip` has a compressed
intro, an info linker and a trainer before the game. Give the decompression
time to finish. Space leaves the intro; Esc is RUN/STOP and skips the linker.
Answer each Y/N trainer option, select 1 for Commando, then press Right Ctrl
at `PRESS FIRE TO START`. Arrow keys control joystick port 2, Right Ctrl is
fire, Space is grenade. F10 changes ports, F9 pauses, F11 toggles fullscreen,
Ctrl+F11 toggles CRT effects, and F12 sends RESTORE. Start with `--no-crt`
to disable the effects. Keyboard input is a CIA switch matrix, including
Shift and the Commodore key (Alt). Keys are released when the window loses
focus. F2/F4/F6/F8 generate Shift plus F1/F3/F5/F7.

## Memory to shader

`computer.py` connects the 6510, two CIAs, VIC-II, SID, processor-port banking,
keyboard and joysticks. BASIC and KERNAL overlay the CPU's memory view while
writes still reach the RAM underneath. The VIC sees physical RAM and its
own character-ROM wiring, independently of CPU banking. Colour RAM is a
separate 1 KiB array of four-bit values.

Each raster line captures the VIC's raw 16 KiB RAM bank, colour RAM, VIC
registers and CIA2 bank selection into numpy arrays. These are byte snapshots,
not pixels or decoded characters. Keeping each line's state preserves
raster splits, palette changes, screen switches, and sprite multiplexing
that would be lost in a single end-of-frame memory upload.

`sprites.py` tracks the VIC's sprite DMA starts, active row counters and Y
expansion. A Y register change behind the raster does not start a sprite;
an already-active sprite continues its row sequence after Y is rewritten.
The same row state drives CPU stalls, collision comparisons and the shader.
This keeps Commando's chained raster interrupts on time during scrolling.

`renderer.py` uploads the arrays as unsigned integer textures (`GL_R8UI`).
`shaders/vic.frag` generates the whole picture: standard, multicolor and
extended-background text; hires and multicolor bitmaps; eight sprites with
multicolor, expansion and priority; scrolling, borders and the palette.
The VIC shader runs at 384×272, including borders. A second shader scales
that framebuffer to the largest rectangle that fits the window at the same
aspect ratio, using fractional scaling when needed and letterboxing only
on the unused axis. Resizing does not multiply the RAM-decoding work.
No Python framebuffer is generated.

`shaders/crt.frag` adds raster-aligned scanlines, horizontal beam softness,
a light glow, an RGB aperture grille and mild edge falloff. The scanlines
and grille fade out at sizes too small to resolve them, avoiding moire.
`--no-crt` or Ctrl+F11 selects unfiltered nearest-neighbour pixels instead;
toggling effects does not require decoding the VIC frame again.

Rendering requires desktop OpenGL 3.3 with a maximum texture size of at
least 4992. Headless screenshots use EGL. Software OpenGL can reduce the
display rate; the window skips intermediate video captures while catching
up, continuing to emulate the CPU and synthesize audio for those frames.

Collision comparators use integer masks on the CPU, because their latches
and interrupts are emulated hardware inputs to the game. They do not supply
the displayed pixels. RAM transfers are explicit OpenGL uploads, rather
than a claim of zero-copy shared GPU memory. A complete video packet is
about 5.5 MB; only the current packet is kept by the live window.

The CPU and CIAs share a PAL 985,248 Hz timeline, with 63 cycles × 312 lines
per frame (about 50.125 Hz). Register writes retain their instruction-cycle
timestamps. Bad lines and active sprite fetches take CPU cycles; PAL sprite
fetches are scheduled at the beginning and end of the line, rather than
charging their whole budget before the raster interrupt.
The existing numpy SID synthesizer and band-limited resampler produce live
44.1 kHz audio. The main thread advances the machine; the sound device pulls
from a bounded PCM queue and never executes the CPU concurrently.

## Headless runs and verification

```bash
# Boot BASIC and render its actual GLSL output using headless OpenGL/EGL.
.venv/bin/python -m numsid.c64 --headless --frames 1 --screenshot /tmp/basic.png

# Script physical key / joystick transitions relative to program loading.
.venv/bin/python -m numsid.c64 game.zip --headless --frames 1000 \
  --input 900:SPACE:down --input 905:SPACE:up --screenshot /tmp/game.png

# Audio rendering uses the same machine and SID as live playback.
.venv/bin/python -m numsid.c64 game.prg --headless --frames 1000 --wav /tmp/game.wav

.venv/bin/python -m pytest -q
NUMC64_COMMANDO="$HOME/Downloads/Commando (1985)(Elite)[cr HF][t +7 HF].zip" \
  .venv/bin/python -m pytest tests/test_commando.py -q
```

The integration test boots the real ROM, runs the downloaded release through
its intro, linker, trainer and selector, starts gameplay using joystick fire,
checks that 30 frames of right input move the player about 60 pixels, and checks
finite, non-silent SID samples with the expected sample count. It does not
patch game code or bypass the trainer. `NUMC64_ROMS` can override ROM discovery.
It also scrolls forward through all eight fine-scroll positions and checks
that the score strip's raster split remains intact on every captured frame.

The GLSL tests render all five video modes and read actual OpenGL pixels to
check character-ROM banking, raster changes, scrolling, borders, sprite
priority, multicolor, expansion and latched sprite rows, plus fractional
window fitting and CRT scanline contrast at 2× and 3× scales. They skip if headless OpenGL is unavailable;
shader compilation errors fail the tests. Unit tests cover banking, writes
under ROM, hardware BRK, CIA input wiring, collision latches and archive loading.

## Current limits

This is a playable PAL PRG emulator, not yet a replacement for VICE's complete
cycle-accurate hardware coverage. In particular:

* No 1541/IEC disk drive, tape, cartridge hardware, expansion devices or NTSC.
  The downloaded Commando is a standalone PRG and does not need those devices.
* CIA TOD clock, serial transfers and external CNT signals are not implemented.
* Video is sampled per scanline. Cycle-level border opening, FLI/VSP tricks,
  exact CPU read/write arbitration during sprite DMA and sprite expansion
  flip-flop tricks remain.
  The current model reads screen rows from the scanline snapshots rather than
  reproducing the VIC's internal bad-line matrix latches and fetch pipeline.
* CPU interrupt sampling lacks the NMOS one-instruction CLI/SEI/PLP delay,
  and opcode fetches from the I/O window do not reproduce bus side effects.
* Keyboard punctuation follows C64 physical keys rather than a full host
  layout translation; arrows are reserved for joystick control.

Hardware references: Christian Bauer's original
[VIC-II description](https://github.com/martinpiper/C64Docs/blob/master/VIC/vic-ii.txt),
the user-supplied VICE source distribution, and
[pyglet's shader API](https://docs.pyglet.org/en/latest/programming_guide/rendering.html).
