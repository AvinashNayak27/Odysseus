"""Stable, data-only citations for independently retrieved evidence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Citation:
    source_id: str
    title: str
    publisher: str
    canonical_url: str
    excerpt: str
    content_sha256: str


def render_citation(citation: Citation) -> str:
    if not all(
        (
            citation.source_id,
            citation.title,
            citation.publisher,
            citation.canonical_url,
            citation.excerpt,
            citation.content_sha256,
        )
    ):
        raise ValueError("citation fields are required")
    return (
        f"[{citation.source_id}] {citation.publisher}. {citation.title}. {citation.canonical_url}\n"
        f"> {citation.excerpt}\n"
        f"Content SHA-256: `{citation.content_sha256}`"
    )
