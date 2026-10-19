#version 330 core
// Presentation pass: the VIC decoder still runs once per C64 pixel.
uniform sampler2D screen;
uniform vec2 target_size;
uniform bool crt_enabled;
in vec2 uv;
out vec4 final_color;

void main() {
    vec2 size = vec2(textureSize(screen, 0));
    ivec2 pixel = clamp(ivec2(uv * size), ivec2(0), ivec2(size) - 1);
    if (!crt_enabled) {
        final_color = texelFetch(screen, pixel, 0);
        return;
    }

    // Keep individual raster lines, with a soft horizontal phosphor beam.
    vec2 sample_uv = vec2(uv.x, (float(pixel.y) + 0.5) / size.y);
    vec3 color = texture(screen, sample_uv).rgb;
    vec2 texel = 1.0 / size;
    vec3 glow = (texture(screen, sample_uv - vec2(texel.x, 0)).rgb
               + texture(screen, sample_uv + vec2(texel.x, 0)).rgb
               + texture(screen, sample_uv - vec2(0, texel.y)).rgb
               + texture(screen, sample_uv + vec2(0, texel.y)).rgb) * 0.25;
    color = mix(color, max(color, glow), 0.10);

    // Fade unresolved scanlines at small sizes to avoid moire when shrinking.
    float strength = smoothstep(1.0, 2.5, target_size.y / size.y);
    // A half-output-pixel offset keeps a visible dark gap even at 2x scale.
    float phase = uv.y * size.y + 0.5 * size.y / target_size.y;
    float beam = 1.0 - 0.22 * strength * (0.5 + 0.5 * cos(6.2831853 * phase));
    color *= beam;

    // Subtle RGB aperture grille and edge falloff, without cropping the image.
    int stripe = int(gl_FragCoord.x) % 3;
    vec3 grille = vec3(0.96);
    grille[stripe] = 1.0;
    color *= mix(vec3(1.0), grille, strength);
    vec2 centered = uv * 2.0 - 1.0;
    color *= 1.0 - 0.065 * dot(centered, centered);
    final_color = vec4(clamp(color * (1.0 + 0.06 * strength), 0.0, 1.0), 1.0);
}
