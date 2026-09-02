"""Immutable contracts for content-addressed closeout gate certificates."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    ArtifactId,
    CandidateIdentity,
    FrozenContractModel,
    GateId,
    RailIdentity,
    RailStatus,
    SemanticText,
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_INPUT_KIND_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"


def _require_git_tree(value: CandidateIdentity) -> CandidateIdentity:
    if value.kind != "git-tree":
        raise ValueError("certificate candidate identity must be an exact Git tree")
    return value


GitTreeIdentity = Annotated[CandidateIdentity, AfterValidator(_require_git_tree)]


class CertificateInputIdentity(FrozenContractModel):
    """One exact semantic input consumed by a single gate."""

    inputKind: str = Field(pattern=_INPUT_KIND_PATTERN, max_length=128)
    inputId: SemanticText = Field(max_length=1024)
    contentDigest: str = Field(pattern=_DIGEST_PATTERN)

    @property
    def key(self) -> tuple[str, str]:
        return self.inputKind, self.inputId


class AdmissionGateIdentity(FrozenContractModel):
    gate: GateId
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    gateSemanticDigest: str = Field(pattern=_DIGEST_PATTERN)
    repositoryGatePlanDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    semanticInputs: tuple[CertificateInputIdentity, ...] = Field(max_length=16384)

    @model_validator(mode="after")
    def _require_gate_shape(self) -> Self:
        if (self.gate <= 4) != (self.repositoryGatePlanDigest is not None):
            raise ValueError("only Gates 1-4 require a repository gate-plan digest")
        _require_canonical_inputs(self.semanticInputs)
        return self


class CertificationAdmissionSemanticEnvelope(FrozenContractModel):
    schemaVersion: Literal["closeout-certification-admission-semantic/v1"] = (
        "closeout-certification-admission-semantic/v1"
    )
    repositoryId: str = Field(pattern=_INPUT_KIND_PATTERN, max_length=128)
    candidateCodeTree: GitTreeIdentity
    profileId: str = Field(pattern=_INPUT_KIND_PATTERN, max_length=128)
    certificationPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    admittedProfileDigest: str = Field(pattern=_DIGEST_PATTERN)
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    gates: tuple[AdmissionGateIdentity, ...] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def _require_all_gates(self) -> Self:
        if tuple(item.gate for item in self.gates) != (1, 2, 3, 4, 5):
            raise ValueError("certification admission must freeze exact ordered Gates 1-5")
        return self


class CreationProvenance(FrozenContractModel):
    """Audit evidence deliberately excluded from semantic object identity."""

    createdAt: SemanticText = Field(max_length=128)
    producer: SemanticText = Field(max_length=512)
    evidenceRef: SemanticText = Field(max_length=16384)


class CertificationAdmissionManifest(FrozenContractModel):
    schemaVersion: Literal["closeout-certification-admission/v1"] = (
        "closeout-certification-admission/v1"
    )
    semanticEnvelope: CertificationAdmissionSemanticEnvelope
    admissionDigest: str = Field(pattern=_DIGEST_PATTERN)
    provenance: CreationProvenance

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        if self.admissionDigest != content_digest(self.semanticEnvelope):
            raise ValueError("certification admission digest does not match its semantic envelope")
        return self


class GateCertificateIdentity(FrozenContractModel):
    gate: GateId
    certificateDigest: str = Field(pattern=_DIGEST_PATTERN)


class CertificateRailInventory(FrozenContractModel):
    rail: RailIdentity
    resultDigest: str = Field(pattern=_DIGEST_PATTERN)
    status: RailStatus
    code: str = Field(pattern=_INPUT_KIND_PATTERN, max_length=128)
    correctiveOwner: str = Field(pattern=_INPUT_KIND_PATTERN, max_length=128)


class CertificateArtifactInventory(FrozenContractModel):
    rail: RailIdentity
    artifactId: ArtifactId
    sha256: str = Field(pattern=_DIGEST_PATTERN)
    size: int = Field(ge=0, le=1_000_000_000_000)
    evidenceRef: SemanticText = Field(max_length=16384)

    @property
    def key(self) -> tuple[str, str]:
        return self.rail.key, self.artifactId


class CertificateEvidenceInventory(FrozenContractModel):
    rail: RailIdentity
    evidenceId: str = Field(pattern=_INPUT_KIND_PATTERN, max_length=128)
    sha256: str = Field(pattern=_DIGEST_PATTERN)
    size: int = Field(ge=0, le=1_000_000_000)
    reference: SemanticText = Field(max_length=16384)

    @property
    def key(self) -> tuple[str, str]:
        return self.rail.key, self.evidenceId


class ConsumedArtifactIdentity(FrozenContractModel):
    artifactId: ArtifactId
    producerGate: GateId
    producerCertificateDigest: str = Field(pattern=_DIGEST_PATTERN)
    sha256: str = Field(pattern=_DIGEST_PATTERN)
    size: int = Field(ge=0, le=1_000_000_000_000)


class CoherenceSubrecordIdentity(FrozenContractModel):
    subrecordId: str = Field(pattern=_INPUT_KIND_PATTERN, max_length=128)
    contentDigest: str = Field(pattern=_DIGEST_PATTERN)


class GateFiveSemanticInputs(FrozenContractModel):
    memoryTree: GitTreeIdentity
    affectedClosurePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    memoryCheckerRegistryDigest: str = Field(pattern=_DIGEST_PATTERN)
    coherenceSubrecords: tuple[CoherenceSubrecordIdentity, ...] = Field(
        min_length=1,
        max_length=4096,
    )
    candidatePairAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _require_canonical_subrecords(self) -> Self:
        expected = tuple(
            sorted(
                self.coherenceSubrecords,
                key=lambda item: (item.subrecordId, item.contentDigest),
            )
        )
        if self.coherenceSubrecords != expected or len(
            {item.subrecordId for item in self.coherenceSubrecords}
        ) != len(self.coherenceSubrecords):
            raise ValueError("Gate-5 coherence subrecords must be unique and canonical")
        return self


class GateCertificateIssuanceContext(FrozenContractModel):
    """Evidence provenance and optional Gate-5 inputs for one issuance call."""

    provenance: CreationProvenance
    gateFiveInputs: GateFiveSemanticInputs | None = None


class GateCertificateSemanticEnvelope(FrozenContractModel):
    schemaVersion: Literal["closeout-gate-certificate-semantic/v1"] = (
        "closeout-gate-certificate-semantic/v1"
    )
    certificateVersion: Literal["1.0.0"] = "1.0.0"
    gate: GateId
    repositoryId: str = Field(pattern=_INPUT_KIND_PATTERN, max_length=128)
    candidateCodeTree: GitTreeIdentity
    admissionDigest: str = Field(pattern=_DIGEST_PATTERN)
    admittedProfileDigest: str = Field(pattern=_DIGEST_PATTERN)
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    gateSemanticDigest: str = Field(pattern=_DIGEST_PATTERN)
    repositoryGatePlanDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    directPredecessors: tuple[GateCertificateIdentity, ...] = Field(max_length=4)
    semanticInputs: tuple[CertificateInputIdentity, ...] = Field(max_length=16384)
    consumedArtifacts: tuple[ConsumedArtifactIdentity, ...] = Field(max_length=4096)
    resultManifestDigest: str = Field(pattern=_DIGEST_PATTERN)
    terminalDisposition: Literal["green"] = "green"
    railInventory: tuple[CertificateRailInventory, ...] = Field(min_length=1, max_length=4096)
    artifactInventory: tuple[CertificateArtifactInventory, ...] = Field(max_length=4096)
    evidenceInventory: tuple[CertificateEvidenceInventory, ...] = Field(max_length=4096)
    gateFiveInputs: GateFiveSemanticInputs | None = None

    @model_validator(mode="after")
    def _require_canonical_envelope(self) -> Self:
        if tuple(item.gate for item in self.directPredecessors) != tuple(range(1, self.gate)):
            raise ValueError("certificate predecessors must be the exact earlier-gate prefix")
        if (self.gate <= 4) != (self.repositoryGatePlanDigest is not None):
            raise ValueError("only Gates 1-4 bind a repository gate-plan digest")
        if (self.gate == 5) != (self.gateFiveInputs is not None):
            raise ValueError("only Gate 5 binds memory and coherence inputs")
        _require_canonical_inputs(self.semanticInputs)
        _require_canonical_inventory(self)
        return self


class GateCertificate(FrozenContractModel):
    schemaVersion: Literal["closeout-gate-certificate/v1"] = "closeout-gate-certificate/v1"
    semanticEnvelope: GateCertificateSemanticEnvelope
    certificateDigest: str = Field(pattern=_DIGEST_PATTERN)
    provenance: CreationProvenance

    @property
    def identity(self) -> GateCertificateIdentity:
        return GateCertificateIdentity(
            gate=self.semanticEnvelope.gate,
            certificateDigest=self.certificateDigest,
        )

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        if self.certificateDigest != content_digest(self.semanticEnvelope):
            raise ValueError("gate certificate digest does not match its semantic envelope")
        return self


class FinalizationSemanticEnvelope(FrozenContractModel):
    schemaVersion: Literal["closeout-finalization-certificate-authority-semantic/v1"] = (
        "closeout-finalization-certificate-authority-semantic/v1"
    )
    repositoryId: str = Field(pattern=_INPUT_KIND_PATTERN, max_length=128)
    candidateCodeTree: GitTreeIdentity
    candidateMemoryTree: GitTreeIdentity
    admissionDigest: str = Field(pattern=_DIGEST_PATTERN)
    certificates: tuple[GateCertificateIdentity, ...] = Field(min_length=5, max_length=5)
    candidatePairAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    taskIntentAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    journalAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _require_certificate_prefix(self) -> Self:
        if tuple(item.gate for item in self.certificates) != (1, 2, 3, 4, 5):
            raise ValueError("finalization authority requires exact ordered Gates 1-5")
        return self


class FinalizationCurrentInputs(FrozenContractModel):
    """Exact mutable-edge authorities revalidated by transactional finalization."""

    gateFiveInputs: GateFiveSemanticInputs
    taskIntentAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    journalAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)


class FinalizationCertificateAuthority(FrozenContractModel):
    schemaVersion: Literal["closeout-finalization-certificate-authority/v1"] = (
        "closeout-finalization-certificate-authority/v1"
    )
    semanticEnvelope: FinalizationSemanticEnvelope
    authorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    provenance: CreationProvenance

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        if self.authorityDigest != content_digest(self.semanticEnvelope):
            raise ValueError("finalization authority digest does not match its semantic envelope")
        return self


def _require_canonical_inputs(inputs: tuple[CertificateInputIdentity, ...]) -> None:
    expected = tuple(sorted(inputs, key=lambda item: (*item.key, item.contentDigest)))
    if inputs != expected or len({item.key for item in inputs}) != len(inputs):
        raise ValueError("certificate semantic inputs must be unique and canonical")


def _require_canonical_inventory(envelope: GateCertificateSemanticEnvelope) -> None:
    rail_expected = tuple(sorted(envelope.railInventory, key=lambda item: item.rail.key))
    artifact_expected = tuple(sorted(envelope.artifactInventory, key=lambda item: item.key))
    evidence_expected = tuple(sorted(envelope.evidenceInventory, key=lambda item: item.key))
    consumed_expected = tuple(
        sorted(envelope.consumedArtifacts, key=lambda item: (item.artifactId, item.producerGate))
    )
    if envelope.railInventory != rail_expected or len(
        {item.rail.key for item in envelope.railInventory}
    ) != len(envelope.railInventory):
        raise ValueError("certificate rail inventory must be unique and canonical")
    if envelope.artifactInventory != artifact_expected or len(
        {item.key for item in envelope.artifactInventory}
    ) != len(envelope.artifactInventory):
        raise ValueError("certificate artifact inventory must be unique and canonical")
    if envelope.evidenceInventory != evidence_expected or len(
        {item.key for item in envelope.evidenceInventory}
    ) != len(envelope.evidenceInventory):
        raise ValueError("certificate evidence inventory must be unique and canonical")
    if envelope.consumedArtifacts != consumed_expected or len(
        {item.artifactId for item in envelope.consumedArtifacts}
    ) != len(envelope.consumedArtifacts):
        raise ValueError("consumed artifact identities must be unique and canonical")


__all__ = [
    "AdmissionGateIdentity",
    "CertificateArtifactInventory",
    "CertificateEvidenceInventory",
    "CertificateInputIdentity",
    "CertificateRailInventory",
    "CertificationAdmissionManifest",
    "CertificationAdmissionSemanticEnvelope",
    "CoherenceSubrecordIdentity",
    "ConsumedArtifactIdentity",
    "CreationProvenance",
    "FinalizationCertificateAuthority",
    "FinalizationCurrentInputs",
    "FinalizationSemanticEnvelope",
    "GateCertificate",
    "GateCertificateIdentity",
    "GateCertificateIssuanceContext",
    "GateCertificateSemanticEnvelope",
    "GateFiveSemanticInputs",
]
