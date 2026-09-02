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
from agents_remember.certification import certificate_authority as authority_module
from agents_remember.certification import certificate_store as store_module
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    InputChangeClass,
    classify_certificate_invalidation,
)
from agents_remember.certification.certificate_models import (
    CertificateInputIdentity,
    CertificationAdmissionManifest,
    CoherenceSubrecordIdentity,
    CreationProvenance,
    FinalizationCurrentInputs,
    GateCertificate,
    GateCertificateIssuanceContext,
    GateCertificateSemanticEnvelope,
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
    GateId,
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
    RailIdentity,
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


def test_admission_and_gate_certificates_bind_exact_semantics_not_provenance() -> None:
    scenario = _scenario()
    second_admission = compile_certification_admission(
        scenario.registry,
        scenario.plan,
        scenario.profile,
        scenario.repository_plan,
        provenance=_provenance("two"),
    )
    first = compile_gate_certificate(
        scenario.admission,
        _gate(scenario.plan, 1),
        _manifest(scenario, 1),
        (),
        GateCertificateIssuanceContext(provenance=_provenance()),
    )
    second = compile_gate_certificate(
        second_admission,
        _gate(scenario.plan, 1),
        _manifest(scenario, 1),
        (),
        GateCertificateIssuanceContext(provenance=_provenance("two")),
    )

    assert scenario.admission.admissionDigest == second_admission.admissionDigest
    assert scenario.admission.provenance != second_admission.provenance
    assert first.certificateDigest == second.certificateDigest
    assert first.provenance != second.provenance
    assert first.semanticEnvelope.candidateCodeTree == _CANDIDATE
    assert first.semanticEnvelope.railInventory
    assert first.semanticEnvelope.evidenceInventory


def test_certificate_inventories_refuse_duplicate_identities() -> None:
    chain = _certify_through(_scenario(), 3)
    gate_two = chain[1].semanticEnvelope
    envelope = chain[2].semanticEnvelope
    cases: tuple[tuple[GateCertificateSemanticEnvelope, dict[str, object], str], ...] = (
        (
            envelope,
            {"railInventory": (envelope.railInventory[0],) * 2},
            "certificate rail inventory must be unique and canonical",
        ),
        (
            gate_two,
            {"artifactInventory": (gate_two.artifactInventory[0],) * 2},
            "certificate artifact inventory must be unique and canonical",
        ),
        (
            envelope,
            {"evidenceInventory": (envelope.evidenceInventory[0],) * 2},
            "certificate evidence inventory must be unique and canonical",
        ),
        (
            envelope,
            {"consumedArtifacts": (envelope.consumedArtifacts[0],) * 2},
            "consumed artifact identities must be unique and canonical",
        ),
    )

    for source, update, message in cases:
        malformed = source.model_copy(update=update).model_dump(mode="json")
        with pytest.raises(ValueError, match=message):
            GateCertificateSemanticEnvelope.model_validate(malformed)


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


def test_normative_invalidation_matrix() -> None:
    cases: tuple[_InvalidationCase, ...] = (
        ("code", (), (), (1, 2, 3, 4, 5)),
        ("gate-1-input", (), (), (1, 2, 3, 4, 5)),
        ("gate-2-input", (), (), (2, 3, 4, 5)),
        ("gate-3-input", (), (), (3, 4, 5)),
        ("gate-4-input", (), (), (4, 5)),
        ("runtime-toolchain-executor-image", (3,), (), (3, 4, 5)),
        ("memory-onboarding", (), (), (5,)),
        ("coherence-evidence", (), ("route-entity",), (5,)),
        ("topology-intent", (2,), (), (2, 3, 4, 5)),
        ("journal-review-approval-attempt", (), (), ()),
        ("unchanged-interruption", (), (), ()),
        ("unclassified", (), (), (1, 2, 3, 4, 5)),
    )
    for change_class, gates, subrecords, invalidated in cases:
        change = CertificateInputChange(
            changeClass=change_class,
            consumingGates=gates,
            affectedGateFiveSubrecords=subrecords,
            reason=f"exercise {change_class}",
        )
        decision = classify_certificate_invalidation((change,))
        assert decision.invalidatedGates == invalidated
        assert decision.finalizationRevalidationRequired


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

    path = store.publish_admission(scenario.admission)
    assert store.load_admission(scenario.admission.admissionDigest) == scenario.admission
    assert store.publish_result_manifest(red).is_file()
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = tuple(pool.map(lambda _: store.publish_admission(scenario.admission), range(8)))
    assert set(paths) == {path}

    with pytest.raises(CertificationContractError) as caught:
        store.load_admission("f" * 64)
    assert _finding_codes(caught.value) == {"certificate-object-missing"}
    with pytest.raises(CertificationContractError) as caught:
        store.load_admission("latest")
    assert _finding_codes(caught.value) == {"certificate-object-digest-invalid"}

    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(CertificationContractError) as caught:
        store.load_admission(scenario.admission.admissionDigest)
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
    bounded.publish_admission(scenario.admission)
    with pytest.raises(CertificationContractError) as caught:
        bounded.publish_result_manifest(red)
    assert _finding_codes(caught.value) == {"certificate-store-capacity-exceeded"}


def test_certificate_models_refuse_forged_noncanonical_envelopes() -> None:
    scenario = _scenario()
    memory_inputs = _memory_inputs()
    chain = _certify_through(scenario, 5, memory_inputs=memory_inputs)

    non_tree = memory_inputs.model_dump(mode="json")
    non_tree["memoryTree"] = {"kind": "commit", "value": "f" * 40}
    with pytest.raises(ValueError, match="exact Git tree"):
        GateFiveSemanticInputs.model_validate(non_tree)

    gate_identity = scenario.admission.semanticEnvelope.gates[0]
    malformed_gate_identity = gate_identity.model_dump(mode="json")
    malformed_gate_identity["repositoryGatePlanDigest"] = None
    with pytest.raises(ValueError, match="Gates 1-4 require"):
        type(gate_identity).model_validate(malformed_gate_identity)

    malformed_admission_envelope = scenario.admission.semanticEnvelope.model_dump(mode="json")
    malformed_admission_envelope["gates"][0], malformed_admission_envelope["gates"][1] = (
        malformed_admission_envelope["gates"][1],
        malformed_admission_envelope["gates"][0],
    )
    with pytest.raises(ValueError, match="ordered Gates 1-5"):
        type(scenario.admission.semanticEnvelope).model_validate(malformed_admission_envelope)

    forged_admission = scenario.admission.model_dump(mode="json")
    forged_admission["admissionDigest"] = "f" * 64
    with pytest.raises(ValueError, match="admission digest"):
        CertificationAdmissionManifest.model_validate(forged_admission)

    duplicate_subrecords = memory_inputs.model_dump(mode="json")
    duplicate_subrecords["coherenceSubrecords"].append(
        {"subrecordId": "route-entity", "contentDigest": "f" * 64}
    )
    with pytest.raises(ValueError, match="subrecords must be unique and canonical"):
        GateFiveSemanticInputs.model_validate(duplicate_subrecords)

    gate_three = chain[2].semanticEnvelope
    malformed_envelopes = (
        ({"directPredecessors": ()}, "exact earlier-gate prefix"),
        ({"repositoryGatePlanDigest": None}, "only Gates 1-4"),
        ({"gateFiveInputs": memory_inputs}, "only Gate 5"),
        (
            {"semanticInputs": (gate_three.semanticInputs[0],) * 2},
            "semantic inputs must be unique and canonical",
        ),
    )
    for update, message in malformed_envelopes:
        malformed = gate_three.model_copy(update=update).model_dump(mode="json")
        with pytest.raises(ValueError, match=message):
            GateCertificateSemanticEnvelope.model_validate(malformed)

    forged_certificate = chain[0].model_dump(mode="json")
    forged_certificate["certificateDigest"] = "f" * 64
    with pytest.raises(ValueError, match="certificate digest"):
        GateCertificate.model_validate(forged_certificate)

    current_inputs = FinalizationCurrentInputs(
        gateFiveInputs=memory_inputs,
        taskIntentAuthorityDigest="b" * 64,
        journalAuthorityDigest="d" * 64,
    )
    finalization = compile_finalization_authority(
        scenario.admission,
        chain,
        current_inputs,
        _provenance(),
    )
    malformed_final_envelope = finalization.semanticEnvelope.model_dump(mode="json")
    malformed_final_envelope["certificates"][0], malformed_final_envelope["certificates"][1] = (
        malformed_final_envelope["certificates"][1],
        malformed_final_envelope["certificates"][0],
    )
    with pytest.raises(ValueError, match="ordered Gates 1-5"):
        type(finalization.semanticEnvelope).model_validate(malformed_final_envelope)

    forged_finalization = finalization.model_dump(mode="json")
    forged_finalization["authorityDigest"] = "f" * 64
    with pytest.raises(ValueError, match="authority digest"):
        type(finalization).model_validate(forged_finalization)


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


def test_certificate_publication_refuses_stale_dependencies_and_incomplete_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario()
    memory_inputs = _memory_inputs()
    chain = _certify_through(scenario, 5, memory_inputs=memory_inputs)

    with pytest.raises(CertificationContractError) as caught:
        compile_gate_certificate(
            scenario.admission,
            _gate(scenario.plan, 1),
            _manifest(scenario, 1),
            (),
            GateCertificateIssuanceContext(
                provenance=_provenance(),
                gateFiveInputs=memory_inputs,
            ),
        )
    assert _finding_codes(caught.value) == {"gate-five-input-mismatch"}

    with monkeypatch.context() as patch:
        patch.setattr(authority_module, "_bind_consumed_artifacts", lambda *_: ())
        with pytest.raises(CertificationContractError) as caught:
            compile_gate_certificate(
                scenario.admission,
                _gate(scenario.plan, 3),
                _manifest(scenario, 3),
                chain[:2],
                GateCertificateIssuanceContext(provenance=_provenance()),
            )
    assert _finding_codes(caught.value) == {"gate-three-suite-artifacts-unbound"}

    with pytest.raises(CertificationContractError) as caught:
        validate_certificate_chain(scenario.admission, (chain[1],))
    assert _finding_codes(caught.value) == {"certificate-prefix-invalid"}

    current_inputs = FinalizationCurrentInputs(
        gateFiveInputs=memory_inputs,
        taskIntentAuthorityDigest="b" * 64,
        journalAuthorityDigest="d" * 64,
    )
    with pytest.raises(CertificationContractError) as caught:
        compile_finalization_authority(
            scenario.admission,
            chain[:4],
            current_inputs,
            _provenance(),
        )
    assert _finding_codes(caught.value) == {"certificate-chain-incomplete"}

    stale_manifest = _manifest(scenario, 1).model_copy(
        update={"candidateIdentity": CandidateIdentity(kind="git-tree", value="f" * 40)}
    )
    with pytest.raises(CertificationContractError) as caught:
        compile_gate_certificate(
            scenario.admission,
            _gate(scenario.plan, 1),
            stale_manifest,
            (),
            GateCertificateIssuanceContext(provenance=_provenance()),
        )
    assert _finding_codes(caught.value) == {"result-manifest-authority-mismatch"}

    with pytest.raises(CertificationContractError) as caught:
        compile_gate_certificate(
            scenario.admission,
            _gate(scenario.plan, 2),
            _manifest(scenario, 2),
            (),
            GateCertificateIssuanceContext(provenance=_provenance()),
        )
    assert _finding_codes(caught.value) == {"predecessor-certificate-mismatch"}

    with pytest.raises(CertificationContractError) as caught:
        validate_certificate_chain(scenario.admission, chain)
    assert _finding_codes(caught.value) == {"gate-five-current-inputs-missing"}

    empty_artifact_envelope = chain[1].semanticEnvelope.model_copy(update={"artifactInventory": ()})
    empty_artifact_gate_two = GateCertificate(
        semanticEnvelope=empty_artifact_envelope,
        certificateDigest=content_digest(empty_artifact_envelope),
        provenance=chain[1].provenance,
    )
    with pytest.raises(CertificationContractError) as caught:
        compile_gate_certificate(
            scenario.admission,
            _gate(scenario.plan, 3),
            _manifest(scenario, 3),
            (chain[0], empty_artifact_gate_two),
            GateCertificateIssuanceContext(provenance=_provenance()),
        )
    assert _finding_codes(caught.value) == {"required-artifact-not-exact"}


def test_input_change_and_reuse_shapes_fail_closed() -> None:
    malformed_changes = (
        (
            {"changeClass": "runtime-toolchain-executor-image", "consumingGates": (2, 1)},
            "consuming gates must be unique and ordered",
        ),
        (
            {"changeClass": "code", "consumingGates": (1,)},
            "fixed invalidation scope",
        ),
        (
            {"changeClass": "code", "affectedGateFiveSubrecords": ("route-entity",)},
            "only coherence changes",
        ),
        (
            {
                "changeClass": "coherence-evidence",
                "affectedGateFiveSubrecords": ("two", "one"),
            },
            "subrecords must be unique and ordered",
        ),
    )
    for update, message in malformed_changes:
        with pytest.raises(ValueError, match=message):
            CertificateInputChange(reason="unproven change", **update)

    scenario = _scenario()
    chain = _certify_through(scenario, 2)
    with pytest.raises(ValueError, match="exact ordered prefix"):
        plan_certificate_reuse(scenario.admission, (chain[1],), ())


def test_content_store_all_exact_kinds_and_address_refusals(tmp_path: Path) -> None:
    scenario = _scenario()
    memory_inputs = _memory_inputs()
    chain = _certify_through(scenario, 5, memory_inputs=memory_inputs)
    current_inputs = FinalizationCurrentInputs(
        gateFiveInputs=memory_inputs,
        taskIntentAuthorityDigest="b" * 64,
        journalAuthorityDigest="d" * 64,
    )
    finalization = compile_finalization_authority(
        scenario.admission,
        chain,
        current_inputs,
        _provenance(),
    )
    red = _manifest(scenario, 2, status="fail")
    policy = CertificateStorePolicy(
        scopeId="all-kinds",
        maxObjects=32,
        maxBytes=10_000_000,
        reclamationOwner="closeout-operation-owner",
    )
    store = ContentAddressedCertificateStore(tmp_path / "all-kinds", policy)
    admission_path = store.publish_admission(scenario.admission)
    store.publish_result_manifest(red)
    store.publish_certificate(chain[0])
    store.publish_finalization(finalization)
    assert store.load_result_manifest(red.manifestDigest) == red
    assert store.load_certificate(chain[0].certificateDigest) == chain[0]
    assert store.load_finalization(finalization.authorityDigest) == finalization

    wrong_address = store.exact_path("admission", "f" * 64)
    wrong_address.parent.mkdir(parents=True)
    wrong_address.write_bytes(admission_path.read_bytes())
    with pytest.raises(CertificationContractError) as caught:
        store.load_admission("f" * 64)
    assert _finding_codes(caught.value) == {"certificate-object-address-mismatch"}

    unsafe_address = store.exact_path("admission", "e" * 64)
    unsafe_address.mkdir(parents=True)
    with pytest.raises(CertificationContractError) as caught:
        store.load_admission("e" * 64)
    assert _finding_codes(caught.value) == {"certificate-object-unsafe"}


def test_content_store_refuses_publication_and_capacity_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario()
    red = _manifest(scenario, 2, status="fail")
    policy = CertificateStorePolicy(
        scopeId="publication-refusals",
        maxObjects=32,
        maxBytes=10_000_000,
        reclamationOwner="closeout-operation-owner",
    )
    seed = ContentAddressedCertificateStore(tmp_path / "seed", policy)
    admission_size = seed.publish_admission(scenario.admission).stat().st_size

    collision = ContentAddressedCertificateStore(tmp_path / "collision", policy)
    collision_path = collision.exact_path("admission", scenario.admission.admissionDigest)
    collision_path.parent.mkdir(parents=True)
    collision_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(CertificationContractError) as caught:
        collision.publish_admission(scenario.admission)
    assert _finding_codes(caught.value) == {"content-address-collision"}

    readback = ContentAddressedCertificateStore(tmp_path / "readback", policy)

    def write_corrupt_bytes(path: Path, _: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}\n")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "atomic_write_bytes", write_corrupt_bytes)
        with pytest.raises(CertificationContractError) as caught:
            readback.publish_admission(scenario.admission)
    assert _finding_codes(caught.value) == {"certificate-object-readback-mismatch"}

    unsafe_capacity_root = tmp_path / "unsafe-capacity"
    unsafe_object = unsafe_capacity_root / "admission" / "sha256" / "aa" / "unsafe.json"
    unsafe_object.mkdir(parents=True)
    unsafe_capacity = ContentAddressedCertificateStore(unsafe_capacity_root, policy)
    with pytest.raises(CertificationContractError) as caught:
        unsafe_capacity.publish_admission(scenario.admission)
    assert _finding_codes(caught.value) == {"certificate-store-object-invalid"}

    byte_bounded = ContentAddressedCertificateStore(
        tmp_path / "byte-bounded",
        policy.model_copy(update={"scopeId": "byte-bounded", "maxBytes": admission_size + 1}),
    )
    byte_bounded.publish_admission(scenario.admission)
    with pytest.raises(CertificationContractError) as caught:
        byte_bounded.publish_result_manifest(red)
    assert _finding_codes(caught.value) == {"certificate-store-capacity-exceeded"}

    empty_bounded = ContentAddressedCertificateStore(
        tmp_path / "empty-bounded",
        policy.model_copy(update={"scopeId": "empty-bounded", "maxBytes": 1}),
    )
    with pytest.raises(CertificationContractError) as caught:
        empty_bounded.publish_admission(scenario.admission)
    assert _finding_codes(caught.value) == {"certificate-store-capacity-exceeded"}
