"""Admission compilation for content-addressed gate certificates."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Never

from agents_remember.certification.certificate_models import (
    AdmissionGateIdentity,
    CertificateInputIdentity,
    CertificationAdmissionManifest,
    CertificationAdmissionSemanticEnvelope,
    CreationProvenance,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationContractFinding,
    CertificationPlan,
    CompiledRail,
    GatePlan,
)
from agents_remember.certification.planning import admit_certification_plan
from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
    RepositoryGatePlan,
    RepositoryProfilePlan,
)
from agents_remember.certification.repository_profiles.planning import (
    admit_repository_profile_plan,
)
from agents_remember.errors import CertificationContractError


def compile_certification_admission(
    registry: CanonicalRailRegistry,
    certification_plan: CertificationPlan,
    repository_profile: CanonicalRepositoryCertificationProfile,
    repository_plan: RepositoryProfilePlan,
    *,
    provenance: CreationProvenance,
) -> CertificationAdmissionManifest:
    """Freeze the exact dual-authority inputs before Gate 1 can start."""

    candidate = certification_plan.candidateIdentity
    _require_git_tree(candidate)
    admit_certification_plan(
        registry,
        certification_plan,
        profile_id=certification_plan.profileId,
        candidate_identity=candidate,
    )
    admit_repository_profile_plan(
        repository_profile,
        repository_plan,
        selection_id=repository_plan.selectionId,
        candidate_identity=candidate,
    )
    _require_admission_identity_alignment(
        registry,
        certification_plan,
        repository_profile,
        repository_plan,
    )
    repository_gates = {gate.gate: gate for gate in repository_plan.gates}
    gate_identities = tuple(
        _compile_admission_gate(gate, repository_gates.get(gate.gate))
        for gate in certification_plan.gates
    )
    envelope = CertificationAdmissionSemanticEnvelope(
        repositoryId=registry.registry.repositoryId,
        candidateCodeTree=candidate,
        profileId=certification_plan.profileId,
        certificationPlanDigest=certification_plan.planDigest,
        admittedProfileDigest=repository_profile.profileDigest,
        registryDigest=registry.registryDigest,
        gates=gate_identities,
    )
    return CertificationAdmissionManifest(
        semanticEnvelope=envelope,
        admissionDigest=content_digest(envelope),
        provenance=provenance,
    )


def admitted_gate_identity(
    admission: CertificationAdmissionManifest,
    gate: int,
) -> AdmissionGateIdentity:
    """Resolve one gate only from the exact admitted manifest."""

    return next(item for item in admission.semanticEnvelope.gates if item.gate == gate)


def canonicalize_certificate_inputs(
    inputs: Iterable[CertificateInputIdentity],
) -> tuple[CertificateInputIdentity, ...]:
    """Canonicalize unique semantic inputs and reject digest conflicts."""

    catalog: dict[tuple[str, str], CertificateInputIdentity] = {}
    for item in inputs:
        prior = catalog.get(item.key)
        if prior is not None and prior != item:
            _raise(
                "certificate semantic input compilation failed",
                "semantic-input-conflict",
                f"semanticInputs.{item.inputKind}.{item.inputId}",
                "one semantic input identity resolved to conflicting content digests",
            )
        catalog[item.key] = item
    return tuple(sorted(catalog.values(), key=lambda item: (*item.key, item.contentDigest)))


def gate_semantic_digest(gate: GatePlan) -> str:
    """Digest gate-local plan semantics without aggregate registry identity."""

    payload = gate.model_dump(mode="json", exclude={"planDigest", "registryDigest"})
    return content_digest(payload)


def _require_admission_identity_alignment(
    registry: CanonicalRailRegistry,
    certification_plan: CertificationPlan,
    repository_profile: CanonicalRepositoryCertificationProfile,
    repository_plan: RepositoryProfilePlan,
) -> None:
    expected = (
        registry.registry.repositoryId,
        certification_plan.candidateIdentity,
        certification_plan.profileId,
    )
    observed = (
        repository_profile.profile.repositoryId,
        repository_plan.candidateIdentity,
        repository_plan.selectionId,
    )
    if observed != expected:
        _raise(
            "certification admission failed",
            "profile-registry-identity-mismatch",
            "admission",
            "R11 registry and R22 profile must select the same repository, candidate, and profile",
        )
    repository_gates = {gate.gate: gate for gate in repository_plan.gates}
    for gate in certification_plan.gates[:4]:
        _require_rail_alignment(gate, repository_gates[gate.gate])


def _require_rail_alignment(generic: GatePlan, repository: RepositoryGatePlan) -> None:
    if repository.applicability == "not-applicable":
        if any(rail.applicability.status != "not-applicable" for rail in generic.rails):
            _raise(
                "certification admission failed",
                "gate-applicability-mismatch",
                f"gates.{generic.gate}",
                "R11 and R22 disagree about gate applicability",
            )
        return
    generic_rails = {_rail_contract(rail) for rail in generic.rails}
    repository_rails = {_rail_contract(rail) for rail in repository.rails}
    if generic_rails != repository_rails:
        _raise(
            "certification admission failed",
            "profile-registry-rail-mismatch",
            f"gates.{generic.gate}.rails",
            "R11 and R22 must contribute the same exact rail contracts",
        )


def _rail_contract(rail: CompiledRail) -> str:
    payload = rail.model_dump(mode="json", exclude={"definitionDigest"})
    applicability = payload["applicability"]
    assert isinstance(applicability, dict)
    applicability.pop("profileId")
    return content_digest(payload)


def _compile_admission_gate(
    gate: GatePlan,
    repository_gate: RepositoryGatePlan | None,
) -> AdmissionGateIdentity:
    if (gate.gate <= 4) != (repository_gate is not None):
        raise ValueError("certification admission has an incomplete repository gate plan")
    inputs = _generic_gate_inputs(gate)
    repository_digest = None
    if repository_gate is not None:
        inputs.extend(
            CertificateInputIdentity(
                inputKind=f"repository-{item.inputKind}",
                inputId=item.inputId,
                contentDigest=item.contentDigest,
            )
            for item in repository_gate.semanticInputs
        )
        repository_digest = repository_gate.planDigest
    return AdmissionGateIdentity(
        gate=gate.gate,
        gatePlanDigest=gate.planDigest,
        gateSemanticDigest=gate_semantic_digest(gate),
        repositoryGatePlanDigest=repository_digest,
        semanticInputs=canonicalize_certificate_inputs(inputs),
    )


def _generic_gate_inputs(gate: GatePlan) -> list[CertificateInputIdentity]:
    inputs: list[CertificateInputIdentity] = []
    for rail in gate.rails:
        inputs.extend(
            (
                CertificateInputIdentity(
                    inputKind="rail-definition",
                    inputId=rail.identity.key,
                    contentDigest=rail.definitionDigest,
                ),
                CertificateInputIdentity(
                    inputKind="rail-adapter",
                    inputId=rail.identity.key,
                    contentDigest=rail.adapter.configurationDigest,
                ),
                CertificateInputIdentity(
                    inputKind="rail-runtime",
                    inputId=rail.identity.key,
                    contentDigest=content_digest(rail.runtimeInputs),
                ),
                CertificateInputIdentity(
                    inputKind="selection-scope",
                    inputId=rail.identity.key,
                    contentDigest=content_digest(rail.applicability),
                ),
                CertificateInputIdentity(
                    inputKind="artifact-dependency",
                    inputId=rail.identity.key,
                    contentDigest=content_digest(
                        {"requiredArtifacts": list(rail.requiredArtifacts)}
                    ),
                ),
            )
        )
    return inputs


def _require_git_tree(candidate: CandidateIdentity) -> None:
    if candidate.kind != "git-tree":
        _raise(
            "certification admission failed",
            "candidate-code-tree-required",
            "candidateIdentity",
            "certification requires an exact Git tree rather than a caller-defined identity",
        )


def _raise(detail: str, code: str, path: str, finding_detail: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=finding_detail)
    raise CertificationContractError(detail, (finding.model_dump(mode="json"),))


__all__ = [
    "admitted_gate_identity",
    "canonicalize_certificate_inputs",
    "compile_certification_admission",
    "gate_semantic_digest",
]
