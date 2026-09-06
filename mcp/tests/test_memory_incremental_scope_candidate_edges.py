"""Fail-closed canonical-candidate adapter edges for R06 scope evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents_remember.memory_quality.incremental_scope import candidate as candidate_impl
from agents_remember.memory_quality.incremental_scope.candidate import (
    ContractScopeAuthority,
    observe_contract_task,
    observe_contract_task_pair,
    observe_scope_candidate,
)
from agents_remember.memory_quality.incremental_scope.errors import (
    ScopeFailure,
    ScopeUnprovenError,
)
from agents_remember.memory_quality.incremental_scope.models import (
    GitTreeDelta,
    TaskObservationPair,
)
from agents_remember.models.lifecycles.door import CloseoutDoorGeneration
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document import CanonicalTaskObservation
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity, missing_task_intent
from agents_remember.tasks.document_refs import TaskDocumentRefError
from agents_remember.tasks.store import TaskDocSourceSnapshot
from agents_remember.worktrees.worktree_contract import WorktreeContract


def _contract(tmp_path: Path) -> WorktreeContract:
    return WorktreeContract(
        task_id="task",
        task_name="master",
        repo_name="repo",
        workflow_kind="light-task",
        memory_mode="external",
        coordination_root=tmp_path / "coordination",
        task_root=tmp_path / "coordination/tasks/repo/master",
        contract_path=tmp_path
        / "coordination/tasks/repo/master/enclosures/leaf/series-contract.md",
        task_artifact=tmp_path / "coordination/tasks/repo/master/task.md",
        worktree_group=tmp_path / "group",
        code_repo_path=tmp_path / "code-repo",
        code_source_branch="main",
        code_work_branch="work",
        code_base_commit="1" * 40,
        code_worktree=tmp_path / "code",
        memory_repo_path=tmp_path / "memory-repo",
        memory_source_branch="memory",
        memory_work_branch="work-memory",
        memory_base_commit="2" * 40,
        memory_worktree=tmp_path / "memory",
        ledger_path=tmp_path / "memory/memory.md",
        leaf_id="leaf",
    )


def _pair(contract: WorktreeContract) -> MemoryCandidatePairIdentity:
    assert contract.memory_worktree is not None
    assert contract.ledger_path is not None
    return MemoryCandidatePairIdentity(
        repoId=contract.repo_name,
        contractPath=contract.contract_path.as_posix(),
        contractDigest="a" * 64,
        codeRoot=contract.code_worktree.resolve().as_posix(),
        memoryRoot=contract.memory_worktree.resolve().as_posix(),
        codeSourceBranch=contract.code_source_branch,
        codeWorkBranch=contract.code_work_branch,
        codeBaseCommit=contract.code_base_commit,
        memorySourceBranch=contract.memory_source_branch,
        memoryWorkBranch=contract.memory_work_branch,
        memoryBaseCommit=contract.memory_base_commit,
        onboardingRoot=(contract.memory_worktree / "onboarding").resolve().as_posix(),
        ledgerPath=contract.ledger_path.resolve().as_posix(),
    )


def _observation(
    contract: WorktreeContract,
    *,
    ref: TaskDocumentRef | None = None,
) -> CanonicalTaskObservation:
    return CanonicalTaskObservation(
        taskRoot=contract.task_root.resolve().as_posix(),
        taskDocumentRef=ref or TaskDocumentRef(repository="repo", path="master/leaf.json"),
        sourceDigest="b" * 64,
        sourceAuthorityNamespace="agents-remember.task-document-source",
        sourceValidatorVersion="fixture/v1",
        semanticTopologyDigest="c" * 64,
        taskIntent=TaskIntentIdentity(digest="d" * 64),
    )


def _door(
    contract: WorktreeContract,
    *,
    intent: object | None = None,
    ref: TaskDocumentRef | None = None,
) -> CloseoutDoorGeneration:
    return cast(
        CloseoutDoorGeneration,
        SimpleNamespace(
            taskIntent=intent or TaskIntentIdentity(digest="d" * 64),
            contractPath=contract.contract_path.as_posix(),
            taskId=contract.task_id,
            taskName=contract.task_name,
            codeBaseCommit=contract.code_base_commit,
            memoryBaseCommit=contract.memory_base_commit,
            candidateTree="3" * 40,
            memoryCandidateTree="4" * 40,
            taskDocumentRef=ref or TaskDocumentRef(repository="repo", path="master/leaf.json"),
            generationId="e" * 64,
            schemaVersion="ar-closeout-door/v1",
            taskTopologyFingerprint="f" * 64,
        ),
    )


def _reason(error: pytest.ExceptionInfo[ScopeUnprovenError]) -> str:
    return error.value.failure.code


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "series"},
        {"memory_mode": "internal"},
        {"memory_worktree": None},
    ],
)
def test_scope_candidate_requires_one_external_memory_leaf(
    tmp_path: Path,
    changes: dict[str, Any],
) -> None:
    contract = replace(_contract(tmp_path), **changes)
    with pytest.raises(ScopeUnprovenError) as caught:
        ContractScopeAuthority(contract).observe()
    assert _reason(caught) in {
        "candidate-not-external-leaf",
        "candidate-memory-root-missing",
    }


def test_scope_candidate_preserves_typed_refusal_and_wraps_owner_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract(tmp_path)
    typed = ScopeUnprovenError(ScopeFailure(code="typed", detail="fixture"))
    monkeypatch.setattr(
        candidate_impl,
        "resolve_memory_candidate_pair",
        lambda *args, **kwargs: (_ for _ in ()).throw(typed),
    )
    with pytest.raises(ScopeUnprovenError) as caught:
        observe_scope_candidate(contract)
    assert caught.value is typed

    monkeypatch.setattr(
        candidate_impl,
        "resolve_memory_candidate_pair",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    with pytest.raises(ScopeUnprovenError) as caught:
        observe_scope_candidate(contract)
    assert _reason(caught) == "candidate-owner-unavailable"


def test_scope_candidate_composes_exact_pair_code_memory_and_task_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract(tmp_path)
    pair = _pair(contract)
    task = TaskObservationPair(base=_observation(contract), candidate=_observation(contract))
    monkeypatch.setattr(
        candidate_impl, "resolve_memory_candidate_pair", lambda *args, **kwargs: pair
    )
    monkeypatch.setattr(
        candidate_impl,
        "capture_future_code_candidate",
        lambda _contract: SimpleNamespace(codeCandidateTree="3" * 40),
    )
    monkeypatch.setattr(candidate_impl, "_candidate_tree", lambda *args: "4" * 40)
    monkeypatch.setattr(candidate_impl, "observe_contract_task_pair", lambda *args, **kwargs: task)

    def observe_delta(
        repository: Path,
        *,
        namespace: str,
        root: str,
        base_ref: str,
        candidate_tree: str,
    ) -> GitTreeDelta:
        del repository, base_ref
        return GitTreeDelta.model_validate(
            {
                "namespace": namespace,
                "root": root,
                "baseTree": "5" * 40,
                "candidateTree": candidate_tree,
                "changes": (),
            }
        )

    monkeypatch.setattr(candidate_impl, "observe_git_tree_delta", observe_delta)

    observed = observe_scope_candidate(contract)

    assert observed.pairIdentity == pair
    assert observed.code.candidateTree == "3" * 40
    assert observed.memory.candidateTree == "4" * 40
    assert observed.task == task


@pytest.mark.parametrize(
    ("door_change", "expected"),
    [
        ({"taskIntent": missing_task_intent()}, "task-base-intent-unavailable"),
        ({"taskId": "other"}, "task-base-identity-mismatch"),
        ({"candidateTree": "9" * 40}, "task-base-candidate-mismatch"),
    ],
)
def test_task_pair_refuses_invalid_door_authority(
    tmp_path: Path,
    door_change: dict[str, object],
    expected: str,
) -> None:
    contract = _contract(tmp_path)
    door = _door(contract)
    for key, value in door_change.items():
        setattr(door, key, value)
    contract = replace(contract, closeout_door=door)

    with pytest.raises(ScopeUnprovenError) as caught:
        observe_contract_task_pair(
            contract,
            code_candidate="3" * 40,
            memory_candidate="4" * 40,
        )

    assert _reason(caught) == expected


def test_task_pair_binds_door_baseline_and_requires_same_current_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract(tmp_path)
    door = _door(contract)
    contract = replace(contract, closeout_door=door)
    current = _observation(contract)
    monkeypatch.setattr(candidate_impl, "observe_contract_task", lambda _contract: current)

    observed = observe_contract_task_pair(
        contract,
        code_candidate="3" * 40,
        memory_candidate="4" * 40,
    )
    assert observed.base is not None
    assert observed.base.sourceDigest == door.generationId
    assert observed.candidate == current

    other = _observation(
        contract,
        ref=TaskDocumentRef(repository="repo", path="master/other.json"),
    )
    monkeypatch.setattr(candidate_impl, "observe_contract_task", lambda _contract: other)
    with pytest.raises(ScopeUnprovenError) as caught:
        observe_contract_task_pair(
            contract,
            code_candidate="3" * 40,
            memory_candidate="4" * 40,
        )
    assert _reason(caught) == "task-document-identity-mismatch"


class _FakeTopology:
    def __init__(
        self,
        contract: WorktreeContract,
        source: TaskDocSourceSnapshot,
        *,
        graph: object | None,
        missing_parent: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.contract = contract
        self.source = source
        self.graph = graph
        self.missing_parent = missing_parent
        self.raises = raises
        self.leaf_ref = TaskDocumentRef(repository="repo", path="master/leaf.json")
        self.master_ref = TaskDocumentRef(repository="repo", path="master/task.json")
        self.sprint_ref = TaskDocumentRef(repository="repo", path="sprint/task.json")

    def canonical_ref(self, repository: str, path: Path) -> TaskDocumentRef:
        del repository, path
        return self.leaf_ref

    def resolve(self, ref: TaskDocumentRef) -> SimpleNamespace:
        if self.raises is not None:
            raise self.raises
        if ref == self.leaf_ref:
            return SimpleNamespace(ref=ref, document=SimpleNamespace(executionGraph=None))
        if ref == self.master_ref:
            return SimpleNamespace(ref=ref, document=SimpleNamespace(executionGraph=None))
        return SimpleNamespace(ref=ref, document=SimpleNamespace(executionGraph=self.graph))

    def parent(self, ref: TaskDocumentRef) -> TaskDocumentRef | None:
        if ref == self.leaf_ref:
            return None if self.missing_parent == "master" else self.master_ref
        if ref == self.master_ref:
            return None if self.missing_parent == "sprint" else self.sprint_ref
        return None


@dataclass(frozen=True)
class _TaskOwnerCase:
    graph: object | None = None
    missing_parent: str | None = None
    raises: Exception | None = None
    intent: object | None = None
    moved: bool = False


def _install_task_owners(
    monkeypatch: pytest.MonkeyPatch,
    contract: WorktreeContract,
    case: _TaskOwnerCase | None = None,
) -> None:
    selected = case or _TaskOwnerCase()
    source = TaskDocSourceSnapshot(
        json_path=contract.task_root / "leaf.json",
        json_bytes=b"{}",
        markdown_path=contract.task_root / "leaf.md",
        markdown_bytes=b"leaf",
    )
    fake = _FakeTopology(
        contract,
        source,
        graph=selected.graph,
        missing_parent=selected.missing_parent,
        raises=selected.raises,
    )

    def topology_factory(root: Path, *, source_observer: object) -> _FakeTopology:
        del root
        cast(Any, source_observer)(source)
        return fake

    monkeypatch.setattr(candidate_impl, "TaskDocumentTopology", topology_factory)
    monkeypatch.setattr(
        candidate_impl,
        "resolve_terminal_leaf_doc",
        lambda *args: (contract.task_root / "leaf.json", SimpleNamespace()),
    )
    monkeypatch.setattr(
        candidate_impl,
        "graph_context",
        lambda topology, ref, *, authored_graph: SimpleNamespace(
            sprint=SimpleNamespace(ref=ref, document=SimpleNamespace(executionGraph=authored_graph))
        ),
    )
    monkeypatch.setattr(
        candidate_impl,
        "candidate_task_topology_fingerprint",
        lambda *args, **kwargs: "e" * 64,
    )
    monkeypatch.setattr(
        candidate_impl,
        "task_intent_identity",
        lambda *args: (
            selected.intent if selected.intent is not None else TaskIntentIdentity(digest="f" * 64)
        ),
    )
    monkeypatch.setattr(
        candidate_impl,
        "current_task_doc_source",
        lambda accepted: replace(accepted, markdown_bytes=b"moved") if selected.moved else accepted,
    )


@pytest.mark.parametrize("graph", [None, SimpleNamespace(nodes=[])])
def test_current_task_observation_handles_authored_and_legacy_graphs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph: object | None,
) -> None:
    contract = _contract(tmp_path)
    _install_task_owners(monkeypatch, contract, _TaskOwnerCase(graph=graph))

    observed = observe_contract_task(contract)

    assert observed.taskDocumentRef.path == "master/leaf.json"
    assert observed.semanticTopologyDigest == "e" * 64
    assert observed.sourceDigest


def test_current_task_observation_refuses_missing_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract(tmp_path)
    monkeypatch.setattr(candidate_impl, "resolve_terminal_leaf_doc", lambda *args: None)
    with pytest.raises(ScopeUnprovenError) as caught:
        observe_contract_task(contract)
    assert _reason(caught) == "task-document-missing"


@pytest.mark.parametrize("missing", ["master", "sprint"])
def test_current_task_observation_refuses_missing_structural_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    contract = _contract(tmp_path)
    _install_task_owners(monkeypatch, contract, _TaskOwnerCase(missing_parent=missing))
    with pytest.raises(ScopeUnprovenError) as caught:
        observe_contract_task(contract)
    assert _reason(caught) == "task-owner-unavailable"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (
            _TaskOwnerCase(raises=TaskDocumentRefError("fixture", "bad ref")),
            "task-owner-unavailable",
        ),
        (_TaskOwnerCase(raises=ValueError("bad topology")), "task-owner-unavailable"),
        (_TaskOwnerCase(intent=missing_task_intent()), "task-intent-unavailable"),
        (_TaskOwnerCase(moved=True), "task-source-moved"),
    ],
)
def test_current_task_observation_refuses_owner_intent_and_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _TaskOwnerCase,
    expected: str,
) -> None:
    contract = _contract(tmp_path)
    _install_task_owners(monkeypatch, contract, case)
    with pytest.raises(ScopeUnprovenError) as caught:
        observe_contract_task(contract)
    assert _reason(caught) == expected


def test_unclassified_git_status_and_candidate_tree_helper_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(candidate_impl, "require_git", lambda *args: "a" * 40)
    with pytest.raises(ScopeUnprovenError) as caught:
        candidate_impl._parse_name_status(
            tmp_path,
            "1" * 40,
            "2" * 40,
            "T\0file.py\0",
        )
    assert _reason(caught) == "git-change-unclassified"

    monkeypatch.setattr(
        candidate_impl,
        "worktree_candidate_tree",
        lambda repository, index: f"{repository.name}:{index.name}",
    )
    assert candidate_impl._candidate_tree(tmp_path, tmp_path / "group").endswith(":index")
