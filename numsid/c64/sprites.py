"""VIC sprite row sequencer, clocked at the PAL DMA/display checkpoints.

Y registers select when a sprite starts. Once started, its row counter is
independent of subsequent Y writes; writing a Y value behind the beam does
not retroactively start DMA. This matters for raster sprite multiplexers.
"""

import numpy as np


class Sprites:
    def __init__(self):
        self.rows = np.full(8, 255, dtype=np.uint8)
        self.next_rows = [255] * 8
        self.repeated = [False] * 8
        self.active = 0

    def begin_line(self):
        self.rows[:] = self.next_rows
        self.active = sum(1 << n for n, row in enumerate(self.next_rows) if row < 21)

    def prepare_next_line(self, regs, line):
        for n in range(8):
            bit = 1 << n
            row = self.next_rows[n]
            if row < 21:
                if regs[0x17] & bit and not self.repeated[n]:
                    self.repeated[n] = True
                else:
                    row += 1
                    self.repeated[n] = False
                    if row >= 21:
                        row = 255
            if row == 255 and regs[0x15] & bit and regs[n * 2 + 1] == line & 255:
                row = 0
                self.repeated[n] = False
            self.next_rows[n] = row
