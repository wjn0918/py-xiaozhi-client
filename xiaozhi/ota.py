# -*- coding: utf-8 -*-
"""OTA 连接模块：对应 web 端 js/core/network/ota-connector.js

流程：
1. POST 设备信息到 OTA 服务器，换取 websocket 地址与 token
2. 拼接 websocket URL（authorization / device-id / client-id 参数）
"""
import requests
from urllib.parse import urlencode, urlparse


def ota_request(ota_url, device_id, client_id, device_name):
    """发送 OTA 请求，返回服务器响应 JSON。"""
    payload = {
        "version": 0,
        "uuid": "",
        "application": {
            "name": "xiaozhi-py-client",
            "version": "1.0.0",
            "compile_time": "2025-04-16 10:00:00",
            "idf_version": "4.4.3",
            "elf_sha256": "1234567890abcdef1234567890abcdef1234567890abcdef",
        },
        "ota": {"label": "xiaozhi-py-client"},
        "board": {
            "type": device_name,
            "ssid": "xiaozhi-py-client",
            "rssi": 0,
            "channel": 0,
            "ip": "192.168.1.1",
            "mac": device_id,
        },
        "flash_size": 0,
        "minimum_free_heap_size": 0,
        "mac_address": device_id,
        "chip_model_name": "",
        "chip_info": {"model": 0, "cores": 0, "revision": 0, "features": 0},
        "partition_table": [{"label": "", "type": 0, "subtype": 0, "address": 0, "size": 0}],
    }
    headers = {
        "Content-Type": "application/json",
        "Device-Id": device_id,
        "Client-Id": client_id,
    }
    resp = requests.post(ota_url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def build_ws_url(ota_result, device_id, client_id):
    """从 OTA 响应中提取 websocket 地址并拼接认证参数。"""
    ws_info = ota_result.get("websocket") or {}
    url = ws_info.get("url")
    if not url:
        raise RuntimeError("OTA响应中缺少websocket信息")

    parts = urlparse(url)
    params = []
    if parts.query:
        params.append(parts.query)

    token = ws_info.get("token")
    if token:
        if not str(token).startswith("Bearer "):
            token = "Bearer " + str(token)
        params.append("authorization=" + urlencode({"": token})[1:])

    params.append("device-id=" + urlencode({"": device_id})[1:])
    params.append("client-id=" + urlencode({"": client_id})[1:])

    return f"{parts.scheme}://{parts.netloc}{parts.path}?" + "&".join(params)
