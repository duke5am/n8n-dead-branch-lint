"""Command line interface.

Exit codes (documented in the README):

* ``0`` -- nothing at or above the severity threshold
* ``1`` -- at least one finding at or above the threshold
* ``2`` -- usage error, unreadable path, or a file that is not JSON
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Sequence

from . import __version__
from .model import (
    DEFAULT_SEVERITY,
    SEVERITY_ORDER,
    ParsedFile,
    WorkflowParseError,
    parse_file,
)
from .report import (
    build_json_payload,
    render_findings,
    render_json,
    render_list_rules,
    render_quiet,
    render_summary,
)
from .rules import Finding, filter_findings, lint_parsed_file

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

_JSON_SUFFIXES = (".json",)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="n8n_workflow_lint.py",
        description=(
            "Static linter for exported n8n workflow JSON. Reads an export and "
            "reports the mistakes that break automations in production. It "
            "never contacts an n8n instance and never executes a workflow."
        ),
        epilog=(
            "exit codes: 0 nothing at/above --severity, "
            "1 findings at/above --severity, 2 usage or parse error"
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="one or more workflow .json files, or a directory to scan",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit machine-readable JSON instead of text",
    )
    parser.add_argument(
        "--severity",
        choices=SEVERITY_ORDER,
        default=DEFAULT_SEVERITY,
        help=(
            "lowest severity to report and to fail on "
            f"(default: {DEFAULT_SEVERITY} -- report everything)"
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "one line per finding and nothing else; no banner, no summary, "
            "no output at all when the file is clean"
        ),
    )
    parser.add_argument(
        "--list-rules",
        action="store_true",
        help="print every rule with its severity and what it catches, then exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"n8n-workflow-lint {__version__}",
    )
    return parser


def _collect_inputs(paths: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """Split the arguments into explicit files, directory-discovered files, errors."""

    files: list[str] = []
    discovered: list[str] = []
    problems: list[str] = []
    for raw in paths:
        path = os.path.expanduser(raw)
        if os.path.isdir(path):
            found: list[str] = []
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames.sort()
                for name in sorted(filenames):
                    if name.lower().endswith(_JSON_SUFFIXES):
                        found.append(os.path.join(dirpath, name))
            if not found:
                problems.append(f"{raw}: no .json files found in this directory")
            discovered.extend(found)
        elif os.path.isfile(path):
            files.append(path)
        else:
            problems.append(f"{raw}: no such file or directory")
    return files, discovered, problems


def _read(path: str) -> ParsedFile:
    return parse_file(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.list_rules:
        sys.stdout.write(render_list_rules() + "\n")
        return EXIT_CLEAN

    if not args.paths:
        parser.print_usage(sys.stderr)
        sys.stderr.write(
            "n8n_workflow_lint.py: error: give me at least one file or "
            "directory (or use --list-rules)\n"
        )
        return EXIT_USAGE

    explicit, discovered, problems = _collect_inputs(args.paths)
    if problems and not explicit and not discovered:
        for problem in problems:
            sys.stderr.write(f"n8n_workflow_lint.py: error: {problem}\n")
        return EXIT_USAGE

    all_findings: list[Finding] = []
    file_reports: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    skipped = 0

    for problem in problems:
        errors.append({"path": problem, "error": problem})

    for path, is_explicit in [(p, True) for p in explicit] + [
        (p, False) for p in discovered
    ]:
        try:
            parsed = _read(path)
        except WorkflowParseError as exc:
            errors.append({"path": path, "error": str(exc)})
            continue

        if not parsed.is_workflow_shaped and not is_explicit:
            # Directory scan: a random .json file next to the exports is not
            # an error, so skip it instead of reporting it.
            skipped += 1
            continue

        findings = lint_parsed_file(parsed)
        all_findings.extend(findings)
        file_reports.append(
            {
                "path": path,
                "workflows": len(parsed.workflows),
                "findings": len(findings),
            }
        )

    if errors:
        # A usage/parse problem always fails the run, even if other files
        # produced findings: exit 2 dominates 1 so CI cannot mistake a
        # truncated export for a clean pass.
        if args.as_json:
            sys.stdout.write(
                render_json(
                    build_json_payload(
                        files=file_reports,
                        findings=[],
                        errors=errors,
                        threshold=args.severity,
                        version=__version__,
                    )
                )
            )
        elif args.quiet:
            for error in errors:
                sys.stderr.write(f"n8n_workflow_lint.py: error: {error['error']}\n")
        else:
            for error in errors:
                sys.stderr.write(f"n8n_workflow_lint.py: error: {error['error']}\n")
            sys.stdout.write("(run failed before linting completed)\n")
        return EXIT_USAGE

    reported = filter_findings(all_findings, args.severity)

    if args.as_json:
        sys.stdout.write(
            render_json(
                build_json_payload(
                    files=file_reports,
                    findings=reported,
                    errors=errors,
                    threshold=args.severity,
                    version=__version__,
                )
            )
        )
    elif args.quiet:
        text = render_quiet(reported)
        if text:
            sys.stdout.write(text + "\n")
    else:
        by_file: dict[str, list[Finding]] = {}
        for finding in reported:
            by_file.setdefault(finding.file or "-", []).append(finding)

        checked = len(file_reports)
        for report in file_reports:
            path = report["path"]
            findings_here = by_file.get(path, [])
            if not findings_here:
                sys.stdout.write(f"{path}: clean\n")
                continue
            counts = {}
            for finding in findings_here:
                counts[finding.severity] = counts.get(finding.severity, 0) + 1
            detail = ", ".join(
                f"{counts[s]} {s}" for s in SEVERITY_ORDER if counts.get(s)
            )
            sys.stdout.write(
                f"{path}: {len(findings_here)} finding(s) ({detail})\n\n"
            )
            sys.stdout.write(render_findings(findings_here) + "\n")

        sys.stdout.write(
            render_summary(
                reported, files=checked, skipped=skipped, errors=len(errors)
            )
            + "\n"
        )

    return EXIT_FINDINGS if reported else EXIT_CLEAN


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
