"""Canonical task and provenance source owner for closeout-door generations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from agents_remember.errors import TaskIntentError
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.models.closeout.source import (
    CandidateAdmissionFacts,
    SchedulingGradeInput,
)
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.door import (
    CloseoutDoorDisposition,
    CloseoutDoorGeneration,
    CloseoutDoorRequest,
    DoorAdmissionProvenance,
    DoorEvidenceFact,
    DoorSchedulingProvenance,
)
from agents_remember.models.task_intent import TaskIntentIdentity, task_intent_is_missing
from agents_remember.tasks import completion_blockers
from agents_remember.tasks.document_refs import (
    ResolvedTaskDocument,
    TaskDocumentRefError,
    TaskDocumentTopology,
)
from agents_remember.tasks.leaf_doc import resolve_terminal_leaf_doc
from agents_remember.tasks.task_intent import task_intent_identity
from agents_remember.worktrees.integration.closeout.door_evidence import (
    capture_door_candidate_evidence,
)
from agents_remember.worktrees.queue.closeout_projection import now_iso
from agents_remember.worktrees.queue.closeout_projection_members import (
    candidate_task_topology_fingerprint,
)
from agents_remember.worktrees.queue.closeout_queue_errors import CloseoutQueueError
from agents_remember.worktrees.queue.closeout_queue_evidence import (
    GradeAuthority,
    canonical_grade,
    planning_authorities,
)
from agents_remember.worktrees.queue.closeout_queue_graph import graph_context
from agents_remember.worktrees.worktree_contract import WorktreeContract


@dataclass(frozen=True)
class DoorSourceContext:
    contract: WorktreeContract
    candidate: ResolvedTaskDocument | None
    master: ResolvedTaskDocument
    sprint: ResolvedTaskDocument
    graph: Any


def door_task_context(
    config: McpRuntimeConfig,
    contract: WorktreeContract,
    request: CloseoutDoorRequest,
) -> DoorSourceContext:
    """Resolve the exact leaf or sanctioned series candidate source addresses."""

    topology = TaskDocumentTopology(contract.coordination_root)
    try:
        candidate = _door_candidate(config, contract, request, topology)
        master, sprint = _door_owners(contract, candidate, topology)
    except TaskDocumentRefError as exc:
        raise CloseoutQueueError(exc.status, str(exc)) from exc
    authored_graph = sprint.document.executionGraph
    graph = (
        graph_context(topology, sprint.ref, authored_graph=authored_graph)
        if authored_graph is not None
        else None
    )
    return DoorSourceContext(
        contract,
        candidate,
        master,
        graph.sprint if graph is not None else sprint,
        graph,
    )


def _door_candidate(
    config: McpRuntimeConfig,
    contract: WorktreeContract,
    request: CloseoutDoorRequest,
    topology: TaskDocumentTopology,
) -> ResolvedTaskDocument | None:
    if contract.kind == "leaf":
        return _leaf_door_candidate(contract, request, topology)
    if contract.kind == "series":
        return _series_door_candidate(config, contract, request, topology)
    raise TaskDocumentRefError(
        "closeout-door-contract-kind-refused",
        "closeout-door source publication requires a leaf or series contract",
    )


def _leaf_door_candidate(
    contract: WorktreeContract,
    request: CloseoutDoorRequest,
    topology: TaskDocumentTopology,
) -> ResolvedTaskDocument:
    if request.candidate_task_document_ref is not None:
        raise TaskDocumentRefError(
            "closeout-door-candidate-assertion-forbidden",
            "leaf enclosure already owns its exact candidate task address",
        )
    found = resolve_terminal_leaf_doc(contract.task_root, contract.leaf_id)
    if found is None:
        raise TaskDocumentRefError(
            "closeout-door-task-missing",
            "leaf contract has no exact canonical task document",
        )
    return topology.resolve(topology.canonical_ref(contract.repo_name, found[0]))


def _series_door_candidate(
    config: McpRuntimeConfig,
    contract: WorktreeContract,
    request: CloseoutDoorRequest,
    topology: TaskDocumentTopology,
) -> ResolvedTaskDocument | None:
    if not config.direct_execution_enabled:
        raise TaskDocumentRefError(
            "closeout-door-direct-policy-disabled",
            "series door publication requires sanctioned direct execution policy",
        )
    asserted = request.candidate_task_document_ref or (
        contract.closeout_door.taskDocumentRef if contract.closeout_door is not None else None
    )
    if asserted is None:
        if request.action != "status":
            raise TaskDocumentRefError(
                "closeout-door-candidate-required",
                "series door declaration for direct landing requires candidate_task_document_ref",
            )
        return None
    candidate = topology.resolve(asserted)
    if not candidate.path.resolve().is_relative_to(contract.task_root.resolve()):
        raise TaskDocumentRefError(
            "closeout-door-candidate-outside-task",
            "series door candidate must be inside the contract task root",
        )
    return candidate


def _door_owners(
    contract: WorktreeContract,
    candidate: ResolvedTaskDocument | None,
    topology: TaskDocumentTopology,
) -> tuple[ResolvedTaskDocument, ResolvedTaskDocument]:
    if candidate is None:
        master = topology.resolve(
            topology.canonical_ref(
                contract.repo_name,
                contract.task_artifact.with_suffix(".json"),
            )
        )
        master_ref = master.ref
    else:
        master_ref = topology.parent(candidate.ref)
        if master_ref is None:
            raise TaskDocumentRefError("task-document-parent-missing", candidate.ref.key)
        master = topology.resolve(master_ref)
    if contract.kind == "series" and master.path.resolve() != (
        contract.task_artifact.with_suffix(".json").resolve()
    ):
        raise TaskDocumentRefError(
            "closeout-door-candidate-owner-mismatch",
            "series door candidate is not directly owned by the contract task root",
        )
    sprint_ref = topology.parent(master_ref)
    if sprint_ref is None:
        raise TaskDocumentRefError("task-document-parent-missing", master_ref.key)
    return master, topology.resolve(sprint_ref)


def authorize_door_actor(
    actor: DeclaredCaller,
    context: DoorSourceContext,
    action: str,
) -> None:
    if actor.role == "manager" and actor.task_document_ref == context.master.ref:
        return
    if (
        action != "declare"
        and actor.role in {"architect", "orchestrator"}
        and (actor.task_document_ref == context.sprint.ref)
    ):
        return
    raise CloseoutQueueError(
        "closeout-door-caller-refused",
        "door declaration requires the owning manager; later source controls require "
        "that manager or the sprint architect/orchestrator",
    )


def updated_door_generation(
    context: DoorSourceContext,
    request: CloseoutDoorRequest,
    actor: DeclaredCaller,
) -> CloseoutDoorGeneration:
    """Derive one idempotent source mutation from current canonical facts."""

    if request.action == "declare":
        return _declared_generation(context, request, actor)
    if request.action == "update-provenance":
        return _provenance_successor(context, request, actor)
    return _transitioned_generation(context.contract.closeout_door, request)


def _declared_generation(
    context: DoorSourceContext,
    request: CloseoutDoorRequest,
    actor: DeclaredCaller,
) -> CloseoutDoorGeneration:
    current = context.contract.closeout_door
    if current is None or current.disposition == "withdrawn":
        return _declare_generation(
            context,
            request,
            actor,
            predecessor=current.generationId if current is not None else "",
            disposition="waiting",
        )
    replay = _declare_generation(
        context,
        request,
        actor,
        predecessor=current.predecessorGenerationId,
        disposition="waiting",
    )
    if replay.generationId == current.generationId:
        return current
    raise CloseoutQueueError(
        "closeout-door-already-declared",
        "the current live generation has different canonical declaration content",
    )


def _provenance_successor(
    context: DoorSourceContext,
    request: CloseoutDoorRequest,
    actor: DeclaredCaller,
) -> CloseoutDoorGeneration:
    assert request.expected_generation_id is not None
    current = _required_provenance_generation(context)
    if current.generationId != request.expected_generation_id:
        return _replayed_provenance_successor(context, request, actor, current)
    _require_provenance_source_disposition(current)
    return _declare_generation(
        context,
        request,
        actor,
        predecessor=current.generationId,
        disposition=current.disposition,
    )


def _required_provenance_generation(context: DoorSourceContext) -> CloseoutDoorGeneration:
    current = context.contract.closeout_door
    if current is None:
        raise CloseoutQueueError(
            "closeout-door-source-update-refused",
            "provenance update has no current door generation",
        )
    return current


def _replayed_provenance_successor(
    context: DoorSourceContext,
    request: CloseoutDoorRequest,
    actor: DeclaredCaller,
    current: CloseoutDoorGeneration,
) -> CloseoutDoorGeneration:
    assert request.expected_generation_id is not None
    replay = _declare_generation(
        context,
        request,
        actor,
        predecessor=request.expected_generation_id,
        disposition="waiting",
    )
    observed = (current.predecessorGenerationId, replay.generationId)
    expected = (request.expected_generation_id, current.generationId)
    if observed == expected:
        return current
    _require_expected(current, request.expected_generation_id)
    raise AssertionError("generation mismatch refusal must raise")


def _require_provenance_source_disposition(current: CloseoutDoorGeneration) -> None:
    if current.disposition not in {"waiting", "deferred"}:
        raise CloseoutQueueError(
            "closeout-door-source-update-refused",
            "only a waiting or deferred generation may publish a provenance successor",
        )


def _transitioned_generation(
    current: CloseoutDoorGeneration | None,
    request: CloseoutDoorRequest,
) -> CloseoutDoorGeneration:
    _require_expected(current, request.expected_generation_id)
    if current is None:
        raise CloseoutQueueError(
            "closeout-door-missing", "door source transition has no generation"
        )
    if task_intent_is_missing(current.taskIntent):
        raise CloseoutQueueError(
            "closeout-door-task-intent-unavailable",
            "legacy door source cannot transition until update-provenance republishes task intent",
        )
    target = {"defer": "deferred", "resume": "waiting", "withdraw": "withdrawn"}[request.action]
    allowed = {
        "defer": {"waiting", "deferred"},
        "resume": {"waiting", "deferred"},
        "withdraw": {"waiting", "deferred", "withdrawn"},
    }[request.action]
    if current.disposition not in allowed:
        raise CloseoutQueueError(
            "closeout-door-transition-refused",
            f"{request.action} cannot transition {current.disposition!r}",
        )
    return current.model_copy(update={"disposition": target})


def superseding_door_generation(
    config: McpRuntimeConfig,
    contract: WorktreeContract,
    *,
    actor: DeclaredCaller,
    grade: SchedulingGradeInput,
    admission: CandidateAdmissionFacts,
) -> CloseoutDoorGeneration:
    """Build the fresh waiting source successor authorized by journal supersession."""

    current = contract.closeout_door
    if current is None or current.disposition != "claimed":
        raise CloseoutQueueError(
            "closeout-door-supersede-owner-mismatch",
            "journal supersession requires its exact current claimed door predecessor",
        )
    request = CloseoutDoorRequest(
        action="update-provenance",
        contract_path=contract.contract_path.as_posix(),
        candidate_task_document_ref=(
            current.taskDocumentRef if contract.kind == "series" else None
        ),
        expected_generation_id=current.generationId,
        grade=grade,
        admission=admission,
        caller=actor,
    )
    context = door_task_context(config, contract, request)
    authorize_door_actor(actor, context, "update-provenance")
    return _declare_generation(
        context,
        request,
        actor,
        predecessor=current.generationId,
        disposition="waiting",
    )


def _declare_generation(
    context: DoorSourceContext,
    request: CloseoutDoorRequest,
    actor: DeclaredCaller,
    *,
    predecessor: str,
    disposition: CloseoutDoorDisposition,
) -> CloseoutDoorGeneration:
    contract = context.contract
    candidate = context.candidate
    master = context.master
    sprint = context.sprint
    if candidate is None:
        raise CloseoutQueueError(
            "closeout-door-candidate-required",
            "source publication requires one exact candidate task document",
        )
    unresolved = completion_blockers(candidate.document)
    if unresolved:
        raise CloseoutQueueError(
            "closeout-door-task-incomplete",
            "door declaration requires a complete candidate task: "
            f"{[row.model_dump() for row in unresolved]!r}",
        )
    current_evidence = capture_door_candidate_evidence(contract, candidate)
    candidate_tree = current_evidence.candidate_tree
    ledger_commit = current_evidence.ledger_memory_commit
    grade_input = request.grade
    admission = request.admission
    assert grade_input is not None and admission is not None
    grade, grade_digest, grade_facts = canonical_grade(
        grade_input.model_dump(mode="json"),
        authority=_grade_authority(context),
        candidate_ref=candidate.ref,
        owning_master=master.ref,
    )
    admission_provenance = DoorAdmissionProvenance(
        **admission.model_dump(mode="json"),
        fingerprint=_fingerprint(admission.model_dump(mode="json")),
    )
    scheduling = DoorSchedulingProvenance(
        priority=grade.priority,
        judgmentId=grade.judgmentId,
        fingerprint=grade_digest,
        evidence=[_door_fact(fact) for fact in grade_facts],
    )
    memory_tree = current_evidence.memory_candidate_tree
    task_fingerprint = candidate_task_topology_fingerprint(
        sprint,
        master,
        candidate,
        graph=context.graph,
    )
    intent = _door_task_intent(contract, candidate)
    identity = {
        "schema": "ar-closeout-door/v1",
        "predecessor": predecessor,
        "task": candidate.ref.model_dump(mode="json"),
        "master": master.ref.model_dump(mode="json"),
        "sprint": sprint.ref.model_dump(mode="json"),
        "contract": contract.contract_path.as_posix(),
        "candidateTree": candidate_tree,
        "memoryCandidateTree": memory_tree,
        "codeBaseCommit": contract.code_base_commit,
        "memoryBaseCommit": contract.memory_base_commit,
        "ledgerMemoryCommit": ledger_commit,
        "taskTopologyFingerprint": task_fingerprint,
        "taskIntent": intent.model_dump(mode="json", by_alias=True),
        "review": current_evidence.review.model_dump(mode="json"),
        "memory": current_evidence.memory.model_dump(mode="json"),
        "ledger": current_evidence.ledger.model_dump(mode="json"),
        "admission": admission_provenance.model_dump(mode="json"),
        "scheduling": scheduling.model_dump(mode="json"),
    }
    return CloseoutDoorGeneration(
        generationId=_fingerprint(identity),
        predecessorGenerationId=predecessor,
        disposition=disposition,
        taskId=contract.task_id,
        taskName=contract.task_name,
        taskDocumentRef=candidate.ref,
        owningMasterTaskDocumentRef=master.ref,
        sprintTaskDocumentRef=sprint.ref,
        contractPath=contract.contract_path.as_posix(),
        candidateTree=candidate_tree,
        memoryCandidateTree=memory_tree,
        codeBaseCommit=contract.code_base_commit,
        memoryBaseCommit=contract.memory_base_commit,
        ledgerMemoryCommit=ledger_commit,
        taskTopologyFingerprint=task_fingerprint,
        taskIntent=intent,
        reviewProvenance=current_evidence.review,
        memoryProvenance=current_evidence.memory,
        ledgerProvenance=current_evidence.ledger,
        admissionProvenance=admission_provenance,
        schedulingProvenance=scheduling,
        declaredBy=f"{actor.role}@{actor.task_document_ref.key}",
        declaredAt=now_iso(),
    )


def _door_task_intent(
    contract: WorktreeContract,
    candidate: ResolvedTaskDocument,
) -> TaskIntentIdentity:
    try:
        return task_intent_identity(contract.task_root, candidate)
    except TaskIntentError as exc:
        raise CloseoutQueueError(exc.status, exc.detail) from exc


def _require_expected(
    current: CloseoutDoorGeneration | None,
    expected_generation_id: str | None,
) -> None:
    if expected_generation_id is None:
        return
    if current is None or current.generationId != expected_generation_id:
        raise CloseoutQueueError(
            "closeout-door-generation-stale",
            "door source changed after the caller observed its generation",
        )


def _grade_authority(context: DoorSourceContext) -> GradeAuthority:
    if context.graph is not None:
        return context.graph.grade_authority
    judgments, priorities = planning_authorities(context.sprint)
    return GradeAuthority(context.sprint, judgments, priorities)


def _door_fact(fact: Any) -> DoorEvidenceFact:
    return DoorEvidenceFact(path=fact.path, sha256=fact.sha256)


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
