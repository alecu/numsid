"""Opening a tune of either kind: detect the format and pick its player."""

from __future__ import annotations

from pathlib import Path

from .amiga import modfile, protracker
from .amiga.player import ModPlayer
from .c64 import psid
from .c64.player import SIDPlayer, sid_song_length
from .midi import smf
from .player import Player


class TuneError(ValueError):
    pass


def load_tune(path: str | Path):
    """A PSIDFile, a ModFile or a MidiFile, by the file's contents."""
    blob = Path(path).read_bytes()
    if blob[:4] in (b"PSID", b"RSID"):
        return psid.parse(blob)
    if blob[:4] == b"MThd" or (blob[:4] == b"RIFF" and blob[8:12] == b"RMID"):
        try:
            return smf.parse(blob, str(path))
        except smf.MidiFileError as exc:
            raise TuneError(f"{path}: {exc}") from None
    try:
        return modfile.parse(blob, str(path))
    except modfile.NotAModError:
        raise TuneError(f"{path}: not a PSID/RSID file, a MOD or a MIDI file") from None
    except modfile.ModFileError as exc:
        raise TuneError(f"{path}: {exc}") from None


def open_player(tune, song: int | None = None, **kwargs) -> "Player":
    """A player for a tune or a path.  `song` is the subtune of a SID file,
    or the starting order position of a MOD."""
    if isinstance(tune, (str, Path)):
        tune = load_tune(tune)
    separation = kwargs.pop("separation", None)        # MOD only
    digi_boost = kwargs.pop("digi_boost", None)        # SID only
    if isinstance(tune, smf.MidiFile):
        raise TuneError("MIDI playback isn't implemented yet; --info works")
    if isinstance(tune, modfile.ModFile):
        if separation is not None:
            kwargs["separation"] = separation
        return ModPlayer(tune, song or 0, **kwargs)
    if digi_boost is not None:
        kwargs["digi_boost"] = digi_boost
    return SIDPlayer(tune, song, **kwargs)


def song_length(tune, song: int | None = None, max_seconds: float = 600.0, **kwargs) \
        -> tuple[float, str] | None:
    """How long a song lasts: (seconds, "loop") for intro plus one pass of
    its loop, or (seconds, "end") for one that stops.  None if neither is
    found within `max_seconds`.  For a MOD, `song` is the starting order
    position, and the replayer's own bookkeeping gives an exact answer."""
    if isinstance(tune, modfile.ModFile):
        return protracker.song_length(tune, song or 0, max_seconds=max(max_seconds, 3600.0))
    return sid_song_length(tune, song or tune.start_song, max_seconds, **kwargs)
