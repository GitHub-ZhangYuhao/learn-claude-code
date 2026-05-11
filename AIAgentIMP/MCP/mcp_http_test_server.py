#!/usr/bin/env python3
"""
MCP Streamable HTTP Test Server - Streamable HTTP 传输方式的 MCP 测试服务器。
"""
import sys

import uvicorn
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("HTTPTools")


@mcp.tool()
def http_echo(message: str) -> str:
    """回显消息（HTTP）"""
    return f"HTTP echo: {message}"


@mcp.tool()
def http_multiply(x: float, y: float) -> float:
    """HTTP 乘法运算"""
    return x * y


@mcp.tool()
def http_greet(name: str) -> str:
    """打招呼（HTTP）"""
    return f"HTTP greet: Hello, {name}!"


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8002
    uvicorn.run(mcp.streamable_http_app, host="127.0.0.1", port=port)
