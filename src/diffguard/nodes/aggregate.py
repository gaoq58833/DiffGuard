"""汇总层：去重、等级归序、给出确定性结论。

这里刻意不调用模型：结论必须可复现，否则门禁指标无法归因。
"""

from __future__ import annotations

import time

from ..report import decide_verdict
from ..schemas import Issue, TraceEvent
from ..state import PRReviewState


def dedupe(issues: list[Issue]) -> list[Issue]:
    """以 文件 + 起始行 + 规则标识 为唯一键。

    同键时规则命中优先：规则是确定性判定，模型只是拿到 diff 后的二次筛查。
    模型若确认自己描述的是某条候选，会在产出时复用该候选的 rule_id
    （见 experts._resolve_rule_id），因此同键即同一条问题，不做位置猜测。
    同一来源内再比置信度，平手保留先到的。
    """
    best: dict[tuple[str, int, str], Issue] = {}
    for issue in issues:
        key = issue.dedup_key
        current = best.get(key)
        if current is None:
            best[key] = issue
            continue
        if current.source != issue.source:
            if issue.source == "rule":
                best[key] = issue
            continue
        if issue.confidence > current.confidence:
            best[key] = issue
    return sorted(best.values(), key=lambda i: (i.severity.rank, i.file, i.start_line))


def aggregate_node(state: PRReviewState) -> dict:
    started = time.perf_counter()
    raw = state.get("issues", [])
    final = dedupe(raw)
    verdict = decide_verdict(final)
    elapsed = int((time.perf_counter() - started) * 1000)

    stats = {
        "raw_issues": len(raw),
        "deduped_issues": len(raw) - len(final),
        "final_issues": len(final),
        "p0": sum(1 for i in final if i.severity.value == "P0"),
        "p1": sum(1 for i in final if i.severity.value == "P1"),
        "p2": sum(1 for i in final if i.severity.value == "P2"),
        "p3": sum(1 for i in final if i.severity.value == "P3"),
        "steps": state.get("steps", 0) + 1,  # 含汇总节点自身
    }
    return {
        "final_issues": final,
        "final_verdict": verdict.value,
        "stats": stats,
        "trace": [
            TraceEvent(
                node="aggregate",
                kind="aggregate",
                detail=f"verdict={verdict.value} stats={stats}",
                elapsed_ms=elapsed,
            )
        ],
        "steps": 1,
    }
