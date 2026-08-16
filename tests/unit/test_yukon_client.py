from pathlib import Path

from odysseus.approvals import approval_hash
from odysseus.models import ApprovalPlan, CommandResult, CommandSpec
from odysseus.yukon.client import YUKON_ENV_ALLOWLIST, YukonClient
from odysseus.yukon.workspace import RestartGate

BENCHMARK_ID = "1b5f7092-c2df-4ee8-94e7-1c03836d6b4e"


class FakeRunner:
    def __init__(self) -> None:
        self.specs: list[CommandSpec] = []
        self.allowlists: list[tuple[str, ...]] = []

    def run(self, spec: CommandSpec) -> CommandResult:
        self.specs.append(spec)
        allowlist = spec.environment_allowlist
        assert allowlist is not None
        self.allowlists.append(allowlist)
        return CommandResult(
            spec.argv, spec.cwd, 0, False, "yukon v2026.08.15-1\n", "", 0.01, False
        )


def test_client_has_restricted_commands_and_preserves_yukon_context_names(tmp_path: Path) -> None:
    runner = FakeRunner()
    client = YukonClient(runner)
    assert client.version(cwd=tmp_path).value == "2026.08.15-1"
    client.submissions(BENCHMARK_ID, workdir=tmp_path)
    sync_spec = client._spec(("sync", "--harness-only"), cwd=tmp_path, timeout_seconds=10)
    plan = ApprovalPlan(
        "yukon-sync-harness-only",
        "safe refresh",
        BENCHMARK_ID,
        "source",
        "target",
        sync_spec,
    )
    client.sync_harness_only(
        workdir=tmp_path, timeout_seconds=10, approval=plan, approval_hash=approval_hash(plan)
    )
    assert runner.specs[-2].argv == ("yukon", "submissions", BENCHMARK_ID, "--all")
    assert runner.specs[-1].argv == ("yukon", "sync", "--harness-only")
    assert "YUKON_API_TOKEN" in YUKON_ENV_ALLOWLIST
    assert "YUKON_TRACE" in YUKON_ENV_ALLOWLIST
    assert "YUKON_SESSION_ID" in YUKON_ENV_ALLOWLIST
    assert not any("submit" in name or "notes" in name for name in dir(client))


def test_setup_and_run_are_hard_stopped_until_restart_contract_is_satisfied(tmp_path: Path) -> None:
    from odysseus.yukon.workspace import RestartGate, WorkspaceContractError

    runner = FakeRunner()
    client = YukonClient(runner)
    restart = RestartGate(True, tmp_path / "root")
    try:
        client.setup(
            workdir=tmp_path,
            timeout_seconds=10,
            restart_gate=restart,
            process_root=tmp_path,
        )
    except WorkspaceContractError:
        pass
    else:
        raise AssertionError("restart gate allowed Yukon setup")
    assert not runner.specs

    client.run(
        workdir=tmp_path,
        timeout_seconds=10,
        restart_gate=restart,
        process_root=tmp_path / "root",
    )
    assert runner.specs[-1].argv == ("yukon", "run")


class SequencedRunner:
    def __init__(self, results: list[CommandResult]) -> None:
        self._results = results
        self.specs: list[CommandSpec] = []

    def run(self, spec: CommandSpec) -> CommandResult:
        self.specs.append(spec)
        result = self._results.pop(0)
        return CommandResult(
            spec.argv,
            spec.cwd,
            result.exit_code,
            result.timed_out,
            result.stdout,
            result.stderr,
            result.duration_seconds,
            result.output_truncated,
        )


def result(*, exit_code: int = 0, timed_out: bool = False, stderr: str = "") -> CommandResult:
    return CommandResult((), "", exit_code, timed_out, "", stderr, 0.01, False)


def test_read_commands_retry_only_timed_out_or_transient_failures(tmp_path: Path) -> None:
    runner = SequencedRunner(
        [
            result(timed_out=True),
            result(),
            result(exit_code=75, stderr="temporary failure"),
            result(),
            result(exit_code=1, stderr="authentication failed"),
        ]
    )
    client = YukonClient(runner, read_retries=1, max_output_bytes=2048)

    assert client.benchmark_list(cwd=tmp_path).exit_code == 0
    assert client.benchmark_show(BENCHMARK_ID, cwd=tmp_path).exit_code == 0
    assert client.submissions(BENCHMARK_ID, workdir=tmp_path).exit_code == 1
    assert [spec.argv[1:3] for spec in runner.specs] == [
        ("benchmark", "list"),
        ("benchmark", "list"),
        ("benchmark", "show"),
        ("benchmark", "show"),
        ("submissions", BENCHMARK_ID),
    ]
    assert all(spec.max_output_bytes == 2048 for spec in runner.specs)


def test_mutating_or_experiment_commands_never_retry(tmp_path: Path) -> None:
    runner = SequencedRunner(
        [result(timed_out=True), result(timed_out=True), result(timed_out=True)]
    )
    client = YukonClient(runner, read_retries=5)
    restart = RestartGate(False, tmp_path)

    assert client.clone(
        BENCHMARK_ID, tmp_path / "clone", cwd=tmp_path, timeout_seconds=10
    ).timed_out
    assert client.setup(
        workdir=tmp_path,
        timeout_seconds=10,
        restart_gate=restart,
        process_root=tmp_path,
    ).timed_out
    assert client.run(
        workdir=tmp_path,
        timeout_seconds=10,
        restart_gate=restart,
        process_root=tmp_path,
    ).timed_out
    assert len(runner.specs) == 3
    assert [spec.argv[1] for spec in runner.specs] == ["clone", "setup", "run"]


def test_cli_factory_binds_workspace_output_limit_and_yukon_read_retries(tmp_path: Path) -> None:
    from odysseus.cli import _client

    client = _client(
        {
            "workspace": {"max_output_bytes": 4096},
            "yukon": {"binary": "yukon", "read_retries": 2},
        }
    )
    spec = client._spec(("benchmark", "list"), cwd=tmp_path)
    assert spec.max_output_bytes == 4096
    assert client._read_retries == 2
