"""Structured candidate-provider protocol with external executable or human import modes."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from odysseus.candidates.prompts import CandidateRequest
from odysseus.commands import CommandRunner
from odysseus.models import CommandSpec
from odysseus.redaction import redact_private


class CandidateProviderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CandidateHypothesis:
    candidate_id: str
    mechanism: str
    hotspot: str
    expected_metric_effect: str
    evidence_ids: tuple[str, ...]
    candidate_files: tuple[str, ...]
    correctness_risk: str
    falsification_test: str
    unified_diff: str
    dependency: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateResponse:
    protocol_version: int
    provider: str
    model: str
    effort: str
    hypotheses: tuple[CandidateHypothesis, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class CandidateProvider(Protocol):
    def generate(self, request: CandidateRequest) -> CandidateResponse: ...


class HumanImportedProvider:
    """Validate JSON a human imported from an external provider; never executes it."""

    def __init__(self, response_path: Path, *, max_response_bytes: int) -> None:
        self._path = response_path
        self._max_response_bytes = max_response_bytes

    def generate(self, request: CandidateRequest) -> CandidateResponse:
        del request
        try:
            raw = self._path.read_bytes()
        except OSError as error:
            raise CandidateProviderError(
                f"cannot read human-imported candidate JSON: {error}"
            ) from error
        return parse_response(raw, max_candidates=None, max_response_bytes=self._max_response_bytes)


class ExternalExecutableProvider:
    """Invoke a configured provider only through the harness command policy.

    The executable receives the validated request on standard input and must return
    one CandidateResponse JSON object on standard output. Provider credentials can
    only be inherited through the explicit environment allowlist.
    """

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_request_bytes: int,
        max_response_bytes: int,
        max_candidates: int,
        environment_allowlist: tuple[str, ...] | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        if not argv or any(not item or "\x00" in item for item in argv):
            raise CandidateProviderError("provider argv must be a non-empty argument array")
        if min(timeout_seconds, max_request_bytes, max_response_bytes, max_candidates) <= 0:
            raise CandidateProviderError("provider limits must be positive")
        if environment_allowlist is not None and any(
            not name.isidentifier() or name.upper() != name for name in environment_allowlist
        ):
            raise CandidateProviderError("provider environment allowlist contains an invalid name")
        self._argv = argv
        self._cwd = cwd.resolve(strict=False)
        self._timeout = timeout_seconds
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._max_candidates = max_candidates
        self._environment_allowlist = environment_allowlist
        self._runner = runner or CommandRunner()

    def generate(self, request: CandidateRequest) -> CandidateResponse:
        if not self._cwd.is_dir():
            raise CandidateProviderError("provider cwd must exist")
        payload = json.dumps(
            request.as_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if not payload or len(payload) > self._max_request_bytes:
            raise CandidateProviderError("candidate request exceeds the configured byte limit")
        executable = self._argv[0]
        result = self._runner.run(
            CommandSpec(
                executable=executable,
                argv=self._argv,
                cwd=str(self._cwd),
                timeout_seconds=self._timeout,
                allowed_executables=(executable,),
                allowed_argv_prefixes=(self._argv,),
                max_output_bytes=self._max_response_bytes,
                inherit_environment=self._environment_allowlist is not None,
                environment_allowlist=self._environment_allowlist,
            ),
            stdin=payload,
        )
        if result.timed_out:
            raise CandidateProviderError("candidate provider timed out")
        if result.output_truncated:
            raise CandidateProviderError(
                "candidate provider output exceeds the configured byte limit"
            )
        if "\ufffd" in result.stdout:
            raise CandidateProviderError("candidate provider response must be UTF-8 JSON")
        if result.exit_code != 0:
            detail = redact_private(result.stderr).strip()
            suffix = f": {detail}" if detail else ""
            raise CandidateProviderError(
                f"candidate provider failed with exit code {result.exit_code}{suffix}"
            )
        return parse_response(
            result.stdout.encode("utf-8"),
            max_candidates=self._max_candidates,
            max_response_bytes=self._max_response_bytes,
        )


def parse_response(
    raw: bytes, *, max_candidates: int | None, max_response_bytes: int
) -> CandidateResponse:
    if not raw or len(raw) > max_response_bytes:
        raise CandidateProviderError(
            "candidate response is empty or exceeds the configured byte limit"
        )
    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateProviderError("candidate response must be UTF-8 JSON") from error
    if not isinstance(payload, dict) or set(payload) != {
        "protocol_version",
        "provider",
        "model",
        "effort",
        "hypotheses",
    }:
        raise CandidateProviderError("candidate response has unknown or missing top-level fields")
    if payload["protocol_version"] != 1:
        raise CandidateProviderError("unsupported candidate response protocol version")
    for field in ("provider", "model", "effort"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise CandidateProviderError(f"candidate response requires exact non-empty {field}")
    if "\x00" in payload["model"]:
        raise CandidateProviderError("candidate response model must not contain NUL bytes")
    hypotheses_raw = payload["hypotheses"]
    if not isinstance(hypotheses_raw, list) or not hypotheses_raw:
        raise CandidateProviderError("candidate response requires one or more hypotheses")
    if max_candidates is not None and len(hypotheses_raw) > max_candidates:
        raise CandidateProviderError("candidate response exceeds configured candidate limit")
    hypotheses = tuple(_parse_hypothesis(value) for value in hypotheses_raw)
    ids = [hypothesis.candidate_id for hypothesis in hypotheses]
    if len(set(ids)) != len(ids):
        raise CandidateProviderError("candidate IDs must be unique")
    return CandidateResponse(
        1, payload["provider"], payload["model"], payload["effort"], hypotheses
    )


def _parse_hypothesis(value: object) -> CandidateHypothesis:
    required = {
        "candidate_id",
        "mechanism",
        "hotspot",
        "expected_metric_effect",
        "evidence_ids",
        "candidate_files",
        "correctness_risk",
        "falsification_test",
        "unified_diff",
    }
    allowed = required | {"dependency"}
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise CandidateProviderError("candidate hypothesis has unknown or missing fields")
    strings = (
        "candidate_id",
        "mechanism",
        "hotspot",
        "expected_metric_effect",
        "correctness_risk",
        "falsification_test",
        "unified_diff",
    )
    if any(not isinstance(value[field], str) or not value[field].strip() for field in strings):
        raise CandidateProviderError("candidate hypothesis string fields must be non-empty")
    for field in ("evidence_ids", "candidate_files"):
        if (
            not isinstance(value[field], list)
            or not value[field]
            or any(not isinstance(item, str) or not item for item in value[field])
        ):
            raise CandidateProviderError(
                f"candidate hypothesis {field} must be a non-empty string list"
            )
    dependency = value.get("dependency")
    if dependency is not None and (not isinstance(dependency, str) or not dependency.strip()):
        raise CandidateProviderError(
            "candidate dependency must be a non-empty string when supplied"
        )
    return CandidateHypothesis(
        value["candidate_id"],
        value["mechanism"],
        value["hotspot"],
        value["expected_metric_effect"],
        tuple(value["evidence_ids"]),
        tuple(value["candidate_files"]),
        value["correctness_risk"],
        value["falsification_test"],
        value["unified_diff"],
        dependency,
    )
