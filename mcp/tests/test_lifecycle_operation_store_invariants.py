"""Behavioral store and retained-integration invariants for lifecycle journals."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from agents_remember.application.lifecycle.lifecycle_operation_worker import OperationRuntime
from agents_remember.models.lifecycles.mutation_evidence import (
    CloseoutMutationLeg,
    GitMutationSnapshot,
)
from agents_remember.models.lifecycles.operation import (
    IntegrateOperationInput,
    IntegrationPublicationIntent,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.worktrees.integration.lifecycle.generation.resume import (
    requeued_same_generation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls import (
    LifecycleControlError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationReadError,
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from agents_remember.worktrees.modules.git import head_commit
from agents_remember.worktrees.worktree_contract import WorktreeContract
from closeout_fixture_test_support import selected_fixture
from closeout_input_test_support import (
    closeout_operation_input,
    start_closeout_operation,
    with_commit_proven,
    with_mutation_intent,
)
from lifecycle_control_test_support import cancel_current_generation
from pydantic import ValidationError
from selected_lifecycle_test_support import (
    completed_selected_closeout_for_integration,
    selected_contract,
    selected_successor,
)
from test_closeout_queue import MASTER_A


def _closeout_store(root: Path) -> tuple[WorktreeContract, LifecycleOperationStore]:
    contract = selected_contract(root)
    start_closeout_operation(closeout_operation_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    return contract, store


def _external_store(root: Path) -> LifecycleOperationStore:
    fixture = selected_fixture(root, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    start_closeout_operation(
        operation_input,
        launcher=lambda *_: None,
    )
    return LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))


def _snapshot(seed: str) -> GitMutationSnapshot:
    return GitMutationSnapshot(
        headRef="refs/heads/test-closeout",
        head=seed * 40,
        headTree="b" * 40,
        refLogFingerprint=seed * 64,
        indexTree="b" * 40,
        candidateTree="c" * 40,
        statusFingerprint="d" * 64,
    )


def _proven_publication(operation_key: str, *, transferred_at: str) -> dict[str, object]:
    return {
        "operationKey": operation_key,
        "generation": 1,
        "preparedAt": "2026-08-23T00:00:00+00:00",
        "claimState": "proven",
        "claimTransferredAt": transferred_at,
        "queueSprintTaskDocument": "tasks/repo/sprint/task.md",
        "queueCandidateTaskDocument": "tasks/repo/leaf/task.md",
        "queueCandidateSha256": "1" * 64,
        "closeoutDoorGenerationId": "2" * 64,
        "closeoutOperationFingerprint": "3" * 64,
        "closeoutOperationKey": "4" * 64,
    }


def test_proven_integration_claim_timestamp_is_nonempty_and_strictly_read(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        IntegrationPublicationIntent.model_validate(
            _proven_publication("5" * 64, transferred_at="")
        )

    contract = completed_selected_closeout_for_integration(selected_contract(tmp_path))
    operation_input = IntegrateOperationInput(
        configPath=(contract.code_repo_path.parent / "settings.json").as_posix(),
        contractPath=contract.contract_path.as_posix(),
    )
    start_or_observe_operation(operation_input, contract, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "integrate"))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    authority = payload["integrationAuthority"]
    payload["recoveryCommits"] = {
        "codeCommit": authority["codeCandidateCommit"],
        "memoryContentCommit": authority["memoryContentCommit"],
        "ledgerCommit": authority["ledgerCommit"],
    }
    payload["integrationPublication"] = _proven_publication(
        payload["operationKey"], transferred_at=""
    )
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LifecycleOperationReadError) as raised:
        store.read()
    assert raised.value.error_type == "ValidationError"


def test_store_reads_one_schema_and_revalidates_every_update(tmp_path: Path) -> None:
    _contract_value, store = _closeout_store(tmp_path)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["agentSelectedJobId"] = "forbidden"
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LifecycleOperationReadError) as raised:
        store.read()
    assert raised.value.side == "current-record"
    assert raised.value.error_type == "ValidationError"
    assert store.path.as_posix() not in str(raised.value)

    _contract_value, store = _closeout_store(tmp_path / "invalid-status")
    with pytest.raises(ValidationError):
        store.update(lambda record: record.model_copy(update={"status": "willy-nilly"}))


def test_store_replacement_and_recovery_require_valid_terminal_identity(tmp_path: Path) -> None:
    _contract_value, store = _closeout_store(tmp_path)
    current = store.read()
    assert current is not None
    with pytest.raises(RuntimeError, match="active lifecycle operation"):
        store.replace_terminal(current)
    store.update(
        lambda record: record.model_copy(
            update={"status": "failed", "phase": "failed", "finishedAt": "2026-08-22T00:00Z"}
        )
    )
    previous = store.read()
    assert previous is not None
    candidate, select_initial = selected_successor(_contract_value, previous)
    with pytest.raises(RuntimeError, match="cannot change taskId"):
        store.replace_terminal(
            candidate.model_copy(update={"taskId": "different"}),
            initial_certification=select_initial,
        )
    assert store.replace_terminal(candidate, initial_certification=select_initial).attempt == 1


@pytest.mark.parametrize(
    ("field", "value", "error_type", "message"),
    [
        ("taskName", "other", RuntimeError, "cannot change taskName"),
        ("operationKey", "f" * 64, ValidationError, "certification selection must name its exact"),
        ("status", "completed", RuntimeError, "invalid lifecycle operation transition"),
    ],
)
def test_store_refuses_immutable_identity_and_status_transitions(
    tmp_path: Path, field: str, value: str, error_type: type[Exception], message: str
) -> None:
    _contract_value, store = _closeout_store(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(error_type, match=message):
        store.update(lambda record: record.model_copy(update={field: value}))
    assert store.path.read_bytes() == before


def _worker_termination(
    *,
    state: str = "requested",
    requested_at: str = "2026-08-22T00:00:00+00:00",
) -> WorkerTerminationEvidence:
    values: dict[str, object] = {
        "state": state,
        "pid": 4242,
        "lease": "a" * 64,
        "processFingerprint": "b" * 64,
        "requestedAt": requested_at,
    }
    if state == "exited":
        values.update({"signal": "none", "observedAt": "2026-08-22T00:01:00+00:00"})
    return WorkerTerminationEvidence.model_validate(values)


def test_store_refuses_worker_termination_history_rewrite(tmp_path: Path) -> None:
    _contract_value, store = _closeout_store(tmp_path)
    archived = _worker_termination(state="exited")
    store.update(lambda record: record.model_copy(update={"workerTerminationHistory": [archived]}))
    replacement = archived.model_copy(update={"pid": 4343})
    with pytest.raises(RuntimeError, match="worker termination history is append-only"):
        store.update(
            lambda record: record.model_copy(update={"workerTerminationHistory": [replacement]})
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("clear", "worker termination evidence is monotonic"),
        ("identity", "worker termination identity is immutable"),
    ],
)
def test_store_refuses_worker_termination_regression(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    _contract_value, store = _closeout_store(tmp_path)
    requested = _worker_termination()
    store.update(
        lambda record: record.model_copy(
            update={
                "workerPid": requested.pid,
                "workerLease": requested.lease,
                "workerProcessFingerprint": requested.processFingerprint,
                "workerTermination": requested,
            }
        )
    )

    def mutate(record):
        after = (
            None
            if mutation == "clear"
            else requested.model_copy(update={"requestedAt": "2026-08-22T00:02:00+00:00"})
        )
        return record.model_copy(update={"workerTermination": after})

    with pytest.raises(RuntimeError, match=message):
        store.update(mutate)


def test_store_refuses_claim_boundary_and_ambiguous_cancellation(tmp_path: Path) -> None:
    _contract_value, claimed_store = _closeout_store(tmp_path / "claim")
    claimed_store.update(lambda record: record.model_copy(update={"approvalClaimed": True}))
    with pytest.raises(RuntimeError, match="cannot become unclaimed"):
        claimed_store.update(lambda record: record.model_copy(update={"approvalClaimed": False}))

    contract = selected_contract(
        tmp_path / "ambiguous", candidate_file=("candidate.txt", "candidate\n")
    )
    start_closeout_operation(closeout_operation_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    store.update(with_mutation_intent)
    with pytest.raises(RuntimeError, match="ambiguous Git intent"):
        store.update(
            lambda record: record.model_copy(update={"status": "cancelled", "phase": "cancelled"})
        )


def test_store_refuses_clearing_or_cancelling_commit_boundary(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path, candidate_file=("candidate.txt", "candidate\n"))
    start_closeout_operation(closeout_operation_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    store.update(with_mutation_intent)
    store.update(with_commit_proven)
    with pytest.raises(
        ValidationError,
        match="closeout irreversible boundary must be derived from commit proof or legacy output proof",
    ):
        store.update(
            lambda record: record.model_copy(update={"irreversibleBoundaryEntered": False})
        )
    with pytest.raises(LifecycleControlError) as caught:
        cancel_current_generation(contract.contract_path, "closeout")
    assert caught.value.status == "lifecycle-immutable-output-recovery-required"
    assert caught.value.next_action == "recover"
    assert caught.value.expected == {
        "operationKind": "closeout",
        "generation": 1,
        "nextAction": "recover",
    }
    assert caught.value.observed["irreversibleBoundaryEntered"] is True
    mutation = caught.value.observed["mutationEvidence"]
    assert isinstance(mutation, dict)
    assert mutation["code"]["state"] == "commit-proven"


def test_integrate_boundary_cannot_be_cleared_or_cancelled(tmp_path: Path) -> None:
    contract = completed_selected_closeout_for_integration(selected_contract(tmp_path))
    operation_input = IntegrateOperationInput(
        configPath=(contract.code_repo_path.parent / "settings.json").as_posix(),
        contractPath=contract.contract_path.as_posix(),
    )
    start_or_observe_operation(operation_input, contract, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "integrate"))
    runtime = OperationRuntime(store)
    runtime.start()
    runtime.progress("source-merge", {"irreversible_boundary": True})

    with pytest.raises(RuntimeError, match="irreversible boundary cannot be cleared"):
        store.update(
            lambda record: record.model_copy(update={"irreversibleBoundaryEntered": False})
        )
    with pytest.raises(LifecycleControlError) as caught:
        cancel_current_generation(contract.contract_path, "integrate")
    assert caught.value.status == "lifecycle-immutable-output-recovery-required"
    assert caught.value.next_action == "recover"
    assert caught.value.expected == {
        "operationKind": "integrate",
        "generation": 1,
        "nextAction": "recover",
    }
    assert caught.value.observed["irreversibleBoundaryEntered"] is True
    assert caught.value.observed["integrationRefs"] is not None


@pytest.mark.parametrize(
    "mutation",
    [
        "repository",
        "state",
        "before",
        "observed",
        "expected-tree",
    ],
)
def test_store_checks_mutation_evidence_monotonicity(tmp_path: Path, mutation: str) -> None:
    store = _external_store(tmp_path)
    current = store.read()
    assert current is not None
    leg = "code"
    if mutation == "repository":
        with pytest.raises(RuntimeError, match="evidence identity is immutable"):
            store.update(
                lambda record: record.model_copy(
                    update={
                        "mutationEvidence": {
                            **record.mutationEvidence,
                            leg: record.mutationEvidence[leg].model_copy(
                                update={"repository": "/different"}
                            ),
                        }
                    }
                )
            )
        return
    store.update(with_mutation_intent)
    if mutation == "state":
        store.update(
            lambda record: record.model_copy(
                update={
                    "mutationEvidence": {
                        **record.mutationEvidence,
                        leg: record.mutationEvidence[leg].model_copy(
                            update={
                                "state": "reconciled-unchanged",
                                "observed": record.mutationEvidence[leg].before,
                            }
                        ),
                    }
                }
            )
        )
    elif mutation == "observed":
        store.update(lambda record: _with_observed_intent(record, _snapshot("4")))
    expected = {
        "state": "invalid closeout mutation evidence transition",
        "before": "pre-command Git evidence is immutable",
        "observed": "observed Git evidence is immutable",
        "expected-tree": "expected output tree is immutable",
    }[mutation]
    with pytest.raises(RuntimeError, match=expected):
        store.update(
            lambda record: _replace_evidence(
                record,
                leg,
                _changed_evidence(record.mutationEvidence[leg], mutation),
            )
        )


@pytest.mark.parametrize("leg", ["memory", "ledger"])
def test_closeout_retry_reset_preserves_admission_prestate_and_refuses_tampering(
    tmp_path: Path,
    leg: CloseoutMutationLeg,
) -> None:
    store = _external_store(tmp_path)
    admitted = store.read()
    assert admitted is not None
    accepted = admitted.mutationEvidence[leg].acceptedBefore
    assert accepted is not None
    store.update(
        lambda record: _replace_evidence(
            record,
            leg,
            record.mutationEvidence[leg].model_copy(
                update={
                    "state": "mutation-intent",
                    "before": accepted,
                    "expectedOutputTree": accepted.candidateTree,
                }
            ),
        )
    )
    reconciled = store.update(
        lambda record: _replace_evidence(
            record,
            leg,
            record.mutationEvidence[leg].model_copy(
                update={"state": "reconciled-unchanged", "observed": accepted}
            ),
        )
    )

    def tamper(record):
        reset = requeued_same_generation(record)
        changed = reset.mutationEvidence[leg].model_copy(
            update={"acceptedBefore": accepted.model_copy(update={"head": "9" * 40})}
        )
        return _replace_evidence(reset, leg, changed)

    with pytest.raises(RuntimeError, match="accepted Git prestate is immutable"):
        store.resume_generation(tamper, expected_generation=reconciled.generation)
    resumed, changed = store.resume_generation(
        requeued_same_generation,
        expected_generation=reconciled.generation,
    )
    assert changed is True
    reset = resumed.mutationEvidence[leg]
    assert reset.acceptedBefore == accepted
    assert reset.before is None
    assert reset.observed is None
    assert reset.expectedOutputTree is None
    assert resumed.mutationHistory[leg] == [reconciled.mutationEvidence[leg]]


def _replace_evidence(record, leg: str, evidence):
    return record.model_copy(
        update={"mutationEvidence": {**record.mutationEvidence, leg: evidence}}
    )


def _with_observed_intent(record, observed: GitMutationSnapshot):
    return _replace_evidence(
        record,
        "code",
        record.mutationEvidence["code"].model_copy(update={"observed": observed}),
    )


def _changed_evidence(evidence, mutation: str):
    if mutation == "state":
        return evidence.model_copy(update={"state": "mutation-intent", "observed": _snapshot("9")})
    if mutation == "before":
        return evidence.model_copy(update={"before": _snapshot("3")})
    if mutation == "observed":
        return evidence.model_copy(update={"observed": _snapshot("5")})
    return evidence.model_copy(update={"expectedOutputTree": "6" * 40})


def test_commit_change_is_preempted_by_model_and_recovery_fill_only(tmp_path: Path) -> None:
    store = _external_store(tmp_path)
    store.update(with_mutation_intent)
    store.update(with_commit_proven)
    with pytest.raises(ValidationError, match="contradicts commit-proven evidence"):
        store.update(lambda record: _changed_proof(record, include_recovery=False))
    with pytest.raises(RuntimeError, match="can only fill empty cells"):
        store.update(lambda record: _changed_proof(record, include_recovery=True))


def _changed_proof(record, *, include_recovery: bool):
    current = record.mutationEvidence["code"]
    assert current.observed is not None
    evidence = current.model_copy(
        update={
            "observed": current.observed.model_copy(
                update={"head": "9" * 40, "refLogFingerprint": "8" * 64}
            ),
            "commit": "9" * 40,
        }
    )
    updates: dict[str, object] = {"mutationEvidence": {**record.mutationEvidence, "code": evidence}}
    if include_recovery:
        assert record.recoveryCommits is not None
        updates["recoveryCommits"] = record.recoveryCommits.model_copy(
            update={"codeCommit": "9" * 40}
        )
    return record.model_copy(update=updates)


def test_finalization_hash_transition_is_phase_bound_and_immutable(tmp_path: Path) -> None:
    contract, store = _closeout_store(tmp_path / "wrong-phase")
    operation_input = closeout_operation_input(contract)
    finalized = replace(
        contract,
        approved_for_commit=True,
        commit_approval_note=operation_input.approvalNote,
        human_review_status="approved",
        closeout_status="completed",
        code_commit=head_commit(contract.code_worktree),
    )
    runtime = OperationRuntime(store)
    runtime.start()
    expected_hash = closeout_contract_sha256(finalized)
    evidence = {
        "recovery_commits": {"codeCommit": finalized.code_commit},
        "closeout_finalized_contract_sha256": expected_hash,
    }
    with pytest.raises(RuntimeError, match="claimed approval and complete recovery"):
        store.update(
            lambda record: record.model_copy(
                update={
                    "phase": "contract-finalization",
                    "recoveryCommits": LifecycleOperationRecoveryCommits(
                        codeCommit=finalized.code_commit
                    ),
                    "closeoutFinalizedContractSha256": expected_hash,
                }
            )
        )
    runtime.progress("approval-claim", {"approval_claimed": True})
    with pytest.raises(RuntimeError, match="introduced at contract-finalization"):
        runtime.progress("quality", evidence)
    runtime.progress("contract-finalization", evidence)
    persisted = store.read()
    assert persisted is not None and persisted.recoveryCommits is not None
    assert persisted.phase == "contract-finalization"
    assert persisted.closeoutFinalizedContractSha256 == expected_hash
    assert persisted.recoveryCommits.model_dump() == {
        "codeCommit": finalized.code_commit,
        "memoryContentCommit": "",
        "ledgerCommit": "",
    }
    finalized_bytes = store.path.read_bytes()
    with pytest.raises(RuntimeError) as raised:
        runtime.progress(
            "quality",
            {"current_command": "refuse phase advancement after finalization"},
        )
    assert str(raised.value) == (
        "closeout finalized contract SHA-256 requires claimed approval and complete "
        "recovery commits"
    )
    after_refusal = store.read()
    assert after_refusal is not None
    assert after_refusal == persisted
    assert store.path.read_bytes() == finalized_bytes
    assert after_refusal.phase == "contract-finalization"
    assert after_refusal.closeoutFinalizedContractSha256 == expected_hash
    assert after_refusal.recoveryCommits == persisted.recoveryCommits
    with pytest.raises(RuntimeError, match="is immutable"):
        store.update(
            lambda record: record.model_copy(update={"closeoutFinalizedContractSha256": "9" * 64})
        )


def test_external_finalization_requires_complete_recovery_tuple(tmp_path: Path) -> None:
    store = _external_store(tmp_path)
    runtime = OperationRuntime(store)
    runtime.start()
    runtime.progress("approval-claim", {"approval_claimed": True})

    with pytest.raises(RuntimeError, match="claimed approval and complete recovery"):
        store.update(
            lambda record: record.model_copy(
                update={
                    "phase": "contract-finalization",
                    "recoveryCommits": LifecycleOperationRecoveryCommits(codeCommit="7" * 40),
                    "closeoutFinalizedContractSha256": "8" * 64,
                }
            )
        )


def test_completed_integration_retains_its_exact_parameters(tmp_path: Path) -> None:
    contract = completed_selected_closeout_for_integration(selected_contract(tmp_path))
    first = IntegrateOperationInput(
        configPath=(contract.code_repo_path.parent / "settings.json").as_posix(),
        contractPath=contract.contract_path.as_posix(),
        strategy="ff-only",
    )
    start_or_observe_operation(first, contract, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "integrate"))
    runtime = OperationRuntime(store)
    runtime.start()
    runtime.finish({"state": "integrated"}, ok=True)

    with pytest.raises(RuntimeError, match="already completed task state"):
        start_or_observe_operation(
            first.model_copy(update={"strategy": "replay"}),
            contract,
            launcher=lambda *_: None,
        )
