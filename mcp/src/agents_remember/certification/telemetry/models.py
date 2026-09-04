"""CCR-R16-v3 durable boundary, gate, and rail telemetry event schema.

Every event carries executionKind, executionId, a monotonic
eventRevision, the candidate / R22 profile / applicable R11 plan, the
consumed runtime identity, timestamp, bounded evidence references, and the
always-present certificateDisposition / certificateId /
gateResultManifestId keys.  A closeout-generation event requires operation
kind and the public generation with a null diagnostic nonce; a diagnostic-run
event requires the R13 nonce and can never acquire gate, certificate, delivery,
approval, or finalization authority.  The exhaustive event matrix, certificate
refusal codes, and terminal result classes are closed vocabularies.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.certificate_invalidation import CertificateInputChange
from agents_remember.certification.certificate_models import GateCertificateIdentity
from agents_remember.certification.digests import content_digest
from agents_remember.certification.lifecycle_models import FinalizationLeg
from agents_remember.certification.models import (
    CandidateIdentity,
    CertificationContractFinding,
    FrozenContractModel,
    GateId,
    RailEvidenceReference,
    RailIdentity,
    RailPosture,
    RailStatus,
    SemanticText,
)
from agents_remember.models.lifecycles.operation_kinds import LifecycleOperationKind

TelemetryExecutionKind = Literal["closeout-generation", "diagnostic-run"]
CertificateDisposition = Literal[
    "not-applicable",
    "pending",
    "published",
    "reused",
    "refused",
    "invalidated",
]
CertificateRefusalCode = Literal[
    "missing-input-digest",
    "unknown-dependency",
    "mismatched-predecessor",
    "stale-result",
    "malformed-result-manifest",
    "diagnostic-promotion-attempt",
    "profile-mismatch",
    "artifact-mismatch",
    "unclassified-input-change",
]
TerminalResultClass = Literal[
    "gate-result",
    "admission-refusal",
    "finalization-failure",
    "success",
    "cancelled",
    "superseded",
    "worker-execution-unclassified",
    "terminal-rail-result-unavailable",
]
RailTerminalDisposition = Literal["pass", "fail", "blocked", "not-applicable", "report-only"]
TelemetrySpanKind = Literal[
    "dagger-environment-setup",
    "test-execution",
    "post-test-scoring",
    "clean-room-api-provider",
    "memory-work",
    "waiting",
    "repair",
    "operator-attention",
    "finalization",
]
PassFailAborted = Literal["pass", "fail", "aborted"]
EventKind = Literal[
    "admission-started",
    "admission-refused",
    "candidate-admitted",
    "gate-started",
    "rail-started",
    "rail-terminal",
    "gate-catalog-complete",
    "gate-pass",
    "gate-fail",
    "certificate-refused",
    "gate-blocked",
    "certificate-invalidated",
    "diagnostic-started",
    "diagnostic-terminal",
    "finalization-started",
    "finalization-boundary-resumed",
    "finalization-completed",
    "execution-cancelled",
    "execution-interrupted",
    "execution-recovered",
    "execution-superseded",
    "unknown-termination",
    "worker-exited",
    "operation-terminal",
]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_NONCE_PATTERN = r"^[0-9a-f]{16,128}$"

CONTROL_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "execution-cancelled",
        "execution-interrupted",
        "execution-recovered",
        "execution-superseded",
        "unknown-termination",
        "worker-exited",
    }
)
DIAGNOSTIC_ONLY_EVENT_KINDS: frozenset[str] = CONTROL_EVENT_KINDS | {
    "diagnostic-started",
    "diagnostic-terminal",
}
CLOSEOUT_EVENT_KINDS: frozenset[str] = frozenset(EventKind.__args__) - DIAGNOSTIC_ONLY_EVENT_KINDS  # type: ignore[attr-defined]
CERTIFICATE_REFUSAL_CODES: tuple[CertificateRefusalCode, ...] = (
    "missing-input-digest",
    "unknown-dependency",
    "mismatched-predecessor",
    "stale-result",
    "malformed-result-manifest",
    "diagnostic-promotion-attempt",
    "profile-mismatch",
    "artifact-mismatch",
    "unclassified-input-change",
)
TERMINAL_RESULT_CLASSES: tuple[TerminalResultClass, ...] = (
    "gate-result",
    "admission-refusal",
    "finalization-failure",
    "success",
    "cancelled",
    "superseded",
    "worker-execution-unclassified",
    "terminal-rail-result-unavailable",
)


@dataclass(frozen=True)
class MatrixCell:
    """One exhaustive-matrix row: disposition plus ID cardinality for an event kind."""

    disposition: CertificateDisposition
    certificateId: Literal["null", "required"]
    manifestId: Literal["null", "required", "gate-result-only"]


EVENT_MATRIX: dict[str, MatrixCell] = {
    "admission-started": MatrixCell("not-applicable", "null", "null"),
    "admission-refused": MatrixCell("not-applicable", "null", "null"),
    "candidate-admitted": MatrixCell("not-applicable", "null", "null"),
    "gate-started": MatrixCell("pending", "null", "null"),
    "rail-started": MatrixCell("pending", "null", "null"),
    "rail-terminal": MatrixCell("pending", "null", "null"),
    "gate-catalog-complete": MatrixCell("pending", "null", "required"),
    "gate-pass": MatrixCell("published", "required", "required"),
    "gate-fail": MatrixCell("refused", "null", "required"),
    "certificate-refused": MatrixCell("refused", "null", "required"),
    "gate-blocked": MatrixCell("not-applicable", "null", "null"),
    "certificate-invalidated": MatrixCell("invalidated", "required", "required"),
    "diagnostic-started": MatrixCell("not-applicable", "null", "null"),
    "diagnostic-terminal": MatrixCell("not-applicable", "null", "null"),
    "finalization-started": MatrixCell("not-applicable", "null", "null"),
    "finalization-boundary-resumed": MatrixCell("not-applicable", "null", "null"),
    "finalization-completed": MatrixCell("not-applicable", "null", "null"),
    "execution-cancelled": MatrixCell("not-applicable", "null", "null"),
    "execution-interrupted": MatrixCell("not-applicable", "null", "null"),
    "execution-recovered": MatrixCell("not-applicable", "null", "null"),
    "execution-superseded": MatrixCell("not-applicable", "null", "null"),
    "unknown-termination": MatrixCell("not-applicable", "null", "null"),
    "worker-exited": MatrixCell("not-applicable", "null", "null"),
    "operation-terminal": MatrixCell("not-applicable", "null", "gate-result-only"),
}


@dataclass(frozen=True)
class GateCitation:
    """The exact attempt / catalog revision / catalog manifest identity triple.

    Gate-pass, gate-fail, certificate-refused, and certificate-invalidated
    events all cite one earlier gate catalog through the same three scalars;
    bundling them keeps the compile adapters at the bounded argument surface.
    """

    attempt: int
    catalog_revision: int
    catalog_manifest_id: str


_GATE_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "gate-started",
        "rail-started",
        "rail-terminal",
        "gate-catalog-complete",
        "gate-pass",
        "gate-fail",
        "certificate-refused",
        "gate-blocked",
        "certificate-invalidated",
    }
)
_RAIL_EVENT_KINDS: frozenset[str] = frozenset({"rail-started", "rail-terminal"})


class TelemetrySpan(FrozenContractModel):
    """One separately timed span; Dagger is an executor span, never a gate."""

    spanKind: TelemetrySpanKind
    startedAt: SemanticText = Field(max_length=128)
    startedEpochMillis: int = Field(ge=0)
    wallMillis: int = Field(ge=0)
    activeMillis: int = Field(ge=0)
    evidence: tuple[RailEvidenceReference, ...] = Field(default_factory=tuple, max_length=128)

    @model_validator(mode="after")
    def _require_active_within_wall(self) -> Self:
        if self.activeMillis > self.wallMillis:
            raise ValueError("active span time cannot exceed wall span time")
        return self


class SpanTotals(FrozenContractModel):
    """Gross wall and active totals; overlap is never double-counted."""

    grossWallMillis: int = Field(ge=0)
    activeMillis: int = Field(ge=0)
    spanCount: int = Field(ge=0)


def aggregate_span_totals(spans: Sequence[TelemetrySpan]) -> SpanTotals:
    """Union overlapping wall intervals and sum active time independently."""
    ordered = sorted(spans, key=lambda span: span.startedEpochMillis)
    gross_wall = 0
    cursor: int | None = None
    for span in ordered:
        start = span.startedEpochMillis
        end = start + span.wallMillis
        if cursor is None or start > cursor:
            gross_wall += span.wallMillis
            cursor = end
        elif end > cursor:
            gross_wall += end - cursor
            cursor = end
    active = sum(span.activeMillis for span in ordered)
    return SpanTotals(
        grossWallMillis=gross_wall,
        activeMillis=active,
        spanCount=len(ordered),
    )


class ConsumedRuntimeIdentity(FrozenContractModel):
    """Consumed executor and runtime identity bound to one telemetry event."""

    adapterKind: str = Field(pattern=_ID_PATTERN, max_length=128)
    adapterId: str = Field(pattern=_ID_PATTERN, max_length=256)
    configurationDigest: str = Field(pattern=_DIGEST_PATTERN)
    runtimeIdentity: SemanticText | None = Field(default=None, max_length=512)


class PredecessorBoundary(FrozenContractModel):
    """The exact prior boundary an admission-started event carries."""

    priorAdmissionDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    priorGeneration: int | None = Field(default=None, ge=1)
    priorRedDispositionDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    zeroStart: bool = False

    @model_validator(mode="after")
    def _require_boundary_shape(self) -> Self:
        populated = any(
            (self.priorAdmissionDigest, self.priorGeneration, self.priorRedDispositionDigest)
        )
        if self.zeroStart and populated:
            raise ValueError("zero-start boundary cannot cite a prior admission or generation")
        if not self.zeroStart and not populated:
            raise ValueError("non-zero-start boundary requires a prior identity or red disposition")
        return self


class CatalogCounts(FrozenContractModel):
    """Complete status counts for one gate catalog, never first-failure-only."""

    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    notApplicable: int = Field(ge=0)
    reportOnly: int = Field(ge=0)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.blocked + self.notApplicable + self.reportOnly


class CatalogRailRecord(FrozenContractModel):
    """One ordered catalog row: planned rail, immutable result ID, and status."""

    rail: RailIdentity
    resultId: str = Field(pattern=_DIGEST_PATTERN)
    status: RailStatus
    posture: RailPosture


def rail_terminal_class(record: CatalogRailRecord) -> RailTerminalDisposition:
    """Map one catalog row to its telemetry terminal class."""
    if record.status == "not-applicable":
        return "not-applicable"
    if record.posture == "report-only":
        return "report-only"
    if record.status == "pass":
        return "pass"
    if record.status == "fail":
        return "fail"
    return "blocked"


def catalog_manifest_digest(
    records: Sequence[CatalogRailRecord],
    counts: CatalogCounts,
) -> str:
    """The digest of the ordered terminal set and counts (matrix-literal)."""
    payload = {
        "railResults": [
            {"rail": record.rail.key, "resultId": record.resultId} for record in records
        ],
        "counts": counts.model_dump(mode="json"),
    }
    return content_digest(payload)


class R21DependencyDecision(FrozenContractModel):
    """The current R21 reuse decision cited by a gate-pass event."""

    reusedCertificateIds: tuple[str, ...] = Field(default_factory=tuple, max_length=5)
    firstGateToRun: GateId | None = None
    zeroGateStarts: bool = False
    finalizationRevalidationRequired: bool = False
    decisionDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_decision(self) -> Self:
        if len({item for item in self.reusedCertificateIds}) != len(self.reusedCertificateIds):
            raise ValueError("reused certificate ids must be unique")
        if (
            self.firstGateToRun is None
            and self.zeroGateStarts is False
            and self.reusedCertificateIds
        ):
            raise ValueError("a zero-gate-start decision must declare zeroGateStarts")
        payload = self.model_dump(mode="json", exclude={"decisionDigest"})
        if self.decisionDigest != content_digest(payload):
            raise ValueError("R21 dependency decision digest does not match its content")
        return self


class FinalizationAuthorityRecord(FrozenContractModel):
    """One bounded authority record bound to finalization telemetry."""

    authorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    certificateIds: tuple[str, ...] = Field(min_length=5, max_length=5)
    candidatePairAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    taskIntentAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    journalAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)


class _PayloadKindBase(FrozenContractModel):
    """Every payload member declares its own closed kind discriminator."""

    kind: str


class AdmissionStartedPayload(_PayloadKindBase):
    kind: Literal["admission-started"] = "admission-started"
    predecessorBoundary: PredecessorBoundary


class AdmissionRefusedPayload(_PayloadKindBase):
    kind: Literal["admission-refused"] = "admission-refused"
    refusalCode: str = Field(pattern=_ID_PATTERN, max_length=128)
    finding: CertificationContractFinding
    gateStarts: Literal[0] = 0


class CandidateAdmittedPayload(_PayloadKindBase):
    kind: Literal["candidate-admitted"] = "candidate-admitted"
    admissionManifestDigest: str = Field(pattern=_DIGEST_PATTERN)
    certificationAdmissionDigest: str = Field(pattern=_DIGEST_PATTERN)
    gateOnePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    gateStarts: Literal[0] = 0


class GateStartedPayload(_PayloadKindBase):
    kind: Literal["gate-started"] = "gate-started"
    gate: GateId
    attempt: int = Field(ge=1)
    greenPredecessors: tuple[GateCertificateIdentity, ...] = Field(max_length=4)

    @model_validator(mode="after")
    def _require_exact_green_prefix(self) -> Self:
        if tuple(item.gate for item in self.greenPredecessors) != tuple(range(1, self.gate)):
            raise ValueError("gate-started predecessors must be the exact green earlier prefix")
        return self


class RailStartedPayload(_PayloadKindBase):
    kind: Literal["rail-started"] = "rail-started"
    gate: GateId
    rail: RailIdentity
    attempt: int = Field(ge=1)
    certifying: Literal[True] = True
    repetition: int | None = None

    @model_validator(mode="after")
    def _require_attempt_shape(self) -> Self:
        if (self.gate == 4) != (self.repetition is not None):
            raise ValueError("Gate-4 certifying E2E rails require exactly one repetition count")
        if self.repetition is not None and self.repetition < 1:
            raise ValueError("certifying E2E repetition count must be positive")
        return self


class RailTerminalPayload(_PayloadKindBase):
    kind: Literal["rail-terminal"] = "rail-terminal"
    gate: GateId
    rail: RailIdentity
    attempt: int = Field(ge=1)
    resultId: str = Field(pattern=_DIGEST_PATTERN)
    disposition: RailTerminalDisposition


class GateCatalogCompletePayload(_PayloadKindBase):
    kind: Literal["gate-catalog-complete"] = "gate-catalog-complete"
    gate: GateId
    attempt: int = Field(ge=1)
    catalogRevision: int = Field(ge=1)
    disposition: Literal["green", "red"]
    railResults: tuple[CatalogRailRecord, ...] = Field(min_length=1, max_length=4096)
    counts: CatalogCounts
    enforcingFailures: tuple[CertificationContractFinding, ...] = Field(
        default_factory=tuple,
        max_length=4096,
    )

    @model_validator(mode="after")
    def _require_catalog_shape(self) -> Self:
        _require_unique_ordered_records(self.railResults)
        if self.counts.total != len(self.railResults):
            raise ValueError("catalog counts must cover every ordered rail result")
        derived = _derived_counts(self.railResults)
        if derived != self.counts:
            raise ValueError("catalog counts do not match the complete ordered terminal set")
        red = any(
            item.posture == "enforcing" and item.status in {"fail", "blocked"}
            for item in self.railResults
        )
        if (self.disposition == "red") != red:
            raise ValueError("catalog disposition does not match its enforcing rail results")
        if red and not self.enforcingFailures:
            raise ValueError("complete red catalog requires every enforcing failure")
        if not red and self.enforcingFailures:
            raise ValueError("green catalog cannot carry enforcing failures")
        return self


def _derived_counts(records: Sequence[CatalogRailRecord]) -> CatalogCounts:
    totals = {cls: 0 for cls in ("pass", "fail", "blocked", "not-applicable", "report-only")}
    for record in records:
        totals[rail_terminal_class(record)] += 1
    return CatalogCounts(
        passed=totals["pass"],
        failed=totals["fail"],
        blocked=totals["blocked"],
        notApplicable=totals["not-applicable"],
        reportOnly=totals["report-only"],
    )


def _require_unique_ordered_records(records: Sequence[CatalogRailRecord]) -> None:
    keys = [record.rail.key for record in records]
    if len(keys) != len(set(keys)):
        raise ValueError("catalog rail result identities must be unique")
    if [item.rail.key for item in sorted(records, key=lambda item: item.rail.key)] != keys:
        raise ValueError("catalog rail results must be canonical ordered")


class GatePassPayload(_PayloadKindBase):
    kind: Literal["gate-pass"] = "gate-pass"
    gate: GateId
    attempt: int = Field(ge=1)
    mode: Literal["published", "reused"]
    catalogRevision: int = Field(ge=1)
    catalogManifestId: str = Field(pattern=_DIGEST_PATTERN)
    priorCertificateId: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    dependencyDecision: R21DependencyDecision

    @model_validator(mode="after")
    def _require_reuse_identity(self) -> Self:
        if (self.mode == "reused") != (self.priorCertificateId is not None):
            raise ValueError("exact reuse requires the prior immutable certificate identity")
        return self


class GateFailPayload(_PayloadKindBase):
    kind: Literal["gate-fail"] = "gate-fail"
    gate: GateId
    attempt: int = Field(ge=1)
    catalogRevision: int = Field(ge=1)
    catalogManifestId: str = Field(pattern=_DIGEST_PATTERN)
    stableCause: SemanticText = Field(max_length=4096)


class CertificateRefusedPayload(_PayloadKindBase):
    kind: Literal["certificate-refused"] = "certificate-refused"
    gate: GateId
    attempt: int = Field(ge=1)
    catalogRevision: int = Field(ge=1)
    catalogManifestId: str = Field(pattern=_DIGEST_PATTERN)
    refusalCode: CertificateRefusalCode
    refusalDetail: SemanticText = Field(max_length=4096)


class GateBlockedPayload(_PayloadKindBase):
    kind: Literal["gate-blocked"] = "gate-blocked"
    gate: GateId
    redPredecessorGate: GateId
    zeroGateStarts: Literal[True] = True
    gateStarts: Literal[0] = 0
    lifecycleAdmissionDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _require_later_gate(self) -> Self:
        if self.gate <= 1:
            raise ValueError("only a later Gate 2-5 can be blocked")
        if self.redPredecessorGate >= self.gate:
            raise ValueError("blocked gate must cite an exact earlier red predecessor")
        return self


class CertificateInvalidatedPayload(_PayloadKindBase):
    kind: Literal["certificate-invalidated"] = "certificate-invalidated"
    gate: GateId
    attempt: int = Field(ge=1)
    catalogRevision: int = Field(ge=1)
    catalogManifestId: str = Field(pattern=_DIGEST_PATTERN)
    priorCertificateId: str = Field(pattern=_DIGEST_PATTERN)
    invalidatedGates: tuple[GateId, ...]
    affectedGateFiveSubrecords: tuple[str, ...] = Field(default_factory=tuple, max_length=4096)
    finalizationRevalidationRequired: bool
    changes: tuple[CertificateInputChange, ...] = Field(default_factory=tuple, max_length=4096)

    @model_validator(mode="after")
    def _require_closure_shape(self) -> Self:
        if self.gate not in self.invalidatedGates:
            raise ValueError("invalidated gate must belong to its affected closure")
        if self.invalidatedGates != tuple(sorted(set(self.invalidatedGates))):
            raise ValueError("invalidation closure must be an ordered unique gate set")
        if self.affectedGateFiveSubrecords != tuple(sorted(set(self.affectedGateFiveSubrecords))):
            raise ValueError("affected Gate-5 subrecords must be unique and ordered")
        if not self.changes:
            raise ValueError("invalidation requires at least one changed typed input")
        return self


class DiagnosticStartedPayload(_PayloadKindBase):
    kind: Literal["diagnostic-started"] = "diagnostic-started"
    planDigest: str = Field(pattern=_DIGEST_PATTERN)
    planVersion: SemanticText = Field(max_length=256)
    railCount: int = Field(ge=1)
    certifying: Literal[False] = False


class DiagnosticTerminalPayload(_PayloadKindBase):
    kind: Literal["diagnostic-terminal"] = "diagnostic-terminal"
    resultId: str = Field(pattern=_DIGEST_PATTERN)
    disposition: PassFailAborted
    certifying: Literal[False] = False


class FinalizationStartedPayload(_PayloadKindBase):
    kind: Literal["finalization-started"] = "finalization-started"
    gateFiveCertificateId: str = Field(pattern=_DIGEST_PATTERN)
    authority: FinalizationAuthorityRecord


class FinalizationBoundaryResumedPayload(_PayloadKindBase):
    kind: Literal["finalization-boundary-resumed"] = "finalization-boundary-resumed"
    journalLeg: FinalizationLeg
    journalStateDigest: str = Field(pattern=_DIGEST_PATTERN)
    predecessorRevision: int = Field(ge=1)


class FinalizationCompletedPayload(_PayloadKindBase):
    kind: Literal["finalization-completed"] = "finalization-completed"
    authority: FinalizationAuthorityRecord
    finalizationDigest: str = Field(pattern=_DIGEST_PATTERN)


class ExecutionDispositionPayload(_PayloadKindBase):
    kind: Literal["execution-disposition"] = "execution-disposition"
    cause: SemanticText = Field(max_length=8192)
    producer: SemanticText = Field(max_length=512)
    evidenceRef: SemanticText = Field(max_length=16384)
    predecessorRevision: int = Field(ge=1)


class OperationTerminalPayload(_PayloadKindBase):
    kind: Literal["operation-terminal"] = "operation-terminal"
    terminalId: str = Field(pattern=_DIGEST_PATTERN)
    terminalResultClass: TerminalResultClass


TelemetryEventPayload = Annotated[
    AdmissionStartedPayload
    | AdmissionRefusedPayload
    | CandidateAdmittedPayload
    | GateStartedPayload
    | RailStartedPayload
    | RailTerminalPayload
    | GateCatalogCompletePayload
    | GatePassPayload
    | GateFailPayload
    | CertificateRefusedPayload
    | GateBlockedPayload
    | CertificateInvalidatedPayload
    | DiagnosticStartedPayload
    | DiagnosticTerminalPayload
    | FinalizationStartedPayload
    | FinalizationBoundaryResumedPayload
    | FinalizationCompletedPayload
    | ExecutionDispositionPayload
    | OperationTerminalPayload,
    Field(discriminator="kind"),
]

_EXECUTION_DISPOSITION_KINDS: frozenset[str] = frozenset(
    {
        "execution-cancelled",
        "execution-interrupted",
        "execution-recovered",
        "execution-superseded",
        "unknown-termination",
        "worker-exited",
    }
)


_GATE_EVENT_PAYLOAD_TYPES: tuple[type, ...] = (
    GateStartedPayload,
    RailStartedPayload,
    RailTerminalPayload,
    GateCatalogCompletePayload,
    GatePassPayload,
    GateFailPayload,
    CertificateRefusedPayload,
    GateBlockedPayload,
    CertificateInvalidatedPayload,
)


class TelemetryEvent(FrozenContractModel):
    """One immutable durable telemetry event under one execution-coherent identity."""

    schemaVersion: Literal["closeout-telemetry-event/v1"] = "closeout-telemetry-event/v1"
    executionKind: TelemetryExecutionKind
    executionId: SemanticText = Field(max_length=512)
    eventRevision: int = Field(ge=1)
    operationKind: LifecycleOperationKind | None = None
    generation: int | None = None
    diagnosticNonce: str | None = Field(default=None, pattern=_NONCE_PATTERN)
    eventKind: EventKind
    occurredAt: SemanticText = Field(max_length=128)
    candidate: CandidateIdentity
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    gatePlanDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    gate: GateId | None = None
    rail: RailIdentity | None = None
    runtime: ConsumedRuntimeIdentity | None = None
    evidence: tuple[RailEvidenceReference, ...] = Field(default_factory=tuple, max_length=128)
    spans: tuple[TelemetrySpan, ...] = Field(default_factory=tuple, max_length=256)
    certificateDisposition: CertificateDisposition
    certificateId: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    gateResultManifestId: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    message: SemanticText | None = Field(default=None, max_length=4096)
    payload: TelemetryEventPayload

    @model_validator(mode="after")
    def _verify_execution_identity(self) -> Self:
        if self.executionKind == "closeout-generation":
            if self.operationKind is None or self.generation is None or self.generation < 1:
                raise ValueError(
                    "closeout-generation telemetry requires operation kind and public generation"
                )
            if self.diagnosticNonce is not None:
                raise ValueError("closeout-generation telemetry forbids a diagnostic nonce")
        else:
            if self.operationKind is not None or self.generation is not None:
                raise ValueError("diagnostic-run telemetry carries no operation kind or generation")
            if self.diagnosticNonce is None:
                raise ValueError("diagnostic-run telemetry requires the R13 diagnostic nonce")
        return self

    @model_validator(mode="after")
    def _verify_kind_payload(self) -> Self:
        payload_kind = self.payload.kind
        if self.eventKind == "operation-terminal":
            expected: str | None = "operation-terminal"
        elif self.eventKind in _EXECUTION_DISPOSITION_KINDS:
            expected = "execution-disposition"
        else:
            expected = self.eventKind
        if payload_kind != expected:
            raise ValueError(
                f"eventKind {self.eventKind!r} requires payload kind {expected!r}, "
                f"not {payload_kind!r}"
            )
        if self.eventKind in DIAGNOSTIC_ONLY_EVENT_KINDS and self.executionKind != "diagnostic-run":
            raise ValueError("diagnostic and control events belong only to diagnostic runs")
        if self.eventKind in CLOSEOUT_EVENT_KINDS and self.executionKind != "closeout-generation":
            raise ValueError("closeout events cannot appear inside a diagnostic-run envelope")
        return self

    @model_validator(mode="after")
    def _verify_id_cardinality(self) -> Self:
        cell = EVENT_MATRIX[self.eventKind]
        if self.certificateDisposition not in dispositions_for_event_kind(self.eventKind):
            raise ValueError(
                f"eventKind {self.eventKind!r} requires a legal certificate disposition"
            )
        if cell.certificateId == "null" and self.certificateId is not None:
            raise ValueError(f"eventKind {self.eventKind!r} forbids a certificate identity")
        if cell.certificateId == "required" and self.certificateId is None:
            raise ValueError(f"eventKind {self.eventKind!r} requires a certificate identity")
        manifest = self._manifest_requirement(cell)
        if manifest == "null" and self.gateResultManifestId is not None:
            raise ValueError(
                f"eventKind {self.eventKind!r} forbids a gate result manifest identity"
            )
        if manifest == "required" and self.gateResultManifestId is None:
            raise ValueError(
                f"eventKind {self.eventKind!r} requires a gate result manifest identity"
            )
        return self

    def _manifest_requirement(self, cell: MatrixCell) -> Literal["null", "required"]:
        if cell.manifestId == "required":
            return "required"
        if cell.manifestId == "gate-result-only":
            payload = self.payload
            if isinstance(payload, OperationTerminalPayload) and (
                payload.terminalResultClass == "gate-result"
            ):
                return "required"
            return "null"
        return "null"

    @model_validator(mode="after")
    def _verify_event_shape(self) -> Self:
        self._require_exact_gate_identity()
        self._require_exact_rail_identity()
        self._require_gate_payload_shape()
        self._require_rail_payload_shape()
        return self

    def _require_exact_gate_identity(self) -> None:
        if (self.eventKind in _GATE_EVENT_KINDS) != (self.gate is not None):
            if self.gate is None:
                raise ValueError(f"eventKind {self.eventKind!r} requires the exact gate identity")
            raise ValueError("only gate events may carry a gate identity")

    def _require_exact_rail_identity(self) -> None:
        if (self.eventKind in _RAIL_EVENT_KINDS) != (self.rail is not None):
            if self.rail is None:
                raise ValueError(f"eventKind {self.eventKind!r} requires the exact rail identity")
            raise ValueError("only rail events may carry a rail identity")

    def _require_gate_payload_shape(self) -> None:
        if self.eventKind not in _GATE_EVENT_KINDS:
            return
        payload = self.payload
        if not isinstance(payload, _GATE_EVENT_PAYLOAD_TYPES):
            raise ValueError("gate event payload does not belong to the gate vocabulary")
        if getattr(payload, "gate", None) != self.gate:
            raise ValueError("gate event identity contradicts its payload gate")

    def _require_rail_payload_shape(self) -> None:
        if self.eventKind not in _RAIL_EVENT_KINDS:
            return
        payload = self.payload
        assert isinstance(payload, (RailStartedPayload, RailTerminalPayload))
        if payload.gate != self.gate or payload.rail != self.rail:
            raise ValueError("rail event identity contradicts its payload rail")

    @model_validator(mode="after")
    def _verify_catalog_payload_digest(self) -> Self:
        if self.eventKind == "gate-catalog-complete":
            payload = self.payload
            assert isinstance(payload, GateCatalogCompletePayload)
            expected = catalog_manifest_digest(payload.railResults, payload.counts)
            if self.gateResultManifestId != expected:
                raise ValueError(
                    "gateResultManifestId must be the digest of the ordered terminal set and counts"
                )
        if self.eventKind in {"gate-pass", "gate-fail", "certificate-refused"}:
            payload = self.payload
            if self.gateResultManifestId != payload.catalogManifestId:  # type: ignore[union-attr]
                raise ValueError("gate decision must cite the exact catalog manifest identity")
        if self.eventKind == "gate-pass":
            payload = self.payload
            assert isinstance(payload, GatePassPayload)
            if (payload.mode == "reused") != (self.certificateDisposition == "reused"):
                raise ValueError("gate-pass mode and certificate disposition must agree")
            if payload.mode == "reused" and self.certificateId != payload.priorCertificateId:
                raise ValueError("reuse must cite the prior immutable certificate identity")
        return self

    @model_validator(mode="after")
    def _verify_gate_four_shape(self) -> Self:
        if self.eventKind == "rail-started":
            payload = self.payload
            assert isinstance(payload, RailStartedPayload)
            if self.executionKind == "diagnostic-run" and payload.certifying is not False:
                raise ValueError("diagnostic rails can never certify")
        return self

    @model_validator(mode="after")
    def _verify_operation_terminal(self) -> Self:
        if self.eventKind == "operation-terminal":
            payload = self.payload
            assert isinstance(payload, OperationTerminalPayload)
            if payload.terminalResultClass == "gate-result" and self.gateResultManifestId is None:
                raise ValueError("gate-result terminal class requires the available manifest")
            if (
                payload.terminalResultClass != "gate-result"
                and self.gateResultManifestId is not None
            ):
                raise ValueError("only the gate-result terminal class may carry a manifest")
        return self


def dispositions_for_event_kind(event_kind: str) -> tuple[CertificateDisposition, ...]:
    """The exactly legal certificate dispositions for one event-kind row."""
    if event_kind == "gate-pass":
        return ("published", "reused")
    return (EVENT_MATRIX[event_kind].disposition,)


def event_matrix_cell(event_kind: str) -> MatrixCell:
    try:
        return EVENT_MATRIX[event_kind]
    except KeyError as error:
        raise ValueError(
            f"eventKind {event_kind!r} is not part of the exhaustive event matrix"
        ) from error


__all__ = [
    "CERTIFICATE_REFUSAL_CODES",
    "CLOSEOUT_EVENT_KINDS",
    "CONTROL_EVENT_KINDS",
    "DIAGNOSTIC_ONLY_EVENT_KINDS",
    "EVENT_MATRIX",
    "TERMINAL_RESULT_CLASSES",
    "AdmissionRefusedPayload",
    "AdmissionStartedPayload",
    "CandidateAdmittedPayload",
    "CatalogCounts",
    "CatalogRailRecord",
    "CertificateDisposition",
    "CertificateInvalidatedPayload",
    "CertificateRefusalCode",
    "CertificateRefusedPayload",
    "ConsumedRuntimeIdentity",
    "DiagnosticStartedPayload",
    "DiagnosticTerminalPayload",
    "EventKind",
    "ExecutionDispositionPayload",
    "FinalizationAuthorityRecord",
    "FinalizationBoundaryResumedPayload",
    "FinalizationCompletedPayload",
    "FinalizationStartedPayload",
    "GateBlockedPayload",
    "GateCatalogCompletePayload",
    "GateCitation",
    "GateFailPayload",
    "GatePassPayload",
    "GateStartedPayload",
    "MatrixCell",
    "OperationTerminalPayload",
    "PassFailAborted",
    "PredecessorBoundary",
    "R21DependencyDecision",
    "RailStartedPayload",
    "RailTerminalDisposition",
    "RailTerminalPayload",
    "SpanTotals",
    "TelemetryEvent",
    "TelemetryEventPayload",
    "TelemetryExecutionKind",
    "TelemetrySpan",
    "TelemetrySpanKind",
    "TerminalResultClass",
    "aggregate_span_totals",
    "catalog_manifest_digest",
    "dispositions_for_event_kind",
    "event_matrix_cell",
    "rail_terminal_class",
]
