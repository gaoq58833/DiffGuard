"""专家层：安全 / 规范 / 性能三个单职责 Agent。

三者共享同一骨架，只在系统提示、判定边界与上下文筛选上不同。
关键约束：
- Agent 是纯函数，只返回增量（issues / trace / steps），不持有状态；
- 只返回结构化字段，不返回自由长文；
- 没有 API Key 时优雅降级为 skipped，保证骨架离线可跑。
"""

from __future__ import annotations

import re
import time

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..config import Settings
from ..models import ModelFactory
from ..prompts import load_prompt
from ..schemas import ExpertResult, Issue, Severity, TraceEvent
from ..state import PRReviewState
from ..tools.diff import FileDiff
from ..tools.redact import redact_secrets
from ..tools.rules import LINE_RULES

_EXTRA_RULE_IDS = {
    "SEC_SQL_INJECTION",
    "PERF_LOOP_IN_QUERY",
    "PERF_BLOCKING_IO_IN_ASYNC",
    "STY_COMPLEX_FUNC",
}
KNOWN_RULE_IDS = {r.rule_id for r in LINE_RULES} | _EXTRA_RULE_IDS
_RULE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")

EXPERT_CATEGORY = {"security": "security", "style": "style", "perf": "perf"}
_PROMPT_FILE = {"security": "security.md", "style": "style.md", "perf": "perf.md"}
_MAX_LINES_PER_FILE = 200


class ExpertFinding(BaseModel):
    """模型侧输出。字段固定，规则打分器可直接读取。"""

    rule_id: str = Field(description="规则标识；命不中已有规则时用 <类别>_LLM_FINDING")
    related_rule_id: str | None = Field(
        default=None,
        description="若本条描述的就是静态候选中的某一条，填该候选的 rule_id；否则留空",
    )
    file: str
    line: int
    severity: Severity
    message: str
    suggestion: str = ""
    confidence: float = 0.6


class ExpertResponse(BaseModel):
    findings: list[ExpertFinding] = Field(default_factory=list)
    summary: str = ""


def _estimate_tokens(text: str) -> int:
    # 粗估：代码约 3 字符 / token
    return max(1, len(text) // 3)


def _render_file(fd: FileDiff) -> str:
    lines = [f"### {fd.path} ({fd.language})"]
    count = 0
    for hunk in fd.hunks:
        lines.append(hunk.header)
        for ln in hunk.lines:
            if count >= _MAX_LINES_PER_FILE:
                lines.append("... (truncated)")
                return "\n".join(lines)
            no = ln.new_lineno if ln.new_lineno is not None else ln.old_lineno
            lines.append(f"{no:>5} {ln.kind} {ln.text}")
            count += 1
    return "\n".join(lines)


def build_context(state: PRReviewState, agent: str, budget: int) -> tuple[str, list[str]]:
    """只给当前 Diff 的代码块与同维度静态候选，不给整仓上下文。"""
    category = EXPERT_CATEGORY[agent]
    files = [f for f in state.get("files", []) if f.hunks and not f.is_binary and not f.is_deleted]
    hinted = {i.file for i in state.get("candidates", []) if i.category == category}
    ordered = sorted(files, key=lambda f: (f.path not in hinted, f.path))

    blocks: list[str] = []
    dropped: list[str] = []
    used = 0
    for fd in ordered:
        block = _render_file(fd)
        cost = _estimate_tokens(block)
        if blocks and used + cost > budget:
            dropped.append(fd.path)
            continue
        blocks.append(block)
        used += cost

    candidates = [c for c in state.get("candidates", []) if c.category == category]
    if candidates:
        rendered = "\n".join(
            f"- {c.rule_id} {c.location} [{c.severity.value}] {c.message}" for c in candidates[:20]
        )
        blocks.append("### 静态初筛候选（仅供参考，需你确认或否定）\n" + rendered)

    header = f"PR 标题：{state.get('pr_title', '')}\n\n"
    return header + "\n\n".join(blocks), dropped


_CATEGORY_PREFIX = {"security": "SEC", "style": "STY", "perf": "PERF"}


def _resolve_rule_id(finding: ExpertFinding, category: str, candidates: list[Issue]) -> str:
    """模型声明「我与某条候选是同一问题」时复用候选的 rule_id，让去重按同一键收敛。

    只认同一文件、同一行的声明；对不上就退回通用 ID——宁可重复上报，
    也不把两条不同的问题错并成一条。
    """
    linked = (finding.related_rule_id or "").strip().upper()
    if linked:
        line = max(finding.line, 1)
        for cand in candidates:
            if cand.rule_id == linked and cand.file == finding.file and cand.start_line == line:
                return linked
    return _normalize_rule_id(finding.rule_id, category)


def _normalize_rule_id(raw: str, category: str) -> str:
    candidate = (raw or "").strip().upper()
    if candidate in KNOWN_RULE_IDS:
        return candidate
    if _RULE_ID_RE.match(candidate):
        return candidate
    return f"{_CATEGORY_PREFIX.get(category, 'GEN')}_LLM_FINDING"


def _is_grounded(finding: ExpertFinding, files: list[FileDiff]) -> bool:
    """幻觉拦截：文件必须存在于输入，行号必须落在本次改动的行范围内。"""
    target = next((f for f in files if f.path == finding.file), None)
    if target is None:
        return False
    added = target.added_linenos()
    if not added:
        return False
    return min(added) - 20 <= finding.line <= max(added) + 20


def make_expert_node(agent: str, settings: Settings, factory: ModelFactory | None):
    category = EXPERT_CATEGORY[agent]
    prompt_name = _PROMPT_FILE[agent]

    async def expert_node(state: PRReviewState) -> dict:
        started = time.perf_counter()
        llm = factory.get(agent) if factory else None
        if llm is None:
            note = "DASHSCOPE_API_KEY 未配置，跳过模型审查（仅规则模式）"
            return {
                "expert_results": [ExpertResult(agent=agent, status="skipped", note=note)],
                "trace": [TraceEvent(node=agent, kind="skip", detail=note)],
                "steps": 1,
            }

        context, dropped = build_context(state, agent, settings.budget.per_agent_context_tokens)
        safe_context, redacted = redact_secrets(context)

        try:
            structured = llm.with_structured_output(ExpertResponse, method="function_calling")
            response = await structured.ainvoke(
                [SystemMessage(content=load_prompt(prompt_name)), HumanMessage(content=safe_context)]
            )
        except Exception as exc:  # 外部服务边界，失败降级而不是打断整条流水线
            return {
                "expert_results": [
                    ExpertResult(agent=agent, status="error", note=f"{type(exc).__name__}: {exc}")
                ],
                "trace": [TraceEvent(node=agent, kind="error", detail=str(exc)[:200])],
                "steps": 1,
            }

        if response is None:  # 模型未发起 tool call 时，结构化解析会返回 None
            elapsed = int((time.perf_counter() - started) * 1000)
            note = "模型未返回结构化结果（空响应）"
            return {
                "expert_results": [ExpertResult(agent=agent, status="error", note=note)],
                "trace": [
                    TraceEvent(
                        node=agent,
                        kind="error",
                        detail=f"{note} model={settings.model(agent).name}",
                        elapsed_ms=elapsed,
                    )
                ],
                "steps": 1,
            }

        files = state.get("files", [])
        candidates = [i for i in state.get("candidates", []) if i.category == category]
        issues: list[Issue] = []
        dropped_hallucination = 0
        for finding in response.findings:
            if not _is_grounded(finding, files):
                dropped_hallucination += 1
                continue
            issues.append(
                Issue(
                    rule_id=_resolve_rule_id(finding, category, candidates),
                    category=category,  # type: ignore[arg-type]
                    severity=finding.severity,
                    file=finding.file,
                    start_line=max(finding.line, 1),
                    message=finding.message,
                    suggestion=finding.suggestion,
                    confidence=finding.confidence,
                    source="llm",
                )
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        detail = (
            f"issues={len(issues)} dropped_ungrounded={dropped_hallucination} "
            f"redacted={redacted} model={settings.model(agent).name}"
        )
        if dropped:
            detail += f" truncated_files={dropped[:3]}"
        return {
            "issues": issues,
            "expert_results": [
                ExpertResult(agent=agent, status="ok", issue_count=len(issues), note=response.summary[:200])
            ],
            "trace": [
                TraceEvent(node=agent, kind="llm", tool=f"llm:{agent}", detail=detail, elapsed_ms=elapsed)
            ],
            "steps": 1,
        }

    return expert_node
