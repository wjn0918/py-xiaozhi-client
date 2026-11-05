# -*- coding: utf-8 -*-
"""可选的 Live2D 渲染窗口：对应 web 端 Live2D 部分（js/live2d/live2d.js + PIXI）

- pygame + live2d-py 渲染模型与背景
- 播放 TTS 时根据播放音量驱动口型（ParamMouthOpenY / PARAM_MOUTH_OPEN_Y）
- 收到情绪时随机触发一个动作（模拟 web 端 triggerEmotionAction）
- 空格键：开始/停止录音；ESC：退出
- 左下角聊天面板渲染后端返回内容（STT/LLM/TTS 文本），顶部显示状态
- 自动识别 live2d v2（.moc）/ v3（.moc3）模型
"""
import importlib
import json
import os
import queue
import random
import threading

import pygame
from pygame.locals import (
    DOUBLEBUF, KEYDOWN, K_ESCAPE, K_SPACE, MOUSEMOTION, OPENGL, QUIT,
)

# PyOpenGL 用于把文本纹理叠加到 OpenGL 窗口
try:
    from OpenGL import GL as _gl
    HAS_PYOpenGL = True
except ImportError:
    _gl = None
    HAS_PYOpenGL = False


def _wrap_text(font, text, max_width):
    """按像素宽度对文本换行（中英文通用，逐字符贪心）。"""
    lines = []
    for raw in text.split("\n"):
        line = ""
        for ch in raw:
            if font.size(line + ch)[0] > max_width and line:
                lines.append(line)
                line = ch
            else:
                line += ch
        lines.append(line)
    return lines


def _gl_version():
    """读取当前 OpenGL 上下文版本（用于排查渲染问题）。"""
    try:
        return _gl.glGetString(_gl.GL_VERSION).decode("utf-8", "replace")
    except Exception:
        return "unknown"


def _gl_error(tag):
    """清空并打印 GL 错误，返回错误码。"""
    try:
        err = int(_gl.glGetError())
    except Exception:
        return 0
    if err:
        print(f"[Live2D] GL 错误（{tag}）: 0x{err:04X}", flush=True)
    return err


def _gl_clear_errors():
    """吞掉之前（live2d 渲染）遗留的错误标记，避免误判。"""
    try:
        for _ in range(8):
            if not _gl.glGetError():
                break
    except Exception:
        pass


class TextTexture:
    """把 pygame Surface 上传为 OpenGL 纹理并绘制（固定管线）。

    关键点：live2d-py 每帧渲染结束后会把 shader program、VBO、活动纹理单元
    留在 GL 上下文里。此时直接用 immediate mode 画四边形，顶点/纹理状态是
    未定义的 —— 表现就是画出一块灰/白色方块，看不到文字。
    所以每次绘制前必须显式把状态恢复成「固定管线 + 纹理单元 0 + 无 VAO」。
    """

    def __init__(self):
        self.tex = 0
        self.size = (0, 0)
        self._data = None          # 纹理数据：第 0 行 = 图像顶部（配合 v=0 在屏幕上方）
        self._data_pixels = None   # glDrawPixels 数据：第 0 行 = 图像底部（窗口坐标原点在左下）

    @staticmethod
    def _surface_bytes(surface, flip):
        try:
            return pygame.image.tobytes(surface, "RGBA", flip)
        except AttributeError:  # pygame < 2.3
            return pygame.image.tostring(surface, "RGBA", flip)

    def update(self, surface):
        w, h = surface.get_size()
        # 纹理按「不翻转」上传：GL 的 v=0 取第 0 行，正好是图像顶部，
        # 而下面绘制时 v=0 映射到屏幕上方，两者一致才不会上下颠倒。
        data = self._surface_bytes(surface, False)
        self._data = data
        self._data_pixels = self._surface_bytes(surface, True)
        if not self.tex:
            self.tex = int(_gl.glGenTextures(1))
        _gl.glBindTexture(_gl.GL_TEXTURE_2D, self.tex)
        _gl.glTexParameteri(_gl.GL_TEXTURE_2D, _gl.GL_TEXTURE_MIN_FILTER, _gl.GL_LINEAR)
        _gl.glTexParameteri(_gl.GL_TEXTURE_2D, _gl.GL_TEXTURE_MAG_FILTER, _gl.GL_LINEAR)
        _gl.glTexParameteri(_gl.GL_TEXTURE_2D, _gl.GL_TEXTURE_WRAP_S, _gl.GL_CLAMP_TO_EDGE)
        _gl.glTexParameteri(_gl.GL_TEXTURE_2D, _gl.GL_TEXTURE_WRAP_T, _gl.GL_CLAMP_TO_EDGE)
        _gl.glPixelStorei(_gl.GL_UNPACK_ALIGNMENT, 1)
        _gl.glTexImage2D(_gl.GL_TEXTURE_2D, 0, _gl.GL_RGBA, w, h, 0,
                         _gl.GL_RGBA, _gl.GL_UNSIGNED_BYTE, data)
        _gl.glBindTexture(_gl.GL_TEXTURE_2D, 0)
        self.size = (w, h)

    @staticmethod
    def _prepare_pipeline():
        """把 GL 恢复到固定管线可安全绘制叠加层的最简状态。"""
        _gl_clear_errors()
        # 1) 解绑 live2d 的着色器程序（否则 immediate mode 输出未定义）
        try:
            _gl.glUseProgram(0)
        except Exception:
            pass
        # 2) 纹理单元回到 0（live2d 可能改过 active texture，导致采样到空纹理）
        try:
            _gl.glActiveTexture(_gl.GL_TEXTURE0)
            _gl.glClientActiveTexture(_gl.GL_TEXTURE0)
        except Exception:
            pass
        # 3) 解绑 VAO，并关闭遗留的顶点数组（VBO）开关
        try:
            _gl.glBindVertexArray(0)
        except Exception:
            pass
        for state in (_gl.GL_VERTEX_ARRAY, _gl.GL_TEXTURE_COORD_ARRAY,
                      _gl.GL_NORMAL_ARRAY, _gl.GL_COLOR_ARRAY):
            try:
                _gl.glDisableClientState(state)
            except Exception:
                pass
        # 4) 关闭会干扰叠加层的固定功能状态
        for cap in (_gl.GL_SCISSOR_TEST, _gl.GL_STENCIL_TEST, _gl.GL_CULL_FACE,
                    _gl.GL_DEPTH_TEST, _gl.GL_ALPHA_TEST, _gl.GL_LIGHTING, _gl.GL_FOG):
            try:
                _gl.glDisable(cap)
            except Exception:
                pass
        try:
            _gl.glPolygonMode(_gl.GL_FRONT_AND_BACK, _gl.GL_FILL)
        except Exception:
            pass
        _gl.glDepthMask(_gl.GL_FALSE)
        _gl.glColorMask(_gl.GL_TRUE, _gl.GL_TRUE, _gl.GL_TRUE, _gl.GL_TRUE)
        # 5) 混合与纹理采样
        _gl.glEnable(_gl.GL_BLEND)
        try:
            _gl.glBlendEquation(_gl.GL_FUNC_ADD)
        except Exception:
            pass
        _gl.glBlendFunc(_gl.GL_SRC_ALPHA, _gl.GL_ONE_MINUS_SRC_ALPHA)
        _gl.glEnable(_gl.GL_TEXTURE_2D)
        try:
            _gl.glTexEnvi(_gl.GL_TEXTURE_ENV, _gl.GL_TEXTURE_ENV_MODE, _gl.GL_MODULATE)
        except Exception:
            pass

    def draw(self, x, y, view_w, view_h):
        if not self.tex:
            return
        w, h = self.size
        if w <= 0 or h <= 0:
            return

        self._prepare_pipeline()
        # viewport 可能被 live2d 改过，强制对齐窗口尺寸，否则叠加层会错位
        vp = None
        try:
            vp = _gl.glGetIntegerv(_gl.GL_VIEWPORT)
            _gl.glViewport(0, 0, int(view_w), int(view_h))
        except Exception:
            vp = None
        _gl.glBindTexture(_gl.GL_TEXTURE_2D, self.tex)

        _gl.glMatrixMode(_gl.GL_PROJECTION)
        _gl.glPushMatrix()
        _gl.glLoadIdentity()
        _gl.glOrtho(0, view_w, view_h, 0, -1, 1)
        _gl.glMatrixMode(_gl.GL_MODELVIEW)
        _gl.glPushMatrix()
        _gl.glLoadIdentity()

        _gl.glColor4f(1.0, 1.0, 1.0, 1.0)
        _gl.glBegin(_gl.GL_QUADS)
        _gl.glTexCoord2f(0.0, 0.0); _gl.glVertex2f(x, y)
        _gl.glTexCoord2f(1.0, 0.0); _gl.glVertex2f(x + w, y)
        _gl.glTexCoord2f(1.0, 1.0); _gl.glVertex2f(x + w, y + h)
        _gl.glTexCoord2f(0.0, 1.0); _gl.glVertex2f(x, y + h)
        _gl.glEnd()

        _gl.glMatrixMode(_gl.GL_PROJECTION)
        _gl.glPopMatrix()
        _gl.glMatrixMode(_gl.GL_MODELVIEW)
        _gl.glPopMatrix()

        if vp is not None:
            try:
                _gl.glViewport(int(vp[0]), int(vp[1]), int(vp[2]), int(vp[3]))
            except Exception:
                pass
        _gl.glDisable(_gl.GL_TEXTURE_2D)
        _gl.glDisable(_gl.GL_BLEND)
        _gl.glDepthMask(_gl.GL_TRUE)
        _gl.glBindTexture(_gl.GL_TEXTURE_2D, 0)

    def draw_pixels(self, x, y, view_w, view_h):
        """退路：glDrawPixels 直接光栅化，完全不依赖纹理/矩阵/顶点数组。

        某些驱动/核心模式下固定管线不可用（glBegin 直接报错或不出图），
        此时用这个方式仍能把面板画出来。
        """
        if not self._data_pixels:
            return
        w, h = self.size
        if w <= 0 or h <= 0:
            return
        try:
            _gl.glUseProgram(0)
        except Exception:
            pass
        # glDrawPixels 的窗口坐标原点在左下角
        win_y = max(0, view_h - int(y) - h)
        _gl.glPixelStorei(_gl.GL_UNPACK_ALIGNMENT, 1)
        _gl.glDisable(_gl.GL_DEPTH_TEST)
        _gl.glDisable(_gl.GL_TEXTURE_2D)
        _gl.glEnable(_gl.GL_BLEND)
        _gl.glBlendFunc(_gl.GL_SRC_ALPHA, _gl.GL_ONE_MINUS_SRC_ALPHA)
        _gl.glWindowPos2i(int(x), win_y)
        _gl.glDrawPixels(w, h, _gl.GL_RGBA, _gl.GL_UNSIGNED_BYTE, self._data_pixels)
        _gl.glDisable(_gl.GL_BLEND)


class ChatPanel:
    """聊天面板 + 状态栏：线程安全地接收消息，脏标记时重建纹理。"""

    MAX_MESSAGES = 6
    PANEL_WIDTH = 480
    LINE_H = 26
    PADDING = 10
    FONT_SIZE = 18

    def __init__(self):
        self.messages = []          # (text, is_user)
        self.status = "未连接"
        self._lock = threading.Lock()
        self._chat_dirty = True
        self._status_dirty = True
        self._chat_tex = None
        self._status_tex = None
        self._use_fallback = False

        self.font = None
        self.font_path = ""
        self._diag_done = False
        # 中文字体：优先取 Windows 自带微软雅黑/黑体（缺字体会渲染成灰方块）
        for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
                     "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/Deng.ttf",
                     "C:/Windows/Fonts/simsun.ttc"):
            if not os.path.exists(path):
                continue
            try:
                self.font = pygame.font.Font(path, self.FONT_SIZE)
                self.font_path = path
                break
            except Exception:
                continue
        if self.font is None:
            self.font = pygame.font.SysFont("microsoftyahei,simhei,dengxian,arial",
                                            self.FONT_SIZE)
            self.font_path = "SysFont"
        # 自检：确认字体能画出中文字形（否则全是缺字方块）
        glyph_px = -1
        try:
            probe = self.font.render("小智", True, (255, 255, 255, 255))
            glyph_px = int(pygame.surfarray.array_alpha(probe).sum())
        except Exception:
            pass
        print(f"[Live2D] 聊天面板字体: {self.font_path}（中文字形像素 {glyph_px}）",
              flush=True)
        if glyph_px == 0:
            print("[Live2D] 警告：字体无法渲染中文，面板文字会显示为方块", flush=True)

    # ---------- 线程安全入口 ----------
    def push_message(self, text, is_user):
        with self._lock:
            self.messages.append((text, is_user))
            if len(self.messages) > self.MAX_MESSAGES:
                self.messages = self.messages[-self.MAX_MESSAGES:]
            self._chat_dirty = True

    def set_status(self, status):
        with self._lock:
            if status != self.status:
                self.status = status
                self._status_dirty = True

    # ---------- 渲染 ----------
    def draw(self, view_w, view_h):
        if self._chat_tex is None:
            self._chat_tex = TextTexture()
            self._status_tex = TextTexture()
        if self._chat_dirty:
            self._chat_tex.update(self._build_chat_surface(view_w, view_h))
            self._chat_dirty = False
        if self._status_dirty:
            self._status_tex.update(self._build_status_surface())
            self._status_dirty = False
        # 左下角聊天面板 / 顶部状态栏
        cw, ch = self._chat_tex.size
        cy = max(0, view_h - ch - 14)
        sw, sh = self._status_tex.size
        sx = max(0, (view_w - sw) // 2)

        if self._use_fallback:
            self._chat_tex.draw_pixels(14, cy, view_w, view_h)
            self._status_tex.draw_pixels(sx, 12, view_w, view_h)
        else:
            self._chat_tex.draw(14, cy, view_w, view_h)
            self._status_tex.draw(sx, 12, view_w, view_h)
        if not self._diag_done:
            self._diag_done = True
            err = _gl_error("聊天面板首帧")
            print(f"[Live2D] 叠加层就绪: 面板 {cw}x{ch}, 状态栏 {sw}x{sh}, "
                  f"OpenGL {_gl_version()}", flush=True)
            if err:
                # 固定管线不可用 -> 切到 glDrawPixels 退路重画一次
                print("[Live2D] 固定管线绘制异常，改用 glDrawPixels 渲染文字叠加层",
                      flush=True)
                self._use_fallback = True
                self._chat_tex.draw_pixels(14, cy, view_w, view_h)
                self._status_tex.draw_pixels(sx, 12, view_w, view_h)

    def _wrap(self, text, max_width):
        return _wrap_text(self.font, text, max_width)

    def _build_chat_surface(self, view_w=800, view_h=600):
        panel_w = max(240, min(self.PANEL_WIDTH, view_w - 28))
        max_text_w = panel_w - self.PADDING * 2
        with self._lock:
            messages = list(self.messages)
        # 预折行
        rendered = []  # (line, is_user)
        for text, is_user in messages:
            prefix = "[我] " if is_user else "[AI] "
            lines = self._wrap(prefix + text, max_text_w)
            for ln in lines:
                rendered.append((ln, is_user))
        # 面板高度不超过窗口一半，超出时只保留最新的行
        max_lines = max(1, int(view_h * 0.5 - self.PADDING * 2) // self.LINE_H)
        if len(rendered) > max_lines:
            rendered = rendered[-max_lines:]
        if not rendered:
            rendered = [("（等待对话…）", None)]
        panel_h = self.PADDING * 2 + max(self.LINE_H, len(rendered) * self.LINE_H)
        surface = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
        surface.fill((10, 12, 18, 170))
        y = self.PADDING
        for line, is_user in rendered:
            if is_user is None:
                color = (140, 140, 140, 200)
            elif is_user:
                color = (120, 200, 255, 235)
            else:
                color = (250, 250, 250, 235)
            surf = self.font.render(line, True, color)
            surface.blit(surf, (self.PADDING, y))
            y += self.LINE_H
        return surface

    def _build_status_surface(self):
        with self._lock:
            status = self.status
        surf = self.font.render(status, True, (255, 255, 255, 220))
        w = surf.get_width() + self.PADDING * 2
        h = self.LINE_H + self.PADDING
        surface = pygame.Surface((w, h), pygame.SRCALPHA)
        surface.fill((10, 12, 18, 140))
        surface.blit(surf, (self.PADDING, self.PADDING // 2))
        return surface


class Live2DWindow:
    def __init__(self, config, client):
        self.cfg = config
        self.client = client
        self.width = config.live2d_width
        self.height = config.live2d_height
        self.model_json = config.live2d_model_json
        self.background_path = config.live2d_background

        self.running = True
        self._mouth = 0.0
        self._motion_groups = {}
        self._version = 3
        self._pending_emotions = []
        self._emotion_lock = threading.Lock()

        # 后端聊天内容队列（其他线程 -> 渲染线程）
        self._chat_queue = queue.Queue()
        self._panel = None

        self._detect_version_and_motions()
        self._init_window()
        self._load_model()

    # ============================== 初始化 ==============================

    def _detect_version_and_motions(self):
        with open(self.model_json, encoding="utf-8") as f:
            data = json.load(f)
        model_file = str(data.get("model", ""))
        if model_file.endswith(".moc3"):
            self._version = 3
            motions = (data.get("FileReferences") or {}).get("Motions") or {}
        else:
            self._version = 2
            motions = data.get("motions") or {}
        for group, lst in motions.items():
            if isinstance(lst, list) and lst:
                self._motion_groups[group] = len(lst)

    def _init_window(self):
        pygame.init()
        pygame.display.gl_set_attribute(pygame.GL_DEPTH_SIZE, 24)
        pygame.display.gl_set_attribute(pygame.GL_DOUBLEBUFFER, 1)
        self.screen = pygame.display.set_mode(
            (self.width, self.height), DOUBLEBUF | OPENGL, vsync=1)
        pygame.display.set_caption("xiaozhi py-client · Live2D（空格=录音 ESC=退出）")

        self.l2d = importlib.import_module(
            "live2d.v3" if self._version == 3 else "live2d.v2")
        self.l2d.init()
        self.l2d.glInit()

        self.background = None
        if self.background_path and os.path.exists(self.background_path):
            from live2d.utils.image import Image
            self.background = Image(self.background_path)

        # 聊天面板（需要 PyOpenGL，缺失时仅提示不渲染文字）
        if HAS_PYOpenGL:
            pygame.font.init()
            self._panel = ChatPanel()
        else:
            print("[Live2D] 未安装 PyOpenGL，聊天内容不会渲染到窗口"
                  "（pip install PyOpenGL）", flush=True)

    def _load_model(self):
        self.model = self.l2d.Model()
        self.model.LoadModelJson(self.model_json)
        self.model.Resize(self.width, self.height)

    # ============================== 对外接口（线程安全） ==============================

    def push_chat(self, text, is_user):
        """后端聊天内容 -> 窗口面板（供 main.py 回调转发）。"""
        if self._panel is not None:
            self._chat_queue.put((text, is_user))

    def trigger_emotion(self, emotion):
        """情绪事件入队，主循环里消费。"""
        with self._emotion_lock:
            self._pending_emotions.append(emotion)

    # ============================== 主循环 ==============================

    def run(self):
        clock = pygame.time.Clock()
        while self.running:
            for event in pygame.event.get():
                if event.type == QUIT:
                    self.running = False
                elif event.type == KEYDOWN:
                    if event.key == K_ESCAPE:
                        self.running = False
                    elif event.key == K_SPACE:
                        if self.client.is_recording():
                            self.client.stop_recording_threadsafe()
                        else:
                            self.client.start_recording_threadsafe()
                elif event.type == MOUSEMOTION:
                    self.model.Drag(*pygame.mouse.get_pos())

            self._drain_chat_queue()
            self._update_status_line()
            self._consume_pending_emotions()
            self._update_mouth()

            self.l2d.clearBuffer()
            if self.background:
                self.background.Draw()
            self.model.Update()
            self.model.Draw()

            # 叠加聊天面板与状态栏（最后绘制，保证在最上层）
            if self._panel is not None:
                try:
                    self._panel.draw(self.width, self.height)
                except Exception as e:
                    # GL 状态异常时降级：关闭文字叠加，不影响 Live2D 渲染
                    print(f"[Live2D] 聊天面板绘制失败，已停用文字叠加: {e}", flush=True)
                    self._panel = None

            pygame.display.flip()
            clock.tick(60)

        self._close()

    # ============================== 动画与叠加层 ==============================

    def _drain_chat_queue(self):
        if self._panel is None:
            return
        while True:
            try:
                text, is_user = self._chat_queue.get_nowait()
            except queue.Empty:
                break
            self._panel.push_message(text, is_user)

    def _update_status_line(self):
        if self._panel is None:
            return
        if not self.client.connected:
            status = "未连接（终端输入 /connect，或检查 config.json 的 auto_connect）"
        elif self.client.is_recording():
            status = "录音中…（按空格结束）"
        elif self.client.is_remote_speaking:
            status = "对方说话中…（按空格或发文本可打断）"
        else:
            status = "已连接 · 聆听中（输入 /rec 或按空格说话）"
        self._panel.set_status(status)

    def _update_mouth(self):
        """播放音量 -> 口型开合。"""
        level = self.client.player.level
        target = min(1.0, level * 3.0)
        self._mouth += (target - self._mouth) * 0.35
        value = round(self._mouth, 3)
        setter = (
            getattr(self.model, "SetParameterValue", None)
            or getattr(self.model, "SetParamValue", None)
        )
        if setter is None:
            return
        params = ("ParamMouthOpenY",) if self._version == 3 else \
                 ("PARAM_MOUTH_OPEN_Y", "PARAM_MOUTH_OPEN")
        for name in params:
            try:
                setter(name, value)
                break
            except Exception:
                continue

    # ============================== 情绪/动作 ==============================

    def _consume_pending_emotions(self):
        with self._emotion_lock:
            emotions = self._pending_emotions
            self._pending_emotions = []
        for emotion in emotions:
            self._start_random_motion(emotion)

    def _start_random_motion(self, emotion):
        if not self._motion_groups:
            return
        group = random.choice(list(self._motion_groups))
        index = random.randrange(self._motion_groups[group])
        try:
            self.model.StartMotion(group, index, 3)
            print(f"[Live2D] 情绪 {emotion} -> 动作 {group}[{index}]", flush=True)
        except TypeError:
            try:
                self.model.StartMotion(group, index)
            except Exception:
                pass
        except Exception:
            pass

    # ============================== 收尾 ==============================

    def _close(self):
        try:
            self.l2d.dispose()
        except Exception:
            pass
        pygame.quit()
