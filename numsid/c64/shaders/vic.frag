#version 330 core
// All C64 pixels are decoded here from raw memory and register textures.
uniform usampler2D ram;
uniform usampler2D color_ram;
uniform usampler2D vic;
uniform usampler2D chargen;
in vec2 uv;
out vec4 final_color;

const vec3 palette[16] = vec3[16](
    vec3(0,0,0), vec3(255,255,255), vec3(129,51,56), vec3(117,206,200),
    vec3(142,60,151), vec3(86,172,77), vec3(46,44,155), vec3(237,241,113),
    vec3(142,80,41), vec3(85,56,0), vec3(196,108,113), vec3(74,74,74),
    vec3(123,123,123), vec3(169,255,159), vec3(112,109,235), vec3(178,178,178)
);
int line;
int bank;
int screen_base;
int R(int addr) { return int(texelFetch(vic, ivec2(addr, line), 0).r); }
int M(int addr) {
    addr &= 16383;
    // The VIC sees character ROM in banks 0 and 2 at $1000-$1fff.
    if ((bank & 1) == 0 && (addr & 0x3000) == 0x1000) {
        int a = addr & 4095;
        return int(texelFetch(chargen, ivec2(a & 255, a >> 8), 0).r);
    }
    return int(texelFetch(ram, ivec2(addr & 1023, line * 16 + (addr >> 10)), 0).r);
}
int C(int addr) { return int(texelFetch(color_ram, ivec2(addr & 1023, line), 0).r) & 15; }

int background(int x, int y, out bool foreground) {
    foreground = false;
    int ctrl1 = R(0x11), ctrl2 = R(0x16), pointers = R(0x18);
    int gx = x - (ctrl2 & 7), gy = y - 48 - (ctrl1 & 7);
    if (gx < 0 || gx >= 320 || gy < 0 || gy >= 200) return R(0x21) & 15;
    int cell = (gy >> 3) * 40 + (gx >> 3);
    int screen = M(((pointers >> 4) << 10) + cell);
    int color = C(cell), bits;
    bool bitmap = (ctrl1 & 32) != 0, extended = (ctrl1 & 64) != 0;
    bool multi = (ctrl2 & 16) != 0;
    if (extended && (bitmap || multi)) return 0;
    if (bitmap) {
        bits = M(((pointers & 8) << 10) + cell * 8 + (gy & 7));
        if (multi) {
            int pair = (bits >> (6 - (gx & 6))) & 3;
            foreground = pair >= 2;
            if (pair == 0) return R(0x21) & 15;
            if (pair == 1) return screen >> 4;
            if (pair == 2) return screen & 15;
            return color;
        }
        foreground = ((bits >> (7 - (gx & 7))) & 1) != 0;
        return foreground ? screen >> 4 : screen & 15;
    }
    int code = extended ? screen & 63 : screen;
    bits = M(((pointers & 14) << 10) + code * 8 + (gy & 7));
    if (multi && (color & 8) != 0) {
        int pair = (bits >> (6 - (gx & 6))) & 3;
        foreground = pair >= 2;
        return pair == 3 ? color & 7 : R(0x21 + pair) & 15;
    }
    foreground = ((bits >> (7 - (gx & 7))) & 1) != 0;
    return foreground ? color : R(0x21 + (extended ? screen >> 6 : 0)) & 15;
}

int sprite(int n, int x, int y) {
    int mask = 1 << n;
    int px = R(n * 2) | (((R(0x10) >> n) & 1) << 8);
    int dx = x + 24 - px, dy = R(48 + n);
    if ((R(0x1d) & mask) != 0) dx >>= 1;
    if (dx < 0 || dx >= 24 || dy >= 21) return -1;
    int pointer = M(screen_base + 0x3f8 + n);
    int bits = M(pointer * 64 + dy * 3 + (dx >> 3));
    if ((R(0x1c) & mask) != 0) {
        int pair = (bits >> (6 - (dx & 6))) & 3;
        if (pair == 0) return -1;
        if (pair == 1) return R(0x25) & 15;
        if (pair == 3) return R(0x26) & 15;
        return R(0x27 + n) & 15;
    }
    return ((bits >> (7 - (dx & 7))) & 1) != 0 ? R(0x27 + n) & 15 : -1;
}

void main() {
    ivec2 p = ivec2(floor(uv * vec2(384, 272)));
    line = 16 + 271 - p.y;
    bank = R(63);
    screen_base = (R(0x18) >> 4) << 10;
    int x = p.x - 32;
    int ctrl1 = R(0x11), ctrl2 = R(0x16);
    int top = (ctrl1 & 8) != 0 ? 51 : 55;
    int bottom = (ctrl1 & 8) != 0 ? 251 : 247;
    int left = (ctrl2 & 8) != 0 ? 0 : 7;
    int right = (ctrl2 & 8) != 0 ? 320 : 311;
    int color = R(0x20) & 15;
    if ((ctrl1 & 16) != 0 && line >= top && line < bottom && x >= left && x < right) {
        bool foreground;
        color = background(x, line, foreground);
        int winner = -1, sprite_color = 0;
        int active_sprites = R(47);
        for (int n = 7; n >= 0; --n) {
            if ((active_sprites & (1 << n)) != 0) {
                int c = sprite(n, x, line);
                if (c >= 0) { winner = n; sprite_color = c; }
            }
        }
        if (winner >= 0 && (!foreground || (R(0x1b) & (1 << winner)) == 0))
            color = sprite_color;
    }
    final_color = vec4(palette[color] / 255.0, 1.0);
}
