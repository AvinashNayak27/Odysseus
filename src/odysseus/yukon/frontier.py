"""Submission-only frontier quarantine; text remains untrusted inert evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from odysseus.redaction import redact_private
from odysseus.yukon.parser import is_benchmark_uuid, strip_ansi

_ACTIVE_OPEN = re.compile(r"(?is)<\s*(script|style|iframe|object|embed)\b[^>]*>")
_ACTIVE_CLOSE = re.compile(r"(?is)<\s*/\s*(script|style|iframe|object|embed)\s*>")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>()]+")
_TRACE = re.compile(
    r"(?is)(?:traceback \(most recent call last\):|trace[_ -]?(?:id|payload))[^\n]*"
)
_NUL = re.compile("\x00")


@dataclass(frozen=True, slots=True)
class FrontierArtifact:
    benchmark_id: str
    path: Path
    content_sha256: str
    byte_count: int
    source: str = "yukon submissions --all"


def _inert_submission_text(text: str) -> str:
    """Redact and neutralize hostile input without public-render validation or I/O."""
    result = strip_ansi(text)
    result = _NUL.sub("[NUL]", result)
    result = _ACTIVE_OPEN.sub(r"[NEUTERED HTML \1]", result)
    result = _ACTIVE_CLOSE.sub(r"[/NEUTERED HTML \1]", result)
    result = _URL.sub("[UNFOLLOWED URL]", result)
    result = _TRACE.sub("[REDACTED TRACE]", result)
    # This is a private, inert evidence store: redact credentials but never
    # reject trace-like untrusted input as a public renderer would.
    return redact_private(result)


def quarantine_submissions(
    *, benchmark_id: str, text: str, quarantine_root: Path
) -> FrontierArtifact:
    """Persist terminal-safe submission text; never follow URLs, notes, or patches."""
    if not is_benchmark_uuid(benchmark_id):
        raise ValueError("benchmark ID must have the Yukon UUID shape")
    normalized = _inert_submission_text(text)
    encoded = normalized.encode("utf-8")
    digest = sha256(encoded).hexdigest()
    directory = quarantine_root.resolve(strict=False) / benchmark_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"submissions-{digest}.txt"
    if not path.exists():
        path.write_bytes(encoded)
    return FrontierArtifact(benchmark_id, path, digest, len(encoded))
