"""Derive bounded research questions while preserving trusted/untrusted boundaries."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256

from odysseus.adapters.base import canonical_request_json


@dataclass(frozen=True, slots=True)
class ResearchQuestion:
    question_id: str
    query: str
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    claim_id: str
    claim: str
    source_ids: tuple[str, ...]
    excerpts: tuple[str, ...]
    hotspot: str | None
    confidence: str
    independently_verified: bool

    def require_primary_support(self) -> None:
        if not self.independently_verified or not self.source_ids or not self.excerpts:
            raise ValueError("claims require independently retrieved source IDs and excerpts")


def derive_questions(
    *,
    objective: str,
    adapter: str,
    hotspots: Iterable[str],
    editable_files: Iterable[str],
    frontier_claims: Iterable[str] = (),
    maximum: int = 8,
) -> tuple[ResearchQuestion, ...]:
    """Create deterministic queries; frontier claims remain quoted, untrusted context."""
    if maximum < 1 or not objective.strip() or not adapter.strip():
        raise ValueError("objective, adapter, and positive maximum are required")
    questions: list[tuple[str, tuple[str, ...]]] = []
    for hotspot in sorted({item.strip() for item in hotspots if item.strip()}):
        questions.append(
            (f"{adapter} performance {hotspot} {objective}", ("profile-hotspot", hotspot))
        )
    if not questions:
        questions.append((f"{adapter} performance profiling {objective}", ("profiling-gap",)))
    for claim in sorted({item.strip() for item in frontier_claims if item.strip()}):
        # The claim cannot introduce a command: it only becomes an exact quoted
        # search term and always retains its untrusted provenance marker.
        questions.append(
            (f'{adapter} performance evidence "{claim[:160]}"', ("untrusted-frontier-claim",))
        )
    files = tuple(sorted({item.strip() for item in editable_files if item.strip()}))
    if files:
        questions.append(
            (f"{adapter} optimization {files[0]} {objective}", ("editable-surface", files[0]))
        )
    output: list[ResearchQuestion] = []
    for query, provenance in questions:
        if len(output) >= maximum:
            break
        digest = sha256(canonical_request_json((query, provenance)).encode("utf-8")).hexdigest()[
            :16
        ]
        output.append(ResearchQuestion(f"question-{digest}", query, provenance))
    return tuple(output)
