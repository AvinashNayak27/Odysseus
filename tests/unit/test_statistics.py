from __future__ import annotations

import pytest

from odysseus.experiments.statistics import (
    CandidateClassification,
    CandidateOutcome,
    Objective,
    StatisticsError,
    bootstrap_relative_effect_ci,
    classify_candidate,
    interleaved_trial_schedule,
    rank_outcomes,
    summarize_samples,
)


def test_statistics_and_bootstrap_are_deterministic() -> None:
    samples = summarize_samples((10.0, 11.0, 12.0, 13.0))
    assert samples.median == 11.5
    assert samples.mad == 1.0
    assert samples.iqr == 1.5
    first = bootstrap_relative_effect_ci(
        (100, 101, 99, 100), (90, 91, 89, 90), Objective.MINIMIZE, seed=17, bootstrap_samples=500
    )
    second = bootstrap_relative_effect_ci(
        (100, 101, 99, 100), (90, 91, 89, 90), Objective.MINIMIZE, seed=17, bootstrap_samples=500
    )
    assert first == second
    assert first.lower > 0
    assert classify_candidate(True, first) is CandidateClassification.IMPROVED
    assert classify_candidate(False, first) is CandidateClassification.INVALID


def test_statistics_rejects_invalid_samples_and_mismatched_pairs() -> None:
    with pytest.raises(StatisticsError):
        summarize_samples(())
    with pytest.raises(StatisticsError):
        summarize_samples((1, 0))
    with pytest.raises(StatisticsError):
        bootstrap_relative_effect_ci((1, 2), (1,), Objective.MAXIMIZE, seed=1)


def test_trial_schedule_is_seeded_serialized_and_paired() -> None:
    first = interleaved_trial_schedule(("b", "a"), 3, seed=4)
    assert first == interleaved_trial_schedule(("a", "b"), 3, seed=4)
    assert len(first) == 12
    for left, right in zip(first[::2], first[1::2], strict=True):
        assert left.repetition == right.repetition
        assert left.candidate_id == right.candidate_id
        assert {left.arm, right.arm} == {"baseline", "candidate"}


def test_correctness_first_ranking_uses_conservative_interval_bound() -> None:
    winner = bootstrap_relative_effect_ci(
        (10, 10), (8, 8), Objective.MINIMIZE, seed=1, bootstrap_samples=100
    )
    weaker = bootstrap_relative_effect_ci(
        (10, 10), (9, 9), Objective.MINIMIZE, seed=1, bootstrap_samples=100
    )
    outcomes = (
        CandidateOutcome(
            "invalid", False, winner, "high", 0, 1, CandidateClassification.INVALID, True
        ),
        CandidateOutcome(
            "missing-ci", True, None, "high", 0, 1, CandidateClassification.INCONCLUSIVE, True
        ),
        CandidateOutcome(
            "imported", True, winner, "high", 0, 1, CandidateClassification.IMPROVED, False
        ),
        CandidateOutcome(
            "weaker", True, weaker, "high", 0.1, 2, CandidateClassification.IMPROVED, True
        ),
        CandidateOutcome(
            "winner", True, winner, "low", 0.9, 3, CandidateClassification.IMPROVED, True
        ),
    )
    assert [item.candidate_id for item in rank_outcomes(outcomes)] == ["winner", "weaker"]
