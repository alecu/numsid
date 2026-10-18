"""numsid command line.

    python -m numsid TUNE.sid                 window with scopes + live audio
    python -m numsid TUNE.mod                 the same for an Amiga MOD
    python -m numsid TUNE.mid                 a MIDI file on an emulated MT-32
    python -m numsid TUNE.sid --headless      live audio only
    python -m numsid TUNE.sid --wav out.wav   render to a WAV file instead
"""

from __future__ import annotations

import argparse
import sys
import time

from .amiga import modfile
from .midi import smf
from .player import write_wav
from .tunes import load_tune, open_player

MODELS = ("6581", "8580", "A500", "A1200", "MT-32", "CM-32L")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="numsid", description="Play C64 SID tunes and Amiga MODs, emulated in Python.")
    ap.add_argument("tune", help="PSID/RSID file, a ProTracker/SoundTracker MOD, or a MIDI file")
    ap.add_argument("-s", "--song", type=int, help="SID subtune (default: the file's start song)")
    ap.add_argument("-p", "--pos", type=int, default=0,
                    help="MOD order position, or MIDI start time in seconds (default 0)")
    ap.add_argument("-m", "--model", type=str.upper, choices=MODELS,
                    help="SID chip (default: as the file recommends, else 6581), "
                         "Amiga (default A500) or MIDI synth (default MT-32)")
    ap.add_argument("-r", "--rate", type=int, default=44100, help="sample rate (default 44100)")
    ap.add_argument("-g", "--gain", type=float, default=1.0, help="output gain (default 1.0)")
    ap.add_argument("--no-filter", action="store_true",
                    help="bypass the SID filter / ignore the Amiga LED filter")
    ap.add_argument("--no-digiboost", action="store_true",
                    help="8580: leave $D418 samples nearly inaudible, as on a real C64C")
    ap.add_argument("--separation", type=float, default=100.0,
                    help="MOD stereo separation in percent: 100 is the Amiga's hard "
                         "left/right, 0 mono (default 100)")
    ap.add_argument("--wav", metavar="FILE", help="render to a WAV file instead of playing")
    ap.add_argument("--video", metavar="FILE",
                    help="render the visualiser + audio to an MP4 (needs ffmpeg); length "
                         "defaults to one full loop of the song plus a fade-out")
    ap.add_argument("--fps", type=int, default=60, help="video frame rate (default 60)")
    ap.add_argument("--size", default="1920x1080", help="video size (default 1920x1080)")
    ap.add_argument("--fade", type=float, default=6.0, help="video fade-out seconds (default 6)")
    ap.add_argument("--screenshot", metavar="FILE", help="save a PNG of the visualiser")
    ap.add_argument("--at", type=float, default=30.0,
                    help="playback time for --screenshot, in seconds (default 30)")
    ap.add_argument("--mute", default="", help="voices to mute, e.g. 1,3")
    ap.add_argument("-t", "--seconds", type=float,
                    help="length to render/play (default: 180 s for --wav, endless otherwise)")
    ap.add_argument("--headless", action="store_true", help="play without a window")
    ap.add_argument("--latency", type=float, default=0.4,
                    help="audio buffer in seconds for live playback (default 0.4)")
    ap.add_argument("--info", action="store_true", help="print the file header and exit")
    args = ap.parse_args(argv)

    try:
        tune = load_tune(args.tune)
    except (OSError, ValueError) as exc:
        print(f"numsid: {exc}", file=sys.stderr)
        return 1
    print(tune.describe())
    if args.info:
        return 0

    is_mod = isinstance(tune, modfile.ModFile)
    is_midi = isinstance(tune, smf.MidiFile)
    if is_midi:
        if args.song:
            print("numsid: a MIDI file has one song", file=sys.stderr)
            return 1
        start = args.pos
        models = ("MT-32", "CM-32L")
    elif is_mod:
        if args.song:
            print("numsid: a MOD has one song; use --pos to start at an order position",
                  file=sys.stderr)
            return 1
        if not 0 <= args.pos < len(tune.orders):
            print(f"numsid: position must be 0..{len(tune.orders) - 1}", file=sys.stderr)
            return 1
        start = args.pos
        models = ("A500", "A1200")
    else:
        start = args.song or tune.start_song
        if not 1 <= start <= tune.songs:
            print(f"numsid: song must be 1..{tune.songs}", file=sys.stderr)
            return 1
        models = ("6581", "8580")
    if args.model and args.model not in models:
        print(f"numsid: --model for this file must be {' or '.join(models)}", file=sys.stderr)
        return 1

    visual = args.video or args.screenshot or not (args.wav or args.headless)
    try:
        player = open_player(tune, start, sample_rate=args.rate, model=args.model,
                             filter_enabled=not args.no_filter, gain=args.gain,
                             scope_rate=44100 if visual else 0,
                             separation=args.separation / 100 if is_mod else None,
                             digi_boost=None if is_mod or is_midi else not args.no_digiboost)
    except ValueError as exc:
        print(f"numsid: {exc}", file=sys.stderr)
        return 1
    for v in filter(None, args.mute.split(",")):
        if not 1 <= int(v) <= player.voices:
            print(f"numsid: --mute takes voices 1..{player.voices}", file=sys.stderr)
            return 1
        player.chip.mute[int(v) - 1] = True
    for w in player.warnings:
        print(f"warning: {w}")

    if args.screenshot:
        from .record import screenshot
        screenshot(player, args.screenshot, args.at)
        print(f"wrote {args.screenshot}")
    elif args.video:
        from .tunes import song_length
        from .record import record_video
        seconds = args.seconds
        if not seconds:
            found = song_length(tune, start)
            if found is None:
                seconds = 180.0 + args.fade
                print("song length: no loop or end found; ", end="")
            elif found[1] == "loop":
                seconds = found[0] + args.fade
                print(f"song loops after {found[0]:.1f} s; ", end="")
            else:
                seconds = found[0]
                print(f"song ends after {found[0]:.1f} s; ", end="")
            print(f"recording {seconds:.1f} s")
        w, h = (int(x) for x in args.size.lower().split("x"))
        record_video(player, args.video, seconds, fps=args.fps, size=(w, h), fade_out=args.fade)
    elif args.wav:
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
