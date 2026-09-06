"""Replay freeze identity, incident population, and pair comparability.

A replay freezes the exact source revision, candidate sequence, R22 profile,
R11 plan, configuration, runtime/toolchain/executor/image, machine class,
instrumentation posture, and measurement schema before either leg runs.
Baseline and treatment are comparable only when those frozen dimensions agree;
observation metadata is deliberately not part of the frozen identity and can
never invalidate a pair.  The incident population is the approved three-view
baseline: frozen original generations 1-8, post-analysis tail 9-13, and dated
supplements 14+ that are append-only and never enter the primary denominator.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CertificationContractFinding
from agents_remember.certification.replay.models import (
    PopulationGeneration,
    ReplayFreezeInputChange,
    ReplayStratum,
)
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import FrozenContractModel, SemanticText

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"

FROZEN_DENOMINATOR_LIMIT = 13


class ReplayFreezeInput(FrozenContractModel):
    """The exact frozen inputs one replay leg ran under."""

    sourceRevision: SemanticText = Field(max_length=4096)
    candidateDigest: str = Field(pattern=_DIGEST_PATTERN)
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    profileDigest: str = Field(pattern=_DIGEST_PATTERN)
    planDigest: str = Field(pattern=_DIGEST_PATTERN)
    configurationDigest: str = Field(pattern=_DIGEST_PATTERN)
    runtimeIdentity: SemanticText = Field(max_length=512)
    toolchainDigest: str = Field(pattern=_DIGEST_PATTERN)
    executorDigest: str = Field(pattern=_DIGEST_PATTERN)
    imageDigest: str = Field(pattern=_DIGEST_PATTERN)
    machineClass: SemanticText = Field(max_length=512)
    instrumentationOnly: bool = True
    measurementSchema: SemanticText = Field(max_length=256)


class ReplayFreeze(FrozenContractModel):
    """One digest-bound replay freeze."""

    schemaVersion: Literal["measured-replay-freeze/v1"] = "measured-replay-freeze/v1"
    input: ReplayFreezeInput
    freezeDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        expected = content_digest(
            {"schemaVersion": self.schemaVersion, "input": self.input.model_dump(mode="json")}
        )
        if self.freezeDigest != expected:
            raise ValueError("replay freeze digest does not match its frozen input")
        return self


class ReplayComparabilityReport(FrozenContractModel):
    """Comparability verdict for one baseline/treatment replay pair."""

    schemaVersion: Literal["measured-replay-comparability/v1"] = "measured-replay-comparability/v1"
    baselineFreezeDigest: str = Field(pattern=_DIGEST_PATTERN)
    treatmentFreezeDigest: str = Field(pattern=_DIGEST_PATTERN)
    comparable: bool
    changes: tuple[ReplayFreezeInputChange, ...] = Field(default_factory=tuple)
    findings: tuple[CertificationContractFinding, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _require_report_shape(self) -> Self:
        if self.comparable and (self.changes or self.findings):
            raise ValueError("a comparable pair carries no change or finding")
        if not self.comparable and not (self.changes or self.findings):
            raise ValueError("an incomparable pair requires a typed change or finding")
        return self


class ReplayPopulation(FrozenContractModel):
    """The append-only three-view incident population for a replay pair."""

    schemaVersion: Literal["measured-replay-population/v1"] = "measured-replay-population/v1"
    generations: tuple[PopulationGeneration, ...] = Field(min_length=1)
    populationDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_population(self) -> Self:
        generations = [item.generation for item in self.generations]
        if len(set(generations)) != len(generations):
            raise ValueError("incident generations must be unique")
        expected = content_digest(
            {
                "schemaVersion": self.schemaVersion,
                "generations": [item.model_dump(mode="json") for item in self.generations],
            }
        )
        if self.populationDigest != expected:
            raise ValueError("incident population digest does not match its rows")
        return self

    def stratum(self, stratum: ReplayStratum) -> tuple[PopulationGeneration, ...]:
        return tuple(item for item in self.generations if item.stratum == stratum)


def compile_replay_freeze(input: ReplayFreezeInput) -> ReplayFreeze:
    """Compile one digest-bound replay freeze from its exact frozen inputs."""
    digest = content_digest(
        {"schemaVersion": "measured-replay-freeze/v1", "input": input.model_dump(mode="json")}
    )
    return ReplayFreeze(input=input, freezeDigest=digest)


def freeze_digest(input: ReplayFreezeInput) -> str:
    """The exact content digest of one frozen-input set."""
    return content_digest(
        {"schemaVersion": "measured-replay-freeze/v1", "input": input.model_dump(mode="json")}
    )


def compare_replay_freezes(
    baseline: ReplayFreeze,
    treatment: ReplayFreeze,
) -> ReplayComparabilityReport:
    """Refuse the pair when any frozen dimension changed between the legs."""
    changes: list[ReplayFreezeInputChange] = [
        *_identity_changes(baseline, treatment),
        *_runtime_changes(baseline, treatment),
    ]
    findings = tuple(
        CertificationContractFinding(
            code="replay-pair-incomparable",
            path=f"freeze.{change.changeClass}",
            detail=change.reason,
        )
        for change in changes
    )
    return ReplayComparabilityReport(
        baselineFreezeDigest=baseline.freezeDigest,
        treatmentFreezeDigest=treatment.freezeDigest,
        comparable=not changes,
        changes=tuple(changes),
        findings=findings,
    )


def _identity_changes(
    baseline: ReplayFreeze,
    treatment: ReplayFreeze,
) -> list[ReplayFreezeInputChange]:
    """Frozen dimensions other than the runtime/executor/image tuple."""
    changes: list[ReplayFreezeInputChange] = []
    if baseline.input.sourceRevision != treatment.input.sourceRevision:
        changes.append(
            ReplayFreezeInputChange(
                changeClass="source",
                reason="source revision changed between baseline and treatment",
            )
        )
    if baseline.input.candidateDigest != treatment.input.candidateDigest:
        changes.append(
            ReplayFreezeInputChange(
                changeClass="source",
                reason="candidate digest changed between baseline and treatment",
            )
        )
    if (baseline.input.profileId, baseline.input.profileDigest) != (
        treatment.input.profileId,
        treatment.input.profileDigest,
    ):
        changes.append(
            ReplayFreezeInputChange(
                changeClass="profile",
                reason="R22 repository profile changed between baseline and treatment",
            )
        )
    if baseline.input.planDigest != treatment.input.planDigest:
        changes.append(
            ReplayFreezeInputChange(
                changeClass="plan",
                reason="R11 gate plan changed between baseline and treatment",
            )
        )
    if baseline.input.configurationDigest != treatment.input.configurationDigest:
        changes.append(
            ReplayFreezeInputChange(
                changeClass="configuration",
                reason="configuration changed between baseline and treatment",
            )
        )
    if baseline.input.machineClass != treatment.input.machineClass:
        changes.append(
            ReplayFreezeInputChange(
                changeClass="machine-class",
                reason="machine class changed between baseline and treatment",
            )
        )
    if baseline.input.instrumentationOnly != treatment.input.instrumentationOnly:
        changes.append(
            ReplayFreezeInputChange(
                changeClass="instrumentation",
                reason="instrumentation posture changed between baseline and treatment",
            )
        )
    if baseline.input.measurementSchema != treatment.input.measurementSchema:
        changes.append(
            ReplayFreezeInputChange(
                changeClass="measurement-schema",
                reason="measurement schema changed between baseline and treatment",
            )
        )
    return changes


def _runtime_changes(
    baseline: ReplayFreeze,
    treatment: ReplayFreeze,
) -> list[ReplayFreezeInputChange]:
    """The runtime, toolchain, executor, and image tuple is one dimension."""
    if (
        baseline.input.runtimeIdentity,
        baseline.input.toolchainDigest,
        baseline.input.executorDigest,
        baseline.input.imageDigest,
    ) != (
        treatment.input.runtimeIdentity,
        treatment.input.toolchainDigest,
        treatment.input.executorDigest,
        treatment.input.imageDigest,
    ):
        return [
            ReplayFreezeInputChange(
                changeClass="runtime-toolchain-executor-image",
                reason="runtime, toolchain, executor, or image changed between the legs",
            )
        ]
    return []


def require_comparable_replay_pair(
    baseline: ReplayFreeze,
    treatment: ReplayFreeze,
) -> None:
    """Raise when the pair changed a frozen dimension; never on metadata."""
    report = compare_replay_freezes(baseline, treatment)
    if not report.comparable:
        raise CertificationContractError(
            "replay pair is incomparable: "
            + "; ".join(f"{change.changeClass}: {change.reason}" for change in report.changes),
            [],
        )


def compile_replay_population(
    generations: Sequence[PopulationGeneration],
) -> ReplayPopulation:
    """Compile one append-only incident population from validated generation rows."""
    ordered = sorted(generations, key=lambda item: (item.generation, item.stratum))
    digest = content_digest(
        {
            "schemaVersion": "measured-replay-population/v1",
            "generations": [item.model_dump(mode="json") for item in ordered],
        }
    )
    return ReplayPopulation(generations=tuple(ordered), populationDigest=digest)


def population_denominator(population: ReplayPopulation) -> tuple[int, ...]:
    """Generations counted in the primary denominator (never dated supplements)."""
    return tuple(
        item.generation
        for item in population.generations
        if item.stratum in {"frozen-original", "post-analysis-tail"}
    )


def frozen_generation_rows(
    population: ReplayPopulation,
) -> tuple[PopulationGeneration, ...]:
    """Frozen original generations 1-8 exactly as recorded."""
    return population.stratum("frozen-original")


def tail_generation_rows(
    population: ReplayPopulation,
) -> tuple[PopulationGeneration, ...]:
    """Post-analysis tail generations 9-13 exactly as recorded."""
    return population.stratum("post-analysis-tail")


def dated_supplement_rows(
    population: ReplayPopulation,
) -> tuple[PopulationGeneration, ...]:
    """Dated supplements 14+; qualitative only, excluded from the denominator."""
    return population.stratum("dated-supplement")


def require_append_only_population(
    population: ReplayPopulation,
    successor: ReplayPopulation,
) -> None:
    """Historical baseline rows are append-only; supplements never rewrite them."""
    base_by_generation = {item.generation: item for item in population.generations}
    max_base_generation = max(base_by_generation)
    for row in successor.generations:
        existing = base_by_generation.get(row.generation)
        if existing is None:
            if row.stratum != "dated-supplement" or row.generation <= max_base_generation:
                raise CertificationContractError(
                    "successor generations may only append dated supplements",
                    [],
                )
            continue
        if existing != row:
            if row.stratum == "dated-supplement" or existing.stratum != row.stratum:
                raise CertificationContractError(
                    "a dated supplement may never rewrite a frozen baseline row",
                    [],
                )
            raise CertificationContractError(
                "a frozen baseline row may never be rewritten",
                [],
            )


__all__ = [
    "FrozenContractModel",
    "PopulationGeneration",
    "ReplayComparabilityReport",
    "ReplayFreeze",
    "ReplayFreezeInput",
    "ReplayFreezeInputChange",
    "ReplayPopulation",
    "ReplayStratum",
    "SemanticText",
    "compare_replay_freezes",
    "compile_replay_freeze",
    "compile_replay_population",
    "dated_supplement_rows",
    "freeze_digest",
    "frozen_generation_rows",
    "population_denominator",
    "require_append_only_population",
    "require_comparable_replay_pair",
    "tail_generation_rows",
]
