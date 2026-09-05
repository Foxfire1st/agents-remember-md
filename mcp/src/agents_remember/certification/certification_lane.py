"""Production R11<->R22 certification-lane admission authority (CCR-L28 bridge).

The executable gate flow admits one repository-owned R22 profile and compiles its
Gates 1-4 repository plan, while the R21 certificate layer is keyed to the R11
rail-registry vocabulary and freezes an admission manifest before Gate 1.  This
module is the production bridge between the two authorities:  it derives the
canonical R11 rail registry contribution from the exact admitted R22 repository
plan (the R11 rails the admission alignment contract in
certificate_admission requires to equal the R22 rail contracts for
Gates 1-4) plus the caller-provided Gate-5 memory-domain rails, compiles the
R11 certification plan, and freezes the R21 certification admission manifest.

The derivation is repository-neutral:  the R22 plan rails are already R11
CompiledRail objects (the R22 profile planner projects the repository rail
vocabulary into R11 before planning), so the registry contribution is an
identity projection of the admitted plan plus the memory rails.  Provenance is
deliberately excluded from every semantic digest, so two admissions of the same
candidate/profile/plan produce byte-identical manifest digests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Never

from agents_remember.certification.canonical import canonicalize_registry
from agents_remember.certification.certificate_admission import (
    compile_certification_admission,
)
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    CreationProvenance,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationContractFinding,
    CertificationPlan,
    CompiledRail,
    RailDefinition,
    RailRegistry,
    RegistryProfile,
)
from agents_remember.certification.planning import compile_certification_plan
from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
    RepositoryProfilePlan,
)
from agents_remember.errors import CertificationContractError

_CERTIFYING_KIND = "certifying"
_ALL_GATES = (1, 2, 3, 4, 5)


@dataclass(frozen=True)
class CertificationLane:
    """The exact owner objects one R21 admission freezes for an executable run.

    repositoryPlan is the admitted R22 plan the run executed; registry
    and certificationPlan are the derived R11 authorities; admission is
    the frozen R21 admission manifest binding both authorities.
    """

    registry: CanonicalRailRegistry
    certificationPlan: CertificationPlan
    repositoryPlan: RepositoryProfilePlan
    admission: CertificationAdmissionManifest

    @property
    def candidate(self) -> CandidateIdentity:
        return self.repositoryPlan.candidateIdentity

    @property
    def selectionId(self) -> str:
        return self.repositoryPlan.selectionId


def compile_certification_lane(
    profile: CanonicalRepositoryCertificationProfile,
    repository_plan: RepositoryProfilePlan,
    *,
    provenance: CreationProvenance,
    memory_rails: Sequence[RailDefinition],
    registry_id: str | None = None,
) -> CertificationLane:
    """Admit one exact executable run into the R21 certificate vocabulary.

    The R22 plan must have admitted the exact repository profile and every
    repository gate must be applicable:  a not-applicable repository gate cannot
    contribute an R11 gate plan, so no certifying R21 admission exists for it.
    memory_rails must name the complete Gate-5 memory-domain rail population
    the terminal Gate-5 phase executes (at least one applicable rail), so the
    derived R11 registry yields the required five-gate certification plan.
    """
    _require_applicable_repository_gates(repository_plan)
    _require_memory_rails(memory_rails)
    registry = _compile_registry_contribution(
        profile,
        repository_plan,
        memory_rails=memory_rails,
        registry_id=registry_id,
    )
    certification_plan = compile_certification_plan(
        registry,
        profile_id=repository_plan.selectionId,
        candidate_identity=repository_plan.candidateIdentity,
    )
    admission = compile_certification_admission(
        registry,
        certification_plan,
        profile,
        repository_plan,
        provenance=provenance,
    )
    return CertificationLane(
        registry=registry,
        certificationPlan=certification_plan,
        repositoryPlan=repository_plan,
        admission=admission,
    )


def admit_certification_lane(
    profile: CanonicalRepositoryCertificationProfile,
    lane: CertificationLane,
    *,
    provenance: CreationProvenance,
    memory_rails: Sequence[RailDefinition],
    registry_id: str | None = None,
) -> CertificationLane:
    """Rebuild the exact lane from current inputs and refuse any movement.

    An unchanged interruption must resume with byte-identical admission and
    certificate identities; this compile is the currentness reread that proves
    the recorded lane is still exact before a certificate is reused or a
    finalization leg resumes.
    """
    expected = compile_certification_lane(
        profile,
        lane.repositoryPlan,
        provenance=provenance,
        memory_rails=memory_rails,
        registry_id=registry_id,
    )
    if expected != lane:
        _refuse(
            "certification lane admission is stale",
            "certification-lane-mismatch",
            "certificationLane",
            "profile, repository plan, or candidate moved between attempts",
        )
    return expected


def _compile_registry_contribution(
    profile: CanonicalRepositoryCertificationProfile,
    repository_plan: RepositoryProfilePlan,
    *,
    memory_rails: Sequence[RailDefinition],
    registry_id: str | None,
) -> CanonicalRailRegistry:
    repository_id = profile.profile.repositoryId
    if registry_id is None:
        registry_id = f"{repository_id}-certification-rails"
    rails = tuple(
        _project_repository_rail(rail)
        for gate_plan in repository_plan.gates
        if gate_plan.applicability == "applicable"
        for rail in gate_plan.rails
    )
    registry = RailRegistry(
        registryId=registry_id,
        repositoryId=repository_id,
        profiles=(
            RegistryProfile(
                profileId=repository_plan.selectionId,
                kind=_CERTIFYING_KIND,
                gates=_ALL_GATES,
            ),
        ),
        rails=(*rails, *memory_rails),
    )
    return canonicalize_registry(registry)


def _project_repository_rail(rail: CompiledRail) -> RailDefinition:
    """Identity-projection of one admitted R22 compiled rail into R11 vocabulary.

    The R22 profile planner already produced an R11 CompiledRail; the R11
    registry stores its RailDefinition form with the same adapter, runtime,
    evidence, artifact, and applicability contract so the admission alignment
    check (R11 rails equal R22 rail contracts for Gates 1-4) holds by
    construction.
    """
    return RailDefinition(
        identity=rail.identity,
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
        applicability=(rail.applicability,),
        evidenceContract=rail.evidenceContract,
        outputArtifacts=rail.outputArtifacts,
    )


def _require_applicable_repository_gates(plan: RepositoryProfilePlan) -> None:
    not_applicable = tuple(gate.gate for gate in plan.gates if gate.applicability != "applicable")
    if not_applicable:
        _refuse(
            "certification lane admission refused",
            "repository-gate-not-applicable",
            f"gates.{not_applicable[0]}",
            "a not-applicable repository gate has no R11 rail contribution, so the "
            "exact certifying five-gate admission cannot be compiled for this selection",
        )


def _require_memory_rails(memory_rails: Sequence[RailDefinition]) -> None:
    if not memory_rails:
        _refuse(
            "certification lane admission refused",
            "gate-five-memory-rails-missing",
            "memoryRails",
            "the R11 registry requires the complete Gate-5 memory-domain rail "
            "population before a certifying admission can be compiled",
        )
    invalid = tuple(
        rail for rail in memory_rails if rail.gate != 5 or rail.authority != "memory-domain"
    )
    if invalid:
        _refuse(
            "certification lane admission refused",
            "gate-five-memory-rail-invalid",
            f"memoryRails.{invalid[0].identity.key}",
            "every Gate-5 memory rail must be memory-domain authority for gate 5",
        )


def _refuse(detail: str, code: str, path: str, finding_detail: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=finding_detail)
    raise CertificationContractError(detail, (finding.model_dump(mode="json"),))


__all__ = [
    "CertificationLane",
    "admit_certification_lane",
    "compile_certification_lane",
]
