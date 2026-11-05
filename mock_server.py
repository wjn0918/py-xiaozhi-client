# -*- coding: utf-8 -*-
"""本地模拟 xiaozhi-esp32-server，用于无真实服务器时联调 py-client。

用法：
    python mock_server.py            # OTA: 8003  WS: 10001
    python mock_server.py --port 10001 --ota-port 8003

行为：
- OTA 接口返回 websocket 地址与 token
- 收到 hello 后回复带 session_id 的 hello
- 模拟一轮 TTS：tts start -> sentence_start -> 3 秒 opus 音频包 -> tts stop
- 收到 listen detect 文本：回复 stt + llm（回显）
- 响应 MCP initialize / tools/list / tools/call
- 收到二进制（录音）：计数，收到空帧（结束信号）时打印
"""
import argparse
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import websockets

from xiaozhi.opus_codec import OpusEncoder

SESSION_ID = "mock-session-0001"


# ---------------- OTA HTTP ----------------

class OtaHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        print(f"[OTA] 收到请求 Device-Id={self.headers.get('Device-Id')}", flush=True)
        resp = {
            "websocket": {
                "url": f"ws://127.0.0.1:{MOCK_WS_PORT}/xiaozhi/v1/",
                "token": "mock-token-123",
            },
            "firmware": {"version": "1.0.0", "url": ""},
            "server_time": {"timestamp": 0, "timezone": "Asia/Shanghai"},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


MOCK_WS_PORT = 10001


# ---------------- WebSocket ----------------

async def make_tts_audio(opus_encoder):
    """生成 3 秒 440Hz 正弦的 opus 包序列。"""
    pcm = (np.sin(np.linspace(0, 440 * 2 * np.pi, 16000 * 3)) * 6000).astype(np.int16)
    packets = []
    for i in range(0, len(pcm), 960):
        packets.extend(opus_encoder.encode(pcm[i:i + 960]))
    return packets


async def handle_ws(ws):
    peer = ws.remote_address
    print(f"[WS] 客户端接入: {peer}", flush=True)
    opus_encoder = OpusEncoder(16000, 16000)
    hello_sent = False
    recv_packets = 0

    try:
        async for message in ws:
            if isinstance(message, str):
                msg = json.loads(message)
                mtype = msg.get("type")
                print(f"[WS] 收到文本: {json.dumps(msg, ensure_ascii=False)[:150]}", flush=True)

                if mtype == "hello" and not hello_sent:
                    hello_sent = True
                    await ws.send(json.dumps({
                        "type": "hello",
                        "version": 1,
                        "session_id": SESSION_ID,
                        "transport": "websocket",
                        "audio_params": {
                            "format": "opus", "sample_rate": 16000,
                            "channels": 1, "frame_duration": 60,
                        },
                    }))
                    # 模拟 MCP initialize
                    await ws.send(json.dumps({
                        "session_id": SESSION_ID, "type": "mcp",
                        "payload": {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"capabilities": {"vision": {
                                        "url": "http://127.0.0.1:8003/vision",
                                        "token": "mock-vision-token"}}}},
                    }))
                    # 模拟 MCP tools/list
                    await ws.send(json.dumps({
                        "session_id": SESSION_ID, "type": "mcp",
                        "payload": {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    }))

                elif mtype == "listen" and msg.get("state") == "detect":
                    text = msg.get("text", "")
                    await ws.send(json.dumps({"session_id": SESSION_ID, "type": "stt", "text": text}))
                    await ws.send(json.dumps({
                        "session_id": SESSION_ID, "type": "llm",
                        "text": f"你说的是：{text} 😊", "emotion": "happy",
                    }))
                    # 模拟一轮 TTS 语音回复
                    await ws.send(json.dumps({"session_id": SESSION_ID, "type": "tts",
                                              "state": "start"}))
                    await ws.send(json.dumps({"session_id": SESSION_ID, "type": "tts",
                                              "state": "sentence_start",
                                              "text": f"你说的是：{text}，这是模拟语音回复。"}))
                    for i, pkt in enumerate(await make_tts_audio(opus_encoder)):
                        await ws.send(pkt)
                        if i % 25 == 0:
                            await asyncio.sleep(0.02)  # 模拟实时速率
                    await ws.send(json.dumps({"session_id": SESSION_ID, "type": "tts",
                                              "state": "stop"}))

                elif mtype == "abort":
                    print("[WS] 收到打断消息", flush=True)

                elif mtype == "mcp":
                    payload = msg.get("payload") or {}
                    print(f"[WS] 客户端MCP响应: {payload.get('id')}", flush=True)

            else:
                if len(message) == 0:
                    print(f"[WS] 录音结束信号，共收到 {recv_packets} 个音频包", flush=True)
                else:
                    recv_packets += 1
    except websockets.ConnectionClosed:
        pass
    print(f"[WS] 客户端断开: {peer}", flush=True)


async def main(ws_port, ota_port):
    global MOCK_WS_PORT
    MOCK_WS_PORT = ws_port

    httpd = ThreadingHTTPServer(("0.0.0.0", ota_port), OtaHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[OTA] 模拟OTA服务已启动: http://127.0.0.1:{ota_port}/xiaozhi/ota/", flush=True)

    async with websockets.serve(handle_ws, "0.0.0.0", ws_port, max_size=None):
        print(f"[WS] 模拟WebSocket服务已启动: ws://127.0.0.1:{ws_port}/xiaozhi/v1/", flush=True)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=10001, help="WebSocket端口")
    parser.add_argument("--ota-port", type=int, default=8003, help="OTA HTTP端口")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.port, args.ota_port))
    except KeyboardInterrupt:
        print("模拟服务器已退出")
