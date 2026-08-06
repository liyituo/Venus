"""测试用 MCP server：echo + add 两个工具（stdio 传输）。

mcp SDK 2.0 起 FastMCP 独立成 fastmcp 包。
"""
from fastmcp import FastMCP

mcp = FastMCP("echo-test")


@mcp.tool()
def echo(text: str) -> str:
    """原样返回输入文本。"""
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


if __name__ == "__main__":
    mcp.run()
