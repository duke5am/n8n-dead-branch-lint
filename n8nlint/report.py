"""Rendering findings as text or JSON."""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from .model import SEVERITY_ORDER, SEVERITY_RANK
from .rules import RULES, Finding, count_by_severity


def _severity_label(severity: str) -> str:
    return severity.upper()


def _context_line(finding: Finding) -> str:
    bits: list[str] = []
    if finding.workflow:
        bits.append(f'workflow "{finding.workflow}"')
    if finding.node:
        bits.append(f'node "{finding.node}"')
    if finding.location:
        bits.append(finding.location)
    return " · ".join(bits)


def render_findings(findings: Sequence[Finding], *, show_fix: bool = True) -> str:
    """Render a block of findings (no file banner, no summary)."""

    lines: list[str] = []
    for finding in findings:
        head = f"  {_severity_label(finding.severity):<7} {finding.rule:<28}"
        context = _context_line(finding)
        if context:
            head = f"{head} {context}"
        lines.append(head.rstrip())
        for chunk in _wrap(finding.message, 78, "          "):
            lines.append(chunk)
        if show_fix:
            for idx, chunk in enumerate(_wrap("fix: " + finding.fix, 78, "          ")):
                lines.append(chunk if idx else chunk)
        lines.append("")
    return "\n".join(lines)


def _wrap(text: str, width: int, indent: str) -> list[str]:
    words = text.split()
    if not words:
        return [indent.rstrip()]
    lines: list[str] = []
    current = indent
    for word in words:
        candidate = f"{current} {word}" if current.strip() else f"{current}{word}"
        if len(candidate) > width and current.strip():
            lines.append(current)
            current = indent + word
        else:
            current = candidate
    lines.append(current)
    return lines


def render_quiet(findings: Sequence[Finding]) -> str:
    """One grep-able line per finding; empty string when there is nothing."""

    lines: list[str] = []
    for finding in findings:
        where = finding.location or (finding.node or "")
        prefix = f"{finding.file or '-'}: "
        if finding.workflow:
            prefix += f'[{finding.workflow}] '
        lines.append(
            f"{prefix}{finding.severity} {finding.rule} {where}".rstrip()
            + f" :: {finding.message}"
        )
    return "\n".join(lines)


def render_summary(
    findings: Sequence[Finding],
    *,
    files: int,
    skipped: int = 0,
    errors: int = 0,
) -> str:
    counts = count_by_severity(findings)
    parts = [f"{counts[s]} {s}" for s in SEVERITY_ORDER if counts[s]]
    detail = ", ".join(parts) if parts else "none"
    total = len(findings)
    noun = "finding" if total == 1 else "findings"
    file_noun = "file" if files == 1 else "files"
    line = f"{total} {noun} ({detail}) in {files} {file_noun}"
    if skipped:
        line += f"; {skipped} file(s) skipped (not a workflow export)"
    if errors:
        line += f"; {errors} file(s) could not be parsed"
    return line


def render_list_rules() -> str:
    lines = ["Rules (id, severity, what it catches):", ""]
    for rule in sorted(RULES, key=lambda r: (-SEVERITY_RANK[r.severity], r.id)):
        lines.append(f"  {rule.id}")
        lines.append(f"      severity : {rule.severity}")
        lines.append(f"      catches  : {rule.title}")
        lines.append(f"      why      : {rule.why}")
        lines.append("")
    lines.append(f"{len(RULES)} rules. Thresholds: --severity high|medium|low")
    return "\n".join(lines)


def render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def build_json_payload(
    *,
    files: list[dict[str, Any]],
    findings: Sequence[Finding],
    errors: list[dict[str, str]],
    threshold: str,
    version: str,
) -> dict[str, Any]:
    counts = count_by_severity(findings)
    return {
        "tool": "n8n-workflow-lint",
        "version": version,
        "threshold": threshold,
        "summary": {
            "findings": len(findings),
            "high": counts["high"],
            "medium": counts["medium"],
            "low": counts["low"],
            "files": len(files),
            "parse_errors": len(errors),
        },
        "files": files,
        "errors": errors,
        "findings": [f.as_dict() for f in findings],
    }
