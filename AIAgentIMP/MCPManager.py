#!/usr/bin/env python3
"""
MCPManager.py - 同步版 MCP 工具管理器

桥接异步 mcp SDK 到同步调用，使主 Agent / 子 Agent 的同步工具循环可以直接使用 MCP 工具。
"""

import asyncio
import json
from pathlib import Path
from typing import Dict

from mcp import ClientSession, StdioServerParameters, stdio_client
from contextlib import AsyncExitStack

from GlobalConfig import WORKDIR

MCP_CONFIG_PATH = WORKDIR / "MCP" / ".mcp.json"


class MCPManager:
    """同步版 MCP 管理器。

    内部运行一个独立事件循环来桥接异步 mcp SDK。
    外部调用者只需要同步调用 connect / call_tool 等方法。
    """

    def __init__(self, config_path: Path = MCP_CONFIG_PATH):
        self.config_path = config_path
        # 独立事件循环（不阻塞主循环）
        self._loop = asyncio.new_event_loop()
        self._exit_stack = AsyncExitStack()
        # server_name -> ClientSession
        self.sessions: Dict[str, ClientSession] = {}
        # server_name -> 启动时创建的后台任务（保持 stdio 管道存活）
        self._bg_tasks: Dict[str, asyncio.Task] = {}
        # 工具映射：完整工具名 -> {server_name, original_name, description}
        self.tool_map: Dict[str, dict] = {}
        # 所有工具列表（OpenAI Function Call 格式）
        self.all_tools: list = []

    # --------------- 公开同步 API ---------------

    def connect_to_servers(self) -> None:
        """读取 .mcp.json 配置，连接所有启用的服务器并注册工具。"""
        if not self.config_path.exists():
            print(f"[MCPManager] 配置文件不存在: {self.config_path}，跳过 MCP 初始化")
            return

        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        servers = config.get("servers", {})

        # 过滤 enabled=True 的服务器
        enabled = {
            name: info for name, info in servers.items() if info.get("enabled", False)
        }
        if not enabled:
            print("[MCPManager] 没有启用的 MCP 服务器，跳过初始化")
            return

        self._loop.run_until_complete(self._connect_all(enabled))
        self._print_connection_info(enabled)

    def get_tools_schema(self) -> list:
        """返回 OpenAI Function Call 格式的工具 schema 列表。"""
        return list(self.all_tools)

    def call_tool(self, full_name: str, tool_args: dict) -> str:
        """同步调用 MCP 工具。

        Args:
            full_name: 完整工具名（server_name_tool_name）
            tool_args: 工具参数字典
        """
        meta = self.tool_map.get(full_name)
        if not meta:
            return f"[MCP] 未找到工具: {full_name}"
        return self._call_tool_sync(meta["server_name"], meta["original_name"], tool_args)

    def get_mcp_tool_names(self) -> set:
        """返回所有已注册的 MCP 工具名称集合。"""
        return set(self.tool_map.keys())

    def cleanup(self) -> None:
        """清理所有 MCP 资源。"""
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.run_until_complete(self._cleanup_all())
            except Exception:
                pass
            self._loop.close()
        self.sessions.clear()
        self.tool_map.clear()
        self.all_tools.clear()

    # --------------- 内部异步实现 ---------------

    async def _connect_all(self, servers: dict) -> None:
        """异步：连接所有服务器并注册工具。"""
        for server_name, info in servers.items():
            try:
                await self._start_one_server(server_name, info)
                await self._register_server_tools(server_name)
            except Exception as e:
                print(f"[MCPManager] 连接服务器 {server_name} 失败: {e}")

    async def _start_one_server(self, server_name: str, info: dict) -> None:
        """异步：启动单个 MCP 服务器并保存会话。"""
        command = info.get("command", "python")
        args_raw = info.get("args", "")
        args = args_raw.split() if isinstance(args_raw, str) else args_raw

        # 如果 args 是相对路径，解析为绝对路径
        resolved_args = []
        for a in args:
            p = Path(a)
            if not p.is_absolute():
                resolved = (self.config_path.parent / p).resolve()
                resolved_args.append(str(resolved))
            else:
                resolved_args.append(a)

        server_params = StdioServerParameters(
            command=command, args=resolved_args, env=None
        )

        stdio_transport = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        read_stream, write_stream = stdio_transport

        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()

        self.sessions[server_name] = session

        # 创建一个持续运行的读取任务，保持 stdio 管道活跃
        task = asyncio.create_task(self._keep_pipe_alive(read_stream))
        self._bg_tasks[server_name] = task

    async def _keep_pipe_alive(self, read_stream):
        """持续读取 stdin 以保持管道存活。"""
        try:
            while True:
                await read_stream.read()
        except Exception:
            pass

    async def _register_server_tools(self, server_name: str) -> None:
        """异步：注册单个服务器的所有工具。"""
        session = self.sessions[server_name]
        resp = await session.list_tools()

        for tool in resp.tools:
            full_name = f"{server_name}_{tool.name}"

            # 将 outputSchema 转为 output_schema（OpenAI 兼容字段名）
            output_schema = {}
            if hasattr(tool, "outputSchema") and tool.outputSchema:
                output_schema = tool.outputSchema

            mcp_tool = {
                "type": "function",
                "function": {
                    "name": full_name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                }
            }
            if output_schema:
                mcp_tool["function"]["output_schema"] = output_schema

            self.tool_map[full_name] = {
                "server_name": server_name,
                "original_name": tool.name,
                "description": tool.description or "",
            }
            self.all_tools.append(mcp_tool)

    def _call_tool_sync(self, server_name: str, original_name: str, args: dict) -> str:
        """同步调用 MCP 工具（在独立事件循环中执行）。"""
        session = self.sessions.get(server_name)
        if not session:
            return f"[MCP] 未找到服务器: {server_name}"

        try:
            return self._loop.run_until_complete(
                self._call_tool_async(session, server_name, original_name, args)
            )
        except Exception as e:
            return f"[MCP] 工具调用异常: {str(e)}"

    async def _call_tool_async(self, session, server_name, original_name, args):
        """异步执行单个工具调用。"""
        resp = await session.call_tool(original_name, args)

        if resp.content:
            if isinstance(resp.content, list):
                texts = [item.text for item in resp.content if hasattr(item, "text")]
                return "\n".join(texts) if texts else str(resp.content)
            return str(resp.content)
        elif getattr(resp, "isError", False):
            err = getattr(resp, "errorMessage", "未知错误")
            return f"[MCP] 工具执行失败: {err}"
        else:
            return "[MCP] 工具执行成功，无输出"

    async def _cleanup_all(self) -> None:
        """异步清理：取消后台任务并关闭 exit_stack。"""
        for task in self._bg_tasks.values():
            task.cancel()
        await self._exit_stack.aclose()

    # --------------- 辅助 ---------------

    def _print_connection_info(self, servers: dict) -> None:
        print("\n=== MCP 服务器连接状态 ===")
        for name, info in servers.items():
            status = "[OK]" if name in self.sessions else "[FAIL]"
            print(f" - {name}: {info.get('command', '?')} {status}")

        print("\n=== 已注册的 MCP 工具 ===")
        if self.all_tools:
            for tool in self.all_tools:
                desc = tool["function"]["description"][:60]
                print(f" - {tool['function']['name']}: {desc}...")
        else:
            print(" - 无可用工具")


if __name__ == "__main__":
    """快速自测：连接、注册、调用、清理全流程。"""
    print("=" * 50)
    print("MCPManager 自测开始")
    print("=" * 50)

    mgr = MCPManager()

    # 1. 连接
    mgr.connect_to_servers()
    assert len(mgr.all_tools) > 0, "FAIL: 没有注册到任何工具"
    print(f"\n[PASS] 注册了 {len(mgr.all_tools)} 个工具")

    # 2. Schema 格式
    schemas = mgr.get_tools_schema()
    for s in schemas:
        assert s["type"] == "function", f"FAIL: schema type 错误: {s}"
        assert "name" in s["function"], f"FAIL: 缺少 name: {s}"
        assert "description" in s["function"], f"FAIL: 缺少 description: {s}"
        assert "parameters" in s["function"], f"FAIL: 缺少 parameters: {s}"
    print("[PASS] 所有工具 schema 格式正确")

    # 3. 工具调用
    call_args = {
        "hello": {"name": "Test"},
        "add": {"a": 1.5, "b": 2.5},
    }
    for tool_name in mgr.get_mcp_tool_names():
        args = {}
        for key, val in call_args.items():
            if key in tool_name:
                args = val
                break
        result = mgr.call_tool(tool_name, args)
        print(f"  {tool_name} => {result}")
        assert "FAIL" not in result.upper() and "异常" not in result, f"FAIL: 工具调用失败: {result}"
    print("[PASS] 所有工具调用成功")

    # 4. 错误处理
    err = mgr.call_tool("nonexistent_tool", {})
    assert "未找到工具" in err, f"FAIL: 错误处理异常: {err}"
    print("[PASS] 无效工具返回错误信息")

    # 5. 清理
    mgr.cleanup()
    assert mgr.sessions == {}, "FAIL: sessions 未清空"
    assert mgr.tool_map == {}, "FAIL: tool_map 未清空"
    assert mgr.all_tools == [], "FAIL: all_tools 未清空"
    assert mgr._loop.is_closed(), "FAIL: event loop 未关闭"
    print("[PASS] 清理后状态正确")

    # 6. 连续清理不报错
    mgr.cleanup()
    print("[PASS] 连续清理安全")

    print("=" * 50)
    print("MCPManager 自测全部通过")
    print("=" * 50)
