"""Canonical semantic topology identity for one closeout candidate."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agents_remember.errors import AgentsRememberError
from agents_remember.models.task_document import MasterExecutionNature
from agents_remember.models.task_document_ref import TaskDocumentRef

from .document import SubTaskRef
from .document_field_effects import (
    TaskDocumentFieldEffect,
    TaskDocumentFieldEffectProjector,
    TaskDocumentFieldEffectTaxonomyError,
)
from .document_refs import ResolvedTaskDocument
from .leaf_binding import CanonicalLeafBindingError, require_canonical_leaf_binding
from .semantic_topology_graph import (
    SemanticTopologyCandidateWork,
    SemanticTopologyGraphIndex,
    SemanticTopologyGraphIndexError,
)

SEMANTIC_TOPOLOGY_SCHEMA = "semantic-topology/v2"

_PARENT_BINDING_STATUSES = {
    "task-leaf-binding-parent-invalid": "semantic-topology-parent-row-invalid",
    "task-leaf-binding-child-invalid": "semantic-topology-parent-row-invalid",
    "task-leaf-binding-row-invalid": "semantic-topology-parent-row-invalid",
    "task-leaf-binding-row-missing": "semantic-topology-parent-row-missing",
    "task-leaf-binding-row-ambiguous": "semantic-topology-parent-row-ambiguous",
    "task-leaf-binding-identity-split": "semantic-topology-parent-binding-split",
    "task-leaf-binding-stem-only": "semantic-topology-parent-binding-stem-only",
    "task-leaf-binding-wrong-directory": "semantic-topology-parent-binding-wrong-directory",
    "task-leaf-binding-source-mismatch": "semantic-topology-parent-binding-source-mismatch",
}


class SemanticTopologyError(AgentsRememberError):
    """Semantic topology cannot be derived from the authoritative task schema."""

    def __init__(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")


class _StrictProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticTopologyRef(_StrictProjection):
    repository: str
    path: str


class SemanticTopologyParentRow(_StrictProjection):
    number: str
    file: str
    masterRef: SemanticTopologyRef | None
    scope: str


class SemanticTopologyNode(_StrictProjection):
    kind: Literal["master", "segment"]
    ref: SemanticTopologyRef
    leafIds: tuple[str, ...]

    @field_validator("leafIds")
    @classmethod
    def _canonical_leaf_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(value))


class SemanticTopologyEndpoint(_StrictProjection):
    ref: SemanticTopologyRef
    leafId: str | None


SemanticTopologyEdgeEndpoint = SemanticTopologyRef | SemanticTopologyEndpoint


class SemanticTopologyEdge(_StrictProjection):
    predecessor: SemanticTopologyEdgeEndpoint
    successor: SemanticTopologyEdgeEndpoint


class SemanticTopologyDagPlacement(_StrictProjection):
    mode: Literal["dag"] = "dag"
    nodeOrder: int = Field(ge=0)
    node: SemanticTopologyNode
    relevantEdges: tuple[SemanticTopologyEdge, ...]


class SemanticTopologyAtomicPlacement(_StrictProjection):
    mode: Literal["atomic-sequential"] = "atomic-sequential"


SemanticTopologyPlacement = Annotated[
    SemanticTopologyDagPlacement | SemanticTopologyAtomicPlacement,
    Field(discriminator="mode"),
]


class SemanticTopologyV2(_StrictProjection):
    """Versioned candidate-local scheduling facts, excluding all delivery state."""

    schema_: Literal["semantic-topology/v2"] = Field(
        default=SEMANTIC_TOPOLOGY_SCHEMA,
        alias="schema",
    )
    sprint: SemanticTopologyRef
    master: SemanticTopologyRef
    leaf: SemanticTopologyRef
    parentRow: SemanticTopologyParentRow
    masterExecutionNature: MasterExecutionNature
    placement: SemanticTopologyPlacement

    def canonical_value(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=False)


def semantic_topology_projection(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
    *,
    graph_index: SemanticTopologyGraphIndex | None,
    schema_version: str = SEMANTIC_TOPOLOGY_SCHEMA,
) -> SemanticTopologyV2:
    """Select canonical candidate-applicable topology or refuse incomplete authority."""

    projection, _ = semantic_topology_projection_with_work(
        sprint,
        master,
        candidate,
        graph_index=graph_index,
        schema_version=schema_version,
    )
    return projection


def semantic_topology_projection_with_work(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
    *,
    graph_index: SemanticTopologyGraphIndex | None,
    schema_version: str = SEMANTIC_TOPOLOGY_SCHEMA,
) -> tuple[SemanticTopologyV2, SemanticTopologyCandidateWork | None]:
    """Return canonical topology plus exact candidate index work for scaling proof."""

    _require_schema_version(schema_version)
    projector = _validate_document_taxonomy(sprint, master, candidate)
    row = _owning_parent_row(master, candidate)
    authored_graph = sprint.document.executionGraph
    work: SemanticTopologyCandidateWork | None = None
    if authored_graph is None:
        if graph_index is not None:
            raise SemanticTopologyError(
                "semantic-topology-graph-context-mismatch",
                "graph-less atomic-sequential scheduling received a DAG context",
            )
        nature: MasterExecutionNature = "atomic"
        placement: SemanticTopologyPlacement = SemanticTopologyAtomicPlacement()
    else:
        if graph_index is None:
            raise SemanticTopologyError(
                "semantic-topology-dag-placement-missing",
                "an authored execution graph requires exact candidate placement",
            )
        if not graph_index.is_bound_to(authored_graph):
            raise SemanticTopologyError(
                "semantic-topology-graph-context-mismatch",
                "the supplied graph differs from the sprint's authored graph",
            )
        nature = _dag_execution_nature(master)
        placement, work = _dag_placement(graph_index, master.ref, row)
    projection = SemanticTopologyV2(
        sprint=_structural_projection(SemanticTopologyRef, sprint.ref, projector),
        master=_structural_projection(SemanticTopologyRef, master.ref, projector),
        leaf=_structural_projection(SemanticTopologyRef, candidate.ref, projector),
        parentRow=_structural_projection(SemanticTopologyParentRow, row, projector),
        masterExecutionNature=nature,
        placement=placement,
    )
    return projection, work


def semantic_topology_fingerprint(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
    *,
    graph_index: SemanticTopologyGraphIndex | None,
    schema_version: str = SEMANTIC_TOPOLOGY_SCHEMA,
) -> str:
    """Hash the canonical semantic-topology/v2 serialization."""

    fingerprint, _ = semantic_topology_fingerprint_with_work(
        sprint,
        master,
        candidate,
        graph_index=graph_index,
        schema_version=schema_version,
    )
    return fingerprint


def semantic_topology_fingerprint_with_work(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
    *,
    graph_index: SemanticTopologyGraphIndex | None,
    schema_version: str = SEMANTIC_TOPOLOGY_SCHEMA,
) -> tuple[str, SemanticTopologyCandidateWork | None]:
    """Hash canonical topology and expose exact index work from the same operation."""

    projection, work = semantic_topology_projection_with_work(
        sprint,
        master,
        candidate,
        graph_index=graph_index,
        schema_version=schema_version,
    )
    encoded = json.dumps(
        projection.canonical_value(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), work


def _require_schema_version(schema_version: str) -> None:
    if schema_version != SEMANTIC_TOPOLOGY_SCHEMA:
        raise SemanticTopologyError(
            "semantic-topology-schema-unsupported",
            f"unsupported semantic topology schema {schema_version!r}",
        )


def _validate_document_taxonomy(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
) -> TaskDocumentFieldEffectProjector:
    try:
        projector = TaskDocumentFieldEffectProjector()
        for document in (sprint.document, master.document, candidate.document):
            projector.validate(document)
    except TaskDocumentFieldEffectTaxonomyError as exc:
        raise SemanticTopologyError("semantic-topology-schema-unclassified", str(exc)) from exc
    return projector


def _owning_parent_row(
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
) -> SubTaskRef:
    try:
        binding = require_canonical_leaf_binding(
            master.ref,
            master.document,
            candidate.ref,
            candidate.document,
        )
    except CanonicalLeafBindingError as exc:
        try:
            status = _PARENT_BINDING_STATUSES[exc.status]
        except KeyError as unsupported:
            raise SemanticTopologyError(
                "semantic-topology-schema-unclassified",
                f"unsupported canonical leaf-binding status {exc.status!r}",
            ) from unsupported
        raise SemanticTopologyError(
            status,
            exc.detail,
        ) from exc
    return binding.row


def _dag_execution_nature(master: ResolvedTaskDocument) -> MasterExecutionNature:
    nature = master.document.executionNature
    if nature is None:
        raise SemanticTopologyError(
            "semantic-topology-execution-nature-missing",
            "an authored execution graph requires the master's declared execution nature",
        )
    return nature


def _dag_placement(
    graph_index: SemanticTopologyGraphIndex,
    master_ref: TaskDocumentRef,
    row: SubTaskRef,
) -> tuple[SemanticTopologyDagPlacement, SemanticTopologyCandidateWork]:
    try:
        candidate_slice = graph_index.candidate_slice(master_ref, row.number)
        node = SemanticTopologyNode.model_validate_json(candidate_slice.nodeProjection)
        edges = sorted(
            (
                SemanticTopologyEdge.model_validate_json(value)
                for value in candidate_slice.relevantEdgeProjections
            ),
            key=_edge_sort_key,
        )
    except SemanticTopologyGraphIndexError as exc:
        raise SemanticTopologyError(exc.status, exc.detail) from exc
    except ValidationError as exc:
        raise SemanticTopologyError(
            "semantic-topology-schema-unclassified",
            "indexed structural graph projection is not fully consumed",
        ) from exc
    return (
        SemanticTopologyDagPlacement(
            nodeOrder=candidate_slice.nodeOrder,
            node=node,
            relevantEdges=tuple(edges),
        ),
        candidate_slice.work,
    )


def _edge_sort_key(edge: SemanticTopologyEdge) -> str:
    return edge.model_dump_json(by_alias=True, exclude_none=False)


def _structural_projection[ProjectionModel: BaseModel](
    target: type[ProjectionModel],
    source: BaseModel,
    projector: TaskDocumentFieldEffectProjector,
) -> ProjectionModel:
    try:
        value = projector.project(
            source,
            TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY,
        )
        return target.model_validate(value)
    except TaskDocumentFieldEffectTaxonomyError as exc:
        raise SemanticTopologyError("semantic-topology-schema-unclassified", str(exc)) from exc
    except ValidationError as exc:
        raise SemanticTopologyError(
            "semantic-topology-schema-unclassified",
            f"structural projection for {type(source).__name__} is not fully consumed",
        ) from exc


__all__ = [
    "SEMANTIC_TOPOLOGY_SCHEMA",
    "SemanticTopologyAtomicPlacement",
    "SemanticTopologyDagPlacement",
    "SemanticTopologyError",
    "SemanticTopologyV2",
    "semantic_topology_fingerprint",
    "semantic_topology_fingerprint_with_work",
    "semantic_topology_projection",
    "semantic_topology_projection_with_work",
]
