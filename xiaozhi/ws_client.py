# -*- coding: utf-8 -*-
"""xiaozhi-esp32-server WebSocket 客户端核心：对应 web 端 js/core/network/websocket.js

职责：
- OTA 换取 WS 地址 -> 建立 WebSocket 连接 -> hello 握手
- 处理下行文本消息：hello / tts / stt / llm / mcp / audio
- 处理下行二进制消息：opus 音频包 -> 播放器
- 上行：文本对话（listen detect）、录音（opus 二进制帧）、打断（abort）、状态（state）
"""
import asyncio
import json
import re

import websockets

from .audio_player import AudioPlayer
from .audio_recorder import AudioRecorder
from .mcp_tools import McpTools
from .ota import build_ws_url, ota_request

# 表情符号（与 web 端正则一致）
EMOJI_RE = re.compile("[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF]")


class UiCallbacks:
    """与界面解耦的回调集合。默认实现为终端打印，可被 Live2D 窗口等替换。"""

    def __init__(self):
        self.log = print
        self.on_connection = lambda connected: print(
            f"[状态] {'已连接' if connected else '已断开'}", flush=True)
        self.on_chat = lambda text, is_user: print(
            ("[我] " if is_user else "[AI] ") + text, flush=True)
        self.on_talking = lambda talking: None
        self.on_speaking = lambda speaking: print(
            f"[状态] {'对方说话中' if speaking else '聆听中'}", flush=True)
        self.on_emotion = lambda emotion: print(f"[情绪] {emotion}", flush=True)


class XiaozhiClient:
    def __init__(self, config, ui: UiCallbacks = None):
        self.config = config
        self.ui = ui or UiCallbacks()

        self.loop = None
        self.ws = None
        self.session_id = None
        self.connected = False
        self.is_remote_speaking = False

        self._stop_event = None
        self._recv_task = None
        self._hello_future = None

        self.player = AudioPlayer(config.sample_rate)
        self.recorder = None
        self.mcp = McpTools(config.mcp_tools_file)

    # ============================== 生命周期 ==============================

    async def run(self):
        """后台线程入口：自动连接后常驻，直到 stop() 被调用。"""
        self.loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        if self.config.auto_connect:
            try:
                await self.connect()
            except Exception as e:
                self.ui.log(f"[连接失败] {e}")

        await self._stop_event.wait()
        await self.disconnect()

    def stop(self):
        """线程安全停止客户端。"""
        if self.loop and self._stop_event:
            self.loop.call_soon_threadsafe(self._stop_event.set)

    async def connect(self):
        if self.connected:
            return True
        cfg = self.config

        self.ui.log(f"[OTA] 请求 {cfg.ota_url} ...")
        info = await asyncio.to_thread(
            ota_request, cfg.ota_url, cfg.device_id, cfg.client_id, cfg.device_name)
        url = build_ws_url(info, cfg.device_id, cfg.client_id)

        self.ui.log(f"[WS] 正在连接: {url}")
        # 先直连（绕过系统代理，适配局域网部署的 xiaozhi 服务器），
        # 失败则回退到跟随系统代理环境变量
        try:
            self.ws = await websockets.connect(url, max_size=None, ping_interval=20, proxy=None)
        except TypeError:
            # 旧版本 websockets 不支持 proxy 参数
            self.ws = await websockets.connect(url, max_size=None, ping_interval=20)
        except (OSError, websockets.InvalidURI, websockets.InvalidHandshake):
            try:
                self.ws = await websockets.connect(url, max_size=None, ping_interval=20)
            except TypeError:
                self.ws = await websockets.connect(url, max_size=None, ping_interval=20)
        self.connected = True
        self.ui.on_connection(True)

        # hello 握手（对应 web 端 sendHelloMessage）
        self._hello_future = self.loop.create_future()
        await self._send_json({
            "type": "hello",
            "device_id": cfg.device_id,
            "device_name": cfg.device_name,
            "device_mac": cfg.device_mac,
            "features": {"mcp": True},
        })
        self.ui.log("[WS] 已发送 hello 握手消息")

        self._recv_task = asyncio.create_task(self._recv_loop())
        try:
            await asyncio.wait_for(self._hello_future, timeout=5)
        except asyncio.TimeoutError:
            self.ui.log("[WS] 等待 hello 响应超时")
        return True

    async def disconnect(self):
        if self.recorder and self.recorder.recording:
            self.recorder.stop()
        self.connected = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        self.player.clear()
        self.ui.on_talking(False)

    # ============================== 接收 ==============================

    async def _recv_loop(self):
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    try:
                        await self.handle_text(json.loads(message))
                    except Exception as e:
                        self.ui.log(f"[消息处理错误] {e}")
                else:
                    self.player.enqueue(message)
        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.ui.log(f"[WS错误] {e}")
        finally:
            if self.connected or self.ws:
                self.connected = False
                self.ws = None
                self.ui.on_connection(False)
                self.ui.log("[WS] 已断开连接")

    async def handle_text(self, msg: dict):
        mtype = msg.get("type")

        if mtype == "hello":
            self.session_id = msg.get("session_id")
            self.ui.log(f"[WS] 服务器握手成功，会话ID: {self.session_id}")
            if self._hello_future and not self._hello_future.done():
                self._hello_future.set_result(True)

        elif mtype == "tts":
            await self._handle_tts(msg)

        elif mtype == "stt":
            text = msg.get("text", "")
            self.ui.log(f"[STT] 识别结果: {text}")
            if text:
                self.ui.on_chat(text, True)

        elif mtype == "llm":
            text = msg.get("text", "")
            self.ui.log(f"[LLM] 大模型回复: {text}")
            if text:
                self.ui.on_chat(text, False)
                m = EMOJI_RE.search(text)
                emotion = msg.get("emotion") or (m.group() if m else None)
                if emotion:
                    self.ui.on_emotion(emotion)

        elif mtype == "mcp":
            await self.mcp.handle(msg, self._send_json)

        elif mtype == "audio":
            self.ui.log(f"[音频控制] {json.dumps(msg, ensure_ascii=False)}")

        else:
            self.ui.log(f"[未知消息] {mtype}: {json.dumps(msg, ensure_ascii=False)[:200]}")

    async def _handle_tts(self, msg):
        state = msg.get("state")
        if state == "start":
            self.ui.log("[TTS] 服务器开始发送语音")
            self.is_remote_speaking = True
            self.ui.on_speaking(True)
            self.ui.on_talking(True)
        elif state == "sentence_start":
            text = msg.get("text") or ""
            if text:
                self.ui.on_chat(text, False)
        elif state == "sentence_end":
            pass  # 句子结束不清除动画，与 web 端一致
        elif state == "stop":
            self.ui.log("[TTS] 服务器语音传输结束，清空音频缓冲")
            self.player.clear()
            self.is_remote_speaking = False
            self.ui.on_speaking(False)
            # 延迟 1s 停止说话动画并发送 speaking_end（与 web 端一致）
            asyncio.create_task(self._delayed_stop_talking())

    async def _delayed_stop_talking(self):
        await asyncio.sleep(1.0)
        self.ui.on_talking(False)
        await self._send_json({"type": "state", "state": "speaking_end", "text": "结束说话"})

    # ============================== 发送 ==============================

    async def _send_json(self, obj):
        if self.ws:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))

    async def _send_bytes(self, data: bytes):
        if self.ws:
            await self.ws.send(data)

    def _send_threadsafe_json(self, obj):
        if self.loop and self.connected:
            asyncio.run_coroutine_threadsafe(self._send_json(obj), self.loop)

    def _send_threadsafe_bytes(self, data: bytes):
        if self.loop and self.connected:
            asyncio.run_coroutine_threadsafe(self._send_bytes(data), self.loop)

    # ---------- 文本对话 ----------
    def send_text_threadsafe(self, text):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.send_text(text), self.loop)

    async def send_text(self, text):
        if not text or not self.connected:
            return False
        # 对方说话中先发送打断消息
        if self.is_remote_speaking and self.session_id:
            await self._send_json({
                "session_id": self.session_id,
                "type": "abort",
                "reason": "wake_word_detected",
            })
            self.player.clear()
            self.is_remote_speaking = False
            self.ui.log("[WS] 已发送打断消息")
        await self._send_json({"type": "listen", "state": "detect", "text": text})
        self.ui.log(f"[WS] 已发送文本消息: {text}")
        self.ui.on_chat(text, True)
        return True

    # ---------- 录音 ----------
    def start_recording_threadsafe(self):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.start_recording(), self.loop)

    def stop_recording_threadsafe(self):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.stop_recording(), self.loop)

    async def start_recording(self):
        if not self.connected:
            self.ui.log("[录音] 尚未连接服务器，无法录音")
            return
        if self.is_remote_speaking and self.session_id:
            await self._send_json({
                "session_id": self.session_id,
                "type": "abort",
                "reason": "wake_word_detected",
            })
            self.player.clear()
            self.is_remote_speaking = False
            self.ui.on_speaking(False)
            self.ui.log("[录音] 已发送中止消息")
        if not self.recorder:
            self.recorder = AudioRecorder(
                self._send_threadsafe_bytes,
                self.config.sample_rate,
                self.config.opus_bitrate)
        if not self.recorder.recording:
            self.recorder.start()
            self.ui.log("[录音] 已开始录音（说完后执行停止）")
        self.ui.on_talking(False)

    async def stop_recording(self):
        if self.recorder and self.recorder.recording:
            self.recorder.stop()
            self.ui.log("[录音] 已停止录音")

    def is_recording(self):
        return bool(self.recorder and self.recorder.recording)
