"""Deterministic two-phase Yukon selection records.

Only `benchmark list` and `benchmark show` facts are used for shortlisting.
Clone-derived facts (language, root/workdir, editable paths, and setup/run
feasibility) are recorded later while candidates are validated in order.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256

from odysseus.yukon.parser import BenchmarkDetails, BenchmarkSummary, is_benchmark_uuid

SELECTION_ALGORITHM_VERSION = "yukon-two-phase-v1"


@dataclass(frozen=True, slots=True)
class ShortlistCandidate:
    benchmark_id: str
    benchmark: str
    status: str
    category: str
    goal: str
    source_url: str
    source_ref: str
    source_branch: str
    score_path: str
    score: int
    score_components: tuple[tuple[str, int], ...]
    excluded_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryRecord:
    algorithm_version: str
    retrieved_at: str
    source_sha256: str
    candidates: tuple[ShortlistCandidate, ...]

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @property
    def sha256(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CloneValidation:
    benchmark_id: str
    accepted: bool
    reason: str
    repository_root: str | None = None
    benchmark_workdir: str | None = None
    editable_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SelectionRecord:
    discovery_sha256: str
    selected_benchmark_id: str | None
    validations: tuple[CloneValidation, ...]
    selection_algorithm_version: str = SELECTION_ALGORITHM_VERSION


def _is_open(status: str) -> bool:
    return status.strip().casefold() in {"open", "active"}


def _objective_score(goal: str) -> int:
    """Accept Yukon list direction and the detail view's score-direction prose."""
    normalized = " ".join(goal.casefold().split())
    supported = {"higher", "lower", "higher score is better", "lower score is better"}
    return 20 if normalized in supported else 0


def _metadata_score(details: BenchmarkDetails) -> int:
    # These are list/show facts, not a claim that a clone will be viable.
    complete = (details.source_url, details.source_ref, details.source_branch, details.score_path)
    return 10 if all(value.strip() for value in complete) else 0


def build_shortlist(
    summaries: Iterable[BenchmarkSummary],
    details: Iterable[BenchmarkDetails],
    *,
    retrieved_at: datetime | None = None,
) -> DiscoveryRecord:
    """Build the immutable pre-clone shortlist, stable by score then canonical ID."""
    by_id = {item.benchmark_id: item for item in details}
    candidates: list[ShortlistCandidate] = []
    for summary in summaries:
        detail = by_id.get(summary.benchmark_id)
        exclusion: str | None = None
        components: tuple[tuple[str, int], ...]
        if not is_benchmark_uuid(summary.benchmark_id):
            exclusion = "benchmark ID is not a Yukon UUID"
            components = (
                ("open_status", 0),
                ("supported_objective", 0),
                ("metadata_completeness", 0),
            )
            source_url = source_ref = source_branch = score_path = ""
        elif detail is None:
            exclusion = "benchmark show record missing"
            components = (
                ("open_status", 0),
                ("supported_objective", 0),
                ("metadata_completeness", 0),
            )
            source_url = source_ref = source_branch = score_path = ""
        else:
            open_score = 70 if _is_open(summary.status) else 0
            objective = _objective_score(detail.goal)
            metadata = _metadata_score(detail)
            components = (
                ("open_status", open_score),
                ("supported_objective", objective),
                ("metadata_completeness", metadata),
            )
            source_url, source_ref, source_branch, score_path = (
                detail.source_url,
                detail.source_ref,
                detail.source_branch,
                detail.score_path,
            )
            if not _is_open(summary.status):
                exclusion = "benchmark is not open"
            elif objective == 0:
                exclusion = "unsupported objective"
        score = sum(value for _, value in components) if exclusion is None else 0
        candidates.append(
            ShortlistCandidate(
                summary.benchmark_id,
                summary.benchmark,
                summary.status,
                summary.category,
                summary.goal,
                source_url,
                source_ref,
                source_branch,
                score_path,
                score,
                components,
                exclusion,
            )
        )
    timestamp = (retrieved_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    return DiscoveryRecord(
        SELECTION_ALGORITHM_VERSION,
        timestamp,
        "",  # caller binds the concatenated scrubbed CLI snapshots before persistence
        tuple(sorted(candidates, key=lambda item: (-item.score, item.benchmark_id))),
    )


def bind_source_snapshot(record: DiscoveryRecord, snapshots: Iterable[str]) -> DiscoveryRecord:
    """Bind exact sanitized list/show snapshots to an otherwise immutable discovery record."""
    digest = sha256("\n\x1e\n".join(snapshots).encode("utf-8")).hexdigest()
    return DiscoveryRecord(record.algorithm_version, record.retrieved_at, digest, record.candidates)


def select_after_validation(
    shortlist: DiscoveryRecord, validations: Iterable[CloneValidation]
) -> SelectionRecord:
    """Select first accepted candidate in shortlist order and preserve all failures."""
    received = {item.benchmark_id: item for item in validations}
    if any(not is_benchmark_uuid(item.benchmark_id) for item in received.values()):
        raise ValueError("clone validation benchmark ID is not a Yukon UUID")
    shortlisted_ids = {candidate.benchmark_id for candidate in shortlist.candidates}
    if any(item.benchmark_id not in shortlisted_ids for item in received.values()):
        raise ValueError("clone validation does not belong to this shortlist")
    history: list[CloneValidation] = []
    selected: str | None = None
    for candidate in shortlist.candidates:
        if candidate.excluded_reason is not None:
            continue
        outcome = received.get(candidate.benchmark_id)
        if outcome is None:
            continue
        history.append(outcome)
        if outcome.accepted:
            selected = candidate.benchmark_id
            break
    return SelectionRecord(shortlist.sha256, selected, tuple(history))
