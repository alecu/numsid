"""Run a ROM-booted C64: python -m numsid.c64 [game.prg|game.zip]."""

import argparse
from pathlib import Path
import sys
import tarfile
import time
from zipfile import BadZipFile

from .computer import Computer
from .cpu6502 import CPUHalt
from .program import Program
from .roms import ROMs, find_roms


def save_screenshot(machine, frame, path, *, crt=True):
    import pyglet
    pyglet.options["headless"] = True
    from pyglet import gl
    from .computer import WIDTH, HEIGHT
    from .renderer import VICRenderer
    window = pyglet.window.Window(WIDTH, HEIGHT, visible=False,
                                  config=gl.Config(major_version=3, minor_version=3,
                                                   red_size=8, green_size=8, blue_size=8))
    renderer = VICRenderer(machine.roms.chargen, crt=crt)
    try:
        renderer.upload(frame)
        renderer.draw(WIDTH, HEIGHT)
        pyglet.image.get_buffer_manager().get_color_buffer().save(str(path))
    finally:
        renderer.delete()
        window.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="numc64", description="A PAL C64 in Python + numpy; "
                                     "VIC-II video decoded from RAM by an OpenGL shader.")
    parser.add_argument("program", nargs="?", help="PRG or ZIP containing one PRG (omit for BASIC)")
    parser.add_argument("--roms", type=Path, help="VICE tarball or C64 ROM directory; "
                        "default: ./roms or ~/Downloads/vice-*.tar.*")
    parser.add_argument("--entry", type=lambda v: int(v.removeprefix("$"), 16) if v.startswith("$")
                        else int(v, 0), help="machine-code start address (e.g. 0x1000)")
    parser.add_argument("--no-run", action="store_true", help="load into BASIC without RUN")
    parser.add_argument("--no-audio", action="store_true", help="disable SID synthesis and output")
    parser.add_argument("--no-crt", action="store_true", help="disable CRT scanlines, glow and grille")
    parser.add_argument("--model", choices=("6581", "8580"), default="6581")
    parser.add_argument("--joystick", type=int, choices=(1, 2), default=2)
    parser.add_argument("--scale", type=int, choices=range(1, 7), default=3)
    parser.add_argument("--headless", action="store_true", help="run a fixed number of frames")
    parser.add_argument("--frames", type=int, help="frames after loading (headless default: 300)")
    parser.add_argument("--screenshot", type=Path, help="save final shader-rendered PNG (headless)")
    parser.add_argument("--wav", type=Path, help="save SID audio (headless)")
    parser.add_argument("--input", action="append", default=[], metavar="FRAME:KEY:down|up",
                        help="script input after loading; KEY is SPACE, RETURN, F1, etc. "
                             "or joy.up/joy.fire (repeatable, headless)")
    args = parser.parse_args(argv)
    if args.frames is not None and args.frames < 1:
        parser.error("--frames must be positive")
    if (args.screenshot or args.wav or args.input) and not args.headless:
        parser.error("--screenshot, --wav and --input require --headless")
    if args.wav and args.no_audio:
        parser.error("--wav requires audio; remove --no-audio")
    try:
        path = args.roms or find_roms()
        if path is None:
            raise ValueError("provide --roms with a VICE tarball or directory of C64 ROMs")
        roms = ROMs.load(path)
        sid = None
        if not args.no_audio and (not args.headless or args.wav):
            from .sid import SID
            from .computer import PAL_CLOCK
            sid = SID(PAL_CLOCK, 44100, args.model)
        machine = Computer(roms, sid)
        machine.boot()
        if args.program:
            program = Program.load(args.program)
            machine.load_program(program, run=not args.no_run, entry=args.entry)
            print(f"Loaded {program.name} at ${program.address:04X} ({len(program.data)} bytes)")
        if args.headless:
            import numpy as np
            from ..player import write_wav
            events = {}
            from .peripherals import KEY_POSITION, JOY_BITS
            for event in args.input:
                try:
                    frame, key, state = event.split(":")
                    if state not in ("down", "up") or int(frame) < 0:
                        raise ValueError()
                    if key.startswith("joy."):
                        if key[4:] not in JOY_BITS:
                            raise ValueError()
                    elif key.upper() not in KEY_POSITION:
                        raise ValueError()
                    events.setdefault(int(frame), []).append((key, state == "down"))
                except ValueError:
                    raise ValueError("--input must be FRAME:KEY:down|up") from None
            count = args.frames or 300
            chunks = []
            t0 = time.perf_counter()
            for number in range(count):
                for key, down in events.get(number, []):
                    if key.startswith("joy."):
                        machine.inputs.joystick(key[4:], down, args.joystick)
                    else:
                        machine.inputs.key(key, down)
                frame = machine.step_frame(capture=bool(args.screenshot and number == count - 1))
                if args.wav:
                    chunks.append(machine.audio)
            if args.screenshot:
                save_screenshot(machine, frame, args.screenshot, crt=not args.no_crt)
                print(f"Saved {args.screenshot}")
            if args.wav:
                write_wav(args.wav, np.concatenate(chunks), 44100)
                print(f"Saved {args.wav}")
            elapsed = time.perf_counter() - t0
            print(f"Ran {count} PAL frames in {elapsed:.2f}s; PC=${machine.cpu.pc:04X}")
        else:
            from .window import run
            run(machine, scale=args.scale, joystick=args.joystick,
                audio=not args.no_audio, frames=args.frames, crt=not args.no_crt)
    except (OSError, ValueError, RuntimeError, CPUHalt, tarfile.TarError, BadZipFile) as exc:
        print(f"numc64: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
