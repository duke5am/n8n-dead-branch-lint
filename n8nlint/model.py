"""Data model, JSON parsing and connection graph for n8n workflow exports.

Everything the linter knows about the n8n export format is in this module and
in :mod:`n8nlint.fields`.  The assumed shape (and the places where the
assumption is a guess rather than a verified fact) is written up in
``README.md`` under *Assumed export format*.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

# --------------------------------------------------------------------------
# severities
# --------------------------------------------------------------------------

#: Report severities, most severe first.
SEVERITY_ORDER: tuple[str, ...] = ("high", "medium", "low")

SEVERITY_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}

#: Default ``--severity`` threshold: report everything.
DEFAULT_SEVERITY = "low"

#: Output type on a connection that carries *data* between nodes.
#: Other types (``ai_tool``, ``ai_languageModel``, ``ai_memory``, ...) wire
#: LangChain sub-nodes to their agent and are not a data path.
DATA_OUTPUT_TYPE = "main"

#: Prefix used by the LangChain wiring output types.
AI_OUTPUT_PREFIX = "ai_"


class WorkflowParseError(ValueError):
    """The file could not be read as JSON at all (exit code 2)."""


# --------------------------------------------------------------------------
# node types
# --------------------------------------------------------------------------

#: Node type short names that start a workflow but do **not** end in
#: ``Trigger``.  (``n8n-nodes-base.webhook`` is the important one.)
NON_SUFFIX_TRIGGER_TYPES: frozenset[str] = frozenset(
    {
        "webhook",
        "cron",  # pre-1.0 name of the Schedule Trigger
        "interval",  # ditto
        "emailReadImap",
        "rssFeedRead",
    }
)

#: Node type short names that n8n uses to run a loop on purpose.
#: A cycle that contains one of these is *assumed* deliberate and is not
#: reported; see README -> "What this does not do".
LOOP_ALLOWING_TYPES: frozenset[str] = frozenset(
    {
        "splitInBatches",  # classic "Split In Batches"
        "loopOverItems",  # newer "Loop Over Items"
        "wait",  # the polling-loop pattern
    }
)


def short_type(type_name: Any) -> str:
    """``"n8n-nodes-base.splitInBatches"`` -> ``"splitInBatches"``.

    Handles the three type-name shapes n8n uses: core nodes
    (``n8n-nodes-base.x``), LangChain nodes (``@n8n/n8n-nodes-langchain.x``)
    and community nodes (``n8n-nodes-something.x``).
    """

    if not isinstance(type_name, str):
        return ""
    if "." not in type_name:
        return type_name
    return type_name.rsplit(".", 1)[-1]


def is_trigger_type(type_name: Any) -> bool:
    """Best-effort answer to "does this node type start a workflow?".

    Heuristic (documented in the README): a node type is treated as a
    trigger when its short name ends in ``Trigger`` (case-insensitive) or is
    one of the handful of legacy / non-suffixed names in
    :data:`NON_SUFFIX_TRIGGER_TYPES`.  The linter does *not* carry a copy of
    n8n's node catalogue, so a community trigger whose name does not end in
    ``Trigger`` will be missed.
    """

    short = short_type(type_name)
    if not short:
        return False
    if short.lower().endswith("trigger"):
        return True
    return short in NON_SUFFIX_TRIGGER_TYPES


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------


@dataclass
class Node:
    """One entry of the export's ``nodes`` array."""

    index: int
    raw: dict
    name: str
    type: str
    type_version: Any = None
    position: Any = None
    parameters: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)

    # -- convenience -------------------------------------------------------

    @property
    def short_type(self) -> str:
        return short_type(self.type)

    @property
    def disabled(self) -> bool:
        """``disabled: true`` on the node object (n8n's "Deactivate" toggle)."""

        return self.raw.get("disabled") is True

    @property
    def is_trigger(self) -> bool:
        return is_trigger_type(self.type)

    @property
    def continue_on_fail(self) -> bool:
        """True for both spellings of "keep going after this node throws".

        ``continueOnFail: true`` is the pre-1.0 flag; newer n8n writes
        ``onError: "continueRegularOutput"``.  ``onError:
        "continueErrorOutput"`` is deliberately *not* included -- it routes
        the error item to a second output instead of the main one.
        """

        if self.raw.get("continueOnFail") is True:
            return True
        return self.raw.get("onError") == "continueRegularOutput"

    @property
    def always_output_data(self) -> bool:
        return self.raw.get("alwaysOutputData") is True

    def param_at(self, path: str, default: Any = None) -> Any:
        """Fetch ``parameters.a.b[0]`` by dot path; ``default`` if missing."""

        cur: Any = self.parameters
        for part in path.split("."):
            if isinstance(cur, dict):
                if part not in cur:
                    return default
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit():
                idx = int(part)
                if idx >= len(cur):
                    return default
                cur = cur[idx]
            else:
                return default
        return cur


# --------------------------------------------------------------------------
# edges / graph
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    output_type: str
    output_index: int
    target_index: Any


@dataclass(frozen=True)
class DanglingRef:
    """A connection entry that points at something that is not there."""

    source: str
    target: str
    output_type: str
    output_index: int
    kind: str  # "missing-target" | "missing-source"


@dataclass
class Workflow:
    """A single workflow parsed out of an export file."""

    label: str
    raw: dict
    nodes: list[Node]
    connections: dict
    workflow_id: str | None = None
    pin_data: Any = None
    name: str | None = None
    active: Any = None
    settings: Any = None
    source_index: int = 0
    #: structural problems found while normalising (informational)
    notes: list[str] = field(default_factory=list)

    # -- lookups -----------------------------------------------------------

    @property
    def node_by_name(self) -> dict[str, Node]:
        out: dict[str, Node] = {}
        for node in self.nodes:
            out.setdefault(node.name, node)
        return out

    def names(self) -> list[str]:
        return [n.name for n in self.nodes]

    def duplicate_names(self) -> list[str]:
        seen: dict[str, int] = {}
        for node in self.nodes:
            seen[node.name] = seen.get(node.name, 0) + 1
        return sorted(name for name, count in seen.items() if count > 1)

    def triggers(self, *, include_disabled: bool = True) -> list[Node]:
        out = [n for n in self.nodes if n.is_trigger]
        if not include_disabled:
            out = [n for n in out if not n.disabled]
        return out

    # -- connections -------------------------------------------------------

    def _iter_connection_refs(self) -> Iterator[tuple[str, str, int, dict]]:
        """Yield ``(source, output_type, output_index, reference)``."""

        if not isinstance(self.connections, dict):
            return
        for source, outputs in self.connections.items():
            if not isinstance(outputs, dict):
                continue
            for output_type, branches in outputs.items():
                if not isinstance(branches, list):
                    # Some tools emit a single branch object instead of a
                    # list of branches; be lenient about it.
                    branches = [branches]
                for out_index, branch in enumerate(branches):
                    if isinstance(branch, dict):
                        branch = [branch]
                    if not isinstance(branch, list):
                        continue
                    for ref in branch:
                        if isinstance(ref, dict):
                            yield source, str(output_type), out_index, ref

    def edges(self) -> list[Edge]:
        known = set(self.names())
        out: list[Edge] = []
        for source, output_type, out_index, ref in self._iter_connection_refs():
            target = ref.get("node")
            if not isinstance(target, str):
                continue
            if source not in known or target not in known:
                continue
            out.append(
                Edge(
                    source=source,
                    target=target,
                    output_type=output_type,
                    output_index=out_index,
                    target_index=ref.get("index"),
                )
            )
        return out

    def dangling_refs(self) -> list[DanglingRef]:
        known = set(self.names())
        out: list[DanglingRef] = []
        for source, output_type, out_index, ref in self._iter_connection_refs():
            target = ref.get("node")
            if not isinstance(target, str):
                continue
            if source not in known:
                out.append(
                    DanglingRef(source, target, output_type, out_index, "missing-source")
                )
            if target not in known:
                out.append(
                    DanglingRef(source, target, output_type, out_index, "missing-target")
                )
        return out

    # -- adjacency ---------------------------------------------------------

    def _adjacency(self, output_types: Sequence[str] | None) -> dict[str, list[str]]:
        adj: dict[str, list[str]] = {n.name: [] for n in self.nodes}
        for edge in self.edges():
            if output_types is not None and edge.output_type not in output_types:
                continue
            adj.setdefault(edge.source, []).append(edge.target)
        return adj

    def data_adjacency(self) -> dict[str, list[str]]:
        """Forward adjacency over ``main`` connections only."""

        return self._adjacency((DATA_OUTPUT_TYPE,))

    def reachable_from(self, roots: Iterable[str]) -> set[str]:
        """Reachability over *every* connection type.

        ``ai_*`` connections run from the sub-node (tool, model, memory)
        *to* the agent, so they are followed in both directions here.  That
        keeps a tool node that is only wired into an agent from being
        reported as unreachable.  Documented as a heuristic in the README.
        """

        adj = self._adjacency(None)
        # undirected view of the ai_* wires
        ai_bi: dict[str, list[str]] = {n.name: [] for n in self.nodes}
        for edge in self.edges():
            if edge.output_type.startswith(AI_OUTPUT_PREFIX):
                ai_bi.setdefault(edge.source, []).append(edge.target)
                ai_bi.setdefault(edge.target, []).append(edge.source)

        seen: set[str] = set()
        stack = [r for r in roots]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(adj.get(cur, ()))
            stack.extend(ai_bi.get(cur, ()))
        return seen

    def cycle_components(self) -> list[list[str]]:
        """Group nodes that can reach themselves over ``main`` connections.

        Deliberately simple and obviously correct rather than clever: a node
        is on a cycle when a walk from its successors comes back to it.
        Workflows are small enough that the quadratic cost does not matter.
        """

        adj = self.data_adjacency()
        on_cycle: set[str] = set()
        for node in self.nodes:
            start = node.name
            stack = list(adj.get(start, ()))
            seen: set[str] = set()
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                if cur == start:
                    on_cycle.add(start)
                    break
                seen.add(cur)
                stack.extend(adj.get(cur, ()))

        # group the cycle nodes into connected components
        components: list[list[str]] = []
        remaining = set(on_cycle)
        while remaining:
            seed = sorted(remaining)[0]
            group = {seed}
            stack = [seed]
            while stack:
                cur = stack.pop()
                for nxt in list(adj.get(cur, ())) + [
                    s for s, ts in adj.items() if cur in ts
                ]:
                    if nxt in remaining and nxt not in group:
                        group.add(nxt)
                        stack.append(nxt)
            remaining -= group
            components.append(sorted(group))
        return components

    def field_predecessors(self, target: str) -> set[str]:
        """Nodes whose output fields can still be visible at ``target``.

        This is *not* simply "all ancestors".  Two node kinds change the
        answer:

        * a node that rebuilds the item (a ``Set`` with
          ``includeOtherFields: false``, a Code node returning fresh
          objects) masks everything before it, so the walk stops there;
        * a pass-through node (IF, Switch, Merge, NoOp, ...) can drop or
          reorder items but never changes their fields, so the walk goes
          *through* it without counting it as a producer.

        See :mod:`n8nlint.fields` for both definitions.
        """

        from .fields import is_field_reset, is_passthrough  # local: no cycle

        adj = self.data_adjacency()
        preds: dict[str, list[str]] = {n.name: [] for n in self.nodes}
        for source, targets in adj.items():
            for t in targets:
                preds.setdefault(t, []).append(source)

        by_name = {n.name: n for n in self.nodes}
        out: set[str] = set()
        stack = list(preds.get(target, ()))
        seen = set(stack)
        while stack:
            cur = stack.pop()
            node = by_name.get(cur)
            if node is not None and is_passthrough(node):
                # Transmits its input unchanged: keep walking past it.
                for prev in preds.get(cur, ()):
                    if prev not in seen:
                        seen.add(prev)
                        stack.append(prev)
                continue
            # Either the node builds new items, or it resets them: from here
            # on nothing older can still contribute a field.
            out.add(cur)
            if node is not None and is_field_reset(node):
                continue
        return out


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def _looks_like_workflow(obj: Any) -> bool:
    return isinstance(obj, dict) and ("nodes" in obj or "connections" in obj)


def _build_workflow(obj: dict, label: str, source_index: int) -> Workflow:
    raw_nodes = obj.get("nodes")
    if not isinstance(raw_nodes, list):
        raw_nodes = []

    nodes: list[Node] = []
    for idx, entry in enumerate(raw_nodes):
        if not isinstance(entry, dict):
            entry = {}
        name = entry.get("name")
        node = Node(
            index=idx,
            raw=entry,
            name=name if isinstance(name, str) else "",
            type=entry.get("type") if isinstance(entry.get("type"), str) else "",
            type_version=entry.get("typeVersion"),
            position=entry.get("position"),
            parameters=entry.get("parameters")
            if isinstance(entry.get("parameters"), dict)
            else {},
            credentials=entry.get("credentials")
            if isinstance(entry.get("credentials"), dict)
            else {},
        )
        nodes.append(node)

    connections = obj.get("connections")
    if not isinstance(connections, dict):
        connections = {}

    workflow_id = obj.get("id") if isinstance(obj.get("id"), str) else None
    name = obj.get("name") if isinstance(obj.get("name"), str) else None

    wf = Workflow(
        label=label,
        raw=obj,
        nodes=nodes,
        connections=connections,
        workflow_id=workflow_id,
        pin_data=obj.get("pinData"),
        name=name,
        active=obj.get("active"),
        settings=obj.get("settings"),
        source_index=source_index,
    )
    if "nodes" not in obj:
        wf.notes.append("top-level `nodes` key is missing")
    elif not isinstance(obj.get("nodes"), list):
        wf.notes.append("top-level `nodes` is not a list")
    if "connections" not in obj:
        wf.notes.append("top-level `connections` key is missing")
    elif not isinstance(obj.get("connections"), dict):
        wf.notes.append("top-level `connections` is not an object")
    return wf


@dataclass
class ParsedFile:
    """Everything the linter extracted from one file."""

    path: str
    workflows: list[Workflow] = field(default_factory=list)
    #: ids declared anywhere in the file (array exports carry them)
    workflow_ids: set[str] = field(default_factory=set)
    is_workflow_shaped: bool = False


def parse_text(text: str, path: str = "<memory>") -> ParsedFile:
    """Parse already-read JSON text.

    Raises :class:`WorkflowParseError` when the text is not JSON.  A file
    that *is* JSON but is not shaped like a workflow is returned with
    ``is_workflow_shaped = False`` so the caller can decide whether that is a
    finding (explicit argument) or a file to skip (directory scan).
    """

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkflowParseError(
            f"{os.path.basename(path)}: not valid JSON ({exc.msg} at line "
            f"{exc.lineno} column {exc.colno})"
        ) from exc

    parsed = ParsedFile(path=path)

    if isinstance(data, list):
        parsed.is_workflow_shaped = True
        for idx, entry in enumerate(data):
            if not isinstance(entry, dict):
                parsed.is_workflow_shaped = False
                continue
            if isinstance(entry.get("id"), str) and entry["id"]:
                parsed.workflow_ids.add(entry["id"])
            label = entry.get("name") if isinstance(entry.get("name"), str) else None
            parsed.workflows.append(
                _build_workflow(entry, label or f"workflow #{idx}", idx)
            )
        return parsed

    if isinstance(data, dict):
        parsed.is_workflow_shaped = _looks_like_workflow(data)
        if isinstance(data.get("id"), str) and data["id"]:
            parsed.workflow_ids.add(data["id"])
        label = data.get("name") if isinstance(data.get("name"), str) else None
        parsed.workflows.append(_build_workflow(data, label or "workflow", 0))
        return parsed

    # Valid JSON, but a number / string / null / bool: not a workflow.
    parsed.is_workflow_shaped = False
    return parsed


def parse_file(path: str, encoding: str = "utf-8") -> ParsedFile:
    try:
        with open(path, "r", encoding=encoding) as handle:
            text = handle.read()
    except OSError as exc:
        raise WorkflowParseError(f"{path}: cannot read file ({exc})") from exc
    return parse_text(text, path)
