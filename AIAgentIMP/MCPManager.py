"""
MCPManager.py - MCP 服务管理系统

支持连接标准 MCP Server（使用 @mcp.tool() 装饰器构建的工具服务器）。
通过 mcp Python SDK 的客户端连接，支持 stdio 和 SSE 两种传输方式。

依赖: pip install mcp

配置文件: .mcp.json
示例配置:
{
    "servers": {
        "my-tools": {
            "transport": "stdio",
            "command": "python",
            "args": ["mcp_sample_server.py"],
            "enabled": true
        },
        "remote": {
            "transport": "sse",
            "url": "http://localhost:8000/sse",
            "headers": {"Authorization": "Bearer ${TOKEN}"},
            "enabled": true
        }
    }
}

典型 MCP Server 示例 (mcp_sample_server.py):
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("MyTools")

    @mcp.tool()
    def add(a: int, b: int) -> str:
        \"\"\"计算两数之和\"\"\"
        return str(a + b)

    mcp.run()  # 默认 stdio 传输
"""

import asyncio
import json
import os
import threading
from pathlib import Path
from dataclasses import dataclass

from GlobalConfig import *

# ------------------------------------------------------------------
# 检测 mcp SDK 是否可用
# ------------------------------------------------------------------

_HAS_MCP = False
_HAS_SSE = False

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters
    _HAS_MCP = True
except ImportError:
    pass

try:
    from mcp.client.sse import sse_client
    _HAS_SSE = True
except ImportError:
    pass

if not _HAS_MCP:
    print("[MCPManager] 未安装 mcp SDK，MCP 功能不可用。安装: pip install mcp")

MCP_CONFIG_FILE = WORKDIR / ".mcp.json"


@dataclass
class MCPToolInfo:
    """MCP 工具元数据"""
    server_name: str            # 所属服务器名
    tool_name: str              # MCP 原始工具名
    namespaced_name: str        # 命名空间化: mcp__{server}__{tool}
    description: str
    input_schema: dict          # JSON Schema


# ======================================================================
# MCPConnection: 单个 MCP Server 的连接
# ======================================================================

class MCPConnection:
    """
    单个 MCP Server 连接

    在后台线程中运行 asyncio 事件循环，维持与 MCP Server 的长连接。
    对外暴露同步的 call_tool() 接口供 agent_loop 使用。

    工作原理:
    1. start() 启动后台线程，在其中建立 mcp SDK 的 ClientSession
    2. 后台事件循环通过 await sleep 保持连接活跃
    3. call_tool() 通过 asyncio.run_coroutine_threadsafe 将调用提交到后台事件循环
    4. 后台循环在 await 间隙执行工具调用
    5. stop() 终止后台循环，关闭连接
    """

    def __init__(self, name: str, transport: str, **kwargs):
        self.name = name
        self.transport = transport
        self.kwargs = kwargs
        self.tools: dict[str, MCPToolInfo] = {}
        self._session: "ClientSession | None" = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._running = False
        self._error: str | None = None

    def start(self) -> bool:
        """启动后台线程，连接到 MCP Server"""
        if not _HAS_MCP:
            print(f"[MCPManager] mcp SDK 未安装，无法启动 [{self.name}]")
            return False
        if self.transport == "sse" and not _HAS_SSE:
            print(f"[MCPManager] SSE 支持需要额外依赖: pip install mcp[sse]")
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._run_event_loop, daemon=True,
            name=f"MCP_{self.name}"
        )
        self._thread.start()

        # 等待连接就绪
        if not self._ready.wait(timeout=30):
            print(f"[MCPManager] 服务器 [{self.name}] 连接超时 (30s)")
            self._running = False
            return False
        if self._error:
            print(f"[MCPManager] 服务器 [{self.name}] 连接失败: {self._error}")
            self._running = False
            return False
        return True

    def stop(self):
        """断开连接"""
        self._running = False
        self._session = None
        if self._thread:
            self._thread.join(timeout=5)
        print(f"[MCPManager] 服务器 [{self.name}] 已断开")

    def is_running(self) -> bool:
        return self._running and self._session is not None

    # ------------------------------------------------------------------
    # 后台事件循环
    # ------------------------------------------------------------------

    def _run_event_loop(self):
        """后台线程入口: 创建 asyncio 事件循环并运行连接"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._maintain_connection())
        except Exception as e:
            if not self._ready.is_set():
                self._error = str(e)
                self._ready.set()
        finally:
            self._loop.close()

    async def _maintain_connection(self):
        """根据传输类型建立连接并保持活跃"""
        try:
            if self.transport == "stdio":
                await self._connect_stdio()
            elif self.transport == "sse":
                await self._connect_sse()
        except Exception as e:
            if not self._ready.is_set():
                self._error = str(e)
                self._ready.set()
        finally:
            self._session = None

    async def _connect_stdio(self):
        """stdio 传输: 启动子进程并建立 MCP 会话"""
        env = self._resolve_env(self.kwargs.get("env", {}))
        full_env = {**os.environ, **env} if env else None
        server_params = StdioServerParameters(
            command=self.kwargs["command"],
            args=self.kwargs.get("args", []),
            env=full_env,
        )
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                self._session = session
                await self._init_and_discover(session)
                self._ready.set()
                print(f"[MCPManager] stdio 服务器 [{self.name}] 已连接，"
                      f"发现 {len(self.tools)} 个工具")
                # 保持连接活跃 — 事件循环在此期间可处理 call_tool 请求
                while self._running:
                    await asyncio.sleep(0.1)

    async def _connect_sse(self):
        """SSE 传输: 连接到远程 MCP Server"""
        url = self.kwargs["url"]
        headers = self._resolve_env(self.kwargs.get("headers", {}))
        async with sse_client(url, headers=headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                self._session = session
                await self._init_and_discover(session)
                self._ready.set()
                print(f"[MCPManager] SSE 服务器 [{self.name}] 已连接，"
                      f"发现 {len(self.tools)} 个工具")
                while self._running:
                    await asyncio.sleep(0.1)

    async def _init_and_discover(self, session: "ClientSession"):
        """MCP 握手 + 工具发现"""
        await session.initialize()
        tools_result = await session.list_tools()
        for tool in tools_result.tools:
            name = tool.name
            namespaced = f"mcp__{self.name}__{name}"
            # 兼容不同版本 mcp SDK 的属性命名 (inputSchema vs input_schema)
            schema = getattr(tool, 'inputSchema', None) or getattr(tool, 'input_schema', {})
            if isinstance(schema, str):
                schema = json.loads(schema)
            self.tools[name] = MCPToolInfo(
                server_name=self.name,
                tool_name=name,
                namespaced_name=namespaced,
                description=tool.description or "",
                input_schema=schema if isinstance(schema, dict) else {},
            )

    # ------------------------------------------------------------------
    # 同步工具调用（供 agent_loop 使用）
    # ------------------------------------------------------------------

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """同步调用 MCP 工具 — 通过 run_coroutine_threadsafe 桥接到后台事件循环"""
        if not self._session or not self._loop or not self._running:
            return f"错误: MCP 服务器 [{self.name}] 未连接"
        future = asyncio.run_coroutine_threadsafe(
            self._async_call_tool(tool_name, arguments),
            self._loop,
        )
        try:
            return future.result(timeout=60)
        except TimeoutError:
            return f"错误: MCP 工具调用超时 [{self.name}/{tool_name}]"
        except Exception as e:
            return f"错误: MCP 工具调用失败 [{self.name}/{tool_name}]: {e}"

    async def _async_call_tool(self, tool_name: str, arguments: dict) -> str:
        """异步执行工具调用并解析结果"""
        result = await self._session.call_tool(tool_name, arguments)
        texts = []
        for part in result.content:
            if hasattr(part, 'text'):
                texts.append(part.text)
            else:
                texts.append(f"[{getattr(part, 'type', 'unknown')}]")
        if getattr(result, 'isError', False):
            return f"MCP 工具执行错误: {' '.join(texts)}"
        return "\n".join(texts) if texts else str(result)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_env(env_dict: dict) -> dict:
        """解析 ${VAR} 环境变量引用"""
        resolved = {}
        for k, v in (env_dict or {}).items():
            if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                v = os.environ.get(v[2:-1], "")
            resolved[k] = v
        return resolved


# ======================================================================
# MCPMockServer: 内存 Mock（不依赖 mcp SDK，用于单元测试）
# ======================================================================

class MCPMockServer:
    """
    纯内存 Mock — 模拟 MCPConnection 的接口，用于不依赖 mcp SDK 的单元测试。

    提供两个测试工具:
    - echo: 回显输入消息
    - add:  计算两数之和
    """

    def __init__(self, name: str = "mock"):
        self.name = name
        self.tools: dict[str, MCPToolInfo] = {}
        self._running = False

    def start(self) -> bool:
        self._running = True
        self.tools["echo"] = MCPToolInfo(
            server_name=self.name, tool_name="echo",
            namespaced_name=f"mcp__{self.name}__echo",
            description="回显输入消息",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string", "description": "要回显的消息"}},
                "required": ["message"]
            }
        )
        self.tools["add"] = MCPToolInfo(
            server_name=self.name, tool_name="add",
            namespaced_name=f"mcp__{self.name}__add",
            description="计算两个数字之和",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "第一个数"},
                    "b": {"type": "number", "description": "第二个数"}
                },
                "required": ["a", "b"]
            }
        )
        print(f"[MCPMockServer] 服务器 [{self.name}] 已启动，注册 {len(self.tools)} 个工具")
        return True

    def stop(self):
        self._running = False
        self.tools.clear()
        print(f"[MCPMockServer] 服务器 [{self.name}] 已停止")

    def is_running(self) -> bool:
        return self._running

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        if not self._running:
            return f"错误: MCP 服务器 [{self.name}] 未运行"
        if tool_name == "echo":
            return f"Echo: {arguments.get('message', '')}"
        elif tool_name == "add":
            return str(arguments.get("a", 0) + arguments.get("b", 0))
        return f"MCP 工具执行错误: 未知工具 {tool_name}"


# ======================================================================
# MCPManager: 集中管理所有 MCP 连接
# ======================================================================

class MCPManager:
    """
    MCP 服务器集中管理

    职责:
    1. 加载 .mcp.json 配置
    2. 为每个服务器创建 MCPConnection（stdio / sse）
    3. 收集所有 MCP 工具并转换为 OpenAI function calling 格式
    4. 路由工具调用到对应的连接
    5. 生成 MCP_TOOLS / MCP_TOOL_HANDLERS 供 agent_loop 使用
    """

    def __init__(self, config_path: Path = None):
        self.config_path = config_path or MCP_CONFIG_FILE
        self.connections: dict[str, "MCPConnection | MCPMockServer"] = {}
        self._tool_routing: dict[str, tuple[str, str]] = {}  # namespaced_name -> (server, tool)

    def load_and_start(self) -> int:
        """加载配置并启动所有已启用的 MCP 服务器"""
        config = self._load_config()
        if not config:
            return 0
        servers = config.get("servers", {})
        started = 0
        for name, srv_cfg in servers.items():
            if not srv_cfg.get("enabled", True):
                print(f"[MCPManager] 服务器 [{name}] 已禁用，跳过")
                continue
            transport = srv_cfg.get("transport", "stdio")
            conn_kwargs = {k: v for k, v in srv_cfg.items() if k not in ("transport", "enabled")}
            conn = MCPConnection(name, transport, **conn_kwargs)
            if conn.start():
                self.connections[name] = conn
                for tool_name, tool_info in conn.tools.items():
                    self._tool_routing[tool_info.namespaced_name] = (name, tool_name)
                started += 1
        print(f"[MCPManager] 共启动 {started}/{len(servers)} 个 MCP 服务器，"
              f"注册 {len(self._tool_routing)} 个工具")
        return started

    def register_server(self, server: "MCPConnection | MCPMockServer"):
        """手动注册一个已启动的服务器（用于测试或程序化注册）"""
        self.connections[server.name] = server
        for tool_name, tool_info in server.tools.items():
            self._tool_routing[tool_info.namespaced_name] = (server.name, tool_name)

    def stop_all(self):
        """停止所有 MCP 服务器"""
        for conn in self.connections.values():
            conn.stop()
        self.connections.clear()
        self._tool_routing.clear()

    # ------------------------------------------------------------------
    # 工具 Schema 转换 & Handler 生成
    # ------------------------------------------------------------------

    def get_openai_tools(self) -> list:
        """将所有 MCP 工具转换为 OpenAI function calling 格式"""
        tools = []
        for conn in self.connections.values():
            for tool_info in conn.tools.values():
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool_info.namespaced_name,
                        "description": f"[MCP:{conn.name}] {tool_info.description}",
                        "parameters": self._convert_schema(tool_info.input_schema),
                    }
                })
        return tools

    def get_tool_handlers(self) -> dict:
        """生成工具处理函数映射: {namespaced_name: handler_fn}"""
        handlers = {}
        for ns_name, (srv_name, tool_name) in self._tool_routing.items():
            def make_handler(sn, tn):
                def handler(**kwargs):
                    conn = self.connections.get(sn)
                    if not conn:
                        return f"错误: MCP 服务器 [{sn}] 未连接"
                    return conn.call_tool(tn, kwargs)
                return handler
            handlers[ns_name] = make_handler(srv_name, tool_name)
        return handlers

    @staticmethod
    def _convert_schema(mcp_schema: dict) -> dict:
        """MCP inputSchema → OpenAI parameters 格式"""
        if not mcp_schema:
            return {"type": "object", "properties": {}}
        result = {
            "type": mcp_schema.get("type", "object"),
            "properties": mcp_schema.get("properties", {}),
        }
        if "required" in mcp_schema:
            result["required"] = mcp_schema["required"]
        if "description" in mcp_schema:
            result["description"] = mcp_schema["description"]
        return result

    # ------------------------------------------------------------------
    # 信息查询
    # ------------------------------------------------------------------

    def list_servers(self) -> str:
        if not self.connections:
            return "当前没有运行中的 MCP 服务器"
        lines = ["# MCP 服务器状态"]
        for name, conn in self.connections.items():
            status = "运行中" if conn.is_running() else "已断开"
            tool_count = len(conn.tools)
            lines.append(f"- [{name}] 状态:{status} 工具数:{tool_count}")
            for t in conn.tools.values():
                lines.append(f"    - {t.namespaced_name}: {t.description[:60]}")
        return "\n".join(lines)

    def describe_tools(self) -> str:
        if not self._tool_routing:
            return ""
        lines = ["\n## MCP 外部工具"]
        for conn in self.connections.values():
            lines.append(f"\n### [{conn.name}] 服务器:")
            for tool_info in conn.tools.values():
                params = ", ".join(tool_info.input_schema.get("properties", {}).keys())
                lines.append(f"- {tool_info.namespaced_name}({params}): {tool_info.description}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            print(f"[MCPManager] 未找到配置文件 {self.config_path}，MCP 功能未启用")
            return {}
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[MCPManager] 配置加载失败: {e}")
            return {}


# ==================================================================
# 模块级初始化
# ==================================================================

_MCP_MANAGER = MCPManager()
_mcp_started = _MCP_MANAGER.load_and_start()

MCP_TOOLS: list = _MCP_MANAGER.get_openai_tools()
MCP_TOOL_HANDLERS: dict = _MCP_MANAGER.get_tool_handlers()

if MCP_TOOLS:
    print(f"[MCPManager] 已注册 {len(MCP_TOOLS)} 个 MCP 工具到工具系统")
