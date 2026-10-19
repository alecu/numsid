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


def test_pcm_decode_format():
    import numpy as np
    from numsid.mt32.pcm import decode
    words = np.array([0x0000, 0x8000, 0x7F7F, 0xFF7F, 0x7F3F, 0x10C0, 0x1040], dtype=np.uint16)
    out = decode(words)
    assert out[0] == 0 and out[1] == 0                     # silence, either sign
    assert out[2] == pytest.approx(1.0, abs=0.01) and out[3] == pytest.approx(-out[2])
    assert out[4] == pytest.approx(out[2] / 256, rel=0.01) # bit 6 is the top magnitude bit
    assert out[5] == out[6]                                # bit 7 is not part of the value
    up = decode(np.array([0x2040, 0x3040], dtype=np.uint16))
    assert (up[1] + 1 / 65535) / (up[0] + 1 / 65535) == pytest.approx(2.0)   # 16 steps per octave


@needs_roms
def test_pcm_wave_table():
    import numpy as np
    from numsid.mt32 import control, pcm
    info, cblob, pinfo, pblob = roms.find("MT-32", ROMS)
    rom = pcm.PCMROM(pblob, cblob, info)
    ctl = control.parse(cblob, info)
    assert len(rom.waves) == 128
    bd = rom.waves[ctl.rhythm_timbres[0].partials[0][5]]   # Acou BD's wave
    assert (bd.start, bd.length, bd.looped) == (0x8000, 4096, False)
    # every one-shot wave starts with silence and then sound
    for w in rom.waves:
        assert w.start + w.length <= len(rom.samples)
    x = rom.wave(54)
    assert not x[:8].any() and np.abs(x).max() > 0.5
    # pitched waves have no single-sample spikes at their zero crossings
    for k in (21, 23, 31, 49):
        y = rom.wave(k)
        assert not np.any((np.abs(y[1:-1] - y[:-2]) > 0.5) & (np.abs(y[1:-1] - y[2:]) > 0.5))
    # a looped wave's measured period matches its pitch field
    w = rom.waves[97]
    seg = rom.wave(97)[-2048:] - rom.wave(97)[-2048:].mean()
    r = np.array([np.dot(seg[:1024], seg[lag:lag + 1024]) for lag in range(16, 512)])
    period = np.argmax(r) + 16
    assert 32000 / period == pytest.approx(w.native_hz, rel=0.03)
    cinfo, cblob, _, cpcm = roms.find("CM-32L", ROMS)
    assert len(pcm.PCMROM(cpcm, cblob, cinfo).waves) == 256
