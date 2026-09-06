"""Focused proof for candidate-local closeout readiness helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks.document import (
    SprintExecutionEdge,
    SprintExecutionEndpoint,
    SprintExecutionGraph,
    SprintExecutionNode,
    TaskDocument,
)
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.semantic_topology_graph import (
    SemanticTopologyGraphIndex,
    build_semantic_topology_graph_index,
)


def _ref(path: str) -> TaskDocumentRef:
    return TaskDocumentRef(repository="agents-remember", path=path)


SPRINT = _ref("sprint/task.json")
PREDECESSOR = _ref("predecessor/task.json")
MASTER = _ref("master/task.json")
SUCCESSOR = _ref("successor/task.json")
LEAF = _ref("master/leaf.json")
NOW = "2026-08-31T00:00:00+00:00"


def _graph(*, leaf_ids: list[str] | None = None) -> SprintExecutionGraph:
    candidate_leaf_ids = leaf_ids or ["L01", "L02"]
    return SprintExecutionGraph(
        nodes=[
            SprintExecutionNode(ref=PREDECESSOR),
            SprintExecutionNode(kind="segment", ref=MASTER, leafIds=candidate_leaf_ids),
            SprintExecutionNode(ref=SUCCESSOR),
        ],
        edges=[
            SprintExecutionEdge(
                predecessor=PREDECESSOR,
                successor=SprintExecutionEndpoint(ref=MASTER, leafId="L01"),
                reason="Predecessor supplies the candidate.",
                judgmentId="J-1",
            ),
            SprintExecutionEdge(
                predecessor=SprintExecutionEndpoint(ref=MASTER, leafId="L02"),
                successor=SUCCESSOR,
                reason="Candidate supplies the successor.",
                judgmentId="J-2",
            ),
        ],
    )


def _candidate_document() -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": "L01",
            "slug": "leaf",
            "title": "Stable topology",
            "kind": "subTask",
            "status": "inProgress",
            "statusNote": "Building",
            "repo": "agents-remember",
            "type": "Implementation",
            "createdAt": NOW,
            "headerNotes": [{"label": "Owner", "value": "worker"}],
            "enclosures": [{"leafId": "L01", "enclosurePath": "/tmp/contract.md"}],
            "executionRegistrations": [
                {
                    "sourceKind": "terminal-catalog-seat",
                    "role": "worker",
                    "sourceId": "seat-1",
                    "observedAt": NOW,
                }
            ],
            "lifecycleId": "lifecycle-1",
            "objective": "Replace the topology identity.",
            "requirements": ["R01"],
            "design": "Typed projection.",
            "steps": [
                {
                    "id": "S1",
                    "title": "Build",
                    "outcome": "Built",
                    "status": "done",
                    "substeps": [
                        {
                            "id": "S1.1",
                            "title": "Proof",
                            "status": "done",
                            "disposition": {
                                "reason": "Covered elsewhere.",
                                "recordedAt": NOW,
                                "lifecycleId": "lifecycle-1",
                            },
                        }
                    ],
                    "disposition": {
                        "reason": "Covered elsewhere.",
                        "recordedAt": NOW,
                        "lifecycleId": "lifecycle-1",
                    },
                }
            ],
            "codeExamples": [
                {
                    "id": "E1",
                    "title": "Projection",
                    "distinctChange": "Typed",
                    "why": "Stable",
                    "language": "python",
                    "snippet": "projection()",
                }
            ],
            "decisions": [{"at": NOW, "decision": "Use v2", "rationale": "Stable"}],
            "routeReview": {
                "candidateTree": "a" * 40,
                "verdict": "pass",
                "verdictRef": "notes/review.md",
                "reviewedAt": NOW,
                "routes": [
                    {
                        "route": "topology",
                        "verdict": "pass",
                        "evidenceRef": "notes/review.md",
                    }
                ],
            },
            "openQuestions": ["None"],
            "references": ["requirements/R01.md"],
            "sections": [{"heading": "Notes", "body": "Audit prose."}],
        }
    )


def _documents(
    graph: SprintExecutionGraph | None = None,
) -> tuple[ResolvedTaskDocument, ResolvedTaskDocument, ResolvedTaskDocument]:
    execution_graph = graph or _graph()
    sprint_doc = TaskDocument.model_validate(
        {
            "id": "SPRINT",
            "slug": "sprint",
            "title": "Sprint",
            "kind": "master",
            "status": "inProgress",
            "repo": "agents-remember",
            "createdAt": NOW,
            "orchestrates": ["predecessor", "master", "successor"],
            "subTasks": [
                {"number": "M1", "name": "Predecessor", "masterRef": PREDECESSOR},
                {"number": "M2", "name": "Master", "masterRef": MASTER},
                {"number": "M3", "name": "Successor", "masterRef": SUCCESSOR},
            ],
            "executionGraph": execution_graph,
            "seats": [{"role": "architect", "label": "Planning", "state": "active"}],
        }
    )
    master_doc = TaskDocument.model_validate(
        {
            "id": "MASTER",
            "slug": "master",
            "title": "Master",
            "kind": "master",
            "status": "inProgress",
            "repo": "agents-remember",
            "createdAt": NOW,
            "executionNature": "organizational",
            "subTasks": [
                {
                    "number": "L01",
                    "name": "Stable topology",
                    "file": "leaf.md",
                    "status": "inProgress",
                    "scope": "Semantic topology",
                },
                {
                    "number": "L02",
                    "name": "Other leaf",
                    "file": "other.md",
                    "status": "planning",
                },
            ],
            "discardedSubTasks": [
                {
                    "number": "L00",
                    "name": "Discarded leaf",
                    "file": "discarded.md",
                    "scope": "Historical audit",
                    "reason": "Never started.",
                    "discardedAt": NOW,
                    "proof": {
                        "taskDocumentRef": {
                            "repository": "agents-remember",
                            "path": "master/discarded.json",
                        },
                        "taskState": "planning-unstarted",
                        "enclosureState": "absent",
                        "locatorState": "absent",
                        "doorState": "absent",
                        "operationState": "absent",
                        "seatState": "absent",
                        "reviewState": "absent",
                        "commitState": "absent",
                        "childJson": {"state": "missing"},
                        "childMarkdown": {"state": "missing"},
                        "fingerprint": "b" * 64,
                    },
                }
            ],
        }
    )
    return (
        ResolvedTaskDocument(ref=SPRINT, path=Path(SPRINT.path), document=sprint_doc),
        ResolvedTaskDocument(ref=MASTER, path=Path(MASTER.path), document=master_doc),
        ResolvedTaskDocument(ref=LEAF, path=Path(LEAF.path), document=_candidate_document()),
    )


def _semantic_index(
    graph: SprintExecutionGraph,
    master: ResolvedTaskDocument,
) -> SemanticTopologyGraphIndex:
    candidate_leaf_ids = {
        master.ref: tuple(row.number for row in master.document.subTasks if row.file)
    }
    return build_semantic_topology_graph_index(
        graph,
        candidate_leaf_ids,
        authored_graph=graph,
    )


def _bound_sprint(
    sprint: ResolvedTaskDocument,
    index: SemanticTopologyGraphIndex,
) -> ResolvedTaskDocument:
    return replace(
        sprint,
        document=sprint.document.model_copy(update={"executionGraph": index.boundGraph}),
    )
