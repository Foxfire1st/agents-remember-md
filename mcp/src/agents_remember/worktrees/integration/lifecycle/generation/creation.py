"""Construct queued lifecycle generations from exact candidate and integration authority."""

from __future__ import annotations

from datetime import datetime

from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    IntegrateOperationInput,
    IntegrationConflictTransaction,
    IntegrationOperationAuthority,
    LifecycleOperationInput,
    LifecycleOperationRecord,
    lifecycle_operation_dependencies,
)
from agents_remember.worktrees.integration.integration_branch_authority import integration_targets
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_candidate import (
    LifecycleOperationCandidate,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    operation_key,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    located_lifecycle_operation_report_path,
)
from agents_remember.worktrees.integration.mutation_evidence import (
    initial_closeout_mutation_evidence,
)
from agents_remember.worktrees.modules.git import branch_commit, is_ancestor
from agents_remember.worktrees.worktree_contract import WorktreeContract


def queued_operation_record(
    contract: WorktreeContract,
    operation_input: LifecycleOperationInput,
    candidate: LifecycleOperationCandidate,
    integration_authority: IntegrationOperationAuthority | None,
    timestamp: datetime,
) -> LifecycleOperationRecord:
    stamp = timestamp.isoformat()
    record = LifecycleOperationRecord(
        taskId=contract.task_id,
        taskName=contract.task_name,
        contractPath=contract.contract_path.as_posix(),
        operationKind=operation_input.kind,
        candidateState=candidate.state,
        candidateTree=candidate.tree,
        taskIntent=candidate.task_intent,
        fingerprint=candidate.fingerprint,
        operationKey=operation_key(
            contract.contract_path, operation_input.kind, candidate.fingerprint
        ),
        integrationAuthority=integration_authority,
        input=operation_input,
        status="queued",
        phase="queued",
        queuedAt=stamp,
        currentCommand=f"waiting to start {operation_input.kind}",
        reportPath=located_lifecycle_operation_report_path(
            contract,
            operation_input.kind,
        ).as_posix(),
        mutationEvidence=(
            initial_closeout_mutation_evidence(contract, operation_input.effectiveInput)
            if isinstance(operation_input, CloseoutOperationInput)
            else {}
        ),
    )
    if operation_input.kind == "integrate":
        return record.model_copy(update={"dependencies": lifecycle_operation_dependencies(record)})
    return record


def snapshot_integration_authority(
    contract: WorktreeContract, operation_input: IntegrateOperationInput
) -> IntegrationOperationAuthority:
    if contract.closeout_status != "completed" or not contract.code_commit:
        raise RuntimeError("integration authority requires a completed closeout code commit")
    targets = {target.side: target for target in integration_targets(contract)}
    code_target = targets["code"]
    code_source_commit = branch_commit(contract.code_repo_path, code_target.branch)
    code_replay_required = not is_ancestor(
        contract.code_repo_path, code_source_commit, contract.code_commit
    )
    memory_source_commit = ""
    memory_replay_required = False
    if contract.memory_mode == "external":
        if (
            contract.memory_repo_path is None
            or not contract.memory_content_commit
            or not contract.ledger_commit
        ):
            raise RuntimeError(
                "external-memory integration authority requires repo and closeout commits"
            )
        memory_target = targets["memory"]
        memory_source_commit = branch_commit(contract.memory_repo_path, memory_target.branch)
        memory_replay_required = not is_ancestor(
            contract.memory_repo_path, memory_source_commit, contract.ledger_commit
        )
    else:
        memory_target = None
    conflict = None
    if operation_input.strategy == "replay" and (code_replay_required or memory_replay_required):
        if contract.kind == "series":
            raise RuntimeError(
                "atomic series integration cannot open a leaf conflict worktree; source drift "
                "requires orchestrator-owned block recovery or graph reshape"
            )
        conflict = IntegrationConflictTransaction(
            codeReplayRequired=code_replay_required,
            memoryReplayRequired=memory_replay_required,
            codeSourceRef=f"refs/heads/{code_target.branch}",
            codeSourceCommit=code_source_commit,
            codeCandidateCommit=contract.code_commit,
            memorySourceRef=(
                f"refs/heads/{memory_target.branch}" if memory_target is not None else ""
            ),
            memorySourceCommit=memory_source_commit,
            memoryContentCommit=contract.memory_content_commit,
            ledgerCommit=contract.ledger_commit,
            codeWorktree=contract.code_worktree.resolve().as_posix(),
            memoryWorktree=(
                contract.memory_worktree.resolve().as_posix()
                if contract.memory_worktree is not None
                else ""
            ),
        )
    return IntegrationOperationAuthority(
        targetKind=code_target.kind,
        codeRepository=code_target.repository.as_posix(),
        codeSourceBranch=code_target.branch,
        codeSourceRef=f"refs/heads/{code_target.branch}",
        codeSourceCommit=code_source_commit,
        codeCandidateCommit=contract.code_commit,
        memoryRepository=(memory_target.repository.as_posix() if memory_target is not None else ""),
        memorySourceBranch=(memory_target.branch if memory_target is not None else ""),
        memorySourceRef=(f"refs/heads/{memory_target.branch}" if memory_target is not None else ""),
        memorySourceCommit=memory_source_commit,
        memoryContentCommit=contract.memory_content_commit,
        ledgerCommit=contract.ledger_commit,
        conflictTransaction=conflict,
    )
