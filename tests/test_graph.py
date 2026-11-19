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


def test_verdict_p0_always_blocks_regardless_of_threshold():
    issues = [_issue("A", 1, Severity.P0)]
    assert decide_verdict(issues, p1_block_threshold=99) is Verdict.BLOCK


def test_verdict_p1_below_threshold_degrades_to_comment():
    issues = [_issue("A", 1, Severity.P1)]
    assert decide_verdict(issues, p1_block_threshold=2) is Verdict.COMMENT


def test_verdict_p1_at_threshold_blocks():
    issues = [_issue("A", 1, Severity.P1), _issue("B", 2, Severity.P1)]
    assert decide_verdict(issues, p1_block_threshold=2) is Verdict.BLOCK


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


def _finding(rule_id: str, line: int = 4) -> ExpertFinding:
    return ExpertFinding(
        rule_id=rule_id,
        file="app/db.py",
        line=line,
        severity=Severity.P0,
        message="m",
    )


def test_known_rule_id_reused_when_candidate_hit():
    candidates = [_issue("SEC_SQL_INJECTION", 4, Severity.P0)]
    assert _resolve_rule_id(_finding("SEC_SQL_INJECTION"), "security", candidates) == "SEC_SQL_INJECTION"


def test_known_rule_id_falls_back_when_not_in_candidates():
    candidates = [_issue("PERF_LOOP_IN_QUERY", 4, Severity.P2)]
    assert _resolve_rule_id(_finding("SEC_SQL_INJECTION"), "security", candidates) == "SEC_LLM_FINDING"


def test_known_rule_id_falls_back_when_no_candidates():
    assert _resolve_rule_id(_finding("SEC_SQL_INJECTION"), "security", []) == "SEC_LLM_FINDING"


def test_unknown_but_well_formed_rule_id_kept():
    assert _resolve_rule_id(_finding("SEC_SSRF"), "security", []) == "SEC_SSRF"


def test_end_to_end_without_llm():
    report = asyncio.run(review(PATCH, pr_title="test", use_llm=False))
    assert report.verdict is Verdict.BLOCK
    assert {i.rule_id for i in report.issues} >= {
        "SEC_SQL_INJECTION",
        "SEC_HARDCODED_SECRET",
        "SEC_EVAL_EXEC",
    }
    assert report.stats["steps"] >= 4
