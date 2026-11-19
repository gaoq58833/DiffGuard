"""核心数据契约。

设计约束：
1. 所有中间结论都以强类型字段暴露，不埋在自然语言里，便于评测直接断言。
2. 严重度只有一套枚举 P0~P3，渲染时映射为 Blocker/Critical/Major/Nit，
   对外的 CRITICAL/HIGH/MEDIUM/LOW 一律视为同一个东西的别名。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["security", "style", "perf"]


class Severity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

    @property
    def label(self) -> str:
        return _SEVERITY_LABEL[self]

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_LABEL: dict[Severity, str] = {
    Severity.P0: "Blocker",
    Severity.P1: "Critical",
    Severity.P2: "Major",
    Severity.P3: "Nit",
}
_SEVERITY_RANK: dict[Severity, int] = {s: i for i, s in enumerate(Severity)}


class Verdict(str, Enum):
    PASS = "pass"
    COMMENT = "comment"
    BLOCK = "block"

    @property
    def label(self) -> str:
        return {"pass": "放行", "comment": "评论", "block": "阻断"}[self.value]


class Issue(BaseModel):
    """一条审查结论。file + start_line + rule_id 是全局去重键。"""

    rule_id: str
    category: Category
    severity: Severity
    file: str
    start_line: int = Field(ge=1)
    end_line: int | None = None
    message: str
    suggestion: str = ""
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    source: Literal["rule", "llm"] = "rule"
    evidence: str | None = None

    @property
    def dedup_key(self) -> tuple[str, int, str]:
        return (self.file, self.start_line, self.rule_id)

    @property
    def location(self) -> str:
        return f"{self.file}:{self.start_line}"


class TraceEvent(BaseModel):
    """轨迹组：工具与节点调用记录，供工具 F1、并行度、绕路检测使用。"""

    node: str
    kind: Literal["parse", "tool", "route", "llm", "skip", "error", "aggregate"]
    detail: str = ""
    tool: str | None = None
    elapsed_ms: int = 0


class ExpertResult(BaseModel):
    agent: str
    status: Literal["ok", "skipped", "error"]
    issue_count: int = 0
    note: str = ""


class ReviewReport(BaseModel):
    verdict: Verdict
    issues: list[Issue] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)
    trace: list[TraceEvent] = Field(default_factory=list)
    summary: str = ""  # 汇总 Agent 生成的用户评论
