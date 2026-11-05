# -*- coding: utf-8 -*-
"""MCP 工具模块：对应 web 端 js/core/mcp/tools.js

- 回复服务器的 initialize / tools/list / tools/call 请求
- 工具为模拟执行（mockResponse），支持 ${param} 模板变量替换
"""
import json

DEFAULT_SERVER_INFO = {"name": "xiaozhi-py-client", "version": "1.0.0"}


class McpTools:
    def __init__(self, tools_file):
        self.tools_file = tools_file
        self.tools = []
        self.vision_config = None  # initialize 时服务器下发的视觉分析配置
        self.load()

    def load(self):
        try:
            with open(self.tools_file, encoding="utf-8") as f:
                self.tools = json.load(f)
            print(f"[MCP] 已加载 {len(self.tools)} 个工具", flush=True)
        except Exception as e:
            print(f"[MCP] 加载工具失败({e})，工具列表为空", flush=True)
            self.tools = []

    def schema_tools(self):
        """返回给服务器 tools/list 的工具描述。"""
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get(
                    "inputSchema", {"type": "object", "properties": {}}),
            }
            for t in self.tools
        ]

    async def handle(self, msg: dict, send_json):
        """处理服务器下发的 mcp 消息并回复。send_json 为协程函数。"""
        payload = msg.get("payload") or {}
        session_id = msg.get("session_id") or ""
        method = payload.get("method")
        rid = payload.get("id")

        if method == "initialize":
            params = payload.get("params") or {}
            vision = (params.get("capabilities") or {}).get("vision")
            if isinstance(vision, dict) and vision.get("url") and vision.get("token"):
                self.vision_config = vision
                print(f"[MCP] 视觉分析接口: {vision['url']}", flush=True)
            else:
                self.vision_config = None
            await send_json({
                "session_id": session_id,
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": DEFAULT_SERVER_INFO,
                    },
                },
            })
            print("[MCP] 已回复初始化响应", flush=True)

        elif method == "tools/list":
            tools = self.schema_tools()
            await send_json({
                "session_id": session_id,
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"tools": tools},
                },
            })
            print(f"[MCP] 已回复工具列表: {len(tools)} 个工具", flush=True)

        elif method == "tools/call":
            params = payload.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            print(f"[MCP] 调用工具: {name} 参数: {json.dumps(args, ensure_ascii=False)}", flush=True)
            try:
                result = self.execute(name, args)
                await send_json({
                    "session_id": session_id,
                    "type": "mcp",
                    "payload": {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [
                                {"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False)}
                            ],
                            "isError": False,
                        },
                    },
                })
                print(f"[MCP] 工具 {name} 执行完成", flush=True)
            except Exception as e:
                await send_json({
                    "session_id": session_id,
                    "type": "mcp",
                    "payload": {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32603, "message": str(e)},
                    },
                })
                print(f"[MCP] 工具执行失败: {e}", flush=True)

        else:
            print(f"[MCP] 未知的MCP方法: {method}", flush=True)

    def execute(self, name, args):
        """执行工具调用（模拟），mockResponse 支持 ${param} 模板替换。"""
        tool = next((t for t in self.tools if t["name"] == name), None)
        if not tool:
            return {"success": False, "error": f"未知工具: {name}"}

        mock = tool.get("mockResponse")
        if mock:
            s = json.dumps(mock, ensure_ascii=False)
            for k, v in (args or {}).items():
                s = s.replace("${%s}" % k, str(v))
            try:
                return json.loads(s)
            except Exception:
                return mock

        return {
            "success": True,
            "message": f"工具 {name} 执行成功",
            "tool": name,
            "arguments": args,
        }
