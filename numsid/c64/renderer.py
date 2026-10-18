"""OpenGL 3.3 VIC-II decoder. Python uploads memory, GLSL builds the image."""

import ctypes
from importlib.resources import files

import numpy as np

from .computer import WIDTH, HEIGHT, VideoFrame


class VICRenderer:
    """Create with an active OpenGL context (a pyglet window, also headless)."""
    def __init__(self, chargen: bytes, *, crt: bool = True):
        from pyglet import gl
        from pyglet.graphics.shader import Shader, ShaderProgram
        self.gl = gl
        self.crt = crt
        limit = gl.GLint()
        gl.glGetIntegerv(gl.GL_MAX_TEXTURE_SIZE, ctypes.byref(limit))
        if limit.value < 312 * 16:
            raise RuntimeError("VIC-II scanline textures require GL_MAX_TEXTURE_SIZE >= 4992")
        root = files("numsid.c64").joinpath("shaders")
        self.program = ShaderProgram(Shader(root.joinpath("vic.vert").read_text(), "vertex"),
                                     Shader(root.joinpath("vic.frag").read_text(), "fragment"))
        self.quad = self.program.vertex_list(4, gl.GL_TRIANGLE_STRIP,
                                            position=("f", (-1, -1, 1, -1, -1, 1, 1, 1)))
        self.presentation = ShaderProgram(
            Shader(root.joinpath("vic.vert").read_text(), "vertex"),
            Shader(root.joinpath("crt.frag").read_text(), "fragment"))
        self.presentation_quad = self.presentation.vertex_list(
            4, gl.GL_TRIANGLE_STRIP, position=("f", (-1, -1, 1, -1, -1, 1, 1, 1)))
        self.presentation.use()
        self.presentation["screen"] = 0
        self.presentation.stop()
        self.textures = []
        self._lines = np.arange(312, dtype=np.int16)[:, None]
        self._sprites = np.arange(8, dtype=np.uint8)[None, :]
        self.program.use()
        for unit, (name, width, height) in enumerate((("ram", 1024, 312 * 16),
                                                     ("color_ram", 1024, 312),
                                                     ("vic", 64, 312),
                                                     ("chargen", 256, 16))):
            texture = gl.GLuint()
            gl.glGenTextures(1, ctypes.byref(texture))
            self.textures.append((texture, width, height))
            gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R8UI, width, height, 0,
                            gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, None)
            self.program[name] = unit
        self._upload(3, np.frombuffer(chargen, dtype=np.uint8))
        self.program.stop()
        # Decode once at C64 resolution; scaling the shader itself would
        # repeat every RAM lookup nine times in the default 3x window.
        self.surface, self.framebuffer = gl.GLuint(), gl.GLuint()
        gl.glGenTextures(1, ctypes.byref(self.surface))
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.surface)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, WIDTH, HEIGHT, 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glGenFramebuffers(1, ctypes.byref(self.framebuffer))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.framebuffer)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, self.surface, 0)
        if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError("cannot create VIC-II framebuffer")
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        self.dirty = True

    def _upload(self, unit: int, data: np.ndarray):
        gl = self.gl
        texture, width, height = self.textures[unit]
        gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, width, height,
                           gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE,
                           data.ctypes.data_as(ctypes.c_void_p))

    def upload(self, frame: VideoFrame):
        self._upload(0, frame.memory)
        self._upload(1, frame.colors)
        # Last VIC register is unused on hardware: carry CIA2's bank there.
        frame.registers[:, 63] = frame.banks
        # Row counters are hardware state; the shader still decodes sprite RAM.
        rows = frame.sprite_rows
        if rows is None:  # standalone synthetic video packets used by tools/tests
            dy = (self._lines - frame.registers[:, 1:16:2]) & 255
            expanded = (frame.registers[:, 0x17:0x18] >> self._sprites) & 1
            enabled = (frame.registers[:, 0x15:0x16] >> self._sprites) & 1
            rows = np.where(enabled & (dy < (21 + 21 * expanded)), dy >> expanded, 255)
        frame.registers[:, 48:56] = rows
        frame.registers[:, 47] = np.sum((rows < 21).astype(np.uint8) << self._sprites,
                                       axis=1).astype(np.uint8)
        self._upload(2, frame.registers)
        self.dirty = True

    def draw(self, width: int, height: int):
        gl = self.gl
        scale = min(width / WIDTH, height / HEIGHT)
        w, h = max(1, round(WIDTH * scale)), max(1, round(HEIGHT * scale))
        if self.dirty:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.framebuffer)
            gl.glViewport(0, 0, WIDTH, HEIGHT)
            self.program.use()
            for unit, (texture, _, _) in enumerate(self.textures):
                gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
                gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            self.quad.draw(gl.GL_TRIANGLE_STRIP)
            self.program.stop()
            self.dirty = False
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        x, y = (width - w) // 2, (height - h) // 2
        gl.glViewport(x, y, w, h)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.surface)
        self.presentation.use()
        self.presentation["target_size"] = (float(w), float(h))
        self.presentation["crt_enabled"] = self.crt
        self.presentation_quad.draw(gl.GL_TRIANGLE_STRIP)
        self.presentation.stop()
        gl.glViewport(0, 0, width, height)

    def delete(self):
        self.quad.delete()
        self.program.delete()
        self.presentation_quad.delete()
        self.presentation.delete()
        for texture, _, _ in self.textures:
            self.gl.glDeleteTextures(1, ctypes.byref(texture))
        self.gl.glDeleteTextures(1, ctypes.byref(self.surface))
        self.gl.glDeleteFramebuffers(1, ctypes.byref(self.framebuffer))
