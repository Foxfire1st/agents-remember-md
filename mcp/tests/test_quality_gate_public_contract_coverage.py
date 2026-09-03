"""Self-contained closeout-readiness coverage companion to the public-contract module.

Carries the negative fixtures that complete diff coverage of
certification/readiness.py and certification/readiness_models.py without
pushing test_quality_gate_public_contract.py past the 1,200-line file-size
hard limit.  This module is fully standalone: it inlines its own scenario
scaffold over the production certification models and never imports test-support
or fixture modules, so the evidence-lifecycle catalog observes no transitive
test-support consumers here.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import cast

import pytest
from agents_remember.certification import (
    build_rail_result,
    canonicalize_registry,
    compile_certification_plan,
    compile_closeout_readiness,
    compile_finalization_authority,
    compile_gate_certificate,
    compile_gate_result_manifest,
    project_closeout_readiness,
)
from agents_remember.certification.certificate_admission import gate_semantic_digest
from agents_remember.certification.certificate_models import (
    AdmissionGateIdentity,
    CertificationAdmissionManifest,
    CertificationAdmissionSemanticEnvelope,
    CoherenceSubrecordIdentity,
    CreationProvenance,
    FinalizationCertificateAuthority,
    FinalizationCurrentInputs,
    GateCertificate,
    GateCertificateIssuanceContext,
    GateFiveSemanticInputs,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    ArtifactDeclaration,
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationPlan,
    GateId,
    GateResultAdmission,
    GateResultManifest,
    RailAdapterDefinition,
    RailApplicability,
    RailArtifactResult,
    RailClass,
    RailDefinition,
    RailEvidenceContract,
    RailEvidenceReference,
    RailIdentity,
    RailPosture,
    RailRegistry,
    RailResult,
    RailRuntimeInputs,
    RailTerminalObservation,
    RegistryProfile,
)
from agents_remember.certification.readiness_models import (
    CloseoutReadinessInput,
    DiagnosticReadinessObservation,
    GateReadinessObservation,
    LifecycleReadinessObservation,
    ProfileReadinessObservation,
    ReadinessEvidenceReference,
    ReadinessRevision,
    ReadinessSurface,
)
from agents_remember.certification.repository_profiles.models import (
    RepositoryGatePlan,
    RepositoryProfilePlan,
    repository_gate_plan_digest,
)
from agents_remember.errors import CloseoutReadinessContractError
from pydantic import ValidationError

_CANDIDATE = CandidateIdentity(kind="git-tree", value="c" * 40)
_PROFILE = "portable-ci"
_PROFILE_DIGEST = "d" * 64
_REVISION = ReadinessRevision(generationId="generation-9", revision=4)
_DIGEST = "a" * 64
_CLASS_BY_GATE: dict[GateId, RailClass] = {
    1: "pre-test-quality",
    2: "ordinary-test-suite",
    3: "post-test-quality",
    4: "integration-test",
    5: "memory-quality",
}


@dataclass(frozen=True)
class _ReadinessScenario:
    registry: CanonicalRailRegistry
    plan: CertificationPlan
    repository_plan: RepositoryProfilePlan
    admission: CertificationAdmissionManifest
    manifests: tuple[GateResultManifest, ...]
    certificates: tuple[GateCertificate, ...]
    finalization_inputs: FinalizationCurrentInputs
    finalization: FinalizationCertificateAuthority


def _readiness_identity(name: str) -> RailIdentity:
    return RailIdentity(railId=name, version="1.0.0")


def _readiness_rail(
    name: str,
    gate: GateId,
    *,
    prerequisite: str | None = None,
    posture: RailPosture = "enforcing",
    applicable: bool = True,
) -> RailDefinition:
    output = (
        (
            ArtifactDeclaration(
                artifactId="suite-data",
                schemaVersion="suite/v1",
                mediaType="application/json",
            ),
        )
        if gate == 2
        else ()
    )
    return RailDefinition(
        identity=_readiness_identity(name),
        gate=gate,
        railClass=_CLASS_BY_GATE[gate],
        authority="memory-domain" if gate == 5 else "repository-profile",
        ownerClass="portable-owner",
        correctiveOwner="portable-owner",
        posture=posture,
        orderKey=name,
        prerequisites=(_readiness_identity(prerequisite),) if prerequisite else (),
        requiredArtifacts=("suite-data",) if gate == 3 else (),
        adapter=RailAdapterDefinition(
            adapterKind="process-adapter",
            adapterId=f"{name}-adapter",
            configurationDigest=_DIGEST,
            executionEvidence=f"adapter://{name}",
        ),
        runtimeInputs=RailRuntimeInputs(runtimeIdentity="portable-runtime"),
        applicability=(
            RailApplicability(
                profileId=_PROFILE,
                status="applicable" if applicable else "not-applicable",
                selectionIdentity=f"selection:{name}",
                population="exact population" if applicable else None,
                reason=None if applicable else "profile excludes memory validation",
            ),
        ),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId=f"{name}-evidence",
                mediaType="application/json",
                maxBytes=256,
            ),
        ),
        outputArtifacts=output,
    )


def _readiness_registry(
    *,
    diagnostic: bool = False,
    gates: tuple[GateId, ...] | None = None,
    advisory: bool = True,
) -> RailRegistry:
    rails = (
        _readiness_rail("advisory", 1, posture="report-only"),
        _readiness_rail("lint", 1),
        _readiness_rail("suite", 2, prerequisite="lint"),
        _readiness_rail("coverage", 3, prerequisite="suite"),
        _readiness_rail("e2e", 4, prerequisite="coverage"),
        _readiness_rail("memory", 5, prerequisite="e2e", applicable=False),
    )
    selected_gates = gates or ((1,) if diagnostic else (1, 2, 3, 4, 5))
    selected = tuple(
        rail
        for rail in rails
        if rail.gate in selected_gates and (advisory or rail.identity.railId != "advisory")
    )
    return RailRegistry(
        registryId="portable-readiness",
        repositoryId="sample-repository",
        profiles=(
            RegistryProfile(
                profileId=_PROFILE,
                kind="diagnostic" if diagnostic else "certifying",
                gates=selected_gates,
            ),
        ),
        rails=selected,
    )


def _readiness_repository_gate(
    plan: CertificationPlan,
    gate: GateId,
) -> RepositoryGatePlan:
    gate_plan = plan.gates[gate - 1]
    payload = {
        "schemaVersion": "repository-gate-plan/v1",
        "profileDigest": _PROFILE_DIGEST,
        "candidateIdentity": _CANDIDATE.model_dump(mode="json"),
        "selectionId": _PROFILE,
        "purpose": "closeout",
        "mode": "targeted",
        "gate": gate,
        "gatePrerequisites": list(range(1, gate)),
        "applicability": "applicable",
        "reason": None,
        "rails": [rail.model_dump(mode="json") for rail in gate_plan.rails],
        "semanticInputs": [],
        "waves": [
            [identity.model_dump(mode="json") for identity in wave] for wave in gate_plan.waves
        ],
    }
    return RepositoryGatePlan(**payload, planDigest=repository_gate_plan_digest(payload))


def _readiness_repository_plan(plan: CertificationPlan) -> RepositoryProfilePlan:
    gates = tuple(_readiness_repository_gate(plan, gate) for gate in (1, 2, 3, 4))
    payload = {
        "schemaVersion": "repository-profile-plan/v1",
        "profileDigest": _PROFILE_DIGEST,
        "candidateIdentity": _CANDIDATE.model_dump(mode="json"),
        "selectionId": _PROFILE,
        "purpose": "closeout",
        "mode": "targeted",
        "gates": [gate.model_dump(mode="json") for gate in gates],
    }
    return RepositoryProfilePlan(**payload, planDigest=content_digest(payload))


def _readiness_provenance() -> CreationProvenance:
    return CreationProvenance(
        createdAt="2026-09-02T00:00:00+00:00",
        producer="readiness-test",
        evidenceRef="evidence://readiness-test",
    )


def _readiness_admission(
    plan: CertificationPlan,
    repository_plan: RepositoryProfilePlan,
) -> CertificationAdmissionManifest:
    envelope = CertificationAdmissionSemanticEnvelope(
        repositoryId="sample-repository",
        candidateCodeTree=_CANDIDATE,
        profileId=_PROFILE,
        certificationPlanDigest=plan.planDigest,
        admittedProfileDigest=_PROFILE_DIGEST,
        registryDigest=plan.registryDigest,
        gates=tuple(
            AdmissionGateIdentity(
                gate=gate.gate,
                gatePlanDigest=gate.planDigest,
                gateSemanticDigest=gate_semantic_digest(gate),
                repositoryGatePlanDigest=(
                    repository_plan.gates[gate.gate - 1].planDigest if gate.gate <= 4 else None
                ),
                semanticInputs=(),
            )
            for gate in plan.gates
        ),
    )
    return CertificationAdmissionManifest(
        semanticEnvelope=envelope,
        admissionDigest=content_digest(envelope),
        provenance=_readiness_provenance(),
    )


def _readiness_manifest(
    registry: CanonicalRailRegistry,
    plan: CertificationPlan,
    gate: GateId,
    *,
    enforcing_failure: bool = False,
    report_only_pass: bool = False,
) -> GateResultManifest:
    gate_plan = plan.gates[gate - 1]
    results = []
    for rail in gate_plan.rails:
        if rail.applicability.status == "not-applicable":
            status = "not-applicable"
        elif rail.posture == "report-only":
            status = "pass" if report_only_pass else "fail"
        else:
            status = "fail" if enforcing_failure else "pass"
        artifacts = tuple(
            RailArtifactResult(
                artifactId=item.artifactId,
                sha256=content_digest({"artifact": item.artifactId}),
                size=32,
                evidenceRef=f"artifact://{item.artifactId}",
            )
            for item in rail.outputArtifacts
            if status == "pass"
        )
        observation = RailTerminalObservation(
            rail=rail.identity,
            status=status,
            code=f"{rail.identity.railId}-{status}",
            artifacts=artifacts,
            evidence=tuple(
                RailEvidenceReference(
                    evidenceId=item.evidenceId,
                    sha256=content_digest({"evidence": item.evidenceId}),
                    size=32,
                    reference=f"evidence://{item.evidenceId}",
                )
                for item in rail.evidenceContract
            ),
        )
        results.append(build_rail_result(gate_plan, observation))
    return compile_gate_result_manifest(
        registry,
        plan,
        gate_plan,
        results,
        GateResultAdmission(
            profileId=_PROFILE,
            candidateIdentity=plan.candidateIdentity,
            altitude=plan.profileKind,
        ),
    )


def _readiness_memory_inputs() -> GateFiveSemanticInputs:
    return GateFiveSemanticInputs(
        memoryTree=CandidateIdentity(kind="git-tree", value="m" * 40),
        affectedClosurePlanDigest=content_digest({"closure": "exact"}),
        memoryCheckerRegistryDigest=content_digest({"registry": "memory"}),
        coherenceSubrecords=(
            CoherenceSubrecordIdentity(
                subrecordId="route-entity",
                contentDigest=content_digest({"coherence": "current"}),
            ),
        ),
        candidatePairAuthorityDigest=content_digest({"pair": "current"}),
    )


def _readiness_scenario() -> _ReadinessScenario:
    registry = canonicalize_registry(_readiness_registry())
    plan = compile_certification_plan(
        registry,
        profile_id=_PROFILE,
        candidate_identity=_CANDIDATE,
    )
    repository_plan = _readiness_repository_plan(plan)
    admission = _readiness_admission(plan, repository_plan)
    manifests = tuple(_readiness_manifest(registry, plan, gate) for gate in (1, 2, 3, 4, 5))
    memory_inputs = _readiness_memory_inputs()
    certificates: list[GateCertificate] = []
    for gate, manifest in enumerate(manifests, 1):
        certificates.append(
            compile_gate_certificate(
                admission,
                plan.gates[gate - 1],
                manifest,
                certificates,
                GateCertificateIssuanceContext(
                    provenance=_readiness_provenance(),
                    gateFiveInputs=memory_inputs if gate == 5 else None,
                ),
            )
        )
    finalization_inputs = FinalizationCurrentInputs(
        gateFiveInputs=memory_inputs,
        taskIntentAuthorityDigest=content_digest({"task": "current"}),
        journalAuthorityDigest=content_digest({"journal": "current"}),
    )
    finalization = compile_finalization_authority(
        admission,
        certificates,
        finalization_inputs,
        _readiness_provenance(),
    )
    return _ReadinessScenario(
        registry,
        plan,
        repository_plan,
        admission,
        manifests,
        tuple(certificates),
        finalization_inputs,
        finalization,
    )


def _complete_readiness_input(scenario: _ReadinessScenario) -> CloseoutReadinessInput:
    return CloseoutReadinessInput(
        revision=_REVISION,
        repositoryId="sample-repository",
        certificationPlan=scenario.plan,
        profile=ProfileReadinessObservation(
            revision=_REVISION,
            state="admitted-current",
            repositoryPlan=scenario.repository_plan,
        ),
        lifecycle=LifecycleReadinessObservation(revision=_REVISION, state="finalized"),
        gates=tuple(
            GateReadinessObservation(
                revision=_REVISION,
                gate=gate,
                state="passed",
                resultManifest=scenario.manifests[gate - 1],
                certificateState="current-green",
                certificate=scenario.certificates[gate - 1],
            )
            for gate in (1, 2, 3, 4, 5)
        ),
        admission=scenario.admission,
        gateFiveInputs=scenario.finalization_inputs.gateFiveInputs,
        finalizationAuthority=scenario.finalization,
        finalizationInputs=scenario.finalization_inputs,
    )


def _readiness_codes(error: CloseoutReadinessContractError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


def _unadmitted_repository_plan(scenario: _ReadinessScenario) -> RepositoryProfilePlan:
    """The same R22 plan identity with an authority digest the admission never bound."""
    payload = scenario.repository_plan.model_dump(mode="json", exclude={"planDigest"})
    for gate in payload["gates"]:
        gate["profileDigest"] = "9" * 64
    payload["profileDigest"] = "9" * 64
    return RepositoryProfilePlan(**payload, planDigest=content_digest(payload))


def _repository_plan_with_candidate(
    scenario: _ReadinessScenario,
    candidate: CandidateIdentity,
) -> RepositoryProfilePlan:
    """An R22 plan whose candidate identity no longer matches the R11 plan."""
    payload = scenario.repository_plan.model_dump(mode="json", exclude={"planDigest"})
    candidate_payload = candidate.model_dump(mode="json")
    payload["candidateIdentity"] = candidate_payload
    for gate in payload["gates"]:
        gate["candidateIdentity"] = candidate_payload
        gate["planDigest"] = repository_gate_plan_digest(gate)
    return RepositoryProfilePlan(**payload, planDigest=content_digest(payload))


def _rebuilt_rail_result(result: RailResult, **changes: object) -> RailResult:
    """One immutable rail result re-bound to its exact content digest."""
    payload = result.model_dump(mode="json", exclude={"resultDigest"})
    payload.update(changes)
    return RailResult(**payload, resultDigest=content_digest(payload))


def _rebuilt_manifest(
    manifest: GateResultManifest,
    rail_results: tuple[RailResult, ...],
) -> GateResultManifest:
    """One immutable manifest re-bound to its exact content digest."""
    payload = manifest.model_dump(mode="json", exclude={"manifestDigest", "railResults"})
    payload["railResults"] = [result.model_dump(mode="json") for result in rail_results]
    return GateResultManifest(**payload, manifestDigest=content_digest(payload))


def _manifest_with_registry_digest(
    manifest: GateResultManifest,
    registry_digest: str,
) -> GateResultManifest:
    payload = manifest.model_dump(mode="json", exclude={"manifestDigest"})
    payload["registryDigest"] = registry_digest
    return GateResultManifest(**payload, manifestDigest=content_digest(payload))


def test_project_closeout_readiness_refuses_unknown_surface() -> None:
    source = _complete_readiness_input(_readiness_scenario())
    with pytest.raises(CloseoutReadinessContractError) as caught:
        project_closeout_readiness(source, surface=cast(ReadinessSurface, "unsupported-terminal"))
    assert "readiness-surface-unknown" in _readiness_codes(caught.value)


def test_closeout_readiness_requires_the_exact_certifying_plan() -> None:
    source = _complete_readiness_input(_readiness_scenario())
    diagnostic_registry = canonicalize_registry(_readiness_registry(diagnostic=True))
    diagnostic_plan = compile_certification_plan(
        diagnostic_registry,
        profile_id=_PROFILE,
        candidate_identity=_CANDIDATE,
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(source.model_copy(update={"certificationPlan": diagnostic_plan}))
    assert "certifying-plan-required" in _readiness_codes(caught.value)


def test_profile_state_requires_exact_repository_plan_authority() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)

    invalid_with_plan = source.model_copy(
        update={
            "profile": ProfileReadinessObservation(
                revision=_REVISION,
                state="invalid",
                repositoryPlan=scenario.repository_plan,
            )
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(invalid_with_plan)
    assert "profile-state-contradiction" in _readiness_codes(caught.value)

    admitted_without_plan = source.model_copy(
        update={
            "profile": ProfileReadinessObservation(
                revision=_REVISION,
                state="admitted-current",
            )
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(admitted_without_plan)
    assert "profile-authority-missing" in _readiness_codes(caught.value)

    unadmitted_plan = _unadmitted_repository_plan(scenario)
    state_disagrees = source.model_copy(
        update={
            "profile": ProfileReadinessObservation(
                revision=_REVISION,
                state="admitted-current",
                repositoryPlan=unadmitted_plan,
            )
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(state_disagrees)
    assert "profile-state-contradiction" in _readiness_codes(caught.value)

    wrong_candidate = _repository_plan_with_candidate(
        scenario,
        CandidateIdentity(kind="git-tree", value="9" * 40),
    )
    identity_mismatch = source.model_copy(
        update={
            "profile": ProfileReadinessObservation(
                revision=_REVISION,
                state="admitted-current",
                repositoryPlan=wrong_candidate,
            )
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(identity_mismatch)
    assert "repository-plan-identity-mismatch" in _readiness_codes(caught.value)


def test_gate_order_and_result_disposition_contracts_fail_closed() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)

    out_of_order = tuple(
        gate.model_copy(update={"gate": 3}) if gate.gate == 2 else gate for gate in source.gates
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(source.model_copy(update={"gates": out_of_order}))
    assert "gate-catalog-order-mismatch" in _readiness_codes(caught.value)

    green_manifest = source.gates[0].resultManifest
    assert green_manifest is not None
    failed_but_green = source.gates[0].model_copy(
        update={
            "state": "failed",
            "resultManifest": green_manifest,
            "certificateState": "absent",
            "certificate": None,
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(update={"gates": (failed_but_green, *source.gates[1:])})
        )
    assert "gate-result-state-contradiction" in _readiness_codes(caught.value)


def test_current_certificate_contradictions_fail_closed() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)

    green_without_green_result = source.gates[0].model_copy(
        update={
            "state": "running",
            "resultManifest": None,
            "certificateState": "current-green",
            "certificate": scenario.certificates[0],
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(update={"gates": (green_without_green_result, *source.gates[1:])})
        )
    assert "current-certificate-state-contradiction" in _readiness_codes(caught.value)

    borrowed_certificate = source.gates[0].model_copy(
        update={"certificate": scenario.certificates[1]}
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(update={"gates": (borrowed_certificate, *source.gates[1:])})
        )
    assert "certificate-result-mismatch" in _readiness_codes(caught.value)


def test_gate_manifest_and_rail_result_contracts_fail_closed() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)
    advisory = scenario.manifests[0].railResults[0]
    lint = scenario.manifests[0].railResults[1]

    foreign_registry = _manifest_with_registry_digest(scenario.manifests[0], "b" * 64)
    gate_foreign = source.gates[0].model_copy(
        update={
            "resultManifest": foreign_registry,
            "certificateState": "absent",
            "certificate": None,
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(update={"gates": (gate_foreign, *source.gates[1:])})
        )
    assert "gate-manifest-plan-mismatch" in _readiness_codes(caught.value)

    not_applicable_lint = _rebuilt_rail_result(
        lint,
        status="not-applicable",
        artifacts=[],
        blockedBy=[],
    )
    plan_mismatch_manifest = _rebuilt_manifest(
        scenario.manifests[0],
        (advisory, not_applicable_lint),
    )
    gate_plan_mismatch = source.gates[0].model_copy(
        update={
            "resultManifest": plan_mismatch_manifest,
            "certificateState": "absent",
            "certificate": None,
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(update={"gates": (gate_plan_mismatch, *source.gates[1:])})
        )
    assert "rail-result-plan-mismatch" in _readiness_codes(caught.value)

    lint_without_evidence = _rebuilt_rail_result(lint, evidence=[])
    evidence_mismatch_manifest = _rebuilt_manifest(
        scenario.manifests[0],
        (advisory, lint_without_evidence),
    )
    gate_evidence_mismatch = source.gates[0].model_copy(
        update={
            "resultManifest": evidence_mismatch_manifest,
            "certificateState": "absent",
            "certificate": None,
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(update={"gates": (gate_evidence_mismatch, *source.gates[1:])})
        )
    assert "rail-result-evidence-mismatch" in _readiness_codes(caught.value)


def test_report_only_pass_rail_preserves_typed_state() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)
    advisory_pass = _readiness_manifest(
        scenario.registry,
        scenario.plan,
        1,
        report_only_pass=True,
    )
    not_started = tuple(
        GateReadinessObservation(revision=_REVISION, gate=gate, state="not-started")
        for gate in (1, 2, 3, 4, 5)
    )
    gate_one = not_started[0].model_copy(
        update={"state": "passed", "resultManifest": advisory_pass}
    )
    partial = source.model_copy(
        update={
            "lifecycle": LifecycleReadinessObservation(revision=_REVISION, state="admitted"),
            "gates": (gate_one, *not_started[1:]),
            "finalizationAuthority": None,
            "finalizationInputs": None,
        }
    )
    projection = compile_closeout_readiness(partial)
    assert projection.gates[0].rails[0].state == "report-only-pass"
    assert projection.certificationReady is False


def test_certificate_chain_and_gate_five_inputs_fail_closed() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)

    stale_two = tuple(
        gate.model_copy(update={"certificateState": "stale", "certificate": None})
        if gate.gate == 2
        else gate
        for gate in source.gates
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(source.model_copy(update={"gates": stale_two}))
    assert "certificate-prefix-invalid" in _readiness_codes(caught.value)

    different_memory = _readiness_memory_inputs().model_copy(
        update={"memoryTree": CandidateIdentity(kind="git-tree", value="n" * 40)}
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(source.model_copy(update={"gateFiveInputs": different_memory}))
    assert "gate-five-input-contradiction" in _readiness_codes(caught.value)


def test_gate_prerequisite_must_be_current_green_before_start() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)
    gate_one = source.gates[0].model_copy(
        update={"certificateState": "absent", "certificate": None}
    )
    gate_two = source.gates[1].model_copy(
        update={
            "state": "running",
            "resultManifest": None,
            "certificateState": "absent",
            "certificate": None,
        }
    )
    not_started = tuple(
        GateReadinessObservation(revision=_REVISION, gate=gate, state="not-started")
        for gate in (3, 4, 5)
    )
    partial = source.model_copy(
        update={
            "lifecycle": LifecycleReadinessObservation(revision=_REVISION, state="admitted"),
            "gates": (gate_one, gate_two, *not_started),
            "finalizationAuthority": None,
            "finalizationInputs": None,
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(partial)
    assert "gate-prerequisite-not-current" in _readiness_codes(caught.value)


def test_diagnostic_catalog_candidate_and_gate_rails_fail_closed() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)
    diagnostic_registry = canonicalize_registry(_readiness_registry(diagnostic=True))
    diagnostic_plan = compile_certification_plan(
        diagnostic_registry,
        profile_id=_PROFILE,
        candidate_identity=_CANDIDATE,
    )
    gate_one_manifest = _readiness_manifest(diagnostic_registry, diagnostic_plan, 1)

    duplicated = source.model_copy(
        update={
            "diagnostics": (
                DiagnosticReadinessObservation(
                    revision=_REVISION,
                    plan=diagnostic_plan,
                    resultManifest=gate_one_manifest,
                ),
                DiagnosticReadinessObservation(
                    revision=_REVISION,
                    plan=diagnostic_plan,
                    resultManifest=gate_one_manifest,
                ),
            )
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(duplicated)
    assert "diagnostic-catalog-noncanonical" in _readiness_codes(caught.value)

    foreign = compile_certification_plan(
        diagnostic_registry,
        profile_id=_PROFILE,
        candidate_identity=CandidateIdentity(kind="git-tree", value="9" * 40),
    )
    foreign_manifest = _readiness_manifest(diagnostic_registry, foreign, 1)
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(
                update={
                    "diagnostics": (
                        DiagnosticReadinessObservation(
                            revision=_REVISION,
                            plan=foreign,
                            resultManifest=foreign_manifest,
                        ),
                    )
                }
            )
        )
    assert "diagnostic-candidate-mismatch" in _readiness_codes(caught.value)

    two_gate_registry = canonicalize_registry(_readiness_registry(diagnostic=True, gates=(1, 2)))
    two_gate_plan = compile_certification_plan(
        two_gate_registry,
        profile_id=_PROFILE,
        candidate_identity=_CANDIDATE,
    )
    gate_two_manifest = _readiness_manifest(two_gate_registry, two_gate_plan, 2)
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(
                update={
                    "diagnostics": (
                        DiagnosticReadinessObservation(
                            revision=_REVISION,
                            plan=diagnostic_plan,
                            resultManifest=gate_two_manifest,
                        ),
                    )
                }
            )
        )
    assert "diagnostic-gate-unplanned" in _readiness_codes(caught.value)

    sparse_registry = canonicalize_registry(_readiness_registry(diagnostic=True, advisory=False))
    sparse_plan = compile_certification_plan(
        sparse_registry,
        profile_id=_PROFILE,
        candidate_identity=_CANDIDATE,
    )
    sparse_manifest = _readiness_manifest(sparse_registry, sparse_plan, 1)
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(
                update={
                    "diagnostics": (
                        DiagnosticReadinessObservation(
                            revision=_REVISION,
                            plan=sparse_plan,
                            resultManifest=sparse_manifest,
                        ),
                    )
                }
            )
        )
    assert "diagnostic-rail-contract-mismatch" in _readiness_codes(caught.value)


def test_finalization_lifecycle_contracts_fail_closed() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)

    unresolved = source.model_copy(
        update={
            "profile": ProfileReadinessObservation(revision=_REVISION, state="unresolved"),
            "lifecycle": LifecycleReadinessObservation(
                revision=_REVISION,
                state="finalization-pending",
            ),
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(unresolved)
    assert "finalization-before-certification" in _readiness_codes(caught.value)

    missing_authority = source.model_copy(update={"finalizationAuthority": None})
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(missing_authority)
    assert "finalization-authority-missing" in _readiness_codes(caught.value)

    stale_inputs = FinalizationCurrentInputs(
        gateFiveInputs=scenario.finalization_inputs.gateFiveInputs,
        taskIntentAuthorityDigest="9" * 64,
        journalAuthorityDigest=scenario.finalization_inputs.journalAuthorityDigest,
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(source.model_copy(update={"finalizationInputs": stale_inputs}))
    assert "finalization-authority-mismatch" in _readiness_codes(caught.value)

    premature = source.model_copy(
        update={
            "lifecycle": LifecycleReadinessObservation(revision=_REVISION, state="admitted"),
            "finalizationInputs": None,
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(premature)
    assert "finalization-authority-state-contradiction" in _readiness_codes(caught.value)


def test_readiness_observation_validators_refuse_incoherent_shapes() -> None:
    scenario = _readiness_scenario()
    with pytest.raises(ValidationError):
        LifecycleReadinessObservation(revision=_REVISION, state="admission-refused")
    with pytest.raises(ValidationError):
        LifecycleReadinessObservation(
            revision=_REVISION,
            state="admitted",
            code="refusal-code",
            correctiveOwner="portable-owner",
            evidence=(
                ReadinessEvidenceReference(
                    evidenceId="refusal-evidence",
                    sha256="a" * 64,
                    size=1,
                    reference="evidence://refusal",
                ),
            ),
        )
    with pytest.raises(ValidationError):
        GateReadinessObservation(revision=_REVISION, gate=1, state="blocked")
    with pytest.raises(ValidationError):
        GateReadinessObservation(
            revision=_REVISION,
            gate=1,
            state="not-started",
            blockedBy=("gate-0",),
        )
    with pytest.raises(ValidationError):
        GateReadinessObservation(revision=_REVISION, gate=1, state="passed")
    with pytest.raises(ValidationError):
        GateReadinessObservation(
            revision=_REVISION,
            gate=1,
            state="running",
            resultManifest=scenario.manifests[0],
        )
    with pytest.raises(ValidationError):
        GateReadinessObservation(
            revision=_REVISION,
            gate=1,
            state="not-started",
            certificateState="current-green",
        )


def test_readiness_projection_digest_verification_refuses_tampering() -> None:
    projection = compile_closeout_readiness(_complete_readiness_input(_readiness_scenario()))
    tampered = projection.model_dump(mode="json")
    tampered["projectionDigest"] = "0" * 64
    with pytest.raises(ValidationError):
        projection.__class__.model_validate(tampered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
