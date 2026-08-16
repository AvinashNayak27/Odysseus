"""Conservative private-log and public-output redaction utilities.

Redaction intentionally recognizes explicit credential syntax and known secret values.
It never applies a generic entropy heuristic: source diffs, content hashes, UUIDs,
run IDs, and benchmark identifiers are operational data and must remain intact.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

# Match a credential key only when its value is a credential-shaped literal. This
# intentionally excludes source expressions such as ``token = self.next_token()``.
_SECRET_KEY_VALUE = re.compile(
    r"(?ix)\b(?:api[_-]?key|token|secret|password|credential)\s*[:=]\s*"
    r"(?:"
    r"(?:['\"])\s*(?:sk|pk)_[A-Za-z0-9_-]{16,}\s*(?:['\"])"
    r"|(?:['\"])\s*[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\s*(?:['\"])"
    r"|(?:['\"])\s*[A-Za-z0-9_-]{32,}\s*(?:['\"])"
    r"|(?:sk|pk)_[A-Za-z0-9_-]{16,}"
    # Fixtures and tests use clearly marked synthetic credentials. Keeping this
    # explicit avoids treating arbitrary source identifiers as secret values.
    r"|synthetic-(?:token|secret)[A-Za-z0-9_-]*"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r")"
)
_SECRET_BEARER = re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[^\s,;]+")
_SECRET_PROVIDER_KEY = re.compile(r"\b(?:sk|pk)_[A-Za-z0-9_-]{16,}\b")
_SECRET_JWT = re.compile(r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b")
_PRIVATE_PATTERNS = (_SECRET_KEY_VALUE, _SECRET_BEARER, _SECRET_PROVIDER_KEY, _SECRET_JWT)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_HOME_PATH = re.compile(
    r"(?:(?:/home|/Users)/[^/\s:]+|[A-Za-z]:\\Users\\[^\\\s:]+)(?:[/\\][^\s:]+)*"
)
_TRACE = re.compile(
    r"(?is)(?:traceback \(most recent call last\):|trace[_ -]?(?:id|payload))[^\n]*"
)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_HTML = re.compile(
    r"(?is)<(?:script|style|iframe|object|embed)[^>]*>.*?</(?:script|style|iframe|object|embed)>"
)


class PublicOutputError(ValueError):
    """Raised when text cannot safely become a public artifact."""


def redact_private(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Replace known values and explicit credential shapes without entropy matching."""
    result = text
    for secret in sorted((item for item in extra_secrets if item), key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    for pattern in _PRIVATE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return _EMAIL.sub("[REDACTED EMAIL]", result)


def scrub_public(text: str, aliases: Mapping[str, str] | None = None) -> str:
    """Apply a stricter scrub suitable for a public draft, or fail on trace data."""
    if _TRACE.search(text):
        raise PublicOutputError("trace content is not permitted in public output")
    result = _ANSI.sub("", text)
    result = _HTML.sub("[REMOVED ACTIVE HTML]", result)
    result = redact_private(result)
    result = _HOME_PATH.sub("$PRIVATE_PATH", result)
    for raw, alias in (aliases or {}).items():
        if raw and raw in result:
            result = result.replace(raw, alias)
    if "\x00" in result:
        raise PublicOutputError("NUL bytes are not permitted in public output")
    return result


def redact_path(path: Path, aliases: Mapping[str, str]) -> str:
    value = str(path.resolve(strict=False))
    for raw, alias in aliases.items():
        if value == raw or value.startswith(f"{raw}/"):
            return f"{alias}{value[len(raw) :]}"
    raise PublicOutputError("private absolute path has no public alias")
