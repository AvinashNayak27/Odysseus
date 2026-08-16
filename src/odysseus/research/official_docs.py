"""Allowlisted official-document retrieval with injectable, deterministic transports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit


class OfficialDocumentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HttpResponse:
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class OfficialDocument:
    source_id: str
    canonical_url: str
    title: str
    publisher: str
    retrieved_at: str
    content_sha256: str
    excerpt: str
    version_relevance: str
    cache_path: Path

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class OfficialDocsClient:
    """Fetch only HTTPS documents whose redirect chain stays allowlisted."""

    def __init__(
        self, transport: HttpTransport, *, allowed_domains: tuple[str, ...], cache_root: Path
    ) -> None:
        if not allowed_domains:
            raise OfficialDocumentError("at least one official domain is required")
        self._transport = transport
        self._allowed = tuple(domain.casefold().rstrip(".") for domain in allowed_domains)
        self._cache_root = cache_root.resolve(strict=False)

    def fetch(
        self, url: str, *, title: str, publisher: str, version_relevance: str, excerpt: str
    ) -> OfficialDocument:
        requested = _canonical_https_url(url)
        self._assert_allowed(requested)
        cache_path = self._cache_root / f"{hashlib.sha256(requested.encode()).hexdigest()}.json"
        if cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                final_url = _canonical_https_url(payload["canonical_url"])
                content = payload["content"]
                digest = payload["content_sha256"]
                retrieved_at = payload["retrieved_at"]
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise OfficialDocumentError("official document cache entry is invalid") from error
            if (
                final_url != requested
                or not isinstance(content, str)
                or not isinstance(digest, str)
            ):
                raise OfficialDocumentError(
                    "official document cache entry does not match requested URL"
                )
        else:
            response = self._transport.get(requested)
            if response.status < 200 or response.status >= 300:
                raise OfficialDocumentError(
                    f"official document retrieval returned HTTP {response.status}"
                )
            final_url = _canonical_https_url(response.url)
            self._assert_allowed(final_url)
            if final_url != requested:
                raise OfficialDocumentError("official document redirects are not permitted")
            try:
                content = response.body.decode("utf-8")
            except UnicodeDecodeError as error:
                raise OfficialDocumentError("official document must be UTF-8 text") from error
            digest = hashlib.sha256(response.body).hexdigest()
            retrieved_at = datetime.now(UTC).isoformat()
            payload = {
                "canonical_url": final_url,
                "content": content,
                "content_sha256": digest,
                "retrieved_at": retrieved_at,
            }
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        if not excerpt or excerpt not in content:
            raise OfficialDocumentError(
                "supporting excerpt is absent from retrieved official document"
            )
        return OfficialDocument(
            source_id=f"official-{digest[:16]}",
            canonical_url=final_url,
            title=title,
            publisher=publisher,
            retrieved_at=retrieved_at,
            content_sha256=digest,
            excerpt=excerpt,
            version_relevance=version_relevance,
            cache_path=cache_path,
        )

    def _assert_allowed(self, url: str) -> None:
        host = urlsplit(url).hostname
        if host is None or not any(
            host.casefold() == domain or host.casefold().endswith(f".{domain}")
            for domain in self._allowed
        ):
            raise OfficialDocumentError("official document host is not allowlisted")


def _canonical_https_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        raise OfficialDocumentError(
            "official document URL must be an absolute HTTPS URL without credentials"
        )
    return urlunsplit((parts.scheme, parts.netloc.casefold(), parts.path or "/", parts.query, ""))
