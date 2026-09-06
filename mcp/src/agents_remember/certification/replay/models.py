"""CCR-R17-v3 measured-replay vocabulary and contract records.

Every durable record in this module is a closed immutable FrozenContractModel.
The vocabulary intentionally mirrors the approved replay protocol only:
freeze identity dimensions, the three-view incident-baseline population, span
category reductions, measured-run gate facts, the seventeen mandatory
acceptance-scenario expectations, and the pair comparison report.  Numeric
reduction thresholds are deliberately absent: no field in this package can
carry an approved performance claim.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CertificationContractFinding
from agents_remember.certification.telemetry.models import (
    CatalogCounts,
    CatalogRailRecord,
    TelemetrySpanKind,
)
from agents_remember.models.certification.base import (
    FrozenContractModel,
    GateId,
    RailIdentity,
    SemanticText,
)

MeasuredSpanCategory = TelemetrySpanKind

ReplayStratum = Literal[
    "frozen-original",
    "post-analysis-tail",
    "incident-baseline",
    "dated-supplement",
]

ScenarioState = Literal["green", "red", "refused", "not-applicable"]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

_REPLAY_SCENARIO_IDS: tuple[str, ...] = (
    "r17-scenario-01",
    "r17-scenario-02",
    "r17-scenario-03",
    "r17-scenario-04",
    "r17-scenario-05",
    "r17-scenario-06",
    "r17-scenario-07",
    "r17-scenario-08",
    "r17-scenario-09",
    "r17-scenario-10",
    "r17-scenario-11",
    "r17-scenario-12",
    "r17-scenario-13",
    "r17-scenario-14",
    "r17-scenario-15",
    "r17-scenario-16",
    "r17-scenario-17",
)


def _require_semantic_text(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("semantic text must be nonblank and unpadded")
    return value


ReplayScenarioId = Annotated[str, AfterValidator(_require_semantic_text)]
ProfileReference = Annotated[str, AfterValidator(_require_semantic_text)]


class ReplayScenarioExpectation(FrozenContractModel):
    """One mandatory acceptance scenario in the R17 replay protocol."""

    scenarioId: ReplayScenarioId
    title: SemanticText = Field(max_length=512)
    requirement: SemanticText = Field(max_length=8192)
    views: tuple[SemanticText, ...] = Field(default_factory=tuple, max_length=16)


class ScenarioOutcome(FrozenContractModel):
    """Machine-readable outcome for one mandatory acceptance scenario."""

    scenarioId: ReplayScenarioId
    state: ScenarioState
    findings: tuple[CertificationContractFinding, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _require_state_findings(self) -> Self:
        if self.state == "green" and self.findings:
            raise ValueError("a green scenario outcome carries no findings")
        if self.state != "green" and not self.findings:
            raise ValueError("a non-green scenario outcome requires a finding")
        return self


class ReplayLegIdentity(FrozenContractModel):
    """One frozen replay leg under one exact external freeze digest and role."""

    role: Literal["baseline", "treatment"]
    freezeDigest: str = Field(pattern=_DIGEST_PATTERN)


class PopulationGeneration(FrozenContractModel):
    """One immutable generation row in the three-view incident baseline."""

    generation: int = Field(ge=1)
    stratum: ReplayStratum
    sourceIdentity: SemanticText = Field(max_length=4096)

    @model_validator(mode="after")
    def _require_stratum_generation(self) -> Self:
        if self.stratum == "frozen-original" and self.generation > 8:
            raise ValueError("frozen-original stratum is generations 1-8 only")
        if self.stratum == "post-analysis-tail" and not 9 <= self.generation <= 13:
            raise ValueError("post-analysis-tail stratum is generations 9-13 only")
        if self.stratum == "incident-baseline" and not 1 <= self.generation <= 13:
            raise ValueError("incident-baseline stratum is generations 1-13 only")
        if self.stratum == "dated-supplement" and self.generation < 14:
            raise ValueError("dated supplements begin at generation 14")
        return self


class SpanCategoryTotals(FrozenContractModel):
    """Union wall and active time for exactly one measured span category."""

    category: MeasuredSpanCategory
    wallMillis: int = Field(ge=0)
    activeMillis: int = Field(ge=0)
    spanCount: int = Field(ge=0)

    @model_validator(mode="after")
    def _require_active_within_wall(self) -> Self:
        if self.activeMillis > self.wallMillis:
            raise ValueError("category active time cannot exceed its union wall")
        return self


class SpanReduction(FrozenContractModel):
    """Deterministic per-category span reduction with no double counting."""

    schemaVersion: Literal["measured-replay-span-reduction/v1"] = (
        "measured-replay-span-reduction/v1"
    )
    categories: tuple[SpanCategoryTotals, ...] = Field(min_length=9, max_length=9)
    grossWallMillis: int = Field(ge=0)
    grossActiveMillis: int = Field(ge=0)
    spanCount: int = Field(ge=0)
    reductionDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_reduction(self) -> Self:
        observed = tuple(item.category for item in self.categories)
        if len(set(observed)) != len(observed):
            raise ValueError("span categories must be unique")
        # The tuple is length-fixed at the closed vocabulary size and every member
        # is a distinct validated TelemetrySpanKind, so coverage of the closed set
        # follows from uniqueness alone; a separate set-equality check would be dead.
        if self.spanCount != sum(item.spanCount for item in self.categories):
            raise ValueError("reduction span count must equal the per-category sum")
        payload = self.model_dump(mode="json", exclude={"reductionDigest"})
        if self.reductionDigest != content_digest(payload):
            raise ValueError("span reduction digest does not match its content")
        return self


class ReplayFreezeInputChange(FrozenContractModel):
    """One typed frozen-dimension change invalidating an affected comparison pair."""

    changeClass: Literal[
        "source",
        "profile",
        "plan",
        "configuration",
        "population",
        "runtime-toolchain-executor-image",
        "machine-class",
        "instrumentation",
        "measurement-schema",
        "fault-injection",
    ]
    reason: SemanticText = Field(max_length=2048)


class GateRunMeasurement(FrozenContractModel):
    """Measured gate facts reduced from one R16 event export."""

    gate: GateId
    started: bool = False
    startedCount: int = Field(default=0, ge=0)
    catalogCount: int = Field(default=0, ge=0)
    lastCatalogDisposition: Literal["green", "red", "not-applicable", "none"] = "none"
    lastCatalog: tuple[CatalogRailRecord, ...] = Field(default_factory=tuple)
    lastCatalogCounts: CatalogCounts | None = None
    decision: Literal[
        "pass-published",
        "pass-reused",
        "fail",
        "certificate-refused",
        "blocked",
        "none",
    ] = "none"
    blocked: bool = False
    zeroStartEvidence: bool = False
    invalidated: bool = False
    railStartedCount: int = Field(default=0, ge=0)
    railTerminalCount: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _require_measurement_shape(self) -> Self:
        if not self.started and self.startedCount:
            raise ValueError("gate started count requires a started gate")
        if self.blocked and self.started:
            raise ValueError("a blocked gate never started")
        if self.zeroStartEvidence and self.started:
            raise ValueError("zero-start evidence cannot accompany a started gate")
        if self.lastCatalogDisposition == "none" and self.lastCatalogCounts is not None:
            raise ValueError("catalog counts require a catalog disposition")
        if self.lastCatalog and self.lastCatalogDisposition == "none":
            raise ValueError("a complete catalog requires a disposition")
        return self

    @property
    def failedRails(self) -> tuple[RailIdentity, ...]:
        return tuple(
            record.rail
            for record in self.lastCatalog
            if record.status == "fail" and record.posture == "enforcing"
        )

    @property
    def blockedRails(self) -> tuple[RailIdentity, ...]:
        return tuple(
            record.rail
            for record in self.lastCatalog
            if record.status == "blocked" and record.posture == "enforcing"
        )

    @property
    def terminalRails(self) -> tuple[RailIdentity, ...]:
        return tuple(record.rail for record in self.lastCatalog if record.posture == "enforcing")


class RunMeasurement(FrozenContractModel):
    """Deterministic measured reduction of one R16 closeout event export."""

    schemaVersion: Literal["measured-replay-run/v1"] = "measured-replay-run/v1"
    executionId: SemanticText = Field(max_length=512)
    leg: ReplayLegIdentity
    admitted: bool = False
    admissionRefused: bool = False
    gates: tuple[GateRunMeasurement, ...] = Field(min_length=5, max_length=5)
    spans: SpanReduction
    finalizationStarted: bool = False
    finalizationResumed: bool = False
    finalizationCompleted: bool = False
    operationTerminalClass: str | None = None
    certificatePublishCount: int = Field(default=0, ge=0)
    certificateReuseCount: int = Field(default=0, ge=0)
    certificateInvalidationCount: int = Field(default=0, ge=0)
    measurementDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_run_measurement(self) -> Self:
        if tuple(item.gate for item in self.gates) != (1, 2, 3, 4, 5):
            raise ValueError("run measurement gates must be the exact ordered Gates 1-5")
        if self.admitted and self.admissionRefused:
            raise ValueError("a run cannot be both admitted and refused")
        payload = self.model_dump(mode="json", exclude={"measurementDigest"})
        if self.measurementDigest != content_digest(payload):
            raise ValueError("run measurement digest does not match its content")
        return self

    def gate(self, gate: GateId) -> GateRunMeasurement:
        return self.gates[gate - 1]


__all__ = [
    "CatalogCounts",
    "CatalogRailRecord",
    "GateRunMeasurement",
    "MeasuredSpanCategory",
    "PopulationGeneration",
    "ProfileReference",
    "ReplayDependencyEdge",
    "ReplayFreezeInputChange",
    "ReplayLegIdentity",
    "ReplayProfileSnapshot",
    "ReplayRailPlacement",
    "ReplayScenarioEvidence",
    "ReplayScenarioExpectation",
    "ReplayScenarioId",
    "ReplayStratum",
    "RunMeasurement",
    "ScenarioOutcome",
    "ScenarioState",
    "SpanCategoryTotals",
    "SpanReduction",
]


class ReplayRailPlacement(FrozenContractModel):
    """One profile-declared rail placement used to interpret a scenario."""

    gate: GateId
    rail: RailIdentity
    railClass: Literal[
        "pre-test-quality",
        "ordinary-test-suite",
        "post-test-quality",
        "integration-test",
        "memory-quality",
    ]
    posture: Literal["enforcing", "report-only"] = "enforcing"

    @model_validator(mode="after")
    def _require_class_gate_contract(self) -> Self:
        expected = {
            "pre-test-quality": 1,
            "ordinary-test-suite": 2,
            "post-test-quality": 3,
            "integration-test": 4,
            "memory-quality": 5,
        }[self.railClass]
        if self.gate != expected:
            raise ValueError(
                f"railClass {self.railClass!r} requires Gate {expected}, not Gate {self.gate}"
            )
        return self


class ReplayDependencyEdge(FrozenContractModel):
    """One prerequisite edge: dependant cannot run when its prerequisite fails."""

    prerequisite: RailIdentity
    dependant: RailIdentity


class ReplayProfileSnapshot(FrozenContractModel):
    """One repository fixture profile observed during a measured replay."""

    repositoryId: str = Field(pattern=_ID_PATTERN, max_length=128)
    toolIdentity: SemanticText = Field(max_length=512)
    frameworkContract: SemanticText = Field(max_length=512)
    placements: tuple[ReplayRailPlacement, ...] = Field(default_factory=tuple)

    def placements_for_gate(self, gate: GateId) -> tuple[ReplayRailPlacement, ...]:
        return tuple(item for item in self.placements if item.gate == gate)


class ReplayScenarioEvidence(FrozenContractModel):
    """The measured evidence one acceptance scenario is evaluated against."""

    treatment: RunMeasurement
    baseline: RunMeasurement | None = None
    railPlacements: tuple[ReplayRailPlacement, ...] = Field(default_factory=tuple)
    peerPlacements: tuple[tuple[ReplayRailPlacement, ...], ...] = Field(default_factory=tuple)
    profiles: tuple[ReplayProfileSnapshot, ...] = Field(default_factory=tuple)
    faultRails: tuple[RailIdentity, ...] = Field(default_factory=tuple)
    companionRails: tuple[RailIdentity, ...] = Field(default_factory=tuple)
    offenderRails: tuple[RailIdentity, ...] = Field(default_factory=tuple)
    dependencyEdges: tuple[ReplayDependencyEdge, ...] = Field(default_factory=tuple)
    changeClasses: tuple[SemanticText, ...] = Field(default_factory=tuple)

    def placements_for_gate(self, gate: GateId) -> tuple[ReplayRailPlacement, ...]:
        return tuple(item for item in self.railPlacements if item.gate == gate)

    def placements_for_rail(self, rail: RailIdentity) -> tuple[ReplayRailPlacement, ...]:
        return tuple(item for item in self.railPlacements if item.rail == rail)

    def baseline_placements(self) -> tuple[ReplayRailPlacement, ...]:
        if self.peerPlacements:
            return self.peerPlacements[0]
        return ()
