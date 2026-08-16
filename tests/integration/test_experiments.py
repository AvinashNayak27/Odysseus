from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from odysseus.commands import CommandRunner
from odysseus.experiments.environment import fingerprint_environment
from odysseus.experiments.runner import (
    CandidatePatch,
    ExperimentError,
    ExperimentPlan,
    ExperimentRunner,
    _git_allowed_prefixes,
)
from odysseus.experiments.statistics import CandidateClassification, Objective
from odysseus.models import CommandResult
from odysseus.yukon.parser import CloneReport
from odysseus.yukon.workspace import TopologyProbe, WorkspaceContract


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def fixture_repository(tmp_path: Path) -> tuple[Path, WorkspaceContract, str]:
    repository = tmp_path / "repository"
    workdir = repository / "challenge"
    (workdir / "src").mkdir(parents=True)
    (workdir / "src" / "value.txt").write_text("100\n", encoding="utf-8")
    git(tmp_path, "init", str(repository))
    git(repository, "config", "user.email", "fixture@example.invalid")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "baseline")
    baseline = git(repository, "rev-parse", "HEAD").strip()
    contract = WorkspaceContract.from_clone_report(
        CloneReport(str(repository), str(workdir), ("src",), False), runtime_root=tmp_path / "state"
    )
    return repository, contract, baseline


def executor(workdir: Path, *, timeout_seconds: float) -> CommandResult:
    value = float((workdir / "src" / "value.txt").read_text(encoding="utf-8"))
    return CommandResult(
        ("yukon", "run"), str(workdir), 0, False, f"ODYSSEUS_METRIC={value}\n", "", 0.01, False
    )


def test_runner_maps_nested_yukon_workdir_and_preserves_canonical_checkout(tmp_path: Path) -> None:
    repository, contract, baseline = fixture_repository(tmp_path)
    patch = CandidatePatch(
        "faster",
        """diff --git a/challenge/src/value.txt b/challenge/src/value.txt
index 4e1fd64..8c8c1b7 100644
--- a/challenge/src/value.txt
+++ b/challenge/src/value.txt
@@ -1 +1 @@
-100
+90
""",
        ("src/value.txt",),
    )
    probe = TopologyProbe.observed(
        checkout=repository, exit_code=0, evidence_sha256="a" * 64, detached_worktree=True
    )
    runner = ExperimentRunner(CommandRunner(), tmp_path / "state", executor)
    plan = ExperimentPlan(
        Objective.MINIMIZE,
        baseline,
        contract.benchmark_workdir,
        3,
        41,
        1,
        fingerprint_environment({"fixture": "local"}),
        probe,
        runner._editable_hash(contract),
        bootstrap_samples=100,
    )
    record = runner.run(contract, patch, plan)
    assert record.correctness_valid
    assert record.classification is CandidateClassification.IMPROVED
    assert Path(record.worktree_path).is_dir()
    assert (contract.benchmark_workdir / "src" / "value.txt").read_text(encoding="utf-8") == "100\n"
    assert (Path(record.worktree_path) / "challenge" / "src" / "value.txt").read_text(
        encoding="utf-8"
    ) == "90\n"
    assert len(record.observations) == 6


def test_runner_fails_closed_without_detached_yukon_probe(tmp_path: Path) -> None:
    repository, contract, baseline = fixture_repository(tmp_path)
    runner = ExperimentRunner(CommandRunner(), tmp_path / "state", executor)
    patch = CandidatePatch(
        "x",
        """diff --git a/challenge/src/value.txt b/challenge/src/value.txt
--- a/challenge/src/value.txt
+++ b/challenge/src/value.txt
@@ -1 +1 @@
-100
+99
""",
        ("src/value.txt",),
    )
    plan = ExperimentPlan(
        Objective.MINIMIZE,
        baseline,
        contract.benchmark_workdir,
        1,
        1,
        1,
        fingerprint_environment({"fixture": "local"}),
        TopologyProbe.unverified(checkout=repository, evidence_sha256="b" * 64),
        runner._editable_hash(contract),
        bootstrap_samples=100,
    )
    with pytest.raises(ExperimentError, match="topology"):
        runner.run(contract, patch, plan)


def test_runner_rejects_forbidden_diff_before_creating_a_worktree(tmp_path: Path) -> None:
    repository, contract, baseline = fixture_repository(tmp_path)
    runner = ExperimentRunner(CommandRunner(), tmp_path / "state", executor)
    patch = CandidatePatch(
        "forbidden",
        """diff --git a/challenge/src/value.txt b/challenge/src/value.txt
new file mode 100644
--- a/challenge/src/value.txt
+++ b/challenge/src/value.txt
@@ -1 +1 @@
-100
+90
""",
        ("src/value.txt",),
    )
    probe = TopologyProbe.observed(
        checkout=repository, exit_code=0, evidence_sha256="a" * 64, detached_worktree=True
    )
    plan = ExperimentPlan(
        Objective.MINIMIZE,
        baseline,
        contract.benchmark_workdir,
        1,
        1,
        1,
        fingerprint_environment({"fixture": "local"}),
        probe,
        runner._editable_hash(contract),
        bootstrap_samples=100,
    )
    with pytest.raises(ExperimentError, match="strong validation"):
        runner.run(contract, patch, plan)
    assert not (tmp_path / "state" / "worktrees").exists()


@pytest.mark.parametrize(
    "argv",
    (
        ("git", "status", "--porcelain"),
        ("git", "rev-parse", "HEAD"),
        ("git", "diff", "--name-only", "--no-renames"),
        ("git", "apply", "--check", "/state/patches/candidate.patch"),
        ("git", "apply", "/state/patches/candidate.patch"),
        ("git", "worktree", "add", "--detach", "/state/worktree", "deadbeef"),
    ),
)
def test_git_allowlist_accepts_only_harness_command_shapes(argv: tuple[str, ...]) -> None:
    assert _git_allowed_prefixes(argv)


@pytest.mark.parametrize(
    "argv",
    (
        ("git", "reset", "--hard"),
        ("git", "clean", "-fd"),
        ("git", "status", "--porcelain", "--ignored"),
        ("git", "apply", "--index", "/state/patches/candidate.patch"),
        ("git", "worktree", "remove", "/state/worktree"),
    ),
)
def test_git_allowlist_rejects_non_harness_commands(argv: tuple[str, ...]) -> None:
    with pytest.raises(ExperimentError, match="not an approved"):
        _git_allowed_prefixes(argv)
