"""VIC collision comparators, using integer masks rather than display pixels.

These run on the emulated chip so software can read the collision latches
and receive their interrupts without waiting for a GPU readback.
"""

REVERSE = tuple(int(f"{i:08b}"[::-1], 2) for i in range(256))
EXPAND = tuple(sum(((i >> bit) & 1) * (3 << (bit * 2)) for bit in range(8))
               for i in range(256))


def sprite_masks(machine, line):
    r = machine.vic.regs
    read = machine.vic_read
    pointers = (r[0x18] >> 4) * 1024 + 0x3F8
    masks = []
    for n in range(8):
        bit = 1 << n
        y = int(machine.sprites.rows[n])
        if y >= 21:
            continue
        address = read(pointers + n) * 64 + y * 3
        row = 0
        for col in range(3):
            data = read(address + col)
            if r[0x1C] & bit:
                pairs = (data | (data >> 1)) & 0x55
                data = pairs * 3
            row |= REVERSE[data] << (8 * col)
        if r[0x1D] & bit:
            row = EXPAND[row & 255] | (EXPAND[(row >> 8) & 255] << 16) | \
                  (EXPAND[(row >> 16) & 255] << 32)
        x = r[n * 2] | (((r[0x10] >> n) & 1) << 8)
        masks.append((bit, row << x))
    return masks


def foreground_mask(machine, line):
    r, read = machine.vic.regs, machine.vic_read
    ctrl, multi = r[0x11], bool(r[0x16] & 16)
    top, bottom = (51, 251) if ctrl & 8 else (55, 247)
    if not ctrl & 16 or not top <= line < bottom:
        return 0
    y = line - 48 - (ctrl & 7)
    if not 0 <= y < 200:
        return 0
    screen = (r[0x18] >> 4) << 10
    bitmap, extended = bool(ctrl & 32), bool(ctrl & 64)
    if extended and (bitmap or multi):
        return 0
    base = ((r[0x18] & 8) if bitmap else (r[0x18] & 14)) << 10
    mask = 0
    for column in range(40):
        cell = (y // 8) * 40 + column
        code = read(screen + cell)
        addr = base + (cell if bitmap else code & (63 if extended else 255)) * 8 + y % 8
        data = read(addr)
        if multi and (bitmap or machine.color_ram[cell] & 8):
            data = (data & 0xAA) | ((data & 0xAA) >> 1)
        mask |= REVERSE[data] << (column * 8)
    mask = (mask << (r[0x16] & 7)) & ((1 << 320) - 1)
    if not r[0x16] & 8:
        mask &= ((1 << 311) - 1) ^ ((1 << 7) - 1)
    return mask << 24


def latch_collisions(machine, line):
    vic = machine.vic
    enabled = machine.sprites.active
    if vic.regs[0x1E] & enabled == enabled and vic.regs[0x1F] & enabled == enabled:
        return  # every enabled sprite is already latched until the CPU reads it
    sprites = sprite_masks(machine, line)
    if not sprites:
        return
    background = foreground_mask(machine, line)
    ss = sb = 0
    for i, (bit, mask) in enumerate(sprites):
        if mask & background:
            sb |= bit
        for other, other_mask in sprites[i + 1:]:
            if mask & other_mask:
                ss |= bit | other
    if ss and not vic.regs[0x1E]:
        vic.flags |= 4
    if sb and not vic.regs[0x1F]:
        vic.flags |= 2
    vic.regs[0x1E] |= ss
    vic.regs[0x1F] |= sb
