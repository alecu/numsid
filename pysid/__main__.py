"""pysid command line.

    python -m pysid TUNE.sid                 window with scopes + live audio
    python -m pysid TUNE.sid --headless      live audio only
    python -m pysid TUNE.sid --wav out.wav   render to a WAV file instead
"""

from __future__ import annotations

import argparse
import sys
import time

from . import psid
from .player import Player, write_wav


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pysid", description="Play C64 SID tunes, emulated in Python.")
    ap.add_argument("tune", help="PSID/RSID file")
    ap.add_argument("-s", "--song", type=int, help="subtune number (default: the file's start song)")
    ap.add_argument("-m", "--model", choices=("6581", "8580"),
                    help="SID chip (default: as recommended by the file, else 6581)")
    ap.add_argument("-r", "--rate", type=int, default=44100, help="sample rate (default 44100)")
    ap.add_argument("-g", "--gain", type=float, default=1.0, help="output gain (default 1.0)")
    ap.add_argument("--no-filter", action="store_true", help="bypass the SID filter")
    ap.add_argument("--wav", metavar="FILE", help="render to a WAV file instead of playing")
    ap.add_argument("-t", "--seconds", type=float,
                    help="length to render/play (default: 180 s for --wav, endless otherwise)")
    ap.add_argument("--headless", action="store_true", help="play without a window")
    ap.add_argument("--latency", type=float, default=0.4,
                    help="audio buffer in seconds for live playback (default 0.4)")
    ap.add_argument("--info", action="store_true", help="print the file header and exit")
    args = ap.parse_args(argv)

    try:
        tune = psid.load(args.tune)
    except (OSError, psid.SIDFileError) as exc:
        print(f"pysid: {exc}", file=sys.stderr)
        return 1
    print(tune.describe())
    if args.info:
        return 0
    if tune.is_rsid:
        print("note: RSID tunes expect a full C64 (CIA/VIC interrupts); "
              "only PSID-style init/play is emulated, so this may not play correctly.")

    song = args.song or tune.start_song
    if not 1 <= song <= tune.songs:
        print(f"pysid: song must be 1..{tune.songs}", file=sys.stderr)
        return 1

    live = not args.wav
    player = Player(tune, song, sample_rate=args.rate, model=args.model,
                    filter_enabled=not args.no_filter, gain=args.gain,
                    scope_rate=44100 if live and not args.headless else 0)
    for w in player.warnings:
        print(f"warning: {w}")

    if args.wav:
        seconds = args.seconds or 180.0
        total = int(seconds * args.rate)
        chunks, done = [], 0
        t0 = time.perf_counter()
        while done < total:
            n = min(args.rate * 5, total - done)
            chunks.append(player.render_int16(n))
            done += n
            print(f"\r  rendered {done / args.rate:6.1f} / {seconds:.0f} s", end="", flush=True)
        import numpy as np
        write_wav(args.wav, np.concatenate(chunks), args.rate)
        dt = time.perf_counter() - t0
        print(f"\nwrote {args.wav}  ({seconds:.0f} s of audio in {dt:.1f} s, "
              f"{seconds / dt:.1f}x real time)")
    elif args.headless:
        from .audio import play_headless
        play_headless(player, args.seconds, args.latency)
    else:
        from .gui import run_gui
        run_gui(player, args.latency)
    for w in player.warnings:
        print(f"warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
