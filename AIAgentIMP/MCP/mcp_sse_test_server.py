#!/usr/bin/env python3
"""
MCP SSE Test Server - 纯 SSE 传输方式的 MCP 测试服务器。
"""
import sys

import uvicorn
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SSETools")


@mcp.tool()
def sse_echo(message: str) -> str:
    """回显消息（SSE）"""
    return f"SSE echo: {message}"


@mcp.tool()
def sse_add(x: float, y: float) -> float:
    """SSE 加法运算"""
    return x + y


@mcp.tool()
def sse_time() -> str:
    """返回当前时间（SSE）"""
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    uvicorn.run(mcp.sse_app, host="127.0.0.1", port=port)
