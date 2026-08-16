from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from pytest import CaptureFixture

from odysseus.cli import main
from odysseus.state import StateStore

VERSION = "yukon v2026.08.15-1\n"
BENCHMARK_ID = "123e4567-e89b-12d3-a456-426614174000"
LIST = """Benchmark  Status  Category  Goal  Best  Closes  ID
Flock  open  systems  minimize  -  soon  123e4567-e89b-12d3-a456-426614174000
"""
SHOW = """Category       systems
Goal           lower
Current best   -
Closes         soon
Source URL     https://example.invalid/flock.git @ abcdef
Source branch  main
Score path     score.json
Setup          ./setup.sh
Benchmark      ./benchmark.sh
fixture description
"""


def _config(root: Path, binary: str = "yukon") -> Path:
    state, clones = root / "state", root / "clones"
    path = root / "odysseus.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "workspace": {
                    "state_root": str(state),
                    "clone_root": str(clones),
                    "artifact_retention_days": 1,
                    "max_output_bytes": 1024 * 1024,
                },
                "yukon": {
                    "binary": binary,
                    "api_url_env": "YUKON_API_URL",
                    "read_retries": 0,
                    "setup_timeout_seconds": 30,
                    "run_timeout_seconds": 30,
                },
                "selection": {"active_window_days": 1, "supported_objectives": ["minimize"]},
                "research": {"allowed_domains": ["go.dev"], "max_sources_per_query": 1},
                "experiments": {"pilot_samples": 3, "min_repetitions": 5, "max_repetitions": 5},
                "candidate_provider": {
                    "mode": "human-import",
                    "timeout_seconds": 1,
                    "max_request_bytes": 1024,
                    "max_candidates": 1,
                    "max_response_bytes": 1024,
                },
                "reporting": {
                    "private_path_aliases": {str(root): "$TEST"},
                    "note_min_bytes": 5120,
                    "note_max_bytes": 10000,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _fake_yukon(root: Path) -> Path:
    binary = root / "yukon"
    binary.write_text(
        "#!/bin/sh\n"
        'case "$1:$2" in\n'
        "version:*) cat <<'OUT'\n" + VERSION + "OUT\n;;\n"
        "benchmark:list) cat <<'OUT'\n" + LIST + "OUT\n;;\n"
        "benchmark:show) cat <<'OUT'\n"
        + SHOW
        + "OUT\n;;\n"
        + ("submissions:" + BENCHMARK_ID + ") printf '<script>x</script> frontier\\n' ;;\n")
        + "run:*) value=$(cat src/value.txt) ; printf 'ODYSSEUS_METRIC=%s\\n' \"$value\" ;;\n"
        + "*) exit 64 ;;\n"
        + "esac\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary


def _single_artifact(root: Path, name: str) -> Path:
    matches = list((root / "state" / "runs").glob(f"*/artifacts/{name}"))
    assert len(matches) == 1
    return matches[0]


def test_offline_discovery_prepare_restart_and_frontier(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    binary = _fake_yukon(tmp_path)
    config = _config(tmp_path, binary.name)
    # Yukon accepts only bare executable names, so fixture lookup uses PATH.
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp_path}:{old_path}"
    try:
        assert main(["--config", str(config), "discover", "--cwd", str(tmp_path)]) == 0
        lock = _single_artifact(tmp_path, "benchmark-lock.json")
        assert json.loads(lock.read_text(encoding="utf-8"))["selected_id"] == BENCHMARK_ID

        assert (
            main(["--config", str(config), "prepare", "--lock", str(lock), "--cwd", str(tmp_path)])
            == 0
        )
        approval = json.loads(capsys.readouterr().out.splitlines()[-1])["plan_hash"]
        clone_report = tmp_path / "clone.txt"
        repo = tmp_path / "checkout"
        workdir = repo / "nested"
        (workdir / "src").mkdir(parents=True)
        clone_report.write_text(
            (
                f"Repository Root: {repo}\nWorkdir: {workdir}\n"
                "Editable Paths: src\nRestart agent now\n"
            ),
            encoding="utf-8",
        )
        assert (
            main(
                [
                    "--config",
                    str(config),
                    "prepare",
                    "--lock",
                    str(lock),
                    "--cwd",
                    str(tmp_path),
                    "--approve-selection",
                    approval,
                    "--clone-report",
                    str(clone_report),
                ]
            )
            == 3
        )
        assert "restart_required" in capsys.readouterr().out
        assert (
            main(
                [
                    "--config",
                    str(config),
                    "inspect-frontier",
                    "--benchmark",
                    BENCHMARK_ID,
                    "--workdir",
                    str(workdir),
                ]
            )
            == 0
        )
        quarantined = list((tmp_path / "state" / "runs").glob(f"*/quarantine/{BENCHMARK_ID}/*.txt"))
        assert len(quarantined) == 1
        assert "<script>" not in quarantined[0].read_text(encoding="utf-8")
    finally:
        os.environ["PATH"] = old_path


def test_offline_pure_stages_and_precise_prerequisites(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    run_output = tmp_path / "run.out"
    run_output.write_text("latency=12.5ms\n", encoding="utf-8")
    assert (
        main(
            [
                "--config",
                str(config),
                "baseline",
                "--workdir",
                str(workdir),
                "--run-output",
                str(run_output),
                "--metric-name",
                "latency",
                "--metric-pattern",
                r"latency=(?P<value>\d+\.\d+)ms",
                "--direction",
                "lower_is_better",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--config",
                str(config),
                "research",
                "--objective",
                "minimize",
                "--adapter",
                "generic",
                "--editable-path",
                "src/lib.py",
            ]
        )
        == 0
    )
    samples = tmp_path / "samples.json"
    samples.write_text(
        json.dumps({"baseline": [10, 10, 10], "candidate": [9, 9, 9]}), encoding="utf-8"
    )
    digest = "a" * 64
    assert (
        main(
            [
                "--config",
                str(config),
                "experiment",
                "import-samples",
                "--samples",
                str(samples),
                "--candidate",
                "candidate-a",
                "--objective",
                "minimize",
                "--source-sha",
                digest,
                "--patch-sha",
                digest,
                "--environment-sha",
                digest,
                "--topology-sha",
                digest,
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--config",
                str(config),
                "candidates",
                "--request",
                str(tmp_path / "missing.json"),
                "--response",
                str(tmp_path / "missing-response.json"),
                "--workdir",
                str(workdir),
                "--editable-path",
                "src",
            ]
        )
        == 2
    )
    assert "cannot load candidate request JSON" in capsys.readouterr().err


def test_reference_candidate_mode_invokes_python_script_without_network(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config_data = json.loads(config.read_text(encoding="utf-8"))
    config_data["candidate_provider"] = {
        "mode": "reference",
        "base_url": "https://provider.example",
        "public_model": "GPT 5.6 Sol",
        "api_model": "gpt-5.6-sol-2026-08-01",
        "effort": "high",
        "secret_env": "TEST_REFERENCE_KEY",
        "timeout_seconds": 2,
        "max_request_bytes": 4096,
        "max_candidates": 1,
        "max_response_bytes": 4096,
    }
    config.write_text(json.dumps(config_data), encoding="utf-8")
    monkeypatch.delenv("TEST_REFERENCE_KEY", raising=False)
    workdir = tmp_path / "work"
    (workdir / "src").mkdir(parents=True)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "objective": "latency",
                "objective_direction": "lower_is_better",
                "hotspot_facts": ["encode"],
                "editable_paths": ["src"],
                "evidence": [{"source_id": "e1"}],
                "untrusted_context": [],
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--config",
                str(config),
                "candidates",
                "--request",
                str(request),
                "--workdir",
                str(workdir),
                "--editable-path",
                "src",
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "reference provider credential is unavailable" in error
    assert "Permission denied" not in error


def test_offline_experiment_run_produces_reportable_sealed_artifact(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    binary = _fake_yukon(tmp_path)
    config = _config(tmp_path, binary.name)
    repository = tmp_path / "repository"
    workdir = repository / "challenge"
    (workdir / "src").mkdir(parents=True)
    (workdir / "src" / "value.txt").write_text("100\n", encoding="utf-8")
    subprocess.run(("git", "init", str(repository)), check=True, capture_output=True)
    subprocess.run(("git", "config", "user.email", "fixture@example.invalid"), cwd=repository, check=True)
    subprocess.run(("git", "config", "user.name", "Fixture"), cwd=repository, check=True)
    subprocess.run(("git", "add", "."), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-m", "baseline"), cwd=repository, check=True, capture_output=True)
    baseline = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    state = StateStore(tmp_path / "state", (tmp_path / "clones",))
    workspace_run = state.create_run()
    workspace = state.write_artifact(
        workspace_run,
        "workspace.json",
        json.dumps(
            {
                "schemaVersion": 1,
                "benchmark_id": BENCHMARK_ID,
                "repository_root": str(repository),
                "workdir": str(workdir),
                "editable_paths": ["src"],
                "source_sha256": "a" * 64,
                "recorded_at": "2026-08-16T00:00:00Z",
            }
        ).encode(),
    )
    state.seal_run(workspace_run)
    candidates_run = state.create_run()
    candidates = state.write_artifact(
        candidates_run,
        "candidates.json",
        json.dumps(
            {
                "provider": "fixture-provider",
                "model": "fixture-model",
                "effort": "high",
                "candidates": [
                    {
                        "candidate": {
                            "candidate_id": "faster",
                            "unified_diff": "diff --git a/challenge/src/value.txt b/challenge/src/value.txt\n"
                            "--- a/challenge/src/value.txt\n+++ b/challenge/src/value.txt\n"
                            "@@ -1 +1 @@\n-100\n+90\n",
                        },
                        "paths": [str(workdir / "src" / "value.txt")],
                        "additions": 1,
                        "deletions": 1,
                    }
                ],
            }
        ).encode(),
    )
    state.seal_run(candidates_run)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp_path}:{old_path}"
    try:
        assert (
            main(
                [
                    "--config",
                    str(config),
                    "experiment",
                    "probe-topology",
                    "--workspace",
                    str(workspace["path"]),
                    "--baseline-sha",
                    baseline,
                ]
            )
            == 0
        )
        topology = json.loads(capsys.readouterr().out)["topology_probe"]
        assert (
            main(
                [
                    "--config",
                    str(config),
                    "experiment",
                    "run",
                    "--workspace",
                    str(workspace["path"]),
                    "--candidate",
                    str(candidates["path"]),
                    "--candidate-id",
                    "faster",
                    "--objective",
                    "minimize",
                    "--baseline-sha",
                    baseline,
                    "--topology-probe",
                    str(topology["path"]),
                    "--repetitions",
                    "5",
                ]
            )
            == 0
        )
        experiment = json.loads(capsys.readouterr().out)["experiment"]
        review = tmp_path / "review.json"
        review.write_text(
            json.dumps(
                {
                    "run_id": "review-input",
                    "benchmark_id": BENCHMARK_ID,
                    "base_source_ref": baseline,
                    "repository_root": str(repository),
                    "workdir": str(workdir),
                    "editable_paths": ["src"],
                    "provider": "fixture-provider",
                    "model": "fixture-model",
                    "effort": "high",
                    "harness": "Odysseus",
                    "hypothesis": "The candidate reuses the locked editable value representation while preserving output semantics.",
                    "evidence_ids": [f"evidence-{number}" for number in range(1, 55)],
                    "changed_files": ["src/value.txt"],
                    "provenance": {
                        "faster": "candidate-record",
                        **{f"evidence-{number}": f"source-{number}" for number in range(1, 55)},
                    },
                    "caveats": [
                        "Local measurements remain estimates; reviewers must inspect the preserved worktree and artifacts."
                    ] * 20,
                    "next_steps": [
                        "Review the sealed evidence, environment fingerprint, trial order, and candidate patch."
                    ] * 20,
                }
            ),
            encoding="utf-8",
        )
        assert (
            main(
                [
                    "--config",
                    str(config),
                    "report",
                    "--input",
                    str(review),
                    "--experiment-artifact",
                    str(experiment["path"]),
                ]
            )
            == 0
        )
        report = json.loads(capsys.readouterr().out)
        assert Path(str(report["public_note"]["path"])).is_file()
    finally:
        os.environ["PATH"] = old_path


def test_verify_run_detects_mutated_artifact(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    samples = tmp_path / "samples.json"
    samples.write_text(
        json.dumps({"baseline": [10, 10, 10], "candidate": [9, 9, 9]}), encoding="utf-8"
    )
    digest = "a" * 64
    assert (
        main(
            [
                "--config",
                str(config),
                "experiment",
                "import-samples",
                "--samples",
                str(samples),
                "--candidate",
                "candidate-a",
                "--objective",
                "minimize",
                "--source-sha",
                digest,
                "--patch-sha",
                digest,
                "--environment-sha",
                digest,
                "--topology-sha",
                digest,
            ]
        )
        == 0
    )
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert main(["--config", str(config), "verify-run", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
    artifact = next(
        (tmp_path / "state" / "runs" / run_id / "artifacts").glob("imported-samples.json")
    )
    artifact.write_text("{}", encoding="utf-8")
    assert main(["--config", str(config), "verify-run", run_id]) == 3
    assert json.loads(capsys.readouterr().out)["verified"] is False
