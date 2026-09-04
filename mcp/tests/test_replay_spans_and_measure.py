"""Fully standalone CCR-R17 span-analysis and measured-run reducer tests.

Span intervals are reduced with union arithmetic so wall time is never double
counted, and the measured-run reducer folds an in-memory closeout event stream
into per-gate facts.  Nothing in this module shares certification-run,
evidence-lifecycle, or Dagger artifacts.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CertificationContractFinding, GateId
from agents_remember.certification.replay.measure import measure_replay_run
from agents_remember.certification.replay.models import (
    GateRunMeasurement,
    ReplayLegIdentity,
    RunMeasurement,
    SpanReduction,
)
from agents_remember.certification.replay.spans import (
    analyze_span_categories,
    category_wall_union_millis,
    gross_wall_union_millis,
)
from agents_remember.certification.telemetry.models import (
    CatalogCounts,
    CatalogRailRecord,
    GateBlockedPayload,
    GateCatalogCompletePayload,
    GatePassPayload,
    R21DependencyDecision,
    TelemetryEvent,
    TelemetrySpan,
    catalog_manifest_digest,
)
from agents_remember.errors import CertificationContractError

_DIGEST = "a" * 64


def _g(value: int) -> GateId:
    return cast(GateId, value)


def _span(
    kind: str,
    start: int,
    wall: int,
    active: int,
) -> TelemetrySpan:
    return TelemetrySpan(
        spanKind=kind,  # type: ignore[arg-type]
        startedAt="2026-09-03T00:00:00+02:00",
        startedEpochMillis=start,
        wallMillis=wall,
        activeMillis=active,
    )


def test_span_gross_wall_unions_overlapping_intervals() -> None:
    spans = [
        _span("test-execution", 0, 100, 80),
        _span("test-execution", 50, 100, 60),
        _span("waiting", 200, 50, 10),
    ]
    assert gross_wall_union_millis(spans) == 200
    assert analyze_span_categories(spans).grossWallMillis == 200


def test_span_category_wall_is_union_within_category() -> None:
    spans = [
        _span("test-execution", 0, 100, 80),
        _span("test-execution", 50, 100, 60),
        _span("waiting", 100, 50, 10),
    ]
    assert category_wall_union_millis(spans, "test-execution") == 150
    assert category_wall_union_millis(spans, "waiting") == 50


def test_span_reduction_covers_closed_vocabulary_and_digest() -> None:
    spans = [_span("repair", 0, 30, 20)]
    reduction = analyze_span_categories(spans)
    assert isinstance(reduction, SpanReduction)
    assert len(reduction.categories) == 9
    assert {item.category for item in reduction.categories} == {
        "dagger-environment-setup",
        "test-execution",
        "post-test-scoring",
        "clean-room-api-provider",
        "memory-work",
        "waiting",
        "repair",
        "operator-attention",
        "finalization",
    }
    expected = content_digest(reduction.model_dump(mode="json", exclude={"reductionDigest"}))
    assert reduction.reductionDigest == expected


def test_span_reduction_span_count_equals_per_category_sum() -> None:
    spans = [
        _span("repair", 0, 30, 20),
        _span("repair", 40, 30, 20),
        _span("waiting", 80, 10, 5),
    ]
    reduction = analyze_span_categories(spans)
    assert reduction.spanCount == 3
    assert reduction.grossWallMillis == 70


def _base_event(
    revision: int,
    kind: str,
    *,
    gate: int | None = None,
    spans: tuple[TelemetrySpan, ...] = (),
    disposition: str = "not-applicable",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schemaVersion": "closeout-telemetry-event/v1",
        "executionKind": "closeout-generation",
        "executionId": "replay-run",
        "eventRevision": revision,
        "operationKind": "closeout",
        "generation": 13,
        "diagnosticNonce": None,
        "eventKind": kind,
        "occurredAt": "2026-09-03T00:00:00+02:00",
        "candidate": {"kind": "git-tree", "value": "c" * 40},
        "profileId": "portable-ci",
        "gatePlanDigest": _DIGEST,
        "gate": gate,
        "rail": None,
        "runtime": None,
        "evidence": (),
        "spans": spans,
        "certificateDisposition": disposition,
        "certificateId": None,
        "gateResultManifestId": None,
        "message": None,
    }
    return event


def _gate_started(revision: int, gate: int) -> TelemetryEvent:
    payload = {
        "kind": "gate-started",
        "gate": gate,
        "attempt": 1,
        "greenPredecessors": tuple(
            {"gate": g, "certificateDigest": _DIGEST} for g in range(1, gate)
        ),
    }
    event = _base_event(revision, "gate-started", gate=gate, disposition="pending")
    event["payload"] = payload
    return TelemetryEvent.model_validate(event)  # type: ignore[arg-type]


def _catalog_record(rail_id: str, status: str) -> CatalogRailRecord:
    return CatalogRailRecord(
        rail={"railId": rail_id, "version": "1.0.0"},  # type: ignore[arg-type]
        resultId=_DIGEST,
        status=status,  # type: ignore[arg-type]
        posture="enforcing",
    )


def _red_catalog(
    revision: int,
    gate: int,
    records: tuple[CatalogRailRecord, ...],
) -> TelemetryEvent:
    counts = CatalogCounts(
        passed=sum(1 for r in records if r.status == "pass"),
        failed=sum(1 for r in records if r.status == "fail"),
        blocked=sum(1 for r in records if r.status == "blocked"),
        notApplicable=0,
        reportOnly=0,
    )
    manifest = catalog_manifest_digest(records, counts)
    payload = GateCatalogCompletePayload(
        kind="gate-catalog-complete",
        gate=_g(gate),
        attempt=1,
        catalogRevision=1,
        disposition="red",
        railResults=records,
        counts=counts,
        enforcingFailures=(
            CertificationContractFinding(
                code="rail-failed",
                path=f"gates.{gate}",
                detail="enforcing failure",
            ),
        ),
    )
    event = _base_event(revision, "gate-catalog-complete", gate=gate, disposition="pending")
    event["payload"] = payload
    event["gateResultManifestId"] = manifest
    return TelemetryEvent.model_validate(event)  # type: ignore[arg-type]


def _gate_fail(revision: int, gate: int, manifest: str) -> TelemetryEvent:
    payload = {
        "kind": "gate-fail",
        "gate": gate,
        "attempt": 1,
        "catalogRevision": 1,
        "catalogManifestId": manifest,
        "stableCause": "enforcing rail failed",
    }
    event = _base_event(revision, "gate-fail", gate=gate, disposition="refused")
    event["payload"] = payload
    event["gateResultManifestId"] = manifest
    return TelemetryEvent.model_validate(event)  # type: ignore[arg-type]


def _gate_blocked(revision: int, gate: int, red_predecessor: int) -> TelemetryEvent:
    payload = GateBlockedPayload(
        kind="gate-blocked",
        gate=_g(gate),
        redPredecessorGate=_g(red_predecessor),
        zeroGateStarts=True,
        gateStarts=0,
    )
    event = _base_event(revision, "gate-blocked", gate=gate)
    event["payload"] = payload
    return TelemetryEvent.model_validate(event)  # type: ignore[arg-type]


def _gate_pass(revision: int, gate: int) -> TelemetryEvent:
    payload = GatePassPayload(
        kind="gate-pass",
        gate=_g(gate),
        attempt=1,
        mode="published",
        catalogRevision=1,
        catalogManifestId=_DIGEST,
        priorCertificateId=None,
        dependencyDecision=R21DependencyDecision(
            reusedCertificateIds=(),
            firstGateToRun=None,
            zeroGateStarts=False,
            finalizationRevalidationRequired=False,
            decisionDigest=content_digest(
                {
                    "reusedCertificateIds": (),
                    "firstGateToRun": None,
                    "zeroGateStarts": False,
                    "finalizationRevalidationRequired": False,
                }
            ),
        ),
    )
    event = _base_event(revision, "gate-pass", gate=gate, disposition="published")
    event["payload"] = payload
    event["certificateId"] = _DIGEST
    event["gateResultManifestId"] = _DIGEST
    return TelemetryEvent.model_validate(event)  # type: ignore[arg-type]


def _admission(revision: int) -> TelemetryEvent:
    event = _base_event(revision, "candidate-admitted")
    event["payload"] = {
        "kind": "candidate-admitted",
        "admissionManifestDigest": _DIGEST,
        "certificationAdmissionDigest": _DIGEST,
        "gateOnePlanDigest": _DIGEST,
        "gateStarts": 0,
    }
    return TelemetryEvent.model_validate(event)  # type: ignore[arg-type]


def _leg() -> dict[str, Any]:
    return {"role": "treatment", "freezeDigest": _DIGEST}


def _run(events: list[TelemetryEvent]) -> RunMeasurement:
    return measure_replay_run(
        events,
        leg=ReplayLegIdentity(role="treatment", freezeDigest=_DIGEST),
    )


def test_measure_refuses_empty_and_diagnostic_exports() -> None:
    leg = ReplayLegIdentity(role="baseline", freezeDigest=_DIGEST)
    with pytest.raises(CertificationContractError):
        measure_replay_run([], leg=leg)
    diagnostic = _base_event(1, "diagnostic-started")
    diagnostic["executionKind"] = "diagnostic-run"
    diagnostic["operationKind"] = None
    diagnostic["generation"] = None
    diagnostic["diagnosticNonce"] = "a" * 32
    diagnostic["payload"] = {
        "kind": "diagnostic-started",
        "planDigest": _DIGEST,
        "planVersion": "scenario-1.0.0",
        "railCount": 1,
        "certifying": False,
    }
    with pytest.raises(CertificationContractError):
        measure_replay_run([TelemetryEvent.model_validate(diagnostic)], leg=leg)  # type: ignore[arg-type]


def test_measure_gate_one_red_records_catalog_and_fail() -> None:
    records = (
        _catalog_record("file-size", "fail"),
        _catalog_record("pyright", "pass"),
    )
    counts = CatalogCounts(
        passed=1,
        failed=1,
        blocked=0,
        notApplicable=0,
        reportOnly=0,
    )
    manifest = catalog_manifest_digest(records, counts)
    events = [
        _admission(1),
        _gate_started(2, 1),
        _red_catalog(3, 1, records),
        _gate_fail(4, 1, manifest),
    ]
    measured = _run(events)
    gate_one = measured.gate(1)
    assert gate_one.started
    assert gate_one.startedCount == 1
    assert gate_one.lastCatalogDisposition == "red"
    assert gate_one.decision == "fail"
    assert gate_one.catalogCount == 1
    assert {record.rail.railId for record in gate_one.lastCatalog} == {
        "file-size",
        "pyright",
    }
    assert len(gate_one.failedRails) == 1
    assert measured.certificatePublishCount == 0
    assert measured.certificateReuseCount == 0


def test_measure_gate_blocked_never_starts_and_zero_start_evidence() -> None:
    records = (_catalog_record("file-size", "fail"),)
    counts = CatalogCounts(
        passed=0,
        failed=1,
        blocked=0,
        notApplicable=0,
        reportOnly=0,
    )
    manifest = catalog_manifest_digest(records, counts)
    events = [
        _admission(1),
        _gate_started(2, 1),
        _red_catalog(3, 1, records),
        _gate_fail(4, 1, manifest),
        _gate_blocked(5, 2, 1),
        _gate_blocked(6, 3, 1),
    ]
    measured = _run(events)
    assert measured.gate(2).blocked
    assert measured.gate(2).zeroStartEvidence
    assert measured.gate(2).decision == "blocked"
    assert not measured.gate(2).started
    assert not measured.gate(3).started
    assert measured.gate(5).decision == "none"


def test_measure_certificate_publish_and_spans() -> None:
    events = [
        _admission(1),
        _gate_started(2, 1),
        _gate_pass(3, 1),
        _gate_started(4, 2),
        _gate_pass(5, 2),
    ]
    measured = _run(events)
    assert measured.certificatePublishCount == 2
    assert measured.gate(1).decision == "pass-published"
    assert measured.gate(2).decision == "pass-published"
    assert measured.measurementDigest == content_digest(
        measured.model_dump(mode="json", exclude={"measurementDigest"})
    )


def test_measure_operation_terminal_and_finalization_flags() -> None:
    events = [
        _admission(1),
        _gate_started(2, 1),
        _gate_pass(3, 1),
    ]
    terminal = _base_event(4, "operation-terminal", gate=None)
    terminal["payload"] = {
        "kind": "operation-terminal",
        "terminalId": _DIGEST,
        "terminalResultClass": "success",
    }
    events.append(TelemetryEvent.model_validate(terminal))  # type: ignore[arg-type]
    measured = _run(events)
    assert measured.operationTerminalClass == "success"
    assert not measured.finalizationStarted


def test_gate_run_measurement_rejects_blocked_and_started_together() -> None:

    with pytest.raises(ValueError):
        GateRunMeasurement(
            gate=1,
            started=True,
            blocked=True,
            decision="blocked",
        )
