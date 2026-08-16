"""Cached scholarly metadata interfaces, designed for fake/offline transports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote_plus

from odysseus.research.official_docs import HttpTransport


class ScholarlyMetadataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScholarlyWork:
    provider: str
    title: str
    year: int | None
    doi: str | None
    arxiv_id: str | None
    canonical_url: str
    peer_reviewed: bool

    def dedupe_key(self) -> tuple[str, str]:
        if self.doi:
            return ("doi", self.doi.casefold())
        if self.arxiv_id:
            return ("arxiv", self.arxiv_id.casefold())
        return ("title", " ".join(self.title.casefold().split()))

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class ScholarlyMetadataProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int) -> tuple[ScholarlyWork, ...]: ...


class JsonScholarlyProvider:
    """Minimal generic JSON endpoint adapter used for Crossref/OpenAlex-style data."""

    def __init__(
        self, name: str, endpoint: str, transport: HttpTransport, cache_root: Path
    ) -> None:
        self.name = name
        self._endpoint = endpoint.rstrip("/")
        self._transport = transport
        self._cache_root = cache_root.resolve(strict=False)

    def search(self, query: str, *, limit: int) -> tuple[ScholarlyWork, ...]:
        if not query.strip() or limit < 1:
            raise ScholarlyMetadataError("query and positive limit are required")
        url = f"{self._endpoint}?query={quote_plus(query)}&rows={limit}"
        cache = self._cache_root / f"{self.name}-{_sha(url)}.json"
        if cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
        else:
            response = self._transport.get(url)
            if response.status != 200:
                raise ScholarlyMetadataError(
                    f"{self.name} metadata request returned HTTP {response.status}"
                )
            payload = json.loads(response.body.decode("utf-8"))
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        items = payload.get("items", payload.get("message", {}).get("items", []))
        if not isinstance(items, list):
            raise ScholarlyMetadataError(f"{self.name} response does not contain metadata items")
        return tuple(_parse_item(self.name, item) for item in items[:limit])


def deduplicate_works(works: tuple[ScholarlyWork, ...]) -> tuple[ScholarlyWork, ...]:
    """Prefer peer-reviewed records, then a canonical stable provider/name order."""
    selected: dict[tuple[str, str], ScholarlyWork] = {}
    for work in works:
        key = work.dedupe_key()
        current = selected.get(key)
        if (
            current is None
            or (work.peer_reviewed and not current.peer_reviewed)
            or (
                work.peer_reviewed == current.peer_reviewed
                and (work.provider, work.canonical_url) < (current.provider, current.canonical_url)
            )
        ):
            selected[key] = work
    return tuple(
        sorted(
            selected.values(),
            key=lambda item: (not item.peer_reviewed, item.title.casefold(), item.canonical_url),
        )
    )


def _parse_item(provider: str, item: object) -> ScholarlyWork:
    if not isinstance(item, dict):
        raise ScholarlyMetadataError("metadata item must be an object")
    title_value = item.get("title", "")
    title = title_value[0] if isinstance(title_value, list) and title_value else title_value
    if not isinstance(title, str) or not title.strip():
        raise ScholarlyMetadataError("metadata item lacks a title")
    year = _published_year(item)
    return ScholarlyWork(
        provider,
        title.strip(),
        year if isinstance(year, int) else None,
        item.get("DOI") or item.get("doi"),
        item.get("arxiv_id") or item.get("arxiv"),
        item.get("URL") or item.get("url") or "",
        bool(
            item.get(
                "peer_reviewed", item.get("type") in {"journal-article", "proceedings-article"}
            )
        ),
    )


def _published_year(item: dict[object, object]) -> int | None:
    direct = item.get("year")
    if isinstance(direct, int):
        return direct
    published = item.get("published")
    if not isinstance(published, dict):
        return None
    date_parts = published.get("date-parts")
    if not isinstance(date_parts, list) or not date_parts:
        return None
    first = date_parts[0]
    if not isinstance(first, list) or not first:
        return None
    year = first[0]
    return year if isinstance(year, int) else None


def _sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
