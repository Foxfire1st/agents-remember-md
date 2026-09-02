"""Validator, typed-error, and checker-registry edges for R06 scope evidence."""

from __future__ import annotations

from typing import Any

import pytest
from agents_remember.memory_quality.incremental_scope import registry
from agents_remember.memory_quality.incremental_scope.errors import (
    ScopeFailure,
    ScopeUnprovenError,
)
from agents_remember.memory_quality.incremental_scope.models import (
    CanonicalTaskObservation,
    CheckerScopePolicy,
    GitPathChange,
    GitTreeDelta,
    ScopeCandidateIdentity,
    ScopeEdge,
    ScopeNode,
    SourceIndexObservation,
    TaskObservationPair,
    canonical_digest,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity
from pydantic import ValidationError


def _pair() -> MemoryCandidatePairIdentity:
    return MemoryCandidatePairIdentity(
        repoId="repo",
        contractPath="/coord/tasks/repo/master/enclosures/leaf/series-contract.md",
        contractDigest="a" * 64,
        codeRoot="/work/code",
        memoryRoot="/work/memory",
        codeSourceBranch="main",
        codeWorkBranch="work",
        codeBaseCommit="1" * 40,
        memorySourceBranch="memory",
        memoryWorkBranch="work-memory",
        memoryBaseCommit="2" * 40,
        onboardingRoot="/work/memory/onboarding",
        ledgerPath="/work/memory/memory.md",
    )


def _delta(
    namespace: str,
    *,
    root: str,
    base: str,
    candidate: str,
    changes: tuple[GitPathChange, ...] = (),
) -> GitTreeDelta:
    return GitTreeDelta.model_validate(
        {
            "namespace": namespace,
            "root": root,
            "baseTree": base,
            "candidateTree": candidate,
            "changes": changes,
        }
    )


def _task() -> TaskObservationPair:
    observation = CanonicalTaskObservation(
        taskRoot="/coord/tasks/repo/master",
        taskDocumentRef=TaskDocumentRef(repository="repo", path="master/leaf.json"),
        sourceDigest="b" * 64,
        sourceAuthorityNamespace="agents-remember.task-document-source",
        sourceValidatorVersion="fixture/v1",
        semanticTopologyDigest="c" * 64,
        taskIntent=TaskIntentIdentity(digest="d" * 64),
    )
    return TaskObservationPair(base=observation, candidate=observation)


def _valid_change(status: str) -> GitPathChange:
    payloads: dict[str, dict[str, str]] = {
        "added": {"newPath": "new.py", "newBlob": "1" * 40},
        "modified": {
            "oldPath": "same.py",
            "newPath": "same.py",
            "oldBlob": "1" * 40,
            "newBlob": "2" * 40,
        },
        "deleted": {"oldPath": "old.py", "oldBlob": "1" * 40},
        "renamed": {
            "oldPath": "old.py",
            "newPath": "new.py",
            "oldBlob": "1" * 40,
            "newBlob": "1" * 40,
        },
    }
    return GitPathChange.model_validate({"status": status, **payloads[status]})


def test_git_change_accepts_every_exact_shape_and_canonical_none() -> None:
    assert GitPathChange._canonical_path(None) is None
    assert tuple(
        _valid_change(status).status for status in ("added", "modified", "deleted", "renamed")
    ) == (
        "added",
        "modified",
        "deleted",
        "renamed",
    )


@pytest.mark.parametrize("path", ["", "/absolute", "a\\b", "a//b", "a/./b", "a/../b"])
def test_git_change_rejects_noncanonical_paths(path: str) -> None:
    with pytest.raises(ValueError):
        GitPathChange._canonical_path(path)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "added", "newPath": "new.py"},
        {
            "status": "modified",
            "oldPath": "a.py",
            "newPath": "b.py",
            "oldBlob": "1" * 40,
            "newBlob": "2" * 40,
        },
        {
            "status": "renamed",
            "oldPath": "a.py",
            "newPath": "a.py",
            "oldBlob": "1" * 40,
            "newBlob": "2" * 40,
        },
    ],
)
def test_git_change_rejects_invalid_status_shapes(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        GitPathChange.model_validate(payload)


def test_git_delta_rejects_bad_roots_order_and_duplicates() -> None:
    added = _valid_change("added")
    deleted = _valid_change("deleted")
    for root in ("relative", "/work/../escape"):
        with pytest.raises(ValidationError):
            _delta("code", root=root, base="1" * 40, candidate="2" * 40)
    with pytest.raises(ValidationError):
        _delta(
            "code",
            root="/work/code",
            base="1" * 40,
            candidate="2" * 40,
            changes=(deleted, added),
        )
    with pytest.raises(ValidationError):
        _delta(
            "code",
            root="/work/code",
            base="1" * 40,
            candidate="2" * 40,
            changes=(added, added),
        )


def test_task_and_index_roots_are_exact_absolute_paths() -> None:
    task_payload: dict[str, Any] = {
        "taskRoot": "relative",
        "taskDocumentRef": {"repository": "repo", "path": "master/leaf.json"},
        "sourceDigest": "a" * 64,
        "sourceAuthorityNamespace": "agents-remember.task-document-source",
        "sourceValidatorVersion": "fixture/v1",
        "semanticTopologyDigest": "b" * 64,
        "taskIntent": {"state": "resolved", "schema": "task-intent/v1", "digest": "c" * 64},
    }
    with pytest.raises(ValidationError):
        CanonicalTaskObservation.model_validate(task_payload)
    with pytest.raises(ValidationError):
        SourceIndexObservation(
            snapshotId="a" * 64,
            codeRoot="relative",
            memoryRoot="/work/memory",
            candidateDigest="b" * 64,
        )


def test_candidate_requires_exact_namespaces_roots_and_tree_changes() -> None:
    pair = _pair()
    code = _delta("code", root=pair.codeRoot, base="3" * 40, candidate="4" * 40)
    memory = _delta("memory", root=pair.memoryRoot, base="5" * 40, candidate="6" * 40)
    cases = (
        {"code": memory, "memory": code},
        {"code": code.model_copy(update={"root": "/other"}), "memory": memory},
        {
            "code": code.model_copy(
                update={
                    "candidateTree": code.baseTree,
                    "changes": (_valid_change("added"),),
                }
            ),
            "memory": memory,
        },
        {
            "code": code,
            "memory": memory.model_copy(
                update={
                    "candidateTree": memory.baseTree,
                    "changes": (_valid_change("added"),),
                }
            ),
        },
    )
    for values in cases:
        with pytest.raises(ValidationError):
            ScopeCandidateIdentity.model_validate({"pairIdentity": pair, "task": _task(), **values})


@pytest.mark.parametrize("node_id", ["bad", "other:a.py", "code:/a.py", "code:a/../b"])
def test_node_identity_and_reason_evidence_fail_closed(node_id: str) -> None:
    with pytest.raises(ValidationError):
        ScopeNode(
            nodeId=node_id,
            contentDigest="a" * 64,
            authorityNamespace="owner",
            validatorVersion="v1",
            reasons=("reason",),
        )
    with pytest.raises(ValidationError):
        ScopeNode(
            nodeId="code:a.py",
            contentDigest="a" * 64,
            authorityNamespace="owner",
            validatorVersion="v1",
            reasons=("same", "same"),
        )


def test_edge_reason_evidence_is_nonblank_unique_and_sorted() -> None:
    with pytest.raises(ValidationError):
        ScopeEdge(
            source="code:a.py",
            target="memory:onboarding/a.py.md",
            edgeClass="source-to-file-sidecar",
            contentDigest="a" * 64,
            authorityNamespace="owner",
            extractorVersion="v1",
            validatorVersion="v1",
            reasons=("",),
        )


def test_scope_error_preserves_all_optional_authority_evidence() -> None:
    empty = ScopeUnprovenError(ScopeFailure(code="missing", detail="detail"))
    assert empty.response_fields() == {
        "status": "scope-unproven",
        "reason": "missing",
        "detail": "detail",
    }
    failure = ScopeFailure(
        code="invalid",
        detail="detail",
        checker="check",
        node="code:a.py",
        edge_class="source-to-file-sidecar",
        snapshot="a" * 64,
        candidate="b" * 64,
        owner="owner",
    )
    assert ScopeUnprovenError(failure).response_fields() == {
        "status": "scope-unproven",
        "reason": "invalid",
        "detail": "detail",
        "checker": "check",
        "node": "code:a.py",
        "edgeClass": "source-to-file-sidecar",
        "snapshot": "a" * 64,
        "candidateDigest": "b" * 64,
        "owner": "owner",
    }


def test_registry_rejects_population_and_policy_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policies = registry.checker_scope_registry()
    monkeypatch.setattr(registry, "AVAILABLE_CHECKS", (*registry.AVAILABLE_CHECKS, "new-check"))
    with pytest.raises(ValueError, match=r"missing=.*new-check"):
        registry.checker_scope_registry()

    incremental = CheckerScopePolicy(
        checker="one",
        mode="incremental",
        reason="missing extractor",
    )
    monkeypatch.setattr(registry, "AVAILABLE_CHECKS", ("one",))
    monkeypatch.setattr(registry, "_POLICIES", (incremental,))
    with pytest.raises(ValueError, match="lacks an executable policy"):
        registry.checker_scope_registry()

    full = CheckerScopePolicy(
        checker="one",
        mode="full-only",
        extractorVersion="impossible/v1",
        edgeClasses=("source-to-file-sidecar",),
        reason="claims incremental facts",
    )
    monkeypatch.setattr(registry, "_POLICIES", (full,))
    with pytest.raises(ValueError, match="claims incremental semantics"):
        registry.checker_scope_registry()

    assert policies
    assert canonical_digest([policy.checker for policy in policies])
