"""Immutable evidence values for content-addressed memory dependency scope."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentState

Hash = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitChangeStatus = Literal["added", "modified", "deleted", "renamed"]
ScopeNamespace = Literal["code", "memory"]
EdgeClass = Literal[
    "source-to-file-sidecar",
    "source-to-governing-route",
    "source-to-citing-memory-document",
    "source-to-entity-manifestation",
    "route-index-dependency",
]

MANIFEST_SCHEMA = "memory-dependency-scope/v1"
SNAPSHOT_SCHEMA = "memory-dependency-snapshot/v1"


def canonical_digest(value: object) -> str:
    """Return SHA-256 over the repository's canonical JSON spelling."""

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GitPathChange(_StrictModel):
    """One exact tree-diff root, including both identities for a rename."""

    status: GitChangeStatus
    oldPath: str | None = None
    newPath: str | None = None
    oldBlob: Hash | None = None
    newBlob: Hash | None = None

    @field_validator("oldPath", "newPath")
    @classmethod
    def _canonical_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or value.startswith("/") or "\\" in value:
            raise ValueError("Git change paths must be canonical repository-relative POSIX paths")
        if any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("Git change paths cannot contain empty, dot, or traversal segments")
        return value

    @model_validator(mode="after")
    def _shape_matches_status(self) -> GitPathChange:
        expected = {
            "added": (False, True, False, True),
            "modified": (True, True, True, True),
            "deleted": (True, False, True, False),
            "renamed": (True, True, True, True),
        }[self.status]
        observed = tuple(
            value is not None for value in (self.oldPath, self.newPath, self.oldBlob, self.newBlob)
        )
        if observed != expected:
            raise ValueError(f"{self.status} Git change has an invalid old/new path or blob shape")
        if self.status == "modified" and self.oldPath != self.newPath:
            raise ValueError("modified Git changes must retain one path")
        if self.status == "renamed" and self.oldPath == self.newPath:
            raise ValueError("renamed Git changes must change path")
        return self


class GitTreeDelta(_StrictModel):
    """Exact Git-owned base/candidate tree difference for one repository root."""

    namespace: ScopeNamespace
    root: str
    baseTree: Hash
    candidateTree: Hash
    owner: Literal["git-tree-diff"] = "git-tree-diff"
    ownerVersion: Literal["git-diff-tree/v1"] = "git-diff-tree/v1"
    changes: tuple[GitPathChange, ...]

    @field_validator("root")
    @classmethod
    def _absolute_root(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise ValueError("Git delta root must be one normalized absolute POSIX path")
        return value

    @field_validator("changes")
    @classmethod
    def _canonical_changes(cls, value: tuple[GitPathChange, ...]) -> tuple[GitPathChange, ...]:
        def key(item: GitPathChange) -> tuple[str, str, str]:
            return item.oldPath or "", item.newPath or "", item.status

        if tuple(sorted(value, key=key)) != value:
            raise ValueError("Git changes must be deterministically sorted")
        if len(set(key(item) for item in value)) != len(value):
            raise ValueError("Git changes must be unique")
        return value


class CanonicalTaskObservation(_StrictModel):
    """Exact output from the task source, R01 topology, and R02 intent owners."""

    taskRoot: str
    taskDocumentRef: TaskDocumentRef
    sourceDigest: Sha256
    sourceAuthorityNamespace: Literal[
        "agents-remember.closeout-door-generation",
        "agents-remember.task-document-source",
    ]
    sourceValidatorVersion: str = Field(min_length=1)
    semanticTopologySchema: Literal["semantic-topology/v2"] = "semantic-topology/v2"
    semanticTopologyDigest: Sha256 | None
    taskIntent: TaskIntentState | None
    owner: Literal["agents-remember.task-domain"] = "agents-remember.task-domain"
    ownerVersion: Literal["task-scope-observation/v1"] = "task-scope-observation/v1"

    @field_validator("taskRoot")
    @classmethod
    def _absolute_task_root(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise ValueError("task root must be one normalized absolute POSIX path")
        return value


class TaskObservationPair(_StrictModel):
    """Content-addressed base and candidate task-domain observations."""

    base: CanonicalTaskObservation | None
    candidate: CanonicalTaskObservation | None


class ScopeCandidateIdentity(_StrictModel):
    """The exact code, memory, task, and contract authority for one compilation."""

    pairIdentity: MemoryCandidatePairIdentity
    code: GitTreeDelta
    memory: GitTreeDelta
    task: TaskObservationPair
    validatorVersion: Literal["scope-candidate/v1"] = "scope-candidate/v1"

    @model_validator(mode="after")
    def _roots_match_pair(self) -> ScopeCandidateIdentity:
        pair = self.pairIdentity
        if self.code.namespace != "code" or self.memory.namespace != "memory":
            raise ValueError("candidate must contain one code and one memory Git delta")
        if self.code.root != pair.codeRoot or self.memory.root != pair.memoryRoot:
            raise ValueError("Git delta roots must equal the canonical pair roots")
        if self.code.baseTree == self.code.candidateTree and self.code.changes:
            raise ValueError("an unchanged code tree cannot have changed roots")
        if self.memory.baseTree == self.memory.candidateTree and self.memory.changes:
            raise ValueError("an unchanged memory tree cannot have changed roots")
        return self

    def canonical_value(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=False)

    @property
    def digest(self) -> str:
        return canonical_digest(self.canonical_value())


class ScopeNode(_StrictModel):
    """One content-addressed source, memory, task, route, or index node."""

    nodeId: str = Field(min_length=1)
    contentDigest: Sha256
    authorityNamespace: str = Field(min_length=1)
    validatorVersion: str = Field(min_length=1)
    reasons: tuple[str, ...] = Field(min_length=1)

    @field_validator("nodeId")
    @classmethod
    def _canonical_node_id(cls, value: str) -> str:
        if value in {"task:normative-intent", "task:semantic-topology"}:
            return value
        namespace, separator, relative = value.partition(":")
        if (
            separator != ":"
            or namespace not in {"code", "memory"}
            or not relative
            or relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise ValueError("scope nodes must use canonical code, memory, or task identities")
        return value

    @field_validator("reasons")
    @classmethod
    def _canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value or any(not reason.strip() for reason in value):
            raise ValueError("node reasons must be nonblank, unique, and sorted")
        return value


class ScopeEdge(_StrictModel):
    """One owner-declared reverse edge from a changed input to its dependent."""

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    edgeClass: EdgeClass
    contentDigest: Sha256
    authorityNamespace: str = Field(min_length=1)
    extractorVersion: str = Field(min_length=1)
    validatorVersion: str = Field(min_length=1)
    reasons: tuple[str, ...] = Field(min_length=1)

    @field_validator("reasons")
    @classmethod
    def _canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value or any(not reason.strip() for reason in value):
            raise ValueError("edge reasons must be nonblank, unique, and sorted")
        return value


class EdgeClassEvidence(_StrictModel):
    edgeClass: EdgeClass
    authorityNamespace: str = Field(min_length=1)
    extractorVersion: str = Field(min_length=1)
    validatorVersion: str = Field(min_length=1)
    observedEdgeCount: int = Field(ge=0)
    evidenceDigest: Sha256


class SourceIndexObservation(_StrictModel):
    """The existing citation index owner's exact immutable generation."""

    owner: Literal["agents-remember.citation-source-index"] = (
        "agents-remember.citation-source-index"
    )
    schemaVersion: Literal[8] = 8
    snapshotId: Sha256
    codeRoot: str
    memoryRoot: str
    candidateDigest: Sha256
    validatorVersion: Literal["citation-source-index/v8"] = "citation-source-index/v8"

    @field_validator("codeRoot", "memoryRoot")
    @classmethod
    def _absolute_root(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise ValueError("source-index roots must be normalized absolute POSIX paths")
        return value


class DependencySnapshot(_StrictModel):
    """One immutable owner-produced dependency population bound to a candidate."""

    schema_: Literal["memory-dependency-snapshot/v1"] = Field(
        default=SNAPSHOT_SCHEMA, alias="schema"
    )
    candidateDigest: Sha256
    sourceIndex: SourceIndexObservation
    nodes: tuple[ScopeNode, ...]
    edges: tuple[ScopeEdge, ...]
    edgeEvidence: tuple[EdgeClassEvidence, ...]
    snapshotDigest: Sha256


class CheckerScopePolicy(_StrictModel):
    checker: str = Field(min_length=1)
    mode: Literal["incremental", "full-only"]
    extractorVersion: str | None = None
    edgeClasses: tuple[EdgeClass, ...] = ()
    reason: str = Field(min_length=1)


class ScopeManifest(_StrictModel):
    """Deterministic selected closure and its reuse/certification readiness."""

    schema_: Literal["memory-dependency-scope/v1"] = Field(default=MANIFEST_SCHEMA, alias="schema")
    candidateDigest: Sha256
    checkerRegistryVersion: Sha256
    sourceIndex: SourceIndexObservation
    changedRoots: tuple[str, ...]
    selectedNodes: tuple[ScopeNode, ...]
    selectedEdges: tuple[ScopeEdge, ...]
    checkerPolicies: tuple[CheckerScopePolicy, ...]
    fullOnlyCheckers: tuple[str, ...]
    incrementalReady: bool
    manifestDigest: Sha256


__all__ = [
    "CanonicalTaskObservation",
    "CheckerScopePolicy",
    "DependencySnapshot",
    "EdgeClass",
    "EdgeClassEvidence",
    "GitPathChange",
    "GitTreeDelta",
    "ScopeCandidateIdentity",
    "ScopeEdge",
    "ScopeManifest",
    "ScopeNode",
    "SourceIndexObservation",
    "TaskObservationPair",
    "canonical_digest",
]
