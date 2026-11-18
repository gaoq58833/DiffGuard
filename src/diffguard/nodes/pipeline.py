"""编排层三个节点：解析、静态初筛、路由。

编排层不做业务判断，只做解析与分发。
解析与静态扫描是 CPU 型工作，放进线程池执行，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import time

from ..config import Settings
from ..schemas import Issue, TraceEvent
from ..state import PRReviewState
from ..tools.ast_scan import scan_blocking_io, scan_complexity
from ..tools.diff import FileDiff, parse_diff
from ..tools.rules import scan_rules

ALL_AGENTS = ["security_agent", "style_agent", "perf_agent"]


async def parse_diff_node(state: PRReviewState) -> dict:
    started = time.perf_counter()
    files = await asyncio.to_thread(parse_diff, state.get("diff", ""))
    elapsed = int((time.perf_counter() - started) * 1000)
    skipped = [f.path for f in files if f.is_binary or f.is_deleted]
    return {
        "files": files,
        "trace": [
            TraceEvent(
                node="parse_diff",
                kind="parse",
                tool="parse_diff",
                detail=f"{len(files)} files, skipped={skipped}",
                elapsed_ms=elapsed,
            )
        ],
        "steps": 1,
    }


def _scan_files(files: list[FileDiff], settings: Settings) -> list[Issue]:
    candidates: list[Issue] = []
    for fd in files:
        candidates.extend(scan_rules(fd, settings.rules.target_line_only))
        candidates.extend(scan_complexity(fd, settings.rules.complexity_threshold))
        candidates.extend(scan_blocking_io(fd))
    return candidates


def make_static_scan(settings: Settings):
    async def static_scan_node(state: PRReviewState) -> dict:
        started = time.perf_counter()
        candidates = await asyncio.to_thread(_scan_files, state.get("files", []), settings)
        elapsed = int((time.perf_counter() - started) * 1000)
        rules_hit = sorted({c.rule_id for c in candidates})
        return {
            "candidates": candidates,
            "issues": candidates,
            "trace": [
                TraceEvent(
                    node="static_scan",
                    kind="tool",
                    tool="static_scan",
                    detail=f"candidates={len(candidates)} rules={rules_hit}",
                    elapsed_ms=elapsed,
                )
            ],
            "steps": 1,
        }

    return static_scan_node


async def supervisor_node(state: PRReviewState) -> dict:
    """三个维度始终全量派发。

    静态候选只用于给各专家排序上下文，不用于跳过专家：
    专家并行执行，多跑两路几乎不增加耗时，覆盖率优先于 token 成本。
    """
    categories = sorted({issue.category for issue in state.get("candidates", [])})
    reason = f"candidates={categories}" if categories else "no candidates"
    return {
        "dispatch": list(ALL_AGENTS),
        "trace": [
            TraceEvent(node="supervisor", kind="route", detail=f"{reason} -> all agents")
        ],
        "steps": 1,
    }


def route_experts(state: PRReviewState) -> list[str]:
    dispatch = state.get("dispatch") or []
    return dispatch or ["aggregate"]
