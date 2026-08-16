"""Stable, serializable records shared by future workflow stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 1


class StageStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEGRADED = "degraded"
    APPROVAL_REQUIRED = "approval_required"
    RESTART_REQUIRED = "restart_required"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    executable: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: float
    allowed_executables: tuple[str, ...]
    allowed_argv_prefixes: tuple[tuple[str, ...], ...] = ()
    max_output_bytes: int = 1_048_576
    inherit_environment: bool = False
    # None preserves the inherited environment without inspecting values in the
    # parent process; a tuple constructs a narrow inherited environment.
    environment_allowlist: tuple[str, ...] | None = ()
    extra_redaction_values: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    duration_seconds: float
    output_truncated: bool


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    run_id: str
    stage: str
    status: StageStatus
    occurred_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    schemaVersion: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ApprovalPlan:
    action: str
    displayed_plan: str
    benchmark_id: str
    source_path: str
    target_path: str
    command: CommandSpec


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: str
    canonical_url: str
    title: str
    publisher: str
    retrieved_at: str
    content_sha256: str
    schemaVersion: int = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    claim: str
    source_ids: tuple[str, ...]
    excerpts: tuple[str, ...]
    confidence: str
    schemaVersion: int = SCHEMA_VERSION
