"""What fields can a node be shown to put on its output items?

This is the static half of the ``missing-upstream-field`` rule.  It is
deliberately conservative: a node whose output cannot be determined is
reported as *unknown*, and *any* unknown node upstream of an expression read
suppresses the finding.  A linter that guesses here produces false alarms on
every workflow that touches an HTTP API.

Only two node types are understood:

* ``Set`` (``n8n-nodes-base.set``) -- field names come from its assignments.
* ``Code`` (``n8n-nodes-base.code``) -- field names are read out of literal
  ``json: { ... }`` object literals, and only when *every* ``json:`` in the
  body is such a literal.  Anything dynamic (``...spread``, a variable, a
  helper call, ``return items``) makes the node unknown.

Everything else is unknown, including triggers: a Webhook's output is
whatever the caller posted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .model import Node

#: Short type names whose output field set this module can determine.
KNOWN_SET_TYPES = frozenset({"set"})
KNOWN_CODE_TYPES = frozenset({"code"})

#: Short type names that hand their input items straight to their output.
#: They neither add nor remove fields (a Filter/Switch/IF drops whole items,
#: it does not change the shape of the ones it keeps), so the upstream field
#: walk passes *through* them instead of stopping at them.
PASSTHROUGH_TYPES = frozenset(
    {
        "if",
        "switch",
        "filter",
        "merge",
        "noOp",
        "splitInBatches",
        "loopOverItems",
        "wait",
        "removeDuplicates",
        "sort",
        "limit",
        "stopAndError",
        "respondToWebhook",
    }
)

_JSON_LITERAL = re.compile(r"(?<![\w$.])json\s*:\s*")
_JSON_KEY = re.compile(r"(?<![\w$.])json\s*:")


@dataclass(frozen=True)
class OutputShape:
    """``known`` fields, or ``unknown`` with a human-readable ``reason``."""

    known: bool
    fields: frozenset[str] = frozenset()
    reason: str = ""
    #: True when the node *replaces* the item, so nothing upstream survives it.
    resets: bool = False

    @property
    def is_known(self) -> bool:
        return self.known


def unknown(reason: str) -> OutputShape:
    return OutputShape(known=False, reason=reason)


def known(fields: Iterable[str], resets: bool) -> OutputShape:
    return OutputShape(known=True, fields=frozenset(fields), resets=resets)


# --------------------------------------------------------------------------
# Set node
# --------------------------------------------------------------------------


def _set_field_names(parameters: dict) -> set[str]:
    """Collect assignment names from every Set-node parameter layout."""

    names: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            names.add(value.strip())

    # Set v3.4+ ("Edit Fields"): parameters.assignments.assignments
    assignments = parameters.get("assignments")
    if isinstance(assignments, dict):
        for item in assignments.get("assignments") or []:
            if isinstance(item, dict):
                add(item.get("name"))
    elif isinstance(assignments, list):
        for item in assignments:
            if isinstance(item, dict):
                add(item.get("name"))

    # Set v3.x: parameters.fields.values
    fields = parameters.get("fields")
    if isinstance(fields, dict):
        for item in fields.get("values") or []:
            if isinstance(item, dict):
                add(item.get("name"))

    # Set v1 / v2: parameters.values is {type: [{name, value}, ...]}
    values = parameters.get("values")
    if isinstance(values, dict):
        for group in values.values():
            if isinstance(group, list):
                for item in group:
                    if isinstance(item, dict):
                        add(item.get("name"))

    # Oldest layout: parameters.values was a list of {name, value}
    if isinstance(values, list):
        for item in values:
            if isinstance(item, dict):
                add(item.get("name"))

    return names


def _include_other_fields(parameters: dict) -> bool | None:
    """Return the *explicit* "Include Other Input Fields" setting, if set."""

    options = parameters.get("options")
    for source in (parameters, options if isinstance(options, dict) else {}):
        value = source.get("includeOtherFields")
        if isinstance(value, bool):
            return value
    return None


def is_field_reset(node: Node) -> bool:
    """Does this node discard the incoming item's fields?

    Only returns True on *evidence*: either ``includeOtherFields: false`` is
    written out explicitly, or the node is a Set in ``raw`` (JSON) mode, or
    it is a Code node whose output is a set of literal objects.

    When n8n leaves the flag out of an export the linter cannot tell whether
    other fields are kept, so it answers False and the upstream walk
    continues -- which can only ever *suppress* a finding, never invent one.
    """

    if is_passthrough(node):
        return False
    if node.short_type in KNOWN_SET_TYPES:
        parameters = node.parameters
        if parameters.get("mode") == "raw":
            return True
        if _include_other_fields(parameters) is False:
            return True
        if parameters.get("keepOnlySet") is True:
            return True
        return False
    if node.short_type in KNOWN_CODE_TYPES:
        shape = _describe_code(node)
        return shape.known and shape.resets
    return False


def is_passthrough(node: Node) -> bool:
    """Does this node forward its input items with the same fields?

    Assumption (documented in the README): IF, Switch, Filter, Merge, NoOp,
    Split In Batches, Loop Over Items, Wait, Remove Duplicates, Sort, Limit,
    Stop And Error and Respond To Webhook do not rebuild items.  They can
    drop items or reorder them, but a field that arrived is still there.
    """

    return node.short_type in PASSTHROUGH_TYPES


def _describe_set(node: Node) -> OutputShape:
    parameters = node.parameters

    if parameters.get("mode") == "raw":
        body = parameters.get("jsonOutput")
        if not isinstance(body, str) or not body.strip():
            return unknown("Set node is in raw JSON mode with no JSON body")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return unknown("Set node raw JSON body could not be parsed")
        if not isinstance(parsed, dict) or not parsed:
            return unknown("Set node raw JSON body is not a non-empty object")
        return known(parsed.keys(), resets=True)

    names = _set_field_names(parameters)

    include_other = _include_other_fields(parameters)
    if include_other is True:
        return unknown(
            "Set node passes other input fields through, so a field missing "
            "from its assignments may still arrive from upstream"
        )
    if include_other is None:
        # No explicit flag: n8n versions differ on the default, and guessing
        # would produce false positives.  Stay quiet.
        return unknown(
            "Set node does not state whether other input fields are kept "
            "(`includeOtherFields` is absent), so its output cannot be "
            "determined"
        )
    if not names:
        return unknown("Set node has no field assignments to read names from")
    return known(names, resets=True)


# --------------------------------------------------------------------------
# Code node
# --------------------------------------------------------------------------


def _scan_object(text: str, start: int) -> tuple[str | None, int]:
    """Return the body of the ``{...}`` starting at ``start`` and its end.

    ``start`` must point at ``{``.  String literals and escapes are honoured
    so a brace inside a string does not end the scan early.
    """

    depth = 0
    idx = start
    length = len(text)
    quote: str | None = None
    while idx < length:
        char = text[idx]
        if quote is not None:
            if char == "\\":
                idx += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : idx], idx + 1
        idx += 1
    return None, length


def _split_top_level(body: str) -> list[str]:
    """Split an object body on commas that are not nested or quoted."""

    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    idx = 0
    while idx < len(body):
        char = body[idx]
        if quote is not None:
            current.append(char)
            if char == "\\":
                if idx + 1 < len(body):
                    current.append(body[idx + 1])
                    idx += 2
                    continue
            elif char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
            current.append(char)
        elif char in "{[(":
            depth += 1
            current.append(char)
        elif char in "}])":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        idx += 1
    if "".join(current).strip():
        parts.append("".join(current))
    return parts


def _key_of(part: str) -> str | None:
    """``"a: 1"`` -> ``"a"``; ``None`` when the part is not a plain key."""

    if ":" not in part:
        return None
    key_part = part.split(":", 1)[0].strip()
    if not key_part or key_part.startswith("..."):
        return None
    if (key_part.startswith("'") and key_part.endswith("'")) or (
        key_part.startswith('"') and key_part.endswith('"')
    ):
        return key_part[1:-1]
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", key_part):
        return key_part
    if re.fullmatch(r"\d+", key_part):
        return key_part
    return None


def extract_json_literal_keys(code: str) -> tuple[set[str], bool]:
    """Read keys out of ``json: {...}`` literals.  Returns ``(keys, complete)``.

    ``complete`` is False as soon as anything in the body is not a plain
    literal object: a spread, a shorthand property, a computed key, or a
    ``json:`` assignment whose value is not ``{``.  Callers must treat an
    incomplete result as "unknown" rather than as "these are all the fields".
    """

    keys: set[str] = set()
    found_any = False
    pos = 0
    while True:
        match = _JSON_LITERAL.search(code, pos)
        if match is None:
            break
        found_any = True
        brace = code.find("{", match.end())
        if brace == -1 or code[match.end() : brace].strip():
            # ``json: someVariable`` / ``json: buildItem()`` -- not a literal
            return set(), False
        body, end = _scan_object(code, brace)
        if body is None:
            return set(), False
        for part in _split_top_level(body):
            if not part.strip():
                continue
            key = _key_of(part)
            if key is None:
                return set(), False
            keys.add(key)
        pos = end

    if not found_any:
        return set(), False
    return keys, True


def _describe_code(node: Node) -> OutputShape:
    parameters = node.parameters
    mode = parameters.get("mode")
    code = parameters.get("jsCode")
    if not isinstance(code, str) or not code.strip():
        code = parameters.get("functionCode")
    if not isinstance(code, str) or not code.strip():
        return unknown("Code node has no body to analyse")

    if mode == "json":
        try:
            parsed = json.loads(code)
        except json.JSONDecodeError:
            return unknown("Code node JSON body could not be parsed")
        if not isinstance(parsed, dict) or not parsed:
            return unknown("Code node JSON body is not a non-empty object")
        return known(parsed.keys(), resets=True)

    keys, complete = extract_json_literal_keys(code)
    if not complete:
        return unknown(
            "Code node builds its items dynamically (spread, variable or "
            "helper call), so its output fields cannot be read statically"
        )
    if not keys:
        return unknown(
            "Code node body contains no literal `json: { ... }` object to "
            "read field names from"
        )
    return known(keys, resets=True)


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


def describe_output(node: Node) -> OutputShape:
    """Best static description of what this node puts on its output items."""

    short = node.short_type
    if short in KNOWN_SET_TYPES:
        return _describe_set(node)
    if short in KNOWN_CODE_TYPES:
        return _describe_code(node)
    return unknown(
        f"`{short or node.type or 'unknown'}` node output is not statically "
        f"known"
    )
