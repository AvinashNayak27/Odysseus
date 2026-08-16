from __future__ import annotations

import pytest

from odysseus.experiments.ledger import ExperimentRecord
from odysseus.experiments.statistics import CandidateClassification, ConfidenceInterval
from odysseus.reports.renderer import ReportError, ReportRenderer, ReviewInput, validate_public_note


def experiment(*, measurement_verified: bool = True) -> ExperimentRecord:
    return ExperimentRecord(
        "experiment-1",
        "candidate-1",
        "succeeded",
        True,
        CandidateClassification.IMPROVED,
        "minimize",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        ((0, "baseline", "candidate-1"),),
        (),
        ConfidenceInterval(0.95, 0.01, 0.1, 0.2, 100, 7),
        "/var/tmp/repository/work",
        ("local estimate",),
        measurement_verified,
    )


def substantive_hypothesis() -> str:
    return "\n\n".join(
        (
            "The hot allocation path constructs a temporary lookup result for every input item. "
            "The candidate reuses an existing bounded buffer, retains the same parsing order, and "
            "does not change the externally visible result.",
            "The baseline profile attributes most sampled allocation activity to lookup-result creation. "
            "The trial plan therefore measures the benchmark's canonical objective after a correctness "
            "smoke check, rather than inferring an improvement from a microbenchmark.",
            "The candidate changes only the locked editable source path. It does not alter build flags, "
            "dependencies, benchmark setup, verifier inputs, or source-selection metadata. The detached "
            "worktree is retained for reviewers to compare its patch against the locked source reference.",
            "Measurements are serialized to avoid benchmark overlap. Every candidate measurement is paired "
            "with a baseline measurement in the seeded interleaved order, so the confidence interval is "
            "derived from objective-normalized paired effects rather than a single best observation.",
            "The local estimate is deliberately conservative: recommendation review uses the lower bound of "
            "the deterministic confidence interval. A reviewer must examine the full patch, observations, "
            "environment fingerprint, toolchain record, and frontier before any independent manual action.",
        )
    )


def input_for(
    hypothesis: str,
    *,
    changed_files: tuple[str, ...] = ("src/file.py",),
    editable_paths: tuple[str, ...] = ("src/file.py",),
    measurement_verified: bool = True,
) -> ReviewInput:
    return ReviewInput(
        "run-1",
        "benchmark",
        "abcdef",
        "/var/tmp/repository",
        "/var/tmp/repository/work",
        editable_paths,
        "provider",
        "exact-model-1",
        "high",
        "Odysseus",
        hypothesis,
        tuple(f"evidence-{number}" for number in range(1, 36)),
        changed_files,
        experiment(measurement_verified=measurement_verified),
        {
            "candidate-1": "candidate-record",
            **{f"evidence-{number}": f"source-record-{number}" for number in range(1, 36)},
        },
        caveats=tuple(
            f"Caveat {number}: local measurements require independent review of workload stability and "
            "environment comparability before a manual decision."
            for number in range(1, 13)
        ),
        next_steps=tuple(
            f"Review step {number}: inspect the corresponding immutable evidence and trial artifact."
            for number in range(1, 13)
        ),
    )


def test_report_renderer_keeps_exact_model_in_metadata_only_and_has_trace_caveat() -> None:
    renderer = ReportRenderer({"/var/tmp": "$WORKSPACE"})
    bundle = renderer.render(input_for(substantive_hypothesis()))
    assert bundle.publication_metadata.model == "exact-model-1"
    assert "Model:" not in bundle.public_note
    assert "Effort: high" in bundle.public_note
    assert "Agent/Harness: Odysseus" in bundle.public_note
    assert "Trace coverage caveat" in bundle.public_note
    assert "/var/tmp" not in bundle.public_note
    assert len(bundle.public_note.encode()) >= 5 * 1024


def test_report_renderer_accepts_changed_file_nested_below_editable_root() -> None:
    renderer = ReportRenderer({"/var/tmp": "$WORKSPACE"})
    bundle = renderer.render(
        input_for(
            substantive_hypothesis(),
            changed_files=("src/subdir/file.py",),
            editable_paths=("src",),
        )
    )
    assert "`src/subdir/file.py`" in bundle.public_note


@pytest.mark.parametrize(
    ("changed_file", "editable_root"),
    (
        ("../escape.py", "src"),
        ("/absolute/path.py", "src"),
        ("other/file.py", "src"),
    ),
)
def test_report_renderer_rejects_changed_file_outside_editable_root(
    changed_file: str, editable_root: str
) -> None:
    renderer = ReportRenderer({"/var/tmp": "$WORKSPACE"})
    with pytest.raises(ReportError, match="contained"):
        renderer.render(
            input_for(
                substantive_hypothesis(),
                changed_files=(changed_file,),
                editable_paths=(editable_root,),
            )
        )


def test_report_renderer_rejects_unverified_measurements() -> None:
    renderer = ReportRenderer({"/var/tmp": "$WORKSPACE"})
    with pytest.raises(ReportError, match="unverified"):
        renderer.render(input_for(substantive_hypothesis(), measurement_verified=False))


def test_public_report_refuses_secret_and_invalid_note_bounds() -> None:
    renderer = ReportRenderer({"/var/tmp": "$WORKSPACE"})
    with pytest.raises(ReportError, match="secret"):
        renderer.render(input_for("token=synthetic-secret"))
    with pytest.raises(ReportError, match="bounds"):
        ReportRenderer({"/var/tmp": "$WORKSPACE"}, note_min_bytes=1).render(
            input_for(substantive_hypothesis())
        )


def test_public_note_minimum_is_configurable_only_above_yukon_floor() -> None:
    note = "x" * (6 * 1024)
    validate_public_note(note, min_bytes=6 * 1024, max_bytes=7 * 1024)
    with pytest.raises(ReportError, match="between"):
        validate_public_note(note, min_bytes=7 * 1024, max_bytes=8 * 1024)
