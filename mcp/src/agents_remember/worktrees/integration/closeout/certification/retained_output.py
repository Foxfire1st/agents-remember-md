"""Recognize only the selected generation's physically proven code output."""

from __future__ import annotations

from pathlib import Path

from agents_remember.certification.digests import content_digest
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    closeout_generation_retained,
    derive_closeout_recovery_commits,
)
from agents_remember.worktrees.integration.mutation_evidence import (
    commit_matches_intent,
    ephemeral_git_mutation_snapshot,
)
from agents_remember.worktrees.modules.git import require_git
from agents_remember.worktrees.worktree_contract import WorktreeContract

from .observation import ObservedCertificationCandidate, refuse
from .selection import LoadedCertificationSelection


def require_retained_output_currentness(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    selected: LoadedCertificationSelection,
    current: ObservedCertificationCandidate,
) -> None:
    """Compare actual authorities after validating the exact original mutation proof.

    A successful code commit changes HEAD and its leaf lineage tip while preserving
    the certified candidate. Nothing is republished or substituted into the selected
    record; all other observed authorities must still equal the original admission.
    """
    envelope = selected.authorities.semanticEnvelope
    expected = selected.admission.lifecycle.semanticEnvelope.candidate
    proof = record.mutationEvidence.get("code")
    commits = derive_closeout_recovery_commits(record)
    if (
        not closeout_generation_retained(record)
        or proof is None
        or proof.state != "commit-proven"
        or proof.before is None
        or proof.acceptedBefore is None
        or proof.observed is None
        or proof.commit is None
        or commits is None
        or commits.codeCommit != proof.commit
        or Path(proof.repository).resolve() != contract.code_worktree.resolve()
        or proof.expectedOutputTree != envelope.worktree.stagedTree
        or record.candidateTree != envelope.worktree.stagedTree
    ):
        refuse("selected-candidate-authority-moved", expected, current.candidate)
    original_head = envelope.worktree.headCommit
    original_tree = require_git(contract.code_worktree, ["rev-parse", f"{original_head}^{{tree}}"])
    for before in (proof.acceptedBefore, proof.before):
        if (
            before.headRef != envelope.worktree.workRef
            or before.head != original_head
            or before.headTree != original_tree
            or before.indexTree != envelope.worktree.stagedTree
            or before.candidateTree != envelope.worktree.stagedTree
        ):
            refuse("selected-code-output-prestate-mismatch", envelope.worktree, before)
    actual = ephemeral_git_mutation_snapshot(contract.code_worktree)
    if actual != proof.observed or not commit_matches_intent(
        contract.code_worktree,
        before=proof.before,
        actual=actual,
        expected_tree=envelope.worktree.stagedTree,
        commit=proof.commit,
    ):
        refuse("selected-code-output-proof-mismatch", proof, actual)
    owned_edges = tuple(
        index
        for index, edge in enumerate(envelope.source.edges)
        if edge.side == "code"
        and edge.repositoryRoot == contract.code_repo_path.as_posix()
        and edge.descendantRef == envelope.worktree.workRef
        and edge.contractPath == contract.contract_path.as_posix()
        and edge.descendantTip == original_head
    )
    if len(owned_edges) != 1:
        refuse("selected-code-output-lineage-mismatch", "one original leaf code edge", owned_edges)
    source = envelope.source.model_copy(
        update={
            "edges": tuple(
                edge.model_copy(update={"descendantTip": proof.commit})
                if index == owned_edges[0]
                else edge
                for index, edge in enumerate(envelope.source.edges)
            )
        }
    )
    worktree = envelope.worktree.model_copy(update={"headCommit": proof.commit})
    permitted_envelope = envelope.model_copy(update={"source": source, "worktree": worktree})
    permitted_candidate = expected.model_copy(
        update={
            "sourceAuthorityDigest": content_digest(source),
            "worktreeRuleDigest": content_digest(worktree),
        }
    )
    if (
        current.authorities.semanticEnvelope != permitted_envelope
        or current.candidate != permitted_candidate
    ):
        refuse("selected-candidate-authority-moved", permitted_candidate, current.candidate)
