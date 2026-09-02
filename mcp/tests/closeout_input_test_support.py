"""Explicit normalized closeout inputs for behavioral fixtures."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agents_remember.models.closeout.input import CloseoutCorrectedCall, EffectiveCloseoutInput
from agents_remember.models.lifecycles.door import (
    CloseoutDoorGeneration,
    DoorAdmissionProvenance,
    DoorDependencyInputs,
    DoorProvenance,
    DoorSchedulingProvenance,
    closeout_door_dependencies,
)
from agents_remember.models.lifecycles.mutation_evidence import (
    CloseoutMutationLeg,
    GitMutationEvidence,
    GitMutationSnapshot,
)
from agents_remember.models.lifecycles.operation import CloseoutOperationInput
from agents_remember.models.task_document_ref import TaskDocumentRef
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
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    derive_closeout_recovery_commits,
)
from agents_remember.worktrees.integration.closeout.task_intent_identity import (
    contract_task_intent,
)
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operations as lifecycle_operations_module,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_closeout_operation,
)
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.worktree_contract import load_contract, write_contract


class MutationEvidenceRecorder:
    """Explicit unit-test authority that verifies every published Git transition."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.evidence: dict[CloseoutMutationLeg, GitMutationEvidence] = {}

    def __call__(self, phase: str, values) -> None:
        captured = dict(values)
        self.events.append((phase, captured))
        raw = captured.get("mutation_evidence")
        if raw is None:
            return
        current = GitMutationEvidence.model_validate(raw)
        previous = self.evidence.get(current.leg)
        if previous is None:
            assert current.state == "mutation-intent"
        else:
            allowed = {
                "mutation-intent": {"mutation-intent", "commit-proven"},
                "commit-proven": {"commit-proven"},
            }
            assert current.state in allowed[previous.state]
            assert current.before == previous.before
            if previous.expectedOutputTree is not None:
                assert current.expectedOutputTree == previous.expectedOutputTree
        self.evidence[current.leg] = current

    def assert_proven(self, *legs: CloseoutMutationLeg) -> None:
        assert set(self.evidence) == set(legs)
        assert all(self.evidence[leg].state == "commit-proven" for leg in legs)


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


def publish_closeout_finalization(runtime, contract) -> None:
    """Publish the exact proof production records before queue certification."""

    runtime.progress(
        "contract-finalization",
        {
            "approval_claimed": True,
            "recovery_commits": {
                "codeCommit": contract.code_commit,
                "memoryContentCommit": contract.memory_content_commit,
                "ledgerCommit": contract.ledger_commit,
            },
            "closeout_finalized_contract_sha256": closeout_contract_sha256(contract),
        },
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


def closeout_worktree_args(
    contract,
    *,
    code: str | None = "close code candidate",
    memory: str | None = "close external memory",
    ledger: str | None = "record code-to-memory mapping",
    **values,
) -> WorktreeArgs:
    values.setdefault("certification_profile", Path("mcp/certification-profile-v1.json"))
    effective = normalize_closeout_input(
        contract,
        raw_closeout_messages(code=code, memory=memory, ledger=ledger),
        route="worktree",
        corrected_call=CloseoutCorrectedCall(
            tool="worktree_closeout_apply",
            arguments={"contract_path": contract.contract_path.as_posix()},
        ),
    )
    return WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=effective,
        **values,
    )


def with_mutation_intent(record, *, leg: CloseoutMutationLeg | None = None):
    """Publish a structurally valid fixture intent for one enabled leg."""
    selected = leg or next(iter(record.mutationEvidence))
    current = record.mutationEvidence[selected]
    before = GitMutationSnapshot(
        headRef="refs/heads/test-closeout",
        head="a" * 40,
        headTree="b" * 40,
        refLogFingerprint="f" * 64,
        indexTree="b" * 40,
        candidateTree="c" * 40,
        statusFingerprint="d" * 64,
    )
    intent = current.model_copy(
        update={
            "state": "mutation-intent",
            "before": before,
            "expectedOutputTree": "c" * 40,
        }
    )
    evidence = dict(record.mutationEvidence)
    evidence[selected] = intent
    return record.model_copy(update={"mutationEvidence": evidence})


def with_commit_proven(
    record,
    *,
    leg: CloseoutMutationLeg | None = None,
    commit: str | None = None,
):
    """Advance one enabled fixture leg through intent and durable proof."""
    selected = leg or next(iter(record.mutationEvidence))
    if record.mutationEvidence[selected].state == "pre-mutation":
        record = with_mutation_intent(record, leg=selected)
    current = record.mutationEvidence[selected]
    assert current.before is not None
    proof_commit = commit or "e" * 40
    observed = current.before.model_copy(
        update={
            "head": proof_commit,
            "headTree": current.expectedOutputTree,
            "refLogFingerprint": "1" * 64,
            "indexTree": current.expectedOutputTree,
            "candidateTree": current.expectedOutputTree,
            "statusFingerprint": "2" * 64,
        }
    )
    proven = current.model_copy(
        update={"state": "commit-proven", "observed": observed, "commit": proof_commit}
    )
    evidence = dict(record.mutationEvidence)
    evidence[selected] = proven
    updated = record.model_copy(
        update={"mutationEvidence": evidence, "irreversibleBoundaryEntered": True}
    )
    return updated.model_copy(update={"recoveryCommits": derive_closeout_recovery_commits(updated)})


def with_reconciled_unchanged(record, *, leg: CloseoutMutationLeg | None = None):
    """Advance one enabled fixture leg to an exact no-output reconciliation."""
    selected = leg or next(iter(record.mutationEvidence))
    if record.mutationEvidence[selected].state == "pre-mutation":
        record = with_mutation_intent(record, leg=selected)
    current = record.mutationEvidence[selected]
    assert current.before is not None
    reconciled = current.model_copy(
        update={"state": "reconciled-unchanged", "observed": current.before}
    )
    evidence = dict(record.mutationEvidence)
    evidence[selected] = reconciled
    return record.model_copy(update={"mutationEvidence": evidence})
