"""Opt-in integration against the user's unmodified Commando +7 HF PRG.

NUMC64_COMMANDO=/path/to/Commando.zip python -m pytest tests/test_commando.py
No game code, ROM contents, or machine addresses are patched to start it.
"""

import os

import numpy as np
import pytest

from numsid.c64.computer import Computer, PAL_CLOCK
from numsid.c64.program import Program
from numsid.c64.roms import ROMs, find_roms
from numsid.c64.sid import SID


@pytest.mark.skipif(not os.environ.get("NUMC64_COMMANDO"),
                    reason="set NUMC64_COMMANDO to the Commando +7 HF PRG or ZIP")
def test_commando_boot_trainer_gameplay_input_and_sound():
    path = os.environ.get("NUMC64_ROMS") or find_roms()
    assert path is not None, "set NUMC64_ROMS to the VICE tarball / ROM directory"
    machine = Computer(ROMs.load(path), SID(PAL_CLOCK, 44100, "6581"))
    machine.boot()
    assert machine.ready
    machine.load_program(Program.load(os.environ["NUMC64_COMMANDO"]))

    def frames(n):
        for _ in range(n):
            machine.step_frame(capture=False)

    def press(name, following):
        machine.inputs.key(name)
        frames(5)
        machine.inputs.key(name, False)
        frames(following)

    frames(900)
    press("SPACE", 900)       # crack intro -> info linker
    press("STOP", 1800)       # skip info -> decompress -> trainer
    for _ in range(6):
        press("N", 30)
    press("1", 500)          # select Commando rather than Space Invasion
    assert machine.cia2.vic_bank == 0xC000
    assert machine.vic.regs[0x16] & 16  # game's multicolor display

    machine.inputs.joystick("fire")
    frames(10)
    machine.inputs.joystick("fire", False)
    frames(180)
    x = machine.vic.regs[6]  # player's sprite, observed at the end of the frame
    machine.inputs.joystick("right")
    machine.inputs.joystick("fire")
    audio = []
    for _ in range(30):
        machine.step_frame(capture=False)
        audio.append(machine.audio)
    machine.inputs.release()
    # Depending on raster phase, the game samples input on 29 or 30 updates.
    assert (machine.vic.regs[6] - x) & 255 == pytest.approx(60, abs=2)
    pcm = np.concatenate(audio)
    assert len(pcm) == pytest.approx(30 * machine.vic.frame * 44100 / PAL_CLOCK, abs=2)
    assert np.isfinite(pcm).all() and np.sqrt(np.mean(pcm ** 2)) > 0.005
    frame = machine.step_frame()
    assert frame.banks[100] == 3 and np.any(frame.memory[100])

    # Scrolling used to start DMA for dummy sprites whose Y had already
    # passed, delaying the IRQ chain and corrupting the score strip.
    machine.inputs.joystick("up")
    machine.inputs.joystick("fire")
    seen_scroll = set()
    for _ in range(240):
        frame = machine.step_frame()
        seen_scroll.add(int(frame.registers[100, 0x11]) & 7)
        assert frame.registers[225, 0x11] & 0x60 == 0
        assert frame.registers[225, 0x18] == 0x84
        assert frame.registers[225, 0x21] == 0
    machine.inputs.release()
    assert len(seen_scroll) == 8
