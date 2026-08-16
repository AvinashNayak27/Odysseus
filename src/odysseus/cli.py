"""Safe, explicit, stage-oriented command-line interface."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from odysseus import __version__
from odysseus.adapters import detect_adapters
from odysseus.adapters.base import MetricDirection, MetricSpec, parse_metric
from odysseus.approvals import approval_hash, verify_approval
from odysseus.candidates.prompts import build_request
from odysseus.candidates.provider import (
    CandidateProvider,
    CandidateProviderError,
    ExternalExecutableProvider,
    HumanImportedProvider,
)
from odysseus.candidates.validation import validate_unified_diff
from odysseus.commands import CommandRunner
from odysseus.config import ConfigError, load_config, validate_record
from odysseus.experiments.environment import local_environment_fingerprint
from odysseus.experiments.ledger import ExperimentRecord
from odysseus.experiments.runner import (
    CandidatePatch,
    ExperimentPlan,
    ExperimentRunner,
    MeasurementExecutor,
)
from odysseus.experiments.statistics import (
    CandidateClassification,
    Objective,
    bootstrap_relative_effect_ci,
)
from odysseus.models import ApprovalPlan, CommandResult, CommandSpec, StageStatus
from odysseus.paths import canonical
from odysseus.reports.renderer import ReportRenderer, ReviewInput
from odysseus.research.pipeline import derive_questions
from odysseus.state import StateError, StateStore, sha256_bytes, utc_now
from odysseus.yukon.client import YukonClient
from odysseus.yukon.discovery import bind_source_snapshot, build_shortlist
from odysseus.yukon.doctor import inspect_yukon
from odysseus.yukon.frontier import quarantine_submissions
from odysseus.yukon.parser import (
    CloneReport,
    parse_benchmark_list,
    parse_benchmark_show,
    parse_clone_report,
)
from odysseus.yukon.workspace import RestartGate, TopologyProbe, WorkspaceContract


class PrerequisiteError(ValueError):
    """A stage needs a concrete artifact or human approval before it can run."""


_JSON = dict[str, Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odysseus", description="Local review-first optimization harness"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config", type=Path, required=True, help="private strict JSON configuration"
    )
    commands = parser.add_subparsers(dest="command", required=True, title="workflow stages")

    doctor = commands.add_parser("doctor", help="inspect installed Yukon and local state safely")
    doctor.add_argument("--cwd", type=Path, default=Path.cwd(), help="Yukon invocation directory")

    discover = commands.add_parser(
        "discover", help="read version/list/show and write an immutable shortlist lock"
    )
    discover.add_argument("--cwd", type=Path, default=Path.cwd(), help="Yukon invocation directory")

    prepare = commands.add_parser(
        "prepare", help="render or verify a hash-bound clone preparation plan"
    )
    prepare.add_argument("--lock", type=Path, required=True, help="discovery lock JSON")
    prepare.add_argument(
        "--cwd", type=Path, default=Path.cwd(), help="Yukon clone invocation directory"
    )
    prepare.add_argument(
        "--approve-selection",
        metavar="PLAN_HASH",
        help="exact approval hash printed by this command",
    )
    prepare.add_argument(
        "--clone-report", type=Path, help="offline Yukon clone output to parse and verify"
    )
    prepare.add_argument(
        "--execute-clone", action="store_true", help="execute only the approved Yukon clone command"
    )

    frontier = commands.add_parser("inspect-frontier", help="quarantine read-only submission text")
    frontier.add_argument("--benchmark", required=True, help="locked benchmark identifier")
    frontier.add_argument("--workdir", type=Path, required=True, help="verified benchmark workdir")

    baseline = commands.add_parser(
        "baseline", help="parse a captured canonical run output without executing a benchmark"
    )
    baseline.add_argument("--workdir", type=Path, required=True)
    baseline.add_argument(
        "--run-output", type=Path, required=True, help="scrubbed Yukon run output artifact"
    )
    baseline.add_argument("--metric-name", required=True)
    baseline.add_argument("--metric-pattern", required=True, help="regex with named 'value' group")
    baseline.add_argument(
        "--direction", choices=[item.value for item in MetricDirection], required=True
    )

    research = commands.add_parser(
        "research", help="derive deterministic bounded research questions from explicit facts"
    )
    research.add_argument("--objective", required=True)
    research.add_argument("--adapter", required=True)
    research.add_argument("--hotspot", action="append", default=[])
    research.add_argument("--editable-path", action="append", default=[])
    research.add_argument("--frontier-claim", action="append", default=[])

    candidates = commands.add_parser(
        "candidates", help="obtain or validate structured candidate response and diffs"
    )
    candidates.add_argument(
        "--request", type=Path, required=True, help="trusted candidate-request JSON"
    )
    candidates.add_argument(
        "--response", type=Path, help="human-imported provider response JSON (human-import mode)"
    )
    candidates.add_argument("--workdir", type=Path, required=True)
    candidates.add_argument("--editable-path", action="append", required=True)

    experiment = commands.add_parser(
        "experiment", help="local experiment operations; imported data is never verified"
    )
    experiment_commands = experiment.add_subparsers(dest="experiment_command", required=True)
    imported = experiment_commands.add_parser(
        "import-samples",
        help="store unverified human-imported samples; cannot produce recommendations",
    )
    imported.add_argument("--samples", type=Path, required=True)
    imported.add_argument("--candidate", required=True)
    imported.add_argument("--objective", choices=[item.value for item in Objective], required=True)
    imported.add_argument("--source-sha", required=True)
    imported.add_argument("--patch-sha", required=True)
    imported.add_argument("--environment-sha", required=True)
    imported.add_argument("--topology-sha", required=True)
    imported.add_argument("--seed", type=int, default=20260816)
    probe = experiment_commands.add_parser(
        "probe-topology", help="verify detached Yukon run support and seal topology evidence"
    )
    probe.add_argument("--workspace", type=Path, required=True, help="sealed workspace.json artifact")
    probe.add_argument("--baseline-sha", required=True, help="locked canonical Git baseline SHA")
    run = experiment_commands.add_parser(
        "run", help="execute a locked candidate in a detached worktree and seal experiment evidence"
    )
    run.add_argument("--workspace", type=Path, required=True, help="sealed workspace.json artifact")
    run.add_argument("--candidate", type=Path, required=True, help="sealed candidates.json artifact")
    run.add_argument("--candidate-id", required=True, help="candidate ID from --candidate")
    run.add_argument("--objective", choices=[item.value for item in Objective], required=True)
    run.add_argument("--baseline-sha", required=True, help="locked canonical Git baseline SHA")
    run.add_argument(
        "--topology-probe",
        type=Path,
        required=True,
        help="sealed detached topology JSON produced by experiment probe",
    )
    run.add_argument("--repetitions", type=int, required=True)
    run.add_argument("--seed", type=int, default=20260816)

    report = commands.add_parser(
        "report", help="render review artifacts from a complete explicit review input"
    )
    report.add_argument(
        "--input", type=Path, required=True, help="review metadata JSON without effects"
    )
    report.add_argument(
        "--experiment-artifact",
        type=Path,
        required=True,
        help="sealed immutable verified experiment artifact",
    )

    status = commands.add_parser("status", help="show private run identifiers")
    status.add_argument("--state-root", type=Path, help="override configured external state root")
    verify = commands.add_parser("verify-run", help="verify immutable artifact hashes for one run")
    verify.add_argument("run_id")
    verify.add_argument("--state-root", type=Path, help="override configured external state root")

    yukon = commands.add_parser("yukon", help="narrow Yukon maintenance operations")
    yukon_commands = yukon.add_subparsers(dest="yukon_command", required=True)
    sync = yukon_commands.add_parser("sync-harness", help="approved Yukon harness-only refresh")
    sync.add_argument("--workspace", type=Path, required=True, help="sealed workspace artifact")
    sync.add_argument("--approve-plan", required=True, metavar="PLAN_HASH")
    return parser


def _load_json(path: Path, label: str) -> _JSON:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PrerequisiteError(f"cannot load {label} JSON: {error}") from error
    if not isinstance(payload, dict):
        raise PrerequisiteError(f"{label} must be a JSON object")
    return cast(_JSON, payload)


def _config_state(config: _JSON) -> StateStore:
    workspace = cast(_JSON, config["workspace"])
    return StateStore(
        Path(cast(str, workspace["state_root"])), (Path(cast(str, workspace["clone_root"])),)
    )


def _config_yukon(config: _JSON) -> _JSON:
    return cast(_JSON, config["yukon"])


def _client(config: _JSON) -> YukonClient:
    """Bind strict Yukon client knobs to the non-secret configured limits."""
    yukon = _config_yukon(config)
    workspace = cast(_JSON, config["workspace"])
    return YukonClient(
        CommandRunner(),
        binary=cast(str, yukon["binary"]),
        read_retries=cast(int, yukon["read_retries"]),
        max_output_bytes=cast(int, workspace["max_output_bytes"]),
    )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot serialize {type(value)!r}")


def _record(
    store: StateStore, stage: str, status: StageStatus, payload: _JSON
) -> tuple[str, _JSON]:
    run_id = store.create_run()
    event = store.append_event(run_id, stage, status, payload)
    return run_id, event.as_dict()


def _artifact(store: StateStore, run_id: str, name: str, value: object) -> _JSON:
    encoded = (_json(value) + "\n").encode("utf-8")
    return cast(_JSON, store.write_artifact(run_id, name, encoded))


def _doctor(config: _JSON, args: argparse.Namespace) -> int:
    store = _config_state(config)
    facts = inspect_yukon(_client(config), cwd=canonical(args.cwd), state_root=store.root)
    payload = asdict(facts)
    run_id, _ = _record(store, "doctor", StageStatus.SUCCEEDED, payload)
    print(_json({"run_id": run_id, "doctor": payload}))
    return 0


def _discover(config: _JSON, args: argparse.Namespace) -> int:
    store, client, cwd = _config_state(config), _client(config), canonical(args.cwd)
    version = client.version(cwd=cwd)
    listed = client.benchmark_list(cwd=cwd)
    if listed.exit_code != 0 or listed.timed_out:
        raise PrerequisiteError("read-only Yukon benchmark list failed")
    summaries = parse_benchmark_list(listed.stdout, cli_version=version)
    details = []
    snapshots = [listed.stdout]
    for summary in summaries:
        result = client.benchmark_show(summary.benchmark_id, cwd=cwd)
        if result.exit_code != 0 or result.timed_out:
            raise PrerequisiteError(
                f"read-only Yukon benchmark show failed for {summary.benchmark_id}"
            )
        snapshots.append(result.stdout)
        details.append(
            parse_benchmark_show(
                result.stdout, cli_version=version, benchmark_id=summary.benchmark_id
            )
        )
    shortlist = bind_source_snapshot(build_shortlist(summaries, details), snapshots)
    eligible = next((item for item in shortlist.candidates if item.excluded_reason is None), None)
    if eligible is None:
        raise PrerequisiteError("discovery found no eligible open benchmark")
    lock = {
        "schemaVersion": 1,
        "lock_id": shortlist.sha256,
        "selected_id": eligible.benchmark_id,
        "retrieved_at": shortlist.retrieved_at,
        "source_sha256": shortlist.source_sha256,
        "candidates": [asdict(item) for item in shortlist.candidates],
    }
    validate_record(lock, "benchmark-lock")
    run_id, _ = _record(
        store, "discover", StageStatus.SUCCEEDED, {"selected_id": eligible.benchmark_id}
    )
    artifact = _artifact(store, run_id, "benchmark-lock.json", lock)
    print(_json({"run_id": run_id, "selected_id": eligible.benchmark_id, "lock": artifact}))
    return 0


def _prepare(config: _JSON, args: argparse.Namespace) -> int:
    store, client = _config_state(config), _client(config)
    cwd = canonical(args.cwd)
    # Version parsing is performed before clone output is consumed so a Yukon
    # restart request is the final decision in this process.
    version = client.version(cwd=cwd)
    lock = _load_json(args.lock, "discovery lock")
    validate_record(lock, "benchmark-lock")
    benchmark_id = cast(str, lock["selected_id"])
    clone_root = Path(cast(str, cast(_JSON, config["workspace"])["clone_root"]))
    target = canonical(clone_root / benchmark_id)
    spec = client.clone_spec(
        benchmark_id,
        target,
        cwd=cwd,
        timeout_seconds=cast(int, _config_yukon(config)["setup_timeout_seconds"]),
    )
    plan = ApprovalPlan(
        "yukon-clone-and-prepare",
        (
            f"Clone selected benchmark {benchmark_id} into {target}; parse Yukon-reported "
            "root/workdir/editablePaths; stop on restart."
        ),
        benchmark_id,
        str(args.lock.resolve(strict=True)),
        str(target),
        spec,
    )
    digest = approval_hash(plan)
    if args.approve_selection is None:
        run_id, _ = _record(
            store,
            "prepare",
            StageStatus.APPROVAL_REQUIRED,
            {"benchmark_id": benchmark_id, "plan_hash": digest},
        )
        print(
            _json(
                {
                    "run_id": run_id,
                    "status": "approval_required",
                    "plan_hash": digest,
                    "plan": plan.displayed_plan,
                }
            )
        )
        return 0
    verify_approval(plan, args.approve_selection)
    clone_text: str | None = None
    if args.execute_clone:
        clone_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists():
            raise PrerequisiteError(
                "approved clone target already exists; immutable preparation will not overwrite it"
            )
        result = client.clone(
            benchmark_id, target, cwd=canonical(args.cwd), timeout_seconds=spec.timeout_seconds
        )
        if result.exit_code != 0 or result.timed_out:
            raise PrerequisiteError("approved Yukon clone failed; scrubbed result was not promoted")
        clone_text = result.stdout
    elif args.clone_report is not None:
        clone_text = args.clone_report.read_text(encoding="utf-8")
    else:
        raise PrerequisiteError(
            "approval verified; provide --clone-report for offline verification "
            "or --execute-clone to run the approved clone"
        )
    report = parse_clone_report(clone_text, cli_version=version)
    contract = WorkspaceContract.from_clone_report(report, runtime_root=store.root)
    restart = RestartGate.from_clone_report(report)
    status = StageStatus.RESTART_REQUIRED if restart.restart_required else StageStatus.SUCCEEDED
    run_id, _ = _record(
        store,
        "prepare",
        status,
        {
            "benchmark_id": benchmark_id,
            "repository_root": str(contract.repository_root),
            "workdir": str(contract.benchmark_workdir),
        },
    )
    artifact = _artifact(
        store,
        run_id,
        "workspace.json",
        {
            "schemaVersion": 1,
            "benchmark_id": benchmark_id,
            "repository_root": str(contract.repository_root),
            "workdir": str(contract.benchmark_workdir),
            "editable_paths": [
                str(item.relative_to(contract.benchmark_workdir))
                for item in contract.editable_paths
            ],
            "source_sha256": lock["source_sha256"],
            "recorded_at": lock["retrieved_at"],
        },
    )
    if restart.restart_required:
        print(
            _json(
                {
                    "run_id": run_id,
                    "status": "restart_required",
                    "restart_at": str(contract.repository_root),
                    "workspace": artifact,
                }
            )
        )
        return 3
    print(_json({"run_id": run_id, "status": "succeeded", "workspace": artifact}))
    return 0


def _frontier(config: _JSON, args: argparse.Namespace) -> int:
    store, client = _config_state(config), _client(config)
    workdir = canonical(args.workdir)
    result = client.submissions(args.benchmark, workdir=workdir)
    if result.exit_code != 0 or result.timed_out:
        raise PrerequisiteError("read-only Yukon submissions lookup failed")
    run_id, _ = _record(
        store, "inspect-frontier", StageStatus.SUCCEEDED, {"benchmark_id": args.benchmark}
    )
    item = quarantine_submissions(
        benchmark_id=args.benchmark,
        text=result.stdout,
        quarantine_root=store.run_path(run_id) / "quarantine",
    )
    print(
        _json(
            {
                "run_id": run_id,
                "benchmark_id": item.benchmark_id,
                "sha256": item.content_sha256,
                "bytes": item.byte_count,
            }
        )
    )
    return 0


def _baseline(config: _JSON, args: argparse.Namespace) -> int:
    store, workdir = _config_state(config), canonical(args.workdir)
    output = args.run_output.read_text(encoding="utf-8")
    metric = parse_metric(
        output, MetricSpec(args.metric_name, MetricDirection(args.direction), args.metric_pattern)
    )
    detections = detect_adapters(workdir)
    run_id, _ = _record(
        store, "baseline", StageStatus.SUCCEEDED, {"metric": metric.name, "value": metric.value}
    )
    artifact = _artifact(
        store,
        run_id,
        "baseline.json",
        {
            "metric": asdict(metric),
            "adapters": [asdict(item) for item in detections],
            "output_sha256": sha256(output.encode("utf-8")).hexdigest(),
        },
    )
    print(_json({"run_id": run_id, "baseline": artifact, "metric": asdict(metric)}))
    return 0


def _research(config: _JSON, args: argparse.Namespace) -> int:
    store = _config_state(config)
    if not args.editable_path:
        raise PrerequisiteError(
            "research requires at least one explicit --editable-path from the locked workspace"
        )
    questions = derive_questions(
        objective=args.objective,
        adapter=args.adapter,
        hotspots=args.hotspot,
        editable_files=args.editable_path,
        frontier_claims=args.frontier_claim,
    )
    run_id, _ = _record(
        store, "research", StageStatus.SUCCEEDED, {"question_count": len(questions)}
    )
    artifact = _artifact(
        store,
        run_id,
        "research-questions.json",
        {"questions": [asdict(item) for item in questions]},
    )
    print(_json({"run_id": run_id, "research": artifact, "question_count": len(questions)}))
    return 0


def _candidates(config: _JSON, args: argparse.Namespace) -> int:
    store = _config_state(config)
    request = _load_json(args.request, "candidate request")
    required = {
        "objective",
        "objective_direction",
        "hotspot_facts",
        "editable_paths",
        "evidence",
        "untrusted_context",
    }
    if set(request) - (required | {"protocol_version", "instructions"}) or required - set(request):
        raise PrerequisiteError("candidate request has unknown or missing trusted protocol fields")
    built = build_request(
        objective=cast(str, request["objective"]),
        objective_direction=cast(str, request["objective_direction"]),
        hotspot_facts=cast(list[str], request["hotspot_facts"]),
        editable_paths=cast(list[str], request["editable_paths"]),
        evidence=cast(list[dict[str, object]], request["evidence"]),
        untrusted_context=cast(list[str], request["untrusted_context"]),
    )
    provider: CandidateProvider
    provider_config = cast(_JSON, config["candidate_provider"])
    mode = cast(str, provider_config["mode"])
    max_response_bytes = cast(int, provider_config["max_response_bytes"])
    if mode == "human-import":
        if args.response is None:
            raise PrerequisiteError("human-import candidate mode requires --response")
        provider = HumanImportedProvider(args.response, max_response_bytes=max_response_bytes)
    elif mode == "external-cli":
        provider = ExternalExecutableProvider(
            tuple(cast(list[str], provider_config["argv"])),
            cwd=store.root,
            timeout_seconds=cast(int, provider_config["timeout_seconds"]),
            max_request_bytes=cast(int, provider_config["max_request_bytes"]),
            max_response_bytes=max_response_bytes,
            max_candidates=cast(int, provider_config["max_candidates"]),
        )
    elif mode == "reference":
        provider = ExternalExecutableProvider(
            (
                sys.executable,
                str(Path(__file__).resolve().parent / "candidates" / "reference.py"),
                "--base-url",
                cast(str, provider_config["base_url"]),
                "--public-model",
                cast(str, provider_config["public_model"]),
                "--api-model",
                cast(str, provider_config["api_model"]),
                "--effort",
                cast(str, provider_config["effort"]),
                "--secret-env",
                cast(str, provider_config["secret_env"]),
                "--timeout-seconds",
                str(provider_config["timeout_seconds"]),
                "--max-request-bytes",
                str(provider_config["max_request_bytes"]),
                "--max-response-bytes",
                str(max_response_bytes),
                "--max-candidates",
                str(provider_config["max_candidates"]),
            ),
            cwd=store.root,
            timeout_seconds=cast(int, provider_config["timeout_seconds"]),
            max_request_bytes=cast(int, provider_config["max_request_bytes"]),
            max_response_bytes=max_response_bytes,
            max_candidates=cast(int, provider_config["max_candidates"]),
            environment_allowlist=(cast(str, provider_config["secret_env"]), "PATH", "HOME"),
        )
    else:
        raise PrerequisiteError("candidate provider mode is not supported")
    try:
        response = provider.generate(built)
    except CandidateProviderError as error:
        raise PrerequisiteError(f"candidate provider failed: {error}") from error
    workdir = canonical(args.workdir)
    editable = tuple(canonical(workdir / path) for path in args.editable_path)
    validated = []
    for hypothesis in response.hypotheses:
        if not set(hypothesis.evidence_ids).issubset(
            {str(item.get("source_id", "")) for item in built.evidence}
        ):
            raise PrerequisiteError(
                f"candidate {hypothesis.candidate_id} references evidence absent from request"
            )
        patch = validate_unified_diff(
            hypothesis.unified_diff, workdir=workdir, editable_roots=editable
        )
        validated.append(
            {
                "candidate": asdict(hypothesis),
                "paths": [str(item) for item in patch.paths],
                "additions": patch.additions,
                "deletions": patch.deletions,
            }
        )
    run_id, _ = _record(
        store, "candidates", StageStatus.SUCCEEDED, {"candidate_count": len(validated)}
    )
    artifact = _artifact(
        store,
        run_id,
        "candidates.json",
        {
            "provider": response.provider,
            "model": response.model,
            "effort": response.effort,
            "candidates": validated,
        },
    )
    manifest = store.seal_run(run_id)
    print(
        _json(
            {
                "run_id": run_id,
                "candidates": artifact,
                "candidate_count": len(validated),
                "manifest": manifest,
            }
        )
    )
    return 0


def _sha(value: str, field: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise PrerequisiteError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _experiment_import_samples(config: _JSON, args: argparse.Namespace) -> int:
    """Keep human-imported values inspectable but explicitly unverified and unrankable."""
    store = _config_state(config)
    samples = _load_json(args.samples, "samples")
    baseline, candidate = samples.get("baseline"), samples.get("candidate")
    if not (isinstance(baseline, list) and isinstance(candidate, list) and baseline and candidate):
        raise PrerequisiteError("samples JSON requires non-empty baseline and candidate arrays")
    interval = bootstrap_relative_effect_ci(
        tuple(float(item) for item in baseline),
        tuple(float(item) for item in candidate),
        Objective(args.objective),
        seed=args.seed,
    )
    imported = {
        "schemaVersion": 1,
        "kind": "imported_unverified_samples",
        "candidate_id": args.candidate,
        "objective": args.objective,
        "source_sha256": _sha(args.source_sha, "source SHA"),
        "patch_sha256": _sha(args.patch_sha, "patch SHA"),
        "environment_sha256": _sha(args.environment_sha, "environment SHA"),
        "topology_evidence_sha256": _sha(args.topology_sha, "topology SHA"),
        "baseline": baseline,
        "candidate": candidate,
        "confidence_interval": asdict(interval),
        "measurement_verified": False,
        "recommendation_eligible": False,
        "caveat": "Imported samples are unverified and cannot be ranked or recommended.",
    }
    run_id, _ = _record(
        store,
        "experiment-import-samples",
        StageStatus.DEGRADED,
        {"candidate_id": args.candidate, "measurement_verified": False},
    )
    artifact = _artifact(store, run_id, "imported-samples.json", imported)
    manifest = store.seal_run(run_id)
    print(_json({"run_id": run_id, "imported_samples": artifact, "manifest": manifest}))
    return 0


def _sealed_artifact(store: StateStore, path: Path, name: str) -> _JSON:
    """Load one named artifact only after verifying its containing sealed run."""
    artifact = canonical(path)
    try:
        relative = artifact.relative_to(store.root / "runs")
        run_id = relative.parts[0]
        expected_name = Path(*relative.parts[2:])
    except (ValueError, IndexError) as error:
        raise PrerequisiteError(f"{name} artifact must be inside configured immutable state") from error
    if expected_name.as_posix() != name:
        raise PrerequisiteError(f"expected sealed {name} artifact")
    verified, mismatches = store.verify_run(run_id)
    if not verified:
        raise PrerequisiteError(f"artifact run manifest verification failed: {mismatches}")
    return _load_json(artifact, name)


def _workspace_contract_from_artifact(store: StateStore, path: Path) -> WorkspaceContract:
    workspace = _sealed_artifact(store, path, "workspace.json")
    validate_record(workspace, "workspace")
    try:
        return WorkspaceContract.from_clone_report(
            CloneReport(
                root=cast(str, workspace["repository_root"]),
                workdir=cast(str, workspace["workdir"]),
                editable_paths=tuple(cast(list[str], workspace["editable_paths"])),
                restart_requested=False,
            ),
            runtime_root=store.root,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PrerequisiteError(f"sealed workspace artifact is malformed: {error}") from error


def _topology_probe_from_artifact(
    store: StateStore, path: Path, contract: WorkspaceContract
) -> TopologyProbe:
    raw = _sealed_artifact(store, path, "topology-probe.json")
    required = {"checkout", "exit_code", "evidence_sha256", "detached_worktree"}
    if set(raw) != required:
        raise PrerequisiteError("topology probe has unknown or missing fields")
    if raw["checkout"] != str(contract.repository_root) or raw["exit_code"] != 0:
        raise PrerequisiteError("topology probe must record a successful locked-checkout Yukon run")
    if raw["detached_worktree"] is not True:
        raise PrerequisiteError("topology probe must verify detached-worktree execution")
    evidence = raw["evidence_sha256"]
    if not isinstance(evidence, str):
        raise PrerequisiteError("topology probe evidence hash is invalid")
    return TopologyProbe.observed(
        checkout=contract.repository_root,
        exit_code=0,
        evidence_sha256=_sha(evidence, "topology evidence SHA"),
        detached_worktree=True,
    )


def _candidate_patch_from_artifact(
    store: StateStore, path: Path, candidate_id: str, contract: WorkspaceContract
) -> CandidatePatch:
    raw = _sealed_artifact(store, path, "candidates.json")
    candidates = raw.get("candidates")
    if not isinstance(candidates, list):
        raise PrerequisiteError("sealed candidates artifact is malformed")
    matches = [item for item in candidates if isinstance(item, dict) and item.get("candidate", {}).get("candidate_id") == candidate_id]
    if len(matches) != 1:
        raise PrerequisiteError("candidate ID must identify exactly one sealed candidate")
    candidate = cast(_JSON, matches[0]["candidate"])
    paths = matches[0].get("paths")
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        raise PrerequisiteError("sealed candidate paths are malformed")
    try:
        expected_paths = tuple(
            str(canonical(Path(item)).relative_to(contract.benchmark_workdir)) for item in paths
        )
        return CandidatePatch(candidate_id, cast(str, candidate["unified_diff"]), expected_paths)
    except (KeyError, TypeError, ValueError) as error:
        raise PrerequisiteError(f"sealed candidate is malformed: {error}") from error


def _experiment_probe_topology(config: _JSON, args: argparse.Namespace) -> int:
    store = _config_state(config)
    contract = _workspace_contract_from_artifact(store, args.workspace)
    baseline_sha = _git_head(contract.repository_root)
    if baseline_sha != args.baseline_sha:
        raise PrerequisiteError("topology probe baseline SHA differs from the locked baseline")
    client = _client(config)
    timeout = cast(int, _config_yukon(config)["run_timeout_seconds"])
    worktree = store.root / "worktrees" / f"topology-{baseline_sha[:12]}"
    if worktree.exists():
        raise PrerequisiteError("harness topology worktree path already exists")
    runner = CommandRunner()
    runner.run(
        CommandSpec(
            "git",
            ("git", "worktree", "add", "--detach", str(worktree), baseline_sha),
            str(contract.repository_root),
            30,
            ("git",),
            (("git", "worktree", "add", "--detach"),),
        )
    )
    detached_workdir = worktree / contract.benchmark_workdir.relative_to(contract.repository_root)
    result = client.run(
        workdir=detached_workdir,
        timeout_seconds=timeout,
        restart_gate=RestartGate(False),
        process_root=detached_workdir,
    )
    if result.exit_code != 0 or result.timed_out:
        raise PrerequisiteError("detached Yukon topology probe failed")
    _metric_from_yukon_output(result.stdout)
    evidence = sha256(f"{result.stdout}\n{result.stderr}".encode()).hexdigest()
    run_id, _ = _record(
        store, "experiment-probe-topology", StageStatus.SUCCEEDED, {"measurement_verified": True}
    )
    artifact = _artifact(
        store,
        run_id,
        "topology-probe.json",
        {
            "checkout": str(contract.repository_root),
            "exit_code": result.exit_code,
            "evidence_sha256": evidence,
            "detached_worktree": True,
        },
    )
    manifest = store.seal_run(run_id)
    print(_json({"run_id": run_id, "topology_probe": artifact, "manifest": manifest}))
    return 0


def _git_head(repository_root: Path) -> str:
    result = CommandRunner().run(
        CommandSpec(
            "git",
            ("git", "rev-parse", "HEAD"),
            str(repository_root),
            30,
            ("git",),
            (("git", "rev-parse", "HEAD"),),
        )
    )
    if result.exit_code != 0 or result.timed_out:
        raise PrerequisiteError("cannot read canonical Git baseline SHA")
    return result.stdout.strip()


def _measurement_executor(client: YukonClient, timeout_seconds: float) -> MeasurementExecutor:
    def measure(workdir: Path, *, timeout_seconds: float) -> CommandResult:
        result = client.run(
            workdir=workdir,
            timeout_seconds=timeout_seconds,
            restart_gate=RestartGate(False),
            process_root=workdir,
        )
        metric = _metric_from_yukon_output(result.stdout)
        return replace(result, stdout=f"ODYSSEUS_METRIC={metric}\n")

    del timeout_seconds
    return measure


def _metric_from_yukon_output(output: str) -> float:
    match = re.search(r"(?m)^ODYSSEUS_METRIC=([0-9]+(?:\.[0-9]+)?)$", output)
    if match is None:
        raise PrerequisiteError("Yukon run output must provide exactly one ODYSSEUS_METRIC=<positive number>")
    value = float(match.group(1))
    if value <= 0:
        raise PrerequisiteError("Yukon metric must be positive")
    return value


def _experiment_run(config: _JSON, args: argparse.Namespace) -> int:
    store = _config_state(config)
    contract = _workspace_contract_from_artifact(store, args.workspace)
    patch = _candidate_patch_from_artifact(store, args.candidate, args.candidate_id, contract)
    probe = _topology_probe_from_artifact(store, args.topology_probe, contract)
    if args.repetitions < cast(int, cast(_JSON, config["experiments"])["min_repetitions"]):
        raise PrerequisiteError("experiment repetitions are below the configured verified minimum")
    if args.repetitions > cast(int, cast(_JSON, config["experiments"])["max_repetitions"]):
        raise PrerequisiteError("experiment repetitions exceed the configured verified maximum")
    client = _client(config)
    runner = ExperimentRunner(
        CommandRunner(),
        store.root,
        _measurement_executor(client, cast(int, _config_yukon(config)["run_timeout_seconds"])),
    )
    plan = ExperimentPlan(
        objective=Objective(args.objective),
        baseline_sha=args.baseline_sha,
        canonical_workdir=contract.benchmark_workdir,
        repetitions=args.repetitions,
        seed=args.seed,
        timeout_seconds=cast(int, _config_yukon(config)["run_timeout_seconds"]),
        environment=local_environment_fingerprint({"yukon_binary": cast(str, _config_yukon(config)["binary"])}),
        topology_probe=probe,
        expected_editable_hash=runner._editable_hash(contract),
    )
    record = runner.run(contract, patch, plan)
    if not record.correctness_valid or record.confidence_interval is None:
        raise PrerequisiteError("verified experiment did not produce correctness and confidence evidence")
    payload = {
        **asdict(record),
        "trial_order": [list(slot) for slot in record.trial_order],
        "observations": [asdict(observation) for observation in record.observations],
        "caveats": list(record.caveats),
        "schemaVersion": 1,
        "recorded_at": utc_now(),
    }
    try:
        validate_record(payload, "experiment")
    except ConfigError as error:
        raise PrerequisiteError(f"verified experiment record is invalid: {error}") from error
    run_id, _ = _record(
        store,
        "experiment-run",
        StageStatus.SUCCEEDED,
        {"candidate_id": record.candidate_id, "measurement_verified": True},
    )
    artifact = _artifact(
        store,
        run_id,
        "experiment.json",
        payload,
    )
    manifest = store.seal_run(run_id)
    print(_json({"run_id": run_id, "experiment": artifact, "manifest": manifest}))
    return 0


def _experiment(config: _JSON, args: argparse.Namespace) -> int:
    if args.experiment_command == "import-samples":
        return _experiment_import_samples(config, args)
    if args.experiment_command == "probe-topology":
        return _experiment_probe_topology(config, args)
    if args.experiment_command == "run":
        return _experiment_run(config, args)
    raise PrerequisiteError("unsupported experiment operation")


def _load_verified_experiment_artifact(store: StateStore, path: Path) -> ExperimentRecord:
    artifact = canonical(path)
    try:
        relative = artifact.relative_to(store.root / "runs")
        run_id = relative.parts[0]
        expected_name = Path(*relative.parts[2:])
    except (ValueError, IndexError) as error:
        raise PrerequisiteError(
            "experiment artifact must be inside configured immutable state"
        ) from error
    verified, mismatches = store.verify_run(run_id)
    if not verified:
        raise PrerequisiteError(f"experiment run manifest verification failed: {mismatches}")
    if not expected_name or expected_name.name != "experiment.json":
        raise PrerequisiteError(
            "report requires a verified experiment.json artifact, not imported samples"
        )
    raw = _load_json(artifact, "experiment artifact")
    if raw.get("measurement_verified") is not True:
        raise PrerequisiteError("report cannot recommend unverified or imported measurements")
    try:
        from odysseus.experiments.statistics import ConfidenceInterval

        interval = ConfidenceInterval(**cast(_JSON, raw["confidence_interval"]))
        return ExperimentRecord(
            experiment_id=cast(str, raw["experiment_id"]),
            candidate_id=cast(str, raw["candidate_id"]),
            status=cast(str, raw["status"]),
            correctness_valid=cast(bool, raw["correctness_valid"]),
            classification=CandidateClassification(cast(str, raw["classification"])),
            objective=cast(str, raw["objective"]),
            source_sha256=cast(str, raw["source_sha256"]),
            patch_sha256=cast(str, raw["patch_sha256"]),
            environment_sha256=cast(str, raw["environment_sha256"]),
            topology_evidence_sha256=cast(str, raw["topology_evidence_sha256"]),
            trial_order=tuple(),
            observations=tuple(),
            confidence_interval=interval,
            worktree_path=cast(str, raw["worktree_path"]),
            caveats=tuple(cast(list[str], raw["caveats"])),
            measurement_verified=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PrerequisiteError(f"verified experiment artifact is malformed: {error}") from error


def _report(config: _JSON, args: argparse.Namespace) -> int:
    store = _config_state(config)
    raw = _load_json(args.input, "review input")
    if "experiment" in raw or "confidence_interval" in raw:
        raise PrerequisiteError("report metadata must not claim effects; use --experiment-artifact")
    experiment = _load_verified_experiment_artifact(store, args.experiment_artifact)
    try:
        item = ReviewInput(experiment=experiment, **raw)
    except TypeError as error:
        raise PrerequisiteError(f"review metadata is incomplete or invalid: {error}") from error
    _validate_changed_files(item)
    reporting = cast(_JSON, config["reporting"])
    renderer = ReportRenderer(
        cast(dict[str, str], reporting["private_path_aliases"]),
        note_min_bytes=cast(int, reporting["note_min_bytes"]),
        note_max_bytes=cast(int, reporting["note_max_bytes"]),
    )
    bundle = renderer.render(item)
    run_id, _ = _record(
        store,
        "report",
        StageStatus.SUCCEEDED,
        {"benchmark_id": item.benchmark_id, "candidate_id": item.experiment.candidate_id},
    )
    private = store.write_artifact(run_id, "review.md", bundle.private_report.encode("utf-8"))
    public = store.write_artifact(run_id, "public-note.md", bundle.public_note.encode("utf-8"))
    metadata = _artifact(
        store, run_id, "publication-metadata.json", asdict(bundle.publication_metadata)
    )
    manifest = store.seal_run(run_id)
    print(
        _json(
            {
                "run_id": run_id,
                "private_report": private,
                "public_note": public,
                "metadata": metadata,
                "manifest": manifest,
            }
        )
    )
    return 0


def _validate_changed_files(item: ReviewInput) -> None:
    workdir = canonical(Path(item.workdir))
    editable_roots = tuple(canonical(workdir / entry) for entry in item.editable_paths)
    for changed in item.changed_files:
        if Path(changed).is_absolute():
            raise PrerequisiteError("reported changed files must be relative to the locked workdir")
        from odysseus.paths import assert_editable_path

        assert_editable_path(workdir, editable_roots, changed)


def _status(config: _JSON, args: argparse.Namespace) -> int:
    root = args.state_root or Path(cast(str, cast(_JSON, config["workspace"])["state_root"]))
    store = StateStore(root)
    runs = sorted((store.root / "runs").glob("run-*")) if (store.root / "runs").exists() else []
    print(_json({"run_count": len(runs), "runs": [item.name for item in runs]}))
    return 0


def _verify_run(config: _JSON, args: argparse.Namespace) -> int:
    root = args.state_root or Path(cast(str, cast(_JSON, config["workspace"])["state_root"]))
    store = StateStore(root)
    verified, mismatches = store.verify_run(args.run_id)
    print(_json({"run_id": args.run_id, "verified": verified, "mismatches": mismatches}))
    return 0 if verified else 3


def _editable_fingerprint(workdir: Path, editable_paths: tuple[str, ...]) -> str:
    """Hash every regular editable file and path; reject missing or escaping entries."""
    from odysseus.paths import assert_editable_path

    entries: list[tuple[str, str]] = []
    roots = tuple(canonical(workdir / path) for path in editable_paths)
    for raw, root in zip(editable_paths, roots, strict=True):
        assert_editable_path(workdir, roots, raw)
        if root.is_file():
            entries.append((raw, sha256_bytes(root.read_bytes())))
        elif root.is_dir():
            for file in sorted(item for item in root.rglob("*") if item.is_file()):
                relative = str(file.relative_to(workdir))
                assert_editable_path(workdir, roots, relative)
                entries.append((relative, sha256_bytes(file.read_bytes())))
        else:
            raise PrerequisiteError("locked editable path no longer exists")
    return sha256(
        "\n".join(f"{name}\0{digest}" for name, digest in entries).encode("utf-8")
    ).hexdigest()


def _sync(config: _JSON, args: argparse.Namespace) -> int:
    client, store = _client(config), _config_state(config)
    workspace = _load_json(args.workspace, "workspace artifact")
    validate_record(workspace, "workspace")
    benchmark_id = cast(str, workspace["benchmark_id"])
    workdir = canonical(Path(cast(str, workspace["workdir"])))
    editable_paths = tuple(cast(list[str], workspace["editable_paths"]))
    before = _editable_fingerprint(workdir, editable_paths)
    spec = client.sync_harness_only_spec(
        workdir=workdir, timeout_seconds=cast(int, _config_yukon(config)["run_timeout_seconds"])
    )
    plan = ApprovalPlan(
        "yukon-sync-harness-only",
        "Refresh Yukon harness-only metadata; verify locked editable-path bytes remain unchanged.",
        benchmark_id,
        str(canonical(args.workspace)),
        str(workdir),
        spec,
    )
    verify_approval(plan, args.approve_plan)
    result = client.sync_harness_only(
        workdir=workdir,
        timeout_seconds=spec.timeout_seconds,
        approval=plan,
        approval_hash=args.approve_plan,
    )
    after = _editable_fingerprint(workdir, editable_paths)
    if result.exit_code != 0 or result.timed_out:
        raise PrerequisiteError("approved Yukon harness-only sync failed")
    if before != after:
        run_id, _ = _record(
            store,
            "yukon-sync-harness",
            StageStatus.FAILED,
            {"benchmark_id": benchmark_id, "before_sha256": before, "after_sha256": after},
        )
        evidence = _artifact(
            store,
            run_id,
            "sync-editable-change.json",
            {
                "benchmark_id": benchmark_id,
                "before_sha256": before,
                "after_sha256": after,
                "setup_stale": True,
            },
        )
        store.seal_run(run_id)
        print(
            _json(
                {"run_id": run_id, "status": "failed", "quarantine": evidence, "setup_stale": True}
            )
        )
        return 3
    run_id, _ = _record(
        store,
        "yukon-sync-harness",
        StageStatus.SUCCEEDED,
        {"benchmark_id": benchmark_id, "editable_sha256": after, "setup_stale": True},
    )
    artifact = _artifact(
        store,
        run_id,
        "sync-harness.json",
        {"benchmark_id": benchmark_id, "editable_sha256": after, "setup_stale": True},
    )
    manifest = store.seal_run(run_id)
    print(
        _json(
            {
                "run_id": run_id,
                "status": "succeeded",
                "record": artifact,
                "manifest": manifest,
                "setup_stale": True,
            }
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        handlers = {
            "doctor": _doctor,
            "discover": _discover,
            "prepare": _prepare,
            "inspect-frontier": _frontier,
            "baseline": _baseline,
            "research": _research,
            "candidates": _candidates,
            "experiment": _experiment,
            "report": _report,
            "status": _status,
            "verify-run": _verify_run,
        }
        if args.command == "yukon":
            return _sync(config, args)
        return handlers[args.command](config, args)
    except (ConfigError, StateError, PrerequisiteError, ValueError, OSError) as error:
        print(f"odysseus: {error}", file=sys.stderr)
        return 2
    except KeyError as error:
        print(f"odysseus: unsupported command: {error}", file=sys.stderr)
        return 2
