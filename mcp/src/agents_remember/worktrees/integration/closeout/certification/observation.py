"""Derive distinct certification authorities from the actual contract and owner outputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Never

from pydantic import BaseModel

from agents_remember.certification.certificate_models import CreationProvenance
from agents_remember.certification.digests import content_digest
from agents_remember.certification.frozen_run.authorities import (
    AuthorityInputSnapshot,
    CandidateAuthorityEnvelope,
    CandidateAuthorityRecords,
    GeneratedInputRecord,
    MutationAuthorityRecord,
    SourceAuthorityEdge,
    SourceAuthorityRecord,
    WorktreeRuleRecord,
)
from agents_remember.certification.lifecycle_models import ExactCandidateObservation
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
)
from agents_remember.errors import CertificationContractError
from agents_remember.models.closeout.input import EffectiveCloseoutInput
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.modules.git import (
    branch_commit,
    is_ancestor,
    require_git,
    worktree_candidate_tree,
)
from agents_remember.worktrees.queue.closeout_staged_quality import PreparedStagedCode
from agents_remember.worktrees.services import worktree_services
from agents_remember.worktrees.source_lineage import source_lineage_for_contract
from agents_remember.worktrees.worktree_contract import WorktreeContract


@dataclass(frozen=True)
class CandidateObservationRequest:
    contract: WorktreeContract
    effective_input: EffectiveCloseoutInput
    profile: CanonicalRepositoryCertificationProfile
    preparation: PreparedStagedCode
    provenance: CreationProvenance


@dataclass(frozen=True)
class ObservedCertificationCandidate:
    candidate: ExactCandidateObservation
    authorities: CandidateAuthorityRecords


def _snapshot(owner: str, address: str, value: object) -> AuthorityInputSnapshot:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return AuthorityInputSnapshot(
        owner=owner,
        address=address,
        canonicalBytes=encoded,
        contentSha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def _finding_value(value: object) -> object:
    """Project typed evidence at every depth; the error owner still validates values."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _finding_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_finding_value(item) for item in value)
    return value


def refuse(code: str, expected: object, observed: object) -> Never:
    raise CertificationContractError(
        "closeout certification admission refused",
        (
            {
                "code": code,
                "path": "candidate",
                "expected": _finding_value(expected),
                "observed": _finding_value(observed),
            },
        ),
    )


def observe_certification_candidate(
    request: CandidateObservationRequest,
) -> ObservedCertificationCandidate:
    contract = request.contract
    if contract.kind != "leaf" or not contract.leaf_id or contract.closeout_door is None:
        refuse("candidate-authority-invalid", "one addressed leaf and door", contract.kind)
    task = worktree_services().memory_quality.observe_contract_task(contract)
    topology = task.semanticTopologyDigest
    intent = task.taskIntent
    if topology is None or not isinstance(intent, TaskIntentIdentity):
        refuse(
            "candidate-task-authority-incomplete",
            "canonical topology and task intent identities",
            task.model_dump(mode="json"),
        )
    door = contract.closeout_door
    if door.disposition not in {"waiting", "claimed"}:
        refuse("candidate-door-not-admissible", ["waiting", "claimed"], door.disposition)
    if (
        door.contractPath != contract.contract_path.as_posix()
        or door.taskDocumentRef != task.taskDocumentRef
        or door.taskTopologyFingerprint != task.semanticTopologyDigest
        or door.taskIntent != task.taskIntent
    ):
        refuse("candidate-task-authority-mismatch", task.model_dump(mode="json"), door.generationId)
    rules = _worktree_rules(request)
    source, lineage_input = _source_authority(contract)
    mutation = _mutation_authority(contract, request.effective_input)
    declarations = _snapshot(
        "repository-certification-profile",
        request.profile.profileDigest,
        [item.model_dump(mode="json") for item in request.profile.profile.generatedInputs],
    )
    generated = GeneratedInputRecord(
        profileDigest=request.profile.profileDigest,
        candidateTree=rules.stagedTree,
        declarations=declarations,
    )
    envelope = CandidateAuthorityEnvelope(
        mutation=mutation, source=source, worktree=rules, generated=generated
    )
    inputs = (
        lineage_input,
        declarations,
        _snapshot("task-document-source", str(task.taskDocumentRef), task.model_dump(mode="json")),
        _snapshot(
            "effective-closeout-input",
            str(contract.contract_path),
            request.effective_input.model_dump(mode="json"),
        ),
        _snapshot(
            "contract-owner",
            str(contract.contract_path),
            contract.contract_path.read_text(encoding="utf-8"),
        ),
    )
    records = CandidateAuthorityRecords(
        semanticEnvelope=envelope,
        authorityDigest=content_digest(envelope),
        inputSnapshots=tuple(sorted(inputs, key=lambda item: (item.owner, item.address))),
        provenance=request.provenance,
    )
    identity = {
        "schemaVersion": "closeout-lifecycle-authority/v1",
        "owner": "configured-leaf-contract",
        "repository": contract.repo_name,
        "task": task.taskDocumentRef.model_dump(mode="json"),
        "contract": contract.contract_path.as_posix(),
        "leaf": contract.leaf_id,
        "lifecycle": contract.lifecycle_id,
    }
    candidate = ExactCandidateObservation(
        taskId=contract.leaf_id,
        contractPath=contract.contract_path.as_posix(),
        lifecycleAuthorityDigest=content_digest(identity),
        repositoryId=contract.repo_name,
        sourceBranchRef=source.sourceRef,
        sourceBranchTip=source.sourceTip,
        candidateCodeTree=CandidateIdentity(kind="git-tree", value=rules.stagedTree),
        topologyIdentityDigest=topology,
        taskIntentIdentityDigest=intent.digest,
        normalizedCommitIntentDigest=mutation.normalizedCommitIntentDigest,
        mutationAuthorityDigest=content_digest(mutation),
        sourceAuthorityDigest=content_digest(source),
        worktreeRuleDigest=content_digest(rules),
        generatedInputsDigest=content_digest(generated),
        mutationAuthorityStatus="valid",
        sourceAuthorityStatus="valid",
        branchAuthorityStatus="valid",
        worktreeStatus="admissible",
        generatedArtifactStatus="unknown",
    )
    return ObservedCertificationCandidate(candidate, records)


def _mutation_authority(
    contract: WorktreeContract, effective: EffectiveCloseoutInput
) -> MutationAuthorityRecord:
    memory = contract.memory_worktree
    # Canonical external leaf contracts require their memory worktree before observation.
    if memory is not None:
        expected = f"refs/heads/{contract.memory_work_branch}"
        actual = require_git(memory, ["symbolic-ref", "HEAD"])
        if actual != expected:
            refuse("candidate-memory-branch-mismatch", expected, actual)
    return MutationAuthorityRecord(
        contractPath=contract.contract_path.as_posix(),
        taskId=contract.leaf_id,
        codeRoot=contract.code_worktree.as_posix(),
        codeWorkRef=f"refs/heads/{contract.code_work_branch}",
        memoryRoot=memory.as_posix() if memory is not None else None,
        memoryWorkRef=f"refs/heads/{contract.memory_work_branch}" if memory is not None else None,
        normalizedCommitIntentDigest=content_digest(effective),
    )


def _worktree_rules(request: CandidateObservationRequest) -> WorktreeRuleRecord:
    contract = request.contract
    root = contract.code_worktree
    git_dir, common_dir = require_git(
        root, ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"]
    ).splitlines()
    work_ref = require_git(root, ["symbolic-ref", "HEAD"])
    expected = f"refs/heads/{contract.code_work_branch}"
    conflicts = tuple(require_git(root, ["diff", "--name-only", "--diff-filter=U"]).splitlines())
    tree = require_git(root, ["write-tree"])
    with TemporaryDirectory(prefix="closeout-certification-observation-") as temporary:
        add_all_tree = worktree_candidate_tree(root, Path(temporary) / "index")
    if git_dir == common_dir or work_ref != expected or conflicts:
        refuse(
            "candidate-worktree-invalid",
            {"workRef": expected, "linked": True, "conflicts": []},
            {"workRef": work_ref, "linked": git_dir != common_dir, "conflicts": conflicts},
        )
    if tree != request.preparation.candidate_tree or tree != add_all_tree:
        refuse(
            "candidate-preparation-moved",
            request.preparation.candidate_tree,
            {"index": tree, "addAll": add_all_tree},
        )
    return WorktreeRuleRecord(
        codeRoot=root.as_posix(),
        gitDirectory=git_dir,
        commonDirectory=common_dir,
        workRef=work_ref,
        headCommit=require_git(root, ["rev-parse", "HEAD"]),
        stagedTree=tree,
        addAllTree=add_all_tree,
        preCommitHookRan=request.preparation.pre_commit_hook_ran,
    )


def _source_authority(
    contract: WorktreeContract,
) -> tuple[SourceAuthorityRecord, AuthorityInputSnapshot]:
    lineage = source_lineage_for_contract(contract)
    if lineage is None or lineage.state != "current" or not lineage.edges:
        refuse(
            "candidate-source-authority-invalid",
            "complete current source lineage",
            lineage.model_dump(mode="json") if lineage is not None else None,
        )
    edges: list[SourceAuthorityEdge] = []
    for edge in lineage.edges:
        root = contract.code_repo_path if edge.side == "code" else contract.memory_repo_path
        # Current lineage cannot contain an edge whose repository owner is unavailable.
        assert root is not None
        before = (
            branch_commit(root, edge.sourceBranch),
            branch_commit(root, edge.descendantBranch),
        )
        if not is_ancestor(root, *before):
            refuse("candidate-source-lineage-moved", "source ancestor of descendant", before)
        after = (branch_commit(root, edge.sourceBranch), branch_commit(root, edge.descendantBranch))
        if before != after:
            refuse("candidate-source-ref-moved", before, after)
        edges.append(
            SourceAuthorityEdge(
                side=edge.side,
                relation=edge.relation,
                repositoryRoot=root.as_posix(),
                sourceRef=f"refs/heads/{edge.sourceBranch}",
                sourceTip=before[0],
                descendantRef=f"refs/heads/{edge.descendantBranch}",
                descendantTip=before[1],
                contractPath=edge.contractPath,
            )
        )
    return (
        SourceAuthorityRecord(
            sourceRef=f"refs/heads/{contract.code_source_branch}",
            sourceTip=branch_commit(contract.code_repo_path, contract.code_source_branch),
            edges=tuple(edges),
        ),
        _snapshot(
            "task-source-lineage", str(contract.contract_path), lineage.model_dump(mode="json")
        ),
    )
