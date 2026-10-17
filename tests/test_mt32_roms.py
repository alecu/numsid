"""Finding and identifying MT-32 / CM-32L ROMs."""

from pathlib import Path

import pytest

from numsid.mt32 import roms

ROMS = Path(__file__).resolve().parent.parent / "roms"
needs_roms = pytest.mark.skipif(not (ROMS / "mt32_ctrl_1_07.rom").exists(),
                                reason="MT-32 ROMs not present in roms/")


def test_no_roms_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NUMSID_ROMS", raising=False)
    monkeypatch.setattr(roms, "search_path", lambda d=None: [tmp_path])
    (tmp_path / "junk.rom").write_bytes(b"\0" * 1024)
    with pytest.raises(roms.RomError, match="control ROM"):
        roms.find("MT-32", tmp_path)


@needs_roms
def test_finds_the_preferred_pair(monkeypatch):
    monkeypatch.setattr(roms, "search_path", lambda d=None: [ROMS])
    control, cblob, pcm, pblob = roms.find("MT-32")
    assert (control.model, control.version, pcm.model) == ("MT-32", "1.07", "MT-32")
    assert len(cblob) == 65536 and len(pblob) == 524288
    control, _, pcm, pblob = roms.find("CM-32L")
    assert (control.version, pcm.model, len(pblob)) == ("1.02", "CM-32L", 1 << 20)


@needs_roms
def test_split_dumps_are_joined(tmp_path):
    ctrl = (ROMS / "mt32_ctrl_1_07.rom").read_bytes()
    pcm = (ROMS / "mt32_pcm.rom").read_bytes()
    (tmp_path / "x1").write_bytes(ctrl[1::2])        # names say nothing about order
    (tmp_path / "x2").write_bytes(ctrl[0::2])
    (tmp_path / "lo").write_bytes(pcm[:len(pcm) // 2])
    (tmp_path / "hi").write_bytes(pcm[len(pcm) // 2:])
    found = roms.scan(tmp_path)
    assert {(i.kind, i.version) for i in found} == {("control", "1.07"), ("pcm", "")}
    assert found[roms.identify(ctrl)] == ctrl


@needs_roms
@pytest.mark.parametrize("name", sorted(p.name for p in ROMS.glob("*ctrl*.rom")
                                         if not p.stem.endswith(("_a", "_b"))))
def test_control_rom_tables(name):
    from numsid.mt32 import control
    blob = (ROMS / name).read_bytes()
    info = roms.identify(blob)
    rom = control.parse(blob, info)
    assert rom.patch(0).name == "AcouPiano1" and rom.patch(127).name == "JungleTune"
    assert rom.patch(64).name == "Acou Bass1" and rom.patch(16).name == "Harpsi 1"
    assert len(rom.rhythm_timbres) == (63 if info.model == "CM-32L" else 30)
    assert rom.rhythm_timbres[0].name == "Acou BD"
    assert rom.rhythm_timbres[29].name == "OpenHiHat2"
    names = {k: rom.rhythm_timbres[v.timbre].name for k, v in rom.rhythm.items()
             if v.timbre is not None}
    assert names[35] == names[36] == "Acou BD" and names[38] == "Acou SD"
    assert names[42] == "Clsd HiHat" and names[46] == "OpenHiHat1"
    assert all(t.active == sum(p is not None for p in t.partials)
               for t in rom.bank_a + rom.bank_b + rom.rhythm_timbres)
