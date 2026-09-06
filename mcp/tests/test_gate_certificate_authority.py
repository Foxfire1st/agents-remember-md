"""CCR-R21 content-addressed certificate, invalidation, and storage contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from agents_remember.certification import (
    build_rail_result,
    canonicalize_registry,
    compile_certification_admission,
    compile_certification_plan,
    compile_finalization_authority,
    compile_gate_certificate,
    compile_gate_result_manifest,
    compile_repository_profile_plan,
    plan_certificate_reuse,
    validate_certificate_chain,
    validate_finalization_currentness,
)
from agents_remember.certification import certificate_admission as admission_module
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    InputChangeClass,
)
from agents_remember.certification.certificate_models import (
    CertificateInputIdentity,
    CertificationAdmissionManifest,
    CoherenceSubrecordIdentity,
    CreationProvenance,
    FinalizationCurrentInputs,
    GateCertificate,
    GateCertificateIssuanceContext,
    GateFiveSemanticInputs,
)
from agents_remember.certification.certificate_store import (
    CertificateStorePolicy,
    ContentAddressedCertificateStore,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationPlan,
    CompiledRail,
    GatePlan,
    GateResultAdmission,
    GateResultManifest,
    ProfileKind,
    RailAdapterDefinition,
    RailApplicability,
    RailArtifactResult,
    RailDefinition,
    RailEvidenceContract,
    RailEvidenceReference,
    RailRegistry,
    RailRuntimeInputs,
    RailStatus,
    RailTerminalObservation,
    RegistryProfile,
)
from agents_remember.certification.repository_profiles import (
    canonicalize_repository_profile,
    repository_profile_digest,
)
from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
    RepositoryProfilePlan,
)
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import GateId, RailIdentity
from repository_profile_test_support import fixture_profile

_CANDIDATE = CandidateIdentity(kind="git-tree", value="c" * 40)
_DIGEST = "a" * 64
_PROFILE_ID = "closeout-targeted"
_ALL_GATES: tuple[GateId, ...] = (1, 2, 3, 4, 5)
type _InvalidationCase = tuple[
    InputChangeClass, tuple[GateId, ...], tuple[str, ...], tuple[GateId, ...]
]


@dataclass(frozen=True)
class _Scenario:
    registry: CanonicalRailRegistry
    plan: CertificationPlan
    profile: CanonicalRepositoryCertificationProfile
    repository_plan: RepositoryProfilePlan
    admission: CertificationAdmissionManifest


def _provenance(marker: str = "one") -> CreationProvenance:
    return CreationProvenance(
        createdAt=f"2026-09-02T00:00:0{1 if marker == 'one' else 2}+00:00",
        producer=f"test-{marker}",
        evidenceRef=f"evidence://{marker}",
    )


def _scenario(
    *,
    profile_kind: ProfileKind = "certifying",
    gate_four_image: str | None = None,
) -> _Scenario:
    raw_profile = fixture_profile()
    if gate_four_image is not None:
        rails = tuple(
            rail.model_copy(
                update={
                    "runtimeInputs": rail.runtimeInputs.model_copy(
                        update={"imageDigest": gate_four_image}
                    )
                }
            )
            if rail.gate == 4
            else rail
            for rail in raw_profile.rails
        )
        provisional = raw_profile.model_copy(update={"rails": rails, "profileDigest": "0" * 64})
        raw_profile = provisional.model_copy(
            update={"profileDigest": repository_profile_digest(provisional)}
        )
    profile = canonicalize_repository_profile(raw_profile)
    repository_plan = compile_repository_profile_plan(
        profile,
        selection_id=_PROFILE_ID,
        candidate_identity=_CANDIDATE,
    )
    repository_rails = tuple(rail for gate in repository_plan.gates for rail in gate.rails)
    memory_prerequisite = repository_plan.gates[-1].rails[-1].identity
    registry = canonicalize_registry(
        RailRegistry(
            registryId="fixture-closeout-certificates",
            repositoryId=profile.profile.repositoryId,
            profiles=(
                RegistryProfile(
                    profileId=_PROFILE_ID,
                    kind=profile_kind,
                    gates=(1, 2, 3, 4, 5),
                ),
            ),
            rails=(
                *(_registry_rail(rail) for rail in repository_rails),
                _memory_rail(memory_prerequisite),
            ),
        )
    )
    plan = compile_certification_plan(
        registry,
        profile_id=_PROFILE_ID,
        candidate_identity=_CANDIDATE,
    )
    admission = compile_certification_admission(
        registry,
        plan,
        profile,
        repository_plan,
        provenance=_provenance(),
    )
    return _Scenario(registry, plan, profile, repository_plan, admission)


def _registry_rail(rail: CompiledRail) -> RailDefinition:
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


def _memory_rail(prerequisite: RailIdentity) -> RailDefinition:
    return RailDefinition(
        identity=RailIdentity(railId="memory-coherence", version="1.0.0"),
        gate=5,
        railClass="memory-quality",
        authority="memory-domain",
        ownerClass="memory-curator",
        correctiveOwner="memory-curator",
        posture="enforcing",
        orderKey="memory-coherence",
        prerequisites=(prerequisite,),
        adapter=RailAdapterDefinition(
            adapterKind="memory-checker",
            adapterId="memory-coherence-checker",
            configurationDigest=_DIGEST,
            executionEvidence="memory-checker://coherence",
        ),
        runtimeInputs=RailRuntimeInputs(runtimeIdentity="memory-quality-v1"),
        applicability=(
            RailApplicability(
                profileId=_PROFILE_ID,
                status="applicable",
                selectionIdentity="memory-affected-closure",
                population="exact candidate-pair affected memory closure",
            ),
        ),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId="memory-coherence-evidence",
                mediaType="application/json",
                maxBytes=4096,
            ),
        ),
    )


def _gate(plan: CertificationPlan, gate: GateId) -> GatePlan:
    return next(item for item in plan.gates if item.gate == gate)


def _manifest(
    scenario: _Scenario,
    gate: GateId,
    *,
    status: RailStatus = "pass",
    include_artifacts: bool = True,
) -> GateResultManifest:
    gate_plan = _gate(scenario.plan, gate)
    results = []
    for rail in gate_plan.rails:
        artifacts = (
            tuple(
                RailArtifactResult(
                    artifactId=item.artifactId,
                    sha256=content_digest({"artifact": item.artifactId}),
                    size=32,
                    evidenceRef=f"artifact://{item.artifactId}",
                )
                for item in rail.outputArtifacts
            )
            if include_artifacts and status == "pass"
            else ()
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
        scenario.registry,
        scenario.plan,
        gate_plan,
        results,
        GateResultAdmission(
            profileId=_PROFILE_ID,
            candidateIdentity=_CANDIDATE,
            altitude=scenario.plan.profileKind,
        ),
    )


def _memory_inputs(marker: str = "a") -> GateFiveSemanticInputs:
    return GateFiveSemanticInputs(
        memoryTree=CandidateIdentity(kind="git-tree", value=marker * 40),
        affectedClosurePlanDigest=content_digest({"closure": marker}),
        memoryCheckerRegistryDigest=content_digest({"checkers": marker}),
        coherenceSubrecords=(
            CoherenceSubrecordIdentity(
                subrecordId="route-entity",
                contentDigest=content_digest({"coherence": marker}),
            ),
        ),
        candidatePairAuthorityDigest=content_digest({"pair": marker}),
    )


def _certify_through(
    scenario: _Scenario,
    final_gate: GateId,
    *,
    memory_inputs: GateFiveSemanticInputs | None = None,
) -> tuple[GateCertificate, ...]:
    certificates: list[GateCertificate] = []
    for gate in _ALL_GATES[:final_gate]:
        certificates.append(
            compile_gate_certificate(
                scenario.admission,
                _gate(scenario.plan, gate),
                _manifest(scenario, gate),
                certificates,
                GateCertificateIssuanceContext(
                    provenance=_provenance(),
                    gateFiveInputs=memory_inputs if gate == 5 else None,
                ),
            )
        )
    return tuple(certificates)


def _finding_codes(error: CertificationContractError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


def test_five_gate_chain_separates_suite_artifacts_and_finalization_authority() -> None:
    scenario = _scenario()
    memory_inputs = _memory_inputs()
    chain = _certify_through(scenario, 5, memory_inputs=memory_inputs)
    gate_three = chain[2].semanticEnvelope

    assert tuple(item.semanticEnvelope.gate for item in chain) == (1, 2, 3, 4, 5)
    assert gate_three.consumedArtifacts
    assert {item.producerGate for item in gate_three.consumedArtifacts} == {2}
    assert gate_three.resultManifestDigest != chain[1].semanticEnvelope.resultManifestDigest
    assert chain[4].semanticEnvelope.gateFiveInputs == memory_inputs
    assert chain[4].semanticEnvelope.directPredecessors == tuple(
        item.identity for item in chain[:4]
    )

    current_inputs = FinalizationCurrentInputs(
        gateFiveInputs=memory_inputs,
        taskIntentAuthorityDigest="b" * 64,
        journalAuthorityDigest="d" * 64,
    )
    authority = compile_finalization_authority(
        scenario.admission,
        chain,
        current_inputs,
        _provenance(),
    )
    assert (
        validate_finalization_currentness(
            authority,
            scenario.admission,
            chain,
            current_inputs,
        )
        == authority
    )
    with pytest.raises(CertificationContractError) as caught:
        validate_finalization_currentness(
            authority,
            scenario.admission,
            chain,
            current_inputs.model_copy(update={"journalAuthorityDigest": "e" * 64}),
        )
    assert _finding_codes(caught.value) == {"finalization-authority-mismatch"}


def test_red_partial_diagnostic_and_combined_results_never_publish_certificates() -> None:
    scenario = _scenario()
    gate_one = _certify_through(scenario, 1)
    red = _manifest(scenario, 2, status="fail")
    assert red.disposition == "red"
    with pytest.raises(CertificationContractError) as caught:
        compile_gate_certificate(
            scenario.admission,
            _gate(scenario.plan, 2),
            red,
            gate_one,
            GateCertificateIssuanceContext(provenance=_provenance()),
        )
    assert _finding_codes(caught.value) == {"non-green-result-manifest"}

    with pytest.raises(CertificationContractError) as caught:
        _manifest(scenario, 2, include_artifacts=False)
    assert "required-result-artifact-missing" in _finding_codes(caught.value)

    diagnostic = _scenario(profile_kind="diagnostic")
    with pytest.raises(CertificationContractError) as caught:
        compile_gate_certificate(
            diagnostic.admission,
            _gate(diagnostic.plan, 1),
            _manifest(diagnostic, 1),
            (),
            GateCertificateIssuanceContext(provenance=_provenance()),
        )
    assert _finding_codes(caught.value) == {"diagnostic-certificate-promotion"}

    gate_two_result = _manifest(scenario, 2).railResults[0]
    with pytest.raises(CertificationContractError) as caught:
        compile_gate_result_manifest(
            scenario.registry,
            scenario.plan,
            _gate(scenario.plan, 3),
            (gate_two_result,),
            GateResultAdmission(
                profileId=_PROFILE_ID,
                candidateIdentity=_CANDIDATE,
                altitude="certifying",
            ),
        )
    assert {"rail-result-omitted", "unplanned-rail-result"} <= _finding_codes(caught.value)


def test_reuse_is_dependency_aware_and_refuses_forged_or_stale_identity() -> None:
    scenario = _scenario()
    memory_inputs = _memory_inputs()
    chain = _certify_through(scenario, 5, memory_inputs=memory_inputs)

    memory_repair = plan_certificate_reuse(
        scenario.admission,
        chain,
        (CertificateInputChange(changeClass="memory-onboarding", reason="memory repair"),),
        gate_five_inputs=memory_inputs,
    )
    assert memory_repair.reusedCertificates == tuple(item.identity for item in chain[:4])
    assert memory_repair.firstGateToRun == 5

    unchanged = plan_certificate_reuse(
        scenario.admission,
        chain,
        (
            CertificateInputChange(
                changeClass="journal-review-approval-attempt",
                reason="finalization interrupted after journal write",
            ),
        ),
        gate_five_inputs=memory_inputs,
    )
    assert unchanged.reusedCertificates == tuple(item.identity for item in chain)
    assert unchanged.zeroGateStarts
    assert unchanged.firstGateToRun is None

    changed_profile = _scenario(gate_four_image="f" * 64)
    image_change = plan_certificate_reuse(
        changed_profile.admission,
        chain,
        (
            CertificateInputChange(
                changeClass="runtime-toolchain-executor-image",
                consumingGates=(4,),
                reason="Gate-4 image changed",
            ),
        ),
        gate_five_inputs=memory_inputs,
    )
    assert image_change.reusedCertificates == tuple(item.identity for item in chain[:3])
    assert image_change.firstGateToRun == 4

    first = chain[0]
    inputs = list(first.semanticEnvelope.semanticInputs)
    inputs[0] = inputs[0].model_copy(update={"contentDigest": "f" * 64})
    forged_envelope = first.semanticEnvelope.model_copy(update={"semanticInputs": tuple(inputs)})
    forged = GateCertificate(
        semanticEnvelope=forged_envelope,
        certificateDigest=content_digest(forged_envelope),
        provenance=first.provenance,
    )
    with pytest.raises(CertificationContractError) as caught:
        validate_certificate_chain(scenario.admission, (forged,))
    assert _finding_codes(caught.value) == {"stale-gate-certificate"}


def test_profile_mismatch_and_unproven_runtime_change_fail_closed() -> None:
    scenario = _scenario()
    local_plan = compile_repository_profile_plan(
        scenario.profile,
        selection_id="local-targeted",
        candidate_identity=_CANDIDATE,
    )
    with pytest.raises(CertificationContractError) as caught:
        compile_certification_admission(
            scenario.registry,
            scenario.plan,
            scenario.profile,
            local_plan,
            provenance=_provenance(),
        )
    assert _finding_codes(caught.value) == {"profile-registry-identity-mismatch"}

    with pytest.raises(ValueError, match="require every declared consuming gate"):
        CertificateInputChange(
            changeClass="runtime-toolchain-executor-image",
            reason="consumer was not proven",
        )


def test_content_store_is_exact_atomic_bounded_and_has_no_historical_lookup(
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    red = _manifest(scenario, 2, status="fail")
    store = ContentAddressedCertificateStore(
        tmp_path / "objects",
        CertificateStorePolicy(
            scopeId="operation-1",
            maxObjects=16,
            maxBytes=1_000_000,
            reclamationOwner="closeout-operation-owner",
        ),
    )

    path = store.publish(scenario.admission)
    assert (
        store.load(CertificationAdmissionManifest, scenario.admission.admissionDigest)
        == scenario.admission
    )
    assert store.publish(red).is_file()
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = tuple(pool.map(lambda _: store.publish(scenario.admission), range(8)))
    assert set(paths) == {path}

    with pytest.raises(CertificationContractError) as caught:
        store.load(CertificationAdmissionManifest, "f" * 64)
    assert _finding_codes(caught.value) == {"certificate-object-missing"}
    with pytest.raises(CertificationContractError) as caught:
        store.load(CertificationAdmissionManifest, "latest")
    assert _finding_codes(caught.value) == {"certificate-object-digest-invalid"}

    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(CertificationContractError) as caught:
        store.load(CertificationAdmissionManifest, scenario.admission.admissionDigest)
    assert _finding_codes(caught.value) == {"certificate-object-invalid"}

    bounded = ContentAddressedCertificateStore(
        tmp_path / "bounded",
        CertificateStorePolicy(
            scopeId="operation-2",
            maxObjects=1,
            maxBytes=1_000_000,
            reclamationOwner="closeout-operation-owner",
        ),
    )
    bounded.publish(scenario.admission)
    with pytest.raises(CertificationContractError) as caught:
        bounded.publish(red)
    assert _finding_codes(caught.value) == {"certificate-store-capacity-exceeded"}


def test_admission_refuses_conflicts_misalignment_and_unproven_candidate() -> None:
    scenario = _scenario()
    semantic_input = CertificateInputIdentity(
        inputKind="test-input",
        inputId="one",
        contentDigest="a" * 64,
    )
    with pytest.raises(CertificationContractError) as caught:
        admission_module.canonicalize_certificate_inputs(
            (semantic_input, semantic_input.model_copy(update={"contentDigest": "f" * 64}))
        )
    assert _finding_codes(caught.value) == {"semantic-input-conflict"}

    non_tree_plan = scenario.plan.model_copy(
        update={"candidateIdentity": CandidateIdentity(kind="commit", value="f" * 40)}
    )
    with pytest.raises(CertificationContractError) as caught:
        compile_certification_admission(
            scenario.registry,
            non_tree_plan,
            scenario.profile,
            scenario.repository_plan,
            provenance=_provenance(),
        )
    assert _finding_codes(caught.value) == {"candidate-code-tree-required"}

    generic_gate = _gate(scenario.plan, 4)
    repository_gate = scenario.repository_plan.gates[3]
    not_applicable = repository_gate.model_copy(
        update={
            "applicability": "not-applicable",
            "reason": "not selected",
            "rails": (),
            "semanticInputs": (),
            "waves": (),
        }
    )
    with pytest.raises(CertificationContractError) as caught:
        admission_module._require_rail_alignment(generic_gate, not_applicable)
    assert _finding_codes(caught.value) == {"gate-applicability-mismatch"}

    generic_not_applicable = generic_gate.model_copy(
        update={
            "rails": tuple(
                rail.model_copy(
                    update={
                        "applicability": rail.applicability.model_copy(
                            update={
                                "status": "not-applicable",
                                "population": None,
                                "reason": "not selected",
                            }
                        )
                    }
                )
                for rail in generic_gate.rails
            )
        }
    )
    admission_module._require_rail_alignment(generic_not_applicable, not_applicable)

    with pytest.raises(CertificationContractError) as caught:
        admission_module._require_rail_alignment(
            generic_gate,
            repository_gate.model_copy(update={"rails": ()}),
        )
    assert _finding_codes(caught.value) == {"profile-registry-rail-mismatch"}

    with pytest.raises(ValueError, match="incomplete repository gate plan"):
        admission_module._compile_admission_gate(_gate(scenario.plan, 1), None)
