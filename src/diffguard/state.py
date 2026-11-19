"""LangGraph 全局状态。

单一状态源：State 只有一份，由编排层持有；子 Agent 是纯函数，
只返回增量（issues / trace / steps），合并交给 reducer 完成。
这样并行分支不会互相覆写，也不会出现“后写覆盖前写”。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from .schemas import ExpertResult, Issue, TraceEvent
from .tools.diff import FileDiff


class PRReviewState(TypedDict, total=False):
    # 输入组
    pr_title: str
    diff: str
    files: list[FileDiff]

    # 候选组（静态初筛，与 Agent 结论分开存放）
    candidates: list[Issue]

    # 控制组
    dispatch: list[str]
    steps: Annotated[int, operator.add]

    # 结论组
    issues: Annotated[list[Issue], operator.add]
    expert_results: Annotated[list[ExpertResult], operator.add]
    # 汇总节点是唯一写入 final_* 的位置；issues 带 reducer，不能在此覆写
    final_issues: list[Issue]
    final_verdict: str

    # 轨迹组
    trace: Annotated[list[TraceEvent], operator.add]

    # 出口：仅汇总节点写入
    stats: dict[str, int]
    summary: str  # 汇总 Agent 生成的用户评论；verdict 仍由代码计算
