"""
mcp_sample_server.py - 示例 MCP Server

使用 @mcp.tool() 装饰器定义工具，供 MCPManager 连接和调用。

依赖: pip install mcp

运行方式:
  stdio 模式（默认）: python mcp_sample_server.py
  SSE 模式:          python mcp_sample_server.py --sse
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SampleTools")


@mcp.tool()
def echo(message: str) -> str:
    """回显输入消息"""
    return f"Echo: {message}"


@mcp.tool()
def add(a: int, b: int) -> str:
    """计算两个数字之和"""
    return str(a + b)


@mcp.tool()
def multiply(a: int, b: int) -> str:
    """计算两个数字之积"""
    return str(a * b)


@mcp.tool()
def greet(name: str, language: str = "zh") -> str:
    """用指定语言打招呼"""
    greetings = {
        "zh": f"你好，{name}！",
        "en": f"Hello, {name}!",
        "ja": f"こんにちは、{name}！",
    }
    return greetings.get(language, f"Hi, {name}!")


if __name__ == "__main__":
    import sys
    if "--sse" in sys.argv:
        mcp.run(transport="sse")
    else:
        mcp.run()
