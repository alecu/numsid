"""Keyboard switches, active-low joystick ports and CIA port wiring."""

from .cia import CIA

# Each row is a CIA port-A bit; columns are port-B bits.
KEY_MATRIX = (
    ("DELETE", "RETURN", "RIGHT", "F7", "F1", "F3", "F5", "DOWN"),
    ("3", "W", "A", "4", "Z", "S", "E", "LSHIFT"),
    ("5", "R", "D", "6", "C", "F", "T", "X"),
    ("7", "Y", "G", "8", "B", "H", "U", "V"),
    ("9", "I", "J", "0", "M", "K", "O", "N"),
    ("+", "P", "L", "-", ".", ":", "@", ","),
    ("POUND", "*", ";", "HOME", "RSHIFT", "=", "UPARROW", "/"),
    ("1", "LEFTARROW", "CTRL", "2", "SPACE", "COMMODORE", "Q", "STOP"),
)
KEY_POSITION = {name: (a, b) for a, row in enumerate(KEY_MATRIX)
                for b, name in enumerate(row)}
JOY_BITS = {"up": 0, "down": 1, "left": 2, "right": 3, "fire": 4}


class Input:
    def __init__(self):
        self.keys: set[str] = set()
        self.joysticks = [0xFF, 0xFF]

    def key(self, name: str, pressed: bool = True):
        name = name.upper()
        if name not in KEY_POSITION:
            raise ValueError(f"unknown C64 key: {name}")
        if pressed:
            self.keys.add(name)
        else:
            self.keys.discard(name)

    def joystick(self, direction: str, pressed: bool = True, port: int = 2):
        if port not in (1, 2):
            raise ValueError("joystick port must be 1 or 2")
        mask = 1 << JOY_BITS[direction]
        if pressed:
            self.joysticks[port - 1] &= ~mask
        else:
            self.joysticks[port - 1] |= mask

    def release(self):
        self.keys.clear()
        self.joysticks[:] = [0xFF, 0xFF]

    def ports(self, regs: bytearray) -> tuple[int, int]:
        a = (regs[0] | (~regs[2] & 255)) & self.joysticks[1]
        b = (regs[1] | (~regs[3] & 255)) & self.joysticks[0]
        # Switches conduct in either direction, including keyboard ghosting.
        switches = [KEY_POSITION[k] for k in self.keys]
        for _ in range(8):
            old = a, b
            for col, row in switches:
                if not a & (1 << col):
                    b &= ~(1 << row)
                if not b & (1 << row):
                    a &= ~(1 << col)
            if (a, b) == old:
                break
        return a, b


class PortCIA(CIA):
    def __init__(self, inputs: Input | None = None):
        super().__init__()
        self.inputs = inputs

    def read(self, reg: int, now: int) -> int:
        port = reg & 15
        if port < 2:
            self.sync(now)
            if self.inputs is not None:
                return self.inputs.ports(self.regs)[port]
            # Unconnected IEC/user-port inputs are pulled high.
            return self.regs[port] | (~self.regs[port + 2] & 255)
        return super().read(reg, now)

    @property
    def vic_bank(self) -> int:
        return (~(self.regs[0] | (~self.regs[2] & 255)) & 3) << 14
