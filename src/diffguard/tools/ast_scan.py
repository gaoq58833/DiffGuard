"""AST 通道：只做正则做不了的复杂度类分析。

与正则通道相互独立：解析失败的 hunk 直接跳过，不中断整个审查。
当前只覆盖 Python；其它语言返回空，等 M2 再接 tree-sitter。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from ..schemas import Issue, Severity
from .diff import FileDiff

_DECISION_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.Assert)


class _ComplexityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.count = 1

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, _DECISION_NODES):
            self.count += 1
        elif isinstance(node, ast.BoolOp):
            self.count += max(len(node.values) - 1, 1)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            self.count += sum(len(gen.ifs) for gen in node.generators)
        super().generic_visit(node)


@dataclass
class _FuncSpan:
    name: str
    lineno: int
    end_lineno: int
    complexity: int


def _functions(tree: ast.AST) -> list[_FuncSpan]:
    spans: list[_FuncSpan] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        visitor = _ComplexityVisitor()
        for child in node.body:
            visitor.visit(child)
        spans.append(
            _FuncSpan(
                name=node.name,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", node.lineno),
                complexity=visitor.count,
            )
        )
    return spans


def scan_complexity(fd: FileDiff, threshold: int) -> list[Issue]:
    if fd.language != "python" or fd.is_binary or fd.is_deleted:
        return []

    issues: list[Issue] = []
    added = fd.added_linenos()
    seen: set[tuple[str, int]] = set()

    for hunk in fd.hunks:
        source_lines: list[str] = []
        linenos: list[int | None] = []
        for line in hunk.lines:
            if line.kind == "-":
                continue
            source_lines.append(line.text)
            linenos.append(line.new_lineno)

        source = "\n".join(source_lines)
        if not source.strip():
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # 局部代码块不完整是常态，跳过而不是报错
            continue

        for span in _functions(tree):
            if span.complexity <= threshold:
                continue
            # 只有本次改动真正触及该函数时才报，避免存量问题刷屏
            touched = [
                linenos[i]
                for i in range(span.lineno - 1, min(span.end_lineno, len(linenos)))
                if linenos[i] in added
            ]
            if not touched:
                continue
            lineno = touched[0] or 1
            if (span.name, lineno) in seen:
                continue
            seen.add((span.name, lineno))
            issues.append(
                Issue(
                    rule_id="STY_COMPLEX_FUNC",
                    category="style",
                    severity=Severity.P3,
                    file=fd.path,
                    start_line=lineno,
                    message=f"函数 {span.name} 圈复杂度约 {span.complexity}，超过阈值 {threshold}。",
                    suggestion="按职责拆分函数，或把分支映射为查表/策略对象以降低嵌套。",
                    confidence=0.8,
                    source="rule",
                    evidence=f"cyclomatic≈{span.complexity}",
                )
            )

    return issues


_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "request")
_BLOCKING_CALLS = {
    "time.sleep",
    "urllib.request.urlopen",
    *(f"requests.{m}" for m in _HTTP_METHODS),
    *(f"httpx.{m}" for m in _HTTP_METHODS),
}


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


class _AsyncBlockingVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            name = _dotted_name(child.func)
            if name in _BLOCKING_CALLS:
                self.hits.append((child.lineno, name))
        self.generic_visit(node)


def scan_blocking_io(fd: FileDiff) -> list[Issue]:
    """async 函数内调用同步阻塞 API 会卡住事件循环；同步函数不管。"""
    if fd.language != "python" or fd.is_binary or fd.is_deleted:
        return []

    issues: list[Issue] = []
    added = fd.added_linenos()
    seen: set[tuple[str, int]] = set()

    for hunk in fd.hunks:
        source_lines = [line.text for line in hunk.lines if line.kind != "-"]
        linenos = [line.new_lineno for line in hunk.lines if line.kind != "-"]
        source = "\n".join(source_lines)
        if not source.strip():
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        visitor = _AsyncBlockingVisitor()
        visitor.visit(tree)
        for local_lineno, call in visitor.hits:
            if not 1 <= local_lineno <= len(linenos):
                continue
            lineno = linenos[local_lineno - 1]
            if lineno is None or lineno not in added:
                continue
            if ("PERF_BLOCKING_IO_IN_ASYNC", lineno) in seen:
                continue
            seen.add(("PERF_BLOCKING_IO_IN_ASYNC", lineno))
            issues.append(
                Issue(
                    rule_id="PERF_BLOCKING_IO_IN_ASYNC",
                    category="perf",
                    severity=Severity.P2,
                    file=fd.path,
                    start_line=lineno,
                    message=f"async 函数内调用同步阻塞的 {call}()，会卡住整个事件循环。",
                    suggestion="改用异步实现：asyncio.sleep / httpx.AsyncClient / aiohttp。",
                    confidence=0.85,
                    source="rule",
                    evidence=f"{call}()",
                )
            )

    return issues
