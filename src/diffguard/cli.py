"""命令行入口。评测、CI 与本地调试都走同一份输出契约。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .graph import review
from .report import render_markdown


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diffguard", description="多 Agent 代码审查")
    sub = parser.add_subparsers(dest="command", required=True)

    cmd = sub.add_parser("review", help="审查一段 diff")
    cmd.add_argument("--diff", required=True, help="diff 文件路径，- 表示从 stdin 读取")
    cmd.add_argument("--title", default="", help="PR 标题")
    cmd.add_argument("--json", action="store_true", help="输出结构化 JSON")
    cmd.add_argument("--no-llm", action="store_true", help="只跑规则，不调用模型")
    cmd.add_argument("--out", help="把 markdown 报告写入文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "review":
        diff_text = (
            sys.stdin.read() if args.diff == "-" else Path(args.diff).read_text(encoding="utf-8")
        )
        try:
            report = asyncio.run(review(diff_text, pr_title=args.title, use_llm=not args.no_llm))
        except TimeoutError:
            sys.stderr.write(
                "审查超时，可调大 DIFFGUARD__BUDGET__TASK_TIMEOUT_SECONDS\n"
            )
            return 1

        if args.json:
            sys.stdout.write(report.model_dump_json(indent=2) + "\n")
        else:
            markdown = render_markdown(report, args.title)
            if args.out:
                Path(args.out).write_text(markdown, encoding="utf-8")
                print(f"报告已写入 {args.out}")
            else:
                print(markdown)

        # 阻断返回 2，便于 CI 直接当门禁用
        return 2 if report.verdict.value == "block" else 0

    return 1
