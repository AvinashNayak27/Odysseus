from datetime import UTC, datetime

import pytest

from odysseus.yukon.discovery import (
    CloneValidation,
    bind_source_snapshot,
    build_shortlist,
    select_after_validation,
)
from odysseus.yukon.parser import BenchmarkDetails, BenchmarkSummary

ID_A = "11111111-1111-4111-8111-111111111111"
ID_Z = "22222222-2222-4222-8222-222222222222"
ID_CLOSED = "33333333-3333-4333-8333-333333333333"
ID_OUTSIDER = "44444444-4444-4444-8444-444444444444"


def details(benchmark_id: str, goal: str = "higher score is better") -> BenchmarkDetails:
    return BenchmarkDetails(
        benchmark_id,
        "proving",
        goal,
        "1",
        "later",
        "https://example.invalid/a.git",
        "abc",
        "main",
        "score.json",
        "setup",
        "run",
        "safe",
    )


def summary(benchmark_id: str, status: str = "open", goal: str = "higher") -> BenchmarkSummary:
    return BenchmarkSummary(benchmark_id, status, "proving", goal, "1", "later", benchmark_id)


def test_shortlist_accepts_real_goal_formats_then_selects_after_clone_validation() -> None:
    record = build_shortlist(
        [summary(ID_Z), summary(ID_A), summary(ID_CLOSED, "closed")],
        [details(ID_A), details(ID_Z), details(ID_CLOSED)],
        retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    bound = bind_source_snapshot(record, ["sanitized list", "sanitized show"])
    assert [item.benchmark_id for item in bound.candidates] == [ID_A, ID_Z, ID_CLOSED]
    assert bound.candidates[-1].excluded_reason == "benchmark is not open"
    selection = select_after_validation(
        bound,
        [
            CloneValidation(ID_A, False, "clone failed"),
            CloneValidation(ID_Z, True, "validated", "/repo", "/repo/work", ("src",)),
        ],
    )
    assert selection.selected_benchmark_id == ID_Z
    assert [item.reason for item in selection.validations] == ["clone failed", "validated"]


@pytest.mark.parametrize(
    ("list_goal", "show_goal"),
    [("higher", "higher score is better"), ("lower", "lower score is better")],
)
def test_real_higher_lower_goal_text_is_eligible(list_goal: str, show_goal: str) -> None:
    record = build_shortlist([summary(ID_A, goal=list_goal)], [details(ID_A, show_goal)])
    assert record.candidates[0].excluded_reason is None
    assert dict(record.candidates[0].score_components)["supported_objective"] == 20


def test_clone_only_fields_are_not_part_of_preclone_shortlist() -> None:
    record = build_shortlist([summary(ID_A)], [details(ID_A)])
    candidate = record.candidates[0]
    assert not hasattr(candidate, "repository_root")
    assert not hasattr(candidate, "editable_paths")


def test_two_phase_record_rejects_invalid_or_unrelated_validation_uuid() -> None:
    record = build_shortlist([summary(ID_A)], [details(ID_A)])
    with pytest.raises(ValueError, match="does not belong"):
        select_after_validation(record, [CloneValidation(ID_OUTSIDER, False, "not shortlisted")])
    with pytest.raises(ValueError, match="not a Yukon UUID"):
        select_after_validation(record, [CloneValidation("not-a-uuid", False, "invalid")])
