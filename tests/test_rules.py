from pathlib import Path

import pytest

from diffguard.tools.ast_scan import scan_blocking_io
from diffguard.tools.diff import parse_diff
from diffguard.tools.rules import scan_rules

PATCH = (Path(__file__).parent / "fixtures" / "sample.patch").read_text(encoding="utf-8")


@pytest.fixture()
def issues():
    out = []
    for fd in parse_diff(PATCH):
        out.extend(scan_rules(fd))
    return out


def _hits(issues, rule_id):
    return sorted(i.start_line for i in issues if i.rule_id == rule_id)


def test_detects_sql_injection_at_construction_line(issues):
    assert _hits(issues, "SEC_SQL_INJECTION") == [4]


def test_detects_hardcoded_secret(issues):
    assert _hits(issues, "SEC_HARDCODED_SECRET") == [7]


def test_detects_eval(issues):
    assert _hits(issues, "SEC_EVAL_EXEC") == [9]


def test_parameterized_query_is_not_flagged(issues):
    safe_hits = [i for i in issues if i.file == "app/safe.py"]
    assert safe_hits == []


def test_secret_evidence_is_masked(issues):
    secret = next(i for i in issues if i.rule_id == "SEC_HARDCODED_SECRET")
    assert "sk-live-1234567890abcdef" not in (secret.evidence or "")
    assert "<REDACTED>" in (secret.evidence or "")


def test_only_added_lines_are_reported(issues):
    assert all(i.start_line >= 1 for i in issues)


STYLE_PATCH = """diff --git a/app/style.py b/app/style.py
--- a/app/style.py
+++ b/app/style.py
@@ -1,1 +1,17 @@
 import asyncio
+import time
+import requests
+from os import *
+
+def load(items=[]):
+    try:
+        pass
+    except:
+        breakpoint()
+    for i in range(len(items)):
+        print(i)
+
+async def fetch():
+    time.sleep(1)
+    return requests.get("http://example.com")
+"""

SYNC_IO_PATCH = """diff --git a/app/sync_io.py b/app/sync_io.py
--- a/app/sync_io.py
+++ b/app/sync_io.py
@@ -1,1 +1,6 @@
 import requests
+import time
+
+def fetch_sync():
+    time.sleep(1)
+    return requests.get("http://example.com")
+"""


def _scan_all(patch):
    out = []
    for fd in parse_diff(patch):
        out.extend(scan_rules(fd))
        out.extend(scan_blocking_io(fd))
    return out


def test_style_rules():
    issues = _scan_all(STYLE_PATCH)
    assert _hits(issues, "STY_WILDCARD_IMPORT") == [4]
    assert _hits(issues, "STY_MUTABLE_DEFAULT_ARG") == [6]
    assert _hits(issues, "STY_BARE_EXCEPT") == [9]
    assert _hits(issues, "STY_DEBUG_BREAKPOINT") == [10]


def test_perf_rules():
    issues = _scan_all(STYLE_PATCH)
    assert _hits(issues, "PERF_RANGE_LEN") == [11]
    assert _hits(issues, "PERF_BLOCKING_IO_IN_ASYNC") == [15, 16]


def test_sync_io_outside_async_is_not_flagged():
    issues = _scan_all(SYNC_IO_PATCH)
    assert _hits(issues, "PERF_BLOCKING_IO_IN_ASYNC") == []
