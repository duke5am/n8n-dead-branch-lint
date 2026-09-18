"""Detecting literal secrets pasted into node parameters.

The patterns below are *shapes*, not a mystery-meat entropy scanner: each one
matches a credential format that has a published, recognisable prefix or
structure, plus a generic ``key=value`` check for long opaque values stored
under a secret-sounding key.

Every match is run past :data:`PLACEHOLDER_HINTS` first, so the deliberately
fake values used in this repo's own fixtures and in most real READMEs
(``CHANGEME``, ``example-token-1234``, ``{{ $env.TOKEN }}``) are not reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator

from .expressions import walk_strings

#: Substrings that mark a value as a placeholder rather than a live secret.
PLACEHOLDER_HINTS: tuple[str, ...] = (
    "changeme",
    "change_me",
    "change-me",
    "example",
    "placeholder",
    "your_",
    "your-",
    "yourkey",
    "xxxx",
    "dummy",
    "redacted",
    "insert_",
    "todo",
    "***",
    "{{",
    "}}",
    "$env.",
    "$credentials",
    "<",
    ">",
)

#: ``(rule label, compiled pattern, what the match looks like)``
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "bearer token",
        re.compile(r"(?i)\bbearer\s+([A-Za-z0-9\-._~+/]{12,}={0,2})"),
        "a literal `Authorization: Bearer <token>` header value",
    ),
    (
        "basic auth header",
        re.compile(r"(?i)\bbasic\s+([A-Za-z0-9+/]{16,}={0,2})"),
        "a literal `Authorization: Basic <base64>` header value",
    ),
    (
        "JSON Web Token",
        re.compile(
            r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"
        ),
        "a literal JSON Web Token (its payload is readable by anyone)",
    ),
    (
        "private key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "a literal PEM private key",
    ),
    (
        "AWS access key id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "a literal AWS access key id",
    ),
    (
        "Google API key",
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
        "a literal Google API key",
    ),
    (
        "Slack token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
        "a literal Slack API token",
    ),
    (
        "Slack incoming webhook URL",
        re.compile(
            r"https://hooks\.slack\.com/services/[A-Za-z0-9_\-]+/"
            r"[A-Za-z0-9_\-]+/[A-Za-z0-9_\-]+"
        ),
        "a literal Slack incoming-webhook URL (anyone holding it can post to the channel)",
    ),
    (
        "Discord webhook URL",
        re.compile(
            r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/"
            r"\d+/[A-Za-z0-9_\-]+"
        ),
        "a literal Discord webhook URL",
    ),
    (
        "Telegram bot URL",
        re.compile(r"https://api\.telegram\.org/bot\d+:[A-Za-z0-9_\-]{30,}"),
        "a literal Telegram bot URL containing the bot token",
    ),
    (
        "credential in a key/value pair",
        re.compile(
            r"(?i)\b(?:api[_-]?key|apikey|access[_-]?key|secret|"
            r"client[_-]?secret|auth[_-]?token|access[_-]?token|"
            r"refresh[_-]?token|private[_-]?key|password|passwd|pwd)"
            r"\b\s*[:=]\s*[\"']?([^\s\"'&,;}\]]{12,})"
        ),
        "a secret-shaped `name=value` pair",
    ),
)

#: Names of HTTP header / query / body parameters that indicate a secret.
SECRET_PARAM_NAMES = re.compile(
    r"(?i)^(?:x[\s_-])?"
    r"(?:authorization|auth|api[\s_-]?key|apikey|access[\s_-]?key|"
    r"token|access[\s_-]?token|auth[\s_-]?token|bearer|secret|"
    r"client[\s_-]?secret|password|passwd|pwd|private[\s_-]?key|"
    r"subscription[\s_-]?key)$"
)

#: Parameter names that are definitely *not* secrets even though they match
#: the pattern above (they hold ids, not credentials).
BENIGN_PARAM_NAMES = frozenset(
    {"tokenid", "token_id", "secretname", "secret_name", "password_field"}
)


@dataclass(frozen=True)
class SecretHit:
    label: str
    description: str
    path: str
    excerpt: str
    #: value with any auth scheme stripped, used to collapse the same secret
    #: reported twice at one location
    dedupe: str = ""


def is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(hint in lowered for hint in PLACEHOLDER_HINTS)


def _looks_literal(value: str) -> bool:
    """True when a value is a real literal rather than an expression/ref."""

    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if len(stripped) < 12:
        return False
    if is_placeholder(stripped):
        return False
    if stripped.startswith("="):  # n8n marks expressions with a leading "="
        return False
    return True


def _redact(value: str, keep: int = 6) -> str:
    """Never echo a full secret back into the report."""

    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "..." + "*" * 4


def _dedupe_value(value: str) -> str:
    """Collapse ``Bearer abc`` and ``abc`` to the same key."""

    return re.sub(r"(?i)^(bearer|basic|token)\s+", "", value.strip())


def scan_string(value: str, path: str) -> list[SecretHit]:
    """Run every pattern over one string."""

    hits: list[SecretHit] = []
    for label, pattern, description in SECRET_PATTERNS:
        for match in pattern.finditer(value):
            captured = match.group(1) if match.groups() else match.group(0)
            if is_placeholder(match.group(0)) or is_placeholder(captured):
                continue
            if not _looks_literal(captured) and label != "private key":
                continue
            hits.append(
                SecretHit(
                    label=label,
                    description=description,
                    path=path,
                    excerpt=_redact(captured),
                    dedupe=_redact(_dedupe_value(captured)),
                )
            )
    return hits


def _iter_name_value_pairs(obj: Any, path: str) -> Iterator[tuple[str, str, str]]:
    """Yield ``(path, name, value)`` for every ``{name, value}`` pair.

    n8n stores HTTP headers, query parameters and body parameters as lists of
    these, so this catches ``{"name": "X-Api-Key", "value": "..."}`` which no
    ``key=value`` regex would ever see.
    """

    if isinstance(obj, dict):
        name = obj.get("name")
        value = obj.get("value")
        if isinstance(name, str) and isinstance(value, str):
            yield path, name, value
        for key, child in obj.items():
            yield from _iter_name_value_pairs(child, f"{path}.{key}")
    elif isinstance(obj, list):
        for idx, child in enumerate(obj):
            yield from _iter_name_value_pairs(child, f"{path}[{idx}]")


def scan_parameters(parameters: Any) -> list[SecretHit]:
    """Find literal secrets anywhere inside a node's ``parameters``."""

    hits: list[SecretHit] = []
    for path, text in walk_strings(parameters):
        hits.extend(scan_string(text, path))

    for path, name, value in _iter_name_value_pairs(parameters, "parameters"):
        if name.strip().lower() in BENIGN_PARAM_NAMES:
            continue
        if not SECRET_PARAM_NAMES.match(name.strip()):
            continue
        if not _looks_literal(value):
            continue
        hits.append(
            SecretHit(
                label=f"literal value for `{name}`",
                description=(
                    f"a literal value in the `{name}` parameter instead of a "
                    f"credential reference"
                ),
                path=f"{path}.value",
                excerpt=_redact(value),
                dedupe=_redact(_dedupe_value(value)),
            )
        )

    # De-duplicate: the same secret is often caught twice at one location
    # (a `Bearer <token>` header value matches the bearer pattern *and* the
    # name/value pass).  One finding per (path, value) is enough.
    unique: list[SecretHit] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        key = (hit.path, hit.dedupe or hit.excerpt)
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    unique.sort(key=lambda h: (h.path, h.label))
    return unique
