"""图编排：固定拓扑 + 动态并行派发。

START -> parse_diff -> static_scan -> supervisor
                                        ├─(并行) security_agent ─┐
                                        ├─(并行) style_agent    ─┼-> aggregate -> END
                                        └─(并行) perf_agent     ─┘
候选为空时 supervisor 直接收敛到 aggregate。

入口是 async 的：三个专家以 asyncio 任务并发，等待模型返回时不占用线程。
"""

from __future__ import annotations

import asyncio

from langgraph.graph import END, START, StateGraph

from .config import Settings, load_settings
from .models import ModelFactory
from .nodes.aggregate import aggregate_node
from .nodes.experts import make_expert_node
from .nodes.pipeline import make_static_scan, parse_diff_node, route_experts, supervisor_node
from .schemas import ReviewReport, Verdict
from .state import PRReviewState

EXPERT_AGENTS = ("security", "style", "perf")
EXPERT_NODES = tuple(f"{agent}_agent" for agent in EXPERT_AGENTS)


def build_graph(settings: Settings, *, use_llm: bool = True, checkpointer=None):
    factory = ModelFactory(settings) if use_llm else None

    graph = StateGraph(PRReviewState)
    graph.add_node("parse_diff", parse_diff_node)
    graph.add_node("static_scan", make_static_scan(settings))
    graph.add_node("supervisor", supervisor_node)
    for agent in EXPERT_AGENTS:
        graph.add_node(f"{agent}_agent", make_expert_node(agent, settings, factory))
    graph.add_node("aggregate", aggregate_node)

    graph.add_edge(START, "parse_diff")
    graph.add_edge("parse_diff", "static_scan")
    graph.add_edge("static_scan", "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_experts,
        [*EXPERT_NODES, "aggregate"],
    )
    for node in EXPERT_NODES:
        graph.add_edge(node, "aggregate")
    graph.add_edge("aggregate", END)

    return graph.compile(checkpointer=checkpointer)


def _initial_state(diff_text: str, pr_title: str) -> dict:
    return {
        "pr_title": pr_title,
        "diff": diff_text,
        "issues": [],
        "trace": [],
        "expert_results": [],
        "steps": 0,
    }


async def run_graph(
    model_graph,
    diff_text: str,
    pr_title: str = "",
    settings: Settings | None = None,
) -> ReviewReport:
    """在已编译的图上跑一次审查。服务层复用同一张图，不必每次请求重建。"""
    settings = settings or load_settings()
    async with asyncio.timeout(settings.budget.task_timeout_seconds):
        state = await model_graph.ainvoke(
            _initial_state(diff_text, pr_title),
            config={"recursion_limit": 50},
        )
    return ReviewReport(
        verdict=Verdict(state.get("final_verdict", "pass")),
        issues=state.get("final_issues", []),
        stats=state.get("stats", {}),
        trace=state.get("trace", []),
    )


async def review(
    diff_text: str,
    pr_title: str = "",
    settings: Settings | None = None,
    use_llm: bool = True,
) -> ReviewReport:
    settings = settings or load_settings()
    graph = build_graph(settings, use_llm=use_llm)
    return await run_graph(graph, diff_text, pr_title, settings)
