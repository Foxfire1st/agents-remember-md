"""Deterministic measured-run reduction over one R16 telemetry export.

measure_replay_run folds an ordered R16 event export into the closed
RunMeasurement record: per-gate start and zero-start evidence, the last
complete gate catalog, the final gate decision, rail start/terminal census,
certificate publication/reuse/invalidation counts, finalization evidence, and
the whole-run per-category span reduction.  The reducer never classifies a
stream or certifies a gate; it only measures what the export records, so a
measurement cannot promote diagnostic or partial evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

from agents_remember.certification.digests import content_digest
from agents_remember.certification.replay.models import (
    CatalogCounts,
    CatalogRailRecord,
    GateRunMeasurement,
    ReplayLegIdentity,
    RunMeasurement,
)
from agents_remember.certification.replay.spans import analyze_span_categories
from agents_remember.certification.telemetry.models import (
    GateCatalogCompletePayload,
    GatePassPayload,
    TelemetryEvent,
)
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import GateId


class _MeasurementState:
    """Mutable fold state; never escapes this module."""

    def __init__(self) -> None:
        self.gates: dict[GateId, GateRunMeasurement] = {
            gate: GateRunMeasurement(gate=gate) for gate in (1, 2, 3, 4, 5)
        }
        self.admitted = False
        self.admission_refused = False
        self.finalization_started = False
        self.finalization_resumed = False
        self.finalization_completed = False
        self.operation_terminal_class: str | None = None
        self.publish_count = 0
        self.reuse_count = 0
        self.invalidation_count = 0
        self.spans: list = []


def measure_replay_run(
    events: Sequence[TelemetryEvent],
    *,
    leg: ReplayLegIdentity,
) -> RunMeasurement:
    """Reduce one ordered closeout telemetry export into its measured facts."""
    if not events:
        raise CertificationContractError("measured replay requires at least one event", [])
    if any(event.executionKind != "closeout-generation" for event in events):
        raise CertificationContractError("measured replay consumes closeout-generation exports", [])
    state = _MeasurementState()
    for event in events:
        _fold_event(state, event)
    span_reduction = analyze_span_categories(state.spans)
    ordered_gates = tuple(state.gates[gate] for gate in (1, 2, 3, 4, 5))
    draft = RunMeasurement.model_construct(
        executionId=events[0].executionId,
        leg=leg,
        admitted=state.admitted,
        admissionRefused=state.admission_refused,
        gates=ordered_gates,
        spans=span_reduction,
        finalizationStarted=state.finalization_started,
        finalizationResumed=state.finalization_resumed,
        finalizationCompleted=state.finalization_completed,
        operationTerminalClass=state.operation_terminal_class,
        certificatePublishCount=state.publish_count,
        certificateReuseCount=state.reuse_count,
        certificateInvalidationCount=state.invalidation_count,
        measurementDigest="",
    )
    payload = draft.model_dump(mode="json", exclude={"measurementDigest"})
    digest = content_digest(payload)
    return RunMeasurement.model_validate({**payload, "measurementDigest": digest})


def _fold_event(state: _MeasurementState, event: TelemetryEvent) -> None:
    if event.spans:
        state.spans.extend(event.spans)
    kind = event.eventKind
    if kind in _FLAG_KINDS:
        _apply_flag(state, kind)
        return
    if kind == "operation-terminal":
        state.operation_terminal_class = event.payload.terminalResultClass  # type: ignore[attr-defined]
        return
    if kind in _GATE_FOLDERS:
        gate = _required_gate(event)
        state.gates[gate] = _GATE_FOLDERS[kind](state, state.gates[gate], event)


_FLAG_KINDS: frozenset[str] = frozenset(
    {
        "candidate-admitted",
        "admission-refused",
        "finalization-started",
        "finalization-boundary-resumed",
        "finalization-completed",
    }
)

_FLAG_ATTRS: dict[str, str] = {
    "candidate-admitted": "admitted",
    "admission-refused": "admission_refused",
    "finalization-started": "finalization_started",
    "finalization-boundary-resumed": "finalization_resumed",
    "finalization-completed": "finalization_completed",
}


def _apply_flag(state: _MeasurementState, kind: str) -> None:
    setattr(state, _FLAG_ATTRS[kind], True)


def _fold_started(
    _state: _MeasurementState,
    measured: GateRunMeasurement,
    _event: TelemetryEvent,
) -> GateRunMeasurement:
    return _replace(
        measured,
        started=True,
        startedCount=measured.startedCount + 1,
    )


def _fold_rail_started(
    _state: _MeasurementState,
    measured: GateRunMeasurement,
    _event: TelemetryEvent,
) -> GateRunMeasurement:
    return _replace(measured, railStartedCount=measured.railStartedCount + 1)


def _fold_rail_terminal(
    _state: _MeasurementState,
    measured: GateRunMeasurement,
    _event: TelemetryEvent,
) -> GateRunMeasurement:
    return _replace(measured, railTerminalCount=measured.railTerminalCount + 1)


def _fold_catalog(
    _state: _MeasurementState,
    measured: GateRunMeasurement,
    event: TelemetryEvent,
) -> GateRunMeasurement:
    payload = event.payload
    assert isinstance(payload, GateCatalogCompletePayload)
    catalog = payload.railResults
    counts = payload.counts
    return _replace(
        measured,
        catalogCount=measured.catalogCount + 1,
        lastCatalog=catalog,
        lastCatalogCounts=counts,
        lastCatalogDisposition=payload.disposition,
    )


def _fold_pass(
    state: _MeasurementState,
    measured: GateRunMeasurement,
    event: TelemetryEvent,
) -> GateRunMeasurement:
    payload = event.payload
    assert isinstance(payload, GatePassPayload)
    if payload.mode == "reused":
        state.reuse_count += 1
        return _replace(measured, decision="pass-reused")
    state.publish_count += 1
    return _replace(measured, decision="pass-published")


def _fold_fail(
    _state: _MeasurementState,
    measured: GateRunMeasurement,
    _event: TelemetryEvent,
) -> GateRunMeasurement:
    return _replace(measured, decision="fail")


def _fold_refused(
    _state: _MeasurementState,
    measured: GateRunMeasurement,
    _event: TelemetryEvent,
) -> GateRunMeasurement:
    return _replace(measured, decision="certificate-refused")


def _fold_blocked(
    _state: _MeasurementState,
    measured: GateRunMeasurement,
    _event: TelemetryEvent,
) -> GateRunMeasurement:
    return _replace(
        measured,
        blocked=True,
        zeroStartEvidence=True,
        decision="blocked",
    )


def _fold_invalidated(
    state: _MeasurementState,
    measured: GateRunMeasurement,
    _event: TelemetryEvent,
) -> GateRunMeasurement:
    state.invalidation_count += 1
    return _replace(measured, invalidated=True)


_GATE_FOLDERS = {
    "gate-started": _fold_started,
    "rail-started": _fold_rail_started,
    "rail-terminal": _fold_rail_terminal,
    "gate-catalog-complete": _fold_catalog,
    "gate-pass": _fold_pass,
    "gate-fail": _fold_fail,
    "certificate-refused": _fold_refused,
    "gate-blocked": _fold_blocked,
    "certificate-invalidated": _fold_invalidated,
}


def _replace(measured: GateRunMeasurement, **changes: object) -> GateRunMeasurement:
    """Immutable in-place helper: validate the mutated shape before returning."""
    return measured.model_copy(update=changes)


def _required_gate(event: TelemetryEvent) -> GateId:
    gate = event.gate
    if gate is None:
        raise CertificationContractError(
            f"gate event {event.eventKind} requires the exact gate identity",
            [],
        )
    return gate


__all__ = [
    "CatalogCounts",
    "CatalogRailRecord",
    "GateRunMeasurement",
    "RunMeasurement",
    "TelemetryEvent",
    "measure_replay_run",
]
