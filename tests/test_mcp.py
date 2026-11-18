import asyncio

from diffguard import mcp_server
from diffguard.mcp_server import parse_diff, scan_blocking_io, scan_complexity, scan_rules

PATCH = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -1,3 +1,6 @@
 import sqlite3
+import requests
 
+def fetch(conn, uid):
+    return conn.execute("SELECT * FROM users WHERE id = " + uid)
 
 def ping():
     return 1
"""

ASYNC_PATCH = """diff --git a/app/worker.py b/app/worker.py
--- a/app/worker.py
+++ b/app/worker.py
@@ -1,2 +1,5 @@
 import time
 
+async def tick():
+    time.sleep(1)
+
"""

COMPLEX_PATCH = """diff --git a/app/calc.py b/app/calc.py
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,1 +1,12 @@
+def calc(a, b, c):
+    if a:
+        if b:
+            if c:
+                return 1
+            return 2
+        return 3
+    if b:
+        if c:
+            return 4
+        return 5
+    return 6
"""


def test_tools_are_registered():
    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert names == {"parse_diff", "scan_rules", "scan_complexity", "scan_blocking_io"}


def test_parse_diff_returns_files_and_hunks():
    result = parse_diff(PATCH)
    assert len(result["files"]) == 1
    assert result["files"][0]["path"] == "app/db.py"
    assert result["files"][0]["added_lines"] == 3


def test_scan_rules_reuses_rule_engine():
    hits = {issue["rule_id"] for issue in scan_rules(PATCH)}
    assert "SEC_SQL_INJECTION" in hits


def test_scan_blocking_io_flags_async_sleep():
    hits = scan_blocking_io(ASYNC_PATCH)
    assert [issue["rule_id"] for issue in hits] == ["PERF_BLOCKING_IO_IN_ASYNC"]


def test_scan_complexity_respects_threshold():
    assert scan_complexity(COMPLEX_PATCH, threshold=1)
    assert scan_complexity(COMPLEX_PATCH, threshold=50) == []
