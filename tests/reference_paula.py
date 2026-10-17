"""A deliberately naive model of one Paula audio channel, stepped one colour
clock at a time, to check the vectorised renderer against.

It follows the hardware's word-oriented description instead of the
renderer's byte arithmetic: DMA fetches a word into a two-byte buffer, a
length counter counts words down and reloads the location and length
latches when it runs out, and the period and volume are read whenever the
next byte is output.
"""

from numsid.paula import BIN, MIN_PERIOD


class RefChannel:
    def __init__(self):
        self.lc, self.len, self.per, self.vol = 0, 1, 0, 0
        self.dma = False
        self.location = 0
        self.counter = 0          # words left in the block
        self.just_started = False
        self.buffer = []          # bytes fetched but not yet output
        self.clocks = 0           # colour clocks left on the current byte
        self.level = 0            # byte x volume being output

    def start(self):
        if not self.dma:
            self.dma = True
            self.location = self.lc
            self.counter = self.len
            self.just_started = True
            self.buffer = []
            self.clocks = 0

    def stop(self):
        self.dma = False

    def _next_byte(self, ram):
        if not self.buffer:
            if not self.just_started:
                self.counter = (self.counter - 1) & 0xFFFF
                if self.counter == 0:
                    self.counter = self.len
                    self.location = self.lc
            self.just_started = False
            self.buffer = [ram[self.location % len(ram)], ram[(self.location + 1) % len(ram)]]
            self.location += 2
        period = self.per or 65536
        self.clocks = max(period, MIN_PERIOD)
        self.level = int(self.buffer.pop(0)) * min(self.vol, 64)

    def run(self, ram, cycles):
        """Per E-clock cycle: the output summed over its colour clocks."""
        out = []
        for _ in range(cycles):
            acc = 0
            for _ in range(BIN):
                if self.dma:
                    if self.clocks == 0:
                        self._next_byte(ram)
                    acc += self.level
                    self.clocks -= 1
            out.append(acc)
        return out
