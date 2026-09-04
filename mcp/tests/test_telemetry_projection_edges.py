"""Edge coverage for the closeout telemetry projection module.

Focused companions to test_telemetry_projection that reach the defensive
validator raises on the gate/projection models, the empty-stream guard, the
certificate-refused and admission-refused fold branches, the unmatched-kind
and empty-history fold exits, and the public project_gate_history helper.
"""

from __future__ import annotations

import pytest
from agents_remember.certification.models import CertificationContractFinding
from agents_remember.certification.telemetry import (
    GateTelemetryProjection,
    RailTelemetryProjection,
    TelemetryProjection,
    compile_admission_refused,
    compile_admission_started,
    compile_certificate_refused,
    compile_diagnostic_terminal,
    compile_operation_terminal,
    compile_rail_terminal,
    project_execution_telemetry,
    project_gate_history,
)
from agents_remember.certification.telemetry.models import (
    GateCitation,
    PredecessorBoundary,
)
from agents_remember.certification.telemetry.projection import _fold_gate_state
from test_telemetry_projection import (
    _diagnostic_ctx,
    _gate_plan,
    _portable,
    _results,
    _Trace,
)

_DIGEST = "a" * 64
_GATE_IDS = (1, 2, 3, 4, 5)


def test_blocked_gate_projection_requires_its_red_predecessor() -> None:
    """Direct construction guard: blocked-without-blockedBy is inconsistent."""
    with pytest.raises(ValueError, match="a blocked gate projection requires"):
        GateTelemetryProjection(gate=3, state="blocked")


def test_certificate_identity_requires_its_disposition() -> None:
    """Direct construction guard: certificateId without disposition is invalid."""
    with pytest.raises(ValueError, match="certificate identity requires its disposition"):
        GateTelemetryProjection(gate=3, certificateId=_DIGEST)


def test_projection_digest_mismatch_is_rejected() -> None:
    """Revalidation guard: a tampered projectionDigest fails the digest check."""
    trace = _Trace()
    trace.push(
        compile_certificate_refused(
            trace.ctx(),
            gate=2,
            citation=GateCitation(attempt=1, catalog_revision=1, catalog_manifest_id=_DIGEST),
            refusal_code="stale-result",
            refusal_detail="stale predecessor digest",
        )
    )
    projection = project_execution_telemetry(trace.events)
    tampered = projection.model_dump(mode="json")
    tampered["projectionDigest"] = "f" * 64
    with pytest.raises(ValueError, match="telemetry projection digest does not match"):
        TelemetryProjection.model_validate(tampered)


def test_execution_projection_requires_at_least_one_event() -> None:
    """The fold refuses an empty durable event stream."""
    with pytest.raises(ValueError, match="requires at least one event"):
        project_execution_telemetry(())


def test_certificate_refused_event_folds_refused_gate_state() -> None:
    """A certificate-refused event dispatches to and runs the refused fold body."""
    trace = _Trace()
    trace.push(
        compile_certificate_refused(
            trace.ctx(),
            gate=4,
            citation=GateCitation(attempt=1, catalog_revision=1, catalog_manifest_id=_DIGEST),
            refusal_code="artifact-mismatch",
            refusal_detail="consumed artifact digest moved",
        )
    )
    projection = project_execution_telemetry(trace.events)
    assert projection.gates[3].state == "certificate-refused"
    assert projection.gates[3].attempt == 1
    assert projection.gates[3].catalogRevision == 1
    assert projection.gates[3].gateResultManifestId == _DIGEST


def test_admission_refused_folds_refused_boundary() -> None:
    """An admission-refused event runs the refused branch of the boundary fold."""
    trace = _Trace()
    trace.push(
        compile_admission_started(trace.ctx(), predecessor=PredecessorBoundary(zeroStart=True))
    )
    trace.push(
        compile_admission_refused(
            trace.ctx(),
            refusal_code="profile-mismatch",
            finding=CertificationContractFinding(
                code="profile-mismatch",
                path="boundary.admission",
                detail="portable profile does not match the closeout profile",
            ),
        )
    )
    projection = project_execution_telemetry(trace.events)
    assert projection.boundary.admissionState == "refused"
    assert projection.boundary.refusalCode == "profile-mismatch"
    assert projection.boundary.admissionRevision == 2


def test_rail_terminal_without_a_start_exits_the_terminal_fold() -> None:
    """A lone rail-terminal event folds an empty rail history without matching."""
    scenario = _portable()
    trace = _Trace()
    result = _results(scenario, 1)[0]
    trace.push(
        compile_rail_terminal(
            trace.ctx(),
            gate_plan=_gate_plan(scenario, 1),
            result=result,
            attempt=1,
        )
    )
    projection = project_execution_telemetry(trace.events)
    assert projection.gates[0].rails == ()
    assert projection.gates[0].state == "not-started"


def test_diagnostic_terminal_without_a_start_exits_the_diagnostic_fold() -> None:
    """A lone diagnostic-terminal event leaves the diagnostics list untouched."""
    trace = _Trace()
    trace.push(
        compile_diagnostic_terminal(
            _diagnostic_ctx(trace),
            result_id=_DIGEST,
            disposition="aborted",
        )
    )
    projection = project_execution_telemetry(trace.events)
    assert projection.diagnostics == ()


def test_gate_state_dispatch_with_unmatched_kind_exits_unchanged() -> None:
    """An event kind outside the fold vocabulary exits the dispatch untouched."""
    trace = _Trace()
    trace.push(
        compile_operation_terminal(
            trace.ctx(),
            terminal_id=_DIGEST,
            terminal_result_class="success",
        )
    )
    gates: dict[int, GateTelemetryProjection] = {
        gate: GateTelemetryProjection(gate=gate) for gate in _GATE_IDS
    }
    rail_history: dict[int, list[RailTelemetryProjection]] = {gate: [] for gate in _GATE_IDS}
    _fold_gate_state(trace.events[0], gates, rail_history)
    assert all(gate.state == "not-started" for gate in gates.values())


def test_project_gate_history_returns_the_exact_gate() -> None:
    """project_gate_history projects one named gate from a folded stream."""
    trace = _Trace()
    trace.push(
        compile_certificate_refused(
            trace.ctx(),
            gate=5,
            citation=GateCitation(attempt=1, catalog_revision=1, catalog_manifest_id=_DIGEST),
            refusal_code="malformed-result-manifest",
            refusal_detail="catalog manifest failed its structural digest",
        )
    )
    gate_projection = project_gate_history(trace.events, 5)
    assert gate_projection.gate == 5
    assert gate_projection.state == "certificate-refused"
