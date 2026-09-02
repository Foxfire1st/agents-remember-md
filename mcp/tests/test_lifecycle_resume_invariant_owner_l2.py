"""Store-choke forcing for the invariant owner shared by update and resume."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from agents_remember.models.lifecycles.operation import (
    IntegrateOperationInput,
    IntegrationPublicationIntent,
    IntegrationQualityCertification,
    LifecycleOperationRecord,
    LifecycleOperationRecoveryCommits,
    OrganizationalCompletionRepairEvidence,
)
from agents_remember.models.lifecycles.termination import LifecycleCancellationEvidence
from agents_remember.worktrees.integration.lifecycle.lifecycle_generation_resume import (
    requeued_same_generation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from closeout_input_test_support import with_commit_proven, with_mutation_intent
from test_lifecycle_operation_controls_l2 import _dirty_closeout
from test_lifecycle_operations import _completed_closeout_for_integration, _contract


def _resume_with(
    record: LifecycleOperationRecord,
    **updates: Any,
) -> LifecycleOperationRecord:
    return requeued_same_generation(record).model_copy(update=updates)


def _integration_store(tmp_path: Path) -> LifecycleOperationStore:
    contract = _completed_closeout_for_integration(_contract(tmp_path))
    start_or_observe_operation(
        IntegrateOperationInput(
            configPath=(tmp_path / "settings.json").as_posix(),
            contractPath=contract.contract_path.as_posix(),
        ),
        contract,
        launcher=lambda *_: None,
    )
    return LifecycleOperationStore(operation_record_path(contract.worktree_group, "integrate"))


def _quality_certification() -> IntegrationQualityCertification:
    result = {
        "required": True,
        "status": "enforced",
        "passed": True,
        "mode": "full",
        "executor": "dagger",
        "diffBase": "d" * 40,
        "memoryCap": None,
        "memoryPolicy": {
            "mode": "container-host-managed",
            "processPolicy": "profile-adapter-owned",
            "swap": "container-host-managed",
        },
    }
    payload = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return IntegrationQualityCertification(
        completionFingerprint="a" * 64,
        codeCommit="b" * 40,
        candidateTree="c" * 40,
        attestation={
            "kind": "organizational-master-completion",
            "completionFingerprint": "a" * 64,
            "codeCommit": "b" * 40,
            "candidateTree": "c" * 40,
            "diffBase": "d" * 40,
            "mode": "full",
            "executor": "dagger",
            "memoryCapBytes": "",
        },
        resultSha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        result=result,
    )


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


def test_resume_preserves_quality_and_integration_publication(tmp_path: Path) -> None:
    store = _integration_store(tmp_path)
    current = store.read()
    assert current is not None and current.integrationAuthority is not None
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
            update={
                "recoveryCommits": commits,
                "qualityCertification": _quality_certification(),
                "integrationPublication": publication,
            }
        )
    )
    alternate_authority = authority.model_copy(update={"codeCandidateCommit": "f" * 40})
    alternate_commits = commits.model_copy(update={"codeCommit": "f" * 40})
    for updates, message in (
        (
            {
                "integrationAuthority": alternate_authority,
                "recoveryCommits": alternate_commits,
            },
            "integrationAuthority|recovery commits",
        ),
        ({"qualityCertification": None}, "quality certification"),
        ({"integrationPublication": None}, "publication intent"),
    ):
        with pytest.raises(RuntimeError, match=message):
            store.resume_generation(
                lambda record, updates=updates: _resume_with(record, **updates),
                expected_generation=current.generation,
            )
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
