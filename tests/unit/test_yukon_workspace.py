from pathlib import Path

import pytest

from odysseus.yukon.parser import CloneReport
from odysseus.yukon.workspace import (
    RestartGate,
    TopologyProbe,
    TopologyStatus,
    WorkspaceContract,
    WorkspaceContractError,
)


def test_workspace_requires_external_state_and_constrains_edits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    work = repo / "nested"
    (work / "src").mkdir(parents=True)
    contract = WorkspaceContract.from_clone_report(
        CloneReport(str(repo), str(work), ("src",), False), runtime_root=tmp_path / "state"
    )
    assert contract.assert_editable("src/lib.rs") == work / "src/lib.rs"
    with pytest.raises(Exception, match="outside"):
        contract.assert_editable("Cargo.toml")
    with pytest.raises(Exception, match="outside"):
        WorkspaceContract.from_clone_report(
            CloneReport(str(repo), str(work), ("src",), False), runtime_root=repo / "state"
        )


def test_restart_and_unverified_isolated_topology_hard_stop(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    restart = RestartGate(True, root)
    with pytest.raises(WorkspaceContractError, match="restart"):
        restart.hard_stop()
    probe = TopologyProbe.unverified(checkout=root, evidence_sha256="a" * 64)
    assert probe.status is TopologyStatus.DECISION_REQUIRED
    with pytest.raises(WorkspaceContractError, match="topology decision"):
        probe.require_isolated_run()
    canonical_only = TopologyProbe.observed(checkout=root, exit_code=0, evidence_sha256="b" * 64)
    with pytest.raises(WorkspaceContractError, match="detached-worktree"):
        canonical_only.require_isolated_run()
    TopologyProbe.observed(
        checkout=root,
        exit_code=0,
        evidence_sha256="c" * 64,
        detached_worktree=True,
    ).require_isolated_run()
    assert (
        TopologyProbe.observed(checkout=root, exit_code=2, evidence_sha256="d" * 64).status
        is TopologyStatus.UNSUPPORTED
    )
