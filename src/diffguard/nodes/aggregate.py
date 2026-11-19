"""汇总层：去重、等级归序、给出确定性结论，并生成给用户看的评论。

结论（verdict）由代码计算，保证可复现；评论文字（summary）交给模型生成，
模型失败或不可用时降级为代码拼出的固定摘要，不影响结论。
"""

from __future__ import annotations

import time

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import Settings
from ..models import ModelFactory
from ..prompts import load_prompt
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


def _fallback_summary(verdict_label: str, issues: list[Issue]) -> str:
    if not issues:
        return f"审查结论：{verdict_label}。本次未发现需要处理的问题。"
    top = issues[0]
    return (
        f"审查结论：{verdict_label}。共发现 {len(issues)} 条问题，"
        f"建议优先处理 {top.location} 的 {top.rule_id}（{top.severity.value}）。"
    )


def _render_for_summary(verdict_label: str, issues: list[Issue]) -> str:
    lines = [f"系统判定结论：{verdict_label}", f"最终问题数：{len(issues)}", ""]
    for i in issues[:30]:
        lines.append(
            f"- [{i.severity.value}] {i.rule_id} {i.location}（{i.category}）"
            f"{i.message} 建议：{i.suggestion or '-'}"
        )
    return "\n".join(lines)


def make_aggregate(settings: Settings, factory: ModelFactory | None):
    async def aggregate_node(state: PRReviewState) -> dict:
        started = time.perf_counter()
        raw = state.get("issues", [])
        final = dedupe(raw)
        verdict = decide_verdict(
            final, p1_block_threshold=settings.verdict.p1_block_threshold
        )
        summary = _fallback_summary(verdict.label, final)

        llm = factory.get("aggregate") if factory else None
        if llm is not None:
            try:
                response = await llm.ainvoke(
                    [
                        SystemMessage(content=load_prompt("aggregate.md")),
                        HumanMessage(content=_render_for_summary(verdict.label, final)),
                    ]
                )
                text = (response.content or "").strip()
                if isinstance(text, str) and text:
                    summary = text
            except Exception:  # 摘要失败不影响结论，降级为代码摘要
                pass

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
            "summary": summary,
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

    return aggregate_node
