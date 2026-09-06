"""Standalone CCR-R13 diagnostic plan projection tests.

The diagnostic plan must project the exact R11 canonical scenario rail at
diagnostic altitude: same rail identity, posture, and applicability semantics
as the certifying plan, never a second scenario implementation.  Refusals cover
non-diagnostic profiles, unplanned scenario gates, missing certifying plans,
and rail-catalog drift.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from agents_remember.certification import canonicalize_registry
from agents_remember.certification.certificate_models import CandidateIdentity
from agents_remember.certification.diagnostics.planning import (
    compile_diagnostic_plan,
    diagnostic_scenario_gate,
)
from agents_remember.certification.models import (
    ArtifactDeclaration,
    CanonicalRailRegistry,
    CertificationPlan,
    RailAdapterDefinition,
    RailApplicability,
    RailClass,
    RailDefinition,
    RailEvidenceContract,
    RailPosture,
    RailRegistry,
    RailRuntimeInputs,
    RegistryProfile,
)
from agents_remember.certification.planning import compile_certification_plan
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import GateId, RailIdentity

CERT_PROFILE = "portable-ci"
DIAG_PROFILE = "diagnostic-ci"
DIGEST = "a" * 64
PREREQUISITES: dict[str, str] = {
    "suite": "lint",
    "coverage": "suite",
    "e2e": "coverage",
    "memory": "e2e",
}

CANDIDATE = CandidateIdentity(kind="git-tree", value="c" * 40)
CLASS_BY_GATE: dict[GateId, RailClass] = {
    1: "pre-test-quality",
    2: "ordinary-test-suite",
    3: "post-test-quality",
    4: "integration-test",
    5: "memory-quality",
}


@dataclass(frozen=True)
class RailSpec:
    rail_id: str
    gate: GateId
    posture: RailPosture = "enforcing"
    diag_gate4_only_extra: bool = False
    diag_not_applicable: bool = False


def _identity(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def _rail(spec: RailSpec) -> RailDefinition:
    rows: list[RailApplicability] = []
    for profile_id, gate in ((CERT_PROFILE, spec.gate), (DIAG_PROFILE, spec.gate)):
        if spec.diag_gate4_only_extra and profile_id != DIAG_PROFILE:
            continue
        if spec.diag_gate4_only_extra and profile_id == DIAG_PROFILE and gate != 4:
            continue
        if gate == 5 and profile_id == DIAG_PROFILE:
            continue
        not_applicable = gate == 5 or (profile_id == DIAG_PROFILE and spec.diag_not_applicable)
        rows.append(
            RailApplicability(
                profileId=profile_id,
                status="applicable" if not not_applicable else "not-applicable",
                selectionIdentity=f"selection:{spec.rail_id}",
                population="exact population" if not not_applicable else None,
                reason=None if not not_applicable else "profile excludes this rail",
            )
        )
    required = ("suite-data",) if spec.gate == 3 else ()
    outputs = (
        (
            ArtifactDeclaration(
                artifactId="suite-data",
                schemaVersion="suite/v1",
                mediaType="application/json",
            ),
        )
        if spec.gate == 2
        else ()
    )
    return RailDefinition(
        identity=_identity(spec.rail_id),
        gate=spec.gate,
        railClass=CLASS_BY_GATE[spec.gate],
        authority="memory-domain" if spec.gate == 5 else "repository-profile",
        ownerClass="portable-owner",
        correctiveOwner="portable-owner",
        posture=spec.posture,
        orderKey=spec.rail_id,
        prerequisites=tuple(
            _identity(item) for item in (PREREQUISITES.get(spec.rail_id, ""),) if item
        ),
        requiredArtifacts=required,
        adapter=RailAdapterDefinition(
            adapterKind="process-adapter",
            adapterId=f"{spec.rail_id}-adapter",
            configurationDigest=DIGEST,
            executionEvidence=f"adapter://{spec.rail_id}",
        ),
        runtimeInputs=RailRuntimeInputs(runtimeIdentity="portable-runtime"),
        applicability=tuple(rows),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId=f"{spec.rail_id}-evidence",
                mediaType="application/json",
                maxBytes=256,
            ),
        ),
        outputArtifacts=outputs,
    )


def registry(
    *,
    diagnostics: list[RailSpec] | None = None,
) -> CanonicalRailRegistry:
    specs = [
        RailSpec("lint", 1),
        RailSpec("suite", 2),
        RailSpec("coverage", 3),
        RailSpec("e2e", 4),
        RailSpec("memory", 5),
    ]
    if diagnostics is not None:
        for spec in diagnostics:
            for index, existing in enumerate(specs):
                if existing.rail_id == spec.rail_id:
                    specs[index] = spec
                    break
            else:
                specs.append(spec)
    rails = [_rail(spec) for spec in specs]
    return canonicalize_registry(
        RailRegistry(
            registryId="portable-diagnostics",
            repositoryId="sample-repository",
            profiles=(
                RegistryProfile(
                    profileId=CERT_PROFILE,
                    kind="certifying",
                    gates=(1, 2, 3, 4, 5),
                ),
                RegistryProfile(
                    profileId=DIAG_PROFILE,
                    kind="diagnostic",
                    gates=(1, 2, 3, 4),
                ),
            ),
            rails=tuple(rails),
        )
    )


def certifying_plan(resolved: CanonicalRailRegistry) -> CertificationPlan:
    return compile_certification_plan(
        resolved,
        profile_id=CERT_PROFILE,
        candidate_identity=CANDIDATE,
    )


def codes(error: CertificationContractError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


class DiagnosticPlanningTests:
    def test_diagnostic_plan_projects_the_canonical_scenario_gate(self) -> None:
        resolved = registry()
        certifying = certifying_plan(resolved)
        plan = compile_diagnostic_plan(
            resolved,
            profile_id=DIAG_PROFILE,
            candidate_identity=CANDIDATE,
            certifying_plan=certifying,
            gate=4,
        )
        assert plan.profileKind == "diagnostic"
        assert tuple(gate.gate for gate in plan.gates) == (1, 2, 3, 4)
        certifying_gate = next(item for item in certifying.gates if item.gate == 4)
        diagnostic_gate = diagnostic_scenario_gate(plan, 4)
        assert diagnostic_gate.gatePrerequisites == (1, 2, 3)
        assert tuple(rail.identity.key for rail in diagnostic_gate.rails) == tuple(
            rail.identity.key for rail in certifying_gate.rails
        )
        assert tuple(rail.posture for rail in diagnostic_gate.rails) == tuple(
            rail.posture for rail in certifying_gate.rails
        )

    def test_scenario_gate_requires_the_complete_earlier_gate_prefix(self) -> None:
        resolved = registry()
        certifying = certifying_plan(resolved)
        with pytest.raises(CertificationContractError) as error:
            compile_diagnostic_plan(
                resolved,
                profile_id=DIAG_PROFILE,
                candidate_identity=CANDIDATE,
                certifying_plan=certifying,
                gate=5,
            )
        assert "diagnostic-scenario-gate-unplanned" in codes(error.value)
        with pytest.raises(CertificationContractError) as error:
            compile_diagnostic_plan(
                resolved,
                profile_id=DIAG_PROFILE,
                candidate_identity=CANDIDATE,
                certifying_plan=certifying,
                gate=3,
            )
        # The profile plans the exact 1-4 prefix for a gate-4 scenario; a
        # scenario selected at any other gate cannot bind that canonical rail.
        assert "diagnostic-scenario-gate-unplanned" in codes(error.value)

    def test_non_diagnostic_and_unknown_profiles_are_refused(self) -> None:
        resolved = registry()
        certifying = certifying_plan(resolved)
        with pytest.raises(CertificationContractError) as error:
            compile_diagnostic_plan(
                resolved,
                profile_id=CERT_PROFILE,
                candidate_identity=CANDIDATE,
                certifying_plan=certifying,
                gate=4,
            )
        assert "diagnostic-profile-kind-mismatch" in codes(error.value)
        with pytest.raises(CertificationContractError) as error:
            compile_diagnostic_plan(
                resolved,
                profile_id="unknown-ci",
                candidate_identity=CANDIDATE,
                certifying_plan=certifying,
                gate=4,
            )
        assert "diagnostic-profile-unknown" in codes(error.value)

    def test_certifying_plan_must_name_the_scenario_gate(self) -> None:
        resolved = registry()
        diagnostic_only_plan = compile_certification_plan(
            resolved,
            profile_id=DIAG_PROFILE,
            candidate_identity=CANDIDATE,
        )
        with pytest.raises(CertificationContractError) as error:
            compile_diagnostic_plan(
                resolved,
                profile_id=DIAG_PROFILE,
                candidate_identity=CANDIDATE,
                certifying_plan=diagnostic_only_plan,
                gate=4,
            )
        assert "diagnostic-certifying-plan-missing" in codes(error.value)

    def test_rail_catalog_drift_is_refused_as_a_second_scenario(self) -> None:
        # The diagnostic profile selects one extra gate-4 rail: the diagnostic
        # catalog would be a second, non-canonical scenario implementation.
        resolved = registry(diagnostics=[RailSpec("extra-scenario", 4, diag_gate4_only_extra=True)])
        certifying = certifying_plan(resolved)
        with pytest.raises(CertificationContractError) as error:
            compile_diagnostic_plan(
                resolved,
                profile_id=DIAG_PROFILE,
                candidate_identity=CANDIDATE,
                certifying_plan=certifying,
                gate=4,
            )
        assert "diagnostic-scenario-rail-mismatch" in codes(error.value)

    def test_applicability_drift_on_the_scenario_rail_is_refused(self) -> None:
        # The e2e rail stays applicable to the certifying profile but becomes
        # not-applicable in the diagnostic profile: replicating the diagnostic
        # catalog would change scenario applicability semantics.
        resolved = registry(diagnostics=[RailSpec("e2e", 4, diag_not_applicable=True)])
        certifying = certifying_plan(resolved)
        with pytest.raises(CertificationContractError) as error:
            compile_diagnostic_plan(
                resolved,
                profile_id=DIAG_PROFILE,
                candidate_identity=CANDIDATE,
                certifying_plan=certifying,
                gate=4,
            )
        assert "diagnostic-scenario-rail-mismatch" in codes(error.value)

    def test_diagnostic_plan_binds_the_exact_candidate(self) -> None:
        resolved = registry()
        other = CandidateIdentity(kind="git-tree", value="d" * 40)
        certifying = certifying_plan(resolved)
        plan = compile_diagnostic_plan(
            resolved,
            profile_id=DIAG_PROFILE,
            candidate_identity=other,
            certifying_plan=certifying,
            gate=4,
        )
        assert plan.candidateIdentity == other
