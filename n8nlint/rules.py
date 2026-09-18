"""The rule registry and every check the linter performs.

Each rule is a small function ``(workflow, context) -> list[Finding]``.  Every
finding carries the reason it matters and a concrete fix, because a linter
that only says "problem here" makes you go and read the docs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import fields as fieldmod
from .expressions import collect_json_reads
from .model import (
    DATA_OUTPUT_TYPE,
    LOOP_ALLOWING_TYPES,
    SEVERITY_RANK,
    Node,
    Workflow,
)
from .secrets import scan_parameters


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    title: str
    why: str


RULES: tuple[Rule, ...] = (
    Rule(
        "invalid-workflow",
        "high",
        "file is not shaped like a workflow export",
        "the file cannot be read as an n8n workflow at all, so nothing else "
        "in it can be trusted",
    ),
    Rule(
        "empty-workflow",
        "medium",
        "workflow has no nodes",
        "an export with an empty `nodes` array is almost always a failed "
        "export or a half-deleted workflow",
    ),
    Rule(
        "node-missing-fields",
        "high",
        "node has no `name` or no `type`",
        "n8n routes data by node name and executes by node type; a node "
        "missing either cannot take part in the workflow",
    ),
    Rule(
        "duplicate-node-name",
        "high",
        "two nodes share the same name",
        "`connections` addresses nodes by name, so duplicate names make the "
        "routing ambiguous and n8n cannot tell which node you meant",
    ),
    Rule(
        "dangling-connection",
        "high",
        "connection points at a node that does not exist",
        "the connection can never be followed; the branch dies silently at "
        "that point",
    ),
    Rule(
        "unreachable-node",
        "high",
        "node cannot be reached from any trigger",
        "nothing ever executes it -- it is dead weight that looks like part "
        "of the automation",
    ),
    Rule(
        "no-trigger",
        "high",
        "workflow has no enabled trigger",
        "without a trigger the workflow can only be started by hand, so it "
        "will never run in production",
    ),
    Rule(
        "manual-trigger-only",
        "low",
        "the only trigger is a Manual Trigger",
        "the workflow looks finished but still has to be clicked to run",
    ),
    Rule(
        "disabled-node-in-path",
        "high",
        "a disabled node sits in the middle of the flow",
        "disabling a node in place changes what the downstream nodes receive "
        "without any visible error",
    ),
    Rule(
        "continue-on-fail-unchecked",
        "medium",
        "errors are swallowed into the normal output",
        "with `continueOnFail` the error item travels down the happy path and "
        "the next node treats it as real data",
    ),
    Rule(
        "missing-upstream-field",
        "medium",
        "expression reads a field no upstream node produces",
        "the expression resolves to `undefined` at runtime and the request or "
        "message that uses it is sent with missing data",
    ),
    Rule(
        "credential-reference",
        "medium",
        "credential reference cannot be resolved from the export",
        "an export never contains credential definitions, so an incomplete "
        "reference is the classic 'works on my machine' failure",
    ),
    Rule(
        "hardcoded-secret",
        "high",
        "a credential is hardcoded in the node parameters",
        "the value travels with the export into git, chat and backups, and it "
        "cannot be rotated per environment",
    ),
    Rule(
        "subworkflow-id-not-in-file",
        "medium",
        "sub-workflow reference points outside this file",
        "the Execute Sub-workflow node resolves its target by id at runtime; "
        "if that workflow is not there the node fails",
    ),
    Rule(
        "unintended-cycle",
        "medium",
        "connections form a loop that is not an obvious batching loop",
        "a cycle without a Split In Batches / Wait node usually means a "
        "mis-drawn connection rather than a deliberate loop",
    ),
    Rule(
        "duplicate-webhook-path",
        "high",
        "two enabled Webhook nodes share a path and method",
        "n8n registers routes by path and method, so only one of them can "
        "ever receive a request",
    ),
    Rule(
        "pinned-data",
        "medium",
        "the export ships pinned test data",
        "pinned data travels with the file and convinces the next person to "
        "open the workflow that it works",
    ),
    Rule(
        "overlapping-nodes",
        "low",
        "nodes sit on the exact same canvas position (cosmetic)",
        "stacked nodes are hard to select and easy to rewire by accident -- "
        "this changes nothing about execution",
    ),
)

RULES_BY_ID: dict[str, Rule] = {rule.id: rule for rule in RULES}


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    fix: str
    node: str | None = None
    location: str | None = None
    workflow: str | None = None
    file: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "fix": self.fix,
            "node": self.node,
            "location": self.location,
            "workflow": self.workflow,
            "file": self.file,
        }


@dataclass
class LintContext:
    """Facts that live outside a single workflow object."""

    #: every workflow id declared anywhere in the file being linted
    workflow_ids: frozenset[str] = frozenset()
    #: how many workflow objects the file holds
    workflow_count: int = 1


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _make(rule_id: str, message: str, fix: str, **kwargs: Any) -> Finding:
    rule = RULES_BY_ID[rule_id]
    return Finding(
        rule=rule.id, severity=rule.severity, message=message, fix=fix, **kwargs
    )


def _data_edges(wf: Workflow) -> list[tuple[str, str]]:
    return [
        (edge.source, edge.target)
        for edge in wf.edges()
        if edge.output_type == DATA_OUTPUT_TYPE
    ]


def _successors(wf: Workflow) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {n.name: [] for n in wf.nodes}
    for source, target in _data_edges(wf):
        out.setdefault(source, []).append(target)
    return out


def _predecessors(wf: Workflow) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {n.name: [] for n in wf.nodes}
    for source, target in _data_edges(wf):
        out.setdefault(target, []).append(source)
    return out


def _quoted(names: Iterable[str], limit: int = 4) -> str:
    items = list(names)
    shown = ", ".join(f'"{n}"' for n in items[:limit])
    if len(items) > limit:
        shown += f", and {len(items) - limit} more"
    return shown


def _mentions_error(node: Node) -> bool:
    """Does anything in this node's parameters look like it handles an error?"""

    try:
        text = json.dumps(node.parameters, default=str)
    except (TypeError, ValueError):
        text = str(node.parameters)
    return re.search(r"\berror\b", text, re.IGNORECASE) is not None


# --------------------------------------------------------------------------
# structural rules
# --------------------------------------------------------------------------


def rule_invalid_workflow(wf: Workflow, ctx: LintContext) -> list[Finding]:
    if not wf.notes:
        return []
    return [
        _make(
            "invalid-workflow",
            "This file parses as JSON but not as an n8n workflow export: "
            + "; ".join(wf.notes)
            + ". The linter assumes an object with a `nodes` array and a "
            "`connections` object (see the README's *Assumed export format*).",
            "Re-export the workflow from n8n (Editor menu -> Download, or "
            "`n8n export:workflow --id=<id> --output=wf.json`) instead of "
            "hand-editing the JSON.",
            workflow=wf.label,
        )
    ]


def rule_empty_workflow(wf: Workflow, ctx: LintContext) -> list[Finding]:
    if wf.notes or wf.nodes:
        return []
    return [
        _make(
            "empty-workflow",
            "The workflow has an empty `nodes` array: there is nothing to "
            "run. An export like this usually comes from a failed export, a "
            "workflow that was emptied in the editor and saved, or a template "
            "placeholder that was never filled in.",
            "Delete the file if it is a leftover, or add the nodes back and "
            "re-export. `--severity high` will not silence this rule's "
            "siblings, so fix the export rather than the threshold.",
            workflow=wf.label,
            location="nodes",
        )
    ]


def rule_node_missing_fields(wf: Workflow, ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    for node in wf.nodes:
        missing = []
        if not node.name.strip():
            missing.append("`name`")
        if not node.type.strip():
            missing.append("`type`")
        if not missing:
            continue
        out.append(
            _make(
                "node-missing-fields",
                f"`nodes[{node.index}]` has no {' or '.join(missing)}. n8n "
                f"addresses nodes by name (`connections` is keyed by name) and "
                f"executes them by type, so a node missing either one cannot "
                f"be wired up or run.",
                "Fill in the missing field, or delete the node if it is a "
                "leftover. Re-exporting from n8n is safer than hand-editing.",
                location=f"nodes[{node.index}]",
                workflow=wf.label,
            )
        )
    return out


def rule_duplicate_node_name(wf: Workflow, ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    for name in wf.duplicate_names():
        indexes = [n.index for n in wf.nodes if n.name == name]
        out.append(
            _make(
                "duplicate-node-name",
                f'{len(indexes)} nodes are all called "{name}" '
                f"(`nodes[{', '.join(str(i) for i in indexes)}]`). "
                f"`connections` is keyed by node name, so every connection "
                f"that mentions \"{name}\" is ambiguous: n8n will resolve it "
                f"to one of them and the other branch will never run.",
                "Give each node a unique name (n8n appends a number "
                "automatically when you paste a node, so this usually means a "
                "hand-edited or merged export). Then re-check the connections "
                "that referenced the old name.",
                node=name,
                location=f'connections["{name}"]',
                workflow=wf.label,
            )
        )
    return out


# --------------------------------------------------------------------------
# graph rules
# --------------------------------------------------------------------------


def rule_dangling_connection(wf: Workflow, ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in wf.dangling_refs():
        key = (ref.kind, ref.source, ref.target)
        if key in seen:
            continue
        seen.add(key)
        if ref.kind == "missing-source":
            out.append(
                _make(
                    "dangling-connection",
                    f'`connections["{ref.source}"]` defines outputs, but no '
                    f"node in `nodes` is called \"{ref.source}\". The whole "
                    f"entry is dead: nothing will ever run the nodes it "
                    f"points at ({_quoted([ref.target])}) from here.",
                    f'Rename the key to the node you actually mean, or delete '
                    f'the `"{ref.source}"` entry from `connections`. This '
                    f'shape almost always comes from renaming a node in one '
                    f"place in the JSON and not the other.",
                    node=ref.source,
                    location=f'connections["{ref.source}"]',
                    workflow=wf.label,
                )
            )
        else:
            out.append(
                _make(
                    "dangling-connection",
                    f'`connections["{ref.source}"]` routes to a node called '
                    f'"{ref.target}", but `nodes` contains no node with that '
                    f"name. The branch stops here at runtime: everything "
                    f"downstream of \"{ref.source}\" never runs.",
                    f'Point the connection at an existing node (check for a '
                    f'typo, a trailing space, or a node that was deleted) or '
                    f'remove the stale connection entry.',
                    node=ref.source,
                    location=f'connections["{ref.source}"]',
                    workflow=wf.label,
                )
            )
    return out


def rule_unreachable_node(wf: Workflow, ctx: LintContext) -> list[Finding]:
    roots = [n for n in wf.triggers(include_disabled=False) if not n.disabled]
    if not roots:
        # `no-trigger` already explains that nothing can start this workflow;
        # listing every node as unreachable on top of that is just noise.
        return []
    reachable = wf.reachable_from(n.name for n in roots)
    out: list[Finding] = []
    for node in wf.nodes:
        if node.name in reachable:
            continue
        if node.is_trigger:
            continue
        out.append(
            _make(
                "unreachable-node",
                f'"{node.name}" ({node.type or "unknown type"}) cannot be '
                f"reached from any trigger. No connection path leads into it, "
                f"so it never executes -- a silent no-op that looks like part "
                f"of the automation.",
                f'Connect it into the flow (check the `connections` block for '
                f'a missing or mistyped source), or delete the node. If it is '
                f'meant to be an independent entry point, give it its own '
                f"trigger.",
                node=node.name,
                location=f"nodes[{node.index}]",
                workflow=wf.label,
            )
        )
    return out


def rule_no_trigger(wf: Workflow, ctx: LintContext) -> list[Finding]:
    all_triggers = wf.triggers(include_disabled=True)
    enabled = [n for n in all_triggers if not n.disabled]
    if enabled or wf.notes or not wf.nodes:
        return []
    if all_triggers:
        return [
            _make(
                "no-trigger",
                f"The workflow has {len(all_triggers)} trigger node(s) "
                f"({_quoted(n.name for n in all_triggers)}), but every one of "
                f"them is disabled (`disabled: true`). Nothing can start the "
                f"workflow, so it will never run in production and cannot be "
                f"activated.",
                "Re-enable the trigger, or add the trigger the workflow is "
                "supposed to use.",
                workflow=wf.label,
                location="nodes",
            )
        ]
    return [
        _make(
            "no-trigger",
            "No node in this workflow is a trigger (checked for types whose "
            "name ends in `Trigger`, plus `webhook`, `cron`, `interval`, "
            "`emailReadImap` and `rssFeedRead` -- see the README for this "
            "assumption). The workflow can therefore only be started by hand "
            "from the editor or by an API call, never on its own.",
            "Add the trigger the automation needs (Schedule Trigger, Webhook, "
            "app trigger, Execute Sub-workflow Trigger, ...). If this is "
            "deliberately a manual utility workflow, this is the one finding "
            "you may want to ignore.",
            workflow=wf.label,
            location="nodes",
        )
    ]


def rule_manual_trigger_only(wf: Workflow, ctx: LintContext) -> list[Finding]:
    enabled = [n for n in wf.triggers(include_disabled=False) if not n.disabled]
    if not enabled:
        return []
    if any(n.short_type != "manualTrigger" for n in enabled):
        return []
    return [
        _make(
            "manual-trigger-only",
            f'The only enabled trigger is a Manual Trigger '
            f'("{enabled[0].name}"), so the workflow only runs when somebody '
            f"opens it and clicks *Test workflow*. It will never fire on a "
            f"schedule, on a webhook or from another workflow.",
            "If you wanted this to run by itself, replace the Manual Trigger "
            "with a Schedule Trigger, Webhook, app trigger or "
            "`When Executed by Another Workflow` trigger. A Manual Trigger is "
            "fine for a utility workflow you always run by hand.",
            node=enabled[0].name,
            location=f"nodes[{enabled[0].index}]",
            workflow=wf.label,
        )
    ]


def rule_disabled_node_in_path(wf: Workflow, ctx: LintContext) -> list[Finding]:
    succ = _successors(wf)
    pred = _predecessors(wf)
    known = set(wf.names())
    out: list[Finding] = []
    for node in wf.nodes:
        if not node.disabled:
            continue
        incoming = [p for p in pred.get(node.name, []) if p in known]
        outgoing = [s for s in succ.get(node.name, []) if s in known]
        if not incoming and not outgoing:
            continue
        where = []
        if incoming:
            where.append(f"receives data from {_quoted(incoming)}")
        if outgoing:
            where.append(f"feeds {_quoted(outgoing)}")
        out.append(
            _make(
                "disabled-node-in-path",
                f'"{node.name}" is disabled (`disabled: true`) but is still '
                f"wired into the flow: it " + " and ".join(where) + ". A "
                f"disabled node is skipped at runtime, and depending on your "
                f"n8n version its input either passes straight through "
                f"unchanged or produces no output at all -- so the nodes "
                f"after it receive untransformed data or never run. Either "
                f"way it is a silent behaviour change with no error in the "
                f"execution log.",
                f"Re-enable the node, or delete it and rewire its neighbours "
                f"directly to each other. If you are testing a replacement, "
                f"do it on a copy of the workflow rather than by disabling a "
                f"node in the live one.",
                node=node.name,
                location=f"nodes[{node.index}]",
                workflow=wf.label,
            )
        )
    return out


def rule_continue_on_fail(wf: Workflow, ctx: LintContext) -> list[Finding]:
    succ = _successors(wf)
    known = set(wf.names())
    by_name = wf.node_by_name
    out: list[Finding] = []
    for node in wf.nodes:
        if not node.continue_on_fail:
            continue
        consumers = [s for s in succ.get(node.name, []) if s in known]
        if not consumers:
            continue
        if any(_mentions_error(by_name[s]) for s in consumers if s in by_name):
            continue
        flag = (
            "`continueOnFail: true`"
            if node.raw.get("continueOnFail") is True
            else '`onError: "continueRegularOutput"`'
        )
        out.append(
            _make(
                "continue-on-fail-unchecked",
                f'"{node.name}" is configured to keep going when it fails '
                f"({flag}). n8n then emits an item that carries an `error` "
                f"field and pushes it onto the *normal* output, so "
                f"{_quoted(consumers)} receives that error item and treats it "
                f"as real data -- an automation that reports success while "
                f"doing nothing. None of the nodes directly after "
                f"\"{node.name}\" mentions `error`, so nothing here checks for "
                f"it.",
                f"Either let the node fail loudly (`continueOnFail: false` / "
                f'`onError: "stopWorkflow"`), or insert an IF node testing '
                f"`{{{{ $json.error }}}}` between \"{node.name}\" and its "
                f"consumers and route the failure somewhere you will see it "
                f"(an alert channel, or an Error Trigger workflow).",
                node=node.name,
                location=f"nodes[{node.index}]",
                workflow=wf.label,
            )
        )
    return out


def rule_unintended_cycle(wf: Workflow, ctx: LintContext) -> list[Finding]:
    by_name = wf.node_by_name
    out: list[Finding] = []
    for component in wf.cycle_components():
        loopers = [
            name
            for name in component
            if name in by_name and by_name[name].short_type in LOOP_ALLOWING_TYPES
        ]
        if loopers:
            continue
        out.append(
            _make(
                "unintended-cycle",
                f"The connections form a loop between {_quoted(component)}: "
                f"following the outputs from any of them leads back to itself. "
                f"n8n loops on purpose only through a Split In Batches / Loop "
                f"Over Items node (or a Wait node for polling), and none of "
                f"these nodes is one. The tool cannot prove intent from an "
                f"export -- if you are closing the loop deliberately and rely "
                f"on node-level error handling or `alwaysOutputData` to break "
                f"out, this finding is expected and can be ignored.",
                "Add the batching node the loop was meant to have (feed the "
                "loop output back into a Split In Batches / Loop Over Items "
                "node), or delete the connection that closes the cycle. "
                "Activate the workflow on a copy first and watch one "
                "execution.",
                location="connections",
                workflow=wf.label,
            )
        )
    return out


# --------------------------------------------------------------------------
# data-shape rule
# --------------------------------------------------------------------------


def rule_missing_upstream_field(wf: Workflow, ctx: LintContext) -> list[Finding]:
    by_name = wf.node_by_name
    out: list[Finding] = []
    for node in wf.nodes:
        reads = collect_json_reads(node.parameters)
        if not reads:
            continue
        producers = wf.field_predecessors(node.name)
        if not producers:
            continue
        shapes = {}
        opaque: list[str] = []
        available: set[str] = set()
        for name in sorted(producers):
            producer = by_name.get(name)
            if producer is None:
                continue
            shape = fieldmod.describe_output(producer)
            shapes[name] = shape
            if shape.known:
                available |= set(shape.fields)
            else:
                opaque.append(name)
        if opaque:
            # Something upstream could be producing the field at runtime.
            # Guessing here is how linters earn a reputation for noise.
            continue
        if not available:
            continue
        source_labels = _quoted(sorted(shapes))
        for path, field_name in reads:
            if field_name in available:
                continue
            out.append(
                _make(
                    "missing-upstream-field",
                    f'"{node.name}" reads `$json.{field_name}` (at '
                    f"`{path}`), but every node that can still contribute "
                    f"fields at that point only produces: "
                    f"{_quoted(sorted(available), limit=6)} (from "
                    f"{source_labels}). "
                    f"This is reported as a **warning, not an error**: field "
                    f"names are matched statically, n8n evaluates expressions "
                    f"at runtime, and this tool does not know your n8n "
                    f"version's node schemas or what an upstream HTTP/Code "
                    f"node really returns. It only fires when *every* "
                    f"producer upstream is statically knowable, which is why a "
                    f"workflow that touches an API will not produce this "
                    f"finding.",
                    f'Add `{field_name}` to the node that builds the item '
                    f"({source_labels}), stop that node from dropping the "
                    f"other input fields, or fix the expression to read a "
                    f"field that is actually there.",
                    node=node.name,
                    location=f"nodes[{node.index}].{path}",
                    workflow=wf.label,
                )
            )
    return out


# --------------------------------------------------------------------------
# credentials and secrets
# --------------------------------------------------------------------------


def rule_credential_reference(wf: Workflow, ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    name_to_ids: dict[str, set[str]] = {}

    for node in wf.nodes:
        for cred_type, ref in sorted(node.credentials.items()):
            location = f"nodes[{node.index}].credentials.{cred_type}"
            if not isinstance(ref, dict):
                out.append(
                    _make(
                        "credential-reference",
                        f'"{node.name}" declares a `{cred_type}` credential as '
                        f"`{ref!r}` instead of an object. n8n writes "
                        f"`{{\"id\": ..., \"name\": ...}}` there, so this "
                        f"reference cannot be resolved on import.",
                        "Re-select the credential for this node in the n8n "
                        "editor and re-export.",
                        node=node.name,
                        location=location,
                        workflow=wf.label,
                    )
                )
                continue

            name = ref.get("name")
            cred_id = ref.get("id")
            name = name.strip() if isinstance(name, str) else ""
            cred_id = str(cred_id).strip() if cred_id is not None else ""

            if not name and not cred_id and not ref:
                out.append(
                    _make(
                        "credential-reference",
                        f'"{node.name}" declares a `{cred_type}` credential '
                        f"slot but the reference is empty (`{{}}`). An n8n "
                        f"export never contains credential definitions -- only "
                        f"references -- so there is nothing in this file that "
                        f"can fill the slot. After import the node has no "
                        f"credential and the first API call fails with an "
                        f"authentication error.",
                        f"Select the `{cred_type}` credential for this node in "
                        f"the editor, and list the credentials the workflow "
                        f"needs in its `notes` so the next person knows what "
                        f"to create.",
                        node=node.name,
                        location=location,
                        workflow=wf.label,
                    )
                )
                continue

            if name and not cred_id:
                out.append(
                    _make(
                        "credential-reference",
                        f'"{node.name}" references the `{cred_type}` '
                        f'credential by name only ("{name}") -- there is no '
                        f"`id`. This file contains no credential definition "
                        f"for it, and n8n resolves credential references by "
                        f"id, not by name. A name-only reference is the usual "
                        f"cause of \"works on my machine\": on another "
                        f"instance the node binds to a different credential "
                        f"with the same name, or to nothing at all.",
                        "Re-select the credential in the node in the n8n "
                        "editor so n8n writes an `{id, name}` pair, then "
                        "re-export. Document the credentials the workflow "
                        "expects in the workflow's `notes`.",
                        node=node.name,
                        location=location,
                        workflow=wf.label,
                    )
                )
                continue

            if cred_id and not name:
                out.append(
                    _make(
                        "credential-reference",
                        f'"{node.name}" references the `{cred_type}` '
                        f'credential by id `{cred_id}` with no name. The '
                        f"credential definition is not in this export (an "
                        f"export never contains one), and on a different n8n "
                        f"instance that id belongs to a different -- or "
                        f"non-existent -- credential.",
                        "Re-select the credential in the node in the n8n "
                        "editor and re-export so the name is written too, and "
                        "note which credential the workflow needs.",
                        node=node.name,
                        location=location,
                        workflow=wf.label,
                    )
                )
                continue

            name_to_ids.setdefault(name, set()).add(cred_id)

    for name, ids in sorted(name_to_ids.items()):
        if len(ids) < 2:
            continue
        out.append(
            _make(
                "credential-reference",
                f'The credential name "{name}" is referenced with '
                f"{len(ids)} different ids in this file "
                f"({_quoted(sorted(ids))}). At most one of them can be the "
                f"credential the workflow was built against, so after import "
                f"at least some nodes will run against the wrong account.",
                "Re-select the credential consistently in every node that "
                "uses it, then re-export. If the nodes really are meant to use "
                "different accounts, give the credentials different names so "
                "the export is unambiguous.",
                node=name,
                location="nodes[*].credentials",
                workflow=wf.label,
            )
        )
    return out


def rule_hardcoded_secret(wf: Workflow, ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    for node in wf.nodes:
        for hit in scan_parameters(node.parameters):
            out.append(
                _make(
                    "hardcoded-secret",
                    f'At `{hit.path}` in "{node.name}": {hit.description} '
                    f"(value redacted as `{hit.excerpt}`). "
                    f"Anything inside a workflow export travels with the file "
                    f"into git, tickets, chat and backups, so the value is out "
                    f"of your control the moment the export is shared, and it "
                    f"cannot be rotated or scoped per environment.",
                    "Replace the literal with a credential: add an *Header "
                    "Auth* / *Query Auth* / app credential in n8n, select it "
                    "on the node, and let n8n inject the value. If you must "
                    "keep it in the file, read it from the environment with "
                    "`{{ $env.MY_TOKEN }}` and keep the value in n8n's "
                    "environment, then rotate the exposed value.",
                    node=node.name,
                    location=f"nodes[{node.index}].{hit.path}",
                    workflow=wf.label,
                )
            )
    return out


# --------------------------------------------------------------------------
# sub-workflows
# --------------------------------------------------------------------------

_SUBWORKFLOW_TYPES = frozenset({"executeWorkflow", "toolWorkflow"})
_EXPRESSION_MARKERS = ("{{", "}}", "$json", "$env", "$(")


def _resource_locator_value(raw: Any) -> str | None:
    """Pull the concrete value out of an n8n resourceLocator parameter."""

    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, dict):
        value = raw.get("value")
        if isinstance(value, str):
            return value.strip() or None
    return None


def rule_subworkflow_id(wf: Workflow, ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    for node in wf.nodes:
        if node.short_type not in _SUBWORKFLOW_TYPES:
            continue
        params = node.parameters
        source = params.get("source")
        if source == "parameter":
            embedded = params.get("workflowJson")
            if isinstance(embedded, dict) and embedded:
                continue  # the sub-workflow is inlined in this file
            if isinstance(embedded, str) and embedded.strip():
                try:
                    parsed = json.loads(embedded)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and parsed:
                    continue
            out.append(
                _make(
                    "subworkflow-id-not-in-file",
                    f'"{node.name}" is configured to take its sub-workflow '
                    f'from a parameter (`source: "parameter"`), but no '
                    f'`workflowJson` body is present in this export. The node '
                    f"has nothing to run and will fail when it executes.",
                    "Re-select the sub-workflow in the node and re-export, or "
                    'switch the node to the `database` source and reference a '
                    "workflow id.",
                    node=node.name,
                    location=f"nodes[{node.index}].parameters.workflowJson",
                    workflow=wf.label,
                )
            )
            continue

        value = _resource_locator_value(params.get("workflowId"))
        if not value:
            continue
        if any(marker in value for marker in _EXPRESSION_MARKERS):
            continue  # computed at runtime; nothing to check statically
        if not ctx.workflow_ids:
            # A single-workflow download carries no workflow ids, so the
            # linter has no ground truth.  Staying silent beats guessing.
            continue
        if value in ctx.workflow_ids:
            continue
        out.append(
            _make(
                "subworkflow-id-not-in-file",
                f'"{node.name}" calls the sub-workflow with id `{value}`, '
                f"which is not among the {len(ctx.workflow_ids)} workflow "
                f"id(s) present in this file. If you import this file as a "
                f"set, the node has no target and fails at runtime with "
                f"\"workflow not found\".",
                "Include the referenced workflow in the same export "
                "(`n8n export:workflow --all --output=all.json`), fix the id, "
                "or inline the sub-workflow with `source: \"parameter\"`.",
                node=node.name,
                location=f"nodes[{node.index}].parameters.workflowId",
                workflow=wf.label,
            )
        )
    return out


# --------------------------------------------------------------------------
# webhooks
# --------------------------------------------------------------------------


def _normalise_path(value: str) -> str:
    return "/" + value.strip().strip("/")


def rule_duplicate_webhook_path(wf: Workflow, ctx: LintContext) -> list[Finding]:
    groups: dict[tuple[str, str], list[Node]] = {}
    for node in wf.nodes:
        if node.short_type != "webhook" or node.disabled:
            continue
        path = node.parameters.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        if any(marker in path for marker in _EXPRESSION_MARKERS):
            continue  # the effective path is computed at runtime
        method = node.parameters.get("httpMethod")
        # Assumption: a Webhook node with no explicit httpMethod listens on
        # any method, but n8n's editor default is GET; grouping on GET keeps
        # the check quiet for the common case.  See README.
        method_key = method.strip().upper() if isinstance(method, str) and method.strip() else "GET"
        groups.setdefault((_normalise_path(path), method_key), []).append(node)

    out: list[Finding] = []
    for (path, method), nodes in sorted(groups.items()):
        if len(nodes) < 2:
            continue
        names = [n.name for n in nodes]
        out.append(
            _make(
                "duplicate-webhook-path",
                f"{len(nodes)} enabled Webhook nodes all listen on "
                f"`{method} {path}`: {_quoted(names)}. An n8n instance "
                f"registers one route per path and method, so at most one of "
                f"these receives the request -- the others silently never "
                f"fire, and depending on the version activation can fail with "
                f"a webhook-conflict error. (Assumed: a Webhook node with no "
                f"explicit `httpMethod` defaults to GET.)",
                "Give each Webhook node a distinct `path` (or a distinct "
                "`httpMethod` if they really are meant to share a URL), then "
                "re-export and re-register the production URL.",
                node=names[0],
                location=", ".join(f"nodes[{n.index}]" for n in nodes),
                workflow=wf.label,
            )
        )
    return out


# --------------------------------------------------------------------------
# hygiene
# --------------------------------------------------------------------------


def rule_pinned_data(wf: Workflow, ctx: LintContext) -> list[Finding]:
    pinned = wf.pin_data
    if not isinstance(pinned, dict):
        return []
    populated = sorted(
        name for name, value in pinned.items() if isinstance(value, list) and value
    )
    if not populated:
        return []
    return [
        _make(
            "pinned-data",
            f"This export ships pinned test data for {len(populated)} node(s): "
            f"{_quoted(populated)}. Pinned data is developer scaffolding, not "
            f"part of the automation, but it travels with the file. The next "
            f"person to import this workflow sees made-up values waiting in "
            f"those nodes and can easily conclude the automation works. "
            f"(Whether a pinned value can also reach a non-manual execution "
            f"depends on your n8n version and how the run is started -- this "
            f"tool cannot check that.)",
            "Unpin the nodes in the editor (right-click the node -> *Unpin*) "
            "or delete the `pinData` key from the exported JSON before "
            "committing or sharing it.",
            node=populated[0],
            location="pinData",
            workflow=wf.label,
        )
    ]


def rule_overlapping_nodes(wf: Workflow, ctx: LintContext) -> list[Finding]:
    positions: dict[tuple[float, float], list[Node]] = {}
    for node in wf.nodes:
        pos = node.position
        if not isinstance(pos, (list, tuple)) or len(pos) != 2:
            continue
        x, y = pos
        if isinstance(x, bool) or isinstance(y, bool):
            continue
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        positions.setdefault((float(x), float(y)), []).append(node)

    out: list[Finding] = []
    for (x, y), nodes in sorted(positions.items()):
        if len(nodes) < 2:
            continue
        names = [n.name for n in nodes]
        out.append(
            _make(
                "overlapping-nodes",
                f"{len(nodes)} nodes sit on exactly the same canvas position "
                f"([{x:g}, {y:g}]): {_quoted(names)}. This is purely cosmetic "
                f"-- it changes nothing about how the workflow executes -- but "
                f"stacked nodes are hard to select, easy to rewire by accident "
                f"and usually a sign of a half-finished edit.",
                "Drag the nodes apart in the n8n canvas and re-export. Nothing "
                "else depends on the position values.",
                node=names[0],
                location=", ".join(f"nodes[{n.index}]" for n in nodes),
                workflow=wf.label,
            )
        )
    return out


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

ALL_RULES = (
    rule_invalid_workflow,
    rule_empty_workflow,
    rule_node_missing_fields,
    rule_duplicate_node_name,
    rule_dangling_connection,
    rule_unreachable_node,
    rule_no_trigger,
    rule_manual_trigger_only,
    rule_disabled_node_in_path,
    rule_continue_on_fail,
    rule_unintended_cycle,
    rule_missing_upstream_field,
    rule_credential_reference,
    rule_hardcoded_secret,
    rule_subworkflow_id,
    rule_duplicate_webhook_path,
    rule_pinned_data,
    rule_overlapping_nodes,
)


def lint_workflow(wf: Workflow, ctx: LintContext | None = None) -> list[Finding]:
    """Run every rule over one workflow and return sorted findings."""

    ctx = ctx or LintContext()
    findings: list[Finding] = []
    for rule in ALL_RULES:
        findings.extend(rule(wf, ctx))
    findings.sort(
        key=lambda f: (
            -SEVERITY_RANK.get(f.severity, 0),
            f.rule,
            f.node or "",
            f.location or "",
        )
    )
    return findings


def lint_parsed_file(parsed: Any) -> list[Finding]:
    """Run every rule over every workflow in a :class:`n8nlint.model.ParsedFile`."""

    ctx = LintContext(
        workflow_ids=frozenset(parsed.workflow_ids),
        workflow_count=len(parsed.workflows),
    )
    out: list[Finding] = []
    multi = len(parsed.workflows) > 1
    for wf in parsed.workflows:
        for finding in lint_workflow(wf, ctx):
            # Only name the workflow when the file holds more than one, so a
            # single-workflow export does not repeat its name on every line.
            finding.workflow = wf.label if multi else None
            finding.file = parsed.path
            out.append(finding)
    # Re-sort across workflows so severity ordering holds for the whole file.
    out.sort(
        key=lambda f: (
            -SEVERITY_RANK.get(f.severity, 0),
            f.rule,
            f.workflow or "",
            f.node or "",
            f.location or "",
        )
    )
    return out


def filter_findings(
    findings: Iterable[Finding], threshold: str
) -> list[Finding]:
    """Keep findings at or above ``threshold``."""

    minimum = SEVERITY_RANK.get(threshold, 1)
    return [f for f in findings if SEVERITY_RANK.get(f.severity, 0) >= minimum]


def count_by_severity(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts
