"""Derive closeout recovery commits from authoritative mutation evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from agents_remember.models.lifecycles.mutation_evidence import (
    CloseoutMutationLeg,
    GitMutationEvidence,
)
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    LifecycleOperationRecord,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.worktrees.integration.mutation_evidence import (
    closeout_requires_recovery,
)

_RECOVERY_FIELD: dict[CloseoutMutationLeg, str] = {
    "code": "codeCommit",
    "memory": "memoryContentCommit",
    "ledger": "ledgerCommit",
}


def derive_closeout_recovery_commits(
    record: LifecycleOperationRecord,
    *,
    mutations: Mapping[CloseoutMutationLeg, GitMutationEvidence] | None = None,
    reported: LifecycleOperationRecoveryCommits | None = None,
) -> LifecycleOperationRecoveryCommits | None:
    """Project exact output cells, with commit proof authoritative for mutated legs."""
    evidence = record.mutationEvidence if mutations is None else mutations
    current = record.recoveryCommits
    cells = (
        current.model_dump(mode="json")
        if current is not None
        else {"codeCommit": "", "memoryContentCommit": "", "ledgerCommit": ""}
    )
    if reported is not None:
        _merge_reported_cells(cells, evidence, reported)
    _overlay_proven_cells(cells, evidence)
    if not cells["codeCommit"]:
        if any(item.state == "commit-proven" for item in evidence.values()):
            raise RuntimeError("commit-proven closeout output requires its accepted code commit")
        return None
    return LifecycleOperationRecoveryCommits.model_validate(cells)


def _merge_reported_cells(
    cells: dict[str, str],
    evidence: Mapping[CloseoutMutationLeg, GitMutationEvidence],
    reported: LifecycleOperationRecoveryCommits,
) -> None:
    for leg, field in _RECOVERY_FIELD.items():
        value = getattr(reported, field)
        if not value:
            continue
        proof = evidence.get(leg)
        if proof is not None and proof.state == "commit-proven" and proof.commit != value:
            raise RuntimeError(f"reported {leg} recovery commit contradicts Git proof")
        if cells[field] and cells[field] != value:
            raise RuntimeError(f"reported {leg} recovery commit changes durable output")
        cells[field] = value


def _overlay_proven_cells(
    cells: dict[str, str],
    evidence: Mapping[CloseoutMutationLeg, GitMutationEvidence],
) -> None:
    for leg, proof in evidence.items():
        if proof.state != "commit-proven":
            continue
        commit = cast(str, proof.commit)
        field = _RECOVERY_FIELD[leg]
        if cells[field] and cells[field] != commit:
            raise RuntimeError(f"durable {leg} recovery commit contradicts Git proof")
        cells[field] = commit


def require_closeout_recovery_projection(record: LifecycleOperationRecord) -> None:
    """Require every proven mutation to appear in the same durable record snapshot."""
    if record.operationKind not in {"closeout", "direct-landing"}:
        return
    projected = derive_closeout_recovery_commits(record)
    if projected != record.recoveryCommits:
        raise RuntimeError(
            "closeout recovery commits must be the exact projection of commit-proven evidence"
        )


def closeout_generation_retained(record: LifecycleOperationRecord) -> bool:
    """Whether recovery/finalization remains owned by this exact closeout generation.

    Private preparation, Git mutation proof and contract-finalization ownership
    are deliberately distinct. The latter requires the exact intended finalized contract-state
    fingerprint plus a complete recovery tuple; arbitrary recovery cells do not
    retain a generation.
    """

    if (
        record.preparation is not None
        or record.legacyMigration is not None
        or closeout_requires_recovery(record)
    ):
        return True
    return _has_exact_finalization_evidence(record)


def closeout_recovery_phase(
    record: LifecycleOperationRecord, *, waiting: bool = False
) -> (
    Literal["recovering-private-preparation", "recovering-after-claim", "contract-finalization"]
    | None
):
    """Project recovery without mistaking retained private work for a consumed claim."""
    if record.operationKind != "closeout" or not closeout_generation_retained(record):
        return None
    claimed = (
        record.approvalClaimed
        or record.irreversibleBoundaryEntered
        or closeout_requires_recovery(record)
        or record.legacyMigration is not None
        or record.closeoutFinalizedContractSha256 is not None
    )
    if record.preparation is not None and not claimed:
        return "recovering-private-preparation"
    return "contract-finalization" if waiting else "recovering-after-claim"


def require_closeout_finalization_evidence(record: LifecycleOperationRecord) -> None:
    """Validate the optional immutable finalization owner recorded by closeout."""

    if record.closeoutFinalizedContractSha256 is None:
        return
    if not _has_exact_finalization_evidence(record):
        raise RuntimeError(
            "closeout finalized contract SHA-256 requires claimed approval and complete "
            "recovery commits"
        )


def _has_exact_finalization_evidence(record: LifecycleOperationRecord) -> bool:
    if record.operationKind != "closeout" or record.closeoutFinalizedContractSha256 is None:
        return False
    operation_input = record.input
    commits = record.recoveryCommits
    if (
        not isinstance(operation_input, CloseoutOperationInput)
        or not record.approvalClaimed
        or commits is None
        or not _valid_finalization_lifecycle(record)
    ):
        return False
    if operation_input.effectiveInput.memoryMode == "external":
        return bool(commits.memoryContentCommit and commits.ledgerCommit)
    return not commits.memoryContentCommit and not commits.ledgerCommit


def _valid_finalization_lifecycle(record: LifecycleOperationRecord) -> bool:
    allowed_phases = {
        "queued": {"recovering-after-claim"},
        "running": {"preflight", "recovering-after-claim", "contract-finalization"},
        "input-required": {"contract-finalization"},
        "completed": {"completed"},
    }
    phases = allowed_phases.get(record.status)
    if phases is None or record.phase not in phases:
        return False
    if record.status != "completed":
        return True
    return isinstance(record.result, dict) and record.result.get("state") in {
        "closed",
        "already-closed",
    }
