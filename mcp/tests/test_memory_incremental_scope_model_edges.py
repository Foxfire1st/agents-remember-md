"""Validator, typed-error, and checker-registry edges for R06 scope evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents_remember.memory_quality.check import AVAILABLE_CHECKS
from agents_remember.memory_quality.incremental_scope import (
    affected_execution,
    affected_planning,
    execution_registry,
    registry,
    subresult_store,
)
from agents_remember.memory_quality.incremental_scope.affected_execution import (
    AffectedClosureExecution,
    RangeResolutionAffectedExecutor,
    RangeResolutionExecutionContext,
    execute_affected_closure,
    plan_affected_subresult_reuse,
)
from agents_remember.memory_quality.incremental_scope.compiler import (
    build_dependency_snapshot,
    compile_scope_manifest,
)
from agents_remember.memory_quality.incremental_scope.errors import (
    GateFiveClosureRefusedError,
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
from agents_remember.memory_quality.incremental_scope.owners import (
    extract_citation_edges,
    observe_git_nodes,
    observe_source_index,
)
from agents_remember.memory_quality.incremental_scope.subresult_store import (
    ContentAddressedSubresultStore,
    SubresultStorePolicy,
)
from agents_remember.memory_quality.style.citations import range_resolution, source_index
from agents_remember.memory_quality.style.citations.resolution import Trees
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity
from pydantic import ValidationError
from test_memory_incremental_scope_compiler import (
    R07RecordingExecutor,
    R07StableAuthority,
    R07StaticDependencies,
    _r07_admission,
    _r07_candidate,
    _r07_plan,
    _r07_scope,
)
from test_memory_incremental_scope_owners import _actual_candidate, _git, _linked_candidate


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


def _r07_store_result(monkeypatch: pytest.MonkeyPatch, *, ok: bool = True):
    candidate, plan = _r07_plan(monkeypatch)
    result = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(candidate), R07RecordingExecutor(ok=ok)),
        plan,
    )
    return candidate, plan, result.subresults[0]


def _r07_store(root: Path, *, max_objects: int = 16, max_bytes: int = 1_000_000):
    return ContentAddressedSubresultStore(
        root,
        SubresultStorePolicy(
            scopeId="operation-1",
            maxObjects=max_objects,
            maxBytes=max_bytes,
            reclamationOwner="gate-five-operation-owner",
        ),
    )


def _r07_store_reason(error: pytest.ExceptionInfo[GateFiveClosureRefusedError]) -> str:
    return error.value.failure.code


def test_r07_subresult_store_is_exact_atomic_bounded_and_has_no_latest_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, _, result = _r07_store_result(monkeypatch)
    store = _r07_store(tmp_path / "objects")

    path = store.publish(result)
    assert store.load(result.resultDigest) == result
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = tuple(pool.map(lambda _: store.publish(result), range(8)))
    assert set(paths) == {path}

    with pytest.raises(GateFiveClosureRefusedError) as caught:
        store.load("latest")
    assert _r07_store_reason(caught) == "subresult-digest-invalid"
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        store.load("f" * 64)
    assert _r07_store_reason(caught) == "subresult-missing"

    bounded = _r07_store(tmp_path / "bounded", max_objects=1)
    assert bounded.publish(result).is_file()
    _, _, second = _r07_store_result(monkeypatch, ok=False)
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        bounded.publish(second)
    assert _r07_store_reason(caught) == "subresult-store-capacity-exceeded"

    forged = result.model_copy(update={"resultDigest": "f" * 64})
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        store.publish(forged)
    assert _r07_store_reason(caught) == "subresult-invalid"


def test_r07_subresult_load_refuses_corrupt_or_wrongly_addressed_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, _, result = _r07_store_result(monkeypatch)
    store = _r07_store(tmp_path / "objects")
    path = store.publish(result)
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        store.load(result.resultDigest)
    assert _r07_store_reason(caught) == "subresult-invalid"


def test_r07_range_executor_uses_one_planned_document_and_exact_live_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _r07_plan(monkeypatch)
    unit = plan.units[0]
    index = cast(
        source_index.RepositoryIndex,
        SimpleNamespace(snapshot_id=unit.sourceIndexSnapshot, candidate_tree=unit.codeTree),
    )
    calls: list[dict[str, object]] = []

    def selected(onboarding, code, **kwargs):
        calls.append({"onboarding": onboarding, "code": code, **kwargs})
        return {
            "check": unit.checker,
            "status": "checked",
            "ok": True,
            "filesChecked": 1,
            "findingCount": 0,
            "findings": [],
        }

    monkeypatch.setattr(range_resolution, "check_onboarding_root", selected)
    executor = RangeResolutionAffectedExecutor(
        RangeResolutionExecutionContext(
            codeRoot=Path(plan.codeRoot),
            memoryRoot=Path(plan.memoryRoot),
            onboardingRoot=Path(plan.onboardingRoot),
            citationIndex=index,
        )
    )
    assert executor.execute(plan, unit)["ok"] is True
    assert calls == [
        {
            "onboarding": Path(plan.onboardingRoot),
            "code": Trees(Path(plan.codeRoot), Path(plan.memoryRoot), candidate_tree=unit.codeTree),
            "only": unit.document,
            "index": index,
        }
    ]
    assert "expected_snapshot" not in calls[0]


def test_r07_range_executor_refuses_wrong_root_or_source_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _r07_plan(monkeypatch)
    unit = plan.units[0]
    stale_index = cast(
        source_index.RepositoryIndex,
        SimpleNamespace(snapshot_id="f" * 64),
    )
    executor = RangeResolutionAffectedExecutor(
        RangeResolutionExecutionContext(
            codeRoot=Path(plan.codeRoot),
            memoryRoot=Path(plan.memoryRoot),
            onboardingRoot=Path(plan.onboardingRoot),
            citationIndex=stale_index,
        )
    )
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        executor.execute(plan, unit)
    assert _r07_store_reason(caught) == "checker-source-index-stale"

    exact_index = cast(
        source_index.RepositoryIndex,
        SimpleNamespace(snapshot_id=unit.sourceIndexSnapshot, candidate_tree=unit.codeTree),
    )
    wrong_root = RangeResolutionAffectedExecutor(
        RangeResolutionExecutionContext(
            codeRoot=Path("/other/code"),
            memoryRoot=Path(plan.memoryRoot),
            onboardingRoot=Path(plan.onboardingRoot),
            citationIndex=exact_index,
        )
    )
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        wrong_root.execute(plan, unit)
    assert _r07_store_reason(caught) == "checker-execution-root-mismatch"


@pytest.mark.parametrize(
    ("citation", "expected_code"),
    (
        ("src/anchor.py:1-2", None),
        ("src/old.py:1-1", "citation_anchor_absent_from_range"),
        ("generated/output.js:1-1", "citation_source_vanished"),
    ),
)
def test_r07_real_range_checker_uses_only_the_candidate_source_population(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    citation: str,
    expected_code: str | None,
) -> None:
    _, trees = _linked_candidate(tmp_path, monkeypatch)
    code, memory = trees.code_root, trees.memory_root
    onboarding = memory / "onboarding"
    (code / "generated/output.js").write_text("function scope_anchor() { return 2; }\n")
    (onboarding / "claims.md").write_text(f"Claim cit:([`scope_anchor`], {citation}).\n")
    _git(memory, "add", "onboarding/claims.md")
    candidate = _actual_candidate(code, memory).model_copy(update={"task": _r07_candidate().task})
    authority = R07StableAuthority(candidate)
    with source_index.open_repository_index(trees) as index:
        nodes = {
            node.nodeId: node
            for node in (
                *observe_git_nodes(code, candidate.code),
                *observe_git_nodes(memory, candidate.memory),
            )
        }
        snapshot = build_dependency_snapshot(
            candidate=candidate,
            source_index=observe_source_index(candidate, index),
            nodes=nodes.values(),
            edges=extract_citation_edges(candidate, onboarding, nodes),
        )
        scope = compile_scope_manifest(
            authority, R07StaticDependencies(snapshot), checkers=AVAILABLE_CHECKS
        )
        plan = affected_planning.compile_affected_closure_plan(
            _r07_admission(monkeypatch, candidate), scope
        )
        assert len(plan.units) == 1
        executor = RangeResolutionAffectedExecutor(
            RangeResolutionExecutionContext(code, memory, onboarding, index)
        )
        result = executor.execute(plan, plan.units[0])
        assert result["filesChecked"] == 1
        assert result["ok"] is (expected_code is None)
        findings = cast(list[dict[str, object]], result["findings"])
        assert [item["code"] for item in findings] == (
            [] if expected_code is None else [expected_code]
        )
        if expected_code == "citation_anchor_absent_from_range":
            message = str(findings[0]["message"])
            assert "src/anchor.py" in message
            assert "generated/output.js" not in message


@pytest.mark.parametrize("candidate_tree", (None, "f" * 40))
def test_r07_range_executor_refuses_another_candidate_before_checker_start(
    monkeypatch: pytest.MonkeyPatch,
    candidate_tree: str | None,
) -> None:
    _, plan = _r07_plan(monkeypatch)
    unit = plan.units[0]
    index = cast(
        source_index.RepositoryIndex,
        SimpleNamespace(snapshot_id=unit.sourceIndexSnapshot, candidate_tree=candidate_tree),
    )
    calls: list[object] = []
    monkeypatch.setattr(
        range_resolution, "check_onboarding_root", lambda *args, **kw: calls.append(args)
    )
    executor = RangeResolutionAffectedExecutor(
        RangeResolutionExecutionContext(
            Path(plan.codeRoot), Path(plan.memoryRoot), Path(plan.onboardingRoot), index
        )
    )
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        executor.execute(plan, unit)
    assert _r07_store_reason(caught) == "checker-source-index-candidate-mismatch"
    assert calls == []


def test_r07_invalid_plan_and_checker_exception_refuse_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)
    forged = plan.model_copy(update={"acceptanceEligible": True})
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        plan_affected_subresult_reuse(forged, ())
    assert _r07_store_reason(caught) == "affected-plan-invalid"

    class BrokenExecutor(R07RecordingExecutor):
        def execute(self, plan, unit) -> dict[str, object]:
            del plan, unit
            raise RuntimeError("fixture checker failure")

    with pytest.raises(GateFiveClosureRefusedError) as caught:
        execute_affected_closure(
            AffectedClosureExecution(R07StableAuthority(candidate), BrokenExecutor()),
            plan,
        )
    assert _r07_store_reason(caught) == "checker-execution-failed"


@pytest.mark.parametrize(
    ("status", "files_checked"),
    (("checked", 0), ("checked", 2), ("blocked", 1)),
)
def test_r07_checker_status_requires_exact_selected_document_count(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    files_checked: int,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)

    class ContradictoryExecutor(R07RecordingExecutor):
        def execute(self, plan, unit) -> dict[str, object]:
            del plan
            return {
                "check": unit.checker,
                "status": status,
                "ok": status == "checked",
                "code": "blocked-fixture" if status == "blocked" else None,
                "filesChecked": files_checked,
                "findingCount": 0,
                "findings": [],
            }

    with pytest.raises(GateFiveClosureRefusedError) as caught:
        execute_affected_closure(
            AffectedClosureExecution(R07StableAuthority(candidate), ContradictoryExecutor()),
            plan,
        )
    assert _r07_store_reason(caught) == "checker-result-unproven"


def test_r07_checker_executor_and_typed_refusal_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)
    unit = plan.units[0]
    executor = RangeResolutionAffectedExecutor(
        RangeResolutionExecutionContext(
            codeRoot=Path(plan.codeRoot),
            memoryRoot=Path(plan.memoryRoot),
            onboardingRoot=Path(plan.onboardingRoot),
            citationIndex=cast(
                source_index.RepositoryIndex,
                SimpleNamespace(snapshot_id=unit.sourceIndexSnapshot, candidate_tree=unit.codeTree),
            ),
        )
    )
    assert executor.registry_version == plan.executionRegistryVersion

    with pytest.raises(GateFiveClosureRefusedError) as caught:
        executor._validate_context(plan, unit.model_copy(update={"checker": "unknown"}))
    assert _r07_store_reason(caught) == "checker-executor-unknown"

    refusal = GateFiveClosureRefusedError(ScopeFailure(code="typed", detail="fixture"))

    class TypedRefusalExecutor(R07RecordingExecutor):
        def execute(self, plan, unit) -> dict[str, object]:
            del plan, unit
            raise refusal

    with pytest.raises(GateFiveClosureRefusedError) as caught:
        execute_affected_closure(
            AffectedClosureExecution(R07StableAuthority(candidate), TypedRefusalExecutor()),
            plan,
        )
    assert caught.value is refusal


def test_r07_checker_evidence_refuses_every_unproven_or_noncanonical_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _r07_plan(monkeypatch)
    unit = plan.units[0]
    valid: dict[str, object] = {
        "check": unit.checker,
        "status": "checked",
        "ok": True,
        "filesChecked": 1,
        "findingCount": 0,
        "findings": [],
    }
    cases: tuple[tuple[object, str], ...] = (
        ({**valid, "check": "other"}, "checker-result-identity-mismatch"),
        ({**valid, "status": "unknown"}, "checker-result-unproven"),
        ({**valid, "ok": 1}, "checker-result-unproven"),
        (
            {**valid, "status": "blocked", "ok": True, "filesChecked": 0},
            "checker-result-unproven",
        ),
        ({**valid, "filesChecked": -1}, "checker-result-unproven"),
        ({"not": object()}, "checker-result-not-canonical-json"),
        (["not-an-object"], "checker-result-not-object"),
    )
    for evidence, reason in cases:
        with pytest.raises(GateFiveClosureRefusedError) as caught:
            affected_execution._unit_result(unit, cast(Any, evidence))
        assert _r07_store_reason(caught) == reason


def test_r07_unit_and_member_plan_models_refuse_noncanonical_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _r07_plan(monkeypatch)
    unit = next(item for item in plan.units if item.document == "claims.md")

    with pytest.raises(ValueError, match="canonical relative"):
        unit._canonical_document("/absolute.md")
    with pytest.raises(ValueError, match="dependencies must be unique"):
        cast(
            Any,
            unit.model_copy(update={"dependencies": tuple(reversed(unit.dependencies))}),
        )._require_canonical_unit()
    with pytest.raises(ValueError, match="dependency edges must be unique"):
        cast(
            Any,
            unit.model_copy(update={"dependencyEdges": tuple(reversed(unit.dependencyEdges))}),
        )._require_canonical_unit()
    dependencies = tuple(sorted((*unit.dependencies, unit.node), key=lambda item: item.nodeId))
    with pytest.raises(ValueError, match="exclude the checked node"):
        cast(Any, unit.model_copy(update={"dependencies": dependencies}))._require_canonical_unit()
    with pytest.raises(ValueError, match="digest does not match"):
        cast(Any, unit.model_copy(update={"unitDigest": "0" * 64}))._require_canonical_unit()

    checked = next(item for item in plan.members if item.disposition == "check-target")
    dependency = next(item for item in plan.members if item.disposition == "dependency-input")
    with pytest.raises(ValueError, match="unit digests must be unique"):
        cast(
            Any,
            checked.model_copy(
                update={"unitDigests": (*checked.unitDigests, *checked.unitDigests)}
            ),
        )._require_member_shape()
    with pytest.raises(ValueError, match="only a check-target"):
        cast(
            Any, dependency.model_copy(update={"disposition": "check-target"})
        )._require_member_shape()


def test_r07_closure_plan_model_refuses_incomplete_or_rebound_populations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _r07_plan(monkeypatch)
    with pytest.raises(ValueError, match="normalized absolute"):
        plan._absolute_root("relative")
    with pytest.raises(ValueError, match="plan digest"):
        cast(Any, plan.model_copy(update={"planDigest": "0" * 64}))._require_complete_population()
    with pytest.raises(ValueError, match="exact R06 selected population"):
        plan.model_copy(update={"members": plan.members[:-1]})._require_member_population()
    with pytest.raises(ValueError, match="affected units must be unique"):
        plan.model_copy(update={"units": tuple(reversed(plan.units))})._require_member_population()

    checked_index = next(
        index for index, member in enumerate(plan.members) if member.disposition == "check-target"
    )
    missing_units = list(plan.members)
    missing_units[checked_index] = missing_units[checked_index].model_copy(
        update={"unitDigests": ()}
    )
    with pytest.raises(ValueError, match="account for every exact unit"):
        plan.model_copy(update={"members": tuple(missing_units)})._require_member_population()

    checked_indexes = tuple(
        index for index, member in enumerate(plan.members) if member.disposition == "check-target"
    )
    wrong_owners = list(plan.members)
    first, second = checked_indexes[:2]
    first_units = wrong_owners[first].unitDigests
    wrong_owners[first] = wrong_owners[first].model_copy(
        update={"unitDigests": wrong_owners[second].unitDigests}
    )
    wrong_owners[second] = wrong_owners[second].model_copy(update={"unitDigests": first_units})
    with pytest.raises(ValueError, match="belong to their exact selected node"):
        plan.model_copy(update={"members": tuple(wrong_owners)})._require_member_population()

    with pytest.raises(ValueError, match="pending-final-full checkers must be unique"):
        plan.model_copy(
            update={"pendingFinalFull": tuple(reversed(plan.pendingFinalFull))}
        )._require_pending_population()
    with pytest.raises(ValueError, match="cannot hide"):
        plan.model_copy(
            update={"pendingFinalFull": plan.pendingFinalFull[:-1]}
        )._require_pending_population()
    with pytest.raises(ValueError, match="coherence subrecords must be unique"):
        plan.model_copy(
            update={"affectedCoherenceSubrecords": ("second", "first")}
        )._require_authority_inputs()
    with pytest.raises(ValueError, match="exact green Gate 1-4 prefix"):
        plan.model_copy(
            update={"gateCertificates": tuple(reversed(plan.gateCertificates))}
        )._require_authority_inputs()
    with pytest.raises(ValueError, match="exact R06 candidate and roots"):
        plan.model_copy(update={"candidateDigest": "0" * 64})._require_authority_inputs()
    with pytest.raises(ValueError, match="inside the memory root"):
        plan.model_copy(update={"onboardingRoot": "/elsewhere"})._onboarding_node_prefix()
    with pytest.raises(ValueError, match="every proven incremental"):
        plan.model_copy(update={"units": plan.units[:-1]})._require_unit_population(
            plan._onboarding_node_prefix()
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("codeTree", "0" * 40),
        ("sourceIndexSnapshot", "0" * 64),
        ("checkerRegistryVersion", "0" * 64),
        ("executionRegistryVersion", "0" * 64),
    ),
)
def test_r07_closure_plan_units_retain_every_exact_input_identity(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    _, plan = _r07_plan(monkeypatch)
    with pytest.raises(ValueError, match="exact plan input identities"):
        plan._require_unit_inputs(plan.units[0].model_copy(update={field: value}))


def test_r07_result_and_reuse_models_refuse_inconsistent_exact_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan, unit_result = _r07_store_result(monkeypatch)
    with pytest.raises(ValueError, match="status must match its finding count"):
        cast(Any, unit_result.model_copy(update={"status": "fail"}))._verify_result()
    with pytest.raises(ValueError, match="selected-document count"):
        cast(Any, unit_result.model_copy(update={"filesChecked": 0}))._verify_result()
    with pytest.raises(ValueError, match="evidence digest"):
        cast(Any, unit_result.model_copy(update={"evidenceDigest": "0" * 64}))._verify_result()

    result = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(_r07_candidate()), R07RecordingExecutor()),
        plan,
    )
    checked = next(item for item in result.memberResults if item.disposition == "checked")
    dependency = next(
        item for item in result.memberResults if item.disposition == "dependency-input"
    )
    with pytest.raises(ValueError, match="unit digests must be unique"):
        cast(
            Any,
            checked.model_copy(
                update={
                    "unitResultDigests": (*checked.unitResultDigests, *checked.unitResultDigests)
                }
            ),
        )._require_result_shape()
    with pytest.raises(ValueError, match="checked members require terminal"):
        cast(Any, dependency.model_copy(update={"disposition": "checked"}))._require_result_shape()
    with pytest.raises(ValueError, match="member result digest"):
        cast(Any, checked.model_copy(update={"resultDigest": "0" * 64}))._require_result_shape()

    reuse = plan_affected_subresult_reuse(plan, ())
    first_digest = reuse.unitsToExecute[0]
    with pytest.raises(ValueError, match="populations must be unique"):
        cast(
            Any, reuse.model_copy(update={"unitsToExecute": (first_digest, first_digest)})
        )._verify_reuse()
    with pytest.raises(ValueError, match="both reused and executed"):
        cast(Any, reuse.model_copy(update={"reusedUnitDigests": (first_digest,)}))._verify_reuse()
    with pytest.raises(ValueError, match="subresult-reuse digest"):
        cast(Any, reuse.model_copy(update={"reusePlanDigest": "0" * 64}))._verify_reuse()


def test_r07_aggregate_model_refuses_incomplete_or_inconsistent_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)
    result = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(candidate), R07RecordingExecutor()),
        plan,
    )
    planned = tuple(item.unitDigest for item in plan.units)

    with pytest.raises(ValueError, match="closure result digest"):
        cast(Any, result.model_copy(update={"resultDigest": "0" * 64}))._require_complete_result()
    with pytest.raises(ValueError, match="exact planned unit population"):
        result.model_copy(
            update={"subresults": tuple(reversed(result.subresults))}
        )._require_result_population(planned)
    with pytest.raises(ValueError, match="exact planned population"):
        result.model_copy(
            update={"memberResults": tuple(reversed(result.memberResults))}
        )._require_result_population(planned)
    with pytest.raises(ValueError, match="partition the plan"):
        result.model_copy(
            update={"executedUnitDigests": planned[:-1]}
        )._require_execution_partition(planned)
    with pytest.raises(ValueError, match="partition the plan"):
        result.model_copy(
            update={
                "reusedUnitDigests": (planned[0],),
                "executedUnitDigests": (planned[0],),
            }
        )._require_execution_partition((planned[0], planned[0]))
    with pytest.raises(ValueError, match="cannot hide pending-final-full"):
        result.model_copy(
            update={"pendingFinalFull": result.pendingFinalFull[:-1]}
        )._require_pending_population()
    with pytest.raises(ValueError, match="incremental readiness"):
        result.model_copy(update={"incrementalMemoryReady": False})._require_terminal_state()
    with pytest.raises(ValueError, match="terminal status"):
        result.model_copy(update={"terminalStatus": "fail"})._require_terminal_state()

    member_results = list(result.memberResults)
    checked_index = next(
        index for index, member in enumerate(member_results) if member.disposition == "checked"
    )
    member_results[checked_index] = member_results[checked_index].model_copy(
        update={"unitResultDigests": ()}
    )
    rebound = result.model_copy(update={"memberResults": tuple(member_results)})
    with pytest.raises(ValueError, match="bind every exact planned unit result"):
        rebound._require_member_results({item.unit.unitDigest: item for item in rebound.subresults})


def _r07_planning_reason(action: Any) -> str:
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        action()
    return _r07_store_reason(caught)


def test_r07_planning_refuses_stale_scope_registry_edges_and_gate_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _r07_candidate()
    scope = _r07_scope(candidate)
    expected = registry.checker_scope_registry()

    assert (
        _r07_planning_reason(
            lambda: affected_planning._validate_scope_identity(
                candidate, scope.model_copy(update={"candidateDigest": "0" * 64})
            )
        )
        == "affected-scope-candidate-mismatch"
    )
    stale_index = scope.sourceIndex.model_copy(update={"candidateDigest": "0" * 64})
    assert (
        _r07_planning_reason(
            lambda: affected_planning._validate_scope_identity(
                candidate, scope.model_copy(update={"sourceIndex": stale_index})
            )
        )
        == "affected-source-index-stale"
    )
    assert (
        _r07_planning_reason(
            lambda: affected_planning._validate_scope_identity(
                candidate, scope.model_copy(update={"manifestDigest": "0" * 64})
            )
        )
        == "affected-scope-digest-invalid"
    )
    assert (
        _r07_planning_reason(
            lambda: affected_planning._validate_scope_registry(
                scope.model_copy(update={"checkerRegistryVersion": "0" * 64}), expected
            )
        )
        == "affected-checker-population-incomplete"
    )
    assert (
        _r07_planning_reason(
            lambda: affected_planning._validate_scope_registry(
                scope.model_copy(update={"fullOnlyCheckers": ()}), expected
            )
        )
        == "affected-checker-disposition-invalid"
    )
    assert (
        _r07_planning_reason(
            lambda: affected_planning._validate_scope_registry(
                scope.model_copy(update={"incrementalReady": True}), expected
            )
        )
        == "affected-checker-disposition-invalid"
    )
    outside_edge = scope.selectedEdges[0].model_copy(update={"source": "memory:unknown.md"})
    assert (
        _r07_planning_reason(
            lambda: affected_planning._validate_scope_edges(
                scope.model_copy(update={"selectedEdges": (outside_edge,)})
            )
        )
        == "affected-edge-endpoint-missing"
    )

    admission = _r07_admission(monkeypatch, candidate)
    short_prefix = affected_planning.AffectedClosureAdmission(
        candidateAuthority=admission.candidateAuthority,
        certificationAdmission=admission.certificationAdmission,
        gateCertificates=admission.gateCertificates[:-1],
    )
    assert (
        _r07_planning_reason(
            lambda: affected_planning._admit_gate_certificates(short_prefix, candidate)
        )
        == "gate-certificate-prefix-incomplete"
    )
    wrong_code = affected_planning.AffectedClosureAdmission(
        candidateAuthority=admission.candidateAuthority,
        certificationAdmission=cast(
            Any,
            SimpleNamespace(
                semanticEnvelope=SimpleNamespace(candidateCodeTree=SimpleNamespace(value="0" * 40))
            ),
        ),
        gateCertificates=admission.gateCertificates,
    )
    assert (
        _r07_planning_reason(
            lambda: affected_planning._admit_gate_certificates(wrong_code, candidate)
        )
        == "gate-certificate-code-candidate-mismatch"
    )

    def invalid_reuse(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ValueError("fixture currentness refusal")

    monkeypatch.setattr(affected_planning, "plan_certificate_reuse", invalid_reuse)
    assert (
        _r07_planning_reason(
            lambda: affected_planning._admit_gate_certificates(admission, candidate)
        )
        == "gate-certificate-currentness-unproven"
    )


def test_r07_planning_closure_targets_are_complete_and_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _r07_candidate()
    scope = _r07_scope(candidate)
    incremental = next(item for item in scope.checkerPolicies if item.mode == "incremental")
    assert (
        _r07_planning_reason(
            lambda: affected_planning._unit(
                scope.selectedNodes[0], None, incremental, cast(Any, None)
            )
        )
        == "incremental-checker-contract-incomplete"
    )

    target = scope.selectedEdges[0].target
    self_edge = scope.selectedEdges[0].model_copy(update={"source": target, "target": target})
    dependencies, edges = affected_planning._dependency_closure(
        target,
        (self_edge,),
        {item.nodeId: item for item in scope.selectedNodes},
    )
    assert dependencies == ()
    assert edges == (self_edge,)

    outside_pair = candidate.pairIdentity.model_copy(update={"onboardingRoot": "/elsewhere"})
    assert (
        _r07_planning_reason(
            lambda: affected_planning._onboarding_relative(
                candidate.model_copy(update={"pairIdentity": outside_pair})
            )
        )
        == "onboarding-root-outside-memory"
    )
    root_pair = candidate.pairIdentity.model_copy(update={"onboardingRoot": candidate.memory.root})
    assert (
        affected_planning._onboarding_relative(
            candidate.model_copy(update={"pairIdentity": root_pair})
        )
        == ""
    )
    assert affected_planning._document_relative("memory:onboarding/../bad.md", "onboarding") is None


def test_r07_execution_registry_refuses_incomplete_population(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_registry, "_EXECUTION_POLICIES", ())
    with pytest.raises(ValueError, match="execution registry is incomplete"):
        execution_registry.checker_execution_registry()


def test_r07_subresult_store_refuses_collision_readback_and_wrong_address(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, _, result = _r07_store_result(monkeypatch)

    collision_store = _r07_store(tmp_path / "collision")
    collision_path = collision_store.exact_path(result.resultDigest)
    collision_path.parent.mkdir(parents=True)
    collision_path.write_bytes(b"different\n")
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        collision_store.publish(result)
    assert _r07_store_reason(caught) == "subresult-content-address-collision"

    readback_store = _r07_store(tmp_path / "readback")

    def wrong_atomic_write(path: Path, payload: bytes) -> None:
        del payload
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wrong\n")

    monkeypatch.setattr(subresult_store, "atomic_write_bytes", wrong_atomic_write)
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        readback_store.publish(result)
    assert _r07_store_reason(caught) == "subresult-readback-mismatch"
    monkeypatch.undo()

    exact_store = _r07_store(tmp_path / "address")
    exact_path = exact_store.publish(result)
    wrong_digest = "0" * 64
    wrong_path = exact_store.exact_path(wrong_digest)
    wrong_path.parent.mkdir(parents=True, exist_ok=True)
    wrong_path.write_bytes(exact_path.read_bytes())
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        exact_store.load(wrong_digest)
    assert _r07_store_reason(caught) == "subresult-address-mismatch"


def test_r07_subresult_store_refuses_nonregular_or_unreadable_objects(
    tmp_path: Path,
) -> None:
    capacity_store = _r07_store(tmp_path / "capacity")
    nonregular = tmp_path / "capacity" / "sha256" / "aa" / "unsafe.json"
    nonregular.mkdir(parents=True)
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        capacity_store._require_capacity(1)
    assert _r07_store_reason(caught) == "subresult-store-object-invalid"

    direct_nonregular = tmp_path / "direct.json"
    direct_nonregular.mkdir()
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        subresult_store._read_regular_file(direct_nonregular, missing_ok=False)
    assert _r07_store_reason(caught) == "subresult-unsafe"
