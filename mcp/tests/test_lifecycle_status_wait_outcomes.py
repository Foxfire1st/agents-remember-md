"""CCR-R18 revision, state-matrix, guidance, and cleanup forcing tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.application.lifecycle import lifecycle_status_wait as wait_app_module
from agents_remember.application.lifecycle.lifecycle_status_wait import (
    LifecycleStatusWaitRequest,
    _coherent_wait_payload,
    _loaded_contract,
    _refusal_expected,
    worktree_status_wait_tool,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.mcp.tools.base import PUBLIC_TOOLS
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
    LifecycleOperationRecord,
    LifecycleOperationRecoveryCommits,
    meaningful_state_changed,
)
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.tools.tool_registry import TOOL_RESPONSE_MODELS
from agents_remember.models.worktree import WorktreeStatusWaitResponse
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
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_closeout_operation,
)
from agents_remember.worktrees.integration.lifecycle.observation.status_wait import (
    OUTCOME_CHANGED,
    OUTCOME_JOURNAL_REPLACED,
    OUTCOME_JOURNAL_UNREADABLE,
    OUTCOME_NO_OPERATION,
    OUTCOME_SUCCESSOR,
    OUTCOME_UNCHANGED,
    OUTCOME_WRONG_CURSOR,
    OUTCOME_WRONG_GENERATION,
    LifecycleWaitClock,
    LifecycleWaitDecision,
    validate_wait_cursor,
    wait_for_lifecycle_change,
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


# ---------------------------------------------------------------------------
# CCR-R15 typed outcomes and the meaningful-state classification map: changed /
# unchanged(timeout) / successor / wrong-contract / wrong-generation /
# wrong-cursor / journal-replaced / journal-unreadable, plus the falsifier that
# heartbeat/current-command/history noise never moves the classification digest.
# ---------------------------------------------------------------------------


def _store_on(tmp_path: Path, name: str = "closeout-operation.json"):
    journal = tmp_path / ".lifecycle" / name
    journal.parent.mkdir(parents=True, exist_ok=True)
    return journal, LifecycleOperationStore(journal)


def _write_record(journal: Path, record: LifecycleOperationRecord) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(record.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")


def _rewrite_record(journal: Path, record: LifecycleOperationRecord, **updates):
    variant = LifecycleOperationRecord.model_validate(
        record.model_copy(update=updates).model_dump(mode="json")
    )
    _write_record(journal, variant)
    return variant


def test_wrong_cursor_is_refused_before_any_journal_read(tmp_path: Path) -> None:

    assert validate_wait_cursor(0) is not None
    assert validate_wait_cursor(-3) is not None
    assert validate_wait_cursor(1) is None
    journal, store = _store_on(tmp_path)
    del journal
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=1,
        after_revision=0,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_WRONG_CURSOR
    assert decision.record is None


def test_no_operation_refuses_typed_when_journal_is_absent(tmp_path: Path) -> None:

    journal, store = _store_on(tmp_path)
    assert not journal.exists()
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=1,
        after_revision=1,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_NO_OPERATION


def test_malformed_journal_is_a_typed_unreadable_refusal(tmp_path: Path) -> None:

    journal, store = _store_on(tmp_path)
    journal.write_text("{ not json", encoding="utf-8")
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=1,
        after_revision=1,
        timeout_seconds=0.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_JOURNAL_UNREADABLE
    assert decision.readError is not None


def test_meaningful_classification_ignores_noise_and_flags_meaningful_fields(
    tmp_path: Path,
) -> None:

    contract, _store, record = _claimed_contract_and_record(tmp_path)
    del contract
    noise_fields = {
        "heartbeatAt": "2026-09-04T00:01:00+00:00",
        "currentCommand": "a new current command",
        "recordRevision": record.recordRevision + 1,
        "meaningfulRevision": record.meaningfulRevision + 1,
    }
    for field, value in noise_fields.items():
        assert not meaningful_state_changed(record, record.model_copy(update={field: value})), field
    meaningful = {
        "generation": record.generation + 1,
        "generationDisposition": "superseded",
        "status": "running",
        "phase": "preflight",
        "attempt": record.attempt + 1,
        "approvalClaimed": True,
        "irreversibleBoundaryEntered": True,
        "cancelRequested": True,
        "failure": "typed failure",
        "result": {"state": "failed", "reason": "typed"},
        "recoveryCommits": LifecycleOperationRecoveryCommits(codeCommit="a" * 40),
        "workerTermination": WorkerTerminationEvidence(
            state="requested",
            pid=4242,
            lease=_LEASE,
            processFingerprint=_WORKER_FINGERPRINT,
            requestedAt="2026-09-04T00:00:00+00:00",
        ),
        "closeoutFinalizedContractSha256": "b" * 64,
    }
    for field, value in meaningful.items():
        assert meaningful_state_changed(record, record.model_copy(update={field: value})), field


def test_wait_reports_unchanged_timeout_without_failure(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    unchanged = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision,
        timeout_seconds=0.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert unchanged.outcome == OUTCOME_UNCHANGED
    assert unchanged.record == record


def test_wait_reports_changed_with_next_cursor_after_meaningful_advance(
    tmp_path: Path,
) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    advanced = _rewrite_record(
        store.path,
        record,
        status="running",
        phase="preflight",
        meaningfulRevision=record.meaningfulRevision + 1,
    )
    changed = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert changed.outcome == OUTCOME_CHANGED
    assert changed.record == advanced


def test_journal_replaced_behind_cursor_refuses_typed(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision + 5,
        timeout_seconds=0.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_JOURNAL_REPLACED


def test_wrong_generation_refuses_without_silently_watching(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation + 1,
        after_revision=record.meaningfulRevision,
        timeout_seconds=0.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_WRONG_GENERATION


def test_generation_successor_wakes_old_wait_with_explicit_information(
    tmp_path: Path,
) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    journal = store.path
    successor = _rewrite_record(
        journal,
        record,
        generation=record.generation + 1,
        meaningfulRevision=record.meaningfulRevision + 1,
        attempt=1,
        predecessorFingerprint=record.fingerprint,
        recordRevision=record.recordRevision + 2,
    )
    # Archive the waited predecessor with its exact successor fingerprint.
    archive = journal.with_name(f"{journal.stem}.generation-{record.generation}.json")
    archived = LifecycleOperationRecord.model_validate(
        record.model_copy(update={"successorFingerprint": successor.fingerprint}).model_dump(
            mode="json"
        )
    )
    archive.write_text(
        archived.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8"
    )
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_SUCCESSOR
    assert decision.successorGeneration == successor.generation
    assert decision.record == successor


def test_unproven_successor_archive_refuses_wrong_generation(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    journal = store.path
    _rewrite_record(
        journal,
        record,
        generation=record.generation + 1,
        meaningfulRevision=record.meaningfulRevision + 1,
        attempt=1,
        recordRevision=record.recordRevision + 2,
    )
    # No archive: the successor claim is unproven and the wait must refuse typed.
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_WRONG_GENERATION


def test_application_refusals_never_recommend_mutation(tmp_path: Path) -> None:
    """Every typed refusal names the exact read-only next snapshot action."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    config = load_config(tmp_path / "settings.json")
    path = contract.contract_path.as_posix()
    refusals = [
        worktree_status_wait_tool(
            config,
            LifecycleStatusWaitRequest(
                contract_path=path,
                operation_kind="closeout",
                expected_generation=record.generation,
                after_revision=0,
                timeout_seconds=0.0,
            ),
        ),
        worktree_status_wait_tool(
            config,
            LifecycleStatusWaitRequest(
                contract_path=path,
                operation_kind="closeout",
                expected_generation=record.generation + 1,
                after_revision=record.meaningfulRevision,
                timeout_seconds=0.0,
            ),
        ),
        worktree_status_wait_tool(
            config,
            LifecycleStatusWaitRequest(
                contract_path=path,
                operation_kind="closeout",
                expected_generation=record.generation,
                after_revision=record.meaningfulRevision + 5,
                timeout_seconds=0.0,
            ),
        ),
    ]
    for refusal in refusals:
        assert refusal["ok"] is False
        assert refusal["state"] == "refused"
        assert refusal["nextAction"] == "snapshot"
        assert refusal["nextTool"] == "worktree_status"
        serialized = json.dumps(refusal)
        for forbidden in (
            "recommendedAction",
            "retry",
            "recover",
            "cancelRequested",
            "operationKey",
            "workerPid",
        ):
            assert forbidden not in serialized, (refusal["outcome"], forbidden)


def test_unreadable_journal_application_refusal_is_typed(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    config = load_config(tmp_path / "settings.json")
    store.path.write_text("{ broken", encoding="utf-8")
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=contract.contract_path.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is False
    assert payload["outcome"] == "journal-unreadable"
    assert "expected" in payload and "observed" in payload


def test_application_wrong_contract_path_is_a_typed_address_refusal(
    tmp_path: Path,
) -> None:
    """Path confinement failure returns the typed wrong-contract refusal."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    config = load_config(tmp_path / "settings.json")
    outside = contract.code_repo_path / "outside-contract.md"
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=outside.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is False
    assert payload["outcome"] == "wrong-contract"
    assert payload["state"] == "refused"
    assert payload["nextTool"] == "worktree_status"
    assert "operationKey" not in json.dumps(payload)


def test_application_unpublished_location_is_a_typed_refusal(tmp_path: Path) -> None:
    """A coordination-confined path with no published locator refuses typed."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    config = load_config(tmp_path / "settings.json")
    unpublished = contract.coordination_root / "tasks/repo/unpublished/unpublished.md"
    unpublished.parent.mkdir(parents=True, exist_ok=True)
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=unpublished.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is False
    assert payload["status"] == "operation-location-adoption-required"
    assert payload["nextAction"] == "developer-decision"


def test_application_successor_outcome_carries_successor_generation(tmp_path: Path) -> None:
    """The successor wire payload returns explicit successor information."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    config = load_config(tmp_path / "settings.json")
    journal = store.path
    successor = _rewrite_record(
        journal,
        record,
        generation=record.generation + 1,
        meaningfulRevision=record.meaningfulRevision + 1,
        attempt=1,
        predecessorFingerprint=record.fingerprint,
        recordRevision=record.recordRevision + 2,
    )
    archive = journal.with_name(f"{journal.stem}.generation-{record.generation}.json")
    archived = LifecycleOperationRecord.model_validate(
        record.model_copy(update={"successorFingerprint": successor.fingerprint}).model_dump(
            mode="json"
        )
    )
    archive.write_text(
        archived.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=contract.contract_path.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is True
    assert payload["outcome"] == "successor"
    assert payload["successorGeneration"] == successor.generation
    assert "lifecycleOperation" in payload
    assert "operationKey" not in json.dumps(payload)


def test_application_incoherent_projection_is_a_read_only_refusal(tmp_path: Path) -> None:
    """A meaningful advance whose projection is incoherent refuses without action."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    config = load_config(tmp_path / "settings.json")
    # running + phase failed is not a declared state-matrix cell, so the record
    # projects incoherent even though the journal advanced meaningfully.
    _rewrite_record(
        store.path,
        record,
        status="running",
        phase="failed",
        meaningfulRevision=record.meaningfulRevision + 1,
    )
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=contract.contract_path.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is False
    assert payload["outcome"] == "projection-incoherent"
    assert payload["nextTool"] == "worktree_status"
    projection = payload["lifecycleOperation"]
    assert projection["status"] == "incoherent"
    # The typed R18 refusal carries no mutating recommendation or legal control:
    # the matrix diagnostic cells may name vocabulary, but nothing actionable.
    assert projection["legalControls"] == []
    assert projection["cancellable"] is False
    assert "recommendedAction" not in json.dumps(payload)


def test_loaded_contract_degrades_to_none_when_unreadable(tmp_path: Path) -> None:
    """A torn contract degrades to no contract; the envelope stays projectable."""
    bogus = tmp_path / "bogus.md"
    bogus.write_text("not: [a valid contract\n", encoding="utf-8")
    assert _loaded_contract(bogus) is None


def test_coherent_payload_refuses_a_missing_record(tmp_path: Path) -> None:
    """A coherent outcome without its durable record is a programmer error."""

    decision = LifecycleWaitDecision(outcome="changed", record=None)
    with pytest.raises(RuntimeError, match="must carry its durable record"):
        _coherent_wait_payload(
            tmp_path / "contract.md",
            operation_kind="closeout",
            decision=decision,
            timeout_seconds=0.0,
        )


def test_coherent_payload_omits_projection_when_observation_is_unavailable(
    tmp_path: Path,
) -> None:
    """A None observed projection simply omits the envelope, staying coherent."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    decision = LifecycleWaitDecision(outcome="changed", record=record)
    with mock.patch.object(
        wait_app_module,
        "observed_operation_projection",
        return_value=None,
    ):
        payload = wait_app_module._coherent_wait_payload(
            contract.contract_path,
            operation_kind="closeout",
            decision=decision,
            timeout_seconds=0.0,
        )
    assert payload["ok"] is True
    assert "lifecycleOperation" not in payload


def test_refusal_expected_names_a_successor_proof_cell() -> None:
    """The typed refusal guidance for successor proof is total even when unused."""

    expected = _refusal_expected(LifecycleWaitDecision(outcome="successor"))
    assert expected["generation"] == "waited generation + 1 with archived successor proof"


@pytest.mark.parametrize("archive_kind", ["oversized", "malformed", "non-object"])
def test_unproven_successor_archive_variants_refuse_wrong_generation(
    tmp_path: Path,
    archive_kind: str,
) -> None:
    """Oversized, malformed, and non-object predecessor archives never wake a wait."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    journal = store.path
    _rewrite_record(
        journal,
        record,
        generation=record.generation + 1,
        meaningfulRevision=record.meaningfulRevision + 1,
        attempt=1,
        recordRevision=record.recordRevision + 2,
    )
    archive = journal.with_name(f"{journal.stem}.generation-{record.generation}.json")
    if archive_kind == "oversized":
        archive.write_text("x" * (4 * 1024 * 1024 + 1), encoding="utf-8")
    elif archive_kind == "malformed":
        archive.write_text("{ not json", encoding="utf-8")
    else:
        archive.write_text("[]", encoding="utf-8")
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_WRONG_GENERATION


def test_wait_tool_is_registered_and_response_model_is_typed() -> None:

    assert "worktree_status_wait" in PUBLIC_TOOLS
    assert TOOL_RESPONSE_MODELS["worktree_status_wait"] is WorktreeStatusWaitResponse
    names = list(PUBLIC_TOOLS)
    assert names.index("worktree_status") + 1 == names.index("worktree_status_wait")
    model = WorktreeStatusWaitResponse.model_validate(
        {
            "ok": True,
            "operation": "worktree_status_wait",
            "outcome": "unchanged",
            "state": "unchanged",
            "contractPath": "/tmp/example/contract.md",
            "meaningfulRevision": 3,
            "timeoutSeconds": 0.0,
        }
    )
    assert model.operation == "worktree_status_wait"
    assert model.outcome == "unchanged"
