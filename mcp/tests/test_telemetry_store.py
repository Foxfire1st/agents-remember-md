"""CCR-R16-v3 durable append / CAS owner and instrumentation-only replay."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest
from agents_remember.certification.models import CandidateIdentity, GatePlan
from agents_remember.certification.telemetry import (
    DurableTelemetryStore,
    TelemetryEvent,
    TelemetryReplay,
    TelemetryStorePolicy,
    compile_gate_started,
)
from agents_remember.certification.telemetry.adapters import TelemetryExecutionContext
from agents_remember.errors import CertificationContractError

_DIGEST = "a" * 64
_CANDIDATE = CandidateIdentity(kind="content-digest", value="c" * 64)

_TIMESTAMP = "2026-09-03T00:00:00+02:00"


def _policy(tmp_path: Path, *, max_events: int = 64) -> TelemetryStorePolicy:
    return TelemetryStorePolicy(
        scopeId="test-closeout-telemetry",
        maxEventsPerExecution=max_events,
        maxBytes=10_000_000,
        reclamationOwner="test-owner",
    )


def _store(tmp_path: Path) -> DurableTelemetryStore:
    return DurableTelemetryStore(tmp_path / "telemetry", _policy(tmp_path))


def _closeout_ctx(
    revision: int, execution_id: str = "gen-13-closeout"
) -> TelemetryExecutionContext:
    return TelemetryExecutionContext(
        executionKind="closeout-generation",
        executionId=execution_id,
        eventRevision=revision,
        operationKind="closeout",
        generation=13,
        candidate=_CANDIDATE,
        profileId="portable-ci",
        occurredAt=_TIMESTAMP,
    )


def _gate_starter(revision: int) -> TelemetryEvent:
    return compile_gate_started(
        _closeout_ctx(revision),
        gate_plan=_gate_plan_stub(),  # type: ignore[arg-type]
        attempt=1,
        green_predecessors=[],
    )


def _gate_plan_stub() -> object:
    """One deterministic Gate-1 plan identity; the store never runs its rails."""

    return cast(
        GatePlan,
        type("GatePlanStub", (), {"gate": 1, "planDigest": _DIGEST})(),
    )


def test_append_and_read_round_trip_preserves_exact_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _gate_starter(1)
    second = _gate_starter(2)
    first_path = store.append(first)
    second_path = store.append(second)
    assert first_path.name == "1.json"
    assert second_path.name == "2.json"
    entries = store.read("closeout-generation", "gen-13-closeout")
    assert len(entries) == 2
    assert entries[0].event == first
    assert entries[1].event == second
    assert entries[1].predecessorDigest == entries[0].eventDigest
    assert entries[0].predecessorDigest == ""


def test_append_enforces_monotonic_event_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_gate_starter(1))
    with pytest.raises(CertificationContractError) as error:
        store.append(_gate_starter(3))
    assert "telemetry-event-revision-mismatch" in {
        str(item["code"]) for item in error.value.findings
    }


def test_tampered_journal_entry_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_gate_starter(1))
    path = store._entry_path("closeout-generation", "gen-13-closeout", 1)
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(CertificationContractError) as error:
        store.append(_gate_starter(2))
    assert "telemetry-event-invalid" in {str(item["code"]) for item in error.value.findings}


def test_read_refuses_journal_gap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_gate_starter(1))
    store.append(_gate_starter(2))
    store.append(_gate_starter(3))
    (tmp_path / "telemetry" / "closeout-generation" / "gen-13-closeout" / "2.json").unlink()
    with pytest.raises(CertificationContractError) as error:
        store.read("closeout-generation", "gen-13-closeout")
    assert "telemetry-event-chain-gap" in {str(item["code"]) for item in error.value.findings}


def test_read_refuses_broken_predecessor_chain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_gate_starter(1))
    store.append(_gate_starter(2))
    entry_path = tmp_path / "telemetry" / "closeout-generation" / "gen-13-closeout" / "2.json"
    payload = entry_path.read_text(encoding="utf-8")
    entry_path.write_text(
        re.sub(
            r'"predecessorDigest":"[0-9a-f]{64}"',
            f'"predecessorDigest":"{"0" * 64}"',
            payload,
            count=1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(CertificationContractError) as error:
        store.read("closeout-generation", "gen-13-closeout")
    assert "telemetry-event-chain-mismatch" in {str(item["code"]) for item in error.value.findings}


def test_replay_is_read_only_instrumentation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_gate_starter(1))
    snapshot = sorted(path.name for path in (tmp_path / "telemetry").rglob("*.json"))
    replay = store.replay("closeout-generation", "gen-13-closeout")
    assert isinstance(replay, TelemetryReplay)
    assert replay.instrumentationOnly is True
    assert replay.eventCount == 1
    assert replay.entries[0].event.eventKind == "gate-started"
    assert sorted(path.name for path in (tmp_path / "telemetry").rglob("*.json")) == snapshot


def test_append_cas_collision_refuses_different_bytes_at_same_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.append(_gate_starter(1))
    # Same envelope and revision but different payload bytes, as if another
    # appender published a competing event inside the race window.
    monkeypatch.setattr(store, "_read_entries", lambda envelope: ())
    competing = compile_gate_started(
        _closeout_ctx(1),
        gate_plan=_gate_plan_stub(),  # type: ignore[arg-type]
        attempt=2,
        green_predecessors=[],
    )
    with pytest.raises(CertificationContractError) as error:
        store.append(competing)
    assert "telemetry-event-cas-collision" in {str(item["code"]) for item in error.value.findings}


def test_append_enforces_byte_capacity_limit(tmp_path: Path) -> None:
    store = DurableTelemetryStore(
        tmp_path / "telemetry",
        TelemetryStorePolicy(
            scopeId="byte-bounded",
            maxEventsPerExecution=64,
            maxBytes=100,
            reclamationOwner="owner",
        ),
    )
    with pytest.raises(CertificationContractError) as error:
        store.append(_gate_starter(1))
    assert "telemetry-store-capacity-exceeded" in {
        str(item["code"]) for item in error.value.findings
    }
