"""CCR-R16-v3 durable telemetry event-schema edge contracts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from agents_remember.certification.certificate_invalidation import CertificateInputChange
from agents_remember.certification.certificate_models import GateCertificateIdentity
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CandidateIdentity,
    CertificationContractFinding,
    GatePlan,
    RailResult,
)
from agents_remember.certification.telemetry import (
    EVENT_MATRIX,
    AdmissionRefusedPayload,
    AdmissionStartedPayload,
    CatalogCounts,
    CatalogRailRecord,
    CertificateInvalidatedPayload,
    CertificateRefusedPayload,
    DiagnosticStartedPayload,
    ExecutionDispositionPayload,
    GateBlockedPayload,
    GateCatalogCompletePayload,
    GatePassPayload,
    GateStartedPayload,
    OperationTerminalPayload,
    PredecessorBoundary,
    R21DependencyDecision,
    RailStartedPayload,
    RailTerminalPayload,
    TelemetryEvent,
    TelemetrySpan,
    aggregate_span_totals,
    catalog_manifest_digest,
)
from agents_remember.certification.telemetry.adapters import (
    TelemetryExecutionContext,
    compile_admission_refused,
    compile_admission_started,
    compile_certificate_refused,
    compile_diagnostic_started,
    compile_diagnostic_terminal,
    compile_execution_disposition,
    compile_gate_blocked,
    compile_operation_terminal,
    compile_rail_started,
    compile_rail_terminal,
    span,
)
from agents_remember.certification.telemetry.models import (
    CERTIFICATE_REFUSAL_CODES,
    TERMINAL_RESULT_CLASSES,
    EventKind,
    GateCitation,
    event_matrix_cell,
    rail_terminal_class,
)
from agents_remember.models.certification.base import GateId, RailIdentity

_DIGEST = "a" * 64
_CANDIDATE = CandidateIdentity(kind="content-digest", value="c" * 64)

_CLOSEOUT = "closeout-generation"
_DIAGNOSTIC = "diagnostic-run"
_TIMESTAMP = "2026-09-03T00:00:00+02:00"
_PROFILE = "portable-ci"


def _ctx(
    *,
    revision: int,
    kind: str = _CLOSEOUT,
    execution_id: str = "gen-13-closeout",
) -> TelemetryExecutionContext:
    return TelemetryExecutionContext(
        executionKind=kind,  # type: ignore[arg-type]
        executionId=execution_id,
        eventRevision=revision,
        operationKind="closeout" if kind == _CLOSEOUT else None,
        generation=13 if kind == _CLOSEOUT else None,
        diagnosticNonce=None if kind == _CLOSEOUT else "a" * 32,
        candidate=_CANDIDATE,
        profileId=_PROFILE,
        occurredAt=_TIMESTAMP,
    )


def test_closeout_execution_identity_requires_operation_kind_and_generation() -> None:
    with pytest.raises(ValueError):
        compile_diagnostic_started(
            _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1").__class__(
                executionKind=_DIAGNOSTIC,
                executionId="diag-1",
                eventRevision=1,
                diagnosticNonce=None,
                candidate=_CANDIDATE,
                profileId=_PROFILE,
                occurredAt=_TIMESTAMP,
            ),
            plan_digest=_DIGEST,
            plan_version="scenario-2.1.0",
            rail_count=3,
        )
    event = compile_diagnostic_started(
        _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
        plan_digest=_DIGEST,
        plan_version="scenario-2.1.0",
        rail_count=3,
    )
    assert event.executionKind == _DIAGNOSTIC
    assert event.operationKind is None
    assert event.generation is None
    assert event.diagnosticNonce == "a" * 32


def test_diagnostic_run_can_never_acquire_certificate_authority() -> None:
    ctx = _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1")
    started = compile_diagnostic_started(
        ctx, plan_digest=_DIGEST, plan_version="scenario-2.1.0", rail_count=2
    )
    terminal = compile_diagnostic_terminal(ctx, result_id=_DIGEST, disposition="pass")
    events = [started, terminal]
    assert all(item.certificateDisposition == "not-applicable" for item in events)
    assert all(item.gateResultManifestId is None for item in events)
    with pytest.raises(ValueError):
        compile_operation_terminal(
            _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
            terminal_id=_DIGEST,
            terminal_result_class="success",
        )
    with pytest.raises(ValueError):
        compile_gate_blocked(
            _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
            gate=2,
            red_predecessor_gate=1,
        )


@pytest.mark.parametrize("event_kind", sorted(EventKind.__args__))
def test_every_matrix_row_is_bounded_and_id_rules_apply(event_kind: str) -> None:
    cell = EVENT_MATRIX[event_kind]
    assert cell.disposition in {
        "not-applicable",
        "pending",
        "published",
        "reused",
        "refused",
        "invalidated",
    }
    assert cell.certificateId in {"null", "required"}
    assert cell.manifestId in {"null", "required", "gate-result-only"}


@pytest.mark.parametrize("code", CERTIFICATE_REFUSAL_CODES)
def test_certificate_refusal_codes_are_closed(code: str) -> None:
    assert code in {
        "missing-input-digest",
        "unknown-dependency",
        "mismatched-predecessor",
        "stale-result",
        "malformed-result-manifest",
        "diagnostic-promotion-attempt",
        "profile-mismatch",
        "artifact-mismatch",
        "unclassified-input-change",
    }


@pytest.mark.parametrize("result_class", TERMINAL_RESULT_CLASSES)
def test_operation_terminal_manifest_cardinality(result_class: str) -> None:
    ctx = _ctx(revision=9)
    if result_class != "gate-result":
        terminal = compile_operation_terminal(
            ctx,
            terminal_id=_DIGEST,
            terminal_result_class=result_class,  # type: ignore[arg-type]
        )
        assert terminal.gateResultManifestId is None
        return
    with pytest.raises(ValueError):
        compile_operation_terminal(ctx, terminal_id=_DIGEST, terminal_result_class="gate-result")
    manifest = compile_operation_terminal(
        ctx,
        terminal_id=_DIGEST,
        terminal_result_class="gate-result",
        gate_result_manifest_id=_DIGEST,
    )
    assert manifest.gateResultManifestId == _DIGEST


def test_gate_pass_reuse_requires_prior_identity() -> None:
    prior = _DIGEST
    decision = R21DependencyDecision(
        reusedCertificateIds=(prior,),
        firstGateToRun=None,
        zeroGateStarts=True,
        finalizationRevalidationRequired=False,
        decisionDigest=content_digest(
            {
                "reusedCertificateIds": [prior],
                "firstGateToRun": None,
                "zeroGateStarts": True,
                "finalizationRevalidationRequired": False,
            }
        ),
    )
    with pytest.raises(ValueError):
        R21DependencyDecision(
            reusedCertificateIds=(prior, prior),
            firstGateToRun=None,
            zeroGateStarts=True,
            finalizationRevalidationRequired=False,
            decisionDigest=content_digest(
                {
                    "reusedCertificateIds": [prior, prior],
                    "firstGateToRun": None,
                    "zeroGateStarts": True,
                    "finalizationRevalidationRequired": False,
                }
            ),
        )
    with pytest.raises(ValueError):
        GatePassPayload(
            gate=1,
            attempt=1,
            mode="reused",
            catalogRevision=1,
            catalogManifestId=_DIGEST,
            dependencyDecision=decision,
        )
    GatePassPayload(
        gate=1,
        attempt=1,
        mode="reused",
        catalogRevision=1,
        catalogManifestId=_DIGEST,
        priorCertificateId=prior,
        dependencyDecision=decision,
    )


def test_predecessor_boundary_zero_start_is_exclusive() -> None:
    PredecessorBoundary(zeroStart=True)
    with pytest.raises(ValueError):
        PredecessorBoundary(zeroStart=True, priorGeneration=3)
    with pytest.raises(ValueError):
        PredecessorBoundary(zeroStart=False)
    PredecessorBoundary(priorGeneration=3, zeroStart=False)


def test_rail_started_gate_four_requires_repetition() -> None:
    rail = RailIdentity(railId="e2e-integration", version="1.0.0")
    with pytest.raises(ValueError):
        compile_rail_started(_ctx(revision=4), gate_plan=_gate_plan_stub(4), rail=rail, attempt=1)
    with pytest.raises(ValueError):
        compile_rail_started(
            _ctx(revision=4),
            gate_plan=_gate_plan_stub(1),
            rail=rail,
            attempt=1,
            repetition=2,
        )


def test_gate_blocked_requires_later_gate_and_red_predecessor() -> None:
    with pytest.raises(ValueError):
        GateBlockedPayload(gate=1, redPredecessorGate=1)
    with pytest.raises(ValueError):
        GateBlockedPayload(gate=3, redPredecessorGate=3)
    GateBlockedPayload(gate=3, redPredecessorGate=2)


def test_span_model_and_overlap_not_double_counted() -> None:
    with pytest.raises(ValueError):
        TelemetrySpan(
            spanKind="test-execution",
            startedAt=_TIMESTAMP,
            startedEpochMillis=100,
            wallMillis=100,
            activeMillis=150,
        )
    spans = (
        TelemetrySpan(
            spanKind="dagger-environment-setup",
            startedAt=_TIMESTAMP,
            startedEpochMillis=0,
            wallMillis=100,
            activeMillis=60,
        ),
        TelemetrySpan(
            spanKind="test-execution",
            startedAt=_TIMESTAMP,
            startedEpochMillis=50,
            wallMillis=100,
            activeMillis=80,
        ),
        TelemetrySpan(
            spanKind="post-test-scoring",
            startedAt=_TIMESTAMP,
            startedEpochMillis=200,
            wallMillis=50,
            activeMillis=50,
        ),
    )
    totals = aggregate_span_totals(spans)
    assert totals.grossWallMillis == 200  # 0-150 unioned, 200-250 appended
    assert totals.activeMillis == 190
    assert totals.spanCount == 3


def test_catalog_counts_and_manifest_digest_are_deterministic() -> None:
    rail = RailIdentity(railId="file-size-check", version="1.0.0")
    records = (
        CatalogRailRecord(rail=rail, resultId=_DIGEST, status="fail", posture="enforcing"),
        CatalogRailRecord(
            rail=RailIdentity(railId="report-only-scan", version="1.0.0"),
            resultId=_DIGEST,
            status="fail",
            posture="report-only",
        ),
        CatalogRailRecord(
            rail=RailIdentity(railId="style-scan", version="1.0.0"),
            resultId=_DIGEST,
            status="pass",
            posture="enforcing",
        ),
    )
    counts = CatalogCounts(passed=1, failed=1, blocked=0, notApplicable=0, reportOnly=1)
    with pytest.raises(ValueError):
        GateCatalogCompletePayload(
            gate=1,
            attempt=1,
            catalogRevision=1,
            disposition="red",
            railResults=records,
            counts=CatalogCounts(passed=0, failed=2, blocked=0, notApplicable=0, reportOnly=1),
            enforcingFailures=(
                CertificationContractFinding(code="file-size-check", path="rail", detail="fail"),
            ),
        )
    GateCatalogCompletePayload(
        gate=1,
        attempt=1,
        catalogRevision=1,
        disposition="red",
        railResults=records,
        counts=counts,
        enforcingFailures=(
            CertificationContractFinding(code="file-size-check", path="rail", detail="fail"),
        ),
    )
    first = catalog_manifest_digest(records, counts)
    assert first == catalog_manifest_digest(records, counts)
    assert len(first) == 64


def test_green_catalog_cannot_carry_enforcing_failures() -> None:
    rail = RailIdentity(railId="file-size-check", version="1.0.0")
    with pytest.raises(ValueError):
        GateCatalogCompletePayload(
            gate=1,
            attempt=1,
            catalogRevision=1,
            disposition="green",
            railResults=(
                CatalogRailRecord(rail=rail, resultId=_DIGEST, status="pass", posture="enforcing"),
            ),
            counts=CatalogCounts(passed=1, failed=0, blocked=0, notApplicable=0, reportOnly=0),
            enforcingFailures=(
                CertificationContractFinding(code="file-size-check", path="rail", detail="x"),
            ),
        )


def test_event_revision_must_be_positive() -> None:
    with pytest.raises(ValueError):
        compile_diagnostic_started(
            _ctx(revision=0),
            plan_digest=_DIGEST,
            plan_version="scenario-2.1.0",
            rail_count=1,
        )


def test_diagnostic_envelope_has_null_operation_kind_and_generation() -> None:
    event = compile_diagnostic_started(
        _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
        plan_digest=_DIGEST,
        plan_version="scenario-2.1.0",
        rail_count=1,
    )
    assert isinstance(event.payload, DiagnosticStartedPayload)
    assert event.payload.certifying is False


def test_admission_started_payload_binds_predecessor_boundary() -> None:
    event = compile_admission_started(
        _ctx(revision=1),
        predecessor=PredecessorBoundary(zeroStart=True),
    )
    assert isinstance(event.payload, AdmissionStartedPayload)
    assert event.payload.predecessorBoundary.zeroStart is True


def test_control_kinds_belong_to_either_execution_kind() -> None:
    event = compile_execution_disposition(
        _ctx(revision=2, kind=_DIAGNOSTIC, execution_id="diag-1"),
        event_kind="execution-interrupted",
        disposition=ExecutionDispositionPayload(
            cause="infrastructure stop",
            producer="worker",
            evidenceRef="evidence://stop",
            predecessorRevision=1,
        ),
    )
    assert event.executionKind == _DIAGNOSTIC
    assert event.certificateDisposition == "not-applicable"


def test_closeout_events_rejected_inside_diagnostic_run() -> None:
    with pytest.raises(ValueError):
        compile_gate_blocked(
            _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
            gate=2,
            red_predecessor_gate=1,
        )


def _gate_plan_stub(gate: int) -> GatePlan:
    return cast(
        "GatePlan",
        type("GatePlanStub", (), {"gate": gate, "planDigest": _DIGEST})(),
    )


def test_gate_event_requires_the_exact_gate_identity() -> None:
    event = compile_gate_blocked(_ctx(revision=2), gate=2, red_predecessor_gate=1)
    raw = event.model_dump(mode="python")
    raw["gate"] = None
    with pytest.raises(ValueError, match="requires the exact gate identity"):
        TelemetryEvent(**raw)


def test_non_gate_event_forbids_a_gate_identity() -> None:
    event = compile_diagnostic_started(
        _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
        plan_digest=_DIGEST,
        plan_version="scenario-2.1.0",
        rail_count=1,
    )
    raw = event.model_dump(mode="python")
    raw["gate"] = 1
    with pytest.raises(ValueError, match="only gate events may carry a gate identity"):
        TelemetryEvent(**raw)


def test_rail_event_requires_the_exact_rail_identity() -> None:
    rail = RailIdentity(railId="e2e-integration", version="1.0.0")
    event = compile_rail_started(
        _ctx(revision=3), gate_plan=_gate_plan_stub(1), rail=rail, attempt=1
    )
    raw = event.model_dump(mode="python")
    raw["rail"] = None
    with pytest.raises(ValueError, match="requires the exact rail identity"):
        TelemetryEvent(**raw)


def test_non_rail_event_forbids_a_rail_identity() -> None:
    event = compile_gate_blocked(_ctx(revision=2), gate=2, red_predecessor_gate=1)
    raw = event.model_dump(mode="python")
    raw["rail"] = RailIdentity(railId="e2e-integration", version="1.0.0").model_dump(mode="python")
    with pytest.raises(ValueError, match="only rail events may carry a rail identity"):
        TelemetryEvent(**raw)


def test_gate_payload_identity_must_agree_with_the_event_gate() -> None:
    event = compile_gate_blocked(_ctx(revision=2), gate=2, red_predecessor_gate=1)
    raw = event.model_dump(mode="python")
    raw["payload"]["gate"] = 3
    with pytest.raises(ValueError, match="contradicts its payload gate"):
        TelemetryEvent(**raw)


def test_rail_payload_identity_must_agree_with_the_event_rail() -> None:
    rail = RailIdentity(railId="e2e-integration", version="1.0.0")
    event = compile_rail_started(
        _ctx(revision=3), gate_plan=_gate_plan_stub(1), rail=rail, attempt=1
    )
    raw = event.model_dump(mode="python")
    raw["rail"] = RailIdentity(railId="e2e-integration", version="2.0.0").model_dump(mode="python")
    with pytest.raises(ValueError, match="contradicts its payload rail"):
        TelemetryEvent(**raw)


# ---------------------------------------------------------------------------
# Focused edge-contract closure tests (CCR-R16-v3 diff-coverage rail).
# ---------------------------------------------------------------------------


def _r21_decision(
    *,
    reused: tuple[str, ...] = (),
    first_gate: GateId | None = None,
    zero_gate_starts: bool = False,
    revalidation: bool = False,
) -> R21DependencyDecision:
    digest = content_digest(
        {
            "reusedCertificateIds": list(reused),
            "firstGateToRun": first_gate,
            "zeroGateStarts": zero_gate_starts,
            "finalizationRevalidationRequired": revalidation,
        }
    )
    return R21DependencyDecision(
        reusedCertificateIds=reused,
        firstGateToRun=first_gate,
        zeroGateStarts=zero_gate_starts,
        finalizationRevalidationRequired=revalidation,
        decisionDigest=digest,
    )


def _catalog_pass_record(rail_id: str = "file-size-check") -> CatalogRailRecord:
    return CatalogRailRecord(
        rail=RailIdentity(railId=rail_id, version="1.0.0"),
        resultId=_DIGEST,
        status="pass",
        posture="enforcing",
    )


def _catalog_fail_record(rail_id: str = "file-size-check") -> CatalogRailRecord:
    return CatalogRailRecord(
        rail=RailIdentity(railId=rail_id, version="1.0.0"),
        resultId=_DIGEST,
        status="fail",
        posture="enforcing",
    )


def _invalidation_change() -> CertificateInputChange:
    return CertificateInputChange(changeClass="code", reason="source changed")


def _invalidated_payload(**overrides: object) -> CertificateInvalidatedPayload:
    base = {
        "gate": 2,
        "attempt": 1,
        "catalogRevision": 1,
        "catalogManifestId": _DIGEST,
        "priorCertificateId": _DIGEST,
        "invalidatedGates": (2,),
        "finalizationRevalidationRequired": False,
        "changes": (_invalidation_change(),),
    }
    base.update(overrides)
    return CertificateInvalidatedPayload(**base)


def _published_gate_pass_event() -> TelemetryEvent:
    return TelemetryEvent(
        executionKind=_CLOSEOUT,
        executionId="gen-13-closeout",
        eventRevision=2,
        operationKind="closeout",
        generation=13,
        eventKind="gate-pass",
        occurredAt=_TIMESTAMP,
        candidate=_CANDIDATE,
        profileId=_PROFILE,
        gate=1,
        certificateDisposition="published",
        certificateId=_DIGEST,
        gateResultManifestId=_DIGEST,
        payload=GatePassPayload(
            gate=1,
            attempt=1,
            mode="published",
            catalogRevision=1,
            catalogManifestId=_DIGEST,
            dependencyDecision=_r21_decision(),
        ),
    )


def _reused_gate_pass_event() -> TelemetryEvent:
    return TelemetryEvent(
        executionKind=_CLOSEOUT,
        executionId="gen-13-closeout",
        eventRevision=2,
        operationKind="closeout",
        generation=13,
        eventKind="gate-pass",
        occurredAt=_TIMESTAMP,
        candidate=_CANDIDATE,
        profileId=_PROFILE,
        gate=1,
        certificateDisposition="reused",
        certificateId=_DIGEST,
        gateResultManifestId=_DIGEST,
        payload=GatePassPayload(
            gate=1,
            attempt=1,
            mode="reused",
            catalogRevision=1,
            catalogManifestId=_DIGEST,
            priorCertificateId=_DIGEST,
            dependencyDecision=_r21_decision(reused=(_DIGEST,), zero_gate_starts=True),
        ),
    )


def _green_catalog_event() -> TelemetryEvent:
    records = (_catalog_pass_record(),)
    counts = CatalogCounts(passed=1, failed=0, blocked=0, notApplicable=0, reportOnly=0)
    manifest = catalog_manifest_digest(records, counts)
    return TelemetryEvent(
        executionKind=_CLOSEOUT,
        executionId="gen-13-closeout",
        eventRevision=2,
        operationKind="closeout",
        generation=13,
        eventKind="gate-catalog-complete",
        occurredAt=_TIMESTAMP,
        candidate=_CANDIDATE,
        profileId=_PROFILE,
        gate=1,
        certificateDisposition="pending",
        gateResultManifestId=manifest,
        payload=GateCatalogCompletePayload(
            gate=1,
            attempt=1,
            catalogRevision=1,
            disposition="green",
            railResults=records,
            counts=counts,
        ),
    )


def test_span_union_covers_contained_and_extending_overlaps() -> None:
    spans = (
        TelemetrySpan(
            spanKind="test-execution",
            startedAt=_TIMESTAMP,
            startedEpochMillis=0,
            wallMillis=100,
            activeMillis=60,
        ),
        TelemetrySpan(
            spanKind="memory-work",
            startedAt=_TIMESTAMP,
            startedEpochMillis=50,
            wallMillis=100,
            activeMillis=80,
        ),
        TelemetrySpan(
            spanKind="waiting",
            startedAt=_TIMESTAMP,
            startedEpochMillis=120,
            wallMillis=10,
            activeMillis=10,
        ),
        TelemetrySpan(
            spanKind="repair",
            startedAt=_TIMESTAMP,
            startedEpochMillis=200,
            wallMillis=50,
            activeMillis=50,
        ),
    )
    totals = aggregate_span_totals(spans)
    assert totals.grossWallMillis == 200  # 0-150 unioned, contained span adds nothing, 200-250
    assert totals.activeMillis == 200
    assert totals.spanCount == 4
    empty = aggregate_span_totals(())
    assert empty.grossWallMillis == 0
    assert empty.activeMillis == 0
    assert empty.spanCount == 0


def test_rail_terminal_class_maps_not_applicable_status() -> None:
    record = CatalogRailRecord(
        rail=RailIdentity(railId="not-run", version="1.0.0"),
        resultId=_DIGEST,
        status="not-applicable",
        posture="enforcing",
    )
    assert rail_terminal_class(record) == "not-applicable"


def test_r21_zero_gate_start_decision_must_declare_itself() -> None:
    with pytest.raises(ValueError, match="must declare zeroGateStarts"):
        R21DependencyDecision(
            reusedCertificateIds=(_DIGEST,),
            firstGateToRun=None,
            zeroGateStarts=False,
            finalizationRevalidationRequired=False,
            decisionDigest=content_digest(
                {
                    "reusedCertificateIds": [_DIGEST],
                    "firstGateToRun": None,
                    "zeroGateStarts": False,
                    "finalizationRevalidationRequired": False,
                }
            ),
        )
    decision = _r21_decision(reused=(_DIGEST,), zero_gate_starts=True)
    assert decision.zeroGateStarts is True


def test_r21_decision_digest_must_match_its_content() -> None:
    with pytest.raises(ValueError, match="does not match its content"):
        R21DependencyDecision(
            reusedCertificateIds=(),
            firstGateToRun=1,
            zeroGateStarts=False,
            finalizationRevalidationRequired=False,
            decisionDigest="b" * 64,
        )
    decision = _r21_decision(first_gate=1)
    assert decision.firstGateToRun == 1


def test_gate_started_requires_the_exact_green_prefix() -> None:
    def predecessor(gate: GateId) -> GateCertificateIdentity:
        return GateCertificateIdentity(gate=gate, certificateDigest=_DIGEST)

    GateStartedPayload(gate=1, attempt=1, greenPredecessors=())
    GateStartedPayload(gate=3, attempt=1, greenPredecessors=(predecessor(1), predecessor(2)))
    with pytest.raises(ValueError, match="exact green earlier prefix"):
        GateStartedPayload(gate=3, attempt=1, greenPredecessors=(predecessor(1),))
    with pytest.raises(ValueError, match="exact green earlier prefix"):
        GateStartedPayload(gate=2, attempt=1, greenPredecessors=())


def test_rail_started_repetition_count_must_be_positive() -> None:
    rail = RailIdentity(railId="e2e-integration", version="1.0.0")
    RailStartedPayload(gate=4, rail=rail, attempt=1, repetition=1)
    with pytest.raises(ValueError, match="repetition count must be positive"):
        RailStartedPayload(gate=4, rail=rail, attempt=1, repetition=0)


def test_catalog_counts_total_must_cover_every_record() -> None:
    records = (_catalog_pass_record(),)
    with pytest.raises(ValueError, match="must cover every ordered rail result"):
        GateCatalogCompletePayload(
            gate=1,
            attempt=1,
            catalogRevision=1,
            disposition="green",
            railResults=records,
            counts=CatalogCounts(passed=1, failed=1, blocked=0, notApplicable=0, reportOnly=0),
        )


def test_catalog_disposition_must_match_enforcing_results() -> None:
    records = (_catalog_fail_record(),)
    with pytest.raises(ValueError, match="does not match its enforcing rail results"):
        GateCatalogCompletePayload(
            gate=1,
            attempt=1,
            catalogRevision=1,
            disposition="green",
            railResults=records,
            counts=CatalogCounts(passed=0, failed=1, blocked=0, notApplicable=0, reportOnly=0),
        )


def test_red_catalog_requires_every_enforcing_failure() -> None:
    records = (_catalog_fail_record(),)
    with pytest.raises(ValueError, match="requires every enforcing failure"):
        GateCatalogCompletePayload(
            gate=1,
            attempt=1,
            catalogRevision=1,
            disposition="red",
            railResults=records,
            counts=CatalogCounts(passed=0, failed=1, blocked=0, notApplicable=0, reportOnly=0),
        )


def test_catalog_rail_result_identities_must_be_unique() -> None:
    with pytest.raises(ValueError, match="identities must be unique"):
        GateCatalogCompletePayload(
            gate=1,
            attempt=1,
            catalogRevision=1,
            disposition="green",
            railResults=(_catalog_pass_record(), _catalog_fail_record()),
            counts=CatalogCounts(passed=1, failed=1, blocked=0, notApplicable=0, reportOnly=0),
        )


def test_catalog_rail_results_must_be_canonically_ordered() -> None:
    with pytest.raises(ValueError, match="must be canonical ordered"):
        GateCatalogCompletePayload(
            gate=1,
            attempt=1,
            catalogRevision=1,
            disposition="green",
            railResults=(_catalog_pass_record("z-scan"), _catalog_pass_record("a-scan")),
            counts=CatalogCounts(passed=2, failed=0, blocked=0, notApplicable=0, reportOnly=0),
        )


def test_invalidation_closure_must_contain_the_gate() -> None:
    with pytest.raises(ValueError, match="must belong to its affected closure"):
        _invalidated_payload(invalidatedGates=(1,))


def test_invalidation_closure_must_be_an_ordered_unique_set() -> None:
    with pytest.raises(ValueError, match="ordered unique gate set"):
        _invalidated_payload(invalidatedGates=(3, 2))
    with pytest.raises(ValueError, match="ordered unique gate set"):
        _invalidated_payload(invalidatedGates=(2, 2))


def test_invalidation_affected_subrecords_must_be_unique_ordered() -> None:
    with pytest.raises(ValueError, match="unique and ordered"):
        _invalidated_payload(affectedGateFiveSubrecords=("b", "a"))
    with pytest.raises(ValueError, match="unique and ordered"):
        _invalidated_payload(affectedGateFiveSubrecords=("same", "same"))


def test_invalidation_requires_at_least_one_changed_input() -> None:
    with pytest.raises(ValueError, match="at least one changed typed input"):
        _invalidated_payload(changes=())
    assert _invalidated_payload().priorCertificateId == _DIGEST


def test_closeout_event_requires_operation_kind_and_generation() -> None:
    event = compile_gate_blocked(_ctx(revision=2), gate=2, red_predecessor_gate=1)
    raw = event.model_dump(mode="python")
    raw["operationKind"] = None
    with pytest.raises(ValueError, match="requires operation kind and public generation"):
        TelemetryEvent(**raw)
    raw = event.model_dump(mode="python")
    raw["generation"] = None
    with pytest.raises(ValueError, match="requires operation kind and public generation"):
        TelemetryEvent(**raw)


def test_closeout_event_forbids_a_diagnostic_nonce() -> None:
    event = compile_gate_blocked(_ctx(revision=2), gate=2, red_predecessor_gate=1)
    raw = event.model_dump(mode="python")
    raw["diagnosticNonce"] = "b" * 32
    with pytest.raises(ValueError, match="forbids a diagnostic nonce"):
        TelemetryEvent(**raw)


def test_diagnostic_run_carries_no_operation_kind_or_generation() -> None:
    event = compile_diagnostic_started(
        _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
        plan_digest=_DIGEST,
        plan_version="scenario-2.1.0",
        rail_count=1,
    )
    raw = event.model_dump(mode="python")
    raw["operationKind"] = "closeout"
    raw["generation"] = 13
    with pytest.raises(ValueError, match="carries no operation kind or generation"):
        TelemetryEvent(**raw)


def test_event_kind_requires_the_matching_payload_kind() -> None:
    event = compile_diagnostic_started(
        _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
        plan_digest=_DIGEST,
        plan_version="scenario-2.1.0",
        rail_count=1,
    )
    raw = event.model_dump(mode="python")
    raw["eventKind"] = "admission-started"
    with pytest.raises(ValueError, match="requires payload kind 'admission-started'"):
        TelemetryEvent(**raw)


def test_diagnostic_kinds_belong_only_to_diagnostic_runs() -> None:
    event = compile_diagnostic_started(
        _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
        plan_digest=_DIGEST,
        plan_version="scenario-2.1.0",
        rail_count=1,
    )
    raw = event.model_dump(mode="python")
    raw["executionKind"] = _CLOSEOUT
    raw["operationKind"] = "closeout"
    raw["generation"] = 13
    raw["diagnosticNonce"] = None
    with pytest.raises(ValueError, match="belong only to diagnostic runs"):
        TelemetryEvent(**raw)


def test_certificate_disposition_must_be_legal_for_the_kind() -> None:
    event = compile_gate_blocked(_ctx(revision=2), gate=2, red_predecessor_gate=1)
    raw = event.model_dump(mode="python")
    raw["certificateDisposition"] = "published"
    with pytest.raises(ValueError, match="requires a legal certificate disposition"):
        TelemetryEvent(**raw)


def test_null_certificate_cells_forbid_a_certificate_identity() -> None:
    event = compile_diagnostic_started(
        _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
        plan_digest=_DIGEST,
        plan_version="scenario-2.1.0",
        rail_count=1,
    )
    raw = event.model_dump(mode="python")
    raw["certificateId"] = _DIGEST
    with pytest.raises(ValueError, match="forbids a certificate identity"):
        TelemetryEvent(**raw)


def test_required_certificate_cells_demand_a_certificate_identity() -> None:
    raw = _published_gate_pass_event().model_dump(mode="python")
    raw["certificateId"] = None
    with pytest.raises(ValueError, match="requires a certificate identity"):
        TelemetryEvent(**raw)


def test_null_manifest_cells_forbid_a_manifest_identity() -> None:
    event = compile_diagnostic_started(
        _ctx(revision=1, kind=_DIAGNOSTIC, execution_id="diag-1"),
        plan_digest=_DIGEST,
        plan_version="scenario-2.1.0",
        rail_count=1,
    )
    raw = event.model_dump(mode="python")
    raw["gateResultManifestId"] = _DIGEST
    with pytest.raises(ValueError, match="forbids a gate result manifest identity"):
        TelemetryEvent(**raw)


def test_gate_event_payload_must_belong_to_the_gate_vocabulary() -> None:
    # Unreachable through full validation: _verify_kind_payload forces the
    # payload kind to equal the gate event kind, whose class is always in the
    # gate vocabulary.  White-box: model_construct skips validation.
    blocked = compile_gate_blocked(_ctx(revision=2), gate=2, red_predecessor_gate=1)
    raw = blocked.model_dump(mode="python")
    raw["payload"] = DiagnosticStartedPayload(
        planDigest=_DIGEST, planVersion="scenario-2.1.0", railCount=1
    )
    constructed = TelemetryEvent.model_construct(**raw)
    verify_shape = cast("Callable[[], Any]", constructed._verify_event_shape)
    with pytest.raises(ValueError, match="does not belong to the gate vocabulary"):
        verify_shape()


def test_catalog_complete_manifest_must_be_the_exact_digest() -> None:
    raw = _green_catalog_event().model_dump(mode="python")
    raw["gateResultManifestId"] = "b" * 64
    with pytest.raises(ValueError, match="must be the digest of the ordered terminal set"):
        TelemetryEvent(**raw)


def test_gate_decision_manifest_must_cite_the_catalog() -> None:
    raw = _published_gate_pass_event().model_dump(mode="python")
    raw["gateResultManifestId"] = "b" * 64
    with pytest.raises(ValueError, match="must cite the exact catalog manifest identity"):
        TelemetryEvent(**raw)


def test_gate_pass_mode_must_agree_with_the_disposition() -> None:
    raw = _published_gate_pass_event().model_dump(mode="python")
    raw["payload"]["mode"] = "reused"
    raw["payload"]["priorCertificateId"] = "b" * 64
    with pytest.raises(ValueError, match="mode and certificate disposition must agree"):
        TelemetryEvent(**raw)


def test_gate_pass_reuse_must_cite_the_prior_certificate() -> None:
    raw = _reused_gate_pass_event().model_dump(mode="python")
    raw["certificateId"] = "b" * 64
    with pytest.raises(ValueError, match="must cite the prior immutable certificate identity"):
        TelemetryEvent(**raw)


def test_diagnostic_rails_can_never_certify() -> None:
    # Unreachable through full validation: _verify_kind_payload first rejects a
    # rail-started closeout event inside a diagnostic-run envelope.  White-box:
    # model_construct skips validation before invoking the shape validator.
    rail = RailIdentity(railId="e2e-integration", version="1.0.0")
    event = compile_rail_started(
        _ctx(revision=3), gate_plan=_gate_plan_stub(1), rail=rail, attempt=1
    )
    raw = event.model_dump(mode="python")
    raw["executionKind"] = _DIAGNOSTIC
    raw["executionId"] = "diag-1"
    raw["operationKind"] = None
    raw["generation"] = None
    raw["diagnosticNonce"] = "b" * 32
    raw["payload"] = RailStartedPayload(**raw["payload"])
    constructed = TelemetryEvent.model_construct(**raw)
    verify_gate_four_shape = cast("Callable[[], Any]", constructed._verify_gate_four_shape)
    with pytest.raises(ValueError, match="diagnostic rails can never certify"):
        verify_gate_four_shape()


def test_gate_result_terminal_class_requires_the_manifest() -> None:
    # Unreachable through full validation: _verify_id_cardinality flags a
    # gate-result class with a null manifest first.  White-box as above.
    terminal = compile_operation_terminal(
        _ctx(revision=9), terminal_id=_DIGEST, terminal_result_class="success"
    )
    raw = terminal.model_dump(mode="python")
    raw["payload"] = OperationTerminalPayload(terminalId=_DIGEST, terminalResultClass="gate-result")
    constructed = TelemetryEvent.model_construct(**raw)
    verify_operation_terminal = cast("Callable[[], Any]", constructed._verify_operation_terminal)
    with pytest.raises(ValueError, match="requires the available manifest"):
        verify_operation_terminal()


def test_only_gate_result_terminal_may_carry_a_manifest() -> None:
    # Unreachable through full validation: _verify_id_cardinality flags a
    # non-gate-result class carrying a manifest first.  White-box as above.
    terminal = compile_operation_terminal(
        _ctx(revision=9),
        terminal_id=_DIGEST,
        terminal_result_class="gate-result",
        gate_result_manifest_id=_DIGEST,
    )
    raw = terminal.model_dump(mode="python")
    raw["payload"] = OperationTerminalPayload(terminalId=_DIGEST, terminalResultClass="success")
    constructed = TelemetryEvent.model_construct(**raw)
    verify_operation_terminal = cast("Callable[[], Any]", constructed._verify_operation_terminal)
    with pytest.raises(ValueError, match="only the gate-result terminal class may carry"):
        verify_operation_terminal()


def test_event_matrix_cell_rejects_unknown_kinds() -> None:
    cell = event_matrix_cell("gate-pass")
    assert cell.certificateId == "required"
    with pytest.raises(ValueError, match="not part of the exhaustive event matrix"):
        event_matrix_cell("not-a-kind")


def _rail_result(status: str, posture: str, rail: RailIdentity) -> RailResult:
    payload = {
        "schemaVersion": "closeout-rail-result/v1",
        "rail": {"railId": rail.railId, "version": rail.version},
        "gate": 1,
        "gatePlanDigest": _DIGEST,
        "posture": posture,
        "status": status,
        "code": f"result-{status}",
        "blockedBy": [],
        "correctiveOwner": "portable-owner",
        "artifacts": [],
        "evidence": [],
    }
    return RailResult(**payload, resultDigest=content_digest(payload))


def test_compile_admission_refused_binds_the_finding() -> None:
    event = compile_admission_refused(
        _ctx(revision=2),
        refusal_code="stale-diagnostic",
        finding=CertificationContractFinding(
            code="stale-diagnostic",
            path="payload",
            detail="the diagnostic snapshot was superseded",
        ),
    )
    assert event.eventKind == "admission-refused"
    assert event.certificateDisposition == "not-applicable"
    assert isinstance(event.payload, AdmissionRefusedPayload)
    assert event.payload.finding.code == "stale-diagnostic"


def test_compile_certificate_refused_binds_the_refusal() -> None:
    event = compile_certificate_refused(
        _ctx(revision=5),
        gate=2,
        citation=GateCitation(
            attempt=1,
            catalog_revision=1,
            catalog_manifest_id=_DIGEST,
        ),
        refusal_code="artifact-mismatch",  # type: ignore[arg-type]
        refusal_detail="the artifact digest did not match the manifest",
    )
    assert event.eventKind == "certificate-refused"
    assert event.certificateDisposition == "refused"
    assert isinstance(event.payload, CertificateRefusedPayload)
    assert event.payload.refusalCode == "artifact-mismatch"


def test_span_helper_builds_a_bounded_span() -> None:
    built = span(
        span_kind="test-execution",
        started_at=_TIMESTAMP,
        started_epoch_millis=1000,
        wall_millis=42,
        active_millis=30,
    )
    assert built.spanKind == "test-execution"
    assert built.wallMillis == 42
    assert built.activeMillis == 30


def test_rail_terminal_maps_not_applicable_results() -> None:
    rail = RailIdentity(railId="e2e-integration", version="1.0.0")
    result = _rail_result("not-applicable", "enforcing", rail)
    event = compile_rail_terminal(
        _ctx(revision=3),
        gate_plan=_gate_plan_stub(1),
        result=result,
        attempt=1,
    )
    assert isinstance(event.payload, RailTerminalPayload)
    assert event.payload.disposition == "not-applicable"


def test_rail_terminal_maps_report_only_results() -> None:
    rail = RailIdentity(railId="lint", version="1.0.0")
    result = _rail_result("fail", "report-only", rail)
    event = compile_rail_terminal(
        _ctx(revision=4),
        gate_plan=_gate_plan_stub(1),
        result=result,
        attempt=1,
    )
    assert isinstance(event.payload, RailTerminalPayload)
    assert event.payload.disposition == "report-only"


def test_compile_requires_the_exact_candidate_identity() -> None:
    ctx = TelemetryExecutionContext(
        executionKind=_CLOSEOUT,
        executionId="gen-13-closeout",
        eventRevision=1,
        operationKind="closeout",
        generation=13,
        candidate=None,
        profileId=_PROFILE,
        occurredAt=_TIMESTAMP,
    )
    with pytest.raises(ValueError, match="require the exact candidate identity"):
        compile_gate_blocked(ctx, gate=2, red_predecessor_gate=1)


def test_compile_requires_the_profile_identity() -> None:
    ctx = TelemetryExecutionContext(
        executionKind=_CLOSEOUT,
        executionId="gen-13-closeout",
        eventRevision=1,
        operationKind="closeout",
        generation=13,
        candidate=_CANDIDATE,
        profileId=None,
        occurredAt=_TIMESTAMP,
    )
    with pytest.raises(ValueError, match="require the R22 profile identity"):
        compile_gate_blocked(ctx, gate=2, red_predecessor_gate=1)
