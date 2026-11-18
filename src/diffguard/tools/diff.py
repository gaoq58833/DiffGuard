"""统一 Diff 解析。

要点：
- 行号以 hunk header 的起始值为准，不靠自增猜；
- 只对新增行报问题，但保留上下文行供 AST 分析与多行模式匹配；
- 二进制、重命名、删除文件、无换行结尾等真实 PR 里常见的形态都要能安全跳过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")

LANG_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
    ".rs": "rust",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".sql": "sql",
}


@dataclass
class HunkLine:
    kind: Literal["+", "-", " "]
    text: str
    old_lineno: int | None
    new_lineno: int | None


@dataclass
class Hunk:
    header: str
    lines: list[HunkLine] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str = ""
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False
    is_renamed: bool = False
    is_deleted: bool = False

    @property
    def language(self) -> str:
        return LANG_BY_SUFFIX.get(PurePosixPath(self.path).suffix.lower(), "unknown")

    @property
    def added_lines(self) -> list[HunkLine]:
        return [ln for hunk in self.hunks for ln in hunk.lines if ln.kind == "+"]

    @property
    def context_lines(self) -> list[HunkLine]:
        """新增 + 上下文，按文件顺序；用于 AST 与多行规则。"""
        return [ln for hunk in self.hunks for ln in hunk.lines if ln.kind != "-"]

    def added_linenos(self) -> set[int]:
        return {ln.new_lineno for ln in self.added_lines if ln.new_lineno is not None}


def flatten(hunk: Hunk) -> tuple[str, list[int | None]]:
    """把 hunk 拼成一段文本，并返回每行对应的新文件行号（删除行记 None）。

    删除行用空字符串占位，保证行序与行号映射不乱，同时避免把旧代码当成新代码扫描。
    """
    parts: list[str] = []
    linenos: list[int | None] = []
    for line in hunk.lines:
        parts.append(line.text if line.kind != "-" else "")
        linenos.append(line.new_lineno)
    return "\n".join(parts), linenos


def lineno_at(text: str, offset: int, linenos: list[int | None]) -> int | None:
    idx = text.count("\n", 0, max(offset, 0))
    if idx >= len(linenos):
        idx = len(linenos) - 1
    return linenos[idx] if idx >= 0 else None


def parse_diff(diff_text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0

    for raw in diff_text.splitlines():
        if GIT_HEADER_RE.match(raw):
            old_path, new_path = GIT_HEADER_RE.match(raw).groups()  # type: ignore[union-attr]
            current = FileDiff(path=new_path, old_path=old_path)
            files.append(current)
            hunk = None
            continue

        m = HUNK_RE.match(raw)
        if m:
            if current is None:
                current = FileDiff(path="unknown")
                files.append(current)
            old_no = int(m.group(1))
            new_no = int(m.group(3))
            hunk = Hunk(header=raw)
            current.hunks.append(hunk)
            continue

        if current is None:
            continue

        if raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
            current.is_binary = True
            continue
        if raw.startswith("rename from "):
            current.is_renamed = True
            current.old_path = raw[len("rename from ") :]
            continue
        if raw.startswith("rename to "):
            current.is_renamed = True
            current.path = raw[len("rename to ") :]
            continue
        if raw.startswith("deleted file mode"):
            current.is_deleted = True
            continue
        if raw.startswith("--- ") and hunk is None:
            if current.path in ("", "unknown") and raw[4:] != "/dev/null":
                current.old_path = raw[4:].removeprefix("a/")
            continue
        if raw.startswith("+++ ") and hunk is None:
            if raw[4:] == "/dev/null":
                current.is_deleted = True
            elif current.path in ("", "unknown"):
                current.path = raw[4:].removeprefix("b/")
            continue
        if raw.startswith("\\ No newline at end of file"):
            continue

        if hunk is None:
            continue

        if raw.startswith("+"):
            hunk.lines.append(HunkLine("+", raw[1:], None, new_no))
            new_no += 1
        elif raw.startswith("-"):
            hunk.lines.append(HunkLine("-", raw[1:], old_no, None))
            old_no += 1
        elif raw.startswith(" ") or raw == "":
            hunk.lines.append(HunkLine(" ", raw[1:] if raw else "", old_no, new_no))
            old_no += 1
            new_no += 1

    return [f for f in files if f.hunks or f.is_binary or f.is_deleted]
