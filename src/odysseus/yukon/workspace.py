"""Yukon root/workdir contracts, restart gates, and detached-worktree topology."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from odysseus.models import CommandResult
from odysseus.paths import PathPolicyError, canonical, require_runtime_root
from odysseus.yukon.parser import CloneReport


class WorkspaceContractError(ValueError):
    pass


class TopologyStatus(StrEnum):
    VERIFIED = "verified"
    UNSUPPORTED = "unsupported"
    DECISION_REQUIRED = "decision_required"


class TopologyDecision(StrEnum):
    CANONICAL_ONLY = "canonical_only"
    APPROVED_ISOLATED = "approved_isolated"


@dataclass(frozen=True, slots=True)
class WorkspaceContract:
    repository_root: Path
    benchmark_workdir: Path
    editable_paths: tuple[Path, ...]
    runtime_root: Path

    @classmethod
    def from_clone_report(cls, report: CloneReport, *, runtime_root: Path) -> WorkspaceContract:
        root = canonical(Path(report.root))
        workdir = canonical(Path(report.workdir))
        if workdir != root and root not in workdir.parents:
            raise WorkspaceContractError(
                "Yukon workdir must be inside the reported repository root"
            )
        checked_runtime = require_runtime_root(runtime_root, (root,))
        editable: list[Path] = []
        for raw in report.editable_paths:
            candidate = Path(raw)
            if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
                raise WorkspaceContractError(
                    "editablePaths must be non-empty relative workdir paths"
                )
            resolved = canonical(workdir.joinpath(*candidate.parts))
            if resolved != workdir and workdir not in resolved.parents:
                raise WorkspaceContractError("editable path escapes the workdir")
            editable.append(resolved)
        if len(set(editable)) != len(editable):
            raise WorkspaceContractError("editablePaths resolve ambiguously")
        return cls(root, workdir, tuple(editable), checked_runtime)

    def assert_editable(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
            raise PathPolicyError("edit path must be non-empty and relative")
        target = canonical(self.benchmark_workdir.joinpath(*candidate.parts))
        if not any(target == root or root in target.parents for root in self.editable_paths):
            raise PathPolicyError("edit path is outside Yukon editablePaths")
        return target


@dataclass(frozen=True, slots=True)
class RestartGate:
    restart_required: bool
    repository_root: Path | None = None

    @classmethod
    def from_clone_report(cls, report: CloneReport) -> RestartGate:
        return cls(report.restart_requested, canonical(Path(report.root)))

    def require_relaunched_at(self, process_root: Path | None) -> None:
        if not self.restart_required:
            return
        if process_root is None or canonical(process_root) != self.repository_root:
            raise WorkspaceContractError(
                f"Yukon requested restart; relaunch at {self.repository_root} before setup or run"
            )

    def hard_stop(self) -> None:
        if self.restart_required:
            raise WorkspaceContractError(
                f"Yukon requested restart; this process must stop at {self.repository_root}"
            )


@dataclass(frozen=True, slots=True)
class TopologyProbe:
    """A persisted contract for whether Yukon permits `run` in a detached worktree."""

    status: TopologyStatus
    checkout: Path
    observed_command: tuple[str, ...]
    result_exit_code: int | None
    evidence_sha256: str
    detached_worktree: bool = False
    decision: TopologyDecision | None = None

    @classmethod
    def unverified(cls, *, checkout: Path, evidence_sha256: str) -> TopologyProbe:
        return cls(
            TopologyStatus.DECISION_REQUIRED,
            canonical(checkout),
            ("yukon", "run"),
            None,
            evidence_sha256,
        )

    @classmethod
    def observed(
        cls,
        *,
        checkout: Path,
        exit_code: int | None,
        evidence_sha256: str,
        detached_worktree: bool = False,
    ) -> TopologyProbe:
        status = TopologyStatus.VERIFIED if exit_code == 0 else TopologyStatus.UNSUPPORTED
        return cls(
            status,
            canonical(checkout),
            ("yukon", "run"),
            exit_code,
            evidence_sha256,
            detached_worktree,
        )

    def require_isolated_run(self) -> None:
        if self.status is not TopologyStatus.VERIFIED or not self.detached_worktree:
            raise WorkspaceContractError(
                "isolated Yukon run mode is unsupported or unverified; a detached-worktree "
                "topology decision is required"
            )
        if self.decision not in (None, TopologyDecision.APPROVED_ISOLATED):
            raise WorkspaceContractError("topology decision permits canonical-only runs")


def record_detached_run_probe(*, checkout: Path, result: CommandResult) -> TopologyProbe:
    """Convert an explicitly performed detached-worktree run into a topology contract.

    Callers must persist the scrubbed result as evidence and may only schedule
    isolated comparison trials after this yields ``verified``.
    """
    payload = "\n".join((result.stdout, result.stderr)).encode("utf-8")
    exit_code = None if result.timed_out else result.exit_code
    return TopologyProbe.observed(
        checkout=checkout,
        exit_code=exit_code,
        evidence_sha256=sha256(payload).hexdigest(),
        detached_worktree=True,
    )
