"""PRG loading, also directly from a ZIP containing one program."""

from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile


@dataclass(frozen=True)
class Program:
    name: str
    address: int
    data: bytes

    @classmethod
    def load(cls, path: str | Path) -> "Program":
        path = Path(path).expanduser()
        if path.suffix.lower() == ".zip":
            with ZipFile(path) as archive:
                files = [i for i in archive.infolist() if i.filename.lower().endswith(".prg")
                         and not i.is_dir()]
                if len(files) != 1:
                    raise ValueError("ZIP must contain exactly one .prg; extract the desired PRG")
                member = files[0]
                if member.file_size > 65538:
                    raise ValueError("PRG is larger than C64 memory")
                blob, name = archive.read(member), Path(member.filename).name
        else:
            if path.stat().st_size > 65538:
                raise ValueError("PRG is larger than C64 memory")
            blob, name = path.read_bytes(), path.name
        if len(blob) < 3:
            raise ValueError("PRG needs a two-byte load address and program data")
        address = int.from_bytes(blob[:2], "little")
        if address + len(blob) - 2 > 65536:
            raise ValueError("PRG extends beyond $FFFF")
        return cls(name, address, blob[2:])
