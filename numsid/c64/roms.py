"""Load user-supplied Commodore ROMs without extracting an archive to disk."""

from dataclasses import dataclass
from pathlib import Path
import tarfile


@dataclass(frozen=True)
class ROMs:
    basic: bytes
    kernal: bytes
    chargen: bytes

    def __post_init__(self):
        for name, size in (("basic", 8192), ("kernal", 8192), ("chargen", 4096)):
            if len(getattr(self, name)) != size:
                raise ValueError(f"{name} ROM must contain {size} bytes")

    @classmethod
    def load(cls, path: str | Path) -> "ROMs":
        """Accept a VICE source tarball or a directory of C64 ROM files.

        Prefer the standard C64 PAL KERNAL, rather than a random revision
        or one of the ROMs for a different Commodore machine.
        """
        path = Path(path).expanduser()
        choices = (("basic-901226-01.bin", "basic"),
                   ("kernal-901227-03.bin", "kernal"),
                   ("chargen-901225-01.bin", "chargen"))
        if path.is_dir():
            roots = (path, path / "C64", path / "data" / "C64")
            blobs = []
            for names in choices:
                found = next((p / n for p in roots for n in names if (p / n).is_file()), None)
                if found is None:
                    raise ValueError(f"missing {names[0]} in {path}")
                blobs.append(found.read_bytes())
        else:
            with tarfile.open(path, "r:*") as archive:
                members = {m.name: m for m in archive.getmembers() if m.isfile()}
                blobs = []
                for names in choices:
                    member = next((m for n in names for key, m in members.items()
                                   if key.endswith("/data/C64/" + n)
                                   or key in ("data/C64/" + n, "C64/" + n, n)), None)
                    if member is None:
                        raise ValueError(f"missing {names[0]} in {path}")
                    if member.size > 8192:
                        raise ValueError(f"oversized ROM: {member.name}")
                    with archive.extractfile(member) as source:
                        blobs.append(source.read())
        return cls(*blobs)


def find_roms() -> Path | None:
    """Convenient local discovery; ROMs are never bundled or downloaded."""
    for path in (Path("roms"), *sorted((Path.home() / "Downloads").glob("vice-*.tar.*"),
                                     reverse=True)):
        if path.exists():
            return path
    return None
