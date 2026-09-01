"""Exact-plan rail result construction and complete gate aggregation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Never

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CanonicalRailRegistry,
    CertificationContractFinding,
    CertificationPlan,
    CompiledRail,
    GatePlan,
    GateResultAdmission,
    GateResultManifest,
    ProfileKind,
    RailResult,
    RailTerminalObservation,
)
from agents_remember.certification.planning import admit_certification_plan
from agents_remember.errors import CertificationContractError


def build_rail_result(
    gate_plan: GatePlan,
    observation: RailTerminalObservation,
) -> RailResult:
    """Bind one executor-neutral observation to its immutable planned definition."""

    compiled = next(
        (item for item in gate_plan.rails if item.identity.key == observation.rail.key),
        None,
    )
    if compiled is None:
        _raise_contract_error(
            "rail result construction failed",
            (
                _finding(
                    "unplanned-rail-result",
                    f"railResults.{observation.rail.key}",
                    "terminal observation does not belong to the gate plan",
                ),
            ),
        )
    payload = {
        "schemaVersion": "closeout-rail-result/v1",
        "rail": observation.rail.model_dump(mode="json"),
        "gate": gate_plan.gate,
        "gatePlanDigest": gate_plan.planDigest,
        "posture": compiled.posture,
        "status": observation.status,
        "code": observation.code,
        "blockedBy": [item.model_dump(mode="json") for item in observation.blockedBy],
        "correctiveOwner": compiled.correctiveOwner,
        "artifacts": [item.model_dump(mode="json") for item in observation.artifacts],
        "evidence": [item.model_dump(mode="json") for item in observation.evidence],
    }
    return RailResult(**payload, resultDigest=content_digest(payload))


def compile_gate_result_manifest(
    registry: CanonicalRailRegistry,
    certification_plan: CertificationPlan,
    gate_plan: GatePlan,
    rail_results: Sequence[RailResult],
    admission: GateResultAdmission,
) -> GateResultManifest:
    """Validate a complete terminal catalog and publish one immutable gate disposition."""

    admit_certification_plan(
        registry,
        certification_plan,
        profile_id=admission.profileId,
        candidate_identity=admission.candidateIdentity,
    )
    findings: list[CertificationContractFinding] = []
    _validate_plan_membership(certification_plan, gate_plan, admission.altitude, findings)
    grouped = _group_results(rail_results)
    _validate_result_catalog(gate_plan, grouped, findings)
    _validate_result_contracts(gate_plan, grouped, findings)
    _validate_blocking_semantics(gate_plan, grouped, findings)
    if findings:
        _raise_contract_error(
            "gate result manifest validation failed",
            tuple(sorted(findings, key=lambda item: (item.code, item.path, item.detail))),
        )
    ordered = tuple(grouped[rail.identity.key][0] for rail in gate_plan.rails)
    disposition = (
        "red"
        if any(
            result.posture == "enforcing" and result.status in {"fail", "blocked"}
            for result in ordered
        )
        else "green"
    )
    payload = {
        "schemaVersion": "closeout-gate-result-manifest/v1",
        "registryDigest": certification_plan.registryDigest,
        "certificationPlanDigest": certification_plan.planDigest,
        "gatePlanDigest": gate_plan.planDigest,
        "candidateIdentity": certification_plan.candidateIdentity.model_dump(mode="json"),
        "profileId": certification_plan.profileId,
        "profileKind": certification_plan.profileKind,
        "altitude": admission.altitude,
        "gate": gate_plan.gate,
        "disposition": disposition,
        "railResults": [result.model_dump(mode="json") for result in ordered],
    }
    return GateResultManifest(**payload, manifestDigest=content_digest(payload))


def _validate_plan_membership(
    certification_plan: CertificationPlan,
    gate_plan: GatePlan,
    altitude: ProfileKind,
    findings: list[CertificationContractFinding],
) -> None:
    admitted = next(
        (item for item in certification_plan.gates if item.gate == gate_plan.gate),
        None,
    )
    if admitted is None or admitted.planDigest != gate_plan.planDigest:
        findings.append(
            _finding(
                "gate-plan-not-admitted",
                f"gates.{gate_plan.gate}",
                "gate plan is not the exact plan admitted by the certification plan",
            )
        )
    if altitude != certification_plan.profileKind:
        findings.append(
            _finding(
                "diagnostic-promotion",
                "altitude",
                "diagnostic and certifying result altitudes cannot be promoted",
            )
        )


def _group_results(results: Sequence[RailResult]) -> dict[str, list[RailResult]]:
    grouped: dict[str, list[RailResult]] = defaultdict(list)
    for result in results:
        grouped[result.rail.key].append(result)
    return grouped


def _validate_result_catalog(
    gate_plan: GatePlan,
    grouped: dict[str, list[RailResult]],
    findings: list[CertificationContractFinding],
) -> None:
    planned = {rail.identity.key for rail in gate_plan.rails}
    observed = set(grouped)
    for identity in sorted(planned - observed):
        findings.append(
            _finding(
                "rail-result-omitted",
                f"railResults.{identity}",
                "complete gate catalog requires one terminal result for this rail",
            )
        )
    for identity in sorted(observed - planned):
        findings.append(
            _finding(
                "unplanned-rail-result",
                f"railResults.{identity}",
                "gate result contains a rail outside the immutable plan",
            )
        )
    for identity, declarations in grouped.items():
        if len(declarations) > 1:
            findings.append(
                _finding(
                    "duplicate-rail-result",
                    f"railResults.{identity}",
                    "gate result contains more than one terminal result for this rail",
                )
            )


def _validate_result_contracts(
    gate_plan: GatePlan,
    grouped: dict[str, list[RailResult]],
    findings: list[CertificationContractFinding],
) -> None:
    for compiled in gate_plan.rails:
        declarations = grouped.get(compiled.identity.key)
        if not declarations:
            continue
        result = declarations[0]
        _validate_result_identity(gate_plan, compiled, result, findings)
        _validate_result_applicability(compiled, result, findings)
        _validate_result_artifacts(compiled, result, findings)
        _validate_result_evidence(compiled, result, findings)


def _validate_result_identity(
    gate_plan: GatePlan,
    compiled: CompiledRail,
    result: RailResult,
    findings: list[CertificationContractFinding],
) -> None:
    expected = (gate_plan.gate, gate_plan.planDigest, compiled.posture, compiled.correctiveOwner)
    observed = (result.gate, result.gatePlanDigest, result.posture, result.correctiveOwner)
    if observed != expected:
        findings.append(
            _finding(
                "rail-result-plan-mismatch",
                f"railResults.{compiled.identity.key}",
                "result gate, plan, posture, or corrective owner differs from its plan",
            )
        )


def _validate_result_applicability(
    compiled: CompiledRail,
    result: RailResult,
    findings: list[CertificationContractFinding],
) -> None:
    should_be_not_applicable = compiled.applicability.status == "not-applicable"
    is_not_applicable = result.status == "not-applicable"
    if should_be_not_applicable != is_not_applicable:
        findings.append(
            _finding(
                "rail-result-applicability-mismatch",
                f"railResults.{compiled.identity.key}.status",
                "terminal status does not match the planned applicability",
            )
        )


def _validate_result_artifacts(
    compiled: CompiledRail,
    result: RailResult,
    findings: list[CertificationContractFinding],
) -> None:
    declared = {artifact.artifactId: artifact for artifact in compiled.outputArtifacts}
    observed = {artifact.artifactId for artifact in result.artifacts}
    for artifact_id in sorted(observed - set(declared)):
        findings.append(
            _finding(
                "undeclared-result-artifact",
                f"railResults.{compiled.identity.key}.artifacts.{artifact_id}",
                "rail result published an artifact outside its plan contract",
            )
        )
    if result.status != "pass":
        return
    required = {artifact.artifactId for artifact in declared.values() if artifact.requiredOnPass}
    for artifact_id in sorted(required - observed):
        findings.append(
            _finding(
                "required-result-artifact-missing",
                f"railResults.{compiled.identity.key}.artifacts.{artifact_id}",
                "passing rail omitted an artifact required by its plan contract",
            )
        )


def _validate_result_evidence(
    compiled: CompiledRail,
    result: RailResult,
    findings: list[CertificationContractFinding],
) -> None:
    declared = {item.evidenceId: item for item in compiled.evidenceContract}
    observed = {item.evidenceId: item for item in result.evidence}
    for evidence_id in sorted(set(declared) - set(observed)):
        findings.append(
            _finding(
                "result-evidence-missing",
                f"railResults.{compiled.identity.key}.evidence.{evidence_id}",
                "terminal rail result omitted declared bounded evidence",
            )
        )
    for evidence_id in sorted(set(observed) - set(declared)):
        findings.append(
            _finding(
                "undeclared-result-evidence",
                f"railResults.{compiled.identity.key}.evidence.{evidence_id}",
                "terminal rail result published evidence outside its plan contract",
            )
        )
    for evidence_id in sorted(set(observed) & set(declared)):
        if observed[evidence_id].size > declared[evidence_id].maxBytes:
            findings.append(
                _finding(
                    "result-evidence-oversize",
                    f"railResults.{compiled.identity.key}.evidence.{evidence_id}",
                    "terminal evidence exceeds its declared byte bound",
                )
            )


def _validate_blocking_semantics(
    gate_plan: GatePlan,
    grouped: dict[str, list[RailResult]],
    findings: list[CertificationContractFinding],
) -> None:
    planned = {rail.identity.key for rail in gate_plan.rails}
    available = {key: declarations[0] for key, declarations in grouped.items() if declarations}
    for rail in gate_plan.rails:
        _validate_rail_blocking(rail, planned, available, findings)


def _validate_rail_blocking(
    rail: CompiledRail,
    planned: set[str],
    available: dict[str, RailResult],
    findings: list[CertificationContractFinding],
) -> None:
    result = available.get(rail.identity.key)
    if result is None or rail.applicability.status == "not-applicable":
        return
    expected = _expected_blockers(rail, planned, available)
    observed = {item.key for item in result.blockedBy}
    if expected and result.status != "blocked":
        findings.append(
            _finding(
                "dependent-result-not-blocked",
                f"railResults.{rail.identity.key}.status",
                "a failed direct prerequisite requires a blocked dependent result",
            )
        )
    if expected != observed:
        findings.append(
            _finding(
                "blocked-by-mismatch",
                f"railResults.{rail.identity.key}.blockedBy",
                "blockedBy must name exactly the failed direct same-gate prerequisites",
            )
        )
    if not expected and result.status == "blocked":
        findings.append(
            _finding(
                "spurious-blocked-result",
                f"railResults.{rail.identity.key}.status",
                "independent or unblocked rail cannot be silently skipped as blocked",
            )
        )


def _expected_blockers(
    rail: CompiledRail,
    planned: set[str],
    available: dict[str, RailResult],
) -> set[str]:
    direct = {item.key for item in rail.prerequisites if item.key in planned}
    return {
        identity
        for identity in direct
        if identity in available and available[identity].status != "pass"
    }


def _finding(code: str, path: str, detail: str) -> CertificationContractFinding:
    return CertificationContractFinding(code=code, path=path, detail=detail)


def _raise_contract_error(
    detail: str,
    findings: tuple[CertificationContractFinding, ...],
) -> Never:
    raise CertificationContractError(
        detail,
        [item.model_dump(mode="json") for item in findings],
    )
