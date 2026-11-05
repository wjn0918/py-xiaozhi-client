# -*- coding: utf-8 -*-
"""xiaozhi-esp32-server Python 客户端入口（web 端 test_page.html 的 Python 实现）

用法：
    python main.py                 # 默认模式（config.json 中 live2d.enabled=true 时带 Live2D 窗口）
    python main.py --no-live2d     # 纯终端模式
    python main.py --ota http://127.0.0.1:8003/xiaozhi/ota/

终端命令：
    /connect    连接服务器        /disconnect 断开连接
    /rec        开始录音          /stop       结束录音
    /help       帮助              /q          退出
    其他文本     作为对话内容发送
"""
import argparse
import asyncio
import os
import sys
import threading

# 保证 py-client 目录可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xiaozhi.config import load_config
from xiaozhi.ws_client import XiaozhiClient

HELP = """
命令说明:
  /connect      连接服务器
  /disconnect   断开连接
  /rec  (/r)    开始录音（再输一次 /stop 结束）
  /stop (/s)    结束录音
  /help         显示帮助
  /q            退出程序
  其他文本        作为对话内容直接发送
"""


def terminal_input_loop(client: XiaozhiClient):
    """终端输入线程：处理文本与命令。"""
    while True:
        try:
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            client.stop()
            return
        if not line:
            continue
        if line == "/q":
            client.stop()
            return
        elif line == "/connect":
            if client.loop:
                asyncio.run_coroutine_threadsafe(client.connect(), client.loop)
        elif line == "/disconnect":
            if client.loop:
                asyncio.run_coroutine_threadsafe(client.disconnect(), client.loop)
        elif line in ("/rec", "/r"):
            client.start_recording_threadsafe()
        elif line in ("/stop", "/s"):
            client.stop_recording_threadsafe()
        elif line == "/help":
            print(HELP)
        else:
            client.send_text_threadsafe(line)


def main():
    parser = argparse.ArgumentParser(description="xiaozhi-esp32-server Python 客户端")
    parser.add_argument("--ota", dest="ota_url", help="OTA服务器地址")
    parser.add_argument("--device-id", dest="device_id", help="设备MAC（device-id）")
    parser.add_argument("--client-id", dest="client_id", help="客户端ID")
    parser.add_argument("--no-live2d", action="store_true", help="禁用Live2D窗口，使用纯终端模式")
    parser.add_argument("--no-auto-connect", action="store_true", help="启动后不自动连接")
    args = parser.parse_args()

    config = load_config({
        "ota_url": args.ota_url,
        "device_id": args.device_id,
        "client_id": args.client_id,
        "no_live2d": args.no_live2d,
    })
    if args.no_auto_connect:
        config.auto_connect = False

    print("=" * 60)
    print("xiaozhi py-client")
    print(f"  OTA地址 : {config.ota_url}")
    print(f"  设备MAC : {config.device_id}")
    print(HELP.rstrip())
    print("=" * 60)

    client = XiaozhiClient(config)

    # 客户端在后台线程跑 asyncio 事件循环
    client_thread = threading.Thread(
        target=lambda: asyncio.run(client.run()), daemon=True)
    client_thread.start()

    # 终端输入线程
    threading.Thread(
        target=terminal_input_loop, args=(client,), daemon=True).start()

    use_live2d = config.live2d_enabled and os.path.exists(config.live2d_model_json)
    if config.live2d_enabled and not use_live2d:
        print(f"[Live2D] 模型文件不存在: {config.live2d_model_json}，使用纯终端模式")

    if use_live2d:
        try:
            from xiaozhi.live2d_window import Live2DWindow

            window = Live2DWindow(config, client)
            # 情绪事件转发给 Live2D 窗口触发动作
            original_emotion = client.ui.on_emotion

            def on_emotion(emotion):
                original_emotion(emotion)
                window.trigger_emotion(emotion)

            client.ui.on_emotion = on_emotion

            # 后端聊天内容（STT/LLM/TTS 文本）转发到 Live2D 窗口面板
            original_chat = client.ui.on_chat

            def on_chat(text, is_user):
                original_chat(text, is_user)
                window.push_chat(text, is_user)

            client.ui.on_chat = on_chat

            # pygame 必须在主线程渲染
            window.run()
            client.stop()
            client_thread.join(timeout=5)
            client.player.close()
        except Exception as e:
            print(f"[Live2D] 窗口初始化失败: {e}\n[Live2D] 回退到纯终端模式，按 Ctrl+C 退出")
            try:
                client_thread.join()
            except KeyboardInterrupt:
                client.stop()
    else:
        try:
            client_thread.join()
        except KeyboardInterrupt:
            client.stop()
            client_thread.join(timeout=5)
        client.player.close()

    print("已退出")


if __name__ == "__main__":
    main()
