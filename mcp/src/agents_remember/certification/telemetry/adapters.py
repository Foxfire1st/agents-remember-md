"""Adapters compiling telemetry events from the owner-produced R11/R20/R21/R22 objects.

Every adapter builds one immutable event whose certificate disposition,
certificate identity, and gate-result-manifest identity come from the closed
exhaustive-matrix table, so an adapter can never publish an authority it does
not own.  Catalog manifest identities are the digest of the ordered terminal
set and counts; rail results, gate certificates, admission manifests,
finalization authorities, reuse plans, and invalidation decisions are bound by
their exact content digests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    CertificateInvalidationDecision,
    CertificateReusePlan,
)
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    FinalizationCertificateAuthority,
    GateCertificate,
    GateCertificateIdentity,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.lifecycle_models import (
    FinalizationLeg,
    LifecycleAdmissionManifest,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    CertificationContractFinding,
    GateId,
    GatePlan,
    GateResultManifest,
    RailEvidenceReference,
    RailIdentity,
    RailResult,
)
from agents_remember.certification.telemetry.models import (
    EVENT_MATRIX,
    AdmissionRefusedPayload,
    AdmissionStartedPayload,
    CandidateAdmittedPayload,
    CatalogCounts,
    CatalogRailRecord,
    CertificateDisposition,
    CertificateInvalidatedPayload,
    CertificateRefusalCode,
    CertificateRefusedPayload,
    DiagnosticStartedPayload,
    DiagnosticTerminalPayload,
    EventKind,
    ExecutionDispositionPayload,
    FinalizationAuthorityRecord,
    FinalizationBoundaryResumedPayload,
    FinalizationCompletedPayload,
    FinalizationStartedPayload,
    GateBlockedPayload,
    GateCatalogCompletePayload,
    GateCitation,
    GateFailPayload,
    GatePassPayload,
    GateStartedPayload,
    OperationTerminalPayload,
    PassFailAborted,
    PredecessorBoundary,
    R21DependencyDecision,
    RailStartedPayload,
    RailTerminalDisposition,
    RailTerminalPayload,
    TelemetryEvent,
    TelemetryEventPayload,
    TelemetryExecutionKind,
    TelemetrySpan,
    TelemetrySpanKind,
    TerminalResultClass,
    catalog_manifest_digest,
    rail_terminal_class,
)
from agents_remember.models.lifecycles.operation_kinds import LifecycleOperationKind

_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_NONCE_PATTERN = r"^[0-9a-f]{16,128}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


@dataclass(frozen=True)
class TelemetryExecutionContext:
    """The execution-coherent identity every compiled event is bound to."""

    executionKind: TelemetryExecutionKind
    executionId: str
    eventRevision: int
    operationKind: LifecycleOperationKind | None = None
    generation: int | None = None
    diagnosticNonce: str | None = None
    candidate: CandidateIdentity | None = None
    profileId: str | None = None
    occurredAt: str = ""


@dataclass(frozen=True)
class _EventFields:
    """Optional event-identity fields a compile adapter may attach to its event."""

    gate: GateId | None = None
    gate_plan_digest: str | None = None
    rail: RailIdentity | None = None
    certificate_id: str | None = None
    manifest_id: str | None = None
    evidence: Sequence[RailEvidenceReference] = ()
    certificate_disposition: CertificateDisposition | None = None


_EMPTY_FIELDS = _EventFields()


def compile_admission_started(
    ctx: TelemetryExecutionContext,
    *,
    predecessor: PredecessorBoundary,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "admission-started",
        AdmissionStartedPayload(predecessorBoundary=predecessor),
    )


def compile_admission_refused(
    ctx: TelemetryExecutionContext,
    *,
    refusal_code: str,
    finding: CertificationContractFinding,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "admission-refused",
        AdmissionRefusedPayload(refusalCode=refusal_code, finding=finding),
    )


def compile_candidate_admitted(
    ctx: TelemetryExecutionContext,
    *,
    lifecycle_admission: LifecycleAdmissionManifest,
    certification_admission: CertificationAdmissionManifest,
    gate_one_plan: GatePlan,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "candidate-admitted",
        CandidateAdmittedPayload(
            admissionManifestDigest=lifecycle_admission.admissionDigest,
            certificationAdmissionDigest=certification_admission.admissionDigest,
            gateOnePlanDigest=gate_one_plan.planDigest,
        ),
    )


def compile_gate_started(
    ctx: TelemetryExecutionContext,
    *,
    gate_plan: GatePlan,
    attempt: int,
    green_predecessors: Sequence[GateCertificateIdentity],
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "gate-started",
        GateStartedPayload(
            gate=gate_plan.gate,
            attempt=attempt,
            greenPredecessors=tuple(green_predecessors),
        ),
        fields=_EventFields(gate=gate_plan.gate, gate_plan_digest=gate_plan.planDigest),
    )


def compile_rail_started(
    ctx: TelemetryExecutionContext,
    *,
    gate_plan: GatePlan,
    rail: RailIdentity,
    attempt: int,
    repetition: int | None = None,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "rail-started",
        RailStartedPayload(
            gate=gate_plan.gate,
            rail=rail,
            attempt=attempt,
            repetition=repetition,
        ),
        fields=_EventFields(
            gate=gate_plan.gate,
            gate_plan_digest=gate_plan.planDigest,
            rail=rail,
        ),
    )


def compile_rail_terminal(
    ctx: TelemetryExecutionContext,
    *,
    gate_plan: GatePlan,
    result: RailResult,
    attempt: int,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "rail-terminal",
        RailTerminalPayload(
            gate=gate_plan.gate,
            rail=result.rail,
            attempt=attempt,
            resultId=result.resultDigest,
            disposition=_terminal_disposition(result),
        ),
        fields=_EventFields(
            gate=gate_plan.gate,
            gate_plan_digest=gate_plan.planDigest,
            rail=result.rail,
            evidence=tuple(result.evidence),
        ),
    )


def compile_gate_catalog_complete(
    ctx: TelemetryExecutionContext,
    *,
    manifest: GateResultManifest,
    attempt: int,
    catalog_revision: int,
) -> TelemetryEvent:
    records = tuple(
        CatalogRailRecord(
            rail=result.rail,
            resultId=result.resultDigest,
            status=result.status,
            posture=result.posture,
        )
        for result in manifest.railResults
    )
    counts = _catalog_counts(records)
    enforcing_failures = tuple(
        _finding_from_result(item)
        for item in manifest.railResults
        if item.posture == "enforcing" and item.status in {"fail", "blocked"}
    )
    payload = GateCatalogCompletePayload(
        gate=manifest.gate,
        attempt=attempt,
        catalogRevision=catalog_revision,
        disposition=manifest.disposition,
        railResults=records,
        counts=counts,
        enforcingFailures=enforcing_failures,
    )
    return _base_event(
        ctx,
        "gate-catalog-complete",
        payload,
        fields=_EventFields(
            gate=manifest.gate,
            gate_plan_digest=manifest.gatePlanDigest,
            manifest_id=catalog_manifest_digest(records, counts),
        ),
    )


def compile_gate_pass_published(
    ctx: TelemetryExecutionContext,
    *,
    certificate: GateCertificate,
    citation: GateCitation,
    dependency_decision: R21DependencyDecision,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "gate-pass",
        GatePassPayload(
            gate=certificate.semanticEnvelope.gate,
            attempt=citation.attempt,
            mode="published",
            catalogRevision=citation.catalog_revision,
            catalogManifestId=citation.catalog_manifest_id,
            dependencyDecision=dependency_decision,
        ),
        fields=_EventFields(
            gate=certificate.semanticEnvelope.gate,
            certificate_id=certificate.certificateDigest,
            manifest_id=citation.catalog_manifest_id,
        ),
    )


def compile_gate_pass_reused(
    ctx: TelemetryExecutionContext,
    *,
    prior_certificate: GateCertificate,
    citation: GateCitation,
    dependency_decision: R21DependencyDecision,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "gate-pass",
        GatePassPayload(
            gate=prior_certificate.semanticEnvelope.gate,
            attempt=citation.attempt,
            mode="reused",
            catalogRevision=citation.catalog_revision,
            catalogManifestId=citation.catalog_manifest_id,
            priorCertificateId=prior_certificate.certificateDigest,
            dependencyDecision=dependency_decision,
        ),
        fields=_EventFields(
            gate=prior_certificate.semanticEnvelope.gate,
            certificate_id=prior_certificate.certificateDigest,
            manifest_id=citation.catalog_manifest_id,
            certificate_disposition="reused",
        ),
    )


def compile_gate_fail(
    ctx: TelemetryExecutionContext,
    *,
    manifest: GateResultManifest,
    citation: GateCitation,
    stable_cause: str,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "gate-fail",
        GateFailPayload(
            gate=manifest.gate,
            attempt=citation.attempt,
            catalogRevision=citation.catalog_revision,
            catalogManifestId=citation.catalog_manifest_id,
            stableCause=stable_cause,
        ),
        fields=_EventFields(
            gate=manifest.gate,
            manifest_id=citation.catalog_manifest_id,
        ),
    )


def compile_certificate_refused(
    ctx: TelemetryExecutionContext,
    *,
    gate: GateId,
    citation: GateCitation,
    refusal_code: CertificateRefusalCode,
    refusal_detail: str,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "certificate-refused",
        CertificateRefusedPayload(
            gate=gate,
            attempt=citation.attempt,
            catalogRevision=citation.catalog_revision,
            catalogManifestId=citation.catalog_manifest_id,
            refusalCode=refusal_code,
            refusalDetail=refusal_detail,
        ),
        fields=_EventFields(
            gate=gate,
            manifest_id=citation.catalog_manifest_id,
        ),
    )


def compile_gate_blocked(
    ctx: TelemetryExecutionContext,
    *,
    gate: GateId,
    red_predecessor_gate: GateId,
    lifecycle_admission_digest: str | None = None,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "gate-blocked",
        GateBlockedPayload(
            gate=gate,
            redPredecessorGate=red_predecessor_gate,
            lifecycleAdmissionDigest=lifecycle_admission_digest,
        ),
        fields=_EventFields(gate=gate),
    )


def compile_certificate_invalidated(
    ctx: TelemetryExecutionContext,
    *,
    certificate: GateCertificate,
    citation: GateCitation,
    decision: CertificateInvalidationDecision,
    changes: Sequence[CertificateInputChange],
) -> TelemetryEvent:
    payload = CertificateInvalidatedPayload(
        gate=certificate.semanticEnvelope.gate,
        attempt=citation.attempt,
        catalogRevision=citation.catalog_revision,
        catalogManifestId=citation.catalog_manifest_id,
        priorCertificateId=certificate.certificateDigest,
        invalidatedGates=decision.invalidatedGates,
        affectedGateFiveSubrecords=decision.affectedGateFiveSubrecords,
        finalizationRevalidationRequired=decision.finalizationRevalidationRequired,
        changes=tuple(changes),
    )
    return _base_event(
        ctx,
        "certificate-invalidated",
        payload,
        fields=_EventFields(
            gate=certificate.semanticEnvelope.gate,
            certificate_id=certificate.certificateDigest,
            manifest_id=citation.catalog_manifest_id,
        ),
    )


def compile_diagnostic_started(
    ctx: TelemetryExecutionContext,
    *,
    plan_digest: str,
    plan_version: str,
    rail_count: int,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "diagnostic-started",
        DiagnosticStartedPayload(
            planDigest=plan_digest,
            planVersion=plan_version,
            railCount=rail_count,
        ),
    )


def compile_diagnostic_terminal(
    ctx: TelemetryExecutionContext,
    *,
    result_id: str,
    disposition: PassFailAborted,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "diagnostic-terminal",
        DiagnosticTerminalPayload(resultId=result_id, disposition=disposition),
    )


def compile_finalization_started(
    ctx: TelemetryExecutionContext,
    *,
    gate_five_certificate: GateCertificate,
    authority: FinalizationCertificateAuthority,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "finalization-started",
        FinalizationStartedPayload(
            gateFiveCertificateId=gate_five_certificate.certificateDigest,
            authority=_authority_record(authority),
        ),
    )


def compile_finalization_boundary_resumed(
    ctx: TelemetryExecutionContext,
    *,
    journal_leg: FinalizationLeg,
    journal_state_digest: str,
    predecessor_revision: int,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "finalization-boundary-resumed",
        FinalizationBoundaryResumedPayload(
            journalLeg=journal_leg,
            journalStateDigest=journal_state_digest,
            predecessorRevision=predecessor_revision,
        ),
    )


def compile_finalization_completed(
    ctx: TelemetryExecutionContext,
    *,
    authority: FinalizationCertificateAuthority,
    finalization_digest: str,
) -> TelemetryEvent:
    return _base_event(
        ctx,
        "finalization-completed",
        FinalizationCompletedPayload(
            authority=_authority_record(authority),
            finalizationDigest=finalization_digest,
        ),
    )


def compile_execution_disposition(
    ctx: TelemetryExecutionContext,
    *,
    event_kind: Literal[
        "execution-cancelled",
        "execution-interrupted",
        "execution-recovered",
        "execution-superseded",
        "unknown-termination",
        "worker-exited",
    ],
    disposition: ExecutionDispositionPayload,
) -> TelemetryEvent:
    return _base_event(ctx, event_kind, disposition)


def compile_operation_terminal(
    ctx: TelemetryExecutionContext,
    *,
    terminal_id: str,
    terminal_result_class: TerminalResultClass,
    gate_result_manifest_id: str | None = None,
) -> TelemetryEvent:
    manifest = gate_result_manifest_id if terminal_result_class == "gate-result" else None
    return _base_event(
        ctx,
        "operation-terminal",
        OperationTerminalPayload(
            terminalId=terminal_id,
            terminalResultClass=terminal_result_class,
        ),
        fields=_EventFields(manifest_id=manifest),
    )


def compile_reuse_dependency_decision(reuse: CertificateReusePlan) -> R21DependencyDecision:
    """Freeze the exact current R21 reuse decision for a gate-pass event."""
    reused_ids = tuple(item.certificateDigest for item in reuse.reusedCertificates)
    payload = {
        "reusedCertificateIds": reused_ids,
        "firstGateToRun": reuse.firstGateToRun,
        "zeroGateStarts": reuse.zeroGateStarts,
        "finalizationRevalidationRequired": reuse.finalizationRevalidationRequired,
    }
    return R21DependencyDecision(
        reusedCertificateIds=reused_ids,
        firstGateToRun=reuse.firstGateToRun,
        zeroGateStarts=reuse.zeroGateStarts,
        finalizationRevalidationRequired=reuse.finalizationRevalidationRequired,
        decisionDigest=content_digest(payload),
    )


def span(
    *,
    span_kind: TelemetrySpanKind,
    started_at: str,
    started_epoch_millis: int,
    wall_millis: int,
    active_millis: int,
) -> TelemetrySpan:
    return TelemetrySpan(
        spanKind=span_kind,
        startedAt=started_at,
        startedEpochMillis=started_epoch_millis,
        wallMillis=wall_millis,
        activeMillis=active_millis,
    )


def _base_event(
    ctx: TelemetryExecutionContext,
    event_kind: str,
    payload: object,
    fields: _EventFields = _EMPTY_FIELDS,
) -> TelemetryEvent:
    cell = EVENT_MATRIX[event_kind]
    return TelemetryEvent(
        executionKind=ctx.executionKind,
        executionId=ctx.executionId,
        eventRevision=ctx.eventRevision,
        operationKind=ctx.operationKind,
        generation=ctx.generation,
        diagnosticNonce=ctx.diagnosticNonce,
        eventKind=cast("EventKind", event_kind),
        occurredAt=ctx.occurredAt,
        candidate=_required_candidate(ctx),
        profileId=_required_profile(ctx),
        gatePlanDigest=fields.gate_plan_digest,
        gate=fields.gate,
        rail=fields.rail,
        evidence=tuple(fields.evidence),
        certificateDisposition=fields.certificate_disposition or cell.disposition,
        certificateId=fields.certificate_id,
        gateResultManifestId=fields.manifest_id,
        payload=cast("TelemetryEventPayload", payload),
    )


def _authority_record(authority: FinalizationCertificateAuthority) -> FinalizationAuthorityRecord:
    envelope = authority.semanticEnvelope
    return FinalizationAuthorityRecord(
        authorityDigest=authority.authorityDigest,
        certificateIds=tuple(item.certificateDigest for item in envelope.certificates),
        candidatePairAuthorityDigest=envelope.candidatePairAuthorityDigest,
        taskIntentAuthorityDigest=envelope.taskIntentAuthorityDigest,
        journalAuthorityDigest=envelope.journalAuthorityDigest,
    )


def _catalog_counts(records: Sequence[CatalogRailRecord]) -> CatalogCounts:
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


def _terminal_disposition(result: RailResult) -> RailTerminalDisposition:
    if result.status == "not-applicable":
        return "not-applicable"
    if result.posture == "report-only":
        return "report-only"
    if result.status == "pass":
        return "pass"
    if result.status == "fail":
        return "fail"
    return "blocked"


def _finding_from_result(result: RailResult) -> CertificationContractFinding:
    rail = result.rail
    return CertificationContractFinding(
        code=f"{rail.railId}-{result.status}",
        path=f"railResults.{rail.key}.status",
        detail=f"enforcing rail terminalized {result.status}: {result.code}",
    )


def _required_candidate(ctx: TelemetryExecutionContext) -> CandidateIdentity:
    if ctx.candidate is None:
        raise ValueError("telemetry events require the exact candidate identity")
    return ctx.candidate


def _required_profile(ctx: TelemetryExecutionContext) -> str:
    if ctx.profileId is None:
        raise ValueError("telemetry events require the R22 profile identity")
    return ctx.profileId


__all__ = [
    "TelemetryExecutionContext",
    "compile_admission_refused",
    "compile_admission_started",
    "compile_candidate_admitted",
    "compile_certificate_invalidated",
    "compile_certificate_refused",
    "compile_diagnostic_started",
    "compile_diagnostic_terminal",
    "compile_execution_disposition",
    "compile_finalization_boundary_resumed",
    "compile_finalization_completed",
    "compile_finalization_started",
    "compile_gate_blocked",
    "compile_gate_catalog_complete",
    "compile_gate_fail",
    "compile_gate_pass_published",
    "compile_gate_pass_reused",
    "compile_gate_started",
    "compile_operation_terminal",
    "compile_rail_started",
    "compile_rail_terminal",
    "compile_reuse_dependency_decision",
    "span",
]
