"""Shared helpers for the test suite.

The suite runs with no third-party packages and never writes to ``/tmp``:
scratch files live under the project's own ``.scratch/`` directory and are
removed again in teardown.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Iterable, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (HERE, ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from n8nlint.model import parse_file, parse_text  # noqa: E402
from n8nlint.rules import Finding, lint_parsed_file  # noqa: E402

EXAMPLES = os.path.join(ROOT, "examples")
GOOD_EXAMPLE = os.path.join(EXAMPLES, "good.json")
BAD_EXAMPLE = os.path.join(EXAMPLES, "bad.json")
CLI = os.path.join(ROOT, "n8n_workflow_lint.py")
README = os.path.join(ROOT, "README.md")

#: /tmp swallows writes in this environment, so scratch space is local.
SCRATCH_ROOT = os.path.join(ROOT, ".scratch")


# --------------------------------------------------------------------------
# building workflow documents
# --------------------------------------------------------------------------


def node(
    name: str | None = "Node",
    type_name: str | None = "n8n-nodes-base.noOp",
    position: Sequence[float] | None = (0, 0),
    parameters: dict | None = None,
    **extra: Any,
) -> dict:
    """Build one entry of a ``nodes`` array.

    ``name=None`` or ``type_name=None`` omit the key entirely, which is how
    the ``node-missing-fields`` fixtures are built.
    """

    entry: dict[str, Any] = {
        "parameters": dict(parameters or {}),
        "typeVersion": 1,
    }
    if position is not None:
        entry["position"] = [float(position[0]), float(position[1])]
    if name is not None:
        entry["name"] = name
    if type_name is not None:
        entry["type"] = type_name
    entry.update(extra)
    return entry


def connections_of(mapping: dict[str, list[list[str]]]) -> dict:
    """``{"A": [["B", "C"], ["D"]]}`` -> an n8n ``connections`` object.

    The outer list is the node's outputs; each inner list is the targets of
    that output, in order.
    """

    out: dict[str, Any] = {}
    for source, branches in mapping.items():
        out[source] = {
            "main": [
                [
                    {"node": target, "type": "main", "index": 0}
                    for target in branch
                ]
                for branch in branches
            ]
        }
    return out


def workflow(
    nodes: list[dict],
    connections: dict | None = None,
    **extra: Any,
) -> dict:
    doc: dict[str, Any] = {
        "name": extra.pop("name", "test workflow"),
        "nodes": nodes,
        "connections": connections if connections is not None else {},
        "settings": {"executionOrder": "v1"},
        "active": False,
    }
    doc.update(extra)
    return doc


WEBHOOK_PARAMS = {"httpMethod": "GET", "path": "test-hook", "options": {}}


def simple_workflow(extra_nodes: Iterable[dict] = (), **extra: Any) -> dict:
    """A minimal clean workflow: one Webhook trigger feeding one NoOp."""

    nodes = [
        node(
            "Start",
            "n8n-nodes-base.webhook",
            (-200, 0),
            dict(WEBHOOK_PARAMS),
        ),
        node("Work", "n8n-nodes-base.noOp", (0, 0)),
    ]
    nodes.extend(extra_nodes)
    return workflow(
        nodes,
        connections_of({"Start": [["Work"]]}),
        **extra,
    )


# --------------------------------------------------------------------------
# linting helpers
# --------------------------------------------------------------------------


def lint(doc: Any, label: str = "wf.json") -> list[Finding]:
    """Lint an in-memory document (dict or list of dicts)."""

    parsed = parse_text(json.dumps(doc), label)
    return lint_parsed_file(parsed)


def lint_file(path: str) -> list[Finding]:
    return lint_parsed_file(parse_file(path))


def rules_of(findings: Iterable[Finding]) -> set[str]:
    return {f.rule for f in findings}


def only(findings: Sequence[Finding], rule: str) -> list[Finding]:
    return [f for f in findings if f.rule == rule]


def messages_for(findings: Sequence[Finding], rule: str) -> str:
    return "\n".join(f.message for f in only(findings, rule))


# --------------------------------------------------------------------------
# scratch files and the CLI
# --------------------------------------------------------------------------


class Scratch:
    """A fresh directory under the project, removed on exit."""

    def __init__(self, name: str) -> None:
        self.path = os.path.join(SCRATCH_ROOT, name)

    def __enter__(self) -> "Scratch":
        shutil.rmtree(self.path, ignore_errors=True)
        os.makedirs(self.path, exist_ok=True)
        return self

    def __exit__(self, *exc: Any) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def write(self, name: str, content: Any) -> str:
        full = os.path.join(self.path, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            if isinstance(content, str):
                handle.write(content)
            else:
                json.dump(content, handle, indent=2)
        return full


class CliResult:
    def __init__(self, completed: subprocess.CompletedProcess) -> None:
        self.code = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"CliResult(code={self.code}, stdout={self.stdout!r}, "
            f"stderr={self.stderr!r})"
        )


def run_cli(*args: str, cwd: str | None = None) -> CliResult:
    """Run the real CLI in a subprocess and capture everything."""

    completed = subprocess.run(
        [sys.executable, CLI, *args],
        capture_output=True,
        text=True,
        cwd=cwd or ROOT,
        timeout=120,
    )
    return CliResult(completed)
