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
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
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
        transport = info.get("transport", "stdio")

        if transport == "stdio":
            await self._start_stdio(server_name, info)
        elif transport == "sse":
            await self._start_sse(server_name, info)
        elif transport == "streamable_http":
            await self._start_streamable_http(server_name, info)
        else:
            raise ValueError(f"不支持的传输方式: {transport}")

    async def _start_stdio(self, server_name: str, info: dict) -> None:
        """通过 stdio 启动本地 MCP 子进程。"""
        command = info.get("command", "python")
        args_raw = info.get("args", "")
        args = args_raw.split() if isinstance(args_raw, str) else args_raw

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
        await self._init_session(server_name, read_stream, write_stream)

    async def _start_sse(self, server_name: str, info: dict) -> None:
        """通过 SSE 连接远程 MCP 服务器。"""
        url = info.get("url")
        if not url:
            raise ValueError(f"SSE 服务器 {server_name} 缺少 url 配置")

        ctx = sse_client(url=url)
        read_stream, write_stream = await self._exit_stack.enter_async_context(ctx)
        await self._init_session(server_name, read_stream, write_stream)

    async def _start_streamable_http(self, server_name: str, info: dict) -> None:
        """通过 Streamable HTTP 连接远程 MCP 服务器。"""
        url = info.get("url")
        if not url:
            raise ValueError(f"Streamable HTTP 服务器 {server_name} 缺少 url 配置")

        ctx = streamablehttp_client(url=url)
        read_stream, write_stream, _ = await self._exit_stack.enter_async_context(ctx)
        await self._init_session(server_name, read_stream, write_stream)

    async def _init_session(self, server_name: str, read_stream, write_stream) -> None:
        """通用：初始化 ClientSession 并启动后台读取任务。"""
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()

        self.sessions[server_name] = session

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
    """单元测试：验证 stdio / SSE / Streamable HTTP 三种传输方式。

    使用正式 .mcp.json 配置，SSE 和 HTTP 服务器默认未启用。
    测试时会将它们临时改为 enabled，测试结束后恢复。
    """
    import subprocess
    import time

    print("=" * 60)
    print("MCPManager 单元测试开始")
    print("=" * 60)

    mcp_dir = Path(__file__).parent / "MCP"
    pass_results = []
    fail_results = []

    # 备份并启用 SSE / HTTP 服务器
    config = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
    original_enabled = {}
    for name in ["sse-tools", "http-tools"]:
        if name in config.get("servers", {}):
            original_enabled[name] = config["servers"][name].get("enabled", False)
            config["servers"][name]["enabled"] = True

    # 写入临时覆盖配置
    MCP_CONFIG_PATH.write_text(json.dumps(config, indent=4, ensure_ascii=False), encoding="utf-8")

    sse_proc = http_proc = None
    try:
        # -------- 1. 启动 SSE 和 HTTP 测试服务器 --------
        print("\n[1] 启动测试服务器...")
        sse_proc = subprocess.Popen(
            ["python", str(mcp_dir / "mcp_sse_test_server.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        http_proc = subprocess.Popen(
            ["python", str(mcp_dir / "mcp_http_test_server.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(3)

        import socket
        for port, name in [(8001, "SSE"), (8002, "HTTP")]:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(("127.0.0.1", port))
            sock.close()
            if result == 0:
                print(f"    {name} 服务器已启动 (端口 {port})")
                pass_results.append(f"{name} 服务器启动成功")
            else:
                print(f"    {name} 服务器启动失败 (端口 {port})")
                fail_results.append(f"{name} 服务器未监听端口 {port}")

        # -------- 2. 连接所有服务器 --------
        print("\n[2] 连接 MCP 服务器...")
        mgr = MCPManager()
        mgr.connect_to_servers()

        for name in ["my-tools", "sse-tools", "http-tools"]:
            if name in mgr.sessions:
                print(f"    {name}: 连接成功")
                pass_results.append(f"{name} 连接成功")
            else:
                print(f"    {name}: 连接失败")
                fail_results.append(f"{name} 未建立 session")

        # -------- 3. 检查工具注册 --------
        print(f"\n[3] 工具注册: 共 {len(mgr.all_tools)} 个工具")
        for tool in mgr.all_tools:
            print(f"    - {tool['function']['name']}")

        transport_counts = {"stdio": 0, "sse": 0, "streamable_http": 0}
        for full_name in mgr.tool_map:
            if full_name.startswith("my-tools_"):
                transport_counts["stdio"] += 1
            elif full_name.startswith("sse-tools_"):
                transport_counts["sse"] += 1
            elif full_name.startswith("http-tools_"):
                transport_counts["streamable_http"] += 1

        for transport, count in transport_counts.items():
            if count > 0:
                print(f"    {transport}: {count} 个工具")
                pass_results.append(f"{transport} 注册了 {count} 个工具")
            else:
                print(f"    {transport}: 0 个工具")
                fail_results.append(f"{transport} 未注册任何工具")

        # -------- 4. 调用工具测试 --------
        print("\n[4] 工具调用测试:")
        test_cases = [
            ("my-tools_hello", {"name": "Test"}),
            ("my-tools_add", {"a": 1.5, "b": 2.5}),
            ("sse-tools_sse_echo", {"message": "hello from SSE"}),
            ("sse-tools_sse_add", {"x": 3.0, "y": 4.0}),
            ("http-tools_http_echo", {"message": "hello from HTTP"}),
            ("http-tools_http_multiply", {"x": 2.5, "y": 4.0}),
        ]
        for full_name, args in test_cases:
            if full_name in mgr.tool_map:
                result = mgr.call_tool(full_name, args)
                status = "OK" if "异常" not in result and "FAIL" not in result.upper() else "FAIL"
                print(f"    {full_name}({args}) => {result} [{status}]")
                if status == "OK":
                    pass_results.append(f"{full_name} 调用成功: {result}")
                else:
                    fail_results.append(f"{full_name} 调用失败: {result}")
            else:
                print(f"    {full_name}: 工具不存在")
                fail_results.append(f"{full_name} 未注册")

        # -------- 5. Schema 格式检查 --------
        print("\n[5] Schema 格式检查:")
        schemas = mgr.get_tools_schema()
        for s in schemas:
            assert s["type"] == "function", f"schema type 错误: {s}"
            assert "name" in s["function"], f"缺少 name: {s}"
            assert "description" in s["function"], f"缺少 description: {s}"
            assert "parameters" in s["function"], f"缺少 parameters: {s}"
        print("    所有工具 schema 格式正确")
        pass_results.append("Schema 格式正确")

        # -------- 6. 错误处理测试 --------
        print("\n[6] 错误处理测试:")
        err = mgr.call_tool("nonexistent_tool", {})
        assert "未找到工具" in err, f"错误处理异常: {err}"
        print(f"    无效工具返回: {err}")
        pass_results.append("错误处理正常")

        # -------- 7. 清理 --------
        print("\n[7] 清理资源...")
        mgr.cleanup()
        assert mgr.sessions == {}, "sessions 未清空"
        assert mgr.tool_map == {}, "tool_map 未清空"
        assert mgr.all_tools == [], "all_tools 未清空"
        assert mgr._loop.is_closed(), "event loop 未关闭"
        print("    MCPManager 清理完毕")
        pass_results.append("MCPManager 清理成功")

        mgr.cleanup()
        print("    连续清理安全")
        pass_results.append("连续清理安全")

    finally:
        # 恢复原始配置
        for name, was_enabled in original_enabled.items():
            if name in config.get("servers", {}):
                config["servers"][name]["enabled"] = was_enabled
        MCP_CONFIG_PATH.write_text(json.dumps(config, indent=4, ensure_ascii=False), encoding="utf-8")

        # 关闭测试服务器
        print("\n[8] 关闭测试服务器...")
        for proc, name in [(sse_proc, "SSE"), (http_proc, "HTTP")]:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                    print(f"    {name} 服务器已关闭")
                except Exception:
                    proc.kill()
                    print(f"    {name} 服务器强制关闭")

    # -------- 汇总 --------
    print("\n" + "=" * 60)
    print(f"测试结果: {len(pass_results)} 通过, {len(fail_results)} 失败")
    print("=" * 60)

    if pass_results:
        print("\n通过项:")
        for r in pass_results:
            print(f"  [PASS] {r}")

    if fail_results:
        print("\n失败项:")
        for r in fail_results:
            print(f"  [FAIL] {r}")

    if not fail_results:
        print("\n全部测试通过!")
    else:
        print(f"\n有 {len(fail_results)} 项失败，请检查上方日志。")
