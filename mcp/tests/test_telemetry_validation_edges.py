"""CCR-R16-v3 telemetry validator edge streams (split for file-size bound)."""

from __future__ import annotations

import pytest
from agents_remember.certification.telemetry import (
    compile_admission_started,
    compile_finalization_boundary_resumed,
    compile_finalization_completed,
    compile_finalization_started,
    compile_gate_catalog_complete,
    compile_operation_terminal,
)
from agents_remember.certification.telemetry import validation as _validation
from agents_remember.certification.telemetry.models import PredecessorBoundary
from test_telemetry_validation import (
    _DIGEST,
    _admit,
    _authority_stub,
    _certificate_stub,
    _codes,
    _gate_plan,
    _manifest,
    _new_trace,
    _results,
    _run_gate,
)


def test_gate_result_terminal_unavailable_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    trace.push(
        compile_operation_terminal(
            trace.ctx(),
            terminal_id=_DIGEST,
            terminal_result_class="gate-result",
            gate_result_manifest_id="b" * 64,
        )
    )
    assert "terminal-result-unavailable" in _codes(trace)


def test_finalization_without_start_or_gate_five_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    trace.push(
        compile_finalization_boundary_resumed(
            trace.ctx(),
            journal_leg="code-commit",
            journal_state_digest=_DIGEST,
            predecessor_revision=len(trace.events),
        )
    )
    trace.push(
        compile_finalization_completed(
            trace.ctx(), authority=_authority_stub(), finalization_digest=_DIGEST
        )
    )
    trace.push(
        compile_finalization_started(
            trace.ctx(),
            gate_five_certificate=_certificate_stub(5, _DIGEST),
            authority=_authority_stub(),
        )
    )
    codes = _codes(trace)
    assert "finalization-resume-without-start" in codes
    assert "finalization-completed-without-start" in codes
    assert "finalization-green-gate-five-missing" in codes


def test_catalog_without_any_gate_start_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    gate_five_plan = _gate_plan(trace, 5)
    manifest = _manifest(
        trace.plan, gate_five_plan, list(_results(gate_five_plan)), altitude="certifying"
    )
    trace.push(
        compile_gate_catalog_complete(trace.ctx(), manifest=manifest, attempt=1, catalog_revision=1)
    )
    assert "catalog-without-gate-start" in _codes(trace)


def test_non_canonical_finding_code_raises() -> None:
    with pytest.raises(ValueError):
        _validation._finding("bad code!", "execution", "detail")


def test_has_earlier_exhausts_an_empty_stream() -> None:
    trace = _new_trace()
    current = compile_admission_started(
        trace.ctx(),
        predecessor=PredecessorBoundary(zeroStart=True),
    )
    assert _validation._has_earlier((), current, "gate-started", 1, 1) is False
