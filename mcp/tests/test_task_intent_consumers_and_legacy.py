"""Consumer threading and bounded legacy task-intent cutover matrices."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from agents_remember.application.lifecycle.direct_landing import _direct_error_payload
from agents_remember.application.lifecycle.lifecycle_operation_location import (
    LifecycleOperationPublicAddress,
)
from agents_remember.application.worktree_tools import _start_operation_refusal
from agents_remember.errors import TaskIntentError
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.models.closeout.input import (
    EffectiveCloseoutInput,
    NotApplicableCloseoutLeg,
)
from agents_remember.models.lifecycles.curator_coherence import CuratorCoherenceRecord
from agents_remember.models.lifecycles.door import (
    CloseoutDoorGeneration,
    DoorAdmissionProvenance,
    DoorProvenance,
    DoorSchedulingProvenance,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    IntegrateOperationInput,
    IntegrationOperationAuthority,
    LifecycleOperationRecord,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import (
    MissingTaskIntent,
    TaskIntentIdentity,
)
from agents_remember.tasks import RouteReviewRecord, TaskDocument, write_task_doc
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.task_intent import (
    require_current_task_intent,
    task_intent_identity,
)
from agents_remember.worktrees import route_review as route_review_module
from agents_remember.worktrees.integration.closeout import door_evidence as door_evidence_module
from agents_remember.worktrees.integration.closeout.curator_coherence_render import (
    render_curator_coherence,
)
from agents_remember.worktrees.integration.closeout.task_intent_legacy_census import (
    TaskIntentLegacyCensusError,
    require_task_intent_decoder_removal,
    task_intent_legacy_census,
)
from agents_remember.worktrees.integration.direct_landing.direct_landing_errors import (
    DirectLandingError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_projection import (
    legal_operation_controls,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleOperationLocation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    operation_projection,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.lifecycle.worker.state import release_worker_after_exit
from agents_remember.worktrees.integration.terminal_enclosure_archive import _canonical_entries
from agents_remember.worktrees.queue.closeout_projection_members import (
    ProjectionMemberContext,
    _projection_blockers,
)
from agents_remember.worktrees.queue.closeout_queue_errors import CloseoutQueueError
from agents_remember.worktrees.worktree_contract import (
    ContractError,
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    WorktreeContract,
    contract_publication_text,
    default_contract,
)

INTENT = TaskIntentIdentity(digest="9" * 64)
OTHER_INTENT = TaskIntentIdentity(digest="8" * 64)
LEAF = TaskDocumentRef(repository="repo", path="master/leaf.json")
MASTER = TaskDocumentRef(repository="repo", path="master/task.json")
SPRINT = TaskDocumentRef(repository="repo", path="sprint/task.json")


def _leaf_document() -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": "L1",
            "slug": "leaf",
            "title": "Leaf",
            "kind": "subTask",
            "status": "Completed",
            "repo": "repo",
            "createdAt": "2026-09-01T00:00:00+00:00",
            "objective": "Bind exact intent.",
            "requirements": ["Intent is required."],
        }
    )


def _master_document(*leaf_files: str) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": "MASTER",
            "slug": "task",
            "title": "Master",
            "kind": "master",
            "repo": "repo",
            "createdAt": "2026-09-01T00:00:00+00:00",
            "subTasks": [
                {
                    "number": f"L{index}",
                    "name": Path(file).stem,
                    "file": file,
                }
                for index, file in enumerate(leaf_files, start=1)
            ],
        }
    )


def _route_review_author_payload() -> dict[str, object]:
    return {
        "verdict": "pass",
        "verdictRef": "notes/review.md",
        "routes": [
            {
                "route": "task-intent",
                "verdict": "pass",
                "evidenceRef": "notes/review.md",
            }
        ],
    }


def _route_review_payload() -> dict[str, object]:
    return {
        "candidateTree": "a" * 40,
        "reviewedAt": "2026-09-01T00:00:00+00:00",
        **_route_review_author_payload(),
    }


def _pair() -> MemoryCandidatePairIdentity:
    return MemoryCandidatePairIdentity(
        repoId="repo",
        contractPath="/coordination/tasks/repo/master/enclosures/l1/series-contract.md",
        contractDigest="1" * 64,
        codeRoot="/group/code",
        memoryRoot="/group/memory",
        codeSourceBranch="main",
        codeWorkBranch="leaf",
        codeBaseCommit="2" * 40,
        memorySourceBranch="main",
        memoryWorkBranch="leaf",
        memoryBaseCommit="3" * 40,
        onboardingRoot="/group/memory/onboarding",
        ledgerPath="/group/memory/memory.md",
    )


def _coherence_payload() -> dict[str, object]:
    return {
        "leafId": "L1",
        "contractPath": "/coordination/tasks/repo/master/enclosures/l1/series-contract.md",
        "taskDocumentRef": LEAF.model_dump(mode="json"),
        "semanticRequirementRevision": "R1@v1",
        "deliveryAttempt": "A001",
        "pairIdentity": _pair().model_dump(mode="json"),
        "codeCandidateTree": "a" * 40,
        "memoryCandidateTree": "b" * 40,
        "taskTopologyFingerprint": "c" * 64,
        "attestationPath": "/group/reports/attestation.json",
        "attestationSha256": "d" * 64,
        "attestationReportSha256": "e" * 64,
        "sourceCandidates": [],
        "judgments": [],
        "publicationFingerprint": "f" * 64,
        "publishedBy": f"curator@{LEAF.key}",
        "reportSha256": "1" * 64,
    }


def _door_payload() -> dict[str, object]:
    not_applicable = DoorProvenance(state="not-applicable", fingerprint="2" * 64)
    return {
        "generationId": "1" * 64,
        "disposition": "waiting",
        "taskId": "L1",
        "taskName": "master",
        "taskDocumentRef": LEAF.model_dump(mode="json"),
        "owningMasterTaskDocumentRef": MASTER.model_dump(mode="json"),
        "sprintTaskDocumentRef": SPRINT.model_dump(mode="json"),
        "contractPath": "/coordination/tasks/repo/master/enclosures/l1/series-contract.md",
        "candidateTree": "a" * 40,
        "codeBaseCommit": "b" * 40,
        "taskTopologyFingerprint": "c" * 64,
        "reviewProvenance": not_applicable.model_dump(mode="json"),
        "memoryProvenance": not_applicable.model_dump(mode="json"),
        "ledgerProvenance": not_applicable.model_dump(mode="json"),
        "admissionProvenance": DoorAdmissionProvenance(fingerprint="d" * 64).model_dump(
            mode="json"
        ),
        "schedulingProvenance": DoorSchedulingProvenance(
            priority="normal", judgmentId="J1", fingerprint="e" * 64
        ).model_dump(mode="json"),
        "declaredBy": f"manager@{MASTER.key}",
        "declaredAt": "2026-09-01T00:00:00+00:00",
    }


def _closeout_record_payload(
    *,
    status: str = "failed",
    contract_path: str = "/coordination/tasks/repo/master/enclosures/l1/series-contract.md",
) -> dict[str, object]:
    not_applicable = NotApplicableCloseoutLeg(reason="not required")
    effective = EffectiveCloseoutInput(
        route="worktree",
        contractKind="leaf",
        memoryMode="disabled",
        code=not_applicable,
        memory=not_applicable,
        ledger=not_applicable,
    )
    operation_input = CloseoutOperationInput(
        configPath="/coordination/config.json",
        contractPath=contract_path,
        effectiveInput=effective,
        approvalNote="Approved test input",
        gatePolicy=[],
    )
    terminal = status in {"completed", "failed", "cancelled"}
    return {
        "taskId": "L1",
        "taskName": "master",
        "contractPath": operation_input.contractPath,
        "operationKind": "closeout",
        "candidateState": "1" * 64,
        "candidateTree": "a" * 40,
        "fingerprint": "2" * 64,
        "operationKey": "3" * 64,
        "input": operation_input.model_dump(mode="json"),
        "status": status,
        "phase": status if terminal else "queued",
        "queuedAt": "2026-09-01T00:00:00+00:00",
        "finishedAt": "2026-09-01T00:01:00+00:00" if terminal else None,
        "reportPath": "/group/reports/closeout.md",
        "failure": "legacy failure" if status == "failed" else None,
        "guidance": "legacy guidance",
        "mutationEvidence": {},
    }


def _integrate_record_payload(*, status: str = "failed") -> dict[str, object]:
    operation_input = IntegrateOperationInput(
        configPath="/coordination/config.json",
        contractPath="/coordination/tasks/repo/master/series-contract.md",
    )
    authority = IntegrationOperationAuthority(
        targetKind="atomic-integration",
        codeRepository="/repo",
        codeSourceBranch="main",
        codeSourceRef="refs/heads/main",
        codeSourceCommit="6" * 40,
        codeCandidateCommit="7" * 40,
    )
    terminal = status in {"completed", "failed", "cancelled"}
    return {
        "taskId": "MASTER",
        "taskName": "master",
        "contractPath": operation_input.contractPath,
        "operationKind": "integrate",
        "candidateState": "1" * 64,
        "candidateTree": "a" * 40,
        "fingerprint": "2" * 64,
        "operationKey": "3" * 64,
        "integrationAuthority": authority.model_dump(mode="json"),
        "input": operation_input.model_dump(mode="json"),
        "status": status,
        "phase": status if terminal else "queued",
        "queuedAt": "2026-09-01T00:00:00+00:00",
        "finishedAt": "2026-09-01T00:01:00+00:00" if terminal else None,
        "reportPath": "/group/reports/integrate.md",
        "failure": "integration failure" if status == "failed" else None,
        "guidance": "integration guidance",
    }


@pytest.mark.parametrize(
    "model,payload",
    [
        (RouteReviewRecord, _route_review_payload()),
        (CuratorCoherenceRecord, _coherence_payload()),
        (CloseoutDoorGeneration, _door_payload()),
        (LifecycleOperationRecord, _closeout_record_payload()),
    ],
)
def test_each_legacy_record_class_decodes_typed_absence(model, payload: dict[str, object]) -> None:
    decoded = model.model_validate(payload)
    assert isinstance(decoded.taskIntent, MissingTaskIntent)
    assert decoded.taskIntent.model_dump() == {"state": "missing-intent"}
    assert "digest" not in decoded.taskIntent.model_dump()


@pytest.mark.parametrize(
    "owner,next_action",
    [
        ("route-review", "record_route_review"),
        ("curator-coherence", "publish"),
        ("closeout-door", "closeout_door.update-provenance"),
        ("lifecycle-operation", "retire-and-republish"),
    ],
)
def test_each_currentness_owner_rejects_missing_and_stale(owner: str, next_action: str) -> None:
    with pytest.raises(TaskIntentError) as missing:
        require_current_task_intent(
            MissingTaskIntent(), INTENT, owner=owner, next_action=next_action
        )
    assert missing.value.status == f"{owner}-task-intent-unavailable"
    assert missing.value.next_action == next_action
    with pytest.raises(TaskIntentError) as stale:
        require_current_task_intent(OTHER_INTENT, INTENT, owner=owner, next_action=next_action)
    assert stale.value.status == f"{owner}-task-intent-stale"


@pytest.mark.parametrize(
    ("observed", "status"),
    [
        (None, "route-review-task-intent-unavailable"),
        (OTHER_INTENT, "route-review-task-intent-stale"),
    ],
)
def test_door_review_provenance_reuses_route_review_intent_currentness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed: TaskIntentIdentity | None,
    status: str,
) -> None:
    task_root = tmp_path / "tasks" / "repo" / "master"
    evidence = task_root / "notes" / "review.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("review evidence\n", encoding="utf-8")
    document = _leaf_document()
    unresolved = ResolvedTaskDocument(
        ref=LEAF,
        path=task_root / "leaf.json",
        document=document,
    )
    current = task_intent_identity(task_root, unresolved)
    monkeypatch.setattr(route_review_module, "code_candidate_tree", lambda _contract: "a" * 40)
    review = route_review_module.build_route_review(
        cast(WorktreeContract, SimpleNamespace(kind="leaf", task_root=task_root)),
        unresolved,
        _route_review_author_payload(),
    )
    review = review.model_copy(
        update={"taskIntent": observed if observed is not None else MissingTaskIntent()}
    )
    candidate = ResolvedTaskDocument(
        ref=LEAF,
        path=unresolved.path,
        document=document.model_copy(update={"routeReview": review}),
    )
    contract = cast(WorktreeContract, SimpleNamespace(task_root=task_root))
    monkeypatch.setattr(door_evidence_module, "code_change_present", lambda _contract: True)
    monkeypatch.setattr(
        door_evidence_module,
        "require_current_route_review",
        lambda _contract: {"required": True, "status": "current"},
    )

    with pytest.raises(CloseoutQueueError) as caught:
        door_evidence_module._review_provenance(contract, candidate, "a" * 40)
    assert getattr(caught.value, "status", None) == status

    matching = candidate.document.model_copy(
        update={
            "routeReview": review.model_copy(update={"taskIntent": current}),
        }
    )
    proven = door_evidence_module._review_provenance(
        contract,
        ResolvedTaskDocument(ref=LEAF, path=candidate.path, document=matching),
        "a" * 40,
    )
    assert proven.state == "proven"


def test_route_review_and_contract_writers_cannot_emit_sentinel(tmp_path: Path) -> None:
    review = RouteReviewRecord.model_validate(_route_review_payload())
    task_root = tmp_path / "tasks" / "repo" / "master"
    with pytest.raises(TaskIntentError) as route:
        write_task_doc(task_root, _leaf_document().model_copy(update={"routeReview": review}))
    assert route.value.status == "route-review-task-intent-unavailable"

    door = CloseoutDoorGeneration.model_validate(_door_payload())
    contract = default_contract(
        ContractTask(
            name="master",
            repo_name="repo",
            coordination_root=tmp_path,
            workflow_kind="chat-task",
            memory_mode="disabled",
        ),
        leaf=LeafIdentity(worktree_name="leaf", leaf_id="L1", lifecycle_id="LC1"),
        code=RepoBranchPlan(
            repo_path=tmp_path / "repo",
            source_branch="main",
            work_branch="leaf",
            base_commit="a" * 40,
        ),
    )
    with pytest.raises(ContractError, match="closeout-door-task-intent-unavailable"):
        contract_publication_text(
            contract.contract_path,
            replace(contract, closeout_door=door),
        )


def test_curator_projection_reads_legacy_without_fabricating_identity() -> None:
    legacy = CuratorCoherenceRecord.model_validate(_coherence_payload())
    rendered = render_curator_coherence(legacy)
    assert "Task intent:" not in rendered
    current = legacy.model_copy(update={"taskIntent": INTENT})
    assert f"Task intent: `{INTENT.schema_}` @ `{INTENT.digest}`" in render_curator_coherence(
        current
    )


def test_door_projection_reports_unavailable_then_stale_exactly() -> None:
    legacy = CloseoutDoorGeneration.model_validate(_door_payload())
    document = _leaf_document()
    candidate = ResolvedTaskDocument(
        ref=LEAF, path=Path("/tasks/repo/master/leaf.json"), document=document
    )
    master = ResolvedTaskDocument(
        ref=MASTER,
        path=Path("/tasks/repo/master/task.json"),
        document=document,
    )
    sprint = ResolvedTaskDocument(
        ref=SPRINT,
        path=Path("/tasks/repo/sprint/task.json"),
        document=document,
    )
    context = ProjectionMemberContext(
        source_address=Path(legacy.contractPath),
        door=legacy,
        candidate=candidate,
        master=master,
        order=1,
        sprint=sprint,
        graph=None,
        task_topology_fingerprint=legacy.taskTopologyFingerprint,
        task_intent=INTENT,
    )
    assert _projection_blockers(context) == ["door-task-intent-unavailable"]
    stale = legacy.model_copy(update={"taskIntent": OTHER_INTENT})
    assert _projection_blockers(replace(context, door=stale)) == ["door-task-intent-stale"]


def test_operation_projection_has_no_controls_for_legacy_missing_intent() -> None:
    record = LifecycleOperationRecord.model_validate(_closeout_record_payload())
    projected = operation_projection(record)
    assert projected.result == {
        "state": "lifecycle-operation-task-intent-unavailable",
        "summary": "The legacy operation predates canonical task intent.",
        "nextAction": "retire-and-republish",
    }
    assert projected.legalControls == []
    assert projected.cancellable is False


@pytest.mark.parametrize(
    "status",
    ["queued", "running", "input-required", "termination-required"],
)
def test_legacy_nonterminal_projection_refuses_same_generation_reuse(
    tmp_path: Path,
    status: str,
) -> None:
    contract = default_contract(
        ContractTask(
            name="master",
            repo_name="repo",
            coordination_root=tmp_path,
            workflow_kind="chat-task",
            memory_mode="disabled",
        ),
        leaf=LeafIdentity(worktree_name="leaf", leaf_id="L1", lifecycle_id="LC1"),
        code=RepoBranchPlan(
            repo_path=tmp_path / "repo",
            source_branch="main",
            work_branch="leaf",
            base_commit="a" * 40,
        ),
    )
    door = _door_payload()
    door["contractPath"] = contract.contract_path.as_posix()
    payload = _closeout_record_payload(
        status=status,
        contract_path=contract.contract_path.as_posix(),
    )
    payload.update(
        {
            "phase": {
                "queued": "queued",
                "running": "quality",
                "input-required": "failed",
                "termination-required": "termination-required",
            }[status],
            "doorPublication": {
                "state": "proven",
                "generation": door,
                "expectedBeforeContractSha256": "4" * 64,
                "expectedPublishedContractSha256": "5" * 64,
                "observedPublishedContractSha256": "5" * 64,
            },
        }
    )
    if status == "termination-required":
        payload.update(
            {
                "workerPid": 4242,
                "workerLease": "6" * 64,
                "workerProcessFingerprint": "7" * 64,
                "workerTermination": {
                    "state": "termination-required",
                    "pid": 4242,
                    "lease": "6" * 64,
                    "processFingerprint": "7" * 64,
                    "requestedAt": "2026-09-01T00:00:30+00:00",
                },
                "terminationReturnStatus": "running",
                "terminationReturnPhase": "quality",
            }
        )
    record = LifecycleOperationRecord.model_validate(payload)

    projected = operation_projection(record, contract=contract)
    handler_actions = [control["action"] for control in legal_operation_controls(contract, record)]

    assert projected.result is not None
    if status == "termination-required":
        assert projected.result["state"] == "worker-termination-required"
        assert [control["action"] for control in projected.legalControls] == ["cancel"]
        assert projected.cancellable is True
    else:
        assert projected.result["state"] == "lifecycle-operation-task-intent-unavailable"
        assert projected.legalControls == []
        assert projected.cancellable is False
    assert set(handler_actions).isdisjoint({"recover", "retry"})


def test_legacy_projection_preserves_worker_termination_until_exact_exit(tmp_path: Path) -> None:
    contract = default_contract(
        ContractTask(
            name="master",
            repo_name="repo",
            coordination_root=tmp_path,
            workflow_kind="chat-task",
            memory_mode="disabled",
        ),
        leaf=LeafIdentity(worktree_name="leaf", leaf_id="L1", lifecycle_id="LC1"),
        code=RepoBranchPlan(
            repo_path=tmp_path / "repo",
            source_branch="main",
            work_branch="leaf",
            base_commit="a" * 40,
        ),
    )
    door = _door_payload()
    door["contractPath"] = contract.contract_path.as_posix()
    payload = _closeout_record_payload(
        status="termination-required",
        contract_path=contract.contract_path.as_posix(),
    )
    payload.update(
        {
            "phase": "termination-required",
            "doorPublication": {
                "state": "proven",
                "generation": door,
                "expectedBeforeContractSha256": "4" * 64,
                "expectedPublishedContractSha256": "5" * 64,
                "observedPublishedContractSha256": "5" * 64,
            },
            "workerPid": 4242,
            "workerLease": "6" * 64,
            "workerProcessFingerprint": "7" * 64,
            "workerTermination": {
                "state": "termination-required",
                "pid": 4242,
                "lease": "6" * 64,
                "processFingerprint": "7" * 64,
                "requestedAt": "2026-09-01T00:00:30+00:00",
            },
            "terminationReturnStatus": "running",
            "terminationReturnPhase": "quality",
        }
    )
    record = LifecycleOperationRecord.model_validate(payload)

    projected = operation_projection(record, contract=contract)

    assert projected.result is not None
    assert projected.result["state"] == "worker-termination-required"
    assert projected.result["nextAction"] == "cancel"
    assert projected.cancellable is True
    handler_actions = [control["action"] for control in legal_operation_controls(contract, record)]
    assert (
        [control["action"] for control in projected.legalControls] == handler_actions == ["cancel"]
    )
    arguments = projected.legalControls[0]["arguments"]
    assert arguments["contract_path"] == contract.contract_path.as_posix()
    assert arguments["expected_generation"] == 1
    assert arguments["action"] == "cancel"

    exited_termination = record.workerTermination
    assert exited_termination is not None
    pending = release_worker_after_exit(
        record.model_copy(update={"cancelRequested": True}),
        exited_termination.model_copy(
            update={
                "state": "exited",
                "observedAt": "2026-09-01T00:00:45+00:00",
            }
        ),
    )
    assert pending.status == "termination-required"
    assert pending.cancelRequested is True
    assert pending.workerPid is None

    pending_projection = operation_projection(pending, contract=contract)
    pending_handler_actions = [
        control["action"] for control in legal_operation_controls(contract, pending)
    ]
    assert [control["action"] for control in pending_projection.legalControls] == (
        pending_handler_actions
    )
    assert "cancel" in pending_handler_actions
    assert pending_projection.cancellable is True
    assert pending_projection.result is not None
    assert pending_projection.result["state"] == "worker-termination-required"
    assert pending_projection.result["observed"]["state"] == "exited"
    assert pending_projection.result["nextAction"] == "cancel"
    assert pending_projection.result["summary"] == (
        "Exact worker exit is proven; complete the pending same-generation cancellation "
        "with the advertised task-addressed cancel action."
    )
    assert pending_projection.guidance == pending_projection.result["summary"]

    exited = LifecycleOperationRecord.model_validate(
        pending.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": "2026-09-01T00:01:00+00:00",
                "terminationReturnStatus": None,
                "terminationReturnPhase": None,
            }
        ).model_dump(mode="json")
    )
    retired = operation_projection(exited, contract=contract)
    assert retired.result is not None
    assert retired.result["state"] == "lifecycle-operation-task-intent-unavailable"
    assert retired.legalControls == []
    assert retired.cancellable is False


def test_public_closeout_start_preserves_typed_task_intent_refusal() -> None:
    result = _start_operation_refusal(
        cast(McpRuntimeConfig, object()),
        Path("/coordination/tasks/repo/master/enclosures/l1/series-contract.md"),
        LifecycleOperationPublicAddress("worktree_closeout_apply", "closeout"),
        TaskIntentError(
            "closeout-door-task-intent-unavailable",
            "the current door predates canonical task intent",
            next_action="closeout_door.update-provenance",
        ),
    )
    assert result == {
        "ok": False,
        "operation": "worktree_closeout_apply",
        "state": "refused",
        "status": "closeout-door-task-intent-unavailable",
        "detail": "the current door predates canonical task intent",
        "nextAction": "closeout_door.update-provenance",
    }


def test_public_direct_landing_preserves_canonical_task_intent_recovery() -> None:
    result = _direct_error_payload(
        DirectLandingError(
            "closeout-door-task-intent-unavailable",
            "the waiting door predates canonical task intent",
            next_action="closeout_door.update-provenance",
        ),
        recovery={"nextAction": "developer-decision"},
    )
    assert result["status"] == "closeout-door-task-intent-unavailable"
    assert result["nextAction"] == "closeout_door.update-provenance"


def test_operation_writer_rejects_new_sentinel_and_terminal_replacement_retires_raw_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "closeout-operation.json"
    store = LifecycleOperationStore(path)
    legacy_payload = _closeout_record_payload()
    legacy = LifecycleOperationRecord.model_validate(legacy_payload)
    with pytest.raises(RuntimeError, match="lifecycle-operation-task-intent-unavailable"):
        store.create(legacy)

    persisted_legacy_payload = {"schemaVersion": "3.0", **legacy_payload}
    raw = json.dumps(persisted_legacy_payload, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(raw, encoding="utf-8")
    successor_payload = _closeout_record_payload(status="queued")
    successor_payload.update(
        {
            "taskIntent": INTENT.model_dump(mode="json", by_alias=True),
            "fingerprint": "4" * 64,
            "operationKey": "5" * 64,
        }
    )
    successor = LifecycleOperationRecord.model_validate(successor_payload)
    published = store.replace_terminal(successor)
    assert published.taskIntent == INTENT
    assert published.generation == 2
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["schemaVersion"] == "3.0"
    assert stored["taskIntent"] == {"schema": "task-intent/v1", "digest": INTENT.digest}
    assert path.read_text(encoding="utf-8") != raw
    archive = tmp_path / "closeout-operation.legacy-missing-intent-generation-1.json"
    assert archive.read_text(encoding="utf-8") == raw
    assert json.loads(archive.read_text(encoding="utf-8")) == persisted_legacy_payload
    assert "missing-intent" not in archive.read_text(encoding="utf-8")


def test_integration_replacement_uses_normal_history_and_links_both_generations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "integrate-operation.json"
    store = LifecycleOperationStore(path)
    current = LifecycleOperationRecord.model_validate(_integrate_record_payload())
    path.write_text(current.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
    candidate_payload = _integrate_record_payload(status="queued")
    candidate_payload.update({"fingerprint": "4" * 64, "operationKey": "5" * 64})

    published = store.replace_terminal(LifecycleOperationRecord.model_validate(candidate_payload))

    archive = tmp_path / "integrate-operation.generation-1.json"
    predecessor = LifecycleOperationRecord.model_validate_json(archive.read_text(encoding="utf-8"))
    assert not (tmp_path / "integrate-operation.legacy-missing-intent-generation-1.json").exists()
    assert predecessor.successorFingerprint == published.fingerprint
    assert published.predecessorFingerprint == predecessor.fingerprint
    assert published.generation == 2


def test_missing_intent_recovery_archive_is_historical_terminal_evidence(
    tmp_path: Path,
) -> None:
    lifecycle = tmp_path / ".lifecycle"
    lifecycle.mkdir()
    (lifecycle / "enclosure-manifest.json").write_text("{}\n", encoding="utf-8")
    payload = {"schemaVersion": "3.0", **_closeout_record_payload()}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    archive_name = "closeout-operation.legacy-missing-intent-generation-1.json"
    (lifecycle / archive_name).write_text(raw, encoding="utf-8")
    location = cast(
        LifecycleOperationLocation,
        SimpleNamespace(lifecycle_directory=lifecycle),
    )

    entries = _canonical_entries(location, operation="worktree_cleanup")

    archived = next(item for item in entries if item.relativePath == archive_name)
    assert archived.content == raw
    assert archived.sha256 == hashlib.sha256(raw.encode()).hexdigest()

    (lifecycle / "closeout-operation.legacy-missing-intent-generation-0.json").write_text(
        raw,
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="unowned artifact exists"):
        _canonical_entries(location, operation="worktree_cleanup")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def test_deterministic_four_class_census_controls_decoder_removal(tmp_path: Path) -> None:
    root = tmp_path / "coordination"
    task_root = root / "tasks" / "repo" / "master"
    review_path = task_root / "leaf.json"
    _write_json(
        task_root / "task.json",
        _master_document("leaf.md", "future.md").model_dump(mode="json", by_alias=True),
    )
    route = {"schema": "ar-task-document/v1", "routeReview": _route_review_payload()}
    _write_json(review_path, route)

    authority_path = task_root / "notes" / "reports" / "L1-curator-coherence.json"
    record_ref = "notes/reports/curator-coherence/L1/generations/current/record.json"
    _write_json(
        authority_path,
        {
            "schemaVersion": "ar-curator-coherence-authority/v1",
            "recordPath": record_ref,
        },
    )
    coherence_path = task_root / record_ref
    _write_json(coherence_path, _coherence_payload())

    contract_path = task_root / "enclosures" / "l1" / "series-contract.md"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(
        "---\ncoordination:\n  closeout_door: "
        + json.dumps(_door_payload(), sort_keys=True, separators=(",", ":"))
        + "\n---\n",
        encoding="utf-8",
    )
    operation_path = root / "worktrees" / "repo" / "leaf" / ".lifecycle" / "closeout-operation.json"
    operation = {"schemaVersion": "3.0", **_closeout_record_payload()}
    _write_json(operation_path, operation)

    census = task_intent_legacy_census(root)
    assert [item.recordClass for item in census.classes] == [
        "route-review",
        "curator-coherence",
        "closeout-door",
        "lifecycle-operation",
    ]
    assert [item.missingIntent for item in census.classes] == [1, 1, 1, 1]
    assert census.unreadable == ()
    with pytest.raises(TaskIntentLegacyCensusError, match="4 current rows"):
        require_task_intent_decoder_removal(census)

    intent = INTENT.model_dump(mode="json", by_alias=True)
    route["routeReview"]["taskIntent"] = intent  # type: ignore[index]
    _write_json(review_path, route)
    coherence = _coherence_payload()
    coherence["taskIntent"] = intent
    _write_json(coherence_path, coherence)
    door = _door_payload()
    door["taskIntent"] = intent
    contract_path.write_text(
        "---\ncoordination:\n  closeout_door: "
        + json.dumps(door, sort_keys=True, separators=(",", ":"))
        + "\n---\n",
        encoding="utf-8",
    )
    operation["taskIntent"] = intent
    _write_json(operation_path, operation)
    _write_json(
        task_root / "lint-inventory-baseline.json",
        {"schema": "memory-hygiene-lint-inventory/v1", "entries": []},
    )

    zero = task_intent_legacy_census(root)
    assert zero.remaining == 0
    assert zero.unreadable == ()
    require_task_intent_decoder_removal(zero)

    _write_json(task_root / "future.json", {"schema": "ar-task-document/future"})
    unclassified = task_intent_legacy_census(root)
    assert any("unsupported-task-document" in row for row in unclassified.unreadable)
    with pytest.raises(TaskIntentLegacyCensusError, match="unreadable or unclassified"):
        require_task_intent_decoder_removal(unclassified)


@pytest.mark.parametrize("worktrees_root_kind", ["missing", "file"])
def test_decoder_removal_refuses_absent_lifecycle_owner_root(
    tmp_path: Path,
    worktrees_root_kind: str,
) -> None:
    root = tmp_path / "coordination"
    (root / "tasks").mkdir(parents=True)
    worktrees = root / "worktrees"
    if worktrees_root_kind == "file":
        worktrees.write_text("not a lifecycle owner directory\n", encoding="utf-8")

    census = task_intent_legacy_census(root)

    assert census.remaining == 0
    assert census.unreadable == (f"lifecycle-operation:{worktrees.as_posix()}:root-missing",)
    with pytest.raises(TaskIntentLegacyCensusError, match="unreadable or unclassified"):
        require_task_intent_decoder_removal(census)
