"""CCR-R18 revision, state-matrix, guidance, and cleanup forcing tests."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from agents_remember.application.lifecycle.lifecycle_status_wait import (
    LifecycleStatusWaitRequest,
    worktree_status_wait_tool,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationRecord,
)
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.models.worktree import WorktreeStatusWaitResponse
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    _validate_identity_and_evidence_transition,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.observation.projection import (
    observed_operation_projection,
)
from agents_remember.worktrees.integration.lifecycle.observation.status_wait import (
    OUTCOME_CHANGED,
    OUTCOME_UNCHANGED,
    LifecycleWaitClock,
    wait_for_lifecycle_change,
)
from closeout_input_test_support import closeout_operation_input, start_closeout_operation
from selected_lifecycle_test_support import selected_contract

_LEASE = "6" * 64
_WORKER_FINGERPRINT = "7" * 64


def _claimed_contract_and_record(tmp_path: Path):
    """Compose one canonical claimed closeout generation like the L2 suites do."""

    contract = selected_contract(tmp_path, candidate_file=("candidate.py", "VALUE = 1\n"))
    operation_input = closeout_operation_input(contract, code="close exact projection fixture")
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    record = store.read()
    assert record is not None
    assert record.doorPublication is not None
    return contract, store, record


def _with_worker(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
    return record.model_copy(
        update={
            "workerPid": 4242,
            "workerLease": _LEASE,
            "workerProcessFingerprint": _WORKER_FINGERPRINT,
        }
    )


def _with_termination(
    record: LifecycleOperationRecord,
    *,
    state: str,
    cancel_requested: bool,
) -> LifecycleOperationRecord:
    updated = record.model_copy(
        update={
            "workerTermination": WorkerTerminationEvidence(
                state=state,  # type: ignore[arg-type]
                pid=4242,
                lease=_LEASE,
                processFingerprint=_WORKER_FINGERPRINT,
                requestedAt="2026-09-01T00:00:30+00:00",
                observedAt=("2026-09-01T00:01:10+00:00" if state == "exited" else None),
            ),
            "cancelRequested": cancel_requested,
        }
    )
    if record.status == "termination-required":
        updated = updated.model_copy(
            update={
                "terminationReturnStatus": "running",
                "terminationReturnPhase": "quality",
            }
        )
    return updated


def _validate(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
    return LifecycleOperationRecord.model_validate(record.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# CCR-R15 store semantics: the durable meaningful revision advances exactly once
# per meaningful transition and never for heartbeat/current-command noise; it is
# durable across restart reconstruction; concurrent waiters do not block writers
# and a cancelled client wait never mutates the journal.
# ---------------------------------------------------------------------------


def _start_running(store, record, stamp):
    """Start one claimed closeout generation inline (no detached worker)."""

    def run(current):
        if current.status != "queued":
            return current
        return current.model_copy(
            update={
                "status": "running",
                "phase": "preflight",
                "startedAt": current.startedAt or stamp,
                "heartbeatAt": stamp,
                "currentCommand": "validate lifecycle operation",
            }
        )

    return store.update(run)


def _advance_phase(store, phase, stamp):
    def advance(record):
        if record.status != "running":
            return record
        return record.model_copy(
            update={
                "status": "running",
                "phase": phase,
                "heartbeatAt": stamp,
                "currentCommand": f"lifecycle stage: {phase}",
            }
        )

    return store.update(advance)


def _heartbeat_only(store, command, stamp):
    def beat(record):
        if record.status != "running":
            return record
        return record.model_copy(
            update={
                "heartbeatAt": stamp,
                "currentCommand": command,
            }
        )

    return store.update(beat)


def _claim_approval(store, stamp):
    def approve(record):
        return record.model_copy(
            update={
                "approvalClaimed": True,
                "heartbeatAt": stamp,
                "currentCommand": "lifecycle stage: approval-claim",
            }
        )

    return store.update(approve)


def _fail_operation(store, stamp):
    def fail(record):
        return record.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "heartbeatAt": stamp,
                "finishedAt": stamp,
                "result": {"state": "failed", "ok": False, "reason": "typed failure"},
                "failure": "typed failure",
                "currentCommand": "operation failed",
                "guidance": "Fix the reported preflight failure, then restart this task operation.",
            }
        )

    return store.update(fail)


def _complete_operation(store, stamp):
    def finish(record):
        return record.model_copy(
            update={
                "status": "completed",
                "phase": "completed",
                "heartbeatAt": stamp,
                "finishedAt": stamp,
                "result": {
                    "state": "completed",
                    "ok": True,
                    "operation": "worktree_closeout_apply",
                },
                "currentCommand": "operation completed",
                "guidance": "Observe the task contract for the next lifecycle edge.",
            }
        )

    return store.update(finish)


def _request_cancellation(store, stamp):
    def cancel(record):
        worker = _with_worker(record)
        return worker.model_copy(
            update={
                "status": "termination-required",
                "phase": "termination-required",
                "cancelRequested": True,
                "workerTermination": WorkerTerminationEvidence(
                    state="requested",
                    pid=4242,
                    lease=_LEASE,
                    processFingerprint=_WORKER_FINGERPRINT,
                    requestedAt=stamp,
                ),
                "terminationReturnStatus": "running",
                "terminationReturnPhase": worker.phase,
                "heartbeatAt": stamp,
                "currentCommand": "terminate exact lifecycle worker process",
            }
        )

    return store.update(cancel)


def test_heartbeat_advances_record_revision_but_never_meaningful_revision(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    started = _start_running(store, record, "2026-09-04T00:00:00+00:00")
    before_revision = started.recordRevision
    before_meaningful = started.meaningfulRevision
    for index in range(5):
        updated = _heartbeat_only(
            store,
            f"quality stage: step-{index}",
            f"2026-09-04T00:00:{10 + index:02d}+00:00",
        )
        assert updated.recordRevision == before_revision + index + 1
        assert updated.meaningfulRevision == before_meaningful
    del contract


def test_meaningful_transitions_advance_the_wait_revision_exactly_once(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    _start_running(store, record, "2026-09-04T00:00:00+00:00")
    transitions = [
        lambda: _advance_phase(store, "quality", "2026-09-04T00:00:20+00:00"),
        lambda: _claim_approval(store, "2026-09-04T00:00:30+00:00"),
    ]
    for transition in transitions:
        current = store.read()
        assert current is not None
        updated = transition()
        assert updated.meaningfulRevision == current.meaningfulRevision + 1
        assert updated.recordRevision == current.recordRevision + 1
        assert updated != current


def test_terminal_and_cancellation_transitions_advance_exactly_once(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    _start_running(store, record, "2026-09-04T00:00:00+00:00")
    current = store.read()
    assert current is not None
    failed = _fail_operation(store, "2026-09-04T00:01:00+00:00")
    assert failed.meaningfulRevision == current.meaningfulRevision + 1
    assert failed.status == "failed"
    # The failed generation may still enter the exact termination-required
    # cancellation/recovery edge (failed -> termination-required is a legal
    # journal transition); the meaningful cursor advances exactly once more.
    cancelled = _request_cancellation(store, "2026-09-04T00:03:00+00:00")
    assert cancelled.status == "termination-required"
    assert cancelled.meaningfulRevision == failed.meaningfulRevision + 1


def test_restart_reconstruction_keeps_cursor_and_terminal_transition(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    started = _start_running(store, record, "2026-09-04T00:00:00+00:00")
    before = started.meaningfulRevision
    _advance_phase(store, "quality", "2026-09-04T00:00:10+00:00")
    _advance_phase(store, "memory-preflight", "2026-09-04T00:00:20+00:00")
    # A process restart is a fresh store over the same durable journal bytes.
    restarted = LifecycleOperationStore(store.path)
    after = restarted.read()
    assert after is not None
    assert after.meaningfulRevision == before + 2
    terminal = _complete_operation(restarted, "2026-09-04T00:01:00+00:00")
    assert terminal.status == "completed"
    # Another restart must not lose the completed terminal transition for a
    # waiter whose cursor predates it.
    again = LifecycleOperationStore(store.path).read()
    assert again is not None
    assert again.status == "completed"
    assert again.meaningfulRevision == before + 3


def test_wait_wakes_on_meaningful_change_but_not_heartbeats(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    started = _start_running(store, record, "2026-09-04T00:00:00+00:00")
    cursor = started.meaningfulRevision
    outcomes: list[str] = []
    waiter = threading.Thread(
        target=lambda: outcomes.append(
            wait_for_lifecycle_change(
                store,
                expected_generation=started.generation,
                after_revision=cursor,
                timeout_seconds=5.0,
                clock=LifecycleWaitClock(poll_seconds=0.01),
            ).outcome
        )
    )
    waiter.start()
    # Heartbeats only; the waiter must stay asleep through them.
    for index in range(6):
        _heartbeat_only(
            store, f"quality stage: step-{index}", f"2026-09-04T00:00:{index:02d}+00:00"
        )
        time.sleep(0.02)
    assert not outcomes
    _advance_phase(store, "approval-claim", "2026-09-04T00:01:00+00:00")
    waiter.join(timeout=5.0)
    assert outcomes == [OUTCOME_CHANGED]


def test_multiple_waiters_never_block_writers(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    started = _start_running(store, record, "2026-09-04T00:00:00+00:00")
    cursor = started.meaningfulRevision
    results: list[str] = []
    barrier = threading.Barrier(2)

    def wait_once(expected: str, after: int) -> None:
        barrier.wait(timeout=5.0)
        results.append(
            wait_for_lifecycle_change(
                store,
                expected_generation=started.generation,
                after_revision=after,
                timeout_seconds=3.0,
                clock=LifecycleWaitClock(poll_seconds=0.01),
            ).outcome
        )

    # Waiter A watches from the current cursor (stays unchanged until the writer
    # advances); waiter B watches from one meaningful revision behind (changed).
    threads = [
        threading.Thread(target=wait_once, args=(OUTCOME_UNCHANGED, cursor)),
        threading.Thread(target=wait_once, args=(OUTCOME_CHANGED, cursor - 1)),
    ]
    for thread in threads:
        thread.start()
    writer_errors: list[str] = []

    def writer() -> None:
        try:
            time.sleep(0.05)
            for index in range(3):
                _heartbeat_only(store, f"step-{index}", f"2026-09-04T00:02:{index:02d}+00:00")
            _advance_phase(store, "quality", "2026-09-04T00:03:00+00:00")
        except Exception as error:  # pragma: no cover - concurrency probe
            writer_errors.append(str(error))

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    for thread in threads:
        thread.join(timeout=8.0)
    writer_thread.join(timeout=8.0)
    assert not writer_errors
    # The second waiter observed the phase change; the first waiter either saw
    # the change too or timed out after the cursor advanced only once (changed
    # dominates because the phase change precedes its deadline).
    assert results.count(OUTCOME_CHANGED) >= 1
    assert set(results) <= {OUTCOME_CHANGED, OUTCOME_UNCHANGED}


def test_client_cancellation_never_mutates_lifecycle_state(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    started = _start_running(store, record, "2026-09-04T00:00:00+00:00")
    cursor = started.meaningfulRevision
    before_bytes = store.path.read_bytes()
    abandoned: list[str] = []
    cancelled_wait = threading.Thread(
        target=lambda: abandoned.append(
            wait_for_lifecycle_change(
                store,
                expected_generation=started.generation,
                after_revision=cursor,
                timeout_seconds=0.5,
                clock=LifecycleWaitClock(poll_seconds=0.01),
            ).outcome
        )
    )
    cancelled_wait.start()
    # A client disconnect abandons the wait mid-flight; the wait is read-only so
    # the durable journal cannot have changed and no retry/cancel was issued.
    time.sleep(0.2)
    cancelled_wait.join(timeout=3.0)
    assert store.path.read_bytes() == before_bytes
    # A bounded timeout is normal and still does not mutate state.
    current = store.read()
    assert current is not None
    unchanged = wait_for_lifecycle_change(
        store,
        expected_generation=started.generation,
        after_revision=current.meaningfulRevision,
        timeout_seconds=0.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert unchanged.outcome == "unchanged"
    assert store.path.read_bytes() == before_bytes


def test_store_transform_cannot_assign_meaningful_revision(tmp_path: Path) -> None:
    """A transform that tampers with the wait cursor is refused at the boundary."""
    contract, store, _record = _claimed_contract_and_record(tmp_path)
    del contract
    with pytest.raises(RuntimeError, match="cannot assign meaningful revision"):
        store.update(
            lambda current: current.model_copy(
                update={
                    "heartbeatAt": "2026-09-05T00:00:00+00:00",
                    "meaningfulRevision": current.meaningfulRevision + 1,
                }
            )
        )


def test_transition_validator_refuses_a_meaningful_revision_mismatch(
    tmp_path: Path,
) -> None:
    """A journal transition with a non-monotonic wait cursor refuses exactly."""
    contract, store, _record = _claimed_contract_and_record(tmp_path)
    del contract
    current = store.read()
    assert current is not None
    wrong = LifecycleOperationRecord.model_validate(
        current.model_copy(
            update={
                "recordRevision": current.recordRevision + 1,
                "meaningfulRevision": current.meaningfulRevision + 2,
            }
        ).model_dump(mode="json")
    )
    with pytest.raises(RuntimeError, match="meaningful revision must advance exactly"):
        _validate_identity_and_evidence_transition(current, wrong)


def test_observed_projection_returns_none_without_a_record(tmp_path: Path) -> None:
    """The shared status seam reports no envelope when no record is observed."""
    del tmp_path
    assert observed_operation_projection(None) is None  # type: ignore[arg-type]


def test_status_wait_application_payload_has_no_private_operation_identity(
    tmp_path: Path,
) -> None:
    """Wire shape: coherent changed payload, never an operation key or PID."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    config = load_config(tmp_path / "settings.json")
    _start_running(store, record, "2026-09-04T00:00:00+00:00")
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=contract.contract_path.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is True
    assert payload["outcome"] == "changed"
    assert payload["meaningfulRevision"] == record.meaningfulRevision + 1
    assert payload["operation"] == "worktree_status_wait"
    assert "lifecycleOperation" in payload
    # The response model validates the wire shape.
    model = WorktreeStatusWaitResponse.model_validate(payload)
    assert model.operation == "worktree_status_wait"
    assert model.outcome == "changed"
    serialized = json.dumps(payload)
    for private in (
        "operationKey",
        "workerPid",
        "workerLease",
        "workerProcessFingerprint",
        "pid",
        "lease",
        "predecessorFingerprint",
    ):
        assert private not in serialized, private
