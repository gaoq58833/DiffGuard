"""静态初筛规则引擎。

设计约束（对应方案的表 4-1）：
- 每条规则有稳定 ID，评测集可直接断言，不依赖自然语言描述；
- 只对新增行报问题；
- 每条规则都配负向守卫，宁可漏报也不制造误报 —— 误报会直接打穿 5% 门禁；
- 命中密钥类问题时，写入报告的 evidence 必须先脱敏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..schemas import Category, Issue, Severity
from .diff import FileDiff
from .redact import redact_secrets

# ---------------------------------------------------------------- 正则定义

_SECRET_KEY = (
    r"(?:api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key|private[_-]?key|auth[_-]?token)"
)
SECRET_RE = re.compile(rf"(?ix)\b{_SECRET_KEY}\b\s*[:=]\s*[\"'][^\"'\s]{{8,}}[\"']")
SECRET_ENV_NEGATIVE = re.compile(
    r"(?i)(os\.getenv|os\.environ|getenv\s*\(|process\.env|System\.getenv|\$\{|%\(|getSecret)"
)

EVAL_EXEC_RE = re.compile(
    r"(?x)"
    r"\b(?:eval|exec)\s*\("
    r"|os\.system\s*\("
    r"|subprocess\.(?:call|run|Popen)\s*\([^)]*shell\s*=\s*True"
    r"|child_process\.exec\s*\("
)

PICKLE_RE = re.compile(r"\b(?:pickle|cPickle)\.loads?\s*\(|\bmarshal\.loads\s*\(|\byaml\.load\s*\(")
PICKLE_SAFE_NEGATIVE = re.compile(
    r"(?i)(SafeLoader|CSafeLoader|Loader\s*=\s*yaml\.(?:Safe|CSafe)Loader)"
)

WEAK_HASH_RE = re.compile(r"hashlib\.(?:md5|sha1)\s*\(|createHash\s*\(\s*[\"'](?:md5|sha1)[\"']", re.I)

PATH_TRAVERSAL_RE = re.compile(
    r"os\.path\.join\s*\([^)]*\brequest\."
    r"|open\s*\(\s*(?:request\.|input\()"
    r"|send_file\s*\(\s*request\."
)

BARE_EXCEPT_RE = re.compile(r"^\s*except\s*:")
MUTABLE_DEFAULT_RE = re.compile(r"\bdef\s+\w+\s*\([^)]*=\s*(?:\[\]|\{\}|set\(\s*\))")
DEBUG_BREAKPOINT_RE = re.compile(
    r"\b(?:breakpoint|pdb\.set_trace|ipdb\.set_trace|pudb\.set_trace)\s*\(\s*\)"
)
WILDCARD_IMPORT_RE = re.compile(r"^\s*from\s+[\w.]+\s+import\s+\*")
RANGE_LEN_RE = re.compile(r"\bfor\s+\w+(?:\s*,\s*\w+)?\s+in\s+range\s*\(\s*len\s*\(")

SQL_KEYWORD_RE = re.compile(r"(?i)\b(?:select|insert|update|delete)\b")
SQL_EXEC_RE = re.compile(r"(?i)\b(?:execute|executemany|executeQuery|executeUpdate|query|raw)\s*\(")
SQL_CONCAT_RE = re.compile(r"(?:\+\s*[A-Za-z_$]|\.format\s*\(|%\s*[\(\{]|\{\s*[A-Za-z_$])")
SQL_FSTRING_RE = re.compile(r"(?i)f[\"'][^\"']*\b(?:select|insert|update|delete)\b")

QUERY_RE = re.compile(
    r"(?i)\.(?:query|filter|get|all|first|execute|find|findOne|findAll)\s*\("
    r"|\b(?:SELECT|INSERT|UPDATE|DELETE)\b"
)


@dataclass(frozen=True)
class LineRule:
    rule_id: str
    category: Category
    severity: Severity
    message: str
    suggestion: str
    pattern: re.Pattern[str]
    negative: re.Pattern[str] | None = None
    langs: tuple[str, ...] | None = None
    mask_evidence: bool = False


LINE_RULES: tuple[LineRule, ...] = (
    LineRule(
        "SEC_HARDCODED_SECRET",
        "security",
        Severity.P1,
        "检测到硬编码的密钥/口令字面量，会随代码库泄露。",
        "改为从环境变量或密钥管理服务读取，并立即轮换已泄露的凭据。",
        SECRET_RE,
        SECRET_ENV_NEGATIVE,
        mask_evidence=True,
    ),
    LineRule(
        "SEC_EVAL_EXEC",
        "security",
        Severity.P1,
        "动态执行或 shell 调用，入参可被操纵时等价于远程命令执行。",
        "改用白名单映射或参数化调用；确需 shell 时使用 shlex.quote 并禁用 shell=True。",
        EVAL_EXEC_RE,
    ),
    LineRule(
        "SEC_PICKLE_LOADS",
        "security",
        Severity.P1,
        "对不可信输入直接反序列化，可导致任意代码执行。",
        "改用 JSON 等纯数据格式；必须用 pickle 时先做签名校验与来源白名单。",
        PICKLE_RE,
        PICKLE_SAFE_NEGATIVE,
    ),
    LineRule(
        "SEC_PATH_TRAVERSAL",
        "security",
        Severity.P1,
        "外部输入直接参与路径拼接或文件读取，存在目录穿越风险。",
        "对路径做规范化后校验其仍位于允许的根目录内，并拒绝 .. 片段。",
        PATH_TRAVERSAL_RE,
    ),
    LineRule(
        "SEC_WEAK_HASH",
        "security",
        Severity.P2,
        "使用 MD5/SHA1 等已被攻破的摘要算法。",
        "口令场景改用 bcrypt/argon2，校验场景改用 SHA-256 及以上。",
        WEAK_HASH_RE,
    ),
    LineRule(
        "STY_BARE_EXCEPT",
        "style",
        Severity.P2,
        "裸 except 会捕获所有异常（含 KeyboardInterrupt / SystemExit），掩盖真实错误。",
        "捕获具体异常类型；确需兜底时写 except Exception 并记录日志。",
        BARE_EXCEPT_RE,
        langs=("python",),
    ),
    LineRule(
        "STY_MUTABLE_DEFAULT_ARG",
        "style",
        Severity.P2,
        "可变对象作默认参数，会在多次调用间共享同一实例，导致状态串味。",
        "默认值改为 None，在函数体内按需初始化。",
        MUTABLE_DEFAULT_RE,
        langs=("python",),
    ),
    LineRule(
        "STY_DEBUG_BREAKPOINT",
        "style",
        Severity.P2,
        "遗留的调试断点会挂起执行，进入生产环境即为故障。",
        "提交前移除断点，改用日志或条件调试。",
        DEBUG_BREAKPOINT_RE,
        langs=("python",),
    ),
    LineRule(
        "STY_WILDCARD_IMPORT",
        "style",
        Severity.P3,
        "通配符导入污染命名空间，且无法静态判断符号来源。",
        "改为显式导入所需名称。",
        WILDCARD_IMPORT_RE,
        langs=("python",),
    ),
    LineRule(
        "PERF_RANGE_LEN",
        "perf",
        Severity.P3,
        "用 range(len(...)) 遍历，每次迭代都重复取长度并做下标访问。",
        "改用 enumerate(items) 或直接遍历元素。",
        RANGE_LEN_RE,
        langs=("python",),
    ),
)


def _scan_sql_injection(text: str) -> bool:
    """单行启发式：SQL 关键字 + 字符串拼接/格式化，且处于赋值或执行上下文。

    已知局限：常量拼接、跨行拼接会误报或漏报，M2 用 AST 数据流补。
    """
    if not SQL_KEYWORD_RE.search(text):
        return False
    if not (SQL_FSTRING_RE.search(text) or SQL_CONCAT_RE.search(text)):
        return False
    return bool(SQL_EXEC_RE.search(text)) or "=" in text


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


def _scan_loop_in_query(fd: FileDiff) -> list[Issue]:
    """基于缩进判断"循环体内发起查询"，只看新增行，避免误报存量代码。"""
    lines = [ln for hunk in fd.hunks for ln in hunk.lines if ln.kind != "-"]
    issues: list[Issue] = []
    for idx, line in enumerate(lines):
        if not re.match(r"\s*(for|while)\b", line.text):
            continue
        base_indent = _indent(line.text)
        for follower in lines[idx + 1 : idx + 40]:
            stripped = follower.text.strip()
            if not stripped:
                continue
            if _indent(follower.text) <= base_indent:
                break
            if follower.kind == "+" and QUERY_RE.search(follower.text):
                issues.append(
                    Issue(
                        rule_id="PERF_LOOP_IN_QUERY",
                        category="perf",
                        severity=Severity.P2,
                        file=fd.path,
                        start_line=follower.new_lineno or 1,
                        message="循环体内逐条发起查询，典型的 N+1 模式。",
                        suggestion="改为一次性批量查询（IN / 预加载 / join），在循环外聚合结果。",
                        confidence=0.75,
                        source="rule",
                        evidence=stripped[:160],
                    )
                )
                break
    return issues


def scan_rules(fd: FileDiff, target_line_only: bool = True) -> list[Issue]:
    if fd.is_binary or fd.is_deleted or not fd.hunks:
        return []

    issues: list[Issue] = []
    seen: set[tuple[int, str]] = set()

    for hunk in fd.hunks:
        for line in hunk.lines:
            if target_line_only and line.kind != "+":
                continue
            lineno = line.new_lineno or 1

            for rule in LINE_RULES:
                if rule.langs and fd.language not in rule.langs:
                    continue
                if not rule.pattern.search(line.text):
                    continue
                if rule.negative and rule.negative.search(line.text):
                    continue
                if (lineno, rule.rule_id) in seen:
                    continue
                seen.add((lineno, rule.rule_id))
                evidence = line.text.strip()[:160]
                if rule.mask_evidence:
                    evidence, _ = redact_secrets(evidence)
                issues.append(
                    Issue(
                        rule_id=rule.rule_id,
                        category=rule.category,
                        severity=rule.severity,
                        file=fd.path,
                        start_line=lineno,
                        message=rule.message,
                        suggestion=rule.suggestion,
                        confidence=0.85,
                        source="rule",
                        evidence=evidence,
                    )
                )

            if (lineno, "SEC_SQL_INJECTION") not in seen and _scan_sql_injection(line.text):
                seen.add((lineno, "SEC_SQL_INJECTION"))
                issues.append(
                    Issue(
                        rule_id="SEC_SQL_INJECTION",
                        category="security",
                        severity=Severity.P0,
                        file=fd.path,
                        start_line=lineno,
                        message="SQL 语句以字符串拼接/格式化方式构造后执行，存在注入风险。",
                        suggestion="改用参数化查询：把变量作为占位符参数传入，不要拼进 SQL 文本。",
                        confidence=0.8,
                        source="rule",
                        evidence=line.text.strip()[:160],
                    )
                )

    issues.extend(_scan_loop_in_query(fd))
    return issues
