"""MCP stdio 过滤器：转发子进程 stdout 中的 JSON-RPC 消息，过滤非 JSON 行。

某些 MCP server（如 github-mcp-server）启动时会往 stdout 打印 banner/
欢迎信息，污染 MCP stdio 协议（协议要求 stdout 只能输出 JSON-RPC 消息）。
本 wrapper 逐行转发，只放行 JSON 开头的行，stderr 原样直通。

用法（mcp_config.json）：
  {"command": "/path/python", "args": ["mcp_stdio_filter.py", "npx", "-y", "github-mcp-server"]}
"""
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: mcp_stdio_filter.py <命令> [参数...]", file=sys.stderr)
        return 1
    proc = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE,
                            stderr=sys.stderr, text=True, bufsize=1)
    for line in proc.stdout:
        if line.lstrip().startswith("{"):      # JSON-RPC 消息（换行分隔）
            sys.stdout.write(line)
            sys.stdout.flush()
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
