"""Exact-candidate lifecycle boundary records for closeout certification."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    CertificateReusePlan,
)
from agents_remember.certification.certificate_models import (
    CreationProvenance,
    GateCertificateIdentity,
    GitTreeIdentity,
)
from agents_remember.certification.digests import content_digest
from agents_remember.models.certification.base import (
    FrozenContractModel,
    GateId,
    SemanticText,
)
from agents_remember.models.certification.corrective import RedCatalogDisposition

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_COMMIT_PATTERN = r"^[0-9a-f]{40,64}$"
_BRANCH_REF_PATTERN = r"^refs/heads/[^\s]+$"

AuthorityStatus = Literal["valid", "invalid"]
GeneratedArtifactStatus = Literal["current", "stale", "unknown"]
FinalizationLeg = Literal[
    "code-commit",
    "external-memory-commit",
    "ledger-commit",
    "contract-finalization",
]
FinalizationLegState = Literal["pending", "intent", "proven", "not-applicable"]

_FINALIZATION_LEGS: tuple[FinalizationLeg, ...] = (
    "code-commit",
    "external-memory-commit",
    "ledger-commit",
    "contract-finalization",
)


class ExactCandidateObservation(FrozenContractModel):
    """One owner-produced observation; admission never scans for an alternative."""

    schemaVersion: Literal["closeout-exact-candidate-observation/v1"] = (
        "closeout-exact-candidate-observation/v1"
    )
    taskId: SemanticText = Field(max_length=512)
    contractPath: SemanticText = Field(max_length=4096)
    lifecycleAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    repositoryId: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=128)
    sourceBranchRef: str = Field(pattern=_BRANCH_REF_PATTERN, max_length=4096)
    sourceBranchTip: str = Field(pattern=_COMMIT_PATTERN)
    candidateCodeTree: GitTreeIdentity
    topologyIdentityDigest: str = Field(pattern=_DIGEST_PATTERN)
    taskIntentIdentityDigest: str = Field(pattern=_DIGEST_PATTERN)
    normalizedCommitIntentDigest: str = Field(pattern=_DIGEST_PATTERN)
    mutationAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    sourceAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    worktreeRuleDigest: str = Field(pattern=_DIGEST_PATTERN)
    generatedInputsDigest: str = Field(pattern=_DIGEST_PATTERN)
    mutationAuthorityStatus: AuthorityStatus
    sourceAuthorityStatus: AuthorityStatus
    branchAuthorityStatus: AuthorityStatus
    worktreeStatus: Literal["admissible", "conflicted", "invalid"]
    conflictedPaths: tuple[SemanticText, ...] = Field(default_factory=tuple, max_length=4096)
    generatedArtifactStatus: GeneratedArtifactStatus

    @model_validator(mode="after")
    def _require_conflict_shape(self) -> Self:
        if (self.worktreeStatus == "conflicted") != bool(self.conflictedPaths):
            raise ValueError("conflicted worktree status must name its exact conflicted paths")
        if self.conflictedPaths != tuple(sorted(set(self.conflictedPaths))):
            raise ValueError("conflicted paths must be unique and canonical")
        return self


class PriorRedDispositionSemanticEnvelope(FrozenContractModel):
    schemaVersion: Literal["closeout-prior-red-disposition-semantic/v1"] = (
        "closeout-prior-red-disposition-semantic/v1"
    )
    priorAdmissionDigest: str = Field(pattern=_DIGEST_PATTERN)
    priorCatalogDigest: str = Field(pattern=_DIGEST_PATTERN)
    gate: GateId
    successorCandidateCodeTree: GitTreeIdentity
    successorCertificationPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    dispositions: tuple[RedCatalogDisposition, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _require_canonical_dispositions(self) -> Self:
        expected = tuple(sorted(self.dispositions, key=lambda item: item.rail.key))
        if self.dispositions != expected or len(
            {item.rail.key for item in self.dispositions}
        ) != len(self.dispositions):
            raise ValueError("prior-red dispositions must be unique and canonical")
        return self


class PriorRedDispositionManifest(FrozenContractModel):
    schemaVersion: Literal["closeout-prior-red-disposition/v1"] = (
        "closeout-prior-red-disposition/v1"
    )
    semanticEnvelope: PriorRedDispositionSemanticEnvelope
    dispositionDigest: str = Field(pattern=_DIGEST_PATTERN)
    provenance: CreationProvenance

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        if self.dispositionDigest != content_digest(self.semanticEnvelope):
            raise ValueError("prior-red disposition digest does not match its semantic envelope")
        return self


class LifecycleAdmissionSemanticEnvelope(FrozenContractModel):
    schemaVersion: Literal["closeout-lifecycle-admission-semantic/v1"] = (
        "closeout-lifecycle-admission-semantic/v1"
    )
    candidate: ExactCandidateObservation
    certificationAdmissionDigest: str = Field(pattern=_DIGEST_PATTERN)
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certificationPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    repositoryProfileDigest: str = Field(pattern=_DIGEST_PATTERN)
    repositoryPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    profileId: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=128)
    priorRedDispositionDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    gateStarts: Literal[0] = 0


class LifecycleAdmissionManifest(FrozenContractModel):
    schemaVersion: Literal["closeout-lifecycle-admission/v1"] = "closeout-lifecycle-admission/v1"
    semanticEnvelope: LifecycleAdmissionSemanticEnvelope
    admissionDigest: str = Field(pattern=_DIGEST_PATTERN)
    provenance: CreationProvenance

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        if self.admissionDigest != content_digest(self.semanticEnvelope):
            raise ValueError("lifecycle admission digest does not match its semantic envelope")
        return self


class CertificationRecoverySemanticEnvelope(FrozenContractModel):
    schemaVersion: Literal["closeout-certification-recovery-semantic/v1"] = (
        "closeout-certification-recovery-semantic/v1"
    )
    lifecycleAdmissionDigest: str = Field(pattern=_DIGEST_PATTERN)
    admittedCertificates: tuple[GateCertificateIdentity, ...] = Field(max_length=5)
    inputChanges: tuple[CertificateInputChange, ...] = Field(max_length=4096)
    reusePlan: CertificateReusePlan


class CertificationRecoveryRecord(FrozenContractModel):
    schemaVersion: Literal["closeout-certification-recovery/v1"] = (
        "closeout-certification-recovery/v1"
    )
    semanticEnvelope: CertificationRecoverySemanticEnvelope
    recoveryDigest: str = Field(pattern=_DIGEST_PATTERN)
    provenance: CreationProvenance

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        if self.recoveryDigest != content_digest(self.semanticEnvelope):
            raise ValueError("certification recovery digest does not match its semantic envelope")
        return self


class FinalizationBoundaryObservation(FrozenContractModel):
    """Current owner-produced authorities revalidated immediately before publication."""

    candidateCodeTree: GitTreeIdentity
    candidateMemoryTree: GitTreeIdentity
    topologyIdentityDigest: str = Field(pattern=_DIGEST_PATTERN)
    taskIntentIdentityDigest: str = Field(pattern=_DIGEST_PATTERN)
    coherentOperationStateDigest: str = Field(pattern=_DIGEST_PATTERN)
    operationStatus: Literal["ready", "finalizing", "cancel-requested", "cancelled", "failed"]
    doorAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    doorStatus: Literal["claimed", "lost", "stale"]
    approvalAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    approvalStatus: Literal["current", "lost", "stale"]
    normalizedCommitIntentDigest: str = Field(pattern=_DIGEST_PATTERN)


class DurableFinalizationLeg(FrozenContractModel):
    leg: FinalizationLeg
    state: FinalizationLegState
    authorityDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    intendedOutputDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    provenOutputDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _require_state_shape(self) -> Self:
        if self.state == "not-applicable":
            if any((self.authorityDigest, self.intendedOutputDigest, self.provenOutputDigest)):
                raise ValueError("not-applicable finalization leg cannot carry write authority")
            return self
        if self.authorityDigest is None:
            raise ValueError("applicable finalization leg requires exact write authority")
        if self.state == "pending" and any((self.intendedOutputDigest, self.provenOutputDigest)):
            raise ValueError("pending finalization leg cannot claim an intended or proven output")
        if self.state == "intent" and (
            self.intendedOutputDigest is None or self.provenOutputDigest is not None
        ):
            raise ValueError("intent finalization leg requires only its intended output")
        if self.state == "proven" and (
            self.intendedOutputDigest is None or self.provenOutputDigest is None
        ):
            raise ValueError("proven finalization leg requires intended and proven outputs")
        return self


class FinalizationJournalState(FrozenContractModel):
    schemaVersion: Literal["closeout-finalization-journal/v1"] = "closeout-finalization-journal/v1"
    journalAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    legs: tuple[DurableFinalizationLeg, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def _require_ordered_progress(self) -> Self:
        if tuple(item.leg for item in self.legs) != _FINALIZATION_LEGS:
            raise ValueError("finalization journal must preserve the existing durable leg order")
        active = tuple(item for item in self.legs if item.state != "not-applicable")
        states = tuple(item.state for item in active)
        intent_count = states.count("intent")
        if intent_count > 1:
            raise ValueError("finalization journal can retain at most one unfinished write intent")
        phase = "proven"
        for state in states:
            if phase == "proven" and state == "proven":
                continue
            if phase == "proven" and state in {"intent", "pending"}:
                phase = state
                continue
            if phase == "intent" and state != "pending":
                raise ValueError("finalization journal progress is not monotonic")
            if phase == "pending" and state != "pending":
                raise ValueError("finalization journal progress is not monotonic")
        return self

    @property
    def next_leg(self) -> FinalizationLeg | None:
        for item in self.legs:
            if item.state == "intent":
                return item.leg
        for item in self.legs:
            if item.state == "pending":
                return item.leg
        return None


class LifecycleFinalizationSemanticEnvelope(FrozenContractModel):
    schemaVersion: Literal["closeout-lifecycle-finalization-semantic/v1"] = (
        "closeout-lifecycle-finalization-semantic/v1"
    )
    lifecycleAdmissionDigest: str = Field(pattern=_DIGEST_PATTERN)
    certificateAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    observation: FinalizationBoundaryObservation
    journal: FinalizationJournalState
    nextLeg: FinalizationLeg | None
    zeroGateStarts: Literal[True] = True

    @model_validator(mode="after")
    def _require_journal_edge(self) -> Self:
        if self.nextLeg != self.journal.next_leg:
            raise ValueError("finalization resume edge must name the exact unfinished journal leg")
        return self


class LifecycleFinalizationManifest(FrozenContractModel):
    schemaVersion: Literal["closeout-lifecycle-finalization/v1"] = (
        "closeout-lifecycle-finalization/v1"
    )
    semanticEnvelope: LifecycleFinalizationSemanticEnvelope
    finalizationDigest: str = Field(pattern=_DIGEST_PATTERN)
    provenance: CreationProvenance

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        if self.finalizationDigest != content_digest(self.semanticEnvelope):
            raise ValueError("lifecycle finalization digest does not match its semantic envelope")
        return self


__all__ = [
    "AuthorityStatus",
    "CertificationRecoveryRecord",
    "CertificationRecoverySemanticEnvelope",
    "DurableFinalizationLeg",
    "ExactCandidateObservation",
    "FinalizationBoundaryObservation",
    "FinalizationJournalState",
    "FinalizationLeg",
    "FinalizationLegState",
    "GeneratedArtifactStatus",
    "LifecycleAdmissionManifest",
    "LifecycleAdmissionSemanticEnvelope",
    "LifecycleFinalizationManifest",
    "LifecycleFinalizationSemanticEnvelope",
    "PriorRedDispositionManifest",
    "PriorRedDispositionSemanticEnvelope",
]
