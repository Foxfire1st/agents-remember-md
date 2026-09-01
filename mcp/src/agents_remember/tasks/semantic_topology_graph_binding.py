"""Deep-immutable runtime binding for one validated semantic-topology graph."""

from __future__ import annotations

from collections.abc import Callable
from typing import Self, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from agents_remember.models.task_document_ref import TaskDocumentRef

from .document import (
    SprintExecutionEdge,
    SprintExecutionEndpoint,
    SprintExecutionGraph,
    SprintExecutionNode,
)

_IMMUTABLE_MODEL_CONFIG = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)
_DEFERRED_ENDPOINT_ERROR_PREFIXES = (
    "execution-graph endpoint leaf ",
    "execution-graph edge endpoint ",
)


class _ImmutableSprintExecutionEndpoint(SprintExecutionEndpoint):
    """An endpoint whose canonical ref and optional leaf sample cannot change."""

    model_config = _IMMUTABLE_MODEL_CONFIG


class _ImmutableSprintExecutionNode(SprintExecutionNode):
    """A node with an immutable leaf-placement collection."""

    model_config = _IMMUTABLE_MODEL_CONFIG

    leafIds: tuple[str, ...] = ()  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("leafIds")
    @classmethod
    def _trim_leaf_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        trimmed = tuple(leaf.strip() for leaf in value)
        if any(not leaf for leaf in trimmed):
            raise ValueError("execution-graph node leafIds must not be blank")
        return trimmed


class _ImmutableSprintExecutionEdge(SprintExecutionEdge):
    """An edge whose endpoint models and scalar facts cannot change."""

    model_config = _IMMUTABLE_MODEL_CONFIG

    predecessor: (  # pyright: ignore[reportIncompatibleVariableOverride]
        TaskDocumentRef | _ImmutableSprintExecutionEndpoint
    )
    successor: (  # pyright: ignore[reportIncompatibleVariableOverride]
        TaskDocumentRef | _ImmutableSprintExecutionEndpoint
    )


class _ImmutableSprintExecutionGraph(SprintExecutionGraph):
    """A validated graph with recursively immutable nodes, edges, and collections."""

    model_config = _IMMUTABLE_MODEL_CONFIG

    nodes: tuple[_ImmutableSprintExecutionNode, ...] = Field(  # pyright: ignore[reportIncompatibleVariableOverride]
        min_length=1
    )
    edges: tuple[_ImmutableSprintExecutionEdge, ...] = ()  # pyright: ignore[reportIncompatibleVariableOverride]

    @model_validator(mode="after")
    def _check_graph_shape(self) -> Self:
        """Run canonical graph admission, retaining the established endpoint boundary.

        Missing or ambiguous endpoints remain a typed candidate-projection refusal. Every
        other whole-graph invariant is admitted here and cannot be bypassed by construction.
        """

        try:
            canonical_admission = cast(
                Callable[[], SprintExecutionGraph],
                super()._check_graph_shape,
            )
            canonical_admission()
        except ValueError as exc:
            if not str(exc).startswith(_DEFERRED_ENDPOINT_ERROR_PREFIXES):
                raise
        return self


def immutable_semantic_topology_graph(canonical_graph: bytes) -> SprintExecutionGraph:
    """Materialize canonical graph bytes as the sole immutable population context.

    Persisted task documents retain the ordinary authoring models. Only the already
    validated semantic-topology context receives this frozen representation, whose
    inherited serializers preserve the persisted JSON shape.
    """

    return _ImmutableSprintExecutionGraph.model_validate_json(canonical_graph)


__all__ = ["immutable_semantic_topology_graph"]
