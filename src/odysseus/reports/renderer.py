"""Render review artifacts without any publication side effect."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from odysseus.experiments.ledger import ExperimentRecord
from odysseus.redaction import PublicOutputError, redact_path, redact_private, scrub_public


class ReportError(ValueError):
    pass


_MIN_NOTE_BYTES = 5 * 1024
_MAX_NOTE_BYTES = 100 * 1024
_PRIVATE_PATH = re.compile(
    r"(?:(?<![A-Za-z0-9._-])/(?:home|Users|private|var|tmp)/|[A-Za-z]:\\(?:Users|home)\\)"
)
_SECRET = re.compile(
    r"(?i)\b(?:api[_-]?key|token|secret|password|credential)\b|\b(?:sk|pk)_[A-Za-z0-9_-]{16,}\b"
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_TRACE = re.compile(r"(?i)trace(?:back|[_ -]?(?:id|payload))")
_MODEL_LINE = re.compile(r"(?im)^\s*model\s*:")


@dataclass(frozen=True, slots=True)
class ReviewInput:
    run_id: str
    benchmark_id: str
    base_source_ref: str
    repository_root: str
    workdir: str
    editable_paths: tuple[str, ...]
    provider: str
    model: str
    effort: str
    harness: str
    hypothesis: str
    evidence_ids: tuple[str, ...]
    changed_files: tuple[str, ...]
    experiment: ExperimentRecord
    provenance: Mapping[str, str]
    caveats: tuple[str, ...] = ()
    failed_paths: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicationMetadata:
    provider: str
    model: str
    effort: str
    harness: str
    benchmark_id: str
    run_id: str
    note_sha256: str

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class ReviewBundle:
    private_report: str
    public_note: str
    publication_metadata: PublicationMetadata


def validate_public_note(
    note: str, *, min_bytes: int = _MIN_NOTE_BYTES, max_bytes: int = _MAX_NOTE_BYTES
) -> None:
    encoded = note.encode("utf-8")
    if min_bytes < _MIN_NOTE_BYTES or max_bytes > _MAX_NOTE_BYTES or min_bytes > max_bytes:
        raise ReportError("Yukon note bounds must remain within 5 KiB through 100 KiB")
    if not min_bytes <= len(encoded) <= max_bytes:
        raise ReportError(f"public note must be between {min_bytes} and {max_bytes} bytes")
    if _MODEL_LINE.search(note):
        raise ReportError("public note body must not duplicate publication Model metadata")
    if (
        _PRIVATE_PATH.search(note)
        or _SECRET.search(note)
        or _EMAIL.search(note)
        or _TRACE.search(note)
    ):
        raise ReportError(
            "public note contains a private path, secret, personal identifier, or trace content"
        )
    if "\x00" in note:
        raise ReportError("public note contains a prohibited NUL byte")


class ReportRenderer:
    """Pure deterministic renderer; it neither invokes Yukon nor writes files."""

    def __init__(
        self,
        aliases: Mapping[str, str],
        *,
        note_min_bytes: int = _MIN_NOTE_BYTES,
        note_max_bytes: int = _MAX_NOTE_BYTES,
    ):
        if not aliases:
            raise ReportError("private path aliases are required for report rendering")
        self._aliases = dict(aliases)
        self._min_bytes = note_min_bytes
        self._max_bytes = note_max_bytes

    def render(self, item: ReviewInput) -> ReviewBundle:
        self._validate_input(item)
        private = self._render_private(item)
        public = self._render_public(item)
        validate_public_note(public, min_bytes=self._min_bytes, max_bytes=self._max_bytes)
        metadata = PublicationMetadata(
            provider=item.provider,
            model=item.model,
            effort=item.effort,
            harness=item.harness,
            benchmark_id=item.benchmark_id,
            run_id=item.run_id,
            note_sha256=sha256(public.encode("utf-8")).hexdigest(),
        )
        return ReviewBundle(private, public, metadata)

    def _validate_input(self, item: ReviewInput) -> None:
        required = (
            item.run_id,
            item.benchmark_id,
            item.provider,
            item.model,
            item.effort,
            item.harness,
            item.hypothesis,
        )
        if any(not value.strip() for value in required):
            raise ReportError(
                "review input requires exact provider, model, effort, harness, and context"
            )
        if not item.evidence_ids or not item.provenance:
            raise ReportError("recommendations require evidence and provenance")
        if item.experiment.candidate_id not in item.provenance:
            raise ReportError("candidate provenance is missing")
        if not item.experiment.measurement_verified:
            raise ReportError(
                "cannot render a recommendation from imported or unverified measurements"
            )
        if not item.experiment.correctness_valid:
            raise ReportError("cannot render a recommendation for a correctness-invalid candidate")
        if item.experiment.confidence_interval is None:
            raise ReportError("experiment confidence interval is required")
        if not item.changed_files or not _changed_files_within_editable_roots(
            item.changed_files, item.editable_paths
        ):
            raise ReportError(
                "all reported changed files must be contained by locked editable paths"
            )
        # Never silently redact a direct secret/path input into a publishable claim.
        values = (
            *item.evidence_ids,
            item.hypothesis,
            *item.caveats,
            *item.failed_paths,
            *item.next_steps,
        )
        if any(
            _SECRET.search(value)
            or _EMAIL.search(value)
            or _TRACE.search(value)
            or _PRIVATE_PATH.search(value)
            for value in values
        ):
            raise ReportError(
                "untrusted review input includes a private path, secret, personal, or trace content"
            )

    def _render_private(self, item: ReviewInput) -> str:
        interval = item.experiment.confidence_interval
        assert interval is not None
        lines = [
            f"# Odysseus Review: {item.benchmark_id}",
            "",
            "## Scope and workspace",
            f"- Run: `{item.run_id}`",
            f"- Base source/ref: `{item.base_source_ref}`",
            f"- Repository root: `{redact_path_path(item.repository_root, self._aliases)}`",
            f"- Benchmark workdir: `{redact_path_path(item.workdir, self._aliases)}`",
            f"- Editable paths: {', '.join(f'`{path}`' for path in item.editable_paths)}",
            "",
            "## Candidate and correctness",
            f"- Candidate: `{item.experiment.candidate_id}`",
            f"- Hypothesis: {redact_private(item.hypothesis)}",
            f"- Correctness: {'valid' if item.experiment.correctness_valid else 'invalid'}",
            f"- Local classification: `{item.experiment.classification.value}`",
            "",
            "## Measurements",
            f"- Objective: `{item.experiment.objective}`",
            f"- Relative effect estimate: {interval.estimate:.4%}",
            (
                f"- Deterministic {interval.confidence_level:.0%} CI: "
                f"[{interval.lower:.4%}, {interval.upper:.4%}]"
            ),
            f"- Bootstrap seed/samples: `{interval.seed}` / `{interval.bootstrap_samples}`",
            f"- Environment fingerprint: `{item.experiment.environment_sha256}`",
            f"- Patch fingerprint: `{item.experiment.patch_sha256}`",
            "",
            "## Provenance",
        ]
        lines.extend(f"- `{key}`: `{value}`" for key, value in sorted(item.provenance.items()))
        lines.extend(
            (
                "",
                "## Caveats",
                "- Local estimates are not official Yukon scores.",
                (
                    "- Trace coverage was not established by this report; "
                    "absent or degraded traces must not be interpreted as absence of work."
                ),
            )
        )
        lines.extend(
            f"- {redact_private(caveat)}" for caveat in (*item.experiment.caveats, *item.caveats)
        )
        lines.extend(
            (
                "",
                "## Reviewer checklist",
                "- Verify source/patch/environment provenance.",
                (
                    "- Independently inspect the exact model in publication metadata before any "
                    "manual operation."
                ),
                (
                    "- Review all measurements and the diff; no publishing capability exists in "
                    "Odysseus."
                ),
            )
        )
        return "\n".join(lines) + "\n"

    def _render_public(self, item: ReviewInput) -> str:
        interval = item.experiment.confidence_interval
        assert interval is not None
        lines = [
            f"# Optimization Review: {item.benchmark_id}",
            "",
            "## Context",
            f"- Base source/ref: `{item.base_source_ref}`",
            f"- Workdir: `{redact_path_path(item.workdir, self._aliases)}`",
            f"- Changed editable files: {', '.join(f'`{path}`' for path in item.changed_files)}",
            f"- Effort: {item.effort}",
            f"- Agent/Harness: {item.harness}",
            "",
            "## Hypothesis and evidence",
            scrub_public(item.hypothesis, self._aliases),
            "",
            "Evidence references:",
        ]
        lines.extend(f"- `{evidence_id}`" for evidence_id in item.evidence_ids)
        lines.extend(
            (
                "",
                "## Local trial method and results",
                "- Correctness smoke check passed before the measured trials.",
                "- Baseline and candidate measurements were serialized and deterministically "
                "interleaved.",
                f"- Objective: `{item.experiment.objective}`.",
                (
                    f"- Relative effect estimate: {interval.estimate:.4%}; deterministic "
                    f"{interval.confidence_level:.0%} CI: [{interval.lower:.4%}, "
                    f"{interval.upper:.4%}]."
                ),
                f"- Local classification: `{item.experiment.classification.value}`.",
                "",
                "## Caveats and learning",
                "- These are local estimates, not official Yukon scores.",
                (
                    "- Trace coverage caveat: this note does not assert complete trace capture; "
                    "trace availability or degradation can limit mechanism evidence."
                ),
                (
                    "- The full review bundle retains trial order, scrubbed observations, "
                    "fingerprints, and provenance for human inspection."
                ),
            )
        )
        lines.extend(
            f"- {scrub_public(caveat, self._aliases)}"
            for caveat in (*item.experiment.caveats, *item.caveats)
        )
        if item.failed_paths:
            lines.extend(("", "## Failed paths"))
            lines.extend(f"- {scrub_public(value, self._aliases)}" for value in item.failed_paths)
        lines.extend(("", "## Next steps"))
        lines.extend(f"- {scrub_public(value, self._aliases)}" for value in item.next_steps)
        lines.extend(
            (
                (
                    "- Human review of exact model metadata, patch, and local evidence is "
                    "required before any separate manual action."
                ),
                "",
            )
        )
        return "\n".join(lines)


def _changed_files_within_editable_roots(
    changed_files: tuple[str, ...], editable_roots: tuple[str, ...]
) -> bool:
    """Accept files nested below an editable root, never paths outside it."""
    try:
        roots = tuple(_relative_path(value) for value in editable_roots)
        changed = tuple(_relative_path(value) for value in changed_files)
    except ValueError:
        return False
    return all(any(path == root or root in path.parents for root in roots) for path in changed)


def _relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("path must be non-empty and relative")
    return path


def redact_path_path(value: str, aliases: Mapping[str, str]) -> str:
    try:
        return redact_path(Path(value), aliases)
    except PublicOutputError as error:
        raise ReportError("report workspace path lacks a private alias") from error
