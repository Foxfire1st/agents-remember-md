"""Certificate reuse and exact durable finalization-leg recovery for R05."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Never

from agents_remember.certification.certificate_authority import (
    compile_finalization_authority,
    validate_finalization_currentness,
)
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    plan_certificate_reuse,
)
from agents_remember.certification.certificate_models import (
    CreationProvenance,
    FinalizationCurrentInputs,
    GateCertificate,
    GateFiveSemanticInputs,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.lifecycle_admission import CompiledLifecycleAdmission
from agents_remember.certification.lifecycle_models import (
    CertificationRecoveryRecord,
    CertificationRecoverySemanticEnvelope,
    FinalizationBoundaryObservation,
    FinalizationJournalState,
    FinalizationLeg,
    LifecycleFinalizationManifest,
    LifecycleFinalizationSemanticEnvelope,
)
from agents_remember.errors import CertificationContractError


@dataclass(frozen=True)
class FinalizationBoundaryInputs:
    admission: CompiledLifecycleAdmission
    certificates: tuple[GateCertificate, ...]
    currentInputs: FinalizationCurrentInputs
    observation: FinalizationBoundaryObservation
    journal: FinalizationJournalState


@dataclass(frozen=True)
class _RefusalEvidence:
    detail: str
    expected: object
    observed: object


def compile_certification_recovery_record(
    admission: CompiledLifecycleAdmission,
    certificates: Sequence[GateCertificate],
    changes: Sequence[CertificateInputChange],
    *,
    provenance: CreationProvenance,
    gate_five_inputs: GateFiveSemanticInputs | None = None,
) -> CertificationRecoveryRecord:
    """Journal the exact R21 reuse decision without searching for another candidate."""

    chain = tuple(certificates)
    ordered_changes = tuple(sorted(changes, key=content_digest))
    reuse = plan_certificate_reuse(
        admission.certification,
        chain,
        ordered_changes,
        gate_five_inputs=gate_five_inputs,
    )
    envelope = CertificationRecoverySemanticEnvelope(
        lifecycleAdmissionDigest=admission.lifecycle.admissionDigest,
        admittedCertificates=tuple(item.identity for item in chain),
        inputChanges=ordered_changes,
        reusePlan=reuse,
    )
    return CertificationRecoveryRecord(
        semanticEnvelope=envelope,
        recoveryDigest=content_digest(envelope),
        provenance=provenance,
    )


def compile_lifecycle_finalization(
    inputs: FinalizationBoundaryInputs,
    *,
    provenance: CreationProvenance,
) -> LifecycleFinalizationManifest:
    """Revalidate the same candidate and bind the exact unfinished publication leg."""

    _require_finalization_alignment(inputs)
    authority = compile_finalization_authority(
        inputs.admission.certification,
        inputs.certificates,
        inputs.currentInputs,
        provenance,
    )
    envelope = LifecycleFinalizationSemanticEnvelope(
        lifecycleAdmissionDigest=inputs.admission.lifecycle.admissionDigest,
        certificateAuthorityDigest=authority.authorityDigest,
        observation=inputs.observation,
        journal=inputs.journal,
        nextLeg=inputs.journal.next_leg,
    )
    return LifecycleFinalizationManifest(
        semanticEnvelope=envelope,
        finalizationDigest=content_digest(envelope),
        provenance=provenance,
    )


def validate_lifecycle_finalization_currentness(
    expected: LifecycleFinalizationManifest,
    inputs: FinalizationBoundaryInputs,
) -> LifecycleFinalizationManifest:
    """Refuse any movement and otherwise resume with zero certification starts."""

    observed = compile_lifecycle_finalization(
        inputs,
        provenance=expected.provenance,
    )
    if observed != expected:
        _refuse(
            "lifecycle finalization is stale",
            "lifecycle-finalization-mismatch",
            "finalization",
            _RefusalEvidence(
                "candidate, certificate, authority, approval, commit intent, or journal moved",
                {"finalizationDigest": expected.finalizationDigest},
                {"finalizationDigest": observed.finalizationDigest},
            ),
        )
    return observed


def authorize_finalization_leg(
    expected: LifecycleFinalizationManifest,
    requested_leg: FinalizationLeg,
    inputs: FinalizationBoundaryInputs,
) -> LifecycleFinalizationManifest:
    """Authorize only the exact journaled leg after a currentness reread."""

    current = validate_lifecycle_finalization_currentness(
        expected,
        inputs,
    )
    next_leg = current.semanticEnvelope.nextLeg
    if requested_leg != next_leg:
        _refuse(
            "finalization write refused",
            "finalization-leg-not-current",
            "finalization.nextLeg",
            _RefusalEvidence(
                "publication may resume only the exact unfinished durable leg",
                {"nextLeg": next_leg},
                {"requestedLeg": requested_leg},
            ),
        )
    return current


def _require_finalization_alignment(
    inputs: FinalizationBoundaryInputs,
) -> None:
    candidate = inputs.admission.lifecycle.semanticEnvelope.candidate
    current_inputs = inputs.currentInputs
    observation = inputs.observation
    if (
        observation.operationStatus not in {"ready", "finalizing"}
        or observation.doorStatus != "claimed"
        or observation.approvalStatus != "current"
    ):
        _refuse(
            "lifecycle finalization refused",
            "finalization-boundary-not-current",
            "finalization.lifecycle",
            _RefusalEvidence(
                "operation, door, and approval must remain current before publication",
                {
                    "operationStatus": ["ready", "finalizing"],
                    "doorStatus": "claimed",
                    "approvalStatus": "current",
                },
                {
                    "operationStatus": observation.operationStatus,
                    "doorStatus": observation.doorStatus,
                    "approvalStatus": observation.approvalStatus,
                },
            ),
        )
    expected = {
        "candidateCodeTree": candidate.candidateCodeTree.model_dump(mode="json"),
        "candidateMemoryTree": current_inputs.gateFiveInputs.memoryTree.model_dump(mode="json"),
        "topologyIdentityDigest": candidate.topologyIdentityDigest,
        "taskIntentIdentityDigest": candidate.taskIntentIdentityDigest,
        "normalizedCommitIntentDigest": candidate.normalizedCommitIntentDigest,
        "journalAuthorityDigest": current_inputs.journalAuthorityDigest,
    }
    observed = {
        "candidateCodeTree": observation.candidateCodeTree.model_dump(mode="json"),
        "candidateMemoryTree": observation.candidateMemoryTree.model_dump(mode="json"),
        "topologyIdentityDigest": observation.topologyIdentityDigest,
        "taskIntentIdentityDigest": observation.taskIntentIdentityDigest,
        "normalizedCommitIntentDigest": observation.normalizedCommitIntentDigest,
        "journalAuthorityDigest": inputs.journal.journalAuthorityDigest,
    }
    if current_inputs.taskIntentAuthorityDigest != observation.taskIntentIdentityDigest:
        observed["finalizationTaskIntentAuthorityDigest"] = current_inputs.taskIntentAuthorityDigest
        expected["finalizationTaskIntentAuthorityDigest"] = candidate.taskIntentIdentityDigest
    if observed != expected:
        _refuse(
            "lifecycle finalization refused",
            "finalization-authority-mismatch",
            "finalization.authority",
            _RefusalEvidence(
                "finalization must revalidate the exact admitted candidate and current journal",
                expected,
                observed,
            ),
        )


def revalidate_certificate_authority(
    expected: LifecycleFinalizationManifest,
    admission: CompiledLifecycleAdmission,
    certificates: Sequence[GateCertificate],
    current_inputs: FinalizationCurrentInputs,
) -> None:
    """Expose the R21 reread without changing finalization or gate state."""

    authority_digest = expected.semanticEnvelope.certificateAuthorityDigest
    authority = compile_finalization_authority(
        admission.certification,
        certificates,
        current_inputs,
        expected.provenance,
    )
    validate_finalization_currentness(
        authority,
        admission.certification,
        certificates,
        current_inputs,
    )
    if authority.authorityDigest != authority_digest:
        _refuse(
            "lifecycle finalization refused",
            "certificate-finalization-authority-mismatch",
            "finalization.certificateAuthorityDigest",
            _RefusalEvidence(
                "the exact R21 finalization authority moved",
                {"authorityDigest": authority_digest},
                {"authorityDigest": authority.authorityDigest},
            ),
        )


def _refuse(
    detail: str,
    code: str,
    path: str,
    evidence: _RefusalEvidence,
) -> Never:
    raise CertificationContractError(
        detail,
        (
            {
                "code": code,
                "path": path,
                "detail": evidence.detail,
                "expected": evidence.expected,
                "observed": evidence.observed,
                "gateStarts": 0,
            },
        ),
    )


__all__ = [
    "FinalizationBoundaryInputs",
    "authorize_finalization_leg",
    "compile_certification_recovery_record",
    "compile_lifecycle_finalization",
    "revalidate_certificate_authority",
    "validate_lifecycle_finalization_currentness",
]
