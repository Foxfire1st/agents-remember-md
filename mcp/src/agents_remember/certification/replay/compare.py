"""Baseline-vs-treatment replay comparison report.

The report binds the exact freeze identity of each leg, the pair comparability
verdict, the measured treatment facts, and the machine-readable outcome of
every mandatory acceptance scenario.  It carries raw measurements only: no
numeric reduction threshold appears anywhere in the record.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.replay.freeze import (
    ReplayComparabilityReport,
    ReplayFreeze,
    ReplayPopulation,
)
from agents_remember.certification.replay.models import (
    ReplayScenarioEvidence,
    RunMeasurement,
    ScenarioOutcome,
)
from agents_remember.certification.replay.scenarios import (
    evaluate_all_replay_scenarios,
)
from agents_remember.models.certification.base import FrozenContractModel, SemanticText

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ReplayComparisonInput(FrozenContractModel):
    """The complete frozen pair plus measured legs one comparison report binds."""

    baselineFreeze: ReplayFreeze
    treatmentFreeze: ReplayFreeze
    comparability: ReplayComparabilityReport
    baselineMeasurement: RunMeasurement
    treatmentMeasurement: RunMeasurement
    evidence: ReplayScenarioEvidence
    population: ReplayPopulation | None = None
    note: SemanticText | None = Field(default=None, max_length=4096)


class ReplayComparisonReport(FrozenContractModel):
    """One digest-bound comparison of a baseline and treatment replay pair."""

    schemaVersion: Literal["measured-replay-comparison/v1"] = "measured-replay-comparison/v1"
    baselineFreeze: ReplayFreeze
    treatmentFreeze: ReplayFreeze
    comparability: ReplayComparabilityReport
    population: ReplayPopulation | None = None
    baselineLegDigest: str = Field(pattern=_DIGEST_PATTERN)
    treatmentLegDigest: str = Field(pattern=_DIGEST_PATTERN)
    scenarioOutcomes: tuple[ScenarioOutcome, ...] = Field(
        min_length=17,
        max_length=17,
    )
    note: SemanticText | None = Field(default=None, max_length=4096)
    reportDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_report_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"reportDigest"})
        if self.reportDigest != content_digest(payload):
            raise ValueError("comparison report digest does not match its content")
        return self


def build_replay_comparison_report(
    inputs: ReplayComparisonInput,
) -> ReplayComparisonReport:
    """Build the machine-readable pair report from measured replay evidence."""
    evidence = inputs.evidence.model_copy(
        update={
            "treatment": inputs.treatmentMeasurement,
            "baseline": inputs.baselineMeasurement,
        }
    )
    outcomes = evaluate_all_replay_scenarios(evidence)
    draft = ReplayComparisonReport.model_construct(
        baselineFreeze=inputs.baselineFreeze,
        treatmentFreeze=inputs.treatmentFreeze,
        comparability=inputs.comparability,
        population=inputs.population,
        baselineLegDigest=inputs.baselineMeasurement.leg.freezeDigest,
        treatmentLegDigest=inputs.treatmentMeasurement.leg.freezeDigest,
        scenarioOutcomes=outcomes,
        note=inputs.note,
        reportDigest="",
    )
    payload = draft.model_dump(mode="json", exclude={"reportDigest"})
    digest = content_digest(payload)
    return ReplayComparisonReport.model_validate({**payload, "reportDigest": digest})


__all__ = [
    "FrozenContractModel",
    "ReplayComparisonInput",
    "ReplayComparisonReport",
    "ReplayFreeze",
    "ReplayPopulation",
    "ReplayScenarioEvidence",
    "RunMeasurement",
    "ScenarioOutcome",
    "SemanticText",
    "build_replay_comparison_report",
    "evaluate_all_replay_scenarios",
]
