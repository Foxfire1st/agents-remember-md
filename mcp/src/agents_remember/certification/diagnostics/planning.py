"""Diagnostic plan projection from the canonical R11 rail registry.

A diagnostic is one optional replication of the exact canonical scenario
catalog the certifying profile would run, compiled at diagnostic altitude.
CCR-R13 forbids a second scenario implementation: the compiled diagnostic
rails for the scenario gate must equal the certifying plan's gate rails in
identity, posture, and applicability, exactly as the R09 readiness compiler
re-checks them.
"""

from __future__ import annotations

from typing import Never

from agents_remember.certification.models import (
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationPlan,
    CompiledRail,
    GatePlan,
    RegistryProfile,
    RegistryValidationFinding,
)
from agents_remember.certification.planning import compile_certification_plan
from agents_remember.certification.validation import validate_registry
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import GateId


def compile_diagnostic_plan(
    registry: CanonicalRailRegistry,
    *,
    profile_id: str,
    candidate_identity: CandidateIdentity,
    certifying_plan: CertificationPlan,
    gate: GateId,
) -> CertificationPlan:
    """Compile the exact diagnostic-altitude plan for one scenario gate.

    Refuses an unknown or non-diagnostic profile, a profile that does not plan
    the requested gate over the complete earlier-gate prefix, and any catalog
    that differs from the exact certifying scenario rails (which would be a
    second, non-canonical scenario implementation).
    """

    report = validate_registry(registry)
    if not report.ok:
        _raise_contract_error("diagnostic plan admission failed", report.findings)
    profile = _selected_profile(registry, profile_id)
    if profile.kind != "diagnostic":
        _raise_contract_error(
            "diagnostic plan admission failed",
            (
                RegistryValidationFinding(
                    code="diagnostic-profile-kind-mismatch",
                    path=f"profiles.{profile_id}.kind",
                    detail="the selected profile is not a diagnostic-altitude profile",
                ),
            ),
        )
    if gate not in profile.gates or tuple(profile.gates) != tuple(range(1, gate + 1)):
        _raise_contract_error(
            "diagnostic plan admission failed",
            (
                RegistryValidationFinding(
                    code="diagnostic-scenario-gate-unplanned",
                    path=f"profiles.{profile_id}.gates",
                    detail=(
                        "a diagnostic scenario gate requires the profile to plan the exact "
                        "complete earlier-gate prefix through the scenario gate"
                    ),
                ),
            ),
        )
    if certifying_plan.profileKind != "certifying" or gate not in {
        item.gate for item in certifying_plan.gates
    }:
        _raise_contract_error(
            "diagnostic plan admission failed",
            (
                RegistryValidationFinding(
                    code="diagnostic-certifying-plan-missing",
                    path="certifyingPlan",
                    detail="diagnostics require the exact certifying plan for the candidate",
                ),
            ),
        )
    diagnostic_plan = compile_certification_plan(
        registry,
        profile_id=profile_id,
        candidate_identity=candidate_identity,
    )
    certifying_gate_plan = next(item for item in certifying_plan.gates if item.gate == gate)
    diagnostic_gate_plan = next(item for item in diagnostic_plan.gates if item.gate == gate)
    _require_canonical_scenario_catalog(certifying_gate_plan, diagnostic_gate_plan)
    return diagnostic_plan


def diagnostic_scenario_gate(plan: CertificationPlan, gate: GateId) -> GatePlan:
    """Return the exact diagnostic gate plan for the scenario gate."""

    matched = tuple(item for item in plan.gates if item.gate == gate)
    if len(matched) != 1:
        _raise_contract_error(
            "diagnostic scenario gate selection failed",
            (
                RegistryValidationFinding(
                    code="diagnostic-gate-absent",
                    path=f"gates.{gate}",
                    detail="the diagnostic plan does not declare exactly one scenario gate",
                ),
            ),
        )
    return matched[0]


def scenario_gate_digest(plan: CertificationPlan, gate: GateId) -> str:
    """The immutable digest of the planned scenario gate catalog."""

    return diagnostic_scenario_gate(plan, gate).planDigest


def _selected_profile(
    registry: CanonicalRailRegistry,
    profile_id: str,
) -> RegistryProfile:
    matched = tuple(item for item in registry.registry.profiles if item.profileId == profile_id)
    if len(matched) != 1:
        _raise_contract_error(
            "diagnostic plan admission failed",
            (
                RegistryValidationFinding(
                    code="diagnostic-profile-unknown",
                    path=f"profiles.{profile_id}",
                    detail="the selected diagnostic profile is not declared exactly once",
                ),
            ),
        )
    return matched[0]


def _require_canonical_scenario_catalog(
    certifying_gate_plan: GatePlan,
    diagnostic_gate_plan: GatePlan,
) -> None:
    certifying = _rail_contract(certifying_gate_plan)
    diagnostic = _rail_contract(diagnostic_gate_plan)
    if diagnostic != certifying:
        _raise_contract_error(
            "diagnostic plan admission failed",
            (
                RegistryValidationFinding(
                    code="diagnostic-scenario-rail-mismatch",
                    path=f"gates.{certifying_gate_plan.gate}.rails",
                    detail=(
                        "the diagnostic catalog changed rail identity, posture, or "
                        "applicability semantics; diagnostics may only replicate the exact "
                        "canonical scenario rails"
                    ),
                ),
            ),
        )


def _rail_contract(gate_plan: GatePlan) -> tuple[tuple[object, ...], ...]:
    return tuple(_compiled_rail_contract(rail) for rail in _ordered_catalog(gate_plan.rails))


def _ordered_catalog(
    rails: tuple[CompiledRail, ...],
) -> tuple[CompiledRail, ...]:
    return tuple(
        sorted(
            rails,
            key=lambda rail: (
                rail.orderKey,
                rail.identity.railId,
                rail.identity.version,
                rail.definitionDigest,
            ),
        )
    )


def _compiled_rail_contract(rail: CompiledRail) -> tuple[object, ...]:
    return (
        rail.identity.key,
        rail.posture,
        rail.applicability.status,
    )


def _raise_contract_error(
    detail: str,
    findings: tuple[RegistryValidationFinding, ...],
) -> Never:
    raise CertificationContractError(
        detail,
        [item.model_dump(mode="json") for item in findings],
    )


__all__ = [
    "compile_diagnostic_plan",
    "diagnostic_scenario_gate",
    "scenario_gate_digest",
]
