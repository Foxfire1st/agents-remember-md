"""Closed owner observations retained alongside lifecycle certification admission."""

from __future__ import annotations

import hashlib
from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.certificate_models import CreationProvenance
from agents_remember.certification.digests import content_digest
from agents_remember.models.certification.base import FrozenContractModel, SemanticText

_DIGEST = r"^[0-9a-f]{64}$"
_GIT_OBJECT = r"^[0-9a-f]{40,64}$"
_BRANCH = r"^refs/heads/[^\s]+$"


class AuthorityInputSnapshot(FrozenContractModel):
    """Exact serialized output of an identified existing input owner."""

    owner: SemanticText = Field(max_length=256)
    address: SemanticText = Field(max_length=8192)
    canonicalBytes: str = Field(max_length=2_000_000)
    contentSha256: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def _require_bytes(self) -> Self:
        if hashlib.sha256(self.canonicalBytes.encode("utf-8")).hexdigest() != self.contentSha256:
            raise ValueError("authority input bytes do not match their retained SHA-256")
        return self


class MutationAuthorityRecord(FrozenContractModel):
    schemaVersion: Literal["closeout-mutation-authority/v1"] = "closeout-mutation-authority/v1"
    owner: Literal["worktree-closeout"] = "worktree-closeout"
    contractPath: SemanticText = Field(max_length=8192)
    taskId: SemanticText = Field(max_length=512)
    codeRoot: SemanticText = Field(max_length=8192)
    codeWorkRef: str = Field(pattern=_BRANCH)
    memoryRoot: SemanticText | None = Field(default=None, max_length=8192)
    memoryWorkRef: str | None = Field(default=None, pattern=_BRANCH)
    normalizedCommitIntentDigest: str = Field(pattern=_DIGEST)
    policy: Literal["linked-leaf-candidate-and-exact-door"] = "linked-leaf-candidate-and-exact-door"

    @model_validator(mode="after")
    def _require_memory_pair(self) -> Self:
        if (self.memoryRoot is None) != (self.memoryWorkRef is None):
            raise ValueError("memory mutation authority requires both root and branch")
        return self


class SourceAuthorityEdge(FrozenContractModel):
    side: Literal["code", "memory"]
    relation: Literal["super-to-master", "master-to-leaf", "super-to-leaf"]
    repositoryRoot: SemanticText = Field(max_length=8192)
    sourceRef: str = Field(pattern=_BRANCH)
    sourceTip: str = Field(pattern=_GIT_OBJECT)
    descendantRef: str = Field(pattern=_BRANCH)
    descendantTip: str = Field(pattern=_GIT_OBJECT)
    contractPath: SemanticText = Field(max_length=8192)


class SourceAuthorityRecord(FrozenContractModel):
    schemaVersion: Literal["closeout-source-authority/v1"] = "closeout-source-authority/v1"
    owner: Literal["task-source-lineage"] = "task-source-lineage"
    sourceRef: str = Field(pattern=_BRANCH)
    sourceTip: str = Field(pattern=_GIT_OBJECT)
    edges: tuple[SourceAuthorityEdge, ...] = Field(min_length=1, max_length=256)


class WorktreeRuleRecord(FrozenContractModel):
    schemaVersion: Literal["closeout-worktree-rules/v1"] = "closeout-worktree-rules/v1"
    owner: Literal["closeout-staged-candidate"] = "closeout-staged-candidate"
    codeRoot: SemanticText = Field(max_length=8192)
    gitDirectory: SemanticText = Field(max_length=8192)
    commonDirectory: SemanticText = Field(max_length=8192)
    workRef: str = Field(pattern=_BRANCH)
    headCommit: str = Field(pattern=_GIT_OBJECT)
    stagedTree: str = Field(pattern=_GIT_OBJECT)
    addAllTree: str = Field(pattern=_GIT_OBJECT)
    conflictedPaths: tuple[SemanticText, ...] = Field(default_factory=tuple, max_length=4096)
    preCommitHookRan: bool
    preparationPolicy: Literal["strict-hook-before-freeze-no-successor-adoption"] = (
        "strict-hook-before-freeze-no-successor-adoption"
    )


class GeneratedInputRecord(FrozenContractModel):
    """Profile-owned declarations bind immutable candidate bytes; Gate 1 checks freshness."""

    schemaVersion: Literal["closeout-generated-inputs/v1"] = "closeout-generated-inputs/v1"
    owner: Literal["repository-certification-profile"] = "repository-certification-profile"
    profileDigest: str = Field(pattern=_DIGEST)
    candidateTree: str = Field(pattern=_GIT_OBJECT)
    declarations: AuthorityInputSnapshot
    status: Literal["unknown"] = "unknown"


class CandidateAuthorityEnvelope(FrozenContractModel):
    schemaVersion: Literal["closeout-candidate-authorities-semantic/v1"] = (
        "closeout-candidate-authorities-semantic/v1"
    )
    mutation: MutationAuthorityRecord
    source: SourceAuthorityRecord
    worktree: WorktreeRuleRecord
    generated: GeneratedInputRecord


class CandidateAuthorityRecords(FrozenContractModel):
    """Semantic projections plus separately retained derivation bytes and provenance."""

    schemaVersion: Literal["closeout-candidate-authorities/v1"] = (
        "closeout-candidate-authorities/v1"
    )
    semanticEnvelope: CandidateAuthorityEnvelope
    authorityDigest: str = Field(pattern=_DIGEST)
    inputSnapshots: tuple[AuthorityInputSnapshot, ...] = Field(min_length=1, max_length=256)
    provenance: CreationProvenance

    @model_validator(mode="after")
    def _require_digest(self) -> Self:
        if self.authorityDigest != content_digest(self.semanticEnvelope):
            raise ValueError("candidate authority digest differs from its semantic projections")
        keys = tuple((item.owner, item.address) for item in self.inputSnapshots)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("authority input snapshots must be unique and canonical")
        return self
