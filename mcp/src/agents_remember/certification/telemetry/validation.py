"""Exhaustive matrix and cardinality validation for one durable execution stream.

Missing, duplicate, out-of-order, cross-identity, cardinality-invalid, or
result-inconsistent events make telemetry readiness red.  The validator never
reruns a rail and never derives a rail pass from telemetry alone: a passing
rail-terminal must carry its own bounded evidence.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.models import (
    CertificationContractFinding,
    FrozenContractModel,
    GateId,
    SemanticText,
)
from agents_remember.certification.telemetry.models import (
    DIAGNOSTIC_ONLY_EVENT_KINDS,
    CatalogRailRecord,
    CertificateInvalidatedPayload,
    CertificateRefusedPayload,
    FinalizationStartedPayload,
    GateCatalogCompletePayload,
    GateFailPayload,
    GatePassPayload,
    GateStartedPayload,
    OperationTerminalPayload,
    RailStartedPayload,
    RailTerminalPayload,
    TelemetryEvent,
)

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_COUNT_FINDINGS_CAP = 4096
_ZERO_START_GATE_KINDS: frozenset[str] = frozenset(
    {
        "gate-started",
        "rail-started",
        "rail-terminal",
        "gate-catalog-complete",
        "gate-pass",
        "gate-fail",
        "certificate-refused",
        "certificate-invalidated",
    }
)


class TelemetryValidationReport(FrozenContractModel):
    """Closed report: red findings never double as rail passes."""

    schemaVersion: Literal["closeout-telemetry-validation/v1"] = "closeout-telemetry-validation/v1"
    executionId: SemanticText
    ok: bool
    eventCount: int = Field(ge=0)
    findings: tuple[CertificationContractFinding, ...] = Field(max_length=_COUNT_FINDINGS_CAP)

    @model_validator(mode="after")
    def _require_state_shape(self) -> Self:
        if self.ok != (not self.findings):
            raise ValueError("a red validation report must carry its exact findings")
        return self


class TelemetryReadiness(FrozenContractModel):
    """Typed telemetry readiness; a failure is itself typed and never a rail pass."""

    executionId: SemanticText
    state: Literal["green", "red"]
    findings: tuple[CertificationContractFinding, ...] = Field(max_length=_COUNT_FINDINGS_CAP)

    @model_validator(mode="after")
    def _require_state_shape(self) -> Self:
        if (self.state == "green") != (not self.findings):
            raise ValueError("telemetry readiness must be red exactly when findings exist")
        return self


TelemetryValidationReport.model_rebuild()
TelemetryReadiness.model_rebuild()


def validate_execution_telemetry(
    events: Sequence[TelemetryEvent],
) -> TelemetryValidationReport:
    """Validate one complete ordered event stream and never raise."""
    findings: list[CertificationContractFinding] = []
    ordered = tuple(events)
    execution_id = ordered[0].executionId if ordered else "empty-execution"
    if not ordered:
        findings.append(_finding("missing-execution-events", "execution", "no events recorded"))
        return _report(execution_id, ordered, findings)
    _validate_stream_identity(ordered, findings)
    _validate_diagnostic_envelope(ordered, findings)
    _validate_zero_start_barrier(ordered, findings)
    _validate_rail_matching(ordered, findings)
    _validate_catalogs(ordered, findings)
    _validate_catalog_citations(ordered, findings)
    _validate_blocked_gates(ordered, findings)
    _validate_invalidations(ordered, findings)
    _validate_operation_terminal(ordered, findings)
    _validate_finalization(ordered, findings)
    _validate_rail_pass_evidence(ordered, findings)
    return _report(execution_id, ordered, findings)


def compile_telemetry_readiness(events: Sequence[TelemetryEvent]) -> TelemetryReadiness:
    """Project the R16 failure surface: readiness red on any telemetry invalidity."""
    report = validate_execution_telemetry(events)
    return TelemetryReadiness(
        executionId=report.executionId,
        state="green" if report.ok else "red",
        findings=report.findings,
    )


def _report(
    execution_id: str,
    events: Sequence[TelemetryEvent],
    findings: list[CertificationContractFinding],
) -> TelemetryValidationReport:
    return TelemetryValidationReport(
        executionId=execution_id,
        ok=not findings,
        eventCount=len(events),
        findings=tuple(sorted(findings, key=lambda item: (item.code, item.path, item.detail))),
    )


def _validate_stream_identity(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    revisions = [event.eventRevision for event in ordered]
    if revisions != list(range(1, len(ordered) + 1)):
        findings.append(
            _finding(
                "event-revision-gap",
                "execution.eventRevision",
                "event revisions must be contiguous starting at one",
            )
        )
    if len({event.executionId for event in ordered}) != 1:
        findings.append(
            _finding("cross-execution-id", "execution.executionId", "events mix execution ids")
        )
    if len({event.executionKind for event in ordered}) != 1:
        findings.append(
            _finding("cross-execution-kind", "execution.executionKind", "events mix envelopes")
        )
    kind = ordered[0].executionKind
    if kind == "closeout-generation":
        if (
            len({event.operationKind for event in ordered}) != 1
            or len({event.generation for event in ordered}) != 1
        ):
            findings.append(
                _finding(
                    "cross-generation-identity",
                    "execution.generation",
                    "closeout events mix operation kind or public generation",
                )
            )
    elif len({event.diagnosticNonce for event in ordered}) != 1:
        findings.append(
            _finding(
                "cross-diagnostic-nonce",
                "execution.diagnosticNonce",
                "diagnostic events mix R13 nonces",
            )
        )


def _validate_diagnostic_envelope(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    if ordered[0].executionKind != "diagnostic-run":
        return
    for event in ordered:
        if event.eventKind not in DIAGNOSTIC_ONLY_EVENT_KINDS:
            findings.append(
                _finding(
                    "diagnostic-authority-promotion",
                    f"events.r{event.eventRevision}.eventKind",
                    "a diagnostic run can never acquire gate, certificate, delivery, approval, "
                    "or finalization authority",
                )
            )


def _validate_zero_start_barrier(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    admitted: dict[str, int] = {}
    for event in ordered:
        if event.eventKind == "candidate-admitted":
            admitted.setdefault(event.executionId, event.eventRevision)
            continue
        if event.eventKind in _ZERO_START_GATE_KINDS and (admitted.get(event.executionId) is None):
            findings.append(
                _finding(
                    "gate-started-before-admission",
                    f"events.r{event.eventRevision}.eventKind",
                    "gates cannot start before the zero-start admission boundary",
                )
            )


def _validate_rail_matching(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    started_gates: dict[tuple[int, int], int] = {}
    rail_starts: dict[tuple[int, str, int], int] = {}
    rail_terminals: dict[tuple[int, str, int], int] = {}
    for event in ordered:
        if event.eventKind == "gate-started":
            _check_gate_start(event, started_gates, findings)
        elif event.eventKind == "rail-started":
            _check_rail_start(event, started_gates, rail_starts, findings)
        elif event.eventKind == "rail-terminal":
            _check_rail_terminal(event, ordered, rail_starts, rail_terminals, findings)


def _check_gate_start(
    event: TelemetryEvent,
    started_gates: dict[tuple[int, int], int],
    findings: list[CertificationContractFinding],
) -> None:
    payload = event.payload
    assert isinstance(payload, GateStartedPayload)
    key = (payload.gate, payload.attempt)
    if key in started_gates:
        findings.append(
            _finding(
                "duplicate-gate-started",
                f"events.r{event.eventRevision}.payload",
                "the same gate attempt started more than once",
            )
        )
    started_gates[key] = event.eventRevision


def _check_rail_start(
    event: TelemetryEvent,
    started_gates: dict[tuple[int, int], int],
    rail_starts: dict[tuple[int, str, int], int],
    findings: list[CertificationContractFinding],
) -> None:
    payload = event.payload
    assert isinstance(payload, RailStartedPayload)
    key = (payload.gate, payload.rail.key, payload.attempt)
    if key in rail_starts:
        findings.append(
            _finding(
                "duplicate-rail-started",
                f"events.r{event.eventRevision}.payload",
                "the same rail attempt started more than once",
            )
        )
    rail_starts[key] = event.eventRevision
    if (payload.gate, payload.attempt) not in started_gates:
        findings.append(
            _finding(
                "rail-started-without-gate",
                f"events.r{event.eventRevision}.payload",
                "a rail cannot start before its exact gate attempt",
            )
        )


def _check_rail_terminal(
    event: TelemetryEvent,
    ordered: tuple[TelemetryEvent, ...],
    rail_starts: dict[tuple[int, str, int], int],
    rail_terminals: dict[tuple[int, str, int], int],
    findings: list[CertificationContractFinding],
) -> None:
    payload = event.payload
    assert isinstance(payload, RailTerminalPayload)
    key = (payload.gate, payload.rail.key, payload.attempt)
    if key in rail_terminals:
        findings.append(
            _finding(
                "duplicate-rail-terminal",
                f"events.r{event.eventRevision}.payload",
                "the same rail attempt terminalized more than once",
            )
        )
    rail_terminals[key] = event.eventRevision
    started = rail_starts.get(key)
    if started is None:
        findings.append(
            _finding(
                "rail-terminal-without-start",
                f"events.r{event.eventRevision}.payload",
                "each rail-terminal requires exactly one earlier matching rail-started",
            )
        )
    elif start_count := sum(
        1
        for prior in ordered
        if prior.eventKind == "rail-started"
        and isinstance(prior.payload, RailStartedPayload)
        and (prior.payload.gate, prior.payload.rail.key, prior.payload.attempt) == key
        and prior.eventRevision < event.eventRevision
    ):
        if start_count != 1:
            findings.append(
                _finding(
                    "rail-terminal-start-cardinality",
                    f"events.r{event.eventRevision}.payload",
                    "each rail-terminal requires exactly one earlier matching rail-started",
                )
            )
    else:
        findings.append(
            _finding(
                "rail-terminal-start-cardinality",
                f"events.r{event.eventRevision}.payload",
                "each rail-terminal requires exactly one earlier matching rail-started",
            )
        )


def _validate_catalogs(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    per_gate_revisions: dict[int, list[int]] = defaultdict(list)
    for catalog_event in (event for event in ordered if event.eventKind == "gate-catalog-complete"):
        payload = catalog_event.payload
        assert isinstance(payload, GateCatalogCompletePayload)
        path = f"events.r{catalog_event.eventRevision}.payload"
        if not _has_earlier(ordered, catalog_event, "gate-started", payload.gate, payload.attempt):
            findings.append(
                _finding(
                    "catalog-without-gate-start",
                    path,
                    "a gate catalog requires its exact earlier gate-started attempt",
                )
            )
        revisions = per_gate_revisions[payload.gate]
        if revisions and payload.catalogRevision <= revisions[-1]:
            findings.append(
                _finding(
                    "catalog-revision-stale",
                    path,
                    "catalog revisions must advance monotonically within one gate",
                )
            )
        revisions.append(payload.catalogRevision)
        _validate_catalog_records(ordered, catalog_event, payload, path, findings)


def _validate_catalog_records(
    ordered: tuple[TelemetryEvent, ...],
    catalog_event: TelemetryEvent,
    payload: GateCatalogCompletePayload,
    path: str,
    findings: list[CertificationContractFinding],
) -> None:
    terminals_by_rail, cross_attempt = _partition_catalog_terminals(ordered, payload)
    referenced = {record.rail.key for record in payload.railResults}
    for record in payload.railResults:
        matches = terminals_by_rail.get(record.rail.key, [])
        if not matches:
            if cross_attempt.get(record.rail.key):
                findings.append(
                    _finding(
                        "catalog-cross-attempt-terminal",
                        f"{path}.railResults.{record.rail.key}",
                        "the catalog references no terminal in its exact attempt",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "catalog-terminal-missing",
                        f"{path}.railResults.{record.rail.key}",
                        "the complete catalog requires exactly one earlier terminal per plan rail",
                    )
                )
            continue
        if len(matches) > 1:
            findings.append(
                _finding(
                    "duplicate-catalog-terminal",
                    f"{path}.railResults.{record.rail.key}",
                    "a catalog row matched more than one terminal in its exact attempt",
                )
            )
            continue
        _validate_catalog_terminal_match(matches[0], record, catalog_event, path, findings)
    _validate_extra_terminals(terminals_by_rail, referenced, path, findings)


def _partition_catalog_terminals(
    ordered: tuple[TelemetryEvent, ...],
    payload: GateCatalogCompletePayload,
) -> tuple[dict[str, list[TelemetryEvent]], dict[str, list[TelemetryEvent]]]:
    terminals_by_rail: dict[str, list[TelemetryEvent]] = defaultdict(list)
    cross_attempt: dict[str, list[TelemetryEvent]] = defaultdict(list)
    for event in ordered:
        if event.eventKind != "rail-terminal":
            continue
        terminal = event.payload
        assert isinstance(terminal, RailTerminalPayload)
        if terminal.gate != payload.gate:
            continue
        if terminal.attempt == payload.attempt:
            terminals_by_rail[terminal.rail.key].append(event)
        else:
            cross_attempt[terminal.rail.key].append(event)
    return terminals_by_rail, cross_attempt


def _validate_catalog_terminal_match(
    terminal_event: TelemetryEvent,
    record: CatalogRailRecord,
    catalog_event: TelemetryEvent,
    path: str,
    findings: list[CertificationContractFinding],
) -> None:
    terminal = terminal_event.payload
    assert isinstance(terminal, RailTerminalPayload)
    if terminal_event.eventRevision > catalog_event.eventRevision:
        findings.append(
            _finding(
                "catalog-later-terminal",
                f"{path}.railResults.{record.rail.key}",
                "a referenced terminal must precede its gate catalog",
            )
        )
    if terminal_event.candidate != catalog_event.candidate:
        findings.append(
            _finding(
                "catalog-cross-candidate-terminal",
                f"{path}.railResults.{record.rail.key}",
                "catalog and terminal must bind the same candidate",
            )
        )
    if terminal_event.gatePlanDigest != catalog_event.gatePlanDigest:
        findings.append(
            _finding(
                "catalog-cross-plan-terminal",
                f"{path}.railResults.{record.rail.key}",
                "catalog and terminal must bind the same gate plan",
            )
        )
    if terminal.resultId != record.resultId:
        findings.append(
            _finding(
                "terminal-result-mismatch",
                f"{path}.railResults.{record.rail.key}",
                "the catalog row must cite the terminal's immutable result identity",
            )
        )


def _validate_extra_terminals(
    terminals_by_rail: dict[str, list[TelemetryEvent]],
    referenced: set[str],
    path: str,
    findings: list[CertificationContractFinding],
) -> None:
    for rail_key, matches in terminals_by_rail.items():
        if rail_key in referenced:
            continue
        for _terminal in matches:
            findings.append(
                _finding(
                    "catalog-extra-terminal",
                    f"{path}.railResults",
                    f"terminal {rail_key} is not part of any applicable plan rail catalog",
                )
            )


def _validate_catalog_citations(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    for event in ordered:
        if event.eventKind not in {"gate-pass", "gate-fail", "certificate-refused"}:
            continue
        _validate_catalog_citation(event, ordered, findings)


def _catalog_decision_fields(payload: object) -> tuple[GateId, int, str, bool]:
    """The cited gate / catalog revision / manifest and the decision polarity."""
    if isinstance(payload, GatePassPayload):
        return payload.gate, payload.catalogRevision, payload.catalogManifestId, True
    if isinstance(payload, GateFailPayload):
        return payload.gate, payload.catalogRevision, payload.catalogManifestId, False
    assert isinstance(payload, CertificateRefusedPayload)
    return payload.gate, payload.catalogRevision, payload.catalogManifestId, True


def _catalog_decision_matches(
    event: TelemetryEvent,
    ordered: tuple[TelemetryEvent, ...],
    gate: GateId,
    revision: int,
) -> tuple[
    list[TelemetryEvent],
    list[TelemetryEvent],
]:
    """Earlier gate catalogs for gate, and the exact-revision citations."""
    catalog_events = [
        prior
        for prior in ordered
        if prior.eventKind == "gate-catalog-complete"
        and prior.eventRevision < event.eventRevision
        and prior.gate == gate
    ]
    matches = [
        prior
        for prior in catalog_events
        if isinstance(prior.payload, GateCatalogCompletePayload)
        and prior.payload.catalogRevision == revision
    ]
    return catalog_events, matches


def _validate_catalog_manifest_match(
    catalog: TelemetryEvent,
    manifest: str,
    path: str,
    findings: list[CertificationContractFinding],
) -> None:
    if catalog.gateResultManifestId != manifest:
        findings.append(
            _finding(
                "catalog-citation-manifest-mismatch",
                path,
                "gate decisions must cite the identical prior catalog manifest",
            )
        )


def _validate_catalog_latest_stale(
    catalog_events: list[TelemetryEvent],
    revision: int,
    path: str,
    findings: list[CertificationContractFinding],
) -> None:
    latest = max(
        (
            prior.payload
            for prior in catalog_events
            if isinstance(prior.payload, GateCatalogCompletePayload)
        ),
        key=lambda item: item.catalogRevision,
    )
    if latest.catalogRevision != revision:
        findings.append(
            _finding(
                "catalog-citation-stale",
                path,
                "later catalog results must never enter an older cited manifest",
            )
        )


def _validate_catalog_disposition(
    catalog_payload: GateCatalogCompletePayload,
    want_green: bool,
    path: str,
    findings: list[CertificationContractFinding],
) -> None:
    if want_green and catalog_payload.disposition != "green":
        findings.append(
            _finding(
                "catalog-citation-disposition-mismatch",
                path,
                "a green decision cannot cite a red catalog",
            )
        )
    if not want_green and catalog_payload.disposition != "red":
        findings.append(
            _finding(
                "catalog-citation-disposition-mismatch",
                path,
                "a red decision cannot cite a green catalog",
            )
        )


def _validate_catalog_citation(
    event: TelemetryEvent,
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    gate, revision, manifest, want_green = _catalog_decision_fields(event.payload)
    path = f"events.r{event.eventRevision}.payload"
    catalog_events, matches = _catalog_decision_matches(event, ordered, gate, revision)
    if not matches:
        findings.append(
            _finding(
                "catalog-citation-missing",
                path,
                "gate decisions must cite one exact earlier catalog revision",
            )
        )
        return
    if len(matches) > 1:
        findings.append(
            _finding(
                "catalog-citation-multiple",
                path,
                "one catalog revision resolved to multiple gate catalogs",
            )
        )
        return
    catalog = matches[0]
    catalog_payload = catalog.payload
    assert isinstance(catalog_payload, GateCatalogCompletePayload)
    _validate_catalog_manifest_match(catalog, manifest, path, findings)
    _validate_catalog_latest_stale(catalog_events, revision, path, findings)
    _validate_catalog_disposition(catalog_payload, want_green, path, findings)


def _validate_blocked_gates(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    for event in ordered:
        if event.eventKind != "gate-blocked":
            continue
        payload = event.payload
        assert isinstance(payload, object) and payload.kind == "gate-blocked"
        gate = int(payload.gate)
        path = f"events.r{event.eventRevision}.payload"
        gate_events = [prior for prior in ordered if prior.gate == gate]
        if any(prior.eventKind in {"gate-started", "rail-started"} for prior in gate_events):
            findings.append(
                _finding(
                    "blocked-gate-started",
                    path,
                    "a blocked later gate has no gate-started or rail-started event",
                )
            )
        red_before = any(
            prior.eventRevision < event.eventRevision
            and (
                (
                    prior.eventKind == "gate-catalog-complete"
                    and isinstance(prior.payload, GateCatalogCompletePayload)
                    and prior.payload.disposition == "red"
                    and prior.gate == int(payload.redPredecessorGate)
                )
                or (
                    prior.eventKind == "gate-fail" and prior.gate == int(payload.redPredecessorGate)
                )
            )
            for prior in ordered
        )
        if not red_before:
            findings.append(
                _finding(
                    "blocked-gate-red-predecessor-missing",
                    path,
                    "a blocked later gate requires an exact earlier red predecessor",
                )
            )


def _validate_invalidations(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    for event in ordered:
        if event.eventKind != "certificate-invalidated":
            continue
        payload = event.payload
        assert isinstance(payload, CertificateInvalidatedPayload)
        path = f"events.r{event.eventRevision}.payload"
        passes = [
            prior
            for prior in ordered
            if prior.eventKind == "gate-pass"
            and prior.eventRevision < event.eventRevision
            and prior.gate == payload.gate
            and prior.certificateId == payload.priorCertificateId
        ]
        if not passes:
            findings.append(
                _finding(
                    "invalidation-prior-certificate-missing",
                    path,
                    "invalidation requires the exact prior published certificate",
                )
            )
            continue
        latest_pass = passes[-1]
        if latest_pass.gateResultManifestId != payload.catalogManifestId:
            findings.append(
                _finding(
                    "invalidation-manifest-mismatch",
                    path,
                    "invalidation must cite the manifest of the invalidated certificate",
                )
            )


def _validate_operation_terminal(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    terminals = [event for event in ordered if event.eventKind == "operation-terminal"]
    if not terminals:
        return
    if len(terminals) > 1:
        findings.append(
            _finding(
                "duplicate-operation-terminal",
                "execution.operation-terminal",
                "one execution has exactly one terminal result",
            )
        )
    terminal = terminals[-1]
    if terminal.eventRevision != ordered[-1].eventRevision:
        findings.append(
            _finding(
                "operation-terminal-not-final",
                f"events.r{terminal.eventRevision}.eventKind",
                "operation terminal must be the final event of its execution",
            )
        )
    payload = terminal.payload
    assert isinstance(payload, OperationTerminalPayload)
    if payload.terminalResultClass == "gate-result":
        catalogs = [
            event
            for event in ordered
            if event.eventKind == "gate-catalog-complete" and event.gateResultManifestId is not None
        ]
        available = sorted(
            (event for event in catalogs),
            key=lambda event: event.eventRevision,
        )
        if not available or available[-1].gateResultManifestId != terminal.gateResultManifestId:
            findings.append(
                _finding(
                    "terminal-result-unavailable",
                    f"events.r{terminal.eventRevision}.payload",
                    "gate-result terminal class requires the exact available manifest",
                )
            )


def _validate_finalization(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    started: int | None = None
    for event in ordered:
        if event.eventKind == "finalization-started":
            started = event.eventRevision
            payload = event.payload
            assert isinstance(payload, FinalizationStartedPayload)
            passes = [
                prior
                for prior in ordered
                if prior.eventKind == "gate-pass"
                and prior.eventRevision < event.eventRevision
                and prior.gate == 5
                and prior.certificateId == payload.gateFiveCertificateId
            ]
            if not passes:
                findings.append(
                    _finding(
                        "finalization-green-gate-five-missing",
                        f"events.r{event.eventRevision}.payload",
                        "finalization requires the exact green Gate-5 certificate",
                    )
                )
        if event.eventKind == "finalization-boundary-resumed" and started is None:
            findings.append(
                _finding(
                    "finalization-resume-without-start",
                    f"events.r{event.eventRevision}.payload",
                    "a resumed finalization boundary requires an earlier start",
                )
            )
        if event.eventKind == "finalization-completed" and started is None:
            findings.append(
                _finding(
                    "finalization-completed-without-start",
                    f"events.r{event.eventRevision}.payload",
                    "completion requires an earlier finalization boundary",
                )
            )


def _validate_rail_pass_evidence(
    ordered: tuple[TelemetryEvent, ...],
    findings: list[CertificationContractFinding],
) -> None:
    for event in ordered:
        if event.eventKind == "rail-terminal":
            payload = event.payload
            assert isinstance(payload, RailTerminalPayload)
            if payload.disposition == "pass" and not event.evidence:
                findings.append(
                    _finding(
                        "rail-pass-without-evidence",
                        f"events.r{event.eventRevision}.payload",
                        "no rail pass may be inferred from exit, heartbeat, queue, or elapsed time",
                    )
                )


def _has_earlier(
    ordered: tuple[TelemetryEvent, ...],
    current: TelemetryEvent,
    event_kind: str,
    gate: int,
    attempt: int,
) -> bool:
    for prior in ordered:
        if prior.eventRevision >= current.eventRevision:
            return False
        if prior.eventKind == event_kind and prior.gate == gate:
            payload = prior.payload
            payload_attempt = getattr(payload, "attempt", None)
            if payload_attempt == attempt:
                return True
    return False


def _finding(code: str, path: str, detail: str) -> CertificationContractFinding:
    if not _ID_PATTERN.fullmatch(code):
        raise ValueError(f"telemetry finding code {code!r} is not canonical")
    return CertificationContractFinding(code=code, path=path, detail=detail)


__all__ = [
    "TelemetryReadiness",
    "TelemetryValidationReport",
    "compile_telemetry_readiness",
    "validate_execution_telemetry",
]
