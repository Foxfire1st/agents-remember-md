"""Explicit classified task inputs for closeout projection currentness."""

from __future__ import annotations

from agents_remember.errors import AgentsRememberError
from agents_remember.tasks.document_field_effects import (
    TaskDocumentFieldEffect,
    TaskDocumentFieldEffectTaxonomyError,
    project_model_field_effect,
)
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.semantic_topology import SEMANTIC_TOPOLOGY_SCHEMA

from .closeout_projection_members import candidate_task_topology_fingerprint
from .closeout_queue_graph import QueueGraphContext


class TaskSourceProjectionError(AgentsRememberError):
    """A task cannot enter projection currentness without classified fields."""

    def __init__(self, address: str, detail: str) -> None:
        self.address = address
        self.detail = detail
        super().__init__(detail)


def task_source_fact(resolved: ResolvedTaskDocument) -> dict[str, object]:
    """Return address plus readiness fields; never serialize the complete document."""

    try:
        readiness = project_model_field_effect(
            resolved.document,
            TaskDocumentFieldEffect.COMPLETION_READINESS,
        )
    except TaskDocumentFieldEffectTaxonomyError as exc:
        raise TaskSourceProjectionError(resolved.ref.key, str(exc)) from exc
    return {
        "address": resolved.ref.model_dump(mode="json"),
        "state": "present",
        "completionReadiness": readiness,
    }


def semantic_topology_source_fact(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
    graph: QueueGraphContext | None,
) -> tuple[str, dict[str, str]]:
    """Return one candidate's task-domain v2 identity as an explicit source plane."""

    fingerprint = candidate_task_topology_fingerprint(
        sprint,
        master,
        candidate,
        graph=graph,
    )
    return fingerprint, {
        "schema": SEMANTIC_TOPOLOGY_SCHEMA,
        "fingerprint": fingerprint,
    }


__all__ = [
    "TaskSourceProjectionError",
    "semantic_topology_source_fact",
    "task_source_fact",
]
