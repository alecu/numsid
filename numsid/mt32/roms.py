"""Finding and identifying MT-32 and CM-32L ROM images.

A module needs two: a control ROM (the firmware, with the factory timbres
and tables) and a PCM ROM (the samples).  Images are identified by SHA-1,
so file names don't matter.  Dumps that come as two chips, as in MAME's
sets, are joined: control ROM halves `_a`/`_b` interleave byte by byte, PCM
halves `_l`/`_h` concatenate.

Where to look, in order: the directory given on the command line, then
`$NUMSID_ROMS`, then `roms/` in the current directory and next to the
package.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


class RomError(ValueError):
    pass


@dataclass(frozen=True)
class RomInfo:
    kind: str                # "control" or "pcm"
    model: str               # "MT-32" (1.x), "MT-32 new" (2.x) or "CM-32L"
    version: str
    size: int


ROMS = {
    "5a5cb5a77d7d55ee69657c2f870416daed52dea7": RomInfo("control", "MT-32", "1.04", 65536),
    "e17a3a6d265bf1fa150312061134293d2b58288c": RomInfo("control", "MT-32", "1.05", 65536),
    "a553481f4e2794c10cfe597fef154eef0d8257de": RomInfo("control", "MT-32", "1.06", 65536),
    "b083518fffb7f66b03c23b7eb4f868e62dc5a987": RomInfo("control", "MT-32", "1.07", 65536),
    "7b8c2a5ddb42fd0732e2f22b3340dcf5360edf92": RomInfo("control", "MT-32", "Blue Ridge", 65536),
    "2c16432b6c73dd2a3947cba950a0f4c19d6180eb": RomInfo("control", "MT-32 new", "2.04", 131072),
    "2869cf4c235d671668cfcb62415e2ce8323ad4ed": RomInfo("control", "MT-32 new", "2.06", 131072),
    "47b52adefedaec475c925e54340e37673c11707c": RomInfo("control", "MT-32 new", "2.07", 131072),
    "73683d585cd6948cc19547942ca0e14a0319456d": RomInfo("control", "CM-32L", "1.00", 65536),
    "a439fbb390da38cada95a7cbb1d6ca199cd66ef8": RomInfo("control", "CM-32L", "1.02", 65536),
    "dc1c5b1b90a4646d00f7daf3679733c7badc7077": RomInfo("control", "CM-32L", "LN 1.00", 65536),
    "f6b1eebc4b2d200ec6d3d21d51325d5b48c60252": RomInfo("pcm", "MT-32", "", 524288),
    "289cc298ad532b702461bfc738009d9ebe8025ea": RomInfo("pcm", "CM-32L", "", 1048576),
}

# the control ROM picked for each --model, best first
PREFERRED = {
    "MT-32": ("1.07", "1.06", "1.05", "1.04", "Blue Ridge", "2.07", "2.06", "2.04"),
    "CM-32L": ("1.02", "1.00", "LN 1.00"),
}


def identify(blob: bytes) -> RomInfo | None:
    return ROMS.get(hashlib.sha1(blob).hexdigest())


def _interleave(even: bytes, odd: bytes) -> bytes:
    out = bytearray(len(even) + len(odd))
    out[0::2] = even
    out[1::2] = odd
    return bytes(out)


def scan(directory: str | Path) -> dict[RomInfo, bytes]:
    """Every known ROM image in `directory`, whole or as two halves."""
    found: dict[RomInfo, bytes] = {}
    files = [p for p in Path(directory).iterdir()
             if p.is_file() and p.stat().st_size <= 1 << 20]
    blobs = {p: p.read_bytes() for p in files}
    for blob in blobs.values():
        info = identify(blob)
        if info:
            found.setdefault(info, blob)
    if {i.kind for i in found} == {"control", "pcm"}:
        return found
    # split dumps: try every pair of same-sized files, both ways round
    halves = [(p, b) for p, b in blobs.items() if identify(b) is None]
    for i, (pa, a) in enumerate(halves):
        for pb, b in halves[i + 1:]:
            if len(a) != len(b):
                continue
            for x, y in ((a, b), (b, a)):
                for joined in (_interleave(x, y), x + y):
                    info = identify(joined)
                    if info:
                        found.setdefault(info, joined)
    return found


def search_path(directory: str | Path | None = None) -> list[Path]:
    dirs = [Path(directory)] if directory else []
    if os.environ.get("NUMSID_ROMS"):
        dirs.append(Path(os.environ["NUMSID_ROMS"]))
    dirs += [Path("roms"), Path(__file__).resolve().parent.parent.parent / "roms"]
    seen, out = set(), []
    for d in dirs:
        r = d.resolve()
        if r.is_dir() and r not in seen:
            seen.add(r)
            out.append(d)
    return out


def find(model: str = "MT-32", directory: str | Path | None = None) \
        -> tuple[RomInfo, bytes, RomInfo, bytes]:
    """(control info, control ROM, PCM info, PCM ROM) for `model`."""
    found: dict[RomInfo, bytes] = {}
    for d in search_path(directory):
        for info, blob in scan(d).items():
            found.setdefault(info, blob)
    by_version = {i.version: i for i in found if i.kind == "control"
                  and (i.model == "CM-32L") == (model == "CM-32L")}
    control = next((by_version[v] for v in PREFERRED[model] if v in by_version), None)
    if control is None:
        raise RomError(f"no {model} control ROM found; put the ROM files in roms/ "
                       f"or point $NUMSID_ROMS at them")
    # a CM-32L needs its own, larger PCM ROM; an MT-32 can use either (the
    # CM-32L's starts with the MT-32's samples) but prefers its own
    order = ("CM-32L",) if model == "CM-32L" else ("MT-32", "CM-32L")
    pcm = next((i for m in order for i in found if i.kind == "pcm" and i.model == m), None)
    if pcm is None:
        raise RomError(f"no {model} PCM ROM found")
    blob = found[pcm]
    if model != "CM-32L" and pcm.model == "CM-32L":
        blob = blob[:524288]
    return control, found[control], pcm, blob
