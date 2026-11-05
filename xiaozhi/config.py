# -*- coding: utf-8 -*-
"""配置管理：对应 web 端 js/config/manager.js

- 从 py-client/config.json 读取配置
- device_id（设备MAC）为空时自动生成随机 MAC 并回写
- 相对路径以仓库根目录（cs_live2d）为基准解析
"""
import json
import os
import random

CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(CLIENT_DIR)
CONFIG_FILE = os.path.join(CLIENT_DIR, "config.json")

DEFAULTS = {
    "ota_url": "http://127.0.0.1:8003/xiaozhi/ota/",
    "device_id": "",
    "client_id": "py_test_client",
    "device_name": "Py测试设备",
    "auto_connect": True,
    "audio": {
        "sample_rate": 16000,
        "opus_bitrate": 16000,
    },
    "live2d": {
        "enabled": True,
        "model_json": "live2d/model/bronya/model.json",
        "background": "1.png",
        "width": 800,
        "height": 600,
    },
}


def _generate_random_mac():
    hex_digits = "0123456789ABCDEF"
    return ":".join(
        "".join(random.choice(hex_digits) for _ in range(2)) for _ in range(6)
    )


def _resolve(path):
    """相对路径基于仓库根目录解析。"""
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(REPO_ROOT, path))


class Config:
    def __init__(self, data):
        self.ota_url = data.get("ota_url", DEFAULTS["ota_url"]).strip()
        self.device_id = data.get("device_id", "").strip()
        self.client_id = data.get("client_id", DEFAULTS["client_id"]).strip()
        self.device_name = data.get("device_name", DEFAULTS["device_name"]).strip()
        self.auto_connect = bool(data.get("auto_connect", True))

        audio = data.get("audio", {})
        self.sample_rate = int(audio.get("sample_rate", 16000))
        self.opus_bitrate = int(audio.get("opus_bitrate", 16000))

        self.mcp_tools_file = os.path.join(CLIENT_DIR, "config", "default-mcp-tools.json")

        live2d = data.get("live2d", {})
        self.live2d_enabled = bool(live2d.get("enabled", True))
        self.live2d_model_json = _resolve(live2d.get("model_json", ""))
        self.live2d_background = _resolve(live2d.get("background", ""))
        self.live2d_width = int(live2d.get("width", 800))
        self.live2d_height = int(live2d.get("height", 600))

    # 语义别名，与 web 端保持一致：deviceId 即设备 MAC
    @property
    def device_mac(self):
        return self.device_id


def load_config(overrides=None):
    """加载配置，overrides 为命令行参数覆盖项。"""
    data = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            data.update(loaded)
        except Exception as e:
            print(f"[配置] 读取 config.json 失败，使用默认配置: {e}")

    # device_id 为空时生成随机 MAC 并回写，保证下次启动使用同一身份
    if not data.get("device_id"):
        data["device_id"] = _generate_random_mac()
        try:
            merged = json.load(open(CONFIG_FILE, encoding="utf-8")) if os.path.exists(CONFIG_FILE) else {}
            merged["device_id"] = data["device_id"]
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=4)
            print(f"[配置] 已生成设备MAC并写入 config.json: {data['device_id']}")
        except Exception as e:
            print(f"[配置] 回写 config.json 失败: {e}")

    if overrides:
        if overrides.get("ota_url"):
            data["ota_url"] = overrides["ota_url"]
        if overrides.get("device_id"):
            data["device_id"] = overrides["device_id"]
        if overrides.get("client_id"):
            data["client_id"] = overrides["client_id"]
        if overrides.get("no_live2d"):
            data.setdefault("live2d", {})["enabled"] = False

    return Config(data)
