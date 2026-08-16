"""Immutable experiment records with canonical content hashes."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256

from odysseus.experiments.statistics import CandidateClassification, ConfidenceInterval


@dataclass(frozen=True, slots=True)
class TrialObservation:
    repetition: int
    arm: str
    metric: float | None
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    output_sha256: str


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    experiment_id: str
    candidate_id: str
    status: str
    correctness_valid: bool
    classification: CandidateClassification
    objective: str
    source_sha256: str
    patch_sha256: str
    environment_sha256: str
    topology_evidence_sha256: str
    trial_order: tuple[tuple[int, str, str | None], ...]
    observations: tuple[TrialObservation, ...]
    confidence_interval: ConfidenceInterval | None
    worktree_path: str
    caveats: tuple[str, ...]
    measurement_verified: bool = False

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @property
    def sha256(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
