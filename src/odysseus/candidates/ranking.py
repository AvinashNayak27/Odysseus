"""Deterministic pre-experiment candidate ranking."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CandidateScore:
    candidate_id: str
    profile_relevance: float
    evidence_strength: float
    expected_impact: float
    implementation_scope: float
    correctness_risk: float

    @property
    def total(self) -> float:
        values = (
            self.profile_relevance,
            self.evidence_strength,
            self.expected_impact,
            self.implementation_scope,
            self.correctness_risk,
        )
        if any(value < 0 or value > 1 for value in values):
            raise ValueError("candidate rank components must be normalized between zero and one")
        return (
            0.30 * self.profile_relevance
            + 0.25 * self.evidence_strength
            + 0.20 * self.expected_impact
            + 0.15 * self.implementation_scope
            + 0.10 * (1 - self.correctness_risk)
        )


def rank_candidates(candidates: tuple[CandidateScore, ...]) -> tuple[CandidateScore, ...]:
    return tuple(
        sorted(candidates, key=lambda candidate: (-candidate.total, candidate.candidate_id))
    )
