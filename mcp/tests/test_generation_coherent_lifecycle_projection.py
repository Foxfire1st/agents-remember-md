"""CCR-R18 revision, state-matrix, guidance, and cleanup forcing tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest import mock

import agents_remember.models.lifecycles.operation_projection as op_projection_module
import pytest
from agents_remember.application.tool_response import bound_next_step
from agents_remember.models.base import NextStep
from agents_remember.models.closeout.input import CloseoutCorrectedCall, EffectiveCloseoutInput
from agents_remember.models.lifecycles.door import (
    CloseoutDoorGeneration,
    DoorAdmissionProvenance,
    DoorDependencyInputs,
    DoorProvenance,
    DoorSchedulingProvenance,
    closeout_door_dependencies,
)
from agents_remember.models.lifecycles.mutation_evidence import CloseoutMutationLeg
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    LifecycleOperationProjection,
    LifecycleOperationRecord,
)
from agents_remember.models.lifecycles.operation_projection import (
    STATE_MATRIX,
    LifecycleProjectionIncoherence,
    _require_coherent_projection_components,
    _require_component_bindings_match_envelope,
    _require_projection_task_addresses,
    _validate_projection_cell,
    validate_state_matrix_is_exhaustive,
)
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.worktree import WorktreeStatusResponse
from agents_remember.tasks import SubTaskRef, TaskDocument, read_task_doc, write_task_doc
from agents_remember.tasks.document_refs import TaskDocumentTopology
from agents_remember.worktrees.closeout_input import (
    capture_closeout_candidate,
    normalize_closeout_input,
    raw_closeout_messages,
)
from agents_remember.worktrees.integration.closeout.operation_admission import (
    CloseoutOperationAdmission,
)
from agents_remember.worktrees.integration.closeout.task_intent_identity import (
    contract_task_intent,
)
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operations as lifecycle_operations_module,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    OperationProjectionContext,
    _require_all_or_none_worker_binding,
    _termination_bound_worker_observation,
    _validate_dependent_identities,
    bind_projection_decision,
    bind_projection_result,
    operation_projection,
    operation_projection_identity,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    _advance_record_revision,
    _validate_identity_and_evidence_transition,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_closeout_operation,
)
from agents_remember.worktrees.integration.lifecycle.worker.state import (
    release_worker_after_exit,
)
from agents_remember.worktrees.integration.terminal_enclosure_archive import (
    _require_archivable_operation,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    default_contract,
    load_contract,
    write_contract,
)

_LEASE = "6" * 64
_WORKER_FINGERPRINT = "7" * 64


# ---------------------------------------------------------------------------
# Standalone fixture support: these helpers are inlined from the shared closeout
# lifecycle fixture suites so this module never imports pre-existing mcp/tests
# support modules. They import only agents_remember.* production sources.
# ---------------------------------------------------------------------------


def start_closeout_operation(
    operation_input: CloseoutOperationInput,
    **options,
):
    """Route a durable-input fixture through canonical raw, lease-bound admission.

    Older lifecycle suites exercise behavior below the L3 scheduling boundary. They receive an
    explicit synthetic waiting door and bypass only the first-ready projection assertion for that
    synthetic generation. Fixtures that already own a real door still exercise the production
    scheduling fence unchanged.
    """
    fixture_bypass_scheduling = bool(options.pop("fixture_bypass_scheduling", False))
    effective = operation_input.effectiveInput
    contract, bypass_scheduling_fence = ensure_fixture_waiting_door(
        load_contract(Path(operation_input.contractPath)),
        force_synthetic=fixture_bypass_scheduling,
    )
    scheduling_fence = (
        mock.patch.object(lifecycle_operations_module, "require_first_ready_generation")
        if bypass_scheduling_fence or fixture_bypass_scheduling
        else nullcontext()
    )
    with scheduling_fence:
        return start_or_observe_closeout_operation(
            CloseoutOperationAdmission(
                config_path=operation_input.configPath,
                contract_path=Path(operation_input.contractPath),
                messages=raw_closeout_messages(
                    code=_enabled_message(effective, "code"),
                    memory=_enabled_message(effective, "memory"),
                    ledger=_enabled_message(effective, "ledger"),
                ),
                approval_note=operation_input.approvalNote,
                gate_policy=operation_input.gatePolicy,
                corrected_call=CloseoutCorrectedCall(
                    tool="worktree_closeout_apply",
                    arguments={
                        "contract_path": operation_input.contractPath,
                        "intent_note": "<developer intent>",
                    },
                ),
            ),
            contract,
            **options,
        )


def ensure_fixture_waiting_door(contract, *, force_synthetic: bool = False):
    """Publish a typed test-only scheduling input for below-queue lifecycle suites."""

    if contract.closeout_door is not None and not (
        force_synthetic and contract.closeout_door.disposition == "waiting"
    ):
        door = contract.closeout_door
        bypass = door.disposition == "waiting" and door.declaredBy.startswith("test-fixture:")
        return contract, bypass
    door = _fixture_waiting_door(contract)
    write_contract(contract.contract_path, replace(contract, closeout_door=door))
    return load_contract(contract.contract_path), True


def _fixture_waiting_door(
    contract,
) -> CloseoutDoorGeneration:
    """Build one typed synthetic source generation for legacy lifecycle fixtures."""

    candidate = capture_closeout_candidate(contract)
    task_ref, master_ref = _publish_fixture_task_context(contract)
    sprint_ref = TaskDocumentRef(
        repository=contract.repo_name,
        path="lifecycle-fixture-sprint/task.json",
    )
    identity = {
        "schema": "test-fixture-closeout-door/v1",
        "contractPath": contract.contract_path.as_posix(),
        "candidateTree": candidate.candidate_tree,
        "codeBaseCommit": contract.code_base_commit,
        "taskDocumentRef": task_ref.model_dump(mode="json"),
    }
    generation_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    not_applicable = DoorProvenance(
        state="not-applicable",
        fingerprint=hashlib.sha256(b"test-fixture-not-applicable").hexdigest(),
    )
    intent = contract_task_intent(contract, candidate_ref=task_ref)
    topology = hashlib.sha256(b"test-fixture-topology").hexdigest()
    admission = DoorAdmissionProvenance(
        fingerprint=hashlib.sha256(b"test-fixture-admission").hexdigest()
    )
    scheduling = DoorSchedulingProvenance(
        priority="normal",
        judgmentId="TEST-FIXTURE-BELOW-SCHEDULING",
        fingerprint=hashlib.sha256(b"test-fixture-scheduling").hexdigest(),
    )
    dependencies = closeout_door_dependencies(
        DoorDependencyInputs(
            candidate_tree=candidate.candidate_tree,
            memory_candidate_tree=contract.memory_base_commit,
            task_topology_fingerprint=topology,
            task_intent=intent,
            review=not_applicable,
            memory=not_applicable,
            ledger=not_applicable,
            admission=admission,
            scheduling=scheduling,
            predecessor="",
        )
    )
    door = CloseoutDoorGeneration(
        generationId=generation_id,
        disposition="waiting",
        taskId=contract.task_id,
        taskName=contract.task_name,
        taskDocumentRef=task_ref,
        owningMasterTaskDocumentRef=master_ref,
        sprintTaskDocumentRef=sprint_ref,
        contractPath=contract.contract_path.as_posix(),
        candidateTree=candidate.candidate_tree,
        memoryCandidateTree=contract.memory_base_commit,
        codeBaseCommit=contract.code_base_commit,
        memoryBaseCommit=contract.memory_base_commit,
        ledgerMemoryCommit=contract.memory_base_commit,
        taskTopologyFingerprint=topology,
        taskIntent=intent,
        reviewProvenance=not_applicable,
        memoryProvenance=not_applicable,
        ledgerProvenance=not_applicable,
        admissionProvenance=admission,
        schedulingProvenance=scheduling,
        dependencies=dependencies,
        declaredBy="test-fixture:lifecycle-below-scheduling",
        declaredAt="2026-08-15T00:00:00+00:00",
    )
    return door


def _publish_fixture_task_context(
    contract,
) -> tuple[TaskDocumentRef, TaskDocumentRef]:
    """Publish or reuse one canonical leaf plus its task-root master reference."""

    master_path = contract.task_root / "task.json"
    master_ref = _confined_fixture_task_ref(contract, master_path)
    if contract.kind == "series":
        if master_path.is_file():
            master = read_task_doc(master_path)
            if master.kind != "master":
                raise AssertionError(
                    "series closeout fixture parent must be a master task document"
                )
            children = TaskDocumentTopology(contract.coordination_root).children(master_ref)
            if children:
                return children[0], master_ref
        else:
            master = TaskDocument.model_validate(
                {
                    "id": contract.task_id,
                    "slug": contract.task_root.name,
                    "title": contract.task_name,
                    "kind": "master",
                    "status": "inProgress",
                    "repo": contract.repo_name,
                    "createdAt": "2026-08-15T00:00:00+00:00",
                    "executionNature": "atomic",
                }
            )
        leaf_id = "TEST-FIXTURE-CLOSEOUT-LEAF"
        leaf_slug = "test-fixture-closeout-leaf"
        leaf = _fixture_leaf_document(contract, leaf_id=leaf_id, leaf_slug=leaf_slug)
        write_task_doc(contract.task_root, leaf)
        if not any(row.number == leaf_id for row in master.subTasks):
            master = master.model_copy(
                update={
                    "subTasks": [
                        *master.subTasks,
                        SubTaskRef(
                            number=leaf_id,
                            name="Test fixture closeout leaf",
                            file=f"{leaf_slug}.md",
                            status="inProgress",
                        ),
                    ]
                }
            )
        write_task_doc(contract.task_root, master)
        return (
            _confined_fixture_task_ref(contract, contract.task_root / f"{leaf_slug}.json"),
            master_ref,
        )

    leaf_slug = contract.leaf_id.lower()
    leaf_path = contract.task_root / f"{leaf_slug}.json"
    if not leaf_path.is_file():
        write_task_doc(
            contract.task_root,
            _fixture_leaf_document(contract, leaf_id=contract.leaf_id, leaf_slug=leaf_slug),
        )
    return _confined_fixture_task_ref(contract, leaf_path), master_ref


def _fixture_leaf_document(contract, *, leaf_id: str, leaf_slug: str) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": leaf_id,
            "slug": leaf_slug,
            "title": leaf_id,
            "kind": "subTask",
            "status": "inProgress",
            "repo": contract.repo_name,
            "createdAt": "2026-08-15T00:00:00+00:00",
            "objective": "Exercise the exact fixture task intent.",
            "requirements": ["The fixture publishes canonical task intent."],
        }
    )


def _confined_fixture_task_ref(contract, path: Path) -> TaskDocumentRef:
    repository_root = (contract.coordination_root / "tasks" / contract.repo_name).resolve(
        strict=False
    )
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(repository_root):
        raise AssertionError("fixture task document must stay under its configured repository root")
    return TaskDocumentRef(
        repository=contract.repo_name,
        path=resolved.relative_to(repository_root).as_posix(),
    )


def _enabled_message(effective: EffectiveCloseoutInput, leg: CloseoutMutationLeg) -> str | None:
    return effective.message_for(leg) if effective.enabled(leg) else None


def closeout_operation_input(
    contract,
    **values,
) -> CloseoutOperationInput:
    config_path = values.pop("config_path", None)
    code = values.pop("code", "close code candidate")
    memory = values.pop("memory", "close external memory")
    ledger = values.pop("ledger", "record code-to-memory mapping")
    approval_note = values.pop("approval_note", "developer approved this exact candidate")
    assert not values, f"unknown closeout operation fixture fields: {sorted(values)}"
    effective = normalize_closeout_input(
        contract,
        raw_closeout_messages(code=code, memory=memory, ledger=ledger),
        route="worktree",
        corrected_call=CloseoutCorrectedCall(
            tool="worktree_closeout_apply",
            arguments={"contract_path": contract.contract_path.as_posix()},
        ),
    )
    configured = config_path or (contract.code_repo_path.parent / "settings.json")
    return CloseoutOperationInput(
        configPath=Path(configured).as_posix(),
        contractPath=contract.contract_path.as_posix(),
        effectiveInput=effective,
        approvalNote=str(approval_note),
    )


def _contract(tmp_path: Path):
    coordination = tmp_path / "ar-coordination"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "lifecycle-tests@agents-remember.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Lifecycle Tests"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", base_commit],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo,
        check=True,
    )
    (repo / "ar-memory").mkdir()
    contract = default_contract(
        ContractTask(
            name="durable-lifecycle",
            repo_name="repo",
            coordination_root=coordination,
            workflow_kind="light-task",
            memory_mode="internal",
        ),
        leaf=LeafIdentity(worktree_name="durable-lifecycle", leaf_id="L23"),
        code=RepoBranchPlan(
            repo_path=repo,
            source_branch="main",
            work_branch="feature/l23",
            base_commit=base_commit,
        ),
    )
    contract.code_worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            contract.code_work_branch,
            contract.code_worktree,
            contract.code_source_branch,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    write_contract(contract.contract_path, contract)
    publish_new_lifecycle_operation_location(
        contract,
        contract_text=contract.contract_path.read_text(encoding="utf-8"),
    )
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "version": 1,
                "coordinationRoot": coordination.as_posix(),
                "workspaceRoot": tmp_path.as_posix(),
                "repositories": {"repo": {}},
            }
        ),
        encoding="utf-8",
    )
    return contract


def _claimed_contract_and_record(tmp_path: Path):
    """Compose one canonical claimed closeout generation like the L2 suites do."""

    contract = _contract(tmp_path)
    (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    operation_input = closeout_operation_input(contract, code="close exact projection fixture")
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    record = store.read()
    assert record is not None
    assert record.doorPublication is not None
    return contract, store, record


def _with_worker(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
    return record.model_copy(
        update={
            "workerPid": 4242,
            "workerLease": _LEASE,
            "workerProcessFingerprint": _WORKER_FINGERPRINT,
        }
    )


def _with_termination(
    record: LifecycleOperationRecord,
    *,
    state: str,
    cancel_requested: bool,
) -> LifecycleOperationRecord:
    updated = record.model_copy(
        update={
            "workerTermination": WorkerTerminationEvidence(
                state=state,  # type: ignore[arg-type]
                pid=4242,
                lease=_LEASE,
                processFingerprint=_WORKER_FINGERPRINT,
                requestedAt="2026-09-01T00:00:30+00:00",
                observedAt=("2026-09-01T00:01:10+00:00" if state == "exited" else None),
            ),
            "cancelRequested": cancel_requested,
        }
    )
    if record.status == "termination-required":
        updated = updated.model_copy(
            update={
                "terminationReturnStatus": "running",
                "terminationReturnPhase": "quality",
            }
        )
    return updated


def _validate(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
    return LifecycleOperationRecord.model_validate(record.model_dump(mode="json"))


@pytest.mark.parametrize(
    "status,phase,result,worker",
    [
        ("queued", "queued", None, True),
        ("running", "quality", None, True),
        (
            "input-required",
            "failed",
            {"state": "repair-required", "nextAction": "retry"},
            False,
        ),
        ("termination-required", "termination-required", None, True),
        ("completed", "completed", {"state": "completed"}, False),
        ("failed", "failed", {"state": "failed"}, False),
        ("cancelled", "cancelled", None, False),
    ],
)
def test_every_lifecycle_status_projects_one_bound_matrix_cell(
    tmp_path: Path,
    status: str,
    phase: str,
    result: dict[str, object] | None,
    worker: bool,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    base = record.model_copy(
        update={
            "status": status,
            "phase": phase,
            "result": result,
            "finishedAt": (
                "2026-09-01T00:01:00+00:00" if status in {"completed", "failed"} else None
            ),
        }
    )
    if status == "termination-required":
        base = _with_termination(base, state="requested", cancel_requested=True)
    if worker:
        base = _with_worker(base)
    variant = _validate(base)

    projected = operation_projection(variant, contract=contract)

    assert projected.status == variant.status
    assert projected.identity is not None
    assert projected.identity.recordRevision == variant.recordRevision
    assert projected.identity.candidateTupleDigest == variant.fingerprint
    assert projected.componentBindings is not None
    digest = projected.identity.identityDigest
    assert projected.componentBindings.approval == digest
    assert projected.componentBindings.worker == digest
    assert projected.componentBindings.legalControls == [digest] * len(projected.legalControls)


def test_state_matrix_exhausts_status_and_phase_vocabularies() -> None:
    validate_state_matrix_is_exhaustive()
    assert len(STATE_MATRIX) == 7


def test_healthy_live_worker_is_legal_to_cancel_but_recommended_to_observe(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    variant = _validate(
        _with_worker(record).model_copy(update={"status": "running", "phase": "quality"})
    )

    projected = operation_projection(variant, contract=contract)

    assert projected.status == "running"
    assert projected.worker is not None and projected.worker.state == "live"
    assert projected.result is None
    assert [item["action"] for item in projected.legalControls] == ["cancel"]
    assert projected.recommendedAction is not None
    assert projected.recommendedAction.action == "observe"
    assert projected.recommendedAction.mutating is False


@pytest.mark.parametrize("phase", ["quality", "memory-preflight"])
def test_generation_11_and_15_contradictions_refuse_without_controls(
    tmp_path: Path,
    phase: str,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    contradictory = _validate(
        _with_termination(
            _with_worker(record).model_copy(update={"status": "running", "phase": phase}),
            state="termination-required",
            cancel_requested=False,
        )
    )

    projected = operation_projection(contradictory, contract=contract)

    assert projected.status == "incoherent"
    assert projected.result is not None
    assert projected.result["state"] == "lifecycle-projection-incoherent"
    assert projected.legalControls == []
    assert projected.recommendedAction is None
    assert projected.cancellable is False


def test_real_termination_recovery_recommends_its_exact_legal_cancel(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    terminating = _validate(
        _with_termination(
            _with_worker(record).model_copy(
                update={"status": "termination-required", "phase": "termination-required"}
            ),
            state="requested",
            cancel_requested=True,
        )
    )

    projected = operation_projection(terminating, contract=contract)

    assert projected.status == "termination-required"
    assert projected.result is not None
    assert projected.result["state"] == "worker-termination-required"
    assert projected.recommendedAction is not None
    assert projected.recommendedAction.action == "cancel"
    assert projected.recommendedAction.arguments == projected.legalControls[0]["arguments"]


def test_exit_proven_cancellation_keeps_only_same_generation_cancel(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    terminating = _validate(
        _with_termination(
            record.model_copy(
                update={"status": "termination-required", "phase": "termination-required"}
            ),
            state="exited",
            cancel_requested=True,
        )
    )

    projected = operation_projection(terminating, contract=contract)

    assert projected.status == "termination-required"
    assert projected.worker is not None and projected.worker.state == "exited"
    assert [control["action"] for control in projected.legalControls] == ["cancel"]
    assert projected.recommendedAction is not None
    assert projected.recommendedAction.action == "cancel"


def test_adjacent_revision_race_refuses_stale_result_approval_and_worker_facts(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    stale = _validate(_with_worker(record).model_copy(update={"status": "running"}))
    current = stale.model_copy(update={"recordRevision": 2})
    stale_identity = operation_projection_identity(stale)

    for context in (
        OperationProjectionContext(resultIdentity=stale_identity),
        OperationProjectionContext(approvalIdentity=stale_identity),
        OperationProjectionContext(workerIdentity=stale_identity),
    ):
        projected = operation_projection(current, contract=contract, context=context)

        assert projected.status == "incoherent"
        assert projected.result is not None
        assert projected.result["state"] == "lifecycle-projection-incoherent"
        assert projected.legalControls == []
        assert projected.recommendedAction is None


def test_adjacent_generation_candidate_fact_cannot_splice_into_current_projection(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    predecessor = _validate(_with_worker(record).model_copy(update={"status": "running"}))
    successor = predicate_with_revision_2(predecessor)

    projected = operation_projection(
        successor,
        contract=contract,
        context=OperationProjectionContext(
            resultIdentity=operation_projection_identity(predecessor)
        ),
    )

    assert projected.status == "incoherent"
    assert projected.identity is not None
    assert projected.identity.generation == 2
    assert projected.result is not None
    assert projected.result["state"] == "lifecycle-projection-incoherent"
    assert projected.legalControls == []


def predicate_with_revision_2(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
    return _validate(
        record.model_copy(update={"generation": 2, "recordRevision": 2, "fingerprint": "9" * 64})
    )


def test_store_revision_is_monotonic_and_stale_cas_cannot_publish(tmp_path: Path) -> None:
    _contract, store, _record = _claimed_contract_and_record(tmp_path)
    del _contract, _record
    initial = store.read()
    assert initial is not None
    advanced = store.update(
        lambda current: current.model_copy(update={"currentCommand": "revision two"})
    )
    assert advanced.recordRevision == initial.recordRevision + 1

    observed, matched = store.update_if_current(
        initial,
        lambda current: current.model_copy(update={"currentCommand": "stale write"}),
    )
    assert matched is False
    assert observed == advanced
    assert store.read() == advanced


def test_cross_task_next_step_is_omitted_while_exact_and_external_guidance_remain() -> None:
    l11 = "/coordination/tasks/repo/master/enclosures/l11/series-contract.md"
    l01 = "/coordination/tasks/repo/master/enclosures/l01/series-contract.md"
    response = WorktreeStatusResponse(
        ok=True,
        contractPath=l11,
        enclosurePath=l11,
    )
    contaminated = NextStep(
        summary="Observe the other leaf.",
        nextTool="worktree_status",
        nextArgs={"contract_path": l01, "enclosure_path": l01},
    )
    exact = contaminated.model_copy(
        update={"nextArgs": {"contract_path": l11, "enclosure_path": l11}}
    )

    assert bound_next_step(response, contaminated) is None
    assert bound_next_step(response, exact) == exact
    assert bound_next_step(WorktreeStatusResponse(ok=True), contaminated) == contaminated


def test_projection_owned_decision_rebinds_every_component_and_clears_controls(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    projected = operation_projection(_validate(_with_worker(record)), contract=contract)
    result = {
        "state": "contract-invalid",
        "developerDecisionRequired": True,
        "nextAction": "developer-decision",
    }

    decision = bind_projection_decision(projected, result, "inspect the exact contract")

    assert decision.componentBindings is not None
    assert decision.identity is not None
    digest = decision.identity.identityDigest
    assert decision.componentBindings.result == digest
    assert decision.componentBindings.recommendedAction == digest
    assert decision.recommendedAction is not None
    assert decision.recommendedAction.action == "developer-decision"
    assert decision.legalControls == []
    assert decision.cancellable is False


def test_projection_rejects_recommendation_or_control_for_another_task(tmp_path: Path) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    projected = operation_projection(_validate(_with_worker(record)), contract=contract)
    assert projected.identity is not None
    other = "/coordination/tasks/repo/other/enclosures/l01/series-contract.md"

    recommendation_payload = projected.model_dump(mode="json")
    recommendation_payload["recommendedAction"] = {
        "action": "observe",
        "tool": "worktree_status",
        "arguments": {"contract_path": other},
        "summary": "observe another task",
    }
    recommendation_payload["componentBindings"]["recommendedAction"] = (
        projected.identity.identityDigest
    )
    with pytest.raises(ValueError, match="different task address"):
        LifecycleOperationProjection.model_validate(recommendation_payload)

    control_payload = projected.model_dump(mode="json")
    control_payload["legalControls"][0]["arguments"]["contract_path"] = other
    with pytest.raises(ValueError, match="different task address"):
        LifecycleOperationProjection.model_validate(control_payload)


def test_projection_rejects_live_cancel_guidance_and_invalid_control_matrix(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    projected = operation_projection(_validate(_with_worker(record)), contract=contract)
    assert projected.identity is not None
    control = projected.legalControls[0]

    required_cancel = projected.model_dump(mode="json")
    required_cancel["recommendedAction"] = {
        "action": "cancel",
        "tool": control["tool"],
        "arguments": control["arguments"],
        "summary": "cancel healthy work",
        "mutating": True,
    }
    required_cancel["componentBindings"]["recommendedAction"] = projected.identity.identityDigest
    with pytest.raises(ValueError, match="healthy live work"):
        LifecycleOperationProjection.model_validate(required_cancel)

    invalid_control = projected.model_dump(mode="json")
    invalid_control["legalControls"][0]["action"] = "integrate"
    invalid_control["cancellable"] = False
    with pytest.raises(ValueError, match="state matrix"):
        LifecycleOperationProjection.model_validate(invalid_control)

    stale_cancellable = projected.model_dump(mode="json")
    stale_cancellable["cancellable"] = False
    with pytest.raises(ValueError, match="exact legal cancel"):
        LifecycleOperationProjection.model_validate(stale_cancellable)

    unreadable_control = projected.model_dump(mode="json")
    unreadable_control["status"] = "unreadable"
    unreadable_control["identity"] = None
    unreadable_control["componentBindings"] = None
    unreadable_control["recommendedAction"] = None
    with pytest.raises(ValueError, match="identity or control authority"):
        LifecycleOperationProjection.model_validate(unreadable_control)


def test_terminal_archive_uses_observed_exit_without_rewriting_audit_identity(
    tmp_path: Path,
) -> None:
    _contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    retained = LifecycleOperationRecord.model_validate(
        _with_worker(record)
        .model_copy(
            update={
                "status": "completed",
                "phase": "completed",
                "result": {"state": "completed"},
                "finishedAt": "2026-09-01T00:01:00+00:00",
            }
        )
        .model_dump(mode="json")
    )
    exit_proof = WorkerTerminationEvidence(
        state="exited",
        pid=4242,
        lease=_LEASE,
        processFingerprint=_WORKER_FINGERPRINT,
        requestedAt="2026-09-01T00:00:30+00:00",
        signal="none",
        observedAt="2026-09-01T00:01:10+00:00",
    )
    original = retained.model_dump_json()
    observed = release_worker_after_exit(retained, exit_proof)

    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.worker.state.observe_worker_termination",
        return_value=exit_proof,
    ) as observe:
        _require_archivable_operation(
            retained,
            operation="worktree_cleanup",
            current=True,
            name="closeout-operation.json",
        )

    observe.assert_called_once()
    assert retained.model_dump_json() == original
    assert retained.workerPid == 4242
    assert observed.workerPid is None


# ---------------------------------------------------------------------------
# L18 diff-coverage closure: refusal/edge cells on the changed projection,
# tool-response, and store surfaces.
# ---------------------------------------------------------------------------


def test_projection_cell_refuses_non_cancel_controls_on_termination_required() -> None:
    # Empty controls pass the invalid-action cell but still violate the exact
    # ["cancel"] requirement, so only [] reaches the termination-required cell.
    with pytest.raises(LifecycleProjectionIncoherence):
        _validate_projection_cell(
            status="termination-required",
            phase="termination-required",
            worker_state="exited",
            result={"state": "worker-termination-required"},
            legal_controls=[],
        )


def test_state_matrix_exhaustiveness_failure_is_raised(monkeypatch) -> None:
    narrowed = {
        status: rule
        for status, rule in op_projection_module.STATE_MATRIX.items()
        if status != "cancelled"
    }
    monkeypatch.setattr(op_projection_module, "STATE_MATRIX", narrowed)
    with pytest.raises(RuntimeError, match="does not exhaust its public vocabulary"):
        validate_state_matrix_is_exhaustive()


def test_validate_projection_state_refuses_cancel_request_outside_termination(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    variant = _validate(
        record.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": "2026-09-01T00:01:00+00:00",
                "cancelRequested": True,
            }
        )
    )
    projected = operation_projection(variant, contract=contract)
    assert projected.status == "incoherent"
    assert projected.result is not None
    assert projected.result["state"] == "lifecycle-projection-incoherent"
    assert projected.legalControls == []


def test_projection_envelope_refusal_cells_are_bounded(tmp_path: Path) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    base = operation_projection(_validate(_with_worker(record)), contract=contract)
    assert base.identity is not None
    assert base.generation is not None

    missing_identity = base.model_dump(mode="json")
    missing_identity["identity"] = None
    with pytest.raises(ValueError, match="requires its exact journal identity"):
        LifecycleOperationProjection.model_validate(missing_identity)

    generation_conflict = base.model_dump(mode="json")
    generation_conflict["generation"] = base.generation + 1
    with pytest.raises(ValueError, match="contradicts its envelope"):
        LifecycleOperationProjection.model_validate(generation_conflict)

    result_binding_conflict = base.model_dump(mode="json")
    result_binding_conflict["result"] = {"state": "fabricated"}
    with pytest.raises(ValueError, match="do not match their envelope"):
        LifecycleOperationProjection.model_validate(result_binding_conflict)

    controls_binding_conflict = base.model_dump(mode="json")
    controls_binding_conflict["legalControls"] = [
        *controls_binding_conflict["legalControls"],
        {"action": "retire", "tool": "worktree_operation_control", "arguments": {}},
    ]
    with pytest.raises(ValueError, match="do not bind the projection envelope"):
        LifecycleOperationProjection.model_validate(controls_binding_conflict)


def test_incoherent_envelope_refuses_advertised_authority(tmp_path: Path) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    contradictory = _validate(
        _with_termination(
            _with_worker(record).model_copy(update={"status": "running", "phase": "quality"}),
            state="termination-required",
            cancel_requested=False,
        )
    )
    incoherent = operation_projection(contradictory, contract=contract)
    assert incoherent.status == "incoherent"
    assert incoherent.identity is not None
    payload = incoherent.model_dump(mode="json")
    payload["legalControls"] = [{"action": "cancel", "tool": "worktree_operation_control"}]
    payload["componentBindings"]["legalControls"] = [payload["identity"]["identityDigest"]]
    with pytest.raises(ValueError, match="cannot advertise mutating authority"):
        LifecycleOperationProjection.model_validate(payload)


def test_component_bindings_helper_refuses_missing_identity() -> None:
    identityless = LifecycleOperationProjection.model_construct(
        status="running",
        phase="quality",
        kind="closeout",
        identity=None,
        componentBindings=None,
    )
    with pytest.raises(ValueError, match="requires its exact journal identity"):
        _require_component_bindings_match_envelope(identityless)


def test_coherent_components_refuse_non_coherent_status() -> None:
    incoherent_envelope = LifecycleOperationProjection.model_construct(
        status="incoherent",
        phase="quality",
        kind="closeout",
    )
    with pytest.raises(ValueError, match="requires a lifecycle status"):
        _require_coherent_projection_components(incoherent_envelope)


def test_coherent_components_require_an_explicit_worker_observation(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    base = operation_projection(_validate(_with_worker(record)), contract=contract)
    workerless = base.model_dump(mode="json")
    workerless["worker"] = None
    workerless["componentBindings"]["worker"] = None
    with pytest.raises(ValueError, match="requires an explicit worker observation"):
        LifecycleOperationProjection.model_validate(workerless)


def test_recommendation_must_match_one_exact_mutating_legal_control(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    base = operation_projection(_validate(record), contract=contract)
    assert base.identity is not None
    digest = base.identity.identityDigest
    control = base.legalControls[0]
    payload = base.model_dump(mode="json")

    payload["recommendedAction"] = {
        "action": control["action"],
        "tool": control["tool"],
        "arguments": control["arguments"],
        "summary": "read-only recommendation",
        "mutating": False,
    }
    payload["componentBindings"]["recommendedAction"] = digest
    with pytest.raises(ValueError, match="recommended control does not match"):
        LifecycleOperationProjection.model_validate(payload)

    payload["recommendedAction"] = {
        "action": control["action"],
        "tool": "worktree_status",
        "arguments": control["arguments"],
        "summary": "wrong tool",
        "mutating": True,
    }
    payload["componentBindings"]["recommendedAction"] = digest
    with pytest.raises(ValueError, match="recommended control does not match"):
        LifecycleOperationProjection.model_validate(payload)


def test_task_address_validation_requires_an_identity() -> None:
    identityless = LifecycleOperationProjection.model_construct(
        status="running",
        phase="quality",
        kind="closeout",
        identity=None,
    )
    with pytest.raises(ValueError, match="requires a projection identity"):
        _require_projection_task_addresses(identityless)


def test_bound_next_step_omits_multi_address_guidance() -> None:
    response = WorktreeStatusResponse(ok=True, contractPath="/one", enclosurePath="/two")
    step = NextStep(
        summary="conflicting addresses",
        nextTool="worktree_status",
        nextArgs={"contract_path": "/one"},
    )
    assert bound_next_step(response, step) is None


def test_bind_projection_result_without_guidance_keeps_envelope(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    projected = operation_projection(_validate(_with_worker(record)), contract=contract)
    rebound = bind_projection_result(projected, {"state": "terminal-edge"})
    assert rebound.guidance is None
    assert rebound.result == {"state": "terminal-edge"}


def test_rebind_projection_refuses_identityless_envelope(tmp_path: Path) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    projected = operation_projection(_validate(_with_worker(record)), contract=contract)
    unreadable = LifecycleOperationProjection.model_validate(
        {
            **projected.model_dump(mode="json"),
            "status": "unreadable",
            "identity": None,
            "componentBindings": None,
            "recommendedAction": None,
            "legalControls": [],
            "cancellable": False,
        }
    )
    with pytest.raises(LifecycleProjectionIncoherence):
        bind_projection_result(unreadable, {"state": "not-visible"})


def test_dependent_identity_requires_door_binding(tmp_path: Path) -> None:
    _contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    identity = operation_projection_identity(record)
    context = OperationProjectionContext(
        door=mock.Mock(),
        doorIdentity=None,
    )
    with pytest.raises(LifecycleProjectionIncoherence) as raised:
        _validate_dependent_identities(identity, context=context)
    assert raised.value.observed == {"doorIdentity": "unbound"}


def test_worker_binding_projection_guards_refuse_incoherent_cells() -> None:
    # The record model already refuses partial bindings, unproven termination
    # identity mismatches, and retained bindings after proven exit; these direct
    # calls pin the projection layer's defensive refusal on the same cells.
    partial = LifecycleOperationRecord.model_construct(
        workerPid=4242,
        workerLease=None,
        workerProcessFingerprint=None,
    )
    with pytest.raises(LifecycleProjectionIncoherence):
        _require_all_or_none_worker_binding(partial, (4242, None, None))

    mismatched_termination = WorkerTerminationEvidence(
        state="requested",
        pid=9999,
        lease=_LEASE,
        processFingerprint=_WORKER_FINGERPRINT,
        requestedAt="2026-09-01T00:00:30+00:00",
    )
    with pytest.raises(LifecycleProjectionIncoherence):
        _termination_bound_worker_observation(
            (4242, _LEASE, _WORKER_FINGERPRINT),
            mismatched_termination,
        )

    exited_retained = WorkerTerminationEvidence(
        state="exited",
        pid=4242,
        lease=_LEASE,
        processFingerprint=_WORKER_FINGERPRINT,
        requestedAt="2026-09-01T00:00:30+00:00",
        observedAt="2026-09-01T00:01:10+00:00",
    )
    with pytest.raises(LifecycleProjectionIncoherence):
        _termination_bound_worker_observation(
            (4242, _LEASE, _WORKER_FINGERPRINT),
            exited_retained,
        )


def test_store_revision_discipline_guards_are_enforced(tmp_path: Path) -> None:
    _contract, store, record = _claimed_contract_and_record(tmp_path)
    del record
    current = store.read()
    assert current is not None

    with pytest.raises(RuntimeError, match="transforms cannot assign record revision"):
        store.update(
            lambda value: value.model_copy(update={"recordRevision": value.recordRevision + 5})
        )

    non_monotonic = current.model_copy(update={"recordRevision": current.recordRevision + 3})
    with pytest.raises(RuntimeError, match="must advance exactly once"):
        _validate_identity_and_evidence_transition(current, non_monotonic)

    same, changed = store.update_if_current(current, lambda value: value)
    assert changed is True
    assert same == current
    assert _advance_record_revision(current, current).recordRevision == (current.recordRevision + 1)
