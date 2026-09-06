"""Direct-input evidence declarations remain typed, bounded, and acyclic."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, get_args

import pytest
from agents_remember.application.memory_quality import controller as memory_quality_controller
from agents_remember.errors import CuratorCoherenceError, MemoryCandidatePairError
from agents_remember.models.lifecycles.curator_coherence import (
    CuratorQualityAttestation,
    memory_quality_attestation_dependencies,
    require_memory_quality_attestation_dependencies,
)
from agents_remember.models.lifecycles.door import (
    CloseoutDoorRequest,
    require_closeout_door_dependencies,
)
from agents_remember.models.lifecycles.evidence_dependencies import (
    EVIDENCE_DEPENDENCY_POLICIES,
    EVIDENCE_DEPENDENCY_VALIDATOR,
    EvidenceDependencies,
    EvidenceDependencyError,
    EvidenceRecordType,
    build_evidence_dependencies,
    canonical_sha256,
    dependency,
    require_evidence_dependencies,
    validate_evidence_dependency_graph,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationRecord,
    lifecycle_operation_dependencies,
    require_lifecycle_operation_dependencies,
)
from agents_remember.models.task_intent import MissingTaskIntent, TaskIntentIdentity
from agents_remember.tasks.document import RouteReviewRecord
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.worktrees import route_review as route_review_module
from agents_remember.worktrees.integration.closeout import curator_coherence as coherence
from agents_remember.worktrees.integration.closeout import door_source as door_source_module
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract
from pydantic import ValidationError
from test_closeout_generation_boundary import _publish_mutated_code_generation
from test_closeout_queue import MASTER_A, QueueFixture
from test_lifecycle_operations import _contract
from test_task_intent_consumers_and_legacy import (
    LEAF,
    _closeout_record_payload,
    _leaf_document,
    _route_review_author_payload,
)


def _route_review(*, review_target: str | None = None) -> EvidenceDependencies:
    edges = [
        dependency("code-tree", "candidate", "a" * 40, algorithm="git-object"),
        dependency("evidence-bytes", "reviews/verdict.md", "b" * 64),
        dependency("task-intent", "leaf", "c" * 64),
        dependency("validator", EVIDENCE_DEPENDENCY_VALIDATOR, "d" * 64),
    ]
    if review_target is not None:
        edges.append(dependency("review-record", "cycle-target", review_target))
    return build_evidence_dependencies("route-review/v1", edges)


def _door(*, review_digest: str, predecessor: str | None = None) -> EvidenceDependencies:
    edges = [
        dependency("admission", "candidate-admission", "1" * 64),
        dependency("code-tree", "candidate", "2" * 40, algorithm="git-object"),
        dependency("ledger-provenance", "ledger", "3" * 64),
        dependency("coherence-record", "memory", "4" * 64),
        dependency("review-record", "route-review", review_digest),
        dependency("scheduling", "grade", "5" * 64),
        dependency("semantic-topology", "leaf-placement", "6" * 64),
        dependency("task-intent", "leaf", "7" * 64),
        dependency("validator", EVIDENCE_DEPENDENCY_VALIDATOR, "8" * 64),
    ]
    if predecessor is not None:
        edges.append(dependency("predecessor-record", "prior-door", predecessor))
    return build_evidence_dependencies("closeout-door/v1", edges)


def test_every_record_type_has_one_versioned_policy() -> None:
    assert set(get_args(EvidenceRecordType)) == set(EVIDENCE_DEPENDENCY_POLICIES)


def test_builder_canonicalizes_edges_and_fingerprint_uses_only_declared_inputs() -> None:
    first = _route_review()
    second = _route_review()
    assert first.edges == tuple(sorted(first.edges, key=lambda edge: edge.sort_key))
    assert first.fingerprint() == second.fingerprint()
    assert first.fingerprint() != canonical_sha256(
        {**first.model_dump(mode="json"), "reviewedAt": "unrelated publication time"}
    )


def test_memory_quality_attestation_binds_pair_trees_report_and_checker() -> None:
    pair = MemoryCandidatePairIdentity(
        repoId="repo",
        contractPath="/coordination/tasks/repo/master/enclosures/l1/series-contract.md",
        contractDigest="1" * 64,
        codeRoot="/code",
        memoryRoot="/memory",
        codeSourceBranch="super",
        codeWorkBranch="ar/l1",
        codeBaseCommit="2" * 40,
        memorySourceBranch="super",
        memoryWorkBranch="ar/l1",
        memoryBaseCommit="3" * 40,
        onboardingRoot="/memory/onboarding",
        ledgerPath="/memory/memory.md",
    )
    dependencies = memory_quality_attestation_dependencies(
        pair_identity=pair,
        code_candidate_tree="4" * 40,
        memory_candidate_tree="5" * 40,
        report_sha256="6" * 64,
    )
    attestation = CuratorQualityAttestation.model_validate(
        {
            "schema": "ar-curator-memory-quality/v1",
            "checklistStatus": "ready-for-closeout",
            "curatorActionableCount": 0,
            "memoryRepairCount": 0,
            "missingOnboardingCount": 0,
            "staleRouteIndexCount": 0,
            "sourceChangeCandidateCount": 0,
            "sourceChangeCandidates": [],
            "pairIdentity": pair.model_dump(mode="json"),
            "onboardingRoot": pair.onboardingRoot,
            "reportPath": "/reports/curator-memory-quality.md",
            "reportSha256": "6" * 64,
            "dependencies": dependencies.model_dump(mode="json"),
        }
    )
    assert (
        require_memory_quality_attestation_dependencies(
            attestation,
            code_candidate_tree="4" * 40,
            memory_candidate_tree="5" * 40,
        )
        == dependencies
    )

    with pytest.raises(EvidenceDependencyError) as changed_tree:
        require_memory_quality_attestation_dependencies(
            attestation,
            code_candidate_tree="7" * 40,
            memory_candidate_tree="5" * 40,
        )
    assert changed_tree.value.status == "memory-quality-attestation-dependencies-stale"


def test_missing_extra_wrong_type_and_duplicate_dependencies_fail_closed() -> None:
    with pytest.raises(EvidenceDependencyError) as absent:
        require_evidence_dependencies(None, record_type="route-review/v1")
    assert absent.value.status == "evidence-dependencies-missing"

    with pytest.raises(EvidenceDependencyError) as missing:
        build_evidence_dependencies(
            "route-review/v1",
            [dependency("code-tree", "candidate", "a" * 40, algorithm="git-object")],
        )
    assert missing.value.status == "evidence-dependencies-required-missing"

    with pytest.raises(EvidenceDependencyError) as extra:
        build_evidence_dependencies(
            "route-review/v1",
            [*_route_review().edges, dependency("scheduling", "grade", "e" * 64)],
        )
    assert extra.value.status == "evidence-dependencies-undeclared-extra"

    with pytest.raises(EvidenceDependencyError) as wrong_type:
        require_evidence_dependencies(_route_review(), record_type="quality-report/v2")
    assert wrong_type.value.status == "evidence-dependencies-record-type-mismatch"

    with pytest.raises(ValidationError, match="identities must be unique"):
        EvidenceDependencies(
            recordType="route-review/v1",
            edges=(*_route_review().edges, _route_review().edges[0]),
        )


def test_sha256_and_canonical_order_are_structural_contracts() -> None:
    with pytest.raises(ValidationError, match="canonical nonblank text"):
        dependency("validator", " validator", "a" * 64)

    with pytest.raises(ValidationError, match="64-hex"):
        dependency("validator", "validator", "a" * 40)

    with pytest.raises(ValidationError, match="canonical order"):
        EvidenceDependencies(
            recordType="route-review/v1", edges=tuple(reversed(_route_review().edges))
        )


def test_supplied_record_closure_refuses_cycles_without_scanning_for_external_roots() -> None:
    review_digest = "a" * 64
    door_digest = "b" * 64
    review = EvidenceDependencies(
        recordType="route-review/v1",
        edges=tuple(
            sorted(
                (
                    dependency("code-tree", "candidate", "a" * 40, algorithm="git-object"),
                    dependency("evidence-bytes", "review", "c" * 64),
                    dependency("task-intent", "leaf", "d" * 64),
                    dependency("validator", EVIDENCE_DEPENDENCY_VALIDATOR, "e" * 64),
                    dependency("review-record", "cycle-target", door_digest),
                ),
                key=lambda edge: edge.sort_key,
            )
        ),
    )
    # The declaration is structurally parseable, but its owner policy rejects the
    # undeclared edge before publication.
    with pytest.raises(EvidenceDependencyError) as unsupported:
        require_evidence_dependencies(review, record_type="route-review/v1")
    assert unsupported.value.status == "evidence-dependencies-undeclared-extra"

    door = _door(review_digest=review_digest, predecessor=door_digest)
    with pytest.raises(EvidenceDependencyError) as cycle:
        validate_evidence_dependency_graph({door_digest: door})
    assert cycle.value.status == "evidence-dependency-cycle"

    external_root = _door(review_digest="f" * 64)
    validate_evidence_dependency_graph({door_digest: external_root})

    shared_digest = "c" * 64
    validate_evidence_dependency_graph(
        {
            review_digest: _door(review_digest=shared_digest),
            door_digest: _door(review_digest=shared_digest),
            shared_digest: _door(review_digest="f" * 64),
        }
    )


def test_door_and_operation_dependencies_refuse_missing_or_stale_inputs(tmp_path: Path) -> None:
    contract = _contract(tmp_path / "lifecycle", selected_profile=True)
    _operation_input, store, _finalized = _publish_mutated_code_generation(contract)
    record = store.read()
    assert record is not None and record.doorPublication is not None
    door = record.doorPublication.generation

    missing_intent = door.model_copy(update={"taskIntent": MissingTaskIntent()})
    with pytest.raises(ValueError, match="digest-bearing task intent"):
        require_closeout_door_dependencies(missing_intent)

    stale_door = door.model_copy(update={"candidateTree": "f" * 40})
    with pytest.raises(EvidenceDependencyError) as stale_door_error:
        require_closeout_door_dependencies(stale_door)
    assert stale_door_error.value.status == "closeout-door-dependencies-stale"

    legacy = LifecycleOperationRecord.model_validate(_closeout_record_payload())
    with pytest.raises(EvidenceDependencyError) as missing_candidate:
        lifecycle_operation_dependencies(legacy)
    assert missing_candidate.value.status == "lifecycle-operation-candidate-dependencies-missing"

    missing_door = legacy.model_copy(update={"taskIntent": TaskIntentIdentity(digest="9" * 64)})
    with pytest.raises(EvidenceDependencyError) as missing_door_error:
        lifecycle_operation_dependencies(missing_door)
    assert missing_door_error.value.status == "lifecycle-operation-door-dependency-missing"

    stale_record = record.model_copy(update={"candidateState": "f" * 64})
    with pytest.raises(EvidenceDependencyError) as stale_operation:
        require_lifecycle_operation_dependencies(stale_record)
    assert stale_operation.value.status == "lifecycle-operation-dependencies-stale"


def _build_route_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, ResolvedTaskDocument, WorktreeContract, RouteReviewRecord]:
    task_root = tmp_path / "tasks" / "repo" / "master"
    evidence = task_root / "notes" / "review.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("review evidence\n", encoding="utf-8")
    candidate = ResolvedTaskDocument(
        ref=LEAF,
        path=task_root / "leaf.json",
        document=_leaf_document(),
    )
    contract = cast(WorktreeContract, SimpleNamespace(kind="leaf", task_root=task_root))
    monkeypatch.setattr(route_review_module, "code_candidate_tree", lambda _contract: "a" * 40)
    review = route_review_module.build_route_review(
        contract,
        candidate,
        _route_review_author_payload(),
    )
    return task_root, candidate, contract, review


def test_route_review_dependency_and_content_addressing_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_root, candidate, contract, review = _build_route_review(tmp_path, monkeypatch)

    with monkeypatch.context() as context:
        context.setattr(
            route_review_module,
            "build_evidence_dependencies",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                EvidenceDependencyError("route-review-dependency-refused", "refused")
            ),
        )
        with pytest.raises(route_review_module.RouteReviewError) as build_error:
            route_review_module.build_route_review(
                contract,
                candidate,
                _route_review_author_payload(),
            )
    assert build_error.value.status == "route-review-dependency-refused"

    with pytest.raises(route_review_module.RouteReviewError) as missing_intent:
        route_review_module._require_current_dependencies(
            review.model_copy(update={"taskIntent": MissingTaskIntent()})
        )
    assert missing_intent.value.status == "route-review-task-intent-missing"

    with pytest.raises(route_review_module.RouteReviewError) as missing_dependencies:
        route_review_module._require_current_dependencies(
            review.model_copy(update={"dependencies": None})
        )
    assert missing_dependencies.value.status == "evidence-dependencies-missing"

    with pytest.raises(route_review_module.RouteReviewError) as stale_dependencies:
        route_review_module._require_current_dependencies(
            review.model_copy(update={"candidateTree": "f" * 40})
        )
    assert stale_dependencies.value.status == "route-review-dependencies-stale"

    payload = review.model_dump(mode="json", by_alias=True)
    with pytest.raises(ValidationError, match="content addressing requires every"):
        RouteReviewRecord.model_validate({**payload, "dependencies": None})
    with pytest.raises(ValidationError, match="record digest does not match"):
        RouteReviewRecord.model_validate({**payload, "recordDigest": "f" * 64})

    with pytest.raises(route_review_module.RouteReviewError, match="must be supplied"):
        route_review_module._stamp_evidence_digests(
            task_root,
            {"verdictRef": None, "routes": []},
        )
    with pytest.raises(route_review_module.RouteReviewError, match="one evidenceRef"):
        route_review_module._stamp_evidence_digests(
            task_root,
            {"verdictRef": "notes/review.md", "routes": [{}]},
        )


def test_curator_dependency_currentness_and_attestation_refusal(tmp_path: Path) -> None:
    fixture = QueueFixture(tmp_path / "curator")
    contract = fixture.contracts[MASTER_A]
    validated = coherence.load_curator_coherence_authority(contract)
    record = validated.record

    with pytest.raises(CuratorCoherenceError) as missing_intent:
        coherence._require_current_dependencies(
            record.model_copy(update={"taskIntent": MissingTaskIntent()})
        )
    assert missing_intent.value.status == "curator-coherence-task-intent-missing"

    with pytest.raises(CuratorCoherenceError) as missing_dependencies:
        coherence._require_current_dependencies(record.model_copy(update={"dependencies": None}))
    assert missing_dependencies.value.status == "evidence-dependencies-missing"

    with pytest.raises(CuratorCoherenceError) as stale_dependencies:
        coherence._require_current_dependencies(
            record.model_copy(update={"codeCandidateTree": "f" * 40})
        )
    assert stale_dependencies.value.status == "curator-coherence-dependencies-stale"

    report = contract.worktree_group / "reports" / coherence.QUALITY_REPORT_NAME
    attestation_path = contract.worktree_group / "reports" / coherence.QUALITY_ATTESTATION_NAME
    original = attestation_path.read_bytes()
    attestation = CuratorQualityAttestation.model_validate_json(original)
    source = coherence._QualityAttestationSource(
        attestation_path=attestation_path,
        report_path=report,
        pair_identity=attestation.pairIdentity,
        code_candidate_tree=coherence.capture_future_code_candidate(contract).codeCandidateTree,
        memory_candidate_tree=coherence._memory_candidate_tree(contract),
    )
    invalid = json.loads(original)
    invalid["dependencies"] = None
    attestation_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(CuratorCoherenceError) as invalid_attestation:
        coherence._quality_attestation(contract, source)
    assert invalid_attestation.value.status == "evidence-dependencies-missing"


def test_door_transition_projects_dependency_refusal(tmp_path: Path) -> None:
    fixture = QueueFixture(tmp_path / "door")
    fixture.declare(MASTER_A)
    contract = load_contract(fixture.contracts[MASTER_A].contract_path)
    assert contract.closeout_door is not None
    broken = contract.closeout_door.model_copy(update={"dependencies": None})
    request = CloseoutDoorRequest(
        action="defer",
        contract_path=contract.contract_path.as_posix(),
        expected_generation_id=broken.generationId,
    )
    with pytest.raises(door_source_module.CloseoutQueueError) as refused:
        door_source_module._transitioned_generation(broken, request)
    assert refused.value.status == "evidence-dependencies-missing"


def test_memory_quality_candidate_guards_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SimpleNamespace(storage=object(), code_repository_name="repo")
    scope = SimpleNamespace(
        repo_id="repo",
        code_root=tmp_path / "code",
        onboarding_root=tmp_path / "memory" / "onboarding",
        curator_report_path=tmp_path / "reports" / "curator.md",
        pair_identity=SimpleNamespace(contractPath="/contract"),
        context=context,
    )
    monkeypatch.setattr(
        memory_quality_controller,
        "split_commit_owned_findings",
        lambda *_args: ([], []),
    )
    monkeypatch.setattr(
        memory_quality_controller,
        "check_missing_onboarding",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        memory_quality_controller,
        "build_route_indexes",
        lambda **_kwargs: SimpleNamespace(stale_indexes=[]),
    )
    monkeypatch.setattr(
        memory_quality_controller,
        "revalidate_memory_candidate_scope",
        lambda _config, _scope: scope,
    )
    with pytest.raises(RuntimeError, match="no exact candidate tree inputs"):
        memory_quality_controller._attach_curator_checklist(
            cast(Any, object()),
            cast(Any, scope),
            {"checks": {}, "findings": []},
            {},
        )

    missing_pair = SimpleNamespace(pair_identity=None, onboarding_root=scope.onboarding_root)
    with pytest.raises(RuntimeError, match="no exact code/memory pair identity"):
        memory_quality_controller._curator_candidate_inputs(cast(Any, missing_pair))

    expected = memory_quality_controller._CuratorCandidateInputs("a" * 40, "b" * 40)
    observed = memory_quality_controller._CuratorCandidateInputs("c" * 40, "b" * 40)
    with pytest.raises(MemoryCandidatePairError) as changed:
        memory_quality_controller._require_same_curator_candidate(
            cast(Any, missing_pair),
            expected=expected,
            observed=observed,
        )
    assert changed.value.status == "memory-quality-candidate-changed"
    assert changed.value.contract_path == ""
