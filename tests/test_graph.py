import asyncio
from pathlib import Path

from diffguard.graph import review
from diffguard.nodes.aggregate import dedupe
from diffguard.nodes.experts import ExpertFinding, _resolve_rule_id
from diffguard.report import decide_verdict
from diffguard.schemas import Issue, Severity, Verdict

PATCH = (Path(__file__).parent / "fixtures" / "sample.patch").read_text(encoding="utf-8")


def _issue(
    rule_id: str,
    line: int,
    severity: Severity,
    confidence: float = 0.8,
    source="rule",
    category="security",
) -> Issue:
    return Issue(
        rule_id=rule_id,
        category=category,
        severity=severity,
        file="app/db.py",
        start_line=line,
        message="m",
        confidence=confidence,
        source=source,
    )


def test_verdict_blocks_on_p0_p1():
    assert decide_verdict([_issue("A", 1, Severity.P1)]) is Verdict.BLOCK


def test_verdict_comments_on_p2_only():
    assert decide_verdict([_issue("A", 1, Severity.P2)]) is Verdict.COMMENT


def test_verdict_passes_when_only_nits():
    assert decide_verdict([_issue("A", 1, Severity.P3)]) is Verdict.PASS


def test_dedupe_prefers_rule_hit_on_same_key():
    issues = [
        _issue("SEC_SQL_INJECTION", 4, Severity.P0, confidence=0.5),
        _issue("SEC_SQL_INJECTION", 4, Severity.P0, confidence=0.95, source="llm"),
        _issue("PERF_LOOP_IN_QUERY", 2, Severity.P2),
    ]
    result = dedupe(issues)
    assert len(result) == 2
    assert result[0].rule_id == "SEC_SQL_INJECTION"
    assert result[0].source == "rule"
    assert result[1].rule_id == "PERF_LOOP_IN_QUERY"


def test_dedupe_prefers_rule_hit_regardless_of_order():
    issues = [
        _issue("SEC_SQL_INJECTION", 4, Severity.P0, confidence=0.95, source="llm"),
        _issue("SEC_SQL_INJECTION", 4, Severity.P0, confidence=0.5),
    ]
    result = dedupe(issues)
    assert len(result) == 1
    assert result[0].source == "rule"


def test_dedupe_confidence_breaks_ties_within_same_source():
    issues = [
        _issue("SEC_LLM_FINDING", 4, Severity.P2, confidence=0.6, source="llm"),
        _issue("SEC_LLM_FINDING", 4, Severity.P2, confidence=0.9, source="llm"),
    ]
    result = dedupe(issues)
    assert len(result) == 1
    assert result[0].confidence == 0.9


def test_dedupe_keeps_generic_finding_without_rule_hit():
    result = dedupe([_issue("SEC_LLM_FINDING", 4, Severity.P2, source="llm")])
    assert len(result) == 1
    assert result[0].rule_id == "SEC_LLM_FINDING"


def _finding(related: str | None, line: int = 4) -> ExpertFinding:
    return ExpertFinding(
        rule_id="SEC_LLM_FINDING",
        related_rule_id=related,
        file="app/db.py",
        line=line,
        severity=Severity.P0,
        message="m",
    )


def test_declared_link_reuses_candidate_rule_id():
    candidates = [_issue("SEC_SQL_INJECTION", 4, Severity.P0)]
    assert _resolve_rule_id(_finding("SEC_SQL_INJECTION"), "security", candidates) == "SEC_SQL_INJECTION"


def test_declared_link_ignored_when_line_differs():
    candidates = [_issue("SEC_SQL_INJECTION", 5, Severity.P0)]
    assert _resolve_rule_id(_finding("SEC_SQL_INJECTION"), "security", candidates) == "SEC_LLM_FINDING"


def test_declared_link_ignored_when_not_in_candidates():
    candidates = [_issue("PERF_LOOP_IN_QUERY", 4, Severity.P2)]
    assert _resolve_rule_id(_finding("SEC_SQL_INJECTION"), "security", candidates) == "SEC_LLM_FINDING"


def test_end_to_end_without_llm():
    report = asyncio.run(review(PATCH, pr_title="test", use_llm=False))
    assert report.verdict is Verdict.BLOCK
    assert {i.rule_id for i in report.issues} >= {
        "SEC_SQL_INJECTION",
        "SEC_HARDCODED_SECRET",
        "SEC_EVAL_EXEC",
    }
    assert report.stats["steps"] >= 4
