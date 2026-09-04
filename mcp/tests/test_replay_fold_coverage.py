"""Fully standalone CCR-R17 reducer fold and span-interval coverage.

Every R16 closeout event kind must reach its reducer fold branch, and the span
wall-union must take its fully-contained and extending-overlap arcs.  Nothing
is shared with another suite; events are constructed here.
"""

from __future__ import annotations

from typing import Any

from agents_remember.certification.models import RailIdentity
from agents_remember.certification.replay.measure import measure_replay_run
from agents_remember.certification.replay.models import (
    ReplayLegIdentity,
    RunMeasurement,
)
from agents_remember.certification.replay.spans import (
    analyze_span_categories,
    category_wall_union_millis,
    gross_wall_union_millis,
)
from agents_remember.certification.telemetry.models import (
    CatalogCounts,
    CatalogRailRecord,
    GateCatalogCompletePayload,
    GatePassPayload,
    OperationTerminalPayload,
    TelemetryEvent,
    TelemetrySpan,
)

_DIGEST = "a" * 64
_GIT = "c" * 40


def _span(kind: str, start: int, wall: int, active: int) -> TelemetrySpan:
    return TelemetrySpan(
        spanKind=kind,  # type: ignore[arg-type]
        startedAt="2026-09-03T00:00:00+02:00",
        startedEpochMillis=start,
        wallMillis=wall,
        activeMillis=active,
    )


def _event(revision: int, kind: str, **fields: Any) -> TelemetryEvent:
    base: dict[str, Any] = {
        "schemaVersion": "closeout-telemetry-event/v1",
        "executionKind": "closeout-generation",
        "executionId": "fold-run",
        "eventRevision": revision,
        "operationKind": "closeout",
        "generation": 13,
        "diagnosticNonce": None,
        "eventKind": kind,
        "occurredAt": "2026-09-03T00:00:00+02:00",
        "candidate": {"kind": "git-tree", "value": _GIT},
        "profileId": "portable-ci",
        "gatePlanDigest": _DIGEST,
        "gate": fields.pop("gate", None),
        "rail": fields.pop("rail", None),
        "runtime": None,
        "evidence": (),
        "spans": fields.pop("spans", ()),
        "certificateDisposition": fields.pop("certificateDisposition", "not-applicable"),
        "certificateId": fields.pop("certificateId", None),
        "gateResultManifestId": fields.pop("gateResultManifestId", None),
        "message": None,
        **fields,
    }
    return TelemetryEvent.model_construct(**base)


def _payload(**fields: Any) -> dict[str, Any]:
    return fields


def _leg() -> ReplayLegIdentity:
    return ReplayLegIdentity(role="treatment", freezeDigest=_DIGEST)


def test_wall_union_handles_contained_and_extending_overlaps() -> None:
    spans = [
        _span("waiting", 0, 100, 10),
        _span("waiting", 20, 30, 5),
        _span("waiting", 80, 60, 6),
        _span("repair", 150, 20, 4),
    ]
    assert gross_wall_union_millis(spans) == 160
    assert category_wall_union_millis(spans, "waiting") == 140
    reduction = analyze_span_categories(spans)
    assert reduction.grossWallMillis == 160
    waiting = next(item for item in reduction.categories if item.category == "waiting")
    assert waiting.wallMillis == 140
    assert waiting.activeMillis == 21
    assert waiting.spanCount == 3


def test_wall_union_touching_intervals_do_not_double_count() -> None:
    spans = [
        _span("waiting", 0, 100, 1),
        _span("waiting", 100, 50, 1),
    ]
    assert gross_wall_union_millis(spans) == 150


def test_wall_union_single_interval() -> None:
    spans = [_span("waiting", 10, 40, 4)]
    assert gross_wall_union_millis(spans) == 40


def test_reducer_folds_candidate_admitted_flag() -> None:
    event = _event(
        1,
        "candidate-admitted",
        payload=_payload(
            kind="candidate-admitted",
            admissionManifestDigest=_DIGEST,
            certificationAdmissionDigest=_DIGEST,
            gateOnePlanDigest=_DIGEST,
            gateStarts=0,
        ),
    )
    measured = measure_replay_run([event], leg=_leg())
    assert measured.admitted
    assert not measured.admissionRefused


def test_reducer_folds_admission_refused_flag() -> None:
    event = _event(
        1,
        "admission-refused",
        payload=_payload(
            kind="admission-refused",
            refusalCode="bad-input",
            finding={"code": "code", "path": "path", "detail": "detail"},
            gateStarts=0,
        ),
    )
    measured = measure_replay_run([event], leg=_leg())
    assert measured.admissionRefused
    assert not measured.admitted


def test_reducer_folds_finalization_flags() -> None:
    authority = {
        "authorityDigest": _DIGEST,
        "certificateIds": tuple(_DIGEST for _ in range(5)),
        "candidatePairAuthorityDigest": _DIGEST,
        "taskIntentAuthorityDigest": _DIGEST,
        "journalAuthorityDigest": _DIGEST,
    }
    finalization = [
        _event(
            1,
            "finalization-started",
            payload=_payload(
                kind="finalization-started",
                gateFiveCertificateId=_DIGEST,
                authority=authority,
            ),
        ),
        _event(
            2,
            "finalization-boundary-resumed",
            payload=_payload(
                kind="finalization-boundary-resumed",
                journalLeg="journal-leg",
                journalStateDigest=_DIGEST,
                predecessorRevision=1,
            ),
        ),
        _event(
            3,
            "finalization-completed",
            payload=_payload(
                kind="finalization-completed",
                authority=authority,
                finalizationDigest=_DIGEST,
            ),
        ),
    ]
    final_measure = measure_replay_run(finalization, leg=_leg())
    assert final_measure.finalizationStarted
    assert final_measure.finalizationResumed
    assert final_measure.finalizationCompleted


def test_reducer_records_operation_terminal() -> None:
    payload = OperationTerminalPayload.model_construct(
        terminalId=_DIGEST, terminalResultClass="success"
    )
    events = [_event(1, "operation-terminal", payload=payload)]
    measured = measure_replay_run(events, leg=_leg())
    assert measured.operationTerminalClass == "success"


def test_reducer_folds_gate_start_and_rail_census() -> None:
    events = [
        _event(
            1,
            "gate-started",
            gate=1,
            certificateDisposition="pending",
            payload=_payload(kind="gate-started", gate=1, attempt=1, greenPredecessors=()),
        ),
        _event(
            2,
            "rail-started",
            gate=1,
            certificateDisposition="pending",
            payload=_payload(
                kind="rail-started",
                gate=1,
                rail=_rail("ruff"),
                attempt=1,
                certifying=True,
                repetition=None,
            ),
        ),
        _event(
            3,
            "rail-terminal",
            gate=1,
            certificateDisposition="pending",
            payload=_payload(
                kind="rail-terminal",
                gate=1,
                rail=_rail("ruff"),
                attempt=1,
                resultId=_DIGEST,
                disposition="pass",
            ),
        ),
    ]
    measured = measure_replay_run(events, leg=_leg())
    gate_one = measured.gate(1)
    assert gate_one.started
    assert gate_one.startedCount == 1
    assert gate_one.railStartedCount == 1
    assert gate_one.railTerminalCount == 1


def _rail(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def test_reducer_folds_gate_pass_published_and_reused() -> None:
    published_payload = GatePassPayload.model_construct(
        kind="gate-pass", gate=1, attempt=1, mode="published"
    )
    reused_payload = GatePassPayload.model_construct(
        kind="gate-pass", gate=1, attempt=1, mode="reused"
    )
    published = _event(
        1,
        "gate-pass",
        gate=1,
        certificateDisposition="published",
        certificateId=_DIGEST,
        gateResultManifestId=_DIGEST,
        payload=published_payload,
    )
    reused = _event(
        2,
        "gate-pass",
        gate=1,
        certificateDisposition="reused",
        certificateId=_DIGEST,
        gateResultManifestId=_DIGEST,
        payload=reused_payload,
    )
    measured = measure_replay_run([published, reused], leg=_leg())
    assert measured.certificatePublishCount == 1
    assert measured.certificateReuseCount == 1
    assert measured.gate(1).decision == "pass-reused"


def test_reducer_folds_red_and_blocked_decision_kinds() -> None:
    events = [
        _event(
            1,
            "gate-started",
            gate=1,
            certificateDisposition="pending",
            payload=_payload(kind="gate-started", gate=1, attempt=1, greenPredecessors=()),
        ),
        _event(
            2,
            "gate-fail",
            gate=1,
            certificateDisposition="refused",
            gateResultManifestId=_DIGEST,
            payload=_payload(
                kind="gate-fail",
                gate=1,
                attempt=1,
                catalogRevision=1,
                catalogManifestId=_DIGEST,
                stableCause="failed",
            ),
        ),
        _event(
            3,
            "certificate-refused",
            gate=1,
            certificateDisposition="refused",
            gateResultManifestId=_DIGEST,
            payload=_payload(
                kind="certificate-refused",
                gate=1,
                attempt=1,
                catalogRevision=1,
                catalogManifestId=_DIGEST,
                refusalCode="stale-result",
                refusalDetail="stale",
            ),
        ),
        _event(
            4,
            "gate-blocked",
            gate=2,
            payload=_payload(
                kind="gate-blocked",
                gate=2,
                redPredecessorGate=1,
                zeroGateStarts=True,
                gateStarts=0,
            ),
        ),
    ]
    measured = measure_replay_run(events, leg=_leg())
    assert measured.gate(1).decision == "certificate-refused"
    assert measured.gate(2).blocked
    assert measured.gate(2).zeroStartEvidence
    assert measured.gate(2).decision == "blocked"


def test_reducer_folds_certificate_invalidation() -> None:
    event = _event(
        1,
        "certificate-invalidated",
        gate=1,
        certificateDisposition="invalidated",
        certificateId=_DIGEST,
        gateResultManifestId=_DIGEST,
        payload=_payload(
            kind="certificate-invalidated",
            gate=1,
            attempt=1,
            catalogRevision=1,
            catalogManifestId=_DIGEST,
            priorCertificateId=_DIGEST,
            invalidatedGates=(1, 2, 3, 4, 5),
            affectedGateFiveSubrecords=(),
            finalizationRevalidationRequired=True,
            changes=({"changeClass": "code", "reason": "changed"},),
        ),
    )
    measured = measure_replay_run([event], leg=_leg())
    assert measured.certificateInvalidationCount == 1
    assert measured.gate(1).invalidated


def test_reducer_folds_catalog_with_spans_attached() -> None:
    record = CatalogRailRecord.model_construct(
        rail=_rail("ruff"),
        resultId=_DIGEST,
        status="pass",
        posture="enforcing",
    )
    counts = CatalogCounts.model_construct(
        passed=1, failed=0, blocked=0, notApplicable=0, reportOnly=0
    )
    catalog_payload = GateCatalogCompletePayload.model_construct(
        kind="gate-catalog-complete",
        gate=1,
        attempt=1,
        catalogRevision=1,
        disposition="green",
        railResults=(record,),
        counts=counts,
        enforcingFailures=(),
    )
    event = _event(
        1,
        "gate-catalog-complete",
        gate=1,
        certificateDisposition="pending",
        gateResultManifestId=_DIGEST,
        spans=(_span("test-execution", 0, 120, 90),),
        payload=catalog_payload,
    )
    measured = measure_replay_run([event], leg=_leg())
    assert measured.gate(1).catalogCount == 1
    assert measured.gate(1).lastCatalogDisposition == "green"
    assert measured.spans.spanCount == 1
    assert measured.spans.grossWallMillis == 120
    assert isinstance(measured, RunMeasurement)


def test_reducer_ignores_unknown_closeout_kinds() -> None:
    event = _event(
        1,
        "execution-cancelled",
        payload=_payload(
            kind="execution-disposition",
            cause="c",
            producer="p",
            evidenceRef="e",
            predecessorRevision=1,
        ),
    )
    measured = measure_replay_run([event], leg=_leg())
    assert measured.measurementDigest
