# xiaozhi py-client

`test/` 目录下 web 客户端（对接 xiaozhi-esp32-server）的 **Python 实现**。
协议行为与 web 端完全一致，可直接对接同一个 xiaozhi-esp32-server。

## 功能对照

| Web 端模块 | Python 实现 |
| --- | --- |
| `js/core/network/ota-connector.js` | `xiaozhi/ota.py`（OTA 请求 + WS 地址拼接） |
| `js/core/network/websocket.js` | `xiaozhi/ws_client.py`（hello 握手、消息分发、打断/状态消息） |
| `js/core/audio/opus-codec.js`（libopus.js） | `xiaozhi/opus_codec.py`（PyAV 实现的 Opus 编解码） |
| `js/core/audio/recorder.js` | `xiaozhi/audio_recorder.py`（麦克风 16kHz → 960样本帧 → Opus 上行） |
| `js/core/audio/player.js` | `xiaozhi/audio_player.py`（Opus 下行 → 播放，输出音量电平） |
| `js/core/mcp/tools.js` | `xiaozhi/mcp_tools.py`（MCP initialize / tools/list / tools/call） |
| Live2D 渲染（PIXI + Cubism） | `xiaozhi/live2d_window.py`（pygame + live2d-py，支持 v2/v3 模型） |

## 协议要点（与 web 端一致）

1. **OTA**：`POST {ota_url}`，携带 Device-Id/Client-Id 头与设备信息体，响应中取 `websocket.url` 与 `websocket.token`
2. **握手**：WS 连接后发送 `hello`（含 features.mcp），等待服务器 `hello`（含 session_id）
3. **语音上行**：16kHz/16bit/单声道 → 960 样本帧 → Opus（16kbps VOIP）→ 二进制帧；**发送空二进制帧表示录音结束**
4. **语音下行**：二进制 Opus 包 → 解码播放；`tts stop` 时清空缓冲，1 秒后停止说话动画并回发 `{"type":"state","state":"speaking_end"}`
5. **文本对话**：`{"type":"listen","state":"detect","text":...}`；对方说话时先发 `abort`
6. **MCP**：自动响应 `initialize`、`tools/list`、`tools/call`（默认为模拟执行，`mockResponse` 支持 `${param}` 替换）

## 安装

```bash
cd py-client
pip install -r requirements.txt
```

> Opus 编解码通过 PyAV（自带 FFmpeg/libopus）实现，Windows 下无需手动安装 DLL。
> Live2D 窗口的聊天面板依赖 PyOpenGL 渲染文本纹理，未安装时窗口正常工作、仅不显示文字。

## 运行

```bash
python main.py                # Live2D 窗口模式（需 live2d-py，模型默认 live2d/model/bronya）
python main.py --no-live2d    # 纯终端模式
python main.py --ota http://127.0.0.1:8003/xiaozhi/ota/
```

### 终端命令

| 命令 | 作用 |
| --- | --- |
| `/connect` | 连接服务器（默认自动连接） |
| `/disconnect` | 断开连接 |
| `/rec` 或 `/r` | 开始录音 |
| `/stop` 或 `/s` | 结束录音（发送空帧结束信号） |
| 其他文本 | 直接作为对话内容发送 |
| `/q` | 退出 |

### Live2D 窗口操作

- **空格键**：开始/停止录音
- **鼠标移动**：模型视线跟随
- **ESC**：退出
- **聊天面板**：后端返回内容（STT 识别结果、LLM 回复、TTS 语音字幕）实时渲染在窗口左下角，顶部显示连接/录音/说话状态
- **口型同步**：播放 TTS 时根据音频音量驱动模型张嘴，收到情绪表情时随机触发动作

## 配置（config.json）

```json
{
    "ota_url": "http://127.0.0.1:8003/xiaozhi/ota/",
    "device_id": "",                 // 设备MAC，留空首次启动自动生成并回写
    "client_id": "py_test_client",
    "device_name": "Py测试设备",
    "auto_connect": true,
    "audio": { "sample_rate": 16000, "opus_bitrate": 16000 },
    "live2d": {
        "enabled": true,
        "model_json": "live2d/model/bronya/model.json",   // 相对路径基于仓库根目录
        "background": "1.png",
        "width": 800,
        "height": 600
    }
}
```

- MCP 工具列表在 `py-client/config/default-mcp-tools.json`（与 web 端同一套默认工具）
- 也可以改用 `test/resources/hiyori_pro_zh/runtime/hiyori_pro_t11.model3.json` 等 Cubism 4 模型（自动识别 v3）

## 备注

- 服务器下发 `llm` 消息中的表情/情绪会触发 Live2D 随机动作
- 拍照工具 `self_camera_take_photo` 在 Python 端为模拟返回（web 端依赖浏览器摄像头）
- 视觉分析配置（`capabilities.vision`）会在 MCP initialize 时自动保存到内存
