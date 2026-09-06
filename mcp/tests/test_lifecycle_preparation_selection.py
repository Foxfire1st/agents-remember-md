"""Real selected objects/Git refs and journal CAS for unfinished private work.

Command-start fixtures deliberately do not launch Git preparation: unknown means
there is no acknowledgement of an invocation, never a claimed successful output.
The report fixtures are consumed by the actual certificate producers/readers.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agents_remember.application.lifecycle.lifecycle_operation_worker import (
    OperationRuntime,
    terminal_operation_record,
)
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.git_command import preparation_command
from agents_remember.models.lifecycles.preparation import CloseoutPreparationIntent
from agents_remember.models.lifecycles.preparation_state import (
    OperationPreparationState,
    PreparationCommand,
    PreparationCommandTerminal,
    SelectedPreparation,
)
from agents_remember.worktrees.integration.closeout.certification.admission import (
    prepare_closeout_certification,
    select_initial_certification,
)
from agents_remember.worktrees.integration.closeout.certification.execution import (
    current_certification_handoff,
)
from agents_remember.worktrees.integration.closeout.preparation.code_output import (
    select_code_preparation,
)
from agents_remember.worktrees.integration.closeout.preparation.private_execution import (
    private_git_binding,
)
from agents_remember.worktrees.integration.closeout.preparation_selection import (
    begin_preparation_command,
    observe_preparation_command,
    require_preparation_logical_refs,
)
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    closeout_generation_retained,
    closeout_recovery_phase,
)
from agents_remember.worktrees.integration.lifecycle.generation.resume import (
    requeued_same_generation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_errors import (
    LifecycleControlError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_evidence import (
    prove_cancellable_git,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location_errors import (
    LifecycleOperationLocationError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    operation_projection,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.worker.launch import launch_or_fail
from agents_remember.worktrees.integration.lifecycle.worker.state import project_worker_exit
from agents_remember.worktrees.integration.lifecycle.worker.termination import (
    worker_process_fingerprint,
)
from agents_remember.worktrees.integration.terminal_enclosure_archive import (
    _require_archivable_operation,
)
from agents_remember.worktrees.modules.git import require_git, worktree_candidate_tree
from agents_remember.worktrees.modules.quality.certification_records import certificate_store
from closeout_fixture_test_support import selected_fixture
from closeout_input_test_support import closeout_operation_input
from pydantic import ValidationError
from test_closeout_queue import MASTER_A
from test_operation_certification_selection import _Fixture, _queued, _select_reports


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _fixture(root: Path) -> _Fixture:
    configured = selected_fixture(root, memory_mode="internal")
    contract = configured.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=configured.config_path)
    frozen = prepare_closeout_certification(
        contract,
        operation_input,
        None,
        candidate_tree=worktree_candidate_tree(contract.code_worktree, root / "candidate.index"),
    )
    assert frozen is not None
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    # Private preparation must remain cancellable even without published-mutation cells.
    queued = _queued(contract, operation_input, frozen.prepared.candidateTree).model_copy(
        update={"mutationEvidence": {}}
    )
    record, created = store.create(queued)
    assert created
    record = select_initial_certification(contract, store, record, frozen)
    return _select_reports(
        _Fixture(contract, operation_input, store, record, frozen), root / "original-code-reports"
    )


@pytest.fixture
def running(tmp_path: Path) -> Iterator[tuple[_Fixture, subprocess.Popen[bytes]]]:
    fixture = _fixture(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        fingerprint = worker_process_fingerprint(process.pid)
        assert fingerprint is not None
        record = fixture.store.update(
            lambda current: current.model_copy(
                update={
                    "status": "running",
                    "phase": "preflight",
                    "startedAt": _stamp(),
                    "workerPid": process.pid,
                    "workerLease": hashlib.sha256(f"fixture:{process.pid}".encode()).hexdigest(),
                    "workerProcessFingerprint": fingerprint,
                }
            )
        )
        yield replace(fixture, record=record), process
    finally:
        process.communicate(timeout=10)


def _selected(fixture: _Fixture) -> tuple[_Fixture, CloseoutPreparationIntent]:
    selected = select_code_preparation(
        current_certification_handoff(fixture.contract, fixture.record, fixture.store)
    )
    return replace(fixture, record=selected.handoff.record), selected.intent


def _command(fixture: _Fixture, intent: CloseoutPreparationIntent) -> PreparationCommand:
    record = fixture.record
    assert record.workerPid and record.workerLease and record.workerProcessFingerprint
    assert intent.privateRoot is not None
    selected = select_code_preparation(
        current_certification_handoff(fixture.contract, record, fixture.store)
    )
    binding = private_git_binding(selected.intent)
    binding.private_root.parent.mkdir(parents=True, exist_ok=True)
    planned = preparation_command(binding, "create")
    return PreparationCommand(
        kind="create",
        cwd=planned.cwd.as_posix(),
        argv=planned.argv,
        workerPid=record.workerPid,
        workerLease=record.workerLease,
        workerProcessFingerprint=record.workerProcessFingerprint,
        startedAt=_stamp(),
    )


def _assert_unpublished(fixture: _Fixture) -> None:
    record = fixture.store.read()
    assert record is not None
    assert record.mutationEvidence == {}
    assert record.recoveryCommits is None
    assert not record.approvalClaimed and not record.irreversibleBoundaryEntered
    assert record.closeoutFinalizedContractSha256 is None
    assert record.result is None


def test_unknown_private_command_retains_original_intent_and_cannot_restart(running) -> None:
    fixture, _process = running
    fixture, intent = _selected(fixture)
    assert closeout_generation_retained(fixture.record)
    command = _command(fixture, intent)
    started = begin_preparation_command(fixture.contract, fixture.store, fixture.record, command)
    assert intent.privateRoot is not None
    assert not Path(intent.privateRoot).exists()
    terminal = PreparationCommandTerminal(
        outcome="unknown", observedAt=_stamp(), detail="No invocation acknowledgement was observed."
    )
    observed = observe_preparation_command(
        fixture.contract, fixture.store, started, command, terminal
    )
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as error:
        begin_preparation_command(fixture.contract, fixture.store, observed, command)
    assert error.value.findings[0]["code"] == "preparation-command-already-started"
    assert fixture.store.path.read_bytes() == before
    assert observed.preparation is not None
    assert observed.preparation.legs[0].commands[-1].terminal == terminal
    _assert_unpublished(fixture)


def test_stale_start_and_cancelled_start_cannot_launch_private_work(running) -> None:
    fixture, _process = running
    fixture, intent = _selected(fixture)
    command = _command(fixture, intent)
    stopped = fixture.store.update(
        lambda record: record.model_copy(update={"cancelRequested": True})
    )
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as error:
        begin_preparation_command(fixture.contract, fixture.store, fixture.record, command)
    assert error.value.findings[0]["code"] == "preparation-selection-stale"
    with pytest.raises(RuntimeError, match="current worker owner"):
        begin_preparation_command(fixture.contract, fixture.store, stopped, command)
    assert fixture.store.path.read_bytes() == before
    assert intent.privateRoot is not None
    assert not Path(intent.privateRoot).exists()
    _assert_unpublished(fixture)


def test_cancellation_reopens_refs_without_creating_mutation_evidence_and_retains_work(
    running,
) -> None:
    fixture, process = running
    fixture, intent = _selected(fixture)
    command = _command(fixture, intent)
    started = begin_preparation_command(fixture.contract, fixture.store, fixture.record, command)
    process.communicate(timeout=10)
    released = fixture.store.update(project_worker_exit)
    assert released.workerPid is None
    assert released.workerTermination is not None and released.workerTermination.state == "exited"
    evidence, unchanged = prove_cancellable_git(fixture.store, released, publish=True)
    assert unchanged == released
    assert evidence.workerExitProven
    assert evidence.expected["preparation:code:head"] == intent.expectedOldCommit
    assert "mutationState" not in evidence.expected
    generic = evidence.model_copy(
        update={
            "expected": {"mutationState": "not-applicable"},
            "observed": {"mutationState": "not-applicable"},
        }
    )
    with pytest.raises(ValidationError, match="every selected preparation intent"):
        fixture.store.update(
            lambda record: record.model_copy(
                update={
                    "status": "cancelled",
                    "phase": "cancelled",
                    "cancellationEvidence": generic,
                }
            )
        )
    cancelled = fixture.store.update(
        lambda record: record.model_copy(
            update={
                "status": "cancelled",
                "phase": "cancelled",
                "generationDisposition": "cancelled",
                "cancelRequested": True,
                "cancellationEvidence": evidence,
                "finishedAt": _stamp(),
            }
        )
    )
    # Late readback retains the original command identity and grants no new action.
    assert started.preparation is not None
    cancelled = observe_preparation_command(
        fixture.contract,
        fixture.store,
        cancelled,
        started.preparation.legs[0].commands[-1],
        PreparationCommandTerminal(outcome="unknown", observedAt=_stamp()),
    )
    assert closeout_generation_retained(cancelled)
    before = fixture.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="retention disposition"):
        fixture.store.update(
            lambda record: record.model_copy(update={"generationDisposition": "retired"})
        )
    with pytest.raises(RuntimeError, match="retention disposition"):
        fixture.store.replace_terminal(
            _queued(
                fixture.contract, fixture.operation_input, fixture.frozen.prepared.candidateTree
            )
        )
    with pytest.raises(LifecycleOperationLocationError) as refusal:
        _require_archivable_operation(
            cancelled, operation="worktree_cleanup", current=True, name="closeout-operation.json"
        )
    assert refusal.value.status == "terminal-archive-operation-preparation-retained"
    assert fixture.store.path.read_bytes() == before
    _assert_unpublished(fixture)


def test_changed_logical_ref_and_corrupt_selected_intent_refuse_cancellation(running) -> None:
    fixture, _process = running
    fixture, intent = _selected(fixture)
    root = fixture.contract.code_worktree
    original_ref = intent.logicalRef
    require_git(root, ["branch", "preparation-other", intent.expectedOldCommit])
    require_git(root, ["symbolic-ref", "HEAD", "refs/heads/preparation-other"])
    try:
        with pytest.raises(CertificationContractError) as error:
            prove_cancellable_git(fixture.store, fixture.record, publish=False)
        assert error.value.findings[0]["code"] == "preparation-logical-refs-changed"
    finally:
        require_git(root, ["symbolic-ref", "HEAD", original_ref])
    assert require_preparation_logical_refs(fixture.contract, fixture.record)
    objects = certificate_store(fixture.contract.worktree_group)
    path = objects.exact_path("preparation-intent", intent.intentDigest)
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    try:
        with pytest.raises(CertificationContractError) as error:
            prove_cancellable_git(fixture.store, fixture.record, publish=False)
        assert error.value.findings[0]["code"] == "certificate-object-address-mismatch"
    finally:
        path.write_bytes(original)
    _assert_unpublished(fixture)


def test_private_state_cannot_arrive_completed_or_combine_with_publication(running) -> None:
    fixture, _process = running
    fixture, intent = _selected(fixture)
    record = fixture.record
    before = fixture.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="unfinished work"):
        fixture.store.update(
            lambda current: current.model_copy(update={"status": "completed", "phase": "completed"})
        )
    with pytest.raises(RuntimeError, match="cannot be cleared"):
        fixture.store.update(lambda current: current.model_copy(update={"preparation": None}))
    assert fixture.store.path.read_bytes() == before
    selected = record.preparation
    assert selected is not None
    command = _command(fixture, intent)
    proposal = selected.model_copy(
        update={"legs": (selected.legs[0].model_copy(update={"commands": (command,)}),)}
    )
    with pytest.raises(RuntimeError, match="cannot publish mutation or approval"):
        fixture.store.update(
            lambda current: current.model_copy(
                update={"preparation": proposal, "approvalClaimed": True}
            )
        )
    assert fixture.store.path.read_bytes() == before
    # A fresh generation cannot inject a preselected private intent at creation.
    fresh = _queued(
        fixture.contract, fixture.operation_input, fixture.frozen.prepared.candidateTree
    )
    with pytest.raises(RuntimeError, match="after generation creation"):
        fixture.store.create(
            fresh.model_copy(
                update={"certification": record.certification, "preparation": selected}
            )
        )


def test_preparation_wire_refuses_duplicate_command_order_and_wrong_owner(running) -> None:
    fixture, _process = running
    fixture, intent = _selected(fixture)
    record = fixture.record
    command = _command(fixture, intent)
    assert record.preparation is not None
    selected = record.preparation.legs[0]
    with pytest.raises(ValidationError, match="unique ordered prefix"):
        SelectedPreparation.model_validate(
            selected.model_copy(update={"commands": (command, command)}).model_dump(mode="json")
        )
    wrong = OperationPreparationState(
        operationKey="f" * 64, generation=record.generation, legs=(selected,)
    )
    before = fixture.store.path.read_bytes()
    with pytest.raises(ValidationError, match="exact closeout generation"):
        fixture.store.update(lambda current: current.model_copy(update={"preparation": wrong}))
    assert fixture.store.path.read_bytes() == before


def test_private_recovery_phase_survives_failure_requeue_launch_and_public_projection(
    running,
) -> None:
    fixture, process = running
    assert closeout_recovery_phase(fixture.record) is None
    fixture, _intent = _selected(fixture)
    retained = fixture.record.preparation
    assert retained is not None
    failed = fixture.store.update(
        lambda current: terminal_operation_record(
            current,
            {
                "state": "private-preparation-interrupted",
                "summary": "Inspect the selected private intent.",
            },
            ok=False,
            stamp=_stamp(),
        )
    )
    assert (failed.status, failed.phase) == ("input-required", "recovering-private-preparation")
    assert failed.guidance is not None and "has not consumed its approval" in failed.guidance
    process.communicate(timeout=10)
    released = fixture.store.update(project_worker_exit)
    assert released.workerPid is None
    queued, matched = fixture.store.resume_generation(
        requeued_same_generation, expected_generation=released.generation
    )
    assert matched
    assert queued.phase == "recovering-private-preparation"
    assert queued.preparation == retained and queued.attempt == failed.attempt + 1

    launches = []

    def unavailable(_contract, record) -> None:
        launches.append((record.operationKey, record.generation))
        raise OSError("isolated launcher failure")

    with pytest.raises(LifecycleControlError) as error:
        launch_or_fail(fixture.contract, queued, unavailable, fixture.store)
    assert error.value.status == "lifecycle-worker-launch-failed"
    assert launches == [(queued.operationKey, queued.generation)]
    parked = fixture.store.read()
    assert parked is not None
    assert parked.phase == "recovering-private-preparation" and parked.status == "input-required"
    public = operation_projection(parked, contract=fixture.contract)
    assert public.phase == "recovering-private-preparation"
    assert public.status == "input-required"
    assert public.approval is not None and public.approval.state == "unclaimed"

    _queued_again, matched = fixture.store.resume_generation(
        requeued_same_generation, expected_generation=parked.generation
    )
    assert matched
    started = OperationRuntime(fixture.store).start()
    assert (started.status, started.phase) == ("running", "recovering-private-preparation")
    assert started.preparation == retained
    assert not started.approvalClaimed and not started.irreversibleBoundaryEntered
    assert not started.mutationEvidence and started.recoveryCommits is None
