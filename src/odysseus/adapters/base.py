"""Language-neutral contracts for safe, data-only benchmark analysis."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from odysseus.candidates.validation import ValidatedPatch

from odysseus.paths import canonical, require_within


class AdapterError(ValueError):
    """Raised when a workspace or metric cannot be analysed safely."""


class MetricDirection(StrEnum):
    LOWER_IS_BETTER = "lower_is_better"
    HIGHER_IS_BETTER = "higher_is_better"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """A locked, named metric format supplied by the benchmark contract."""

    name: str
    direction: MetricDirection
    pattern: str
    value_group: str = "value"
    unit_group: str = "unit"

    def __post_init__(self) -> None:
        if not self.name or not self.pattern:
            raise AdapterError("metric name and pattern are required")
        try:
            compiled = re.compile(self.pattern, re.IGNORECASE | re.MULTILINE)
        except re.error as error:
            raise AdapterError(f"metric pattern is invalid: {error}") from error
        if self.value_group not in compiled.groupindex:
            raise AdapterError("metric pattern must provide a named value group")


@dataclass(frozen=True, slots=True)
class ParsedMetric:
    name: str
    value: float
    unit: str | None
    direction: MetricDirection
    matched_text: str


@dataclass(frozen=True, slots=True)
class ToolchainCapture:
    adapter: str
    language_versions: tuple[tuple[str, str], ...]
    manifest_hashes: tuple[tuple[str, str], ...]
    target_architecture: str | None
    compiler_settings: tuple[tuple[str, str], ...]
    profiler_capabilities: tuple[tuple[str, bool], ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProfilePlan:
    """A reviewable, non-executing profiler request.

    Commands are generated only from fixed adapter templates plus the locked
    Yukon run contract.  Callers still need an explicit approval before running
    any plan.
    """

    adapter: str
    status: str
    executable: str | None
    argv: tuple[str, ...]
    cwd: Path
    artifact_path: Path | None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.status == "available"


@dataclass(frozen=True, slots=True)
class AdapterDetection:
    adapter: str
    confidence: str
    manifest_paths: tuple[Path, ...]


class LanguageAdapter(Protocol):
    name: str

    def detect(self, workdir: Path) -> AdapterDetection | None: ...

    def capture_toolchain(
        self,
        workdir: Path,
        *,
        tool_versions: Mapping[str, str],
        profiler_capabilities: Mapping[str, bool],
        target_architecture: str | None,
    ) -> ToolchainCapture: ...

    def baseline_plan(self, workdir: Path) -> ProfilePlan: ...

    def profile_plan(
        self,
        workdir: Path,
        *,
        artifact_root: Path,
        profiler_capabilities: Mapping[str, bool],
    ) -> ProfilePlan: ...

    def parse_metrics(self, output: str, metric: MetricSpec) -> ParsedMetric: ...

    def validate_patch(
        self, diff: str, *, workdir: Path, editable_roots: tuple[Path, ...]
    ) -> ValidatedPatch: ...


class AdapterBase:
    """Common static patch validation; adapters never apply provider diffs."""

    def validate_patch(
        self, diff: str, *, workdir: Path, editable_roots: tuple[Path, ...]
    ) -> ValidatedPatch:
        from odysseus.candidates.validation import validate_unified_diff

        return validate_unified_diff(diff, workdir=workdir, editable_roots=editable_roots)


def safe_workdir(workdir: Path) -> Path:
    resolved = canonical(workdir)
    if not resolved.is_dir():
        raise AdapterError("adapter workdir must exist")
    return resolved


def locked_files(workdir: Path, names: Sequence[str]) -> tuple[Path, ...]:
    root = safe_workdir(workdir)
    found: list[Path] = []
    for name in names:
        candidate = require_within(root / name, root)
        if candidate.is_file():
            found.append(candidate)
    return tuple(found)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_hashes(workdir: Path, files: Sequence[Path]) -> tuple[tuple[str, str], ...]:
    root = safe_workdir(workdir)
    return tuple(
        sorted(
            (str(require_within(path, root).relative_to(root)), sha256_file(path)) for path in files
        )
    )


def parse_metric(output: str, metric: MetricSpec) -> ParsedMetric:
    """Parse exactly one numeric metric from scrubbed benchmark output."""
    matches = list(re.finditer(metric.pattern, output, re.IGNORECASE | re.MULTILINE))
    if len(matches) != 1:
        raise AdapterError(
            f"expected exactly one {metric.name!r} metric match, found {len(matches)}"
        )
    match = matches[0]
    raw_value = match.group(metric.value_group)
    try:
        value = float(raw_value)
    except ValueError as error:
        raise AdapterError(f"metric {metric.name!r} is not numeric") from error
    if value != value or value in (float("inf"), float("-inf")):
        raise AdapterError(f"metric {metric.name!r} must be finite")
    unit = match.groupdict().get(metric.unit_group)
    return ParsedMetric(metric.name, value, unit, metric.direction, match.group(0))


def unavailable_plan(adapter: str, workdir: Path, reason: str) -> ProfilePlan:
    return ProfilePlan(adapter, "unavailable", None, (), safe_workdir(workdir), None, reason)


def canonical_request_json(value: object) -> str:
    """Stable serialization used to bind profile and evidence requests to data."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
