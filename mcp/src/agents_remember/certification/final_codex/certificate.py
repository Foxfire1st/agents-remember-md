"""Bound Gate-4 certificate compilation for the final real-codex lane.

CCR-R14@v3 publishes one Gate-4 certificate that binds the exact code tree,
the exact green Gate-1..3 predecessor certificate identities, the integration
plan and repository profile, the runtime/provider/Codex identities, the
admitted R12 runner/store snapshot, both fresh certifying repetition
identities and results, the teardown evidence, and the complete result
manifests.  This module compiles that immutable bound certificate from the
exact two-fresh-pass lane:

* it refuses every incomplete, red, stale, retried, or authority-mismatched
  lane and every diagnostic-altitude predecessor;
* it binds the exact ordered Gate-1..3 certificate identities as direct
  predecessors;
* it binds the frozen runtime-authority snapshot digest plus the inspected
  runner/store identities every repetition shares;
* both repetition identities/results and their teardown records are bound by
  content digest, and the certificate digest covers the entire envelope.

The record is repository-neutral and does not modify the R21 certificate or
authority surfaces; it consumes their exact models as inputs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Never, Self

from pydantic import Field, model_validator

from agents_remember.certification.certificate_models import (
    GateCertificateIdentity,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.final_codex.models import (
    CERTIFYING_GATE,
    FinalCodexPlanRecord,
    FinalCodexRepetitionResult,
    FinalCodexRunManifest,
    FinalCodexRuntimeAuthorityBinding,
)
from agents_remember.certification.models import CandidateIdentity, CertificationContractFinding
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import FrozenContractModel

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class FinalCodexCertificateEnvelope(FrozenContractModel):
    """The semantic envelope of one immutable Gate-4 final-codex certificate."""

    schemaVersion: Literal["final-codex-gate4-certificate-semantic/v1"] = (
        "final-codex-gate4-certificate-semantic/v1"
    )
    certificateVersion: Literal["1.0.0"] = "1.0.0"
    gate: Literal[4] = CERTIFYING_GATE
    candidateIdentity: CandidateIdentity
    repositoryId: str = Field(pattern=_ID_PATTERN, max_length=128)
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    certificationPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    scenarioVersion: str = Field(pattern=_VERSION_PATTERN)
    planVersion: str = Field(pattern=_VERSION_PATTERN)
    directPredecessors: tuple[GateCertificateIdentity, ...] = Field(max_length=4)
    resultManifestDigests: tuple[str, ...] = Field(min_length=2, max_length=2)
    runtimeAuthority: FinalCodexRuntimeAuthorityBinding
    repetitionResults: tuple[FinalCodexRepetitionResult, ...] = Field(
        min_length=2,
        max_length=2,
    )

    @model_validator(mode="after")
    def _verify_envelope(self) -> Self:
        if tuple(item.gate for item in self.directPredecessors) != (1, 2, 3):
            raise ValueError("Gate-4 predecessors must be the exact ordered Gate-1..3 prefix")
        if tuple(item.repetitionNumber for item in self.repetitionResults) != (1, 2):
            raise ValueError("certificate repetitions must be the exact ordered slots one and two")
        for result in self.repetitionResults:
            if result.acceptanceEligible is not True or result.certifying is not True:
                raise ValueError("only certifying acceptance-eligible repetitions can certify")
            if result.retryCount != 0:
                raise ValueError("a certificate can never bind a retried repetition")
        if self.runtimeAuthority.snapshotDigest not in {
            item.runtimeAuthority.snapshotDigest for item in self.repetitionResults
        }:
            raise ValueError(
                "certificate authority must match the snapshot bound by both repetitions"
            )
        return self


class FinalCodexGateFourCertificate(FrozenContractModel):
    """One immutable bound Gate-4 certificate for the exact two-fresh lane."""

    schemaVersion: Literal["final-codex-gate4-certificate/v1"] = "final-codex-gate4-certificate/v1"
    semanticEnvelope: FinalCodexCertificateEnvelope
    certificateDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        if self.certificateDigest != content_digest(self.semanticEnvelope):
            raise ValueError("final-codex certificate digest does not match its envelope")
        return self


def compile_gate_four_certificate(
    plan: FinalCodexPlanRecord,
    manifest: FinalCodexRunManifest,
    predecessorCertificates: Sequence[GateCertificateIdentity],
    *,
    repository_id: str,
) -> FinalCodexGateFourCertificate:
    """Compile the one bound Gate-4 certificate from the exact two-fresh run.

    Refuses a non-green run (one pass cannot compensate the other), a stale or
    differently bound manifest, a diagnostic predecessor, or any mismatch in
    plan, candidate, profile, scenario, or runtime authority.
    """

    ordered = tuple(predecessorCertificates)
    if tuple(item.gate for item in ordered) != (1, 2, 3):
        _raise_certificate(
            "final-codex certificate publication refused",
            "final-codex-predecessor-prefix-invalid",
            "directPredecessors",
            "the Gate-4 certificate requires the exact ordered Gate-1..3 certificate identities",
        )
    if manifest.attempt.candidateIdentity != plan.candidateIdentity:
        _raise_certificate(
            "final-codex certificate publication refused",
            "final-codex-certificate-candidate-mismatch",
            "candidateIdentity",
            "the run manifest must bind the exact frozen candidate",
        )
    if manifest.attempt.state != "terminal" or not manifest.complete:
        _raise_certificate(
            "final-codex certificate publication refused",
            "final-codex-certificate-run-incomplete",
            "manifest",
            "only a complete terminal two-repetition run can publish a certificate",
        )
    if manifest.aggregate != "green":
        _raise_certificate(
            "final-codex certificate publication refused",
            "final-codex-certificate-not-two-fresh-pass",
            "manifest.aggregate",
            "one passing repetition can never compensate for the other",
        )
    if (
        manifest.attempt.registryDigest != plan.registryDigest
        or manifest.attempt.certifyingPlanDigest != plan.certifyingPlanDigest
        or manifest.attempt.gatePlanDigest != plan.gatePlanDigest
        or manifest.attempt.scenarioVersion != plan.scenarioVersion
        or manifest.attempt.profileId != plan.profileId
    ):
        _raise_certificate(
            "final-codex certificate publication refused",
            "final-codex-certificate-plan-mismatch",
            "manifest.attempt",
            "the run manifest must bind the exact frozen plan record",
        )
    envelope_payload = {
        "schemaVersion": "final-codex-gate4-certificate-semantic/v1",
        "certificateVersion": "1.0.0",
        "gate": 4,
        "candidateIdentity": plan.candidateIdentity.model_dump(mode="json"),
        "repositoryId": repository_id,
        "profileId": plan.profileId,
        "certificationPlanDigest": plan.certifyingPlanDigest,
        "registryDigest": plan.registryDigest,
        "gatePlanDigest": plan.gatePlanDigest,
        "scenarioVersion": plan.scenarioVersion,
        "planVersion": plan.planVersion,
        "directPredecessors": [item.model_dump(mode="json") for item in ordered],
        "resultManifestDigests": [
            item.manifest.manifestDigest if item.manifest is not None else ""
            for item in manifest.repetitions
        ],
        "runtimeAuthority": manifest.repetitions[0].runtimeAuthority.model_dump(mode="json"),
        "repetitionResults": [item.model_dump(mode="json") for item in manifest.repetitions],
    }
    if any(not digest for digest in envelope_payload["resultManifestDigests"]):
        _raise_certificate(
            "final-codex certificate publication refused",
            "final-codex-certificate-manifest-missing",
            "resultManifestDigests",
            "passing repetitions must bind their complete result manifests",
        )
    envelope = FinalCodexCertificateEnvelope(**envelope_payload)
    return FinalCodexGateFourCertificate(
        semanticEnvelope=envelope,
        certificateDigest=content_digest(envelope),
    )


def _raise_certificate(detail: str, code: str, path: str, message: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=message)
    raise CertificationContractError(
        f"{detail}: {message}",
        (finding.model_dump(mode="json"),),
    )


__all__ = [
    "FinalCodexCertificateEnvelope",
    "FinalCodexGateFourCertificate",
    "compile_gate_four_certificate",
]
