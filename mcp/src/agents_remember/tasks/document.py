"""The ``ar-task-document/v1`` schema: the JSON-primary task document.

This is the persisted source of truth for a task's plan and progress. ``task.md``
(or ``<slug>.md`` for a sub-task) is a deterministic *render* of it (see
``render.py``); the JSON is never produced by parsing markdown back. It is the
peer of ``observer.projection`` -- a persisted/served Pydantic contract, **not**
an MCP response model.

Scope (slice 3c): ``light`` standalone tasks and ``subTask`` slices of a series
(the lifecycle-keyed work-content documents), plus the series ``master`` itself
(commit 3). A master is the series-aggregation entity: a structured ``subTasks``
index (each slice a checkable entry) + an ordered ``sections`` render plan that
preserves bespoke prose sections verbatim. Masters carry no ``lifecycleId``, so
the observer never projects them as a lifecycle node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_serializer,
    model_validator,
)

from agents_remember.models.task_document import DocStatus, MasterExecutionNature, StepStatus
from agents_remember.models.task_document_ref import TaskDocumentRef

from .execution_graph_validation import (
    ExecutionGraphAnalysis,
    ExecutionGraphValidationError,
    ExecutionGraphValidationWork,
    as_execution_graph_edges,
    find_execution_graph_cycle,
    validate_execution_graph,
)

TASK_DOCUMENT_SCHEMA = "ar-task-document/v1"

DocKind = Literal["light", "subTask", "master"]
RouteReviewVerdict = Literal["pass", "pass-with-notes", "block"]

# The structural task altitude per role. Reviewer is deliberately polymorphic across the three
# review seams; SprintSeat admits it in addition to the ordinary sprint roles. ``document_refs``
# re-exports these constants for its altitude checks, so their canonical home is here.
SPRINT_ROLES = frozenset(
    {"architect", "orchestrator", "strategist", "designer", "system-specialist"}
)
MASTER_ROLES = frozenset({"manager"})
LEAF_ROLES = frozenset({"worker", "reviewer", "curator"})
REVIEWER_ALTITUDES = frozenset({"sprint", "master", "leaf"})


class _Doc(BaseModel):
    """Strict base: unknown keys are a schema error; field name or alias accepts."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StepDisposition(_Doc):
    """Durable evidence that one exact work unit was intentionally skipped."""

    kind: Literal["intentionalSkip"] = "intentionalSkip"
    reason: str
    recordedAt: str
    recordedVia: Literal["task_doc.skip_step"] = "task_doc.skip_step"
    lifecycleId: str | None = None

    @field_validator("reason")
    @classmethod
    def _trim_nonblank_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("intentional skip reason must not be blank")
        return trimmed


class SubStep(_Doc):
    id: str
    title: str
    status: StepStatus = "pending"
    note: str | None = None
    disposition: StepDisposition | None = None

    @model_validator(mode="after")
    def _check_disposition_is_terminal(self) -> Self:
        if self.disposition is not None and self.status != "done":
            raise ValueError("a substep with intentional-skip disposition must have status done")
        return self


class Step(_Doc):
    id: str
    title: str
    # The checkbox line (the deliverable), distinct from the heading ``title`` (R2). Optional and
    # ``None``-defaulted so ``exclude_none`` keeps existing step JSON byte-identical.
    outcome: str | None = None
    status: StepStatus = "pending"
    substeps: list[SubStep] = Field(default_factory=list)
    disposition: StepDisposition | None = None

    @model_validator(mode="after")
    def _check_disposition_is_terminal(self) -> Self:
        if self.disposition is not None and self.status != "done":
            raise ValueError("a step with intentional-skip disposition must have status done")
        return self


class Decision(_Doc):
    at: str
    decision: str
    rationale: str


class RouteReviewUnit(_Doc):
    """One independently reviewed major route in the candidate code tree."""

    route: str
    verdict: RouteReviewVerdict
    evidenceRef: str

    @field_validator("route", "evidenceRef")
    @classmethod
    def _trim_nonblank_route_review_value(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("route-review route and evidenceRef must not be blank")
        return trimmed


class RouteReviewRecord(_Doc):
    """Plane-stamped review evidence bound to one exact Git candidate tree."""

    candidateTree: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    verdict: RouteReviewVerdict
    verdictRef: str
    reviewedAt: str
    routes: list[RouteReviewUnit] = Field(min_length=1)

    @field_validator("verdictRef", "reviewedAt")
    @classmethod
    def _trim_nonblank_route_review_metadata(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("route-review verdictRef and reviewedAt must not be blank")
        return trimmed

    @model_validator(mode="after")
    def _check_route_review_coherence(self) -> Self:
        names = [route.route for route in self.routes]
        if len(names) != len(set(names)):
            raise ValueError("route-review routes must be unique")
        blocked = any(route.verdict == "block" for route in self.routes)
        if self.verdict == "block" and not blocked:
            raise ValueError("a blocking route-review verdict requires at least one blocked route")
        if self.verdict != "block" and blocked:
            raise ValueError("a passing route-review verdict cannot contain a blocked route")
        return self


class CodeExample(_Doc):
    id: str
    title: str
    distinctChange: str
    why: str
    language: str = ""
    snippet: str = ""


class HeaderNote(_Doc):
    """An extra header metadata line, rendered as ``**{label}:** {value}`` (R4)."""

    label: str
    value: str = ""


class TaskEnclosureRef(_Doc):
    leafId: str
    enclosurePath: str


class TaskExecutionRegistration(_Doc):
    """Bounded monotonic proof that one leaf acquired typed execution authority."""

    sourceKind: Literal["terminal-catalog-seat", "operator-inbox-turn-report"]
    role: Literal["worker", "reviewer", "curator"]
    sourceId: str
    observedAt: str

    @field_validator("sourceId", "observedAt")
    @classmethod
    def _trim_registration_identity(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("task execution registration identity must not be blank")
        return trimmed


class SprintExecutionEndpoint(_Doc):
    """One edge endpoint: a bare master ref, or a leaf id sampling the target segment.

    A bare ``ref`` addresses the master's only node (a lump, or its single segment);
    ``ref`` + ``leafId`` addresses the segment node containing that leaf. Resolution
    to a node happens in graph validation, never at parse time.
    """

    ref: TaskDocumentRef
    leafId: str | None = None

    @field_validator("leafId")
    @classmethod
    def _trim_nonblank_leaf_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("execution-graph endpoint leafId must not be blank")
        return trimmed


class SprintExecutionNode(_Doc):
    """One graph node: a whole-master lump or a leaf-segment of one master.

    Legacy graphs persist lump nodes as bare ``{"repository", "path"}`` refs. The
    before-validator lifts those into lump nodes and the serializer emits the bare
    shape back, so a lump-only graph parses and re-serializes byte-identically
    (L11-R7). Python equality remains structural node identity; callers that need
    owning-master identity compare ``node.ref`` explicitly.
    """

    kind: Literal["master", "segment"] = "master"
    ref: TaskDocumentRef
    leafIds: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_ref(cls, value: Any) -> Any:
        if isinstance(value, TaskDocumentRef):
            return {"ref": value}
        if isinstance(value, dict) and "ref" not in value and "repository" in value:
            return {"ref": value}
        return value

    @field_validator("leafIds")
    @classmethod
    def _trim_leaf_ids(cls, value: list[str]) -> list[str]:
        trimmed = [leaf.strip() for leaf in value]
        if any(not leaf for leaf in trimmed):
            raise ValueError("execution-graph node leafIds must not be blank")
        return trimmed

    @model_validator(mode="after")
    def _check_kind_leafs(self) -> Self:
        if self.kind == "segment":
            if not self.leafIds:
                raise ValueError("execution-graph segment node requires a non-empty leafIds")
            if len(set(self.leafIds)) != len(self.leafIds):
                raise ValueError("execution-graph segment node leafIds must be unique")
        elif self.leafIds:
            raise ValueError("execution-graph lump node has no leafIds")
        return self

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SprintExecutionNode):
            return NotImplemented
        return self.kind == other.kind and self.ref == other.ref and self.leafIds == other.leafIds

    def __hash__(self) -> int:
        return hash((self.kind, self.ref, tuple(self.leafIds)))

    @model_serializer
    def _serialize(self) -> dict[str, Any]:
        ref = {"repository": self.ref.repository, "path": self.ref.path}
        if self.kind == "master":
            return ref
        return {"kind": "segment", "ref": ref, "leafIds": list(self.leafIds)}


class SprintExecutionEdge(_Doc):
    """One reasoned predecessor edge in the sprint's activity-on-node graph."""

    predecessor: TaskDocumentRef | SprintExecutionEndpoint
    successor: TaskDocumentRef | SprintExecutionEndpoint
    reason: str
    judgmentId: str | None = None

    @field_validator("reason")
    @classmethod
    def _trim_nonblank_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("execution-graph edge reason must not be blank")
        return trimmed

    @field_validator("judgmentId")
    @classmethod
    def _trim_nonblank_judgment_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("execution-graph edge judgmentId must not be blank")
        return trimmed

    @model_validator(mode="after")
    def _check_distinct_endpoints(self) -> Self:
        if self.predecessor == self.successor:
            raise ValueError("execution-graph edge cannot point a node to itself")
        return self


def resolve_graph_endpoint(
    nodes: list[SprintExecutionNode],
    endpoint: TaskDocumentRef | SprintExecutionEndpoint,
) -> SprintExecutionNode:
    """Resolve one edge endpoint to exactly one declared node."""
    ref = endpoint.ref if isinstance(endpoint, SprintExecutionEndpoint) else endpoint
    leaf = endpoint.leafId if isinstance(endpoint, SprintExecutionEndpoint) else None
    matches = [node for node in nodes if node.ref == ref]
    if leaf is not None:
        matches = [node for node in matches if leaf in node.leafIds]
        if not matches:
            raise ValueError(
                f"execution-graph endpoint leaf {leaf!r} is not placed in any node of {ref.key}"
            )
    elif not matches:
        raise ValueError(f"execution-graph edge endpoint must be a declared node: {ref.key}")
    if len(matches) > 1:
        raise ValueError(
            f"execution-graph edge endpoint {ref.key} is ambiguous; "
            "name a leafId of the target segment"
        )
    return matches[0]


class SprintExecutionGraph(_Doc):
    """The persisted AON graph; positions and waves are always derived from it."""

    nodes: list[SprintExecutionNode] = Field(min_length=1)
    edges: list[SprintExecutionEdge] = Field(default_factory=list)
    _executionGraphValidationWork: ExecutionGraphValidationWork = PrivateAttr(
        default_factory=ExecutionGraphValidationWork
    )

    @model_validator(mode="after")
    def _check_graph_shape(self) -> Self:
        self._execution_graph_analysis()
        return self

    def _execution_graph_analysis(self) -> ExecutionGraphAnalysis[SprintExecutionNode]:
        """Run the canonical indexed analysis and retain its exact operation count."""

        try:
            analysis = validate_execution_graph(
                self.nodes,
                as_execution_graph_edges(self.edges),
            )
        except ExecutionGraphValidationError as exc:
            self._executionGraphValidationWork = exc.work
            raise ValueError(exc.detail) from exc
        self._executionGraphValidationWork = analysis.work
        return analysis

    @property
    def execution_graph_validation_work(self) -> ExecutionGraphValidationWork:
        """Exact operations from this instance's canonical whole-graph admission."""

        return self._executionGraphValidationWork

    def resolve_endpoint(
        self, endpoint: TaskDocumentRef | SprintExecutionEndpoint
    ) -> SprintExecutionNode:
        """Resolve one edge endpoint to exactly one declared node."""
        return resolve_graph_endpoint(self.nodes, endpoint)

    def master_refs(self) -> list[TaskDocumentRef]:
        """The distinct masters this graph places, in node declaration order."""
        return list(dict.fromkeys(node.ref for node in self.nodes))

    def nodes_for(self, ref: TaskDocumentRef) -> list[SprintExecutionNode]:
        """Every node (lump or segments) placing one master, in declaration order."""
        return [node for node in self.nodes if node.ref == ref]

    def derived_waves(self) -> list[list[SprintExecutionNode]]:
        """Return deterministic topological waves over nodes, refusing a directed cycle."""

        analysis = self._execution_graph_analysis()
        return [list(wave) for wave in analysis.waves]


def _find_cycle_members(
    graph: SprintExecutionGraph, residual: list[SprintExecutionNode]
) -> list[SprintExecutionNode]:
    """Return the exact members of one directed cycle among the residual nodes.

    Kahn's algorithm leaves cycle members AND nodes downstream of a cycle in
    ``residual``; this extracts one cycle deterministically: DFS seeded from the
    residual node in declaration order, returning the stack slice between the
    repeated node.
    """

    try:
        return list(
            find_execution_graph_cycle(
                graph.nodes,
                as_execution_graph_edges(graph.edges),
                residual,
            )
        )
    except ExecutionGraphValidationError as exc:
        raise ValueError(exc.detail) from exc


@dataclass(frozen=True)
class LeafPlacement:
    """One master's authored and derived leaf-to-segment placement (L11-R2)."""

    placed: dict[str, SprintExecutionNode]
    unknown_leaf_ids: tuple[str, ...]
    unplaced_leaf_ids: tuple[str, ...]
    derived: dict[str, SprintExecutionNode]
    derived_all_blocked: bool


def derived_leaf_placement(
    graph: SprintExecutionGraph,
    master_ref: TaskDocumentRef,
    planned_leaf_ids: list[str],
    completed_refs: set[TaskDocumentRef],
) -> LeafPlacement:
    """Map one master's planned leafs to its segments, deriving unplaced placements.

    Pure: unplaced leafs (the master's leaf set grew after authoring) schedule as if
    appended to the master's latest unblocked segment -- latest by derived wave index,
    then node declaration order. When every segment is blocked the latest overall is
    used and ``derived_all_blocked`` flags the fallback. A lump master covers its leafs
    implicitly and reports no placement facts.
    """

    segments = [node for node in graph.nodes_for(master_ref) if node.kind == "segment"]
    if not segments:
        return LeafPlacement({}, (), (), {}, False)
    planned = list(dict.fromkeys(planned_leaf_ids))
    planned_set = set(planned)
    placed: dict[str, SprintExecutionNode] = {}
    for node in segments:
        for leaf in node.leafIds:
            placed[leaf] = node
    unknown = tuple(leaf for leaf in placed if leaf not in planned_set)
    unplaced = tuple(leaf for leaf in planned if leaf not in placed)
    derived: dict[str, SprintExecutionNode] = {}
    all_blocked = False
    if unplaced:
        target, all_blocked = _latest_unblocked_segment(graph, segments, completed_refs)
        derived = {leaf: target for leaf in unplaced}
    return LeafPlacement(placed, unknown, unplaced, derived, all_blocked)


def _latest_unblocked_segment(
    graph: SprintExecutionGraph,
    segments: list[SprintExecutionNode],
    completed_refs: set[TaskDocumentRef],
) -> tuple[SprintExecutionNode, bool]:
    analysis = graph._execution_graph_analysis()
    wave_of = {node: index for index, wave in enumerate(analysis.waves) for node in wave}
    declaration = {node: index for index, node in enumerate(graph.nodes)}
    predecessors: dict[SprintExecutionNode, list[SprintExecutionNode]] = {
        node: [] for node in graph.nodes
    }
    for predecessor_index, successor_index in analysis.resolvedEdgeIndexes:
        predecessors[graph.nodes[successor_index]].append(graph.nodes[predecessor_index])
    ordered = sorted(segments, key=lambda node: (wave_of[node], declaration[node]))
    for candidate in reversed(ordered):
        if all(predecessor.ref in completed_refs for predecessor in predecessors[candidate]):
            return candidate, False
    return ordered[-1], True


def _leaf_trailing_number(leaf_id: str) -> int | None:
    match = re.search(r"(\d+)$", leaf_id)
    return int(match.group(1)) if match else None


def leaf_placement_facts(master_key: str, placement: LeafPlacement) -> list[dict[str, Any]]:
    """Shape one master's placement drift as reported facts (never silent, L11-R2/R6)."""
    facts: list[dict[str, Any]] = [
        {"kind": "unknown-leaf", "master": master_key, "leafId": leaf}
        for leaf in placement.unknown_leaf_ids
    ]
    facts += [
        {
            "kind": "unplaced-leaf",
            "master": master_key,
            "leafId": leaf,
            "derivedSegmentLeafs": list(placement.derived[leaf].leafIds),
            "derivedAllSegmentsBlocked": placement.derived_all_blocked,
        }
        for leaf in placement.unplaced_leaf_ids
    ]
    return facts


def numbering_drift_hints(graph: SprintExecutionGraph) -> list[dict[str, Any]]:
    """Report leaf-numbering inversions across derived waves (L11-R8); never refuses.

    A hint fires when a lower-numbered leaf of one master sits in a strictly later
    derived wave than a higher-numbered leaf of the same master (e.g. L3 in wave 3
    while L4 is in wave 1). Leaf ids without a trailing number are not comparable
    and never hint.
    """

    wave_of = {node: index for index, wave in enumerate(graph.derived_waves()) for node in wave}
    hints: list[dict[str, Any]] = []
    for ref in graph.master_refs():
        numbered = [
            (leaf, wave_of[node], number)
            for node in graph.nodes_for(ref)
            if node.kind == "segment"
            for leaf in node.leafIds
            if (number := _leaf_trailing_number(leaf)) is not None
        ]
        for lower_leaf, lower_wave, lower_number in numbered:
            for higher_leaf, higher_wave, higher_number in numbered:
                if lower_number < higher_number and lower_wave > higher_wave:
                    hints.append(
                        {
                            "kind": "leaf-numbering-inversion",
                            "master": ref.key,
                            "lowerNumberLeafId": lower_leaf,
                            "lowerNumberWave": lower_wave + 1,
                            "higherNumberLeafId": higher_leaf,
                            "higherNumberWave": higher_wave + 1,
                        }
                    )
    return hints


class SubTaskRef(_Doc):
    """One slice in a master's series index; ``status`` drives the ✅/🔨/⬜ marker.

    On an orchestration sprint the row may carry a typed ``masterRef`` (L14-R1): the
    exact commanded master document it tracks, rendered as a real markdown link. Leaf
    rows on an ordinary master never set it; optional so legacy rows parse unchanged.
    """

    number: str
    name: str
    file: str = ""
    status: DocStatus = "planning"
    scope: str = ""
    masterRef: TaskDocumentRef | None = None


class DiscardSourceProof(_Doc):
    """Exact accepted state of one child source side at discard admission."""

    state: Literal["missing", "present"]
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _proof_matches_state(self) -> Self:
        carries_any_bytes = self.sha256 is not None or self.size is not None
        carries_complete_bytes = self.sha256 is not None and self.size is not None
        if (self.state == "present" and not carries_complete_bytes) or (
            self.state == "missing" and carries_any_bytes
        ):
            raise ValueError("discard source proof state must match complete digest and size")
        return self


class DiscardUnstartedProof(_Doc):
    """Compact typed proof retained after a planning leaf source is removed."""

    version: Literal["task-unstarted-evidence/v1"] = "task-unstarted-evidence/v1"
    taskDocumentRef: TaskDocumentRef
    taskState: Literal["planning-unstarted"] = "planning-unstarted"
    enclosureState: Literal["absent"] = "absent"
    locatorState: Literal["absent"] = "absent"
    doorState: Literal["absent"] = "absent"
    operationState: Literal["absent"] = "absent"
    seatState: Literal["absent"] = "absent"
    reviewState: Literal["absent"] = "absent"
    commitState: Literal["absent"] = "absent"
    childJson: DiscardSourceProof
    childMarkdown: DiscardSourceProof
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiscardedSubTask(_Doc):
    """Parent-owned audit for one leaf removed before execution began."""

    number: str
    name: str
    file: str
    scope: str = ""
    disposition: Literal["discard-unstarted"] = "discard-unstarted"
    reason: str
    discardedAt: str
    proof: DiscardUnstartedProof

    @field_validator("number", "name", "file", "reason", "discardedAt")
    @classmethod
    def _trim_required_discard_value(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("discard-unstarted audit identity, reason, and time must not be blank")
        return trimmed

    @field_validator("file")
    @classmethod
    def _require_direct_markdown_source(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or path.parent != PurePosixPath() or path.suffix != ".md":
            raise ValueError("discard-unstarted audit file must be one direct Markdown child")
        return value


SprintSeatState = Literal["planned", "active", "retired"]


class SprintSeat(_Doc):
    """One first-class seat of an orchestration sprint (L14-R3).

    Mirrors the manager-seat precedent on master docs: the sprint document owns the
    seat record, so seat task documents leave the sprint's task index (existing ones
    stay on disk as historical records). ``identity`` is a correlatable session or
    catalog id — provenance for correlation, never an authority source.
    """

    role: str
    label: str = ""
    identity: str | None = None
    state: SprintSeatState = "planned"

    @field_validator("role")
    @classmethod
    def _check_sprint_role(cls, value: str) -> str:
        role = value.strip()
        allowed = SPRINT_ROLES | {"reviewer"}
        if role not in allowed:
            raise ValueError(f"sprint seat role must be one of {sorted(allowed)}")
        return role

    @field_validator("identity")
    @classmethod
    def _trim_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("sprint seat identity must not be blank")
        return trimmed


class Section(_Doc):
    """One ordered section of a master render: freeform prose or a structured block.

    ``freeform`` renders the heading + ``body`` verbatim; ``subTasks`` and
    ``sharedDecisions`` render the generated block (the series list / the decisions
    table) with ``body`` as optional intro prose, at this position in the order.
    """

    kind: Literal["freeform", "subTasks", "sharedDecisions"] = "freeform"
    heading: str
    body: str = ""


class TaskDocument(_Doc):
    schema_: Literal["ar-task-document/v1"] = Field(default=TASK_DOCUMENT_SCHEMA, alias="schema")
    id: str
    slug: str
    title: str
    kind: DocKind
    status: DocStatus = "planning"
    # Descriptive status suffix appended after the strict enum, e.g.
    # "**Status:** inProgress -- <description>" (R4); the enum stays the dashboard lever.
    statusNote: str | None = None
    repo: str
    type: str = ""
    createdAt: str
    master: str | None = None
    # Extra "**Key:** value" header lines beyond the standard block (e.g. Verified/Source); R4.
    headerNotes: list[HeaderNote] = Field(default_factory=list)
    seriesContractPath: str | None = None
    # Sprint-owned branch identity.  A manager's master integration edge is based on this
    # declaration, never on whichever branch the repository checkout happens to have active.
    integrationBranch: str | None = None
    # Organizational containment and Git execution topology are distinct. Legacy documents may
    # omit these fields only so the explicit migration operation can read and upgrade them; no
    # consumer may infer a default.
    executionNature: MasterExecutionNature | None = None
    executionGraph: SprintExecutionGraph | None = None
    enclosures: list[TaskEnclosureRef] = Field(default_factory=list)
    # Typed seat/report stores are reclaimable. Before their last row is removed, one compact
    # first-observation marker is registered here so historical execution can never become
    # "never started". One marker per source kind and role keeps the task document bounded.
    executionRegistrations: list[TaskExecutionRegistration] = Field(default_factory=list)
    lifecycleId: str | None = None
    objective: str = ""
    requirements: list[str] = Field(default_factory=list)
    design: str | None = None
    steps: list[Step] = Field(default_factory=list)
    codeExamples: list[CodeExample] = Field(default_factory=list)
    # Why code examples are absent (e.g. "Drafted at the plan gate.") -- distinguishes a
    # planning slice that defers its examples from a task that genuinely needs none (R3).
    codeExamplesNote: str | None = None
    decisions: list[Decision] = Field(default_factory=list)
    routeReview: RouteReviewRecord | None = None
    openQuestions: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    # subTasks is the master series index (master-only). sections is the master's ordered
    # render plan AND, since R4, freeform extra sections on a leaf doc (appended after the template).
    subTasks: list[SubTaskRef] = Field(default_factory=list)
    # Planning removal is not completion. The parent retains this compact typed audit after the
    # live row and child JSON/Markdown disappear, which also makes a lost-response retry
    # idempotently observable without a queue tombstone or filesystem scan.
    discardedSubTasks: list[DiscardedSubTask] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    # The orchestration-command relation (260703-L14): a ``master`` doc that carries a non-empty
    # ``orchestrates`` list IS an orchestration task -- each entry names a master task it commands
    # (its task folder / doc id / title; the dashboard matches forgivingly). Additive: there is no
    # new task kind, docs without the field are untouched, and masters named nowhere stay top-level.
    orchestrates: list[str] = Field(default_factory=list)
    # The sprint's first-class seats (L14-R3): role + label + correlatable identity + state.
    # Legal only on an orchestration sprint; roles are unique among non-retired seats.
    seats: list[SprintSeat] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_kind_fields(self) -> Self:
        if self.kind == "master":
            self._check_master_fields()
        else:
            self._check_nonmaster_fields()
        self._check_execution_fields()
        self._check_sprint_rows_and_seats()
        self._check_discarded_subtasks()
        self._normalize_integration_branch()
        return self

    def _check_master_fields(self) -> None:
        if (
            self.steps
            or self.codeExamples
            or self.codeExamplesNote is not None
            or self.lifecycleId is not None
            or self.routeReview is not None
            or self.executionRegistrations
        ):
            raise ValueError(
                "a master document has no steps, codeExamples, codeExamplesNote, lifecycleId, "
                "routeReview, or executionRegistrations"
            )

    def _check_nonmaster_fields(self) -> None:
        master_only = {
            "subTasks": self.subTasks,
            "discardedSubTasks": self.discardedSubTasks,
            "orchestrates": self.orchestrates,
            "seats": self.seats,
        }
        present = next((name for name, value in master_only.items() if value), None)
        if present is not None:
            raise ValueError(f"a {self.kind} document has no {present} (master-only)")
        if any(section.kind != "freeform" for section in self.sections):
            raise ValueError(f"a {self.kind} document allows only freeform sections")
        if self.codeExamplesNote is not None and self.codeExamples:
            raise ValueError(
                "codeExamplesNote explains why code examples are absent; "
                "it cannot be set alongside codeExamples"
            )
        if self.executionNature is not None or self.executionGraph is not None:
            raise ValueError(
                f"a {self.kind} document has no executionNature or executionGraph (master-only)"
            )
        if self.kind != "subTask" and self.executionRegistrations:
            raise ValueError("only a subTask may carry leaf execution registrations")
        registration_keys = [(item.sourceKind, item.role) for item in self.executionRegistrations]
        if len(registration_keys) != len(set(registration_keys)):
            raise ValueError("task execution registrations must be unique by source kind and role")

    def _check_discarded_subtasks(self) -> None:
        discarded = [item.number for item in self.discardedSubTasks]
        if len(discarded) != len(set(discarded)):
            raise ValueError("discardedSubTasks numbers must be unique")
        live = {item.number for item in self.subTasks}
        overlap = sorted(live.intersection(discarded))
        if overlap:
            raise ValueError(
                "a discardedSubTasks identity cannot also remain live: " + ", ".join(overlap)
            )
        for item in self.discardedSubTasks:
            proof_ref = item.proof.taskDocumentRef
            expected_name = PurePosixPath(item.file).with_suffix(".json").name
            if (
                proof_ref.repository != self.repo
                or PurePosixPath(proof_ref.path).name != expected_name
            ):
                raise ValueError(
                    "discardedSubTasks proof identity must match the parent repository and file"
                )

    def _check_execution_fields(self) -> None:
        if self.kind != "master":
            return
        if self.orchestrates and self.executionNature is not None:
            raise ValueError("an orchestration sprint has no executionNature")
        if not self.orchestrates and self.executionGraph is not None:
            raise ValueError("executionGraph belongs only to an orchestration sprint")

    def _check_sprint_rows_and_seats(self) -> None:
        """Typed sprint linkage is sprint-only (L14): masterRef rows and seats require command."""
        if any(row.masterRef is not None for row in self.subTasks) and (
            self.kind != "master" or not self.orchestrates
        ):
            raise ValueError(
                "a subTasks row with masterRef belongs only to an orchestration sprint"
            )
        if not self.seats:
            return
        if self.kind != "master" or not self.orchestrates:
            raise ValueError("seats belong only to an orchestration sprint")
        live_roles = [seat.role for seat in self.seats if seat.state != "retired"]
        if len(set(live_roles)) != len(live_roles):
            raise ValueError("sprint seat roles must be unique among planned/active seats")

    def _normalize_integration_branch(self) -> None:
        if self.integrationBranch is None:
            return
        branch = self.integrationBranch.strip()
        if self.kind != "master":
            raise ValueError("integrationBranch is master-only")
        if not self.orchestrates:
            raise ValueError(
                "integrationBranch belongs only to an orchestration sprint with orchestrates"
            )
        if not branch:
            raise ValueError("integrationBranch must not be blank")
        self.integrationBranch = branch


def _declared_statuses(doc: TaskDocument) -> list[StepStatus]:
    """Every declared step and substep, matching terminal-readiness semantics."""
    statuses: list[StepStatus] = []
    for step in doc.steps:
        statuses.append(step.status)
        statuses.extend(sub.status for sub in step.substeps)
    return statuses


def step_total(doc: TaskDocument) -> int:
    return len(_declared_statuses(doc))


def step_done(doc: TaskDocument) -> int:
    return sum(1 for status in _declared_statuses(doc) if status == "done")


def current_step(doc: TaskDocument) -> str | None:
    """The active step for the dashboard: first in-progress/blocked, else first unfinished."""
    for step in doc.steps:
        if step.status in ("inProgress", "blocked"):
            return f"{step.id} — {step.title}"
        for sub in step.substeps:
            if sub.status in ("inProgress", "blocked"):
                return f"{step.id}/{sub.id} — {sub.title}"
    for step in doc.steps:
        if step.status != "done":
            return f"{step.id} — {step.title}"
        for sub in step.substeps:
            if sub.status != "done":
                return f"{step.id}/{sub.id} — {sub.title}"
    return None


def series_total(doc: TaskDocument) -> int:
    """A master's checkboxes are its subtasks: each ``SubTaskRef`` is one box."""
    return len(doc.subTasks)


def series_done(doc: TaskDocument) -> int:
    """Checked boxes = subtasks whose *declared* status is ``Completed``.

    The declared subtask status is the lever and is authoritative: a slice marked
    ``Completed`` in the master counts as done even if its own leaf doc still has open
    boxes. Never derived from a slice's internal steps.
    """
    return sum(1 for sub in doc.subTasks if sub.status == "Completed")
