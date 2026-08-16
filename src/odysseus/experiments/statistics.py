"""Pure, deterministic statistics and correctness-first ranking helpers."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import sqrt
from random import Random
from statistics import median


class StatisticsError(ValueError):
    pass


class Objective(StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


class CandidateClassification(StrEnum):
    INVALID = "invalid"
    IMPROVED = "improved"
    REGRESSED = "regressed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class TrialSlot:
    repetition: int
    arm: str
    candidate_id: str | None = None


@dataclass(frozen=True, slots=True)
class SampleStatistics:
    count: int
    median: float
    mad: float
    iqr: float
    coefficient_of_variation: float


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    confidence_level: float
    lower: float
    estimate: float
    upper: float
    bootstrap_samples: int
    seed: int


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    candidate_id: str
    correctness_valid: bool
    confidence_interval: ConfidenceInterval | None
    evidence_confidence: str
    correctness_risk: float
    diff_bytes: int
    classification: CandidateClassification
    measurement_verified: bool


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _validate_samples(samples: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in samples)
    if not values:
        raise StatisticsError("at least one measurement is required")
    if any(value <= 0 for value in values):
        raise StatisticsError("measurements must be finite positive values")
    if any(value == float("inf") or value != value for value in values):
        raise StatisticsError("measurements must be finite positive values")
    return values


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values or not 0 <= probability <= 1:
        raise StatisticsError("invalid quantile request")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def summarize_samples(samples: Sequence[float]) -> SampleStatistics:
    """Compute robust spread metrics without dropping observations."""
    values = _validate_samples(samples)
    ordered = tuple(sorted(values))
    middle = median(ordered)
    deviations = tuple(sorted(abs(value - middle) for value in ordered))
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return SampleStatistics(
        count=len(values),
        median=middle,
        mad=median(deviations),
        iqr=_quantile(ordered, 0.75) - _quantile(ordered, 0.25),
        coefficient_of_variation=sqrt(variance) / mean,
    )


def relative_effect(baseline: float, candidate: float, objective: Objective) -> float:
    """Positive values always mean improvement, regardless of objective direction."""
    _validate_samples((baseline, candidate))
    if objective is Objective.MINIMIZE:
        return (baseline - candidate) / baseline
    if objective is Objective.MAXIMIZE:
        return (candidate - baseline) / baseline
    raise StatisticsError(f"unsupported objective: {objective}")


def bootstrap_relative_effect_ci(
    baseline_samples: Sequence[float],
    candidate_samples: Sequence[float],
    objective: Objective,
    *,
    seed: int,
    bootstrap_samples: int = 10_000,
    confidence_level: float = 0.95,
) -> ConfidenceInterval:
    """Use paired, seeded bootstrap resampling to make stored CI results reproducible."""
    baseline = _validate_samples(baseline_samples)
    candidate = _validate_samples(candidate_samples)
    if len(baseline) != len(candidate):
        raise StatisticsError("paired baseline and candidate samples must have equal length")
    if bootstrap_samples < 100:
        raise StatisticsError("at least 100 bootstrap samples are required")
    if not 0 < confidence_level < 1:
        raise StatisticsError("confidence level must be between zero and one")
    effects = tuple(
        relative_effect(left, right, objective)
        for left, right in zip(baseline, candidate, strict=True)
    )
    generator = Random(seed)
    estimates: list[float] = []
    for _ in range(bootstrap_samples):
        draw = [effects[generator.randrange(len(effects))] for _ in range(len(effects))]
        estimates.append(sum(draw) / len(draw))
    estimates.sort()
    alpha = (1 - confidence_level) / 2
    estimate = sum(effects) / len(effects)
    return ConfidenceInterval(
        confidence_level=confidence_level,
        lower=_quantile(estimates, alpha),
        estimate=estimate,
        upper=_quantile(estimates, 1 - alpha),
        bootstrap_samples=bootstrap_samples,
        seed=seed,
    )


def interleaved_trial_schedule(
    candidate_ids: Iterable[str], repetitions: int, *, seed: int
) -> tuple[TrialSlot, ...]:
    """Return a deterministic serialized baseline/candidate schedule.

    Each candidate is paired with a baseline in every repetition; the seeded
    order changes which arm leads each pair and candidate ordering, reducing
    monotonic time drift without concurrent benchmark execution.
    """
    candidates = tuple(sorted(set(candidate_ids)))
    if not candidates:
        raise StatisticsError("at least one candidate is required")
    if any(not value for value in candidates):
        raise StatisticsError("candidate IDs must be non-empty")
    if repetitions < 1:
        raise StatisticsError("at least one repetition is required")
    generator = Random(seed)
    slots: list[TrialSlot] = []
    for repetition in range(repetitions):
        order = list(candidates)
        generator.shuffle(order)
        for candidate_id in order:
            pair = [
                TrialSlot(repetition, "baseline", candidate_id),
                TrialSlot(repetition, "candidate", candidate_id),
            ]
            if generator.randrange(2):
                pair.reverse()
            slots.extend(pair)
    return tuple(slots)


def classify_candidate(
    correctness_valid: bool, interval: ConfidenceInterval | None
) -> CandidateClassification:
    if not correctness_valid:
        return CandidateClassification.INVALID
    if interval is None:
        return CandidateClassification.INCONCLUSIVE
    if interval.lower > 0:
        return CandidateClassification.IMPROVED
    if interval.upper < 0:
        return CandidateClassification.REGRESSED
    return CandidateClassification.INCONCLUSIVE


def rank_outcomes(outcomes: Iterable[CandidateOutcome]) -> tuple[CandidateOutcome, ...]:
    """Rank only verified, correctness-valid outcomes with a confidence interval.

    Imported samples and incomplete measurements remain inspectable records, but
    cannot be recommended or appear in the measured ranking.
    """
    rankable = [
        outcome
        for outcome in outcomes
        if outcome.measurement_verified
        and outcome.correctness_valid
        and outcome.confidence_interval is not None
    ]
    for outcome in rankable:
        if outcome.evidence_confidence not in _CONFIDENCE_RANK:
            raise StatisticsError("evidence confidence must be low, medium, or high")
        if outcome.diff_bytes < 0 or not 0 <= outcome.correctness_risk <= 1:
            raise StatisticsError("invalid candidate risk or diff size")
    return tuple(
        sorted(
            rankable,
            key=lambda item: (
                -_interval_lower(item),
                -_CONFIDENCE_RANK[item.evidence_confidence],
                item.correctness_risk,
                item.diff_bytes,
                item.candidate_id,
            ),
        )
    )


def _interval_lower(outcome: CandidateOutcome) -> float:
    interval = outcome.confidence_interval
    if interval is None:
        raise StatisticsError("rankable candidate unexpectedly lacks a confidence interval")
    return interval.lower
