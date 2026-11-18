"""工具层的 MCP Server。

按方案的分工表，只有「依赖重、需要跨 Agent 复用」的工具对外暴露：
parse_diff、规则引擎、AST 分析。脱敏与编排逻辑留在进程内。

这里只做协议包装，函数体直接调用 tools 下的实现，保证 MCP 调用与
进程内调用的结果完全一致。

运行：
    uv run diffguard-mcp                     # stdio，供本地 Agent 客户端
    uv run diffguard-mcp --http --port 8124  # streamable HTTP
"""

from __future__ import annotations

import argparse
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import load_settings
from .tools import ast_scan, rules
from .tools import diff as diff_tools

mcp = MCPServer(
    "diffguard-tools",
    instructions="代码审查的零成本工具层：解析 diff、跑内置规则、做 AST 分析。",
)


def _dump(issues: list) -> list[dict[str, Any]]:
    return [issue.model_dump(mode="json") for issue in issues]


@mcp.tool()
def parse_diff(diff_text: str) -> dict[str, Any]:
    """把 unified diff 解析成文件、hunk 与行号映射。"""
    files = diff_tools.parse_diff(diff_text)
    return {
        "files": [
            {
                "path": fd.path,
                "language": fd.language,
                "is_binary": fd.is_binary,
                "is_deleted": fd.is_deleted,
                "added_lines": len(fd.added_lines),
                "hunks": [
                    {
                        "header": hunk.header,
                        "lines": [
                            {"kind": ln.kind, "text": ln.text, "new_line": ln.new_lineno}
                            for ln in hunk.lines
                        ],
                    }
                    for hunk in fd.hunks
                ],
            }
            for fd in files
        ]
    }


@mcp.tool()
def scan_rules(diff_text: str, target_line_only: bool = True) -> list[dict[str, Any]]:
    """跑内置正则规则，默认只返回新增行上的命中。"""
    return [
        item
        for fd in diff_tools.parse_diff(diff_text)
        for item in _dump(rules.scan_rules(fd, target_line_only=target_line_only))
    ]


@mcp.tool()
def scan_complexity(diff_text: str, threshold: int | None = None) -> list[dict[str, Any]]:
    """按圈复杂度阈值扫描被本次改动触及的函数。"""
    limit = threshold if threshold is not None else load_settings().rules.complexity_threshold
    return [
        item
        for fd in diff_tools.parse_diff(diff_text)
        for item in _dump(ast_scan.scan_complexity(fd, limit))
    ]


@mcp.tool()
def scan_blocking_io(diff_text: str) -> list[dict[str, Any]]:
    """找出异步函数中调用同步阻塞 IO（time.sleep、requests、httpx 同步方法）。"""
    return [
        item
        for fd in diff_tools.parse_diff(diff_text)
        for item in _dump(ast_scan.scan_blocking_io(fd))
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="DiffGuard 工具 MCP Server")
    parser.add_argument("--http", action="store_true", help="以 streamable HTTP 启动，默认 stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8124)
    args = parser.parse_args()

    if args.http:
        import uvicorn

        uvicorn.run(mcp.streamable_http_app(), host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
