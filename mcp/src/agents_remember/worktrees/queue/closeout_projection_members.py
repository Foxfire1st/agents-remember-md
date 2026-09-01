"""Candidate-local readiness and ordering for closeout projection members."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from agents_remember.models.closeout.projection import (
    MAX_CLOSEOUT_REASONS,
    CloseoutProjectionMember,
)
from agents_remember.models.lifecycles.door import CloseoutDoorGeneration
from agents_remember.tasks import completion_blockers
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.semantic_topology import (
    SEMANTIC_TOPOLOGY_SCHEMA,
    SemanticTopologyError,
    SemanticTopologyV2,
)
from agents_remember.tasks.semantic_topology import (
    semantic_topology_fingerprint as task_semantic_topology_fingerprint,
)
from agents_remember.tasks.semantic_topology import (
    semantic_topology_projection as task_semantic_topology_projection,
)

from .closeout_queue_errors import CloseoutQueueError
from .closeout_queue_evidence import GradeAuthority, canonical_grade
from .closeout_queue_graph import QueueGraphContext, candidate_node, predecessor_waiting_reasons


@dataclass(frozen=True)
class ProjectionMemberContext:
    source_address: Path
    door: CloseoutDoorGeneration
    candidate: ResolvedTaskDocument
    master: ResolvedTaskDocument
    order: int
    sprint: ResolvedTaskDocument
    graph: QueueGraphContext | None
    task_topology_fingerprint: str
    activation_waiting: tuple[str, ...] = ()
    source_blockers: tuple[str, ...] = ()


def projection_member(context: ProjectionMemberContext) -> CloseoutProjectionMember:
    """Re-evaluate one waiting generation from exact current task/source facts."""

    door = context.door
    order = context.order
    blockers = _projection_blockers(context)
    waiting = _admission_waiting_reasons(door)
    waiting.extend(context.activation_waiting)
    graph_waiting, order = _dependency_waiting_and_order(context, order)
    waiting.extend(graph_waiting)
    reasons = _bounded_reasons([*blockers, *waiting])
    classification = "blocked" if blockers else "waiting" if waiting else "ready"
    return CloseoutProjectionMember(
        generationId=door.generationId,
        taskDocumentRef=door.taskDocumentRef,
        owningMaster=door.owningMasterTaskDocumentRef,
        contractPath=door.contractPath,
        candidateTree=door.candidateTree,
        sourceDoorFingerprint=_fingerprint(door.model_dump(mode="json")),
        classification=classification,
        reasons=reasons,
        priority=door.schedulingProvenance.priority,
        order=order,
    )


def _projection_blockers(context: ProjectionMemberContext) -> list[str]:
    door = context.door
    identity = (
        door.taskDocumentRef,
        door.owningMasterTaskDocumentRef,
        door.contractPath,
    ) != (
        context.candidate.ref,
        context.master.ref,
        context.source_address.as_posix(),
    )
    stale = door.taskTopologyFingerprint != context.task_topology_fingerprint
    incomplete = bool(completion_blockers(context.candidate.document))
    derived = [
        reason
        for reason, applies in (
            ("door-canonical-identity-mismatch", identity),
            ("door-task-topology-stale", stale),
            ("leaf-task-incomplete", incomplete),
        )
        if applies
    ]
    return [*context.source_blockers, *derived]


def _admission_waiting_reasons(door: CloseoutDoorGeneration) -> list[str]:
    admission = door.admissionProvenance
    reasons: list[str] = []
    if not admission.resourceReady:
        reasons.append(f"resource-unavailable: {admission.resourceReason}")
    if not admission.admissionReady:
        reasons.append(f"admission-blocked: {admission.admissionReason}")
    return reasons


def _dependency_waiting_and_order(
    context: ProjectionMemberContext,
    order: int,
) -> tuple[list[str], int]:
    if context.graph is None:
        return [], order
    waiting = predecessor_waiting_reasons(
        context.graph,
        context.master.ref,
        context.candidate.ref,
    )
    node = candidate_node(context.graph, context.master.ref, context.candidate.ref)
    if node is not None:
        order = context.graph.node_order[node] * 1000 + order % 1000
    return waiting, order


def scheduling_source_fact(
    door: CloseoutDoorGeneration,
    candidate: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    authority: GradeAuthority,
) -> tuple[list[str], dict[str, object]]:
    """Return current scheduling blockers plus every external grade input fingerprint."""

    priority = authority.priorities.get(candidate.ref.key) or authority.priorities.get(
        master.ref.key
    )
    if priority is None:
        return ["door-scheduling-priority-missing"], {"state": "priority-missing"}
    judgment = authority.judgments.get(priority.judgment_id)
    if judgment is None:
        return ["door-scheduling-judgment-missing"], {
            "state": "judgment-missing",
            "priorityRow": priority.source_row,
        }
    asserted: dict[str, object] = {
        "priority": priority.priority,
        "judgmentId": priority.judgment_id,
    }
    for key in ("urgency", "risk"):
        if key in judgment.decision:
            asserted[key] = judgment.decision[key]
    try:
        grade, digest, facts = canonical_grade(
            asserted,
            authority=authority,
            candidate_ref=candidate.ref,
            owning_master=master.ref,
        )
    except CloseoutQueueError as exc:
        return ["door-scheduling-provenance-stale"], {
            "state": "invalid",
            "errorType": exc.status,
            "priorityRow": priority.source_row,
            "judgmentRow": judgment.source_row,
        }
    expected_evidence = [fact.model_dump(mode="json") for fact in facts]
    observed = door.schedulingProvenance
    fact = {
        "state": "current",
        "grade": grade.model_dump(mode="json"),
        "fingerprint": digest,
        "evidence": expected_evidence,
    }
    if (
        observed.priority != grade.priority
        or observed.judgmentId != grade.judgmentId
        or observed.fingerprint != digest
        or [fact.model_dump(mode="json") for fact in observed.evidence] != expected_evidence
    ):
        return ["door-scheduling-provenance-stale"], fact
    return [], fact


def candidate_task_topology_fingerprint(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
    *,
    graph: QueueGraphContext | None,
    schema_version: str = SEMANTIC_TOPOLOGY_SCHEMA,
) -> str:
    """Adapt queue graph context to the task-domain topology identity owner."""

    try:
        return task_semantic_topology_fingerprint(
            sprint,
            master,
            candidate,
            graph_index=graph.semantic_topology_index if graph is not None else None,
            schema_version=schema_version,
        )
    except SemanticTopologyError as exc:
        raise CloseoutQueueError(exc.status, exc.detail) from exc


def semantic_topology_projection(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
    *,
    graph: QueueGraphContext | None,
    schema_version: str = SEMANTIC_TOPOLOGY_SCHEMA,
) -> SemanticTopologyV2:
    """Return the task-domain projection through the queue's typed error surface."""

    try:
        return task_semantic_topology_projection(
            sprint,
            master,
            candidate,
            graph_index=graph.semantic_topology_index if graph is not None else None,
            schema_version=schema_version,
        )
    except SemanticTopologyError as exc:
        raise CloseoutQueueError(exc.status, exc.detail) from exc


def _bounded_reasons(reasons: list[str]) -> list[str]:
    unique = list(dict.fromkeys(reason.strip() for reason in reasons if reason.strip()))
    if len(unique) <= MAX_CLOSEOUT_REASONS:
        return unique
    return [*unique[: MAX_CLOSEOUT_REASONS - 1], "additional-readiness-reasons-omitted"]


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "ProjectionMemberContext",
    "SemanticTopologyV2",
    "candidate_task_topology_fingerprint",
    "projection_member",
    "scheduling_source_fact",
    "semantic_topology_projection",
]
