"""Boundary and Gate 1-5 projections reconstructed durably, without rerunning.

The projection folds only the durable journal events to expose the operation's
admission/finalization boundaries, per-gate and per-rail history, certificate
history, diagnostics, operation terminal, and span totals for the
journal/status/wait/dashboard surfaces with the same identities the events
carry.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.telemetry.models import (
    AdmissionRefusedPayload,
    CandidateAdmittedPayload,
    CertificateDisposition,
    CertificateInvalidatedPayload,
    CertificateRefusedPayload,
    DiagnosticStartedPayload,
    DiagnosticTerminalPayload,
    FinalizationCompletedPayload,
    FinalizationStartedPayload,
    GateBlockedPayload,
    GateCatalogCompletePayload,
    GateFailPayload,
    GatePassPayload,
    GateStartedPayload,
    OperationTerminalPayload,
    RailStartedPayload,
    RailTerminalPayload,
    SpanTotals,
    TelemetryEvent,
    TelemetryExecutionKind,
    TelemetrySpan,
    TerminalResultClass,
    aggregate_span_totals,
)
from agents_remember.models.certification.base import (
    FrozenContractModel,
    GateId,
    RailIdentity,
    SemanticText,
)
from agents_remember.models.lifecycles.operation_kinds import LifecycleOperationKind

GateProjectionState = Literal[
    "not-started",
    "blocked",
    "started",
    "catalog-green",
    "catalog-red",
    "passed",
    "reused",
    "failed",
    "certificate-refused",
    "invalidated",
]
AdmissionProjectionState = Literal["not-started", "started", "refused", "admitted"]
FinalizationProjectionState = Literal[
    "not-started",
    "started",
    "boundary-resumed",
    "completed",
]
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"


class RailTelemetryProjection(FrozenContractModel):
    """Per-attempt rail history folded from rail-started / rail-terminal events."""

    rail: RailIdentity
    attempt: int = Field(ge=1)
    startedRevision: int = Field(ge=1)
    startedAt: SemanticText = Field(max_length=128)
    terminalRevision: int | None = None
    resultId: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    disposition: str | None = Field(default=None, max_length=64)


class GateTelemetryProjection(FrozenContractModel):
    """One gate's complete durable history: plan, rails, catalog, certificate."""

    gate: GateId
    gatePlanDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    state: GateProjectionState = "not-started"
    attempt: int | None = None
    blockedBy: tuple[SemanticText, ...] = Field(default_factory=tuple, max_length=16)
    catalogRevision: int | None = None
    gateResultManifestId: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    certificateDisposition: CertificateDisposition | None = None
    certificateId: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    rails: tuple[RailTelemetryProjection, ...] = Field(default_factory=tuple, max_length=4096)

    @model_validator(mode="after")
    def _require_state_shape(self) -> Self:
        if self.state == "blocked" and not self.blockedBy:
            raise ValueError("a blocked gate projection requires its exact red predecessor")
        if self.certificateDisposition is None and self.certificateId is not None:
            raise ValueError("a certificate identity requires its disposition")
        return self


class BoundaryTelemetryProjection(FrozenContractModel):
    """Admission and finalization boundaries for one execution."""

    admissionState: AdmissionProjectionState = "not-started"
    admissionRevision: int | None = None
    admissionManifestDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    refusalCode: str | None = Field(default=None, pattern=_ID_PATTERN, max_length=128)
    finalizationState: FinalizationProjectionState = "not-started"
    finalizationAuthorityDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    finalizationGateFiveCertificateId: str | None = Field(
        default=None,
        pattern=_DIGEST_PATTERN,
    )


class DiagnosticTelemetryProjection(FrozenContractModel):
    """One R13 diagnostic run bound to its R13 nonce."""

    diagnosticNonce: str = Field(pattern=r"^[0-9a-f]{16,128}$")
    startedRevision: int = Field(ge=1)
    startedAt: SemanticText = Field(max_length=128)
    planDigest: str = Field(pattern=_DIGEST_PATTERN)
    planVersion: SemanticText = Field(max_length=256)
    railCount: int = Field(ge=1)
    terminalRevision: int | None = None
    resultId: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    disposition: Literal["pass", "fail", "aborted"] | None = None


class OperationTerminalProjection(FrozenContractModel):
    """The closed R20 terminal result projection."""

    revision: int = Field(ge=1)
    terminalId: str = Field(pattern=_DIGEST_PATTERN)
    terminalResultClass: TerminalResultClass


class TelemetryProjection(FrozenContractModel):
    """The durable reconstruction of one execution's gate/rail/boundary history."""

    schemaVersion: Literal["closeout-telemetry-projection/v1"] = "closeout-telemetry-projection/v1"
    executionKind: TelemetryExecutionKind
    executionId: SemanticText = Field(max_length=512)
    operationKind: LifecycleOperationKind | None = None
    generation: int | None = None
    diagnosticNonce: str | None = Field(default=None, pattern=r"^[0-9a-f]{16,128}$")
    candidate: CandidateIdentity | None = None
    profileId: str | None = Field(default=None, pattern=_ID_PATTERN, max_length=128)
    lastRevision: int = Field(ge=1)
    boundary: BoundaryTelemetryProjection
    gates: tuple[GateTelemetryProjection, ...] = Field(min_length=5, max_length=5)
    diagnostics: tuple[DiagnosticTelemetryProjection, ...] = Field(
        default_factory=tuple,
        max_length=128,
    )
    operationTerminal: OperationTerminalProjection | None = None
    spans: SpanTotals
    projectionDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"projectionDigest"})
        if self.projectionDigest != content_digest(payload):
            raise ValueError("telemetry projection digest does not match its content")
        return self


_GATE_STATE_KINDS: frozenset[str] = frozenset(
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
_DIAGNOSTIC_STATE_KINDS: frozenset[str] = frozenset({"diagnostic-started", "diagnostic-terminal"})


def project_execution_telemetry(
    events: Sequence[TelemetryEvent],
) -> TelemetryProjection:
    """Fold the durable event stream into one lossless boundary/gate projection."""
    ordered = tuple(events)
    if not ordered:
        raise ValueError("execution telemetry projection requires at least one event")
    first = ordered[0]
    last = ordered[-1]
    boundary = BoundaryTelemetryProjection()
    gates = {gate: GateTelemetryProjection(gate=gate) for gate in (1, 2, 3, 4, 5)}
    rail_history: dict[int, list[RailTelemetryProjection]] = {gate: [] for gate in (1, 2, 3, 4, 5)}
    diagnostics: list[DiagnosticTelemetryProjection] = []
    spans: list[TelemetrySpan] = []
    terminal: OperationTerminalProjection | None = None
    for event in ordered:
        spans.extend(event.spans)
        boundary = _fold_boundary(event, boundary)
        if event.eventKind in _GATE_STATE_KINDS:
            _fold_gate_state(event, gates, rail_history)
        elif event.eventKind in _DIAGNOSTIC_STATE_KINDS:
            _fold_diagnostic_state(event, diagnostics)
        elif event.eventKind == "operation-terminal":
            terminal = _fold_operation_terminal(event)
    for gate, projections in rail_history.items():
        gates[gate] = _replace(gates[gate], rails=tuple(projections))
    ordered_gates = tuple(gates[index] for index in (1, 2, 3, 4, 5))
    draft = TelemetryProjection.model_construct(
        executionKind=first.executionKind,
        executionId=first.executionId,
        operationKind=first.operationKind,
        generation=first.generation,
        diagnosticNonce=first.diagnosticNonce,
        candidate=first.candidate,
        profileId=first.profileId,
        lastRevision=last.eventRevision,
        boundary=boundary,
        gates=ordered_gates,
        diagnostics=tuple(diagnostics),
        operationTerminal=terminal,
        spans=aggregate_span_totals(spans),
        projectionDigest=None,
    )
    digest = content_digest(draft.model_dump(mode="json", exclude={"projectionDigest"}))
    return TelemetryProjection.model_validate(
        {
            **draft.model_dump(mode="json", exclude={"projectionDigest"}),
            "projectionDigest": digest,
        }
    )


def _fold_gate_state(
    event: TelemetryEvent,
    gates: dict[int, GateTelemetryProjection],
    rail_history: dict[int, list[RailTelemetryProjection]],
) -> None:
    kind = event.eventKind
    if kind == "gate-started":
        _fold_gate_started(event, gates)
    elif kind == "rail-started":
        _fold_rail_started(event, rail_history)
    elif kind == "rail-terminal":
        _fold_rail_terminal(event, rail_history)
    elif kind == "gate-catalog-complete":
        _fold_catalog_complete(event, gates)
    elif kind == "gate-pass":
        _fold_gate_pass(event, gates)
    elif kind == "gate-fail":
        _fold_gate_fail(event, gates)
    elif kind == "certificate-refused":
        _fold_certificate_refused(event, gates)
    elif kind == "gate-blocked":
        _fold_gate_blocked(event, gates)
    elif kind == "certificate-invalidated":
        _fold_certificate_invalidated(event, gates)


def _fold_gate_started(
    event: TelemetryEvent,
    gates: dict[int, GateTelemetryProjection],
) -> None:
    payload = event.payload
    assert isinstance(payload, GateStartedPayload)
    gates[payload.gate] = _replace(
        gates[payload.gate],
        state="started",
        attempt=payload.attempt,
        gatePlanDigest=event.gatePlanDigest,
    )


def _fold_rail_started(
    event: TelemetryEvent,
    rail_history: dict[int, list[RailTelemetryProjection]],
) -> None:
    payload = event.payload
    assert isinstance(payload, RailStartedPayload)
    rail_history[payload.gate].append(
        RailTelemetryProjection(
            rail=payload.rail,
            attempt=payload.attempt,
            startedRevision=event.eventRevision,
            startedAt=event.occurredAt,
        )
    )


def _fold_rail_terminal(
    event: TelemetryEvent,
    rail_history: dict[int, list[RailTelemetryProjection]],
) -> None:
    payload = event.payload
    assert isinstance(payload, RailTerminalPayload)
    history = rail_history[payload.gate]
    for index in range(len(history) - 1, -1, -1):
        existing = history[index]
        if (
            existing.rail == payload.rail
            and existing.attempt == payload.attempt
            and existing.terminalRevision is None
        ):
            history[index] = _replace(
                existing,
                terminalRevision=event.eventRevision,
                resultId=payload.resultId,
                disposition=payload.disposition,
            )
            break


def _fold_catalog_complete(
    event: TelemetryEvent,
    gates: dict[int, GateTelemetryProjection],
) -> None:
    payload = event.payload
    assert isinstance(payload, GateCatalogCompletePayload)
    projection = gates[payload.gate]
    gates[payload.gate] = _replace(
        projection,
        state="catalog-green" if payload.disposition == "green" else "catalog-red",
        attempt=payload.attempt,
        catalogRevision=payload.catalogRevision,
        gateResultManifestId=event.gateResultManifestId,
        gatePlanDigest=event.gatePlanDigest,
    )


def _fold_gate_pass(
    event: TelemetryEvent,
    gates: dict[int, GateTelemetryProjection],
) -> None:
    payload = event.payload
    assert isinstance(payload, GatePassPayload)
    projection = gates[payload.gate]
    gates[payload.gate] = _replace(
        projection,
        state="passed" if payload.mode == "published" else "reused",
        attempt=payload.attempt,
        catalogRevision=payload.catalogRevision,
        gateResultManifestId=event.gateResultManifestId,
        certificateDisposition=event.certificateDisposition,
        certificateId=event.certificateId,
        gatePlanDigest=event.gatePlanDigest,
    )


def _fold_gate_fail(
    event: TelemetryEvent,
    gates: dict[int, GateTelemetryProjection],
) -> None:
    payload = event.payload
    assert isinstance(payload, GateFailPayload)
    projection = gates[payload.gate]
    gates[payload.gate] = _replace(
        projection,
        state="failed",
        attempt=payload.attempt,
        catalogRevision=payload.catalogRevision,
        gateResultManifestId=event.gateResultManifestId,
        gatePlanDigest=event.gatePlanDigest,
    )


def _fold_certificate_refused(
    event: TelemetryEvent,
    gates: dict[int, GateTelemetryProjection],
) -> None:
    payload = event.payload
    assert isinstance(payload, CertificateRefusedPayload)
    projection = gates[payload.gate]
    gates[payload.gate] = _replace(
        projection,
        state="certificate-refused",
        attempt=payload.attempt,
        catalogRevision=payload.catalogRevision,
        gateResultManifestId=event.gateResultManifestId,
        gatePlanDigest=event.gatePlanDigest,
    )


def _fold_gate_blocked(
    event: TelemetryEvent,
    gates: dict[int, GateTelemetryProjection],
) -> None:
    payload = event.payload
    assert isinstance(payload, GateBlockedPayload)
    gates[payload.gate] = _replace(
        gates[payload.gate],
        state="blocked",
        blockedBy=(str(payload.redPredecessorGate),),
        gatePlanDigest=None,
    )


def _fold_certificate_invalidated(
    event: TelemetryEvent,
    gates: dict[int, GateTelemetryProjection],
) -> None:
    payload = event.payload
    assert isinstance(payload, CertificateInvalidatedPayload)
    projection = gates[payload.gate]
    gates[payload.gate] = _replace(
        projection,
        state="invalidated",
        attempt=payload.attempt,
        catalogRevision=payload.catalogRevision,
        gateResultManifestId=event.gateResultManifestId,
        certificateDisposition=event.certificateDisposition,
        certificateId=event.certificateId,
        gatePlanDigest=event.gatePlanDigest,
    )


def _fold_diagnostic_state(
    event: TelemetryEvent,
    diagnostics: list[DiagnosticTelemetryProjection],
) -> None:
    if event.eventKind == "diagnostic-started":
        payload = event.payload
        assert isinstance(payload, DiagnosticStartedPayload)
        diagnostics.append(
            DiagnosticTelemetryProjection(
                diagnosticNonce=event.diagnosticNonce or "",
                startedRevision=event.eventRevision,
                startedAt=event.occurredAt,
                planDigest=payload.planDigest,
                planVersion=payload.planVersion,
                railCount=payload.railCount,
            )
        )
        return
    payload = event.payload
    assert isinstance(payload, DiagnosticTerminalPayload)
    if diagnostics:
        latest = diagnostics[-1]
        diagnostics[-1] = _replace(
            latest,
            terminalRevision=event.eventRevision,
            resultId=payload.resultId,
            disposition=payload.disposition,
        )


def _fold_operation_terminal(event: TelemetryEvent) -> OperationTerminalProjection:
    payload = event.payload
    assert isinstance(payload, OperationTerminalPayload)
    return OperationTerminalProjection(
        revision=event.eventRevision,
        terminalId=payload.terminalId,
        terminalResultClass=payload.terminalResultClass,
    )


def _fold_boundary(
    event: TelemetryEvent,
    boundary: BoundaryTelemetryProjection,
) -> BoundaryTelemetryProjection:
    if event.eventKind == "admission-started":
        boundary = _replace(
            boundary, admissionState="started", admissionRevision=event.eventRevision
        )
    elif event.eventKind == "admission-refused":
        payload = event.payload
        assert isinstance(payload, AdmissionRefusedPayload)
        boundary = _replace(
            boundary,
            admissionState="refused",
            admissionRevision=event.eventRevision,
            refusalCode=payload.refusalCode,
        )
    elif event.eventKind == "candidate-admitted":
        payload = event.payload
        assert isinstance(payload, CandidateAdmittedPayload)
        boundary = _replace(
            boundary,
            admissionState="admitted",
            admissionRevision=event.eventRevision,
            admissionManifestDigest=payload.admissionManifestDigest,
        )
    elif event.eventKind == "finalization-started":
        payload = event.payload
        assert isinstance(payload, FinalizationStartedPayload)
        boundary = _replace(
            boundary,
            finalizationState="started",
            finalizationAuthorityDigest=payload.authority.authorityDigest,
            finalizationGateFiveCertificateId=payload.gateFiveCertificateId,
        )
    elif event.eventKind == "finalization-boundary-resumed":
        boundary = _replace(boundary, finalizationState="boundary-resumed")
    elif event.eventKind == "finalization-completed":
        payload = event.payload
        assert isinstance(payload, FinalizationCompletedPayload)
        boundary = _replace(
            boundary,
            finalizationState="completed",
            finalizationAuthorityDigest=payload.authority.authorityDigest,
        )
    return boundary


def project_gate_history(events: Sequence[TelemetryEvent], gate: GateId) -> GateTelemetryProjection:
    """Project one exact gate's durable history without rerunning."""
    projection = project_execution_telemetry(events)
    return next(item for item in projection.gates if item.gate == gate)


def _replace[ModelT: FrozenContractModel](model: ModelT, **updates: object) -> ModelT:
    return model.model_copy(update=updates)


__all__ = [
    "AdmissionProjectionState",
    "BoundaryTelemetryProjection",
    "DiagnosticTelemetryProjection",
    "FinalizationProjectionState",
    "GateProjectionState",
    "GateTelemetryProjection",
    "OperationTerminalProjection",
    "RailTelemetryProjection",
    "TelemetryProjection",
    "project_execution_telemetry",
    "project_gate_history",
]
