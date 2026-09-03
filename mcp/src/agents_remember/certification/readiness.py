"""Lossless compilation and rendering for closeout readiness."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Never

from agents_remember.certification.certificate_authority import (
    validate_certificate_chain,
    validate_finalization_currentness,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CertificationContractFinding,
    CertificationPlan,
    CompiledRail,
    GatePlan,
    GateResultManifest,
    RailResult,
)
from agents_remember.certification.readiness_models import (
    READINESS_SURFACES,
    CloseoutReadinessInput,
    CloseoutReadinessProjection,
    DiagnosticReadinessObservation,
    DiagnosticReadinessProjection,
    GateReadinessObservation,
    GateReadinessProjection,
    LifecycleReadinessProjection,
    ProfileReadinessProjection,
    RailReadinessProjection,
    RailReadinessState,
    ReadinessSurface,
)
from agents_remember.certification.repository_profiles.models import RepositoryProfilePlan
from agents_remember.errors import (
    CertificationContractError,
    CloseoutReadinessContractError,
)


def compile_closeout_readiness(source: CloseoutReadinessInput) -> CloseoutReadinessProjection:
    """Compile one exact, generation-coherent readiness projection."""

    plan = source.certificationPlan
    _require_certifying_plan(plan)
    _require_one_revision(source)
    _require_admission(source)
    profile = _compile_profile(source)
    gates = tuple(
        _compile_gate(plan, gate_plan, observation)
        for gate_plan, observation in zip(plan.gates, source.gates, strict=True)
    )
    _require_current_certificate_chain(source)
    _require_gate_barriers(gates)
    diagnostics = _compile_diagnostics(source)
    certification_ready = _certification_ready(source, gates, profile)
    _require_lifecycle(source, certification_ready)
    lifecycle = LifecycleReadinessProjection(
        state=source.lifecycle.state,
        blockedBy=source.lifecycle.blockedBy,
        code=source.lifecycle.code,
        correctiveOwner=source.lifecycle.correctiveOwner,
        evidence=source.lifecycle.evidence,
    )
    authority_digest = (
        source.finalizationAuthority.authorityDigest
        if source.finalizationAuthority is not None
        else None
    )
    payload = {
        "schemaVersion": "closeout-readiness/v1",
        "revision": source.revision.model_dump(mode="json"),
        "repositoryId": source.repositoryId,
        "candidateIdentity": plan.candidateIdentity.model_dump(mode="json"),
        "registryDigest": plan.registryDigest,
        "certificationPlanDigest": plan.planDigest,
        "profile": profile.model_dump(mode="json"),
        "lifecycle": lifecycle.model_dump(mode="json"),
        "gates": [gate.model_dump(mode="json") for gate in gates],
        "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
        "certificationReady": certification_ready,
        "finalizationAuthorityDigest": authority_digest,
    }
    return CloseoutReadinessProjection(
        **payload,
        projectionDigest=content_digest(payload),
    )


def project_closeout_readiness(
    source: CloseoutReadinessInput,
    *,
    surface: ReadinessSurface,
) -> CloseoutReadinessProjection:
    """Adapt any supported entry point without translating readiness semantics."""

    if surface not in READINESS_SURFACES:
        _raise("readiness-surface-unknown", "surface", "entry point is not in the closed catalog")
    return compile_closeout_readiness(source)


def readiness_projection_bytes(
    source: CloseoutReadinessInput,
    *,
    surface: ReadinessSurface,
) -> bytes:
    """Render byte-identical canonical JSON for every supported entry point."""

    projection = project_closeout_readiness(source, surface=surface)
    return json.dumps(
        projection.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_certifying_plan(plan: CertificationPlan) -> None:
    if plan.profileKind != "certifying" or tuple(gate.gate for gate in plan.gates) != (
        1,
        2,
        3,
        4,
        5,
    ):
        _raise(
            "certifying-plan-required",
            "certificationPlan",
            "closeout readiness requires the exact complete certifying Gate 1-5 plan",
        )


def _require_one_revision(source: CloseoutReadinessInput) -> None:
    observations = [source.profile.revision, source.lifecycle.revision]
    observations.extend(item.revision for item in source.gates)
    observations.extend(item.revision for item in source.diagnostics)
    if any(item != source.revision for item in observations):
        _raise(
            "mixed-generation-readiness",
            "revision",
            "all readiness observations must belong to one generation and revision",
        )


def _require_admission(source: CloseoutReadinessInput) -> None:
    admission = source.admission
    lifecycle = source.lifecycle.state
    if admission is None:
        if lifecycle not in {"admission-pending", "admission-refused"}:
            _raise(
                "admission-authority-missing",
                "admission",
                "post-admission lifecycle state requires exact admission authority",
            )
        if any(item.state in {"running", "passed", "failed"} for item in source.gates):
            _raise(
                "gate-started-before-admission",
                "gates",
                "no certifying gate may start before exact admission",
            )
        return
    envelope = admission.semanticEnvelope
    plan = source.certificationPlan
    expected = (
        source.repositoryId,
        plan.candidateIdentity,
        plan.profileId,
        plan.planDigest,
        plan.registryDigest,
        tuple(gate.planDigest for gate in plan.gates),
    )
    observed = (
        envelope.repositoryId,
        envelope.candidateCodeTree,
        envelope.profileId,
        envelope.certificationPlanDigest,
        envelope.registryDigest,
        tuple(gate.gatePlanDigest for gate in envelope.gates),
    )
    if observed != expected:
        _raise(
            "admission-plan-mismatch",
            "admission",
            "admission does not bind the exact repository, candidate, profile, and R11 plan",
        )
    if lifecycle in {"admission-pending", "admission-refused"}:
        _raise(
            "admission-lifecycle-contradiction",
            "lifecycle.state",
            "current admission authority contradicts a pre-admission lifecycle state",
        )


def _compile_profile(source: CloseoutReadinessInput) -> ProfileReadinessProjection:
    observation = source.profile
    repository_plan = observation.repositoryPlan
    admission = source.admission
    if observation.state in {"unresolved", "invalid"}:
        if repository_plan is not None:
            _raise(
                "profile-state-contradiction",
                "profile",
                "unresolved or invalid profile cannot carry an admitted R22 plan",
            )
    elif admission is None or repository_plan is None:
        _raise(
            "profile-authority-missing",
            "profile",
            "admitted-current and changed require exact admission and R22 plan authority",
        )
    if repository_plan is not None:
        _require_repository_plan_identity(source, repository_plan)
    admitted_digest = (
        admission.semanticEnvelope.admittedProfileDigest if admission is not None else None
    )
    current = repository_plan is not None and _repository_plan_is_admitted(source, repository_plan)
    if (observation.state == "admitted-current") != current:
        _raise(
            "profile-state-contradiction",
            "profile.state",
            "profile state does not match the exact admitted R22 plan identity",
        )
    return ProfileReadinessProjection(
        state=observation.state,
        profileId=source.certificationPlan.profileId,
        repositoryPlanDigest=repository_plan.planDigest if repository_plan is not None else None,
        admittedProfileDigest=admitted_digest,
    )


def _require_repository_plan_identity(
    source: CloseoutReadinessInput,
    repository_plan: RepositoryProfilePlan,
) -> None:
    plan = source.certificationPlan
    expected = (plan.candidateIdentity, plan.profileId)
    observed = (repository_plan.candidateIdentity, repository_plan.selectionId)
    if observed != expected:
        _raise(
            "repository-plan-identity-mismatch",
            "profile.repositoryPlan",
            "R22 plan candidate and selection must match the R11 plan",
        )


def _repository_plan_is_admitted(
    source: CloseoutReadinessInput,
    repository_plan: RepositoryProfilePlan,
) -> bool:
    admission = source.admission
    assert admission is not None  # guaranteed by _compile_profile's profile-authority-missing guard
    envelope = admission.semanticEnvelope
    return repository_plan.profileDigest == envelope.admittedProfileDigest and tuple(
        gate.planDigest for gate in repository_plan.gates
    ) == tuple(gate.repositoryGatePlanDigest for gate in envelope.gates[:4])


def _compile_gate(
    certification_plan: CertificationPlan,
    gate_plan: GatePlan,
    observation: GateReadinessObservation,
) -> GateReadinessProjection:
    if observation.gate != gate_plan.gate:
        _raise(
            "gate-catalog-order-mismatch",
            f"gates.{gate_plan.gate}",
            "gate observations must follow exact Gate 1-5 order",
        )
    if observation.genericTerminalReplacement:
        _raise(
            "generic-terminal-replacement",
            f"gates.{gate_plan.gate}",
            "generic worker failure cannot replace typed terminal rail results",
        )
    manifest = observation.resultManifest
    if manifest is not None:
        _require_manifest(certification_plan, gate_plan, manifest)
        expected_state = "passed" if manifest.disposition == "green" else "failed"
        if observation.state != expected_state:
            _raise(
                "gate-result-state-contradiction",
                f"gates.{gate_plan.gate}.state",
                "gate state must preserve the typed result disposition",
            )
    certificate = observation.certificate
    if observation.certificateState == "current-green":
        if observation.state != "passed" or manifest is None or certificate is None:
            _raise(
                "current-certificate-state-contradiction",
                f"gates.{gate_plan.gate}.certificate",
                "current-green requires the exact complete green result and certificate",
            )
        if certificate.semanticEnvelope.resultManifestDigest != manifest.manifestDigest:
            _raise(
                "certificate-result-mismatch",
                f"gates.{gate_plan.gate}.certificate",
                "certificate does not bind the projected gate result manifest",
            )
    return GateReadinessProjection(
        gate=gate_plan.gate,
        gatePlanDigest=gate_plan.planDigest,
        state=observation.state,
        blockedBy=observation.blockedBy,
        rails=_compile_rails(gate_plan, manifest),
        resultManifestDigest=manifest.manifestDigest if manifest is not None else None,
        certificateState=observation.certificateState,
        certificateDigest=certificate.certificateDigest if certificate is not None else None,
    )


def _require_manifest(
    certification_plan: CertificationPlan,
    gate_plan: GatePlan,
    manifest: GateResultManifest,
) -> None:
    expected_identity = (
        gate_plan.registryDigest,
        gate_plan.candidateIdentity,
        gate_plan.profileId,
        gate_plan.profileKind,
        gate_plan.gate,
        gate_plan.planDigest,
        certification_plan.planDigest,
    )
    observed_identity = (
        manifest.registryDigest,
        manifest.candidateIdentity,
        manifest.profileId,
        manifest.profileKind,
        manifest.gate,
        manifest.gatePlanDigest,
        manifest.certificationPlanDigest,
    )
    if observed_identity != expected_identity:
        _raise(
            "gate-manifest-plan-mismatch",
            f"gates.{gate_plan.gate}.resultManifest",
            "result manifest is not bound to the exact candidate/profile/gate plan",
        )
    planned = {rail.identity.key: rail for rail in gate_plan.rails}
    observed = {result.rail.key: result for result in manifest.railResults}
    if tuple(observed) != tuple(planned):
        _raise(
            "gate-manifest-catalog-mismatch",
            f"gates.{gate_plan.gate}.resultManifest.railResults",
            "result manifest must preserve the exact complete ordered rail catalog",
        )
    for identity, rail in planned.items():
        _require_result_contract(rail, observed[identity])


def _require_result_contract(rail: CompiledRail, result: RailResult) -> None:
    expected = (rail.posture, rail.correctiveOwner)
    observed = (result.posture, result.correctiveOwner)
    applicable = rail.applicability.status == "applicable"
    if observed != expected or applicable == (result.status == "not-applicable"):
        _raise(
            "rail-result-plan-mismatch",
            f"railResults.{rail.identity.key}",
            "rail posture, owner, or applicability differs from its exact plan",
        )
    declared_evidence = {item.evidenceId: item.maxBytes for item in rail.evidenceContract}
    observed_evidence = {item.evidenceId: item for item in result.evidence}
    if set(observed_evidence) != set(declared_evidence) or any(
        item.size > declared_evidence[evidence_id]
        for evidence_id, item in observed_evidence.items()
    ):
        _raise(
            "rail-result-evidence-mismatch",
            f"railResults.{rail.identity.key}.evidence",
            "terminal result must preserve every declared bounded evidence reference",
        )


def _compile_rails(
    gate_plan: GatePlan,
    manifest: GateResultManifest | None,
) -> tuple[RailReadinessProjection, ...]:
    results = {result.rail.key: result for result in manifest.railResults} if manifest else {}
    return tuple(_compile_rail(rail, results.get(rail.identity.key)) for rail in gate_plan.rails)


def _compile_rail(rail: CompiledRail, result: RailResult | None) -> RailReadinessProjection:
    state: RailReadinessState | None = None
    if result is not None:
        state = result.status
        if rail.posture == "report-only" and result.status == "pass":
            state = "report-only-pass"
        elif rail.posture == "report-only" and result.status == "fail":
            state = "report-only-fail"
    elif rail.applicability.status == "not-applicable":
        state = "not-applicable"
    return RailReadinessProjection(
        rail=rail.identity,
        posture=rail.posture,
        applicability=rail.applicability.status,
        state=state,
        code=result.code if result is not None else None,
        correctiveOwner=rail.correctiveOwner,
        blockedBy=result.blockedBy if result is not None else (),
        evidence=result.evidence if result is not None else (),
    )


def _require_current_certificate_chain(source: CloseoutReadinessInput) -> None:
    current = tuple(
        observation.certificate
        for observation in source.gates
        if observation.certificateState == "current-green"
    )
    certificates = tuple(item for item in current if item is not None)
    if not certificates:
        return
    gate_five_inputs = source.gateFiveInputs
    if source.finalizationInputs is not None:
        current_inputs = source.finalizationInputs.gateFiveInputs
        if gate_five_inputs is not None and gate_five_inputs != current_inputs:
            _raise(
                "gate-five-input-contradiction",
                "gateFiveInputs",
                "readiness and finalization name different current Gate-5 inputs",
            )
        gate_five_inputs = current_inputs
    assert source.admission is not None  # current certificates require exact admission authority
    try:
        validate_certificate_chain(
            source.admission,
            certificates,
            gate_five_inputs=gate_five_inputs,
        )
    except CertificationContractError as error:
        raise CloseoutReadinessContractError(str(error), error.findings) from error


def _require_gate_barriers(gates: Sequence[GateReadinessProjection]) -> None:
    for index, gate in enumerate(gates):
        earlier = gates[:index]
        if any(item.state == "failed" for item in earlier) and gate.state not in {
            "not-started",
            "blocked",
            "invalidated",
        }:
            _raise(
                "red-gate-barrier-violated",
                f"gates.{gate.gate}.state",
                "a red Gate 1-4 prevents every later gate start",
            )
        if gate.state in {"running", "passed", "failed"} and any(
            item.state != "passed" or item.certificateState != "current-green" for item in earlier
        ):
            _raise(
                "gate-prerequisite-not-current",
                f"gates.{gate.gate}.state",
                "a gate cannot start before every earlier gate is passed and current-green",
            )


def _compile_diagnostics(
    source: CloseoutReadinessInput,
) -> tuple[DiagnosticReadinessProjection, ...]:
    projections = tuple(_compile_diagnostic(source, item) for item in source.diagnostics)
    keys = [(item.certificationPlanDigest, item.gate) for item in projections]
    if keys != sorted(set(keys)):
        _raise(
            "diagnostic-catalog-noncanonical",
            "diagnostics",
            "diagnostic plan/gate identities must be unique and canonical",
        )
    return projections


def _compile_diagnostic(
    source: CloseoutReadinessInput,
    observation: DiagnosticReadinessObservation,
) -> DiagnosticReadinessProjection:
    plan = observation.plan
    manifest = observation.resultManifest
    if plan.profileKind != "diagnostic" or manifest.altitude != "diagnostic":
        _raise(
            "diagnostic-promotion",
            "diagnostics",
            "diagnostic results must remain explicitly non-certifying",
        )
    if plan.candidateIdentity != source.certificationPlan.candidateIdentity:
        _raise(
            "diagnostic-candidate-mismatch",
            "diagnostics",
            "diagnostic and certifying views must name the same exact candidate",
        )
    gate_plan = next((gate for gate in plan.gates if gate.gate == manifest.gate), None)
    if gate_plan is None:
        _raise(
            "diagnostic-gate-unplanned",
            "diagnostics",
            "diagnostic result gate is absent from its exact plan",
        )
    _require_manifest(plan, gate_plan, manifest)
    certifying_gate = source.certificationPlan.gates[manifest.gate - 1]
    expected = tuple(
        (rail.identity, rail.posture, rail.applicability) for rail in certifying_gate.rails
    )
    observed = tuple((rail.identity, rail.posture, rail.applicability) for rail in gate_plan.rails)
    if observed != expected:
        _raise(
            "diagnostic-rail-contract-mismatch",
            "diagnostics",
            "diagnostic preview changed rail identity, posture, or applicability semantics",
        )
    return DiagnosticReadinessProjection(
        certificationPlanDigest=plan.planDigest,
        gate=manifest.gate,
        disposition=manifest.disposition,
        rails=_compile_rails(gate_plan, manifest),
        resultManifestDigest=manifest.manifestDigest,
    )


def _certification_ready(
    source: CloseoutReadinessInput,
    gates: Sequence[GateReadinessProjection],
    profile: ProfileReadinessProjection,
) -> bool:
    return (
        source.admission is not None
        and profile.state == "admitted-current"
        and all(
            gate.state == "passed" and gate.certificateState == "current-green" for gate in gates
        )
    )


def _require_lifecycle(source: CloseoutReadinessInput, certification_ready: bool) -> None:
    state = source.lifecycle.state
    if (
        state in {"finalization-pending", "finalization-running", "finalized"}
        and not certification_ready
    ):
        _raise(
            "finalization-before-certification",
            "lifecycle.state",
            "finalization cannot proceed before exact current-green Gates 1-5",
        )
    if state == "finalized":
        if source.finalizationAuthority is None or source.finalizationInputs is None:
            _raise(
                "finalization-authority-missing",
                "finalizationAuthority",
                "finalized state requires current transactional authority and inputs",
            )
        certificates = tuple(
            item.certificate for item in source.gates if item.certificate is not None
        )
        assert source.admission is not None
        try:
            validate_finalization_currentness(
                source.finalizationAuthority,
                source.admission,
                certificates,
                source.finalizationInputs,
            )
        except CertificationContractError as error:
            raise CloseoutReadinessContractError(str(error), error.findings) from error
    elif source.finalizationAuthority is not None:
        _raise(
            "finalization-authority-state-contradiction",
            "finalizationAuthority",
            "current finalization authority is visible only in finalized state",
        )


def _raise(code: str, path: str, detail: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=detail)
    raise CloseoutReadinessContractError(
        "closeout readiness compilation failed",
        (finding.model_dump(mode="json"),),
    )


__all__ = [
    "READINESS_SURFACES",
    "compile_closeout_readiness",
    "project_closeout_readiness",
    "readiness_projection_bytes",
]
