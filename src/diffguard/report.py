"""报告输出模块：结论判定与评论体渲染。

等级到结论的映射保持确定性，不交给模型判断：
- 存在 P0/P1 -> 阻断
- 仅有 P2     -> 评论
- 只有 P3     -> 放行（P3 只作为附注展示，不影响合并）
这样干净代码不会因为一条风格 nit 被判成"评论"，结论一致率才稳得住。
"""

from __future__ import annotations

from .schemas import Issue, ReviewReport, Severity, Verdict


def decide_verdict(issues: list[Issue]) -> Verdict:
    if any(i.severity in (Severity.P0, Severity.P1) for i in issues):
        return Verdict.BLOCK
    if any(i.severity is Severity.P2 for i in issues):
        return Verdict.COMMENT
    return Verdict.PASS


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(report: ReviewReport, pr_title: str = "") -> str:
    lines = ["## DiffGuard 审查报告", ""]
    if pr_title:
        lines.append(f"**PR**：{_cell(pr_title)}  ")
    lines.append(f"**结论**：{report.verdict.label} ({report.verdict.value.upper()})")
    lines.append("")

    stats = report.stats
    if stats:
        lines.append(
            f"问题数：{stats.get('final_issues', 0)} "
            f"(P0 {stats.get('p0', 0)} / P1 {stats.get('p1', 0)} / "
            f"P2 {stats.get('p2', 0)} / P3 {stats.get('p3', 0)})  "
            f"去重折叠：{stats.get('deduped_issues', 0)}  "
            f"步数：{stats.get('steps', 0)}"
        )
        lines.append("")

    if not report.issues:
        lines.append("未发现问题。")
        return "\n".join(lines)

    for severity in (Severity.P0, Severity.P1, Severity.P2, Severity.P3):
        group = [i for i in report.issues if i.severity is severity]
        if not group:
            continue
        lines.append(f"### {severity.value} {severity.label}（{len(group)}）")
        lines.append("")
        lines.append("| 位置 | 规则 | 说明 | 建议 |")
        lines.append("| --- | --- | --- | --- |")
        for issue in group:
            lines.append(
                f"| `{_cell(issue.location)}` | `{issue.rule_id}` | {_cell(issue.message)} "
                f"| {_cell(issue.suggestion or '-')} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
