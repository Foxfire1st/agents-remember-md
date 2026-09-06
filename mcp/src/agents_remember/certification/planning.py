"""Immutable per-gate plan compilation from one admitted rail registry."""

from __future__ import annotations

from typing import Never

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationPlan,
    CompiledRail,
    GatePlan,
    RailDefinition,
    RegistryProfile,
    RegistryValidationFinding,
    canonical_execution_waves,
)
from agents_remember.certification.validation import validate_registry
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import GateId


def compile_certification_plan(
    registry: CanonicalRailRegistry,
    *,
    profile_id: str,
    candidate_identity: CandidateIdentity,
) -> CertificationPlan:
    """Compile all gates selected by one validated repository profile."""

    report = validate_registry(registry)
    if not report.ok:
        _raise_contract_error("rail registry admission failed", report.findings)
    profile = next(
        (item for item in registry.registry.profiles if item.profileId == profile_id),
        None,
    )
    if profile is None:
        _raise_contract_error(
            "rail registry profile selection failed",
            (
                RegistryValidationFinding(
                    code="unknown-profile",
                    path=f"profiles.{profile_id}",
                    detail="selected profile is not declared",
                ),
            ),
        )
    gate_plans = tuple(
        _compile_gate_plan(
            registry,
            profile,
            gate=gate,
            candidate_identity=candidate_identity,
        )
        for gate in sorted(profile.gates)
    )
    payload = {
        "schemaVersion": "closeout-certification-plan/v1",
        "registryDigest": registry.registryDigest,
        "candidateIdentity": candidate_identity.model_dump(mode="json"),
        "profileId": profile.profileId,
        "profileKind": profile.kind,
        "gates": [item.model_dump(mode="json") for item in gate_plans],
    }
    return CertificationPlan(**payload, planDigest=content_digest(payload))


def admit_certification_plan(
    registry: CanonicalRailRegistry,
    plan: CertificationPlan,
    *,
    profile_id: str,
    candidate_identity: CandidateIdentity,
) -> CertificationPlan:
    """Bind a reconstructed plan to one registry-owned profile and candidate."""

    expected = compile_certification_plan(
        registry,
        profile_id=profile_id,
        candidate_identity=candidate_identity,
    )
    if plan != expected:
        _raise_contract_error(
            "certification plan admission failed",
            (
                RegistryValidationFinding(
                    code="certification-plan-not-authorized",
                    path="plan",
                    detail=(
                        "plan bytes do not equal the canonical registry compilation for the "
                        "selected profile and candidate"
                    ),
                ),
            ),
        )
    return plan


def _compile_gate_plan(
    registry: CanonicalRailRegistry,
    profile: RegistryProfile,
    *,
    gate: GateId,
    candidate_identity: CandidateIdentity,
) -> GatePlan:
    definitions = _selected_gate_definitions(registry, profile.profileId, gate)
    compiled = tuple(_compile_rail(rail, profile.profileId) for rail in definitions)
    waves = canonical_execution_waves(compiled)
    payload = {
        "schemaVersion": "closeout-gate-plan/v1",
        "registryDigest": registry.registryDigest,
        "candidateIdentity": candidate_identity.model_dump(mode="json"),
        "profileId": profile.profileId,
        "profileKind": profile.kind,
        "gate": gate,
        "gatePrerequisites": tuple(item for item in sorted(profile.gates) if item < gate),
        "rails": [item.model_dump(mode="json") for item in compiled],
        "waves": [[identity.model_dump(mode="json") for identity in wave] for wave in waves],
    }
    return GatePlan(**payload, planDigest=content_digest(payload))


def _selected_gate_definitions(
    registry: CanonicalRailRegistry,
    profile_id: str,
    gate: GateId,
) -> tuple[RailDefinition, ...]:
    return tuple(
        rail
        for rail in registry.registry.rails
        if rail.gate == gate and any(item.profileId == profile_id for item in rail.applicability)
    )


def _compile_rail(rail: RailDefinition, profile_id: str) -> CompiledRail:
    applicability = next(item for item in rail.applicability if item.profileId == profile_id)
    return CompiledRail(
        identity=rail.identity,
        definitionDigest=content_digest(rail),
        gate=rail.gate,
        railClass=rail.railClass,
        authority=rail.authority,
        ownerClass=rail.ownerClass,
        correctiveOwner=rail.correctiveOwner,
        posture=rail.posture,
        orderKey=rail.orderKey,
        prerequisites=rail.prerequisites,
        requiredArtifacts=rail.requiredArtifacts,
        adapter=rail.adapter,
        runtimeInputs=rail.runtimeInputs,
        applicability=applicability,
        evidenceContract=rail.evidenceContract,
        outputArtifacts=rail.outputArtifacts,
    )


def _raise_contract_error(
    detail: str,
    findings: tuple[RegistryValidationFinding, ...],
) -> Never:
    raise CertificationContractError(
        detail,
        [item.model_dump(mode="json") for item in findings],
    )
