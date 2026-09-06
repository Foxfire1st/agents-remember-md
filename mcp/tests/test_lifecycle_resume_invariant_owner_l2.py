"""Store-choke forcing for the invariant owner shared by update and resume."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.models.lifecycles.operation import (
    IntegrationPublicationIntent,
    LifecycleOperationRecord,
    LifecycleOperationRecoveryCommits,
    OrganizationalCompletionRepairEvidence,
)
from agents_remember.models.lifecycles.termination import LifecycleCancellationEvidence
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.integration.lifecycle.generation.resume import (
    requeued_same_generation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_errors import (
    LifecycleControlError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.worker.state import reconcile_worker_exit
from agents_remember.worktrees.integration.lifecycle.worker.termination import (
    observe_worker_termination,
    worker_process_fingerprint,
    worker_termination_request,
)
from agents_remember.worktrees.modules.quality import gate
from closeout_input_test_support import with_commit_proven, with_mutation_intent
from integration_certification_test_support import integration_fixture
from pydantic import ValidationError
from repository_profile_test_support import NODE_FIXTURE
from test_closeout_certification_entrypoint import _apply, _executor, _fixture
from test_closeout_queue import MASTER_A
from test_integration_certification_selection import _organizational_fixture, _run_completion
from test_worktree_integrate_quality_gate import integration_contract


def _resume_with(
    record: LifecycleOperationRecord,
    **updates: Any,
) -> LifecycleOperationRecord:
    return requeued_same_generation(record).model_copy(update=updates)


def _dirty_closeout(tmp_path: Path):
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    record = store.read()
    assert record is not None
    return contract, record.input, store, record


def _integration_store(tmp_path: Path) -> LifecycleOperationStore:
    return integration_fixture(tmp_path, contract_factory=integration_contract).owner.store


def test_resume_preserves_approval_commits_worker_door_and_irreversible_boundary(
    tmp_path: Path,
) -> None:
    _contract_value, _operation_input, store, _record = _dirty_closeout(tmp_path)
    store.update(lambda record: record.model_copy(update={"approvalClaimed": True}))
    store.update(with_mutation_intent)
    current = store.update(with_commit_proven)
    current = store.update(
        lambda record: record.model_copy(
            update={
                "workerPid": 4242,
                "workerLease": "a" * 64,
                "workerProcessFingerprint": "b" * 64,
            }
        )
    )
    assert current.recoveryCommits is not None
    assert current.doorPublication is not None
    cases = (
        ("approval", {"approvalClaimed": False}, "claimed approval"),
        ("recovery", {"recoveryCommits": None}, "recovery commits"),
        ("worker", {"workerLease": "c" * 64}, "worker lease"),
        ("door", {"doorPublication": None}, "door publication evidence"),
        (
            "irreversible",
            {"irreversibleBoundaryEntered": False},
            "irreversible|commit-proven",
        ),
    )
    for _label, updates, message in cases:
        with pytest.raises((RuntimeError, ValueError), match=message):
            store.resume_generation(
                lambda record, updates=updates: _resume_with(record, **updates),
                expected_generation=current.generation,
            )
    assert store.read() == current


def test_resume_preserves_quality_and_integration_publication() -> None:
    with _organizational_fixture() as (fixture, completion):
        calls = []
        with mock.patch.object(
            gate, "run_clean_quality", side_effect=_executor(NODE_FIXTURE, calls)
        ):
            outcome = _run_completion(fixture, completion)
        assert len(calls) == 1 and outcome.certification is not None
        store = fixture.owner.store
        current = store.read()
        assert current is not None and current.integrationAuthority is not None
        assert current.qualityCertification == outcome.certification
        assert current.integrationCertification is not None
        assert current.integrationCertification.completionFingerprint == completion.fingerprint
        authority = current.integrationAuthority
        commits = LifecycleOperationRecoveryCommits(
            codeCommit=authority.codeCandidateCommit,
            memoryContentCommit=authority.memoryContentCommit,
            ledgerCommit=authority.ledgerCommit,
        )
        publication = IntegrationPublicationIntent(
            operationKey=current.operationKey,
            generation=current.generation,
            preparedAt="2026-08-23T02:00:00+00:00",
            claimState="not-applicable",
        )
        current = store.update(
            lambda record: record.model_copy(
                update={"recoveryCommits": commits, "integrationPublication": publication}
            )
        )
        before = store.path.read_bytes()
        alternate_authority = authority.model_copy(update={"codeCandidateCommit": "f" * 40})
        alternate_commits = commits.model_copy(update={"codeCommit": "f" * 40})
        for updates, error, message in (
            (
                {
                    "integrationAuthority": alternate_authority,
                    "recoveryCommits": alternate_commits,
                },
                ValidationError,
                "selected completion code commit must match integration authority",
            ),
            ({"qualityCertification": None}, RuntimeError, "quality certification"),
            ({"integrationPublication": None}, RuntimeError, "publication intent"),
        ):
            with pytest.raises(error, match=message):
                store.resume_generation(
                    lambda record, updates=updates: _resume_with(record, **updates),
                    expected_generation=current.generation,
                )
            assert store.path.read_bytes() == before
        assert store.read() == current


def test_resume_preserves_closeout_finalization_proof(tmp_path: Path) -> None:
    _contract_value, _operation_input, store, _record = _dirty_closeout(tmp_path)
    store.update(
        lambda record: record.model_copy(update={"status": "running", "phase": "preflight"})
    )
    store.update(lambda record: record.model_copy(update={"approvalClaimed": True}))
    store.update(with_mutation_intent)
    store.update(with_commit_proven)
    finalized = store.update(
        lambda record: record.model_copy(
            update={
                "phase": "contract-finalization",
                "closeoutFinalizedContractSha256": "f" * 64,
            }
        )
    )
    with pytest.raises(RuntimeError, match="finalized contract SHA-256 is immutable"):
        store.resume_generation(
            lambda record: record.model_copy(
                update={
                    "attempt": record.attempt + 1,
                    "status": "queued",
                    "phase": "queued",
                    "closeoutFinalizedContractSha256": None,
                }
            ),
            expected_generation=finalized.generation,
        )
    assert store.read() == finalized


def test_resume_preserves_organizational_repair_and_cancellation_evidence(
    tmp_path: Path,
) -> None:
    repair_store = _integration_store(tmp_path / "repair")
    repair = repair_store.read()
    assert repair is not None and repair.integrationAuthority is not None
    cancel_preview = {
        "contract_path": repair.contractPath,
        "operation_kind": "integrate",
        "action": "cancel",
        "expected_generation": repair.generation,
        "intent_note": "repair the exact organizational failure",
        "dry_run": True,
    }
    repair_result = {
        "state": "organizational-completion-gate-failed",
        "developerDecisionRequired": True,
        "safeToReplace": False,
        "superRefsMoved": False,
        "ok": False,
        "operation": "worktree_integrate",
        "nextTool": "worktree_operation_control",
        "nextArgs": cancel_preview,
        "applyStep": {
            "nextTool": "worktree_operation_control",
            "nextArgs": {**cancel_preview, "dry_run": False},
        },
    }
    authority = repair.integrationAuthority
    evidence = OrganizationalCompletionRepairEvidence(
        operationKey=repair.operationKey,
        candidateState=repair.candidateState,
        contractPath=repair.contractPath,
        taskId=repair.taskId,
        taskName=repair.taskName,
        sprintTaskDocument="tasks/sprint/task.json",
        candidateTaskDocument="tasks/sprint/leaf.json",
        owningMasterTaskDocument="tasks/sprint/master.json",
        codeCommit=authority.codeCandidateCommit,
        memoryContentCommit=authority.memoryContentCommit,
        ledgerCommit=authority.ledgerCommit,
        acceptedContractSha256="a" * 64,
        resetContractSha256="b" * 64,
    )
    repair = repair_store.update(
        lambda record: record.model_copy(
            update={"result": repair_result, "organizationalRepair": evidence}
        )
    )
    with pytest.raises(RuntimeError, match="repair evidence is immutable"):
        repair_store.resume_generation(
            lambda record: _resume_with(record, organizationalRepair=None),
            expected_generation=repair.generation,
        )
    assert repair_store.read() == repair

    _contract_value, _operation_input, cancel_store, cancel = _dirty_closeout(tmp_path / "cancel")
    cancellation = LifecycleCancellationEvidence(
        operationKind="closeout",
        generation=cancel.generation,
        workerExitProven=True,
        expected={"git": "unchanged"},
        observed={"git": "unchanged"},
        provenAt="2026-08-23T02:10:00+00:00",
    )
    cancelled = cancel_store.update(
        lambda record: record.model_copy(
            update={
                "status": "cancelled",
                "phase": "cancelled",
                "generationDisposition": "cancelled",
                "cancellationEvidence": cancellation,
            }
        )
    )
    with pytest.raises(RuntimeError, match="cancellation evidence is immutable"):
        cancel_store.resume_generation(
            lambda record: _resume_with(record, cancellationEvidence=None),
            expected_generation=cancelled.generation,
        )
    assert cancel_store.read() == cancelled


def test_update_and_resume_own_attempt_status_and_phase_exceptions(tmp_path: Path) -> None:
    _contract_value, _operation_input, store, current = _dirty_closeout(tmp_path)
    with pytest.raises(RuntimeError, match="ordinary lifecycle transition cannot change attempt"):
        store.update(lambda record: record.model_copy(update={"attempt": record.attempt + 1}))
    with pytest.raises(RuntimeError, match="increment attempt exactly once"):
        store.resume_generation(
            lambda record: record.model_copy(update={"status": "queued", "phase": "queued"}),
            expected_generation=current.generation,
        )
    with pytest.raises(RuntimeError, match="sanctioned status and phase"):
        store.resume_generation(
            lambda record: record.model_copy(
                update={"attempt": record.attempt + 1, "status": "running", "phase": "quality"}
            ),
            expected_generation=current.generation,
        )
    resumed, changed = store.resume_generation(
        requeued_same_generation,
        expected_generation=current.generation,
    )
    assert changed is True
    assert (resumed.attempt, resumed.status, resumed.phase) == (2, "queued", "queued")


def _bind_actual_resume_worker(
    store: LifecycleOperationStore, child: subprocess.Popen[str], *, observe_live: bool
) -> LifecycleOperationRecord:
    assert child.stdout is not None and child.stdout.readline().strip() == "ready"
    fingerprint = worker_process_fingerprint(child.pid)
    assert fingerprint is not None
    running = store.update(
        lambda record: record.model_copy(
            update={
                "status": "running",
                "phase": "preflight",
                "workerPid": child.pid,
                "workerLease": content_digest({"pid": child.pid, "fingerprint": fingerprint}),
                "workerProcessFingerprint": fingerprint,
            }
        )
    )
    request = worker_termination_request(running)
    current = store.update(lambda record: record.model_copy(update={"workerTermination": request}))
    if observe_live:
        observed = observe_worker_termination(request)
        assert observed is not None and observed.state == "termination-required"
        current = store.update(
            lambda record: record.model_copy(update={"workerTermination": observed})
        )
    return current


@pytest.mark.parametrize("observe_live", [False, True], ids=["requested", "observed-live"])
def test_resume_waits_for_actual_child_exit_then_archives_exact_proof_once(
    tmp_path: Path, observe_live: bool
) -> None:
    _contract_value, _operation_input, store, original = _dirty_closeout(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", "import sys; print('ready', flush=True); sys.stdin.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        retained = _bind_actual_resume_worker(store, child, observe_live=observe_live)
        before = store.path.read_bytes()
        with pytest.raises(LifecycleControlError) as refused:
            store.resume_generation(
                requeued_same_generation, expected_generation=retained.generation
            )
        assert refused.value.status == "worker-termination-required"
        assert refused.value.next_action == "cancel"
        assert store.path.read_bytes() == before and child.poll() is None
        assert retained.workerTerminationHistory == []
        assert child.stdin is not None
        child.stdin.close()
        assert child.wait(timeout=5) == 0
        exited = reconcile_worker_exit(store)
        assert exited is not None and exited.workerTermination is not None
        proof = exited.workerTermination
        assert proof.state == "exited" and proof.observedAt is not None
        assert (
            proof.pid == child.pid and proof.processFingerprint == retained.workerProcessFingerprint
        )
        assert proof.lease == retained.workerLease
        assert exited.workerPid is None and exited.workerLease is None
        resumed, changed = store.resume_generation(
            requeued_same_generation, expected_generation=exited.generation
        )
        assert changed and store.read() == resumed
        assert resumed.workerTermination is None and resumed.workerTerminationHistory == [proof]
        assert resumed.attempt == original.attempt + 1 and resumed.generation == original.generation
        assert resumed.input == original.input and resumed.certification == original.certification
        assert resumed.doorPublication == original.doorPublication
        assert (resumed.status, resumed.phase) == ("queued", "queued")
        repeated, changed = store.resume_generation(
            requeued_same_generation, expected_generation=resumed.generation
        )
        assert changed and repeated.attempt == resumed.attempt + 1
        assert repeated.workerTerminationHistory == [proof] and repeated.workerTermination is None
        assert repeated.certification == original.certification
    finally:
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
