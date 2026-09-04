"""Final real-codex plan projection and exact-predecessor barriers.

CCR-R14@v3 runs exactly two fresh independent no-retry certifying repetitions
of the exact candidate's canonical scenario rails only after exact green
Gate-1..3 certificates for the same code tree, profile, plan, config,
toolchain/runtime, and selected certification profile.  This module compiles
the immutable final-codex plan record from the R11 canonical registry and the
exact certifying plan, and enforces the must-not-run barriers:

* the selected profile must be a certifying altitude profile that plans the
  complete Gate-1..4 prefix (R14 never runs at diagnostic altitude);
* the Gate-4 rails compiled for the run must equal the exact canonical
  scenario catalog the certifying plan would run (no second scenario
  implementation, no framework-hardcoded ARSPAWN behavior);
* the exact certifying Gate-1..3 result manifests must be green, carry
  certifying altitude, bind the exact candidate, and bind the same
  certification-plan and gate-plan digests the run record freezes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Never

from agents_remember.certification.digests import content_digest
from agents_remember.certification.final_codex.models import (
    CERTIFYING_GATE,
    FinalCodexPlanRecord,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationContractFinding,
    CertificationPlan,
    GatePlan,
    GateResultManifest,
    RegistryProfile,
    RegistryValidationFinding,
)
from agents_remember.certification.planning import admit_certification_plan
from agents_remember.certification.validation import validate_registry
from agents_remember.errors import CertificationContractError


def compile_final_codex_plan_record(
    registry: CanonicalRailRegistry,
    *,
    certifying_plan: CertificationPlan,
    candidate_identity: CandidateIdentity,
    scenario_version: str,
    plan_version: str,
) -> FinalCodexPlanRecord:
    """Compile the immutable two-repetition plan record for one scenario gate.

    Refuses a registry that does not validate, a non-certifying profile, a
    certifying plan that is not the exact R11 registry compilation for the
    selected profile and candidate, and a certifying plan that does not plan
    the complete Gate-1..4 prefix.  Because the plan must equal the canonical
    registry compilation, any second or framework-hardcoded scenario catalog
    is structurally impossible.
    """

    report = validate_registry(registry)
    if not report.ok:
        _raise_contract_error("final-codex plan admission failed", report.findings)
    profile = _selected_profile(registry, certifying_plan.profileId)
    _require_certifying_profile(profile, certifying_plan)
    if not set((1, 2, 3, 4)).issubset(set(profile.gates)) or tuple(profile.gates) != tuple(
        sorted(profile.gates)
    ):
        _raise_contract_error(
            "final-codex plan admission failed",
            (
                RegistryValidationFinding(
                    code="final-codex-gate-prefix-incomplete",
                    path=f"profiles.{profile.profileId}.gates",
                    detail=(
                        "the final real-codex lane requires the certifying profile to plan the "
                        "complete Gate-1..4 prefix"
                    ),
                ),
            ),
        )
    if certifying_plan.candidateIdentity != candidate_identity:
        _raise_contract_error(
            "final-codex plan admission failed",
            (
                RegistryValidationFinding(
                    code="final-codex-candidate-mismatch",
                    path="certifyingPlan.candidateIdentity",
                    detail="the certifying plan must bind the exact candidate",
                ),
            ),
        )
    if certifying_plan.profileKind != "certifying":
        _raise_contract_error(
            "final-codex plan admission failed",
            (
                RegistryValidationFinding(
                    code="final-codex-not-certifying",
                    path="certifyingPlan.profileKind",
                    detail="the final real-codex lane certifies only certifying-altitude plans",
                ),
            ),
        )
    try:
        admit_certification_plan(
            registry,
            certifying_plan,
            profile_id=certifying_plan.profileId,
            candidate_identity=candidate_identity,
        )
    except CertificationContractError as error:
        raise CertificationContractError(
            "final-codex plan admission failed: the certifying plan is not the exact "
            "canonical registry compilation",
            error.findings,
        ) from error
    gate_plan = next(item for item in certifying_plan.gates if item.gate == CERTIFYING_GATE)
    payload = {
        "schemaVersion": "final-codex-plan-record/v1",
        "candidateIdentity": candidate_identity.model_dump(mode="json"),
        "registryDigest": registry.registryDigest,
        "certifyingPlanDigest": certifying_plan.planDigest,
        "gate": CERTIFYING_GATE,
        "gatePlanDigest": gate_plan.planDigest,
        "scenarioVersion": scenario_version,
        "profileId": profile.profileId,
        "planVersion": plan_version,
    }
    return FinalCodexPlanRecord(**payload, planDigest=content_digest(payload))


def final_codex_gate_plan(
    plan: FinalCodexPlanRecord,
    certifying_plan: CertificationPlan,
) -> GatePlan:
    """Resolve the exact Gate-4 plan the frozen record certifies.

    Returns the certifying Gate-4 plan only when the certifying plan binds the
    exact candidate, profile, registry, and Gate-4 plan digest the record
    froze.  A stale or rebinding certifying plan refuses before any scenario
    step starts.
    """

    if certifying_plan.candidateIdentity != plan.candidateIdentity:
        _raise_contract_error(
            "final-codex gate selection failed",
            (
                RegistryValidationFinding(
                    code="final-codex-gate-candidate-mismatch",
                    path="certifyingPlan.candidateIdentity",
                    detail="the certifying plan must bind the exact frozen candidate",
                ),
            ),
        )
    if certifying_plan.profileId != plan.profileId:
        _raise_contract_error(
            "final-codex gate selection failed",
            (
                RegistryValidationFinding(
                    code="final-codex-gate-profile-mismatch",
                    path="certifyingPlan.profileId",
                    detail="the certifying plan must bind the exact frozen profile",
                ),
            ),
        )
    if certifying_plan.registryDigest != plan.registryDigest:
        _raise_contract_error(
            "final-codex gate selection failed",
            (
                RegistryValidationFinding(
                    code="final-codex-gate-registry-mismatch",
                    path="certifyingPlan.registryDigest",
                    detail="the certifying plan must bind the exact frozen registry",
                ),
            ),
        )
    matched = tuple(item for item in certifying_plan.gates if item.gate == CERTIFYING_GATE)
    if len(matched) != 1 or matched[0].planDigest != plan.gatePlanDigest:
        _raise_contract_error(
            "final-codex gate selection failed",
            (
                RegistryValidationFinding(
                    code="final-codex-gate-plan-mismatch",
                    path=f"gates.{CERTIFYING_GATE}.planDigest",
                    detail="the certifying plan must declare the exact frozen Gate-4 plan",
                ),
            ),
        )
    return matched[0]


def require_gates_one_to_three_green(
    candidate: CandidateIdentity,
    gateManifests: Sequence[GateResultManifest],
    *,
    certifying_plan: CertificationPlan,
) -> None:
    """Require exact green certifying Gate-1..3 manifests for the same plan.

    Each manifest must be green, certifying altitude, bound to the exact
    candidate, and must bind the exact certification-plan digest plus its own
    gate-plan digest from the certifying plan the final run freezes.  Any
    missing, stale, red, or differently bound predecessor refuses before any
    scenario step starts (must-not-run barrier).
    """

    ordered = tuple(gateManifests)
    expected_plan_digest = certifying_plan.planDigest
    if len(ordered) != 3 or tuple(item.gate for item in ordered) != (1, 2, 3):
        _raise_lane(
            "final-codex prerequisites refused",
            "final-codex-prerequisite-incomplete",
            "gateManifests",
            "the final real-codex lane requires the exact complete certifying Gate-1..3 manifests",
        )
    for manifest in ordered:
        if manifest.candidateIdentity != candidate:
            _raise_lane(
                "final-codex prerequisites refused",
                "final-codex-prerequisite-candidate-mismatch",
                f"gates.{manifest.gate}",
                "every green gate manifest must bind the exact candidate",
            )
        if manifest.profileKind != "certifying" or manifest.altitude != "certifying":
            _raise_lane(
                "final-codex prerequisites refused",
                "final-codex-prerequisite-not-certifying",
                f"gates.{manifest.gate}",
                "final-codex predecessors must come from the certifying altitude",
            )
        if manifest.disposition != "green":
            _raise_lane(
                "final-codex prerequisites refused",
                "final-codex-prerequisite-not-green",
                f"gates.{manifest.gate}",
                "the final real-codex lane runs only after exact green Gates-1..3",
            )
        if manifest.certificationPlanDigest != expected_plan_digest:
            _raise_lane(
                "final-codex prerequisites refused",
                "final-codex-prerequisite-plan-mismatch",
                f"gates.{manifest.gate}.certificationPlanDigest",
                "predecessor manifests must bind the exact certifying plan the run freezes",
            )
        expected_gate_plan = next(
            item for item in certifying_plan.gates if item.gate == manifest.gate
        )
        if manifest.gatePlanDigest != expected_gate_plan.planDigest:
            _raise_lane(
                "final-codex prerequisites refused",
                "final-codex-prerequisite-gate-plan-mismatch",
                f"gates.{manifest.gate}.gatePlanDigest",
                "predecessor manifests must bind the exact plan gate the run certifies",
            )


def _selected_profile(
    registry: CanonicalRailRegistry,
    profile_id: str,
) -> RegistryProfile:
    matched = tuple(item for item in registry.registry.profiles if item.profileId == profile_id)
    if len(matched) != 1:
        _raise_contract_error(
            "final-codex plan admission failed",
            (
                RegistryValidationFinding(
                    code="final-codex-profile-unknown",
                    path=f"profiles.{profile_id}",
                    detail="the selected profile is not declared exactly once",
                ),
            ),
        )
    return matched[0]


def _require_certifying_profile(
    profile: RegistryProfile,
    certifying_plan: CertificationPlan,
) -> None:
    if profile.kind != "certifying":
        _raise_contract_error(
            "final-codex plan admission failed",
            (
                RegistryValidationFinding(
                    code="final-codex-profile-kind-mismatch",
                    path=f"profiles.{profile.profileId}.kind",
                    detail="the selected profile is not a certifying-altitude profile",
                ),
            ),
        )
    if profile.profileId != certifying_plan.profileId:
        _raise_contract_error(
            "final-codex plan admission failed",
            (
                RegistryValidationFinding(
                    code="final-codex-plan-profile-mismatch",
                    path="certifyingPlan.profileId",
                    detail="the certifying plan must select the same profile",
                ),
            ),
        )


def _raise_contract_error(
    detail: str,
    findings: tuple[RegistryValidationFinding, ...],
) -> Never:
    raise CertificationContractError(
        detail,
        [item.model_dump(mode="json") for item in findings],
    )


def _raise_lane(detail: str, code: str, path: str, message: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=message)
    raise CertificationContractError(
        f"{detail}: {message}",
        (finding.model_dump(mode="json"),),
    )


__all__ = [
    "compile_final_codex_plan_record",
    "final_codex_gate_plan",
    "require_gates_one_to_three_green",
]
