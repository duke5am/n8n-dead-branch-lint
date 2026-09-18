"""Finding ``$json.<field>`` reads inside node parameters.

n8n expressions (``{{ $json.email }}``), the Code node's ``$json`` helper and
the legacy ``item.json.x`` accessor all read a *top-level field of the
current input item*.  This module finds those reads and the JSON-ish path
they live at, so a finding can point at the exact parameter.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

# ``$json.field`` / ``$json["field"]`` / ``$json['field']``
_JSON_DOT = re.compile(r"\$json\s*\.\s*(?P<field>[A-Za-z_$][A-Za-z0-9_$]*)")
_JSON_BRACKET = re.compile(r"""\$json\s*\[\s*(?P<q>['"])(?P<field>.*?)(?P=q)\s*\]""")

# ``item.json.field`` / ``$input.item.json.field`` -- the idiom used inside
# Code and legacy Function nodes.  The ``item`` prefix keeps this from
# matching unrelated locals such as ``response.json``.
_ITEM_DOT = re.compile(r"\bitem\s*\.\s*json\s*\.\s*(?P<field>[A-Za-z_$][A-Za-z0-9_$]*)")
_ITEM_BRACKET = re.compile(
    r"""\bitem\s*\.\s*json\s*\[\s*(?P<q>['"])(?P<field>.*?)(?P=q)\s*\]"""
)

_READ_PATTERNS: tuple[re.Pattern[str], ...] = (
    _JSON_DOT,
    _JSON_BRACKET,
    _ITEM_DOT,
    _ITEM_BRACKET,
)

#: Item properties n8n itself puts on every item.  Reading these never means
#: "a previous node should have produced this field", so they are ignored.
BUILTIN_ITEM_FIELDS = frozenset({"json", "binary", "pairedItem", "index", "error"})


def find_json_reads(text: str) -> set[str]:
    """Return the top-level field names read from ``$json`` in ``text``."""

    if not isinstance(text, str) or ("$json" not in text and "json." not in text):
        return set()
    found: set[str] = set()
    for pattern in _READ_PATTERNS:
        for match in pattern.finditer(text):
            # ``$json["a"]["b"]`` -> only the first segment is a field name
            name = (match.group("field") or "").strip()
            if name and name not in BUILTIN_ITEM_FIELDS:
                found.add(name)
    return found


def walk_strings(obj: Any, path: str = "parameters") -> Iterator[tuple[str, str]]:
    """Yield ``(path, string)`` for every string nested inside ``obj``."""

    if isinstance(obj, str):
        yield path, obj
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_part = key if isinstance(key, str) and key.isidentifier() else None
            child = f"{path}.{key_part}" if key_part else f"{path}[{key!r}]"
            yield from walk_strings(value, child)
        return
    if isinstance(obj, (list, tuple)):
        for idx, value in enumerate(obj):
            yield from walk_strings(value, f"{path}[{idx}]")
        return


def collect_json_reads(parameters: Any) -> list[tuple[str, str]]:
    """Return ``(path, field)`` for every ``$json`` read in ``parameters``.

    Sorted and de-duplicated so the report is stable.
    """

    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path, text in walk_strings(parameters):
        for name in sorted(find_json_reads(text)):
            key = (path, name)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out
