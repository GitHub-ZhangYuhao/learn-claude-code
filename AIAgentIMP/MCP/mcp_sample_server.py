"""
MCP Sample Server - 示例 MCP Server

使用 mcp SDK 的 FastMCP 构建，通过 stdio 传输。
包含 3 个工具:
- hello: 打招呼
- add: 计算两数之和
- get_time: 返回当前时间
"""

from datetime import datetime

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("MyTools")


@mcp.tool()
def hello(name: str) -> str:
    """向指定用户打招呼"""
    return f"Hello, {name}! 欢迎使用 MCP 服务。"


@mcp.tool()
def add(a: float, b: float) -> float:
    """计算两个数字之和"""
    return a + b


@mcp.tool()
def get_time() -> str:
    """返回当前系统时间"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    mcp.run()