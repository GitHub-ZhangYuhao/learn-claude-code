#!/usr/bin/env python3
# Harness: integration -- 工具不仅仅在你的代码中。
"""
s19_mcp_plugin_CN.py - MCP 与插件系统

本教学章节专注于最小的有用理念：
外部进程可以暴露工具，而你的 Agent 只需少量规范化处理，
就能将它们当作普通工具来使用。

最小路径：
  1. 启动一个 MCP 服务器进程
  2. 询问它有哪些工具
  3. 添加前缀并注册这些工具
  4. 将匹配调用路由到该服务器

插件只多了一层：发现。一个小型清单（manifest）告诉 Agent
要启动哪个外部服务器。

核心洞察："外部工具应该进入同一个工具流水线，而不是形成一个完全独立的世界。"
实际上这意味着共享权限检查和标准化的 tool_result 载荷。

请按照以下顺序阅读本文件：
1. CapabilityPermissionGate：外部工具仍然需要通过相同的控制门。
2. MCPClient：一个服务器连接如何暴露工具规格和工具调用。
3. PluginLoader：清单如何声明外部服务器。
4. MCPToolRouter / build_tool_pool：原生工具和外部工具如何合并到一个工具池中。

常见困惑：
- 插件清单不是 MCP 服务器
- MCP 服务器不是单个 MCP 工具
- 外部能力不会绕过原生的权限路径

教学边界：
本文件讲解最小的有用 stdio MCP 路径。
市场细节、认证流程、重连逻辑和非工具能力层
有意留到桥接文档和后续扩展中。
"""

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
PERMISSION_MODES = ("default", "auto")


# TODO: 学习MCP部分


# -- 基础工具实现（与 s02 相同） --
def _safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径脱离工作区：{p}")
    return path


def _run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        return "错误：危险命令已阻止执行"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "（无输出）"
    except subprocess.TimeoutExpired:
        return "错误：超时（120秒）"


def _run_read(path: str, limit: int = None) -> str:
    try:
        lines = _safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"...（剩余 {len(lines) - limit} 行）"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误：{e}"


def _run_write(path: str, content: str) -> str:
    try:
        fp = _safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字节"
    except Exception as e:
        return f"错误：{e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        c = fp.read_text(encoding="utf-8")
        if old_text not in c:
            return f"错误：文件中未找到匹配的文本（{path}）"
        fp.write_text(c.replace(old_text, new_text, 1), encoding="utf-8")
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"



# -- 队长工具调度（12 个工具） --
TOOL_HANDLERS = {
    "bash":              lambda **kw: _run_bash(kw["command"]),
    "read_file":         lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":        lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":         lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# 这些基础工具与 s02 相同
TOOLS = [
    {"name": "bash", "description": "运行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "将内容写入文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "在文件中替换精确匹配的文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]


def agent_loop(messages: list):
    while True:

        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"未知工具：{block.name}"
                except Exception as e:
                    output = f"错误：{e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms16_CN >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
