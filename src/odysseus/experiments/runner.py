"""Detached harness-owned Git worktree experiment execution.

This layer never resets, cleans, removes, or otherwise mutates the canonical
checkout. Worktrees remain for inspection; failed ones are quarantined by
classification only, not deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from odysseus.candidates.validation import PatchValidationError, validate_unified_diff
from odysseus.commands import CommandRunner
from odysseus.experiments.environment import EnvironmentFingerprint
from odysseus.experiments.ledger import ExperimentRecord, TrialObservation, sha256_text
from odysseus.experiments.statistics import (
    CandidateClassification,
    Objective,
    bootstrap_relative_effect_ci,
    classify_candidate,
    interleaved_trial_schedule,
)
from odysseus.models import CommandResult, CommandSpec
from odysseus.paths import assert_editable_path, canonical, require_runtime_root
from odysseus.yukon.workspace import TopologyProbe, WorkspaceContract, WorkspaceContractError


class ExperimentError(RuntimeError):
    pass


class MeasurementExecutor(Protocol):
    """A Yukon-backed executor; production adapters must route it through CommandRunner."""

    def __call__(self, workdir: Path, *, timeout_seconds: float) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class CandidatePatch:
    candidate_id: str
    unified_diff: str
    expected_paths: tuple[str, ...]

    @property
    def sha256(self) -> str:
        return sha256(self.unified_diff.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExperimentPlan:
    objective: Objective
    baseline_sha: str
    canonical_workdir: Path
    repetitions: int
    seed: int
    timeout_seconds: float
    environment: EnvironmentFingerprint
    topology_probe: TopologyProbe
    expected_editable_hash: str
    bootstrap_samples: int = 10_000


class ExperimentRunner:
    """Apply one validated diff to a detached worktree and record every trial."""

    def __init__(
        self, command_runner: CommandRunner, runtime_root: Path, measure: MeasurementExecutor
    ):
        self._commands = command_runner
        self._runtime_root = require_runtime_root(runtime_root)
        self._measure = measure

    def plan_worktree(
        self, contract: WorkspaceContract, patch: CandidatePatch, plan: ExperimentPlan
    ) -> Path:
        self._validate_plan(contract, patch, plan)
        token = f"experiment-{_safe_identifier(patch.candidate_id)}-{patch.sha256[:12]}"
        return self._runtime_root / "worktrees" / token

    def run(
        self, contract: WorkspaceContract, patch: CandidatePatch, plan: ExperimentPlan
    ) -> ExperimentRecord:
        self._validate_plan(contract, patch, plan)
        worktree = self.plan_worktree(contract, patch, plan)
        if worktree.exists():
            raise ExperimentError(
                "harness-owned worktree path already exists; records are immutable"
            )
        self._assert_canonical_checkout(contract, plan)
        self._git(
            contract.repository_root,
            ("worktree", "add", "--detach", str(worktree), plan.baseline_sha),
        )
        mapped_workdir = self._map_nested_workdir(contract, worktree)
        try:
            self._apply_validated_patch(contract, worktree, mapped_workdir, patch)
            correctness = self._measure(mapped_workdir, timeout_seconds=plan.timeout_seconds)
            if correctness.exit_code != 0 or correctness.timed_out:
                return self._record_invalid(
                    contract, patch, plan, worktree, plan.baseline_sha, correctness
                )
            schedule = interleaved_trial_schedule(
                (patch.candidate_id,), plan.repetitions, seed=plan.seed
            )
            observations: list[TrialObservation] = []
            baseline: list[float] = []
            candidate: list[float] = []
            for slot in schedule:
                current = contract.benchmark_workdir if slot.arm == "baseline" else mapped_workdir
                result = self._measure(current, timeout_seconds=plan.timeout_seconds)
                metric = _require_metric(result, slot.arm)
                observation = TrialObservation(
                    slot.repetition,
                    slot.arm,
                    metric,
                    result.exit_code,
                    result.timed_out,
                    result.duration_seconds,
                    sha256_text(f"{result.stdout}\n{result.stderr}"),
                )
                observations.append(observation)
                (baseline if slot.arm == "baseline" else candidate).append(metric)
            interval = bootstrap_relative_effect_ci(
                baseline,
                candidate,
                plan.objective,
                seed=plan.seed,
                bootstrap_samples=plan.bootstrap_samples,
            )
            classification = classify_candidate(True, interval)
            return ExperimentRecord(
                experiment_id=f"experiment-{uuid4().hex}",
                candidate_id=patch.candidate_id,
                status="succeeded",
                correctness_valid=True,
                classification=classification,
                objective=plan.objective.value,
                source_sha256=sha256(plan.baseline_sha.encode("utf-8")).hexdigest(),
                patch_sha256=patch.sha256,
                environment_sha256=plan.environment.sha256,
                topology_evidence_sha256=plan.topology_probe.evidence_sha256,
                trial_order=tuple(
                    (slot.repetition, slot.arm, slot.candidate_id) for slot in schedule
                ),
                observations=tuple(observations),
                confidence_interval=interval,
                worktree_path=str(worktree),
                caveats=("Local measurements are estimates, not official Yukon scores.",),
                measurement_verified=True,
            )
        except Exception:
            # No deletion/removal occurs; the deterministic private path is the quarantine location.
            raise

    def _record_invalid(
        self,
        contract: WorkspaceContract,
        patch: CandidatePatch,
        plan: ExperimentPlan,
        worktree: Path,
        source_hash: str,
        result: CommandResult,
    ) -> ExperimentRecord:
        observation = TrialObservation(
            0,
            "correctness",
            None,
            result.exit_code,
            result.timed_out,
            result.duration_seconds,
            sha256_text(f"{result.stdout}\n{result.stderr}"),
        )
        return ExperimentRecord(
            experiment_id=f"experiment-{uuid4().hex}",
            candidate_id=patch.candidate_id,
            status="invalid",
            correctness_valid=False,
            classification=CandidateClassification.INVALID,
            objective=plan.objective.value,
            source_sha256=sha256(source_hash.encode("utf-8")).hexdigest(),
            patch_sha256=patch.sha256,
            environment_sha256=plan.environment.sha256,
            topology_evidence_sha256=plan.topology_probe.evidence_sha256,
            trial_order=((0, "correctness", patch.candidate_id),),
            observations=(observation,),
            confidence_interval=None,
            worktree_path=str(worktree),
            caveats=("Correctness or Yukon gate failed; performance ranking was skipped.",),
            measurement_verified=True,
        )

    def _validate_plan(
        self, contract: WorkspaceContract, patch: CandidatePatch, plan: ExperimentPlan
    ) -> None:
        if not patch.candidate_id or plan.repetitions < 1 or plan.timeout_seconds <= 0:
            raise ExperimentError("candidate ID, repetitions, and timeout must be valid")
        if canonical(
            plan.topology_probe.checkout
        ) != contract.repository_root or plan.topology_probe.observed_command != ("yukon", "run"):
            raise ExperimentError(
                "measurement topology probe does not bind this Yukon checkout and command"
            )
        try:
            plan.topology_probe.require_isolated_run()
        except WorkspaceContractError as error:
            raise ExperimentError(
                "measurement topology is not verified for detached Yukon runs"
            ) from error
        if canonical(plan.canonical_workdir) != contract.benchmark_workdir:
            raise ExperimentError("plan workdir does not match the locked Yukon workdir")
        if self._editable_hash(contract) != plan.expected_editable_hash:
            raise ExperimentError("canonical editable-path fingerprint drifted")
        try:
            validated = validate_unified_diff(
                _workdir_relative_diff(patch.unified_diff, contract),
                workdir=contract.benchmark_workdir,
                editable_roots=contract.editable_paths,
            )
        except PatchValidationError as error:
            raise ExperimentError("candidate patch failed strong validation") from error
        changed = tuple(
            str(path.relative_to(contract.benchmark_workdir)) for path in validated.paths
        )
        if tuple(sorted(changed)) != tuple(sorted(patch.expected_paths)):
            raise ExperimentError("candidate expected paths do not match the validated diff")

    def _assert_canonical_checkout(self, contract: WorkspaceContract, plan: ExperimentPlan) -> None:
        if self._git_output(contract.repository_root, ("status", "--porcelain")).strip():
            raise ExperimentError("canonical checkout is dirty")
        spec = CommandSpec(
            "git",
            ("git", "diff", "--quiet", plan.baseline_sha, "HEAD"),
            str(contract.repository_root),
            30,
            ("git",),
            (("git", "diff", "--quiet"),),
        )
        result = self._commands.run(spec)
        if result.timed_out or result.exit_code != 0:
            raise ExperimentError("canonical checkout SHA drifted")

    def _apply_validated_patch(
        self, contract: WorkspaceContract, worktree: Path, workdir: Path, patch: CandidatePatch
    ) -> None:
        patch_path = self._immutable_patch_path(patch)
        self._git(worktree, ("apply", "--check", str(patch_path)))
        self._git(worktree, ("apply", str(patch_path)))
        changed = self._git_output(worktree, ("diff", "--name-only", "--no-renames")).splitlines()
        relative_prefix = workdir.relative_to(worktree)
        normalized: list[str] = []
        for raw in changed:
            candidate = PurePosixPath(raw)
            try:
                relative = candidate.relative_to(PurePosixPath(relative_prefix.as_posix()))
            except ValueError as error:
                raise ExperimentError(
                    "candidate modified a file outside nested Yukon workdir"
                ) from error
            normalized.append(relative.as_posix())
        if tuple(sorted(normalized)) != tuple(sorted(patch.expected_paths)):
            raise ExperimentError("applied worktree diff differs from validated patch paths")
        for path in normalized:
            assert_editable_path(
                workdir,
                tuple(
                    workdir / root.relative_to(contract.benchmark_workdir)
                    for root in contract.editable_paths
                ),
                path,
            )

    def _git(self, cwd: Path, args: tuple[str, ...]) -> CommandResult:
        argv = ("git", *args)
        allowed_prefixes = _git_allowed_prefixes(argv)
        result = self._commands.run(
            CommandSpec(
                "git",
                argv,
                str(canonical(cwd)),
                30,
                ("git",),
                allowed_prefixes,
            )
        )
        if result.exit_code != 0 or result.timed_out:
            raise ExperimentError("safe Git operation failed")
        return result

    def _git_output(self, cwd: Path, args: tuple[str, ...]) -> str:
        return self._git(cwd, args).stdout

    def _immutable_patch_path(self, patch: CandidatePatch) -> Path:
        directory = self._runtime_root / "patches"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / f"{patch.sha256}.patch"
        if target.exists():
            if target.read_text(encoding="utf-8") != patch.unified_diff:
                raise ExperimentError("immutable patch hash collision")
            return target
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(patch.unified_diff)
        except FileExistsError:
            if target.read_text(encoding="utf-8") != patch.unified_diff:
                raise ExperimentError("immutable patch hash collision") from None
        return target

    @staticmethod
    def _map_nested_workdir(contract: WorkspaceContract, worktree: Path) -> Path:
        try:
            relative = contract.benchmark_workdir.relative_to(contract.repository_root)
        except ValueError as error:
            raise ExperimentError(
                "locked Yukon workdir is not contained by repository root"
            ) from error
        mapped = canonical(worktree / relative)
        if mapped != worktree and worktree not in mapped.parents:
            raise ExperimentError("mapped workdir escapes detached worktree")
        return mapped

    @staticmethod
    def _editable_hash(contract: WorkspaceContract) -> str:
        entries: list[tuple[str, str]] = []
        for root in contract.editable_paths:
            if root.is_file():
                entries.append(
                    (
                        str(root.relative_to(contract.benchmark_workdir)),
                        sha256(root.read_bytes()).hexdigest(),
                    )
                )
            elif root.is_dir():
                for file in sorted(
                    item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts
                ):
                    entries.append(
                        (
                            str(file.relative_to(contract.benchmark_workdir)),
                            sha256(file.read_bytes()).hexdigest(),
                        )
                    )
            else:
                raise ExperimentError("locked editable path no longer exists")
        return sha256(
            "\n".join(f"{path}\0{digest}" for path, digest in entries).encode("utf-8")
        ).hexdigest()


def _safe_identifier(value: str) -> str:
    if not value or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in value
    ):
        raise ExperimentError("candidate IDs must be stable filename-safe identifiers")
    return value


def _workdir_relative_diff(diff: str, contract: WorkspaceContract) -> str:
    """Translate repository-relative unified-diff headers to the Yukon workdir.

    The strong candidate validator is workdir-scoped; only its recognized Git
    and file header paths are translated. Any malformed/mismatched headers
    remain and are rejected by that validator.
    """
    prefix = contract.benchmark_workdir.relative_to(contract.repository_root).as_posix()
    if prefix == ".":
        return diff
    prefix_with_slash = f"{prefix}/"
    translated: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git a/"):
            match = re.fullmatch(r"diff --git a/(.+) b/(.+)(\n?)", line)
            if (
                match
                and match.group(1).startswith(prefix_with_slash)
                and match.group(2).startswith(prefix_with_slash)
            ):
                translated.append(
                    f"diff --git a/{match.group(1)[len(prefix_with_slash) :]} "
                    f"b/{match.group(2)[len(prefix_with_slash) :]}{match.group(3)}"
                )
                continue
        elif line.startswith(("--- a/", "+++ b/")):
            header, path = line[:6], line[6:]
            if path.rstrip("\n").startswith(prefix_with_slash):
                translated.append(header + path[len(prefix_with_slash) :])
                continue
        translated.append(line)
    return "".join(translated)


def _git_allowed_prefixes(argv: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Return the exact fixed argv shape allowed for harness Git operations."""
    if argv[:3] == ("git", "status", "--porcelain") and len(argv) == 3:
        return (("git", "status", "--porcelain"),)
    if argv[:3] == ("git", "rev-parse", "HEAD") and len(argv) == 3:
        return (("git", "rev-parse", "HEAD"),)
    if argv[:4] == ("git", "diff", "--name-only", "--no-renames") and len(argv) == 4:
        return (("git", "diff", "--name-only", "--no-renames"),)
    if argv[:3] == ("git", "apply", "--check") and len(argv) == 4:
        return (("git", "apply", "--check"),)
    if argv[:2] == ("git", "apply") and len(argv) == 3:
        return (("git", "apply"),)
    if argv[:4] == ("git", "worktree", "add", "--detach") and len(argv) == 6:
        return (("git", "worktree", "add", "--detach"),)
    raise ExperimentError("Git command is not an approved harness operation")


def _require_metric(result: CommandResult, arm: str) -> float:
    if result.timed_out or result.exit_code != 0:
        raise ExperimentError(f"{arm} trial failed; no measurement is discarded")
    # The adapter-facing executor encodes a parsed metric in a deliberately narrow result line.
    for line in result.stdout.splitlines():
        if line.startswith("ODYSSEUS_METRIC="):
            try:
                value = float(line.partition("=")[2])
            except ValueError as error:
                raise ExperimentError("measurement executor emitted a malformed metric") from error
            if value <= 0:
                raise ExperimentError("measurement metric must be positive")
            return value
    raise ExperimentError("measurement executor did not provide a metric")
