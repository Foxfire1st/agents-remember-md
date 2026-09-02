"""Structured authority contracts for leaf curator-coherence publication."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agents_remember.models.base import ToolResponse
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.evidence_dependencies import (
    EVIDENCE_DEPENDENCY_VALIDATOR,
    EvidenceDependencies,
    EvidenceDependencyError,
    build_evidence_dependencies,
    canonical_sha256,
    dependency,
    require_evidence_dependencies,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity, TaskIntentState

Digest = str
CuratorCoherenceAction = Literal["status", "prepare", "publish", "validate"]
CuratorDisposition = Literal[
    "reconciled",
    "preserved",
    "extended",
    "superseded",
    "contradicted",
    "no-content-impact",
    "no-route-impact",
    "capture-candidate",
]
MAX_CURATOR_SOURCE_CANDIDATES = 2048
MEMORY_QUALITY_ATTESTATION_VALIDATOR = "curator-memory-quality-attestation/v1"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CuratorSourceCandidate(_StrictModel):
    sourceFile: str = Field(min_length=1, max_length=8192)
    onboardingFile: str = Field(min_length=1, max_length=8192)
    classification: str = Field(min_length=1, max_length=256)

    @field_validator("sourceFile", "onboardingFile", "classification")
    @classmethod
    def _strip_identity(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("curator source-candidate identity fields must not be blank")
        return cleaned

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.sourceFile, self.onboardingFile, self.classification


class CuratorQualityAttestation(_StrictModel):
    """Exact enclosure-local memory-quality source consumed by coherence."""

    schema_: Literal["ar-curator-memory-quality/v1"] = Field(alias="schema")
    checklistStatus: Literal["action-required", "ready-for-closeout"]
    curatorActionableCount: int = Field(ge=0)
    memoryRepairCount: int = Field(ge=0)
    missingOnboardingCount: int = Field(ge=0)
    staleRouteIndexCount: int = Field(ge=0)
    sourceChangeCandidateCount: int = Field(ge=0, le=MAX_CURATOR_SOURCE_CANDIDATES)
    sourceChangeCandidates: list[CuratorSourceCandidate] = Field(
        max_length=MAX_CURATOR_SOURCE_CANDIDATES
    )
    pairIdentity: MemoryCandidatePairIdentity
    onboardingRoot: str = Field(min_length=1, max_length=8192)
    reportPath: str = Field(min_length=1, max_length=8192)
    reportSha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies: EvidenceDependencies | None = None

    @model_validator(mode="after")
    def _candidate_collection_is_exact(self) -> Self:
        identities = [candidate.identity for candidate in self.sourceChangeCandidates]
        if self.sourceChangeCandidateCount != len(identities):
            raise ValueError("sourceChangeCandidateCount does not match its candidate list")
        if len(identities) != len(set(identities)):
            raise ValueError("source-change candidate identities must be unique")
        return self


def memory_quality_attestation_dependencies(
    *,
    pair_identity: MemoryCandidatePairIdentity,
    code_candidate_tree: str,
    memory_candidate_tree: str,
    report_sha256: str,
) -> EvidenceDependencies:
    """Bind the memory-quality result to exactly the candidate pair it inspected."""

    return build_evidence_dependencies(
        "memory-quality-attestation/v1",
        [
            dependency("candidate-state", "memory-candidate-pair", pair_identity.contractDigest),
            dependency(
                "code-tree",
                "candidate",
                code_candidate_tree,
                algorithm="git-object",
            ),
            dependency(
                "memory-tree",
                "candidate",
                memory_candidate_tree,
                algorithm="git-object",
            ),
            dependency("evidence-bytes", "rendered-checklist", report_sha256),
            dependency(
                "validator",
                MEMORY_QUALITY_ATTESTATION_VALIDATOR,
                canonical_sha256(MEMORY_QUALITY_ATTESTATION_VALIDATOR),
            ),
            dependency(
                "validator",
                EVIDENCE_DEPENDENCY_VALIDATOR,
                canonical_sha256(EVIDENCE_DEPENDENCY_VALIDATOR),
            ),
        ],
    )


def require_memory_quality_attestation_dependencies(
    attestation: CuratorQualityAttestation,
    *,
    code_candidate_tree: str,
    memory_candidate_tree: str,
) -> EvidenceDependencies:
    """Refuse an attestation whose declared inputs differ from its current source facts."""

    expected = memory_quality_attestation_dependencies(
        pair_identity=attestation.pairIdentity,
        code_candidate_tree=code_candidate_tree,
        memory_candidate_tree=memory_candidate_tree,
        report_sha256=attestation.reportSha256,
    )
    observed = require_evidence_dependencies(
        attestation.dependencies,
        record_type="memory-quality-attestation/v1",
    )
    if observed != expected:
        raise EvidenceDependencyError(
            "memory-quality-attestation-dependencies-stale",
            "memory-quality attestation dependencies do not match its canonical inputs",
        )
    return observed


class CuratorCoherenceJudgment(_StrictModel):
    """One agent-owned semantic judgment; the lifecycle never invents these fields."""

    sourceFile: str = Field(min_length=1, max_length=8192)
    onboardingFile: str = Field(min_length=1, max_length=8192)
    classification: str = Field(min_length=1, max_length=256)
    disposition: CuratorDisposition
    rationale: str = Field(min_length=1, max_length=8192)
    evidenceRef: str = Field(min_length=1, max_length=8192)

    @field_validator("sourceFile", "onboardingFile", "classification", "rationale", "evidenceRef")
    @classmethod
    def _strip_judgment_fields(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("curator judgment fields must not be blank")
        return cleaned

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.sourceFile, self.onboardingFile, self.classification


class CuratorCoherenceRecordedJudgment(CuratorCoherenceJudgment):
    """Published judgment with the lifecycle-captured evidence digest."""

    evidenceSha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")


class CuratorCoherenceRecord(_StrictModel):
    """Immutable content-bound generation selected by the stable authority manifest."""

    schemaVersion: Literal["ar-curator-coherence-record/v1"] = "ar-curator-coherence-record/v1"
    leafId: str = Field(min_length=1, max_length=4096)
    contractPath: str = Field(min_length=1, max_length=8192)
    taskDocumentRef: TaskDocumentRef
    semanticRequirementRevision: str = Field(min_length=1, max_length=1024)
    deliveryAttempt: str = Field(min_length=1, max_length=256)
    pairIdentity: MemoryCandidatePairIdentity
    codeCandidateTree: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    memoryCandidateTree: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    taskTopologyFingerprint: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    taskIntent: TaskIntentState
    attestationPath: str = Field(min_length=1, max_length=8192)
    attestationSha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    attestationReportSha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    sourceCandidates: list[CuratorSourceCandidate] = Field(max_length=MAX_CURATOR_SOURCE_CANDIDATES)
    judgments: list[CuratorCoherenceRecordedJudgment] = Field(
        max_length=MAX_CURATOR_SOURCE_CANDIDATES
    )
    dependencies: EvidenceDependencies | None = None
    predecessorAuthorityDigest: Digest = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    publicationFingerprint: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    publishedBy: str = Field(min_length=1, max_length=8192)
    reportSha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def _decode_legacy_missing_intent(cls, value: Any) -> Any:
        if isinstance(value, dict) and "taskIntent" not in value:
            return {**value, "taskIntent": {"state": "missing-intent"}}
        return value

    @model_validator(mode="after")
    def _judgments_cover_candidates_exactly(self) -> Self:
        candidates = [candidate.identity for candidate in self.sourceCandidates]
        judgments = [judgment.identity for judgment in self.judgments]
        if len(candidates) != len(set(candidates)):
            raise ValueError("coherence record source candidates must be unique")
        if len(judgments) != len(set(judgments)):
            raise ValueError("coherence record judgments must be unique")
        if set(candidates) != set(judgments):
            raise ValueError("coherence record judgments must exactly cover source candidates")
        return self


class CuratorCoherenceAuthority(_StrictModel):
    """The only stable live pointer; historical generations never compete with it."""

    schemaVersion: Literal["ar-curator-coherence-authority/v1"] = (
        "ar-curator-coherence-authority/v1"
    )
    leafId: str = Field(min_length=1, max_length=4096)
    contractPath: str = Field(min_length=1, max_length=8192)
    currentRecordDigest: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    recordPath: str = Field(min_length=1, max_length=8192)
    reportPath: str = Field(min_length=1, max_length=8192)
    reportSha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")


class CuratorCoherenceSnapshot(_StrictModel):
    schemaVersion: Literal["ar-curator-coherence-snapshot/v1"] = "ar-curator-coherence-snapshot/v1"
    semanticRequirementRevision: str = Field(min_length=1, max_length=1024)
    deliveryAttempt: str = Field(min_length=1, max_length=256)
    recordDigest: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    recordPath: str = Field(min_length=1, max_length=8192)
    reportPath: str = Field(min_length=1, max_length=8192)


class CuratorCoherenceRequest(_StrictModel):
    action: CuratorCoherenceAction
    contract_path: str = Field(min_length=1, max_length=8192)
    semantic_requirement_revision: str | None = Field(default=None, max_length=1024)
    delivery_attempt: str | None = Field(default=None, max_length=256)
    judgments: list[CuratorCoherenceJudgment] = Field(
        default_factory=list, max_length=MAX_CURATOR_SOURCE_CANDIDATES
    )
    expected_predecessor_digest: str | None = Field(default=None, pattern=r"^$|^[0-9a-f]{64}$")
    expected_code_candidate_tree: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    expected_memory_candidate_tree: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    expected_task_topology_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_task_intent: TaskIntentIdentity | None = None
    expected_attestation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    freeze_snapshot: bool = False
    caller: DeclaredCaller | None = None

    @field_validator("contract_path")
    @classmethod
    def _strip_contract_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("contract_path must not be blank")
        return cleaned

    @field_validator("semantic_requirement_revision", "delivery_attempt")
    @classmethod
    def _strip_optional_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("semantic requirement revision and delivery attempt must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _action_has_one_input_shape(self) -> Self:
        publication_fields = (
            self.semantic_requirement_revision,
            self.delivery_attempt,
            self.expected_predecessor_digest,
            self.expected_code_candidate_tree,
            self.expected_memory_candidate_tree,
            self.expected_task_topology_fingerprint,
            self.expected_task_intent,
            self.expected_attestation_sha256,
            self.caller,
        )
        if self.action == "publish":
            if any(value is None for value in publication_fields):
                raise ValueError("publish requires every identity, predecessor, and caller field")
            return self
        if any(value is not None for value in publication_fields) or self.judgments:
            raise ValueError("status/prepare/validate forbid publication-only fields")
        if self.freeze_snapshot:
            raise ValueError("only publish may freeze an immutable attempt snapshot")
        return self


class CuratorCoherenceValidationResult(_StrictModel):
    state: Literal["valid"] = "valid"
    candidateCount: int = Field(ge=0)
    checked: list[str] = Field(default_factory=list, max_length=32)


class CuratorCoherenceResponse(ToolResponse):
    operation: Literal["curator_coherence"] = "curator_coherence"
    action: CuratorCoherenceAction
    state: str = Field(max_length=256)
    summary: str = Field(max_length=8192)
    contractPath: str = Field(max_length=8192)
    canonicalPath: str | None = Field(default=None, max_length=8192)
    recordPath: str | None = Field(default=None, max_length=8192)
    reportPath: str | None = Field(default=None, max_length=8192)
    snapshotPath: str | None = Field(default=None, max_length=8192)
    semanticRequirementRevision: str | None = Field(default=None, max_length=1024)
    deliveryAttempt: str | None = Field(default=None, max_length=256)
    pairIdentity: MemoryCandidatePairIdentity | None = None
    codeCandidateTree: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    memoryCandidateTree: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    taskTopologyFingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    taskIntent: TaskIntentIdentity | None = None
    currentnessStatus: str | None = Field(default=None, max_length=256)
    attestationPath: str | None = Field(default=None, max_length=8192)
    attestationSha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    attestationReportSha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    predecessorAuthorityDigest: str | None = Field(default=None, pattern=r"^$|^[0-9a-f]{64}$")
    recordDigest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reportDigest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidateCount: int | None = Field(default=None, ge=0)
    candidates: list[CuratorSourceCandidate] | None = Field(
        default=None, max_length=MAX_CURATOR_SOURCE_CANDIDATES
    )
    validationResult: CuratorCoherenceValidationResult | None = None
    status: str | None = Field(default=None, max_length=256)
    detail: str | None = Field(default=None, max_length=8192)
    pairField: str | None = Field(default=None, max_length=256)
    expected: dict[str, Any] | None = Field(default=None, max_length=32)
    observed: dict[str, Any] | None = Field(default=None, max_length=32)
    nextAction: str | None = Field(default=None, max_length=8192)
    nextArgs: dict[str, Any] | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _failure_shape_is_coherent(self) -> Self:
        if self.ok:
            if self.status is not None or self.detail is not None:
                raise ValueError("successful coherence response cannot carry refusal fields")
        elif not self.status or not self.detail or self.state != "refused":
            raise ValueError("failed coherence response requires typed refusal fields")
        return self
