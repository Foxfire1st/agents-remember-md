"""Closed contracts for one lossless closeout-readiness vocabulary."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    FinalizationCertificateAuthority,
    FinalizationCurrentInputs,
    GateCertificate,
    GateFiveSemanticInputs,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CandidateIdentity,
    CertificationPlan,
    GateResultManifest,
    RailEvidenceReference,
    RailPosture,
)
from agents_remember.certification.repository_profiles.models import RepositoryProfilePlan
from agents_remember.models.certification.base import (
    FrozenContractModel,
    GateId,
    RailIdentity,
    SemanticText,
)

LifecycleReadinessState = Literal[
    "admission-pending",
    "admission-refused",
    "admitted",
    "finalization-pending",
    "finalization-running",
    "finalization-refused",
    "finalized",
]
GateReadinessState = Literal[
    "not-started",
    "blocked",
    "running",
    "passed",
    "failed",
    "invalidated",
]
RailReadinessState = Literal[
    "pass",
    "fail",
    "blocked",
    "not-applicable",
    "report-only-pass",
    "report-only-fail",
]
CertificateReadinessState = Literal[
    "absent",
    "current-green",
    "stale",
    "invalidated",
    "unavailable",
]
ProfileReadinessState = Literal["unresolved", "invalid", "admitted-current", "changed"]
ReadinessSurface = Literal[
    "interactive",
    "admission",
    "gate-execution",
    "diagnostic",
    "status",
    "wait",
    "journal",
    "dashboard",
    "finalization",
]
ReadinessTransitionDomain = Literal["lifecycle", "gate", "certificate", "profile"]

READINESS_SURFACES: tuple[ReadinessSurface, ...] = (
    "interactive",
    "admission",
    "gate-execution",
    "diagnostic",
    "status",
    "wait",
    "journal",
    "dashboard",
    "finalization",
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"


class ReadinessRevision(FrozenContractModel):
    """One lifecycle generation and monotonically increasing projection revision."""

    generationId: SemanticText = Field(max_length=256)
    revision: int = Field(ge=0)


class ReadinessEvidenceReference(FrozenContractModel):
    evidenceId: str = Field(pattern=_ID_PATTERN, max_length=128)
    sha256: str = Field(pattern=_DIGEST_PATTERN)
    size: int = Field(ge=0, le=1_000_000_000)
    reference: SemanticText = Field(max_length=16384)


class ProfileReadinessObservation(FrozenContractModel):
    revision: ReadinessRevision
    state: ProfileReadinessState
    repositoryPlan: RepositoryProfilePlan | None = None


class LifecycleReadinessObservation(FrozenContractModel):
    revision: ReadinessRevision
    state: LifecycleReadinessState
    blockedBy: tuple[SemanticText, ...] = Field(default_factory=tuple, max_length=256)
    code: str | None = Field(default=None, pattern=_ID_PATTERN, max_length=128)
    correctiveOwner: str | None = Field(default=None, pattern=_ID_PATTERN, max_length=128)
    evidence: tuple[ReadinessEvidenceReference, ...] = Field(
        default_factory=tuple,
        max_length=128,
    )

    @model_validator(mode="after")
    def _require_refusal_payload(self) -> Self:
        refused = self.state in {"admission-refused", "finalization-refused"}
        payload = self.code is not None and self.correctiveOwner is not None and bool(self.evidence)
        if refused != payload:
            raise ValueError("only refused lifecycle states require typed failure evidence")
        return self


class GateReadinessObservation(FrozenContractModel):
    revision: ReadinessRevision
    gate: GateId
    state: GateReadinessState
    blockedBy: tuple[SemanticText, ...] = Field(default_factory=tuple, max_length=256)
    resultManifest: GateResultManifest | None = None
    certificateState: CertificateReadinessState = "absent"
    certificate: GateCertificate | None = None
    genericTerminalReplacement: bool = False

    @model_validator(mode="after")
    def _require_state_shape(self) -> Self:
        if (self.state == "blocked") != bool(self.blockedBy):
            raise ValueError("exactly a blocked gate state requires blockedBy")
        if self.state in {"passed", "failed"} and self.resultManifest is None:
            raise ValueError("passed and failed gates require a typed result manifest")
        if self.state not in {"passed", "failed"} and self.resultManifest is not None:
            raise ValueError("only passed and failed gates may carry a current result manifest")
        mismatched_current = (self.certificateState == "current-green") != (
            self.certificate is not None
        )
        invalid_historical = (
            self.certificateState not in {"stale", "invalidated"} or self.certificate is None
        )
        if mismatched_current and invalid_historical:
            raise ValueError("only current, stale, or invalidated certificate states carry bytes")
        return self


class DiagnosticReadinessObservation(FrozenContractModel):
    revision: ReadinessRevision
    plan: CertificationPlan
    resultManifest: GateResultManifest


class CloseoutReadinessInput(FrozenContractModel):
    revision: ReadinessRevision
    repositoryId: str = Field(pattern=_ID_PATTERN, max_length=128)
    certificationPlan: CertificationPlan
    profile: ProfileReadinessObservation
    lifecycle: LifecycleReadinessObservation
    gates: tuple[GateReadinessObservation, ...] = Field(min_length=5, max_length=5)
    admission: CertificationAdmissionManifest | None = None
    gateFiveInputs: GateFiveSemanticInputs | None = None
    diagnostics: tuple[DiagnosticReadinessObservation, ...] = Field(
        default_factory=tuple,
        max_length=128,
    )
    finalizationAuthority: FinalizationCertificateAuthority | None = None
    finalizationInputs: FinalizationCurrentInputs | None = None


class ProfileReadinessProjection(FrozenContractModel):
    state: ProfileReadinessState
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    repositoryPlanDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    admittedProfileDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)


class LifecycleReadinessProjection(FrozenContractModel):
    state: LifecycleReadinessState
    blockedBy: tuple[SemanticText, ...]
    code: str | None = Field(default=None, pattern=_ID_PATTERN, max_length=128)
    correctiveOwner: str | None = Field(default=None, pattern=_ID_PATTERN, max_length=128)
    evidence: tuple[ReadinessEvidenceReference, ...]


class RailReadinessProjection(FrozenContractModel):
    rail: RailIdentity
    posture: RailPosture
    applicability: Literal["applicable", "not-applicable"]
    state: RailReadinessState | None
    code: str | None = Field(default=None, pattern=_ID_PATTERN, max_length=128)
    correctiveOwner: str = Field(pattern=_ID_PATTERN, max_length=128)
    blockedBy: tuple[RailIdentity, ...]
    evidence: tuple[RailEvidenceReference, ...]


class GateReadinessProjection(FrozenContractModel):
    gate: GateId
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    state: GateReadinessState
    blockedBy: tuple[SemanticText, ...]
    rails: tuple[RailReadinessProjection, ...] = Field(min_length=1, max_length=4096)
    resultManifestDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    certificateState: CertificateReadinessState
    certificateDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)


class DiagnosticReadinessProjection(FrozenContractModel):
    certificationPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    gate: GateId
    disposition: Literal["green", "red"]
    rails: tuple[RailReadinessProjection, ...] = Field(min_length=1, max_length=4096)
    resultManifestDigest: str = Field(pattern=_DIGEST_PATTERN)


class CloseoutReadinessProjection(FrozenContractModel):
    schemaVersion: Literal["closeout-readiness/v1"] = "closeout-readiness/v1"
    revision: ReadinessRevision
    repositoryId: str = Field(pattern=_ID_PATTERN, max_length=128)
    candidateIdentity: CandidateIdentity
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certificationPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    profile: ProfileReadinessProjection
    lifecycle: LifecycleReadinessProjection
    gates: tuple[GateReadinessProjection, ...] = Field(min_length=5, max_length=5)
    diagnostics: tuple[DiagnosticReadinessProjection, ...]
    certificationReady: bool
    finalizationAuthorityDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    projectionDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"projectionDigest"})
        if self.projectionDigest != content_digest(payload):
            raise ValueError("readiness projection digest does not match its content")
        return self


class ReadinessTransitionRule(FrozenContractModel):
    domain: ReadinessTransitionDomain
    before: SemanticText = Field(max_length=64)
    after: tuple[SemanticText, ...] = Field(max_length=16)


__all__ = [
    "READINESS_SURFACES",
    "CertificateReadinessState",
    "CloseoutReadinessInput",
    "CloseoutReadinessProjection",
    "DiagnosticReadinessObservation",
    "DiagnosticReadinessProjection",
    "GateReadinessObservation",
    "GateReadinessProjection",
    "GateReadinessState",
    "LifecycleReadinessObservation",
    "LifecycleReadinessProjection",
    "LifecycleReadinessState",
    "ProfileReadinessObservation",
    "ProfileReadinessProjection",
    "ProfileReadinessState",
    "RailReadinessProjection",
    "RailReadinessState",
    "ReadinessEvidenceReference",
    "ReadinessRevision",
    "ReadinessSurface",
    "ReadinessTransitionDomain",
    "ReadinessTransitionRule",
]
