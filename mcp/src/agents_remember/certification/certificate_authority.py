"""Issuance and finalization authority for gate certificates."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Never

from agents_remember.certification.certificate_admission import (
    admitted_gate_identity,
    canonicalize_certificate_inputs,
    gate_semantic_digest,
)
from agents_remember.certification.certificate_models import (
    AdmissionGateIdentity,
    CertificateArtifactInventory,
    CertificateEvidenceInventory,
    CertificateInputIdentity,
    CertificateRailInventory,
    CertificationAdmissionManifest,
    ConsumedArtifactIdentity,
    CreationProvenance,
    FinalizationCertificateAuthority,
    FinalizationCurrentInputs,
    FinalizationSemanticEnvelope,
    GateCertificate,
    GateCertificateIssuanceContext,
    GateCertificateSemanticEnvelope,
    GateFiveSemanticInputs,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CertificationContractFinding,
    GatePlan,
    GateResultManifest,
)
from agents_remember.errors import CertificationContractError


def compile_gate_certificate(
    admission: CertificationAdmissionManifest,
    gate_plan: GatePlan,
    result_manifest: GateResultManifest,
    predecessors: Sequence[GateCertificate],
    issuance: GateCertificateIssuanceContext,
) -> GateCertificate:
    """Issue one green certificate from exact current plans and predecessors."""

    gate_identity = admitted_gate_identity(admission, gate_plan.gate)
    _require_gate_result_authority(admission, gate_identity, gate_plan, result_manifest)
    _require_current_predecessors(admission, gate_plan.gate, predecessors)
    if result_manifest.disposition != "green":
        _raise(
            "gate certificate publication refused",
            "non-green-result-manifest",
            "resultManifest.disposition",
            "only a complete green gate result can publish a reusable certificate",
        )
    if result_manifest.profileKind != "certifying" or result_manifest.altitude != "certifying":
        _raise(
            "gate certificate publication refused",
            "diagnostic-certificate-promotion",
            "resultManifest.altitude",
            "diagnostic or mixed-altitude results cannot publish a green certificate",
        )
    if (gate_plan.gate == 5) != (issuance.gateFiveInputs is not None):
        _raise(
            "gate certificate publication refused",
            "gate-five-input-mismatch",
            "gateFiveInputs",
            "Gate 5 requires exact memory inputs and Gates 1-4 forbid them",
        )
    consumed = _bind_consumed_artifacts(gate_plan, result_manifest, predecessors)
    if (
        gate_plan.gate == 3
        and any(rail.applicability.status == "applicable" for rail in gate_plan.rails)
        and (not consumed or any(item.producerGate != 2 for item in consumed))
    ):
        _raise(
            "gate certificate publication refused",
            "gate-three-suite-artifacts-unbound",
            "consumedArtifacts",
            "Gate 3 must bind at least one exact artifact from the green Gate-2 certificate",
        )
    semantic_inputs = list(gate_identity.semanticInputs)
    if issuance.gateFiveInputs is not None:
        semantic_inputs.extend(_gate_five_input_identities(issuance.gateFiveInputs))
    envelope = GateCertificateSemanticEnvelope(
        gate=gate_plan.gate,
        repositoryId=admission.semanticEnvelope.repositoryId,
        candidateCodeTree=admission.semanticEnvelope.candidateCodeTree,
        admissionDigest=admission.admissionDigest,
        admittedProfileDigest=admission.semanticEnvelope.admittedProfileDigest,
        registryDigest=admission.semanticEnvelope.registryDigest,
        gatePlanDigest=gate_identity.gatePlanDigest,
        gateSemanticDigest=gate_identity.gateSemanticDigest,
        repositoryGatePlanDigest=gate_identity.repositoryGatePlanDigest,
        directPredecessors=tuple(item.identity for item in predecessors),
        semanticInputs=canonicalize_certificate_inputs(semantic_inputs),
        consumedArtifacts=consumed,
        resultManifestDigest=result_manifest.manifestDigest,
        railInventory=_rail_inventory(result_manifest),
        artifactInventory=_artifact_inventory(result_manifest),
        evidenceInventory=_evidence_inventory(result_manifest),
        gateFiveInputs=issuance.gateFiveInputs,
    )
    return GateCertificate(
        semanticEnvelope=envelope,
        certificateDigest=content_digest(envelope),
        provenance=issuance.provenance,
    )


def validate_certificate_chain(
    admission: CertificationAdmissionManifest,
    certificates: Sequence[GateCertificate],
    *,
    gate_five_inputs: GateFiveSemanticInputs | None = None,
) -> tuple[GateCertificate, ...]:
    """Validate one exact prefix against current gate-local semantic inputs."""

    ordered = tuple(certificates)
    if tuple(item.semanticEnvelope.gate for item in ordered) != tuple(range(1, len(ordered) + 1)):
        _raise(
            "gate certificate chain refused",
            "certificate-prefix-invalid",
            "certificates",
            "certificate chain must be one exact ordered prefix beginning at Gate 1",
        )
    for index, certificate in enumerate(ordered):
        _require_current_certificate(
            admission,
            certificate,
            predecessors=ordered[:index],
            gate_five_inputs=gate_five_inputs,
        )
    return ordered


def compile_finalization_authority(
    admission: CertificationAdmissionManifest,
    certificates: Sequence[GateCertificate],
    current_inputs: FinalizationCurrentInputs,
    provenance: CreationProvenance,
) -> FinalizationCertificateAuthority:
    """Bind current certificates to transactional authority without rerunning a gate."""

    chain = validate_certificate_chain(
        admission,
        certificates,
        gate_five_inputs=current_inputs.gateFiveInputs,
    )
    if len(chain) != 5:
        _raise(
            "finalization authority publication refused",
            "certificate-chain-incomplete",
            "certificates",
            "transactional finalization requires exact green Gates 1-5",
        )
    envelope = FinalizationSemanticEnvelope(
        repositoryId=admission.semanticEnvelope.repositoryId,
        candidateCodeTree=admission.semanticEnvelope.candidateCodeTree,
        candidateMemoryTree=current_inputs.gateFiveInputs.memoryTree,
        admissionDigest=admission.admissionDigest,
        certificates=tuple(item.identity for item in chain),
        candidatePairAuthorityDigest=current_inputs.gateFiveInputs.candidatePairAuthorityDigest,
        taskIntentAuthorityDigest=current_inputs.taskIntentAuthorityDigest,
        journalAuthorityDigest=current_inputs.journalAuthorityDigest,
    )
    return FinalizationCertificateAuthority(
        semanticEnvelope=envelope,
        authorityDigest=content_digest(envelope),
        provenance=provenance,
    )


def validate_finalization_currentness(
    authority: FinalizationCertificateAuthority,
    admission: CertificationAdmissionManifest,
    certificates: Sequence[GateCertificate],
    current_inputs: FinalizationCurrentInputs,
) -> FinalizationCertificateAuthority:
    """Rebuild finalization authority exactly; unchanged recovery starts zero gates."""

    expected = compile_finalization_authority(
        admission,
        certificates,
        current_inputs,
        authority.provenance,
    )
    if authority != expected:
        _raise(
            "finalization authority is stale",
            "finalization-authority-mismatch",
            "finalizationAuthority",
            "current certificates, candidate pair, task intent, or journal authority changed",
        )
    return authority


def _require_gate_result_authority(
    admission: CertificationAdmissionManifest,
    gate_identity: AdmissionGateIdentity,
    gate_plan: GatePlan,
    manifest: GateResultManifest,
) -> None:
    expected = (
        gate_identity.gate,
        gate_identity.gatePlanDigest,
        gate_identity.gateSemanticDigest,
        admission.semanticEnvelope.registryDigest,
        admission.semanticEnvelope.candidateCodeTree,
        admission.semanticEnvelope.profileId,
        admission.semanticEnvelope.certificationPlanDigest,
    )
    observed = (
        gate_plan.gate,
        gate_plan.planDigest,
        gate_semantic_digest(gate_plan),
        manifest.registryDigest,
        manifest.candidateIdentity,
        manifest.profileId,
        manifest.certificationPlanDigest,
    )
    if observed != expected or manifest.gatePlanDigest != gate_plan.planDigest:
        _raise(
            "gate certificate publication refused",
            "result-manifest-authority-mismatch",
            f"gates.{gate_plan.gate}",
            "result manifest is not bound to the exact admitted candidate and gate plan",
        )


def _require_current_predecessors(
    admission: CertificationAdmissionManifest,
    gate: int,
    predecessors: Sequence[GateCertificate],
) -> None:
    expected_gates = tuple(range(1, gate))
    if tuple(item.semanticEnvelope.gate for item in predecessors) != expected_gates:
        _raise(
            "gate certificate publication refused",
            "predecessor-certificate-mismatch",
            "directPredecessors",
            "caller must provide the exact complete earlier-gate certificate prefix",
        )
    for index, certificate in enumerate(predecessors):
        _require_current_certificate(
            admission,
            certificate,
            predecessors=predecessors[:index],
            gate_five_inputs=None,
        )


def _require_current_certificate(
    admission: CertificationAdmissionManifest,
    certificate: GateCertificate,
    *,
    predecessors: Sequence[GateCertificate],
    gate_five_inputs: GateFiveSemanticInputs | None,
) -> None:
    envelope = certificate.semanticEnvelope
    admitted = admitted_gate_identity(admission, envelope.gate)
    expected_inputs = list(admitted.semanticInputs)
    if envelope.gate == 5:
        if gate_five_inputs is None:
            _raise(
                "gate certificate chain refused",
                "gate-five-current-inputs-missing",
                "gateFiveInputs",
                "current Gate-5 memory inputs are required to prove certificate reuse",
            )
        expected_inputs.extend(_gate_five_input_identities(gate_five_inputs))
    expected = (
        admission.semanticEnvelope.repositoryId,
        admission.semanticEnvelope.candidateCodeTree,
        admitted.gateSemanticDigest,
        admitted.repositoryGatePlanDigest,
        canonicalize_certificate_inputs(expected_inputs),
        tuple(item.identity for item in predecessors),
        gate_five_inputs if envelope.gate == 5 else None,
    )
    observed = (
        envelope.repositoryId,
        envelope.candidateCodeTree,
        envelope.gateSemanticDigest,
        envelope.repositoryGatePlanDigest,
        envelope.semanticInputs,
        envelope.directPredecessors,
        envelope.gateFiveInputs,
    )
    if observed != expected:
        _raise(
            "gate certificate chain refused",
            "stale-gate-certificate",
            f"certificates.{envelope.gate}",
            "certificate does not match the current gate-local inputs and exact predecessors",
        )


def _bind_consumed_artifacts(
    gate_plan: GatePlan,
    manifest: GateResultManifest,
    predecessors: Sequence[GateCertificate],
) -> tuple[ConsumedArtifactIdentity, ...]:
    produced_here = {
        artifact.artifactId for result in manifest.railResults for artifact in result.artifacts
    }
    required = {
        artifact
        for rail in gate_plan.rails
        if rail.applicability.status == "applicable"
        for artifact in rail.requiredArtifacts
        if artifact not in produced_here
    }
    available: dict[str, list[tuple[GateCertificate, CertificateArtifactInventory]]] = {}
    for certificate in predecessors:
        for artifact in certificate.semanticEnvelope.artifactInventory:
            available.setdefault(artifact.artifactId, []).append((certificate, artifact))
    consumed: list[ConsumedArtifactIdentity] = []
    for artifact_id in sorted(required):
        matches = available.get(artifact_id, [])
        if len(matches) != 1:
            _raise(
                "gate certificate publication refused",
                "required-artifact-not-exact",
                f"consumedArtifacts.{artifact_id}",
                "required artifact must resolve to exactly one earlier green certificate",
            )
        certificate, artifact = matches[0]
        consumed.append(
            ConsumedArtifactIdentity(
                artifactId=artifact.artifactId,
                producerGate=certificate.semanticEnvelope.gate,
                producerCertificateDigest=certificate.certificateDigest,
                sha256=artifact.sha256,
                size=artifact.size,
            )
        )
    return tuple(consumed)


def _rail_inventory(manifest: GateResultManifest) -> tuple[CertificateRailInventory, ...]:
    return tuple(
        sorted(
            (
                CertificateRailInventory(
                    rail=result.rail,
                    resultDigest=result.resultDigest,
                    status=result.status,
                    code=result.code,
                    correctiveOwner=result.correctiveOwner,
                )
                for result in manifest.railResults
            ),
            key=lambda item: item.rail.key,
        )
    )


def _artifact_inventory(manifest: GateResultManifest) -> tuple[CertificateArtifactInventory, ...]:
    return tuple(
        sorted(
            (
                CertificateArtifactInventory(
                    rail=result.rail,
                    artifactId=artifact.artifactId,
                    sha256=artifact.sha256,
                    size=artifact.size,
                    evidenceRef=artifact.evidenceRef,
                )
                for result in manifest.railResults
                for artifact in result.artifacts
            ),
            key=lambda item: item.key,
        )
    )


def _evidence_inventory(manifest: GateResultManifest) -> tuple[CertificateEvidenceInventory, ...]:
    return tuple(
        sorted(
            (
                CertificateEvidenceInventory(
                    rail=result.rail,
                    evidenceId=evidence.evidenceId,
                    sha256=evidence.sha256,
                    size=evidence.size,
                    reference=evidence.reference,
                )
                for result in manifest.railResults
                for evidence in result.evidence
            ),
            key=lambda item: item.key,
        )
    )


def _gate_five_input_identities(
    inputs: GateFiveSemanticInputs,
) -> tuple[CertificateInputIdentity, ...]:
    identities = [
        CertificateInputIdentity(
            inputKind="memory-tree",
            inputId=inputs.memoryTree.kind,
            contentDigest=content_digest(inputs.memoryTree),
        ),
        CertificateInputIdentity(
            inputKind="memory-scope-plan",
            inputId="affected-closure",
            contentDigest=inputs.affectedClosurePlanDigest,
        ),
        CertificateInputIdentity(
            inputKind="memory-checker-registry",
            inputId="checker-registry",
            contentDigest=inputs.memoryCheckerRegistryDigest,
        ),
        CertificateInputIdentity(
            inputKind="candidate-pair-authority",
            inputId="code-memory-pair",
            contentDigest=inputs.candidatePairAuthorityDigest,
        ),
    ]
    identities.extend(
        CertificateInputIdentity(
            inputKind="coherence-subrecord",
            inputId=item.subrecordId,
            contentDigest=item.contentDigest,
        )
        for item in inputs.coherenceSubrecords
    )
    return canonicalize_certificate_inputs(identities)


def _raise(detail: str, code: str, path: str, finding_detail: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=finding_detail)
    raise CertificationContractError(detail, (finding.model_dump(mode="json"),))


__all__ = [
    "compile_finalization_authority",
    "compile_gate_certificate",
    "validate_certificate_chain",
    "validate_finalization_currentness",
]
