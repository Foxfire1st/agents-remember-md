"""CCR-R16-v3 durable append / CAS owner and instrumentation-only replay."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CandidateIdentity, GatePlan
from agents_remember.certification.telemetry import (
    DurableTelemetryStore,
    TelemetryEvent,
    TelemetryJournalEntry,
    TelemetryReplay,
    TelemetryStorePolicy,
    compile_diagnostic_started,
    compile_gate_started,
)
from agents_remember.certification.telemetry import (
    store as telemetry_store,
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


def _diag_ctx(revision: int) -> TelemetryExecutionContext:
    return TelemetryExecutionContext(
        executionKind="diagnostic-run",
        executionId="diag-1",
        eventRevision=revision,
        diagnosticNonce="f" * 32,
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


def _diagnostic_starter(revision: int) -> TelemetryEvent:
    return compile_diagnostic_started(
        _diag_ctx(revision),
        plan_digest=_DIGEST,
        plan_version="scenario-2.1.0",
        rail_count=2,
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


def test_separate_closeout_and_diagnostic_envelopes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_gate_starter(1))
    store.append(_diagnostic_starter(1))
    assert len(store.read("closeout-generation", "gen-13-closeout")) == 1
    assert len(store.read("diagnostic-run", "diag-1")) == 1
    assert (tmp_path / "telemetry" / "closeout-generation").is_dir()
    assert (tmp_path / "telemetry" / "diagnostic-run").is_dir()


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


def test_replay_of_missing_execution_is_empty_instrumentation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    replay = store.replay("closeout-generation", "missing-execution")
    assert replay.eventCount == 0
    assert replay.instrumentationOnly is True
    assert (tmp_path / "telemetry" / "closeout-generation" / "missing-execution").exists() is False


def test_invalid_execution_id_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CertificationContractError) as error:
        store.replay("closeout-generation", "../escape")
    assert "telemetry-execution-id-invalid" in {str(item["code"]) for item in error.value.findings}


def test_invalid_envelope_kind_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CertificationContractError) as error:
        store.read("bogus-envelope", "gen-13-closeout")  # type: ignore[arg-type]
    assert "telemetry-envelope-invalid" in {str(item["code"]) for item in error.value.findings}


def test_capacity_limit_is_enforced(tmp_path: Path) -> None:
    store = DurableTelemetryStore(
        tmp_path / "telemetry",
        TelemetryStorePolicy(
            scopeId="bounded",
            maxEventsPerExecution=2,
            maxBytes=10_000_000,
            reclamationOwner="owner",
        ),
    )
    store.append(_gate_starter(1))
    store.append(_gate_starter(2))
    with pytest.raises(CertificationContractError) as error:
        store.append(_gate_starter(3))
    assert "telemetry-store-capacity-exceeded" in {
        str(item["code"]) for item in error.value.findings
    }


def test_journal_entry_digest_must_match_event(tmp_path: Path) -> None:
    event = _gate_starter(1)
    entry = TelemetryJournalEntry(
        event=event,
        eventDigest=content_digest(event),
        predecessorDigest="",
    )
    assert entry.eventDigest == content_digest(event)
    with pytest.raises(ValueError):
        TelemetryJournalEntry(
            event=event,
            eventDigest="0" * 64,
            predecessorDigest="",
        )


def test_append_cas_identical_bytes_returns_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    event = _gate_starter(1)
    first_path = store.append(event)
    # Simulate the concurrent-appender race window: the journal snapshot is empty
    # while the exact entry already exists on disk with identical canonical bytes.
    monkeypatch.setattr(store, "_read_entries", lambda envelope: ())
    assert store.append(event) == first_path
    assert [path.name for path in tmp_path.rglob("*.json")] == ["1.json"]


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


def test_append_refuses_readback_mismatch_after_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _tamper_on_commit(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"bytes-changed-before-readback")

    monkeypatch.setattr(telemetry_store, "atomic_write_bytes", _tamper_on_commit)
    store = _store(tmp_path)
    with pytest.raises(CertificationContractError) as error:
        store.append(_gate_starter(1))
    assert "telemetry-event-readback-mismatch" in {
        str(item["code"]) for item in error.value.findings
    }


def test_entry_path_rejects_unsupported_envelope_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CertificationContractError) as error:
        store._entry_path("bogus-envelope", "gen-13-closeout", 1)  # type: ignore[arg-type]
    assert "telemetry-envelope-invalid" in {str(item["code"]) for item in error.value.findings}


def test_entry_path_rejects_nonpositive_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CertificationContractError) as error:
        store._entry_path("closeout-generation", "gen-13-closeout", 0)
    assert "telemetry-event-revision-invalid" in {
        str(item["code"]) for item in error.value.findings
    }


def test_read_refuses_entry_whose_stored_bytes_are_not_canonical(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_gate_starter(1))
    entry_path = tmp_path / "telemetry" / "closeout-generation" / "gen-13-closeout" / "1.json"
    reformatted = json.dumps(json.loads(entry_path.read_text(encoding="utf-8")), indent=2)
    entry_path.write_text(reformatted, encoding="utf-8")
    with pytest.raises(CertificationContractError) as error:
        store.read("closeout-generation", "gen-13-closeout")
    assert "telemetry-event-address-mismatch" in {
        str(item["code"]) for item in error.value.findings
    }


def test_capacity_check_refuses_directory_among_stored_entries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_gate_starter(1))
    envelope = tmp_path / "telemetry" / "closeout-generation" / "gen-13-closeout"
    (envelope / "stray.json").mkdir()
    with pytest.raises(CertificationContractError) as error:
        store.append(_gate_starter(2))
    assert "telemetry-entry-invalid" in {str(item["code"]) for item in error.value.findings}


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


def test_read_refuses_directory_at_revision_address(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_gate_starter(1))
    envelope = tmp_path / "telemetry" / "closeout-generation" / "gen-13-closeout"
    (envelope / "2.json").mkdir()
    with pytest.raises(CertificationContractError) as error:
        store.read("closeout-generation", "gen-13-closeout")
    assert "telemetry-entry-unsafe" in {str(item["code"]) for item in error.value.findings}


def test_read_regular_file_missing_not_ok_is_refused(tmp_path: Path) -> None:
    missing = tmp_path / "telemetry" / "absent-execution" / "1.json"
    assert telemetry_store._read_regular_file(missing, missing_ok=True) is None
    with pytest.raises(CertificationContractError) as error:
        telemetry_store._read_regular_file(missing, missing_ok=False)
    assert "telemetry-entry-missing" in {str(item["code"]) for item in error.value.findings}
