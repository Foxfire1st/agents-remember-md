"""Canonical Git, pair, topology, and intent observation for memory scope."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NoReturn

from agents_remember.errors import FutureCodeCandidateError, MemoryCandidatePairError
from agents_remember.models.task_document import CanonicalTaskObservation
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.tasks.document_refs import TaskDocumentRefError, TaskDocumentTopology
from agents_remember.tasks.leaf_doc import resolve_terminal_leaf_doc
from agents_remember.tasks.store import TaskDocSourceSnapshot, current_task_doc_source
from agents_remember.tasks.task_intent import task_intent_identity
from agents_remember.worktrees.integration.closeout.future_code_candidate import (
    capture_future_code_candidate,
)
from agents_remember.worktrees.integration.closeout.memory_candidate_pair import (
    resolve_memory_candidate_pair,
)
from agents_remember.worktrees.modules.git import require_git, worktree_candidate_tree
from agents_remember.worktrees.queue.closeout_projection_members import (
    candidate_task_topology_fingerprint,
)
from agents_remember.worktrees.queue.closeout_queue_graph import graph_context
from agents_remember.worktrees.worktree_contract import WorktreeContract

from .errors import ScopeFailure, ScopeUnprovenError
from .models import (
    GitPathChange,
    GitTreeDelta,
    ScopeCandidateIdentity,
    ScopeNamespace,
    TaskObservationPair,
    canonical_digest,
)


@dataclass(frozen=True)
class ContractScopeAuthority:
    """Re-observable adapter over existing contract, pair, Git, R01, and R02 owners."""

    contract: WorktreeContract

    def observe(self) -> ScopeCandidateIdentity:
        return observe_scope_candidate(self.contract)


def observe_scope_candidate(
    contract: WorktreeContract,
) -> ScopeCandidateIdentity:
    """Capture one exact external-memory leaf candidate through canonical owners."""

    if contract.kind != "leaf" or contract.memory_mode != "external":
        _refuse("candidate-not-external-leaf", "memory scope requires one external-memory leaf")
    if contract.memory_worktree is None:
        _refuse("candidate-memory-root-missing", "external-memory contract has no memory worktree")
    try:
        pair = resolve_memory_candidate_pair(
            contract,
            requested_contract_path=contract.contract_path,
            requested_repo_id=contract.repo_name,
        )
        code_candidate = capture_future_code_candidate(contract).codeCandidateTree
        memory_candidate = _candidate_tree(contract.memory_worktree, contract.worktree_group)
        task_pair = observe_contract_task_pair(
            contract,
            code_candidate=code_candidate,
            memory_candidate=memory_candidate,
        )
    except ScopeUnprovenError:
        raise
    except (FutureCodeCandidateError, MemoryCandidatePairError, OSError, RuntimeError) as exc:
        _refuse(
            "candidate-owner-unavailable",
            f"canonical candidate owner could not observe the leaf: {type(exc).__name__}",
        )
    code = observe_git_tree_delta(
        contract.code_worktree,
        namespace="code",
        root=pair.codeRoot,
        base_ref=contract.code_base_commit,
        candidate_tree=code_candidate,
    )
    memory = observe_git_tree_delta(
        contract.memory_worktree,
        namespace="memory",
        root=pair.memoryRoot,
        base_ref=contract.memory_base_commit,
        candidate_tree=memory_candidate,
    )
    return ScopeCandidateIdentity(
        pairIdentity=pair,
        code=code,
        memory=memory,
        task=task_pair,
    )


def observe_contract_task_pair(
    contract: WorktreeContract,
    *,
    code_candidate: str,
    memory_candidate: str,
) -> TaskObservationPair:
    """Read the closeout-door baseline and live R01/R02 candidate from their owners."""

    door = contract.closeout_door
    if door is None:
        _refuse(
            "task-base-unavailable",
            "external-memory scope requires one canonical closeout-door task baseline",
        )
    if not isinstance(door.taskIntent, TaskIntentIdentity):
        _refuse("task-base-intent-unavailable", "closeout-door task intent is unavailable")
    if (
        door.contractPath != contract.contract_path.as_posix()
        or door.taskId != contract.task_id
        or door.taskName != contract.task_name
        or door.codeBaseCommit != contract.code_base_commit
        or door.memoryBaseCommit != contract.memory_base_commit
    ):
        _refuse(
            "task-base-identity-mismatch",
            "closeout-door baseline differs from the exact contract authority",
        )
    if door.candidateTree != code_candidate or door.memoryCandidateTree != memory_candidate:
        _refuse(
            "task-base-candidate-mismatch",
            "closeout-door baseline belongs to a different code or memory candidate",
        )
    current = observe_contract_task(contract)
    if current.taskDocumentRef != door.taskDocumentRef:
        _refuse(
            "task-document-identity-mismatch",
            "closeout-door and current task owners resolve different leaf documents",
        )
    baseline = CanonicalTaskObservation(
        taskRoot=contract.task_root.resolve().as_posix(),
        taskDocumentRef=door.taskDocumentRef,
        sourceDigest=door.generationId,
        sourceAuthorityNamespace="agents-remember.closeout-door-generation",
        sourceValidatorVersion=door.schemaVersion,
        semanticTopologyDigest=door.taskTopologyFingerprint,
        taskIntent=door.taskIntent,
    )
    return TaskObservationPair(base=baseline, candidate=current)


def observe_contract_task(contract: WorktreeContract) -> CanonicalTaskObservation:
    """Consume the current task source plus canonical R01 and R02 projections."""

    accepted_sources: list[TaskDocSourceSnapshot] = []
    topology = TaskDocumentTopology(
        contract.coordination_root,
        source_observer=accepted_sources.append,
    )
    found = resolve_terminal_leaf_doc(contract.task_root, contract.leaf_id)
    if found is None:
        _refuse("task-document-missing", "contract leaf has no canonical task document")
    try:
        candidate = topology.resolve(topology.canonical_ref(contract.repo_name, found[0]))
        master_ref = topology.parent(candidate.ref)
        if master_ref is None:
            raise TaskDocumentRefError("task-document-parent-missing", candidate.ref.key)
        master = topology.resolve(master_ref)
        sprint_ref = topology.parent(master.ref)
        if sprint_ref is None:
            raise TaskDocumentRefError("task-document-parent-missing", master.ref.key)
        sprint = topology.resolve(sprint_ref)
        authored_graph = sprint.document.executionGraph
        graph = (
            graph_context(topology, sprint.ref, authored_graph=authored_graph)
            if authored_graph is not None
            else None
        )
        bound_sprint = graph.sprint if graph is not None else sprint
        topology_digest = candidate_task_topology_fingerprint(
            bound_sprint,
            master,
            candidate,
            graph=graph,
        )
        intent = task_intent_identity(contract.task_root, candidate)
    except (TaskDocumentRefError, ValueError) as exc:
        _refuse("task-owner-unavailable", f"canonical task owner refused: {type(exc).__name__}")
    if not isinstance(intent, TaskIntentIdentity):
        _refuse("task-intent-unavailable", "canonical R02 task intent is unavailable")
    if any(current_task_doc_source(source) != source for source in accepted_sources):
        _refuse("task-source-moved", "canonical task source changed during observation")
    return CanonicalTaskObservation(
        taskRoot=contract.task_root.resolve().as_posix(),
        taskDocumentRef=candidate.ref,
        sourceDigest=canonical_digest(
            [
                {
                    "jsonPath": source.json_path.resolve().as_posix(),
                    "markdownPath": source.markdown_path.resolve().as_posix(),
                    "evidence": source.evidence(),
                }
                for source in accepted_sources
            ]
        ),
        sourceAuthorityNamespace="agents-remember.task-document-source",
        sourceValidatorVersion="task-document-source-cas/v1",
        semanticTopologyDigest=topology_digest,
        taskIntent=intent,
    )


def observe_git_tree_delta(
    repository: Path,
    *,
    namespace: ScopeNamespace,
    root: str,
    base_ref: str,
    candidate_tree: str,
) -> GitTreeDelta:
    """Derive roots solely from an exact Git tree diff, including rename endpoints."""

    base_tree = require_git(repository, ["rev-parse", f"{base_ref}^{{tree}}"])
    require_git(repository, ["cat-file", "-e", f"{candidate_tree}^{{tree}}"])
    raw = require_git(
        repository,
        ["diff-tree", "-r", "--name-status", "-z", "--find-renames", base_tree, candidate_tree],
    )
    changes = _parse_name_status(repository, base_tree, candidate_tree, raw)
    return GitTreeDelta(
        namespace=namespace,
        root=Path(root).resolve().as_posix(),
        baseTree=base_tree,
        candidateTree=candidate_tree,
        changes=tuple(sorted(changes, key=lambda item: (item.oldPath or "", item.newPath or ""))),
    )


def _parse_name_status(
    repository: Path,
    base_tree: str,
    candidate_tree: str,
    raw: str,
) -> list[GitPathChange]:
    tokens = raw.split("\0") if raw else []
    changes: list[GitPathChange] = []
    cursor = 0
    while cursor < len(tokens):
        status = tokens[cursor]
        cursor += 1
        if not status:
            continue
        if status.startswith("R"):
            old_path, new_path = tokens[cursor : cursor + 2]
            cursor += 2
            changes.append(
                GitPathChange(
                    status="renamed",
                    oldPath=old_path,
                    newPath=new_path,
                    oldBlob=_blob(repository, base_tree, old_path),
                    newBlob=_blob(repository, candidate_tree, new_path),
                )
            )
            continue
        path = tokens[cursor]
        cursor += 1
        if status == "A":
            changes.append(
                GitPathChange(
                    status="added",
                    newPath=path,
                    newBlob=_blob(repository, candidate_tree, path),
                )
            )
        elif status == "D":
            changes.append(
                GitPathChange(
                    status="deleted",
                    oldPath=path,
                    oldBlob=_blob(repository, base_tree, path),
                )
            )
        elif status == "M":
            changes.append(
                GitPathChange(
                    status="modified",
                    oldPath=path,
                    newPath=path,
                    oldBlob=_blob(repository, base_tree, path),
                    newBlob=_blob(repository, candidate_tree, path),
                )
            )
        else:
            _refuse("git-change-unclassified", f"unsupported Git tree change status {status!r}")
    return changes


def _blob(repository: Path, tree: str, path: str) -> str:
    return require_git(repository, ["rev-parse", f"{tree}:{path}"])


def _candidate_tree(repository: Path, worktree_group: Path) -> str:
    reports = worktree_group / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".scope-candidate-", dir=reports) as temporary:
        return worktree_candidate_tree(repository, Path(temporary) / "index")


def _refuse(code: str, detail: str) -> NoReturn:
    raise ScopeUnprovenError(ScopeFailure(code=code, detail=detail))


__all__ = [
    "ContractScopeAuthority",
    "observe_contract_task",
    "observe_contract_task_pair",
    "observe_git_tree_delta",
    "observe_scope_candidate",
]
