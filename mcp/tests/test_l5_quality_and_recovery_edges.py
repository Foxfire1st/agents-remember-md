"""CCR-R05 exact-candidate admission, recovery, finalization, and quality edges."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from unittest import mock

import organizational_completion_test_support as fixture_mod
import pytest
from agents_remember.certification import (
    authorize_finalization_leg,
    build_rail_result,
    canonicalize_registry,
    compile_certification_plan,
    compile_certification_recovery_record,
    compile_gate_certificate,
    compile_gate_result_manifest,
    compile_lifecycle_admission,
    compile_lifecycle_finalization,
    compile_repository_profile_plan,
    validate_lifecycle_admission_currentness,
    validate_lifecycle_finalization_currentness,
)
from agents_remember.certification.certificate_admission import admitted_gate_identity
from agents_remember.certification.certificate_invalidation import CertificateInputChange
from agents_remember.certification.certificate_models import (
    CoherenceSubrecordIdentity,
    CreationProvenance,
    FinalizationCurrentInputs,
    GateCertificate,
    GateCertificateIssuanceContext,
    GateFiveSemanticInputs,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.lifecycle_admission import (
    CompiledLifecycleAdmission,
    LifecycleAdmissionAuthorities,
    PriorRedAdmissionContext,
)
from agents_remember.certification.lifecycle_models import (
    DurableFinalizationLeg,
    ExactCandidateObservation,
    FinalizationBoundaryObservation,
    FinalizationJournalState,
)
from agents_remember.certification.lifecycle_recovery import (
    FinalizationBoundaryInputs,
    revalidate_certificate_authority,
)
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
    RepositoryCertificationProfile,
    RepositoryProfilePlan,
)
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import GateId, RailIdentity
from agents_remember.models.certification.corrective import (
    CorrectiveInputChange,
    RedCatalogDisposition,
)
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.worktrees.integration import integration_quality as quality
from agents_remember.worktrees.integration import integration_ref_transaction as ref_transaction
from agents_remember.worktrees.integration import (
    organizational_completion_integration as completion,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules.quality import clean_executor as clean_quality_executor
from agents_remember.worktrees.series_closeout import atomic_series_ledger_prefix
from integration_certification_test_support import selected_code_fixture
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    agents_remember_profile_execution,
    fixture_profile,
)

_DIGEST = "a" * 64


@dataclass(frozen=True)
class _Scenario:
    registry: CanonicalRailRegistry
    plan: CertificationPlan
    profile: CanonicalRepositoryCertificationProfile
    repository_plan: RepositoryProfilePlan
    observation: ExactCandidateObservation
    admission: CompiledLifecycleAdmission


def _provenance(marker: str = "one") -> CreationProvenance:
    return CreationProvenance(
        createdAt=f"2026-09-02T00:00:0{1 if marker == 'one' else 2}+00:00",
        producer=f"r05-{marker}",
        evidenceRef=f"evidence://r05/{marker}",
    )


def _scenario(
    marker: str = "c",
    *,
    dependent_gate_one: bool = False,
    profile_kind: ProfileKind = "certifying",
    adapter_timeout_seconds: int | None = None,
) -> _Scenario:
    candidate = CandidateIdentity(kind="git-tree", value=marker * 40)
    profile_value = _profile(
        dependent_gate_one,
        adapter_timeout_seconds=adapter_timeout_seconds,
    )
    profile = canonicalize_repository_profile(profile_value)
    repository_plan = compile_repository_profile_plan(
        profile,
        selection_id="closeout-targeted",
        candidate_identity=candidate,
    )
    repository_rails = tuple(rail for gate in repository_plan.gates for rail in gate.rails)
    registry = canonicalize_registry(
        RailRegistry(
            registryId="r05-boundary-fixture",
            repositoryId=profile.profile.repositoryId,
            profiles=(
                RegistryProfile(
                    profileId="closeout-targeted",
                    kind=profile_kind,
                    gates=(1, 2, 3, 4, 5),
                ),
            ),
            rails=(
                *(_registry_rail(rail) for rail in repository_rails),
                _memory_rail(repository_plan.gates[-1].rails[-1].identity),
            ),
        )
    )
    plan = compile_certification_plan(
        registry,
        profile_id="closeout-targeted",
        candidate_identity=candidate,
    )
    observation = _candidate_observation(candidate, repository_id=profile.profile.repositoryId)
    authorities = LifecycleAdmissionAuthorities(
        registry,
        plan,
        profile,
        repository_plan,
        observation,
    )
    admission = compile_lifecycle_admission(authorities, provenance=_provenance())
    return _Scenario(registry, plan, profile, repository_plan, observation, admission)


def _profile(
    dependent_gate_one: bool,
    *,
    adapter_timeout_seconds: int | None = None,
) -> RepositoryCertificationProfile:
    profile = fixture_profile()
    if adapter_timeout_seconds is not None:
        root = profile.rails[0]
        changed = root.model_copy(
            update={
                "execution": root.execution.model_copy(
                    update={"timeoutSeconds": adapter_timeout_seconds}
                )
            }
        )
        provisional = profile.model_copy(
            update={"rails": (changed, *profile.rails[1:]), "profileDigest": "0" * 64}
        )
        profile = provisional.model_copy(
            update={"profileDigest": repository_profile_digest(provisional)}
        )
    if not dependent_gate_one:
        return profile
    root = profile.rails[0]
    dependant = root.model_copy(
        update={
            "identity": RailIdentity(railId="generated-currentness", version="1.0.0"),
            "orderKey": "generated-currentness",
            "prerequisites": (root.identity,),
            "execution": root.execution.model_copy(
                update={
                    "adapterId": "generated-currentness-adapter",
                    "command": ("python", "scripts/check-generated.py"),
                    "executionEvidence": "adapter://generated-currentness",
                }
            ),
            "evidenceContract": (
                RailEvidenceContract(
                    evidenceId="generated-currentness-evidence",
                    mediaType="application/json",
                    maxBytes=4096,
                ),
            ),
        }
    )
    selections = tuple(
        selection.model_copy(
            update={
                "gates": tuple(
                    gate.model_copy(update={"railIds": (*gate.railIds, dependant.identity)})
                    if gate.gate == 1
                    else gate
                    for gate in selection.gates
                )
            }
        )
        for selection in profile.selections
    )
    provisional = profile.model_copy(
        update={
            "rails": (root, dependant, *profile.rails[1:]),
            "selections": selections,
            "profileDigest": "0" * 64,
        }
    )
    return provisional.model_copy(update={"profileDigest": repository_profile_digest(provisional)})


def _registry_rail(rail: CompiledRail) -> RailDefinition:
    return RailDefinition(
        identity=rail.identity,
        gate=rail.gate,
        railClass=rail.railClass,
        authority="repository-profile",
        ownerClass=rail.ownerClass,
        correctiveOwner=rail.correctiveOwner,
        posture=rail.posture,
        orderKey=rail.orderKey,
        prerequisites=rail.prerequisites,
        requiredArtifacts=rail.requiredArtifacts,
        adapter=rail.adapter,
        runtimeInputs=rail.runtimeInputs,
        applicability=(
            RailApplicability(
                profileId="closeout-targeted",
                status="applicable",
                selectionIdentity=f"closeout-targeted:gate-{rail.gate}",
                population=f"declared Gate {rail.gate} population",
            ),
        ),
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
                profileId="closeout-targeted",
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


def _candidate_observation(
    candidate: CandidateIdentity,
    *,
    repository_id: str,
    generated: Literal["current", "stale", "unknown"] = "current",
) -> ExactCandidateObservation:
    return ExactCandidateObservation(
        taskId="260831-CCR-L05",
        contractPath="/coordination/enclosures/l05/series-contract.md",
        lifecycleAuthorityDigest="1" * 64,
        repositoryId=repository_id,
        sourceBranchRef="refs/heads/ar/260831-closeout-certification-r05",
        sourceBranchTip="2" * 40,
        candidateCodeTree=candidate,
        topologyIdentityDigest="3" * 64,
        taskIntentIdentityDigest="4" * 64,
        normalizedCommitIntentDigest="5" * 64,
        mutationAuthorityDigest="6" * 64,
        sourceAuthorityDigest="7" * 64,
        worktreeRuleDigest="8" * 64,
        generatedInputsDigest="9" * 64,
        mutationAuthorityStatus="valid",
        sourceAuthorityStatus="valid",
        branchAuthorityStatus="valid",
        worktreeStatus="admissible",
        generatedArtifactStatus=generated,
    )


def _gate(plan: CertificationPlan, gate: GateId) -> GatePlan:
    return next(item for item in plan.gates if item.gate == gate)


def _manifest(
    scenario: _Scenario,
    gate: GateId,
    statuses: dict[str, RailStatus] | None = None,
) -> GateResultManifest:
    gate_plan = _gate(scenario.plan, gate)
    compiled = {item.identity.railId: item for item in gate_plan.rails}
    statuses = statuses or {rail_id: "pass" for rail_id in compiled}
    results = []
    for rail_id, rail in compiled.items():
        status = statuses[rail_id]
        blocked_by = rail.prerequisites if status == "blocked" else ()
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
            if status == "pass"
            else ()
        )
        observation = RailTerminalObservation(
            rail=rail.identity,
            status=status,
            code=f"{rail_id}-{status}",
            blockedBy=blocked_by,
            artifacts=artifacts,
            evidence=(
                RailEvidenceReference(
                    evidenceId=rail.evidenceContract[0].evidenceId,
                    sha256=content_digest({"evidence": rail_id, "status": status}),
                    size=32,
                    reference=f"evidence://{rail_id}/{status}",
                ),
            ),
        )
        results.append(build_rail_result(gate_plan, observation))
    return compile_gate_result_manifest(
        scenario.registry,
        scenario.plan,
        gate_plan,
        results,
        GateResultAdmission(
            profileId="closeout-targeted",
            candidateIdentity=scenario.plan.candidateIdentity,
            altitude=scenario.plan.profileKind,
        ),
    )


def _memory_inputs() -> GateFiveSemanticInputs:
    return GateFiveSemanticInputs(
        memoryTree=CandidateIdentity(kind="git-tree", value="e" * 40),
        affectedClosurePlanDigest="b" * 64,
        memoryCheckerRegistryDigest="c" * 64,
        coherenceSubrecords=(
            CoherenceSubrecordIdentity(subrecordId="route-entity", contentDigest="d" * 64),
        ),
        candidatePairAuthorityDigest="f" * 64,
    )


def _certificate_chain(scenario: _Scenario) -> tuple[GateCertificate, ...]:
    certificates: list[GateCertificate] = []
    memory_inputs = _memory_inputs()
    for gate in (1, 2, 3, 4, 5):
        certificate = compile_gate_certificate(
            scenario.admission.certification,
            _gate(scenario.plan, gate),
            _manifest(scenario, gate),
            certificates,
            GateCertificateIssuanceContext(
                provenance=_provenance(),
                gateFiveInputs=memory_inputs if gate == 5 else None,
            ),
        )
        certificates.append(certificate)
    return tuple(certificates)


def _finalization_inputs(
    scenario: _Scenario,
    chain: tuple[GateCertificate, ...],
    *,
    approval_status: Literal["current", "lost", "stale"] = "current",
) -> FinalizationBoundaryInputs:
    memory_inputs = _memory_inputs()
    journal = FinalizationJournalState(
        journalAuthorityDigest="a" * 64,
        legs=(
            DurableFinalizationLeg(
                leg="code-commit",
                state="proven",
                authorityDigest="1" * 64,
                intendedOutputDigest="2" * 64,
                provenOutputDigest="2" * 64,
            ),
            DurableFinalizationLeg(leg="external-memory-commit", state="not-applicable"),
            DurableFinalizationLeg(leg="ledger-commit", state="not-applicable"),
            DurableFinalizationLeg(
                leg="contract-finalization",
                state="pending",
                authorityDigest="3" * 64,
            ),
        ),
    )
    candidate = scenario.observation
    observation = FinalizationBoundaryObservation(
        candidateCodeTree=candidate.candidateCodeTree,
        candidateMemoryTree=memory_inputs.memoryTree,
        topologyIdentityDigest=candidate.topologyIdentityDigest,
        taskIntentIdentityDigest=candidate.taskIntentIdentityDigest,
        coherentOperationStateDigest="b" * 64,
        operationStatus="finalizing",
        doorAuthorityDigest="c" * 64,
        doorStatus="claimed",
        approvalAuthorityDigest="d" * 64,
        approvalStatus=approval_status,
        normalizedCommitIntentDigest=candidate.normalizedCommitIntentDigest,
    )
    return FinalizationBoundaryInputs(
        scenario.admission,
        chain,
        FinalizationCurrentInputs(
            gateFiveInputs=memory_inputs,
            taskIntentAuthorityDigest=candidate.taskIntentIdentityDigest,
            journalAuthorityDigest=journal.journalAuthorityDigest,
        ),
        observation,
        journal,
    )


def _finding_codes(error: CertificationContractError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


def test_corrective_input_change_accepts_exact_git_or_content_digests_only() -> None:
    change = CorrectiveInputChange(
        inputKind="candidate-code-tree",
        inputId="candidate",
        beforeDigest="a" * 40,
        afterDigest="b" * 64,
    )
    assert change.key == ("candidate-code-tree", "candidate")
    with pytest.raises(ValueError, match="corrective input change must move"):
        CorrectiveInputChange(
            inputKind="gate-configuration",
            inputId="gate-2",
            beforeDigest="a" * 64,
            afterDigest="a" * 64,
        )
    for invalid in ("a" * 39, "a" * 41, "a" * 63, "a" * 65):
        with pytest.raises(ValueError, match="String should match pattern"):
            CorrectiveInputChange(
                inputKind="candidate-code-tree",
                inputId="candidate",
                beforeDigest=invalid,
                afterDigest="b" * 64,
            )


def test_admission_freezes_all_authorities_without_running_or_mutating() -> None:
    scenario = _scenario()
    stale = scenario.observation.model_copy(update={"generatedArtifactStatus": "stale"})
    authorities = LifecycleAdmissionAuthorities(
        scenario.registry,
        scenario.plan,
        scenario.profile,
        scenario.repository_plan,
        stale,
    )
    admission = compile_lifecycle_admission(
        authorities,
        provenance=_provenance(),
    )
    envelope = admission.lifecycle.semanticEnvelope
    assert envelope.gateStarts == 0
    assert envelope.candidate.generatedArtifactStatus == "stale"
    assert envelope.certificationAdmissionDigest == admission.certification.admissionDigest
    assert envelope.repositoryProfileDigest == scenario.profile.profileDigest
    assert envelope.repositoryPlanDigest == scenario.repository_plan.planDigest
    assert validate_lifecycle_admission_currentness(admission, authorities) == admission
    with pytest.raises(ValueError, match="admission digest"):
        type(admission.lifecycle).model_validate(
            {**admission.lifecycle.model_dump(mode="json"), "admissionDigest": "0" * 64}
        )
    with pytest.raises(CertificationContractError) as caught:
        compile_lifecycle_admission(
            replace(authorities, candidate=stale.model_copy(update={"repositoryId": "other"})),
            provenance=_provenance(),
        )
    assert _finding_codes(caught.value) == {"candidate-plan-identity-mismatch"}


@pytest.mark.parametrize(
    ("update", "code"),
    (
        ({"mutationAuthorityStatus": "invalid"}, "candidate-authority-invalid"),
        ({"sourceAuthorityStatus": "invalid"}, "candidate-authority-invalid"),
        ({"branchAuthorityStatus": "invalid"}, "candidate-authority-invalid"),
        ({"worktreeStatus": "invalid"}, "candidate-worktree-invalid"),
    ),
)
def test_admission_refuses_invalid_owner_authority_with_zero_starts(
    update: dict[str, str], code: str
) -> None:
    scenario = _scenario()
    candidate = scenario.observation.model_copy(update=update)
    with pytest.raises(CertificationContractError) as caught:
        compile_lifecycle_admission(
            LifecycleAdmissionAuthorities(
                scenario.registry,
                scenario.plan,
                scenario.profile,
                scenario.repository_plan,
                candidate,
            ),
            provenance=_provenance(),
        )
    assert _finding_codes(caught.value) == {code}
    assert caught.value.findings[0]["gateStarts"] == 0


def test_currentness_refuses_candidate_or_authority_movement() -> None:
    scenario = _scenario()
    moved = scenario.observation.model_copy(update={"topologyIdentityDigest": "f" * 64})
    with pytest.raises(CertificationContractError) as caught:
        validate_lifecycle_admission_currentness(
            scenario.admission,
            LifecycleAdmissionAuthorities(
                scenario.registry,
                scenario.plan,
                scenario.profile,
                scenario.repository_plan,
                moved,
            ),
        )
    assert _finding_codes(caught.value) == {"lifecycle-admission-mismatch"}


def test_prior_red_requires_every_root_and_allows_blocked_dependant_to_cite_repair() -> None:
    prior = _scenario(dependent_gate_one=True)
    successor = _scenario(
        "d",
        dependent_gate_one=True,
        adapter_timeout_seconds=601,
    )
    catalog = _manifest(
        prior,
        1,
        {"static-quality": "fail", "generated-currentness": "blocked"},
    )
    results = {item.rail.railId: item for item in catalog.railResults}
    root = results["static-quality"]
    dependant = results["generated-currentness"]
    changed = CorrectiveInputChange(
        inputKind="candidate-code-tree",
        inputId="candidate",
        beforeDigest=prior.plan.candidateIdentity.value,
        afterDigest=successor.plan.candidateIdentity.value,
    )
    prior_input = next(
        item
        for item in admitted_gate_identity(prior.admission.certification, 1).semanticInputs
        if item.key == ("rail-adapter", "static-quality@1.0.0")
    )
    successor_inputs = {
        item.key: item
        for item in admitted_gate_identity(successor.admission.certification, 1).semanticInputs
    }
    changed_adapter = CorrectiveInputChange(
        inputKind=prior_input.inputKind,
        inputId=prior_input.inputId,
        beforeDigest=prior_input.contentDigest,
        afterDigest=successor_inputs[prior_input.key].contentDigest,
    )
    dispositions = (
        RedCatalogDisposition(
            rail=dependant.rail,
            priorStatus="blocked",
            priorResultDigest=dependant.resultDigest,
            correctiveOwner=dependant.correctiveOwner,
            disposition="repaired-root",
            repairedRoot=root.rail,
            rationale="dependant was blocked only by the directly repaired root",
        ),
        RedCatalogDisposition(
            rail=root.rail,
            priorStatus="fail",
            priorResultDigest=root.resultDigest,
            correctiveOwner=root.correctiveOwner,
            disposition="direct-repair",
            changedInputs=(changed, changed_adapter),
            rationale="candidate code changed to repair the failing root",
        ),
    )
    compiled = compile_lifecycle_admission(
        LifecycleAdmissionAuthorities(
            successor.registry,
            successor.plan,
            successor.profile,
            successor.repository_plan,
            successor.observation,
        ),
        provenance=_provenance("two"),
        prior_red=PriorRedAdmissionContext(
            prior.admission.certification,
            catalog,
            dispositions,
        ),
    )
    assert compiled.priorRedDisposition is not None
    assert (
        compiled.lifecycle.semanticEnvelope.priorRedDispositionDigest
        == compiled.priorRedDisposition.dispositionDigest
    )
    prior_manifest = compiled.priorRedDisposition
    with pytest.raises(ValueError, match="disposition digest"):
        type(prior_manifest).model_validate(
            {**prior_manifest.model_dump(mode="json"), "dispositionDigest": "0" * 64}
        )
    with pytest.raises(ValueError, match="unique and canonical"):
        type(prior_manifest.semanticEnvelope).model_validate(
            {
                **prior_manifest.semanticEnvelope.model_dump(mode="json"),
                "dispositions": [dispositions[0], dispositions[0]],
            }
        )

    with pytest.raises(CertificationContractError) as caught:
        compile_lifecycle_admission(
            LifecycleAdmissionAuthorities(
                successor.registry,
                successor.plan,
                successor.profile,
                successor.repository_plan,
                successor.observation,
            ),
            provenance=_provenance("two"),
            prior_red=PriorRedAdmissionContext(
                prior.admission.certification,
                catalog,
                (dispositions[1],),
            ),
        )
    assert _finding_codes(caught.value) == {"prior-red-disposition-catalog-incomplete"}

    invalid_dispositions = (
        ((), "prior-red-authority-incomplete"),
        (
            (dispositions[0], dispositions[1].model_copy(update={"priorResultDigest": _DIGEST})),
            "prior-red-disposition-identity-mismatch",
        ),
        (
            (dispositions[0].model_copy(update={"repairedRoot": dependant.rail}), dispositions[1]),
            "prior-red-repaired-root-invalid",
        ),
    )
    for invalid, code in invalid_dispositions:
        with pytest.raises(CertificationContractError) as caught:
            compile_lifecycle_admission(
                replace(
                    LifecycleAdmissionAuthorities(
                        successor.registry,
                        successor.plan,
                        successor.profile,
                        successor.repository_plan,
                        successor.observation,
                    )
                ),
                provenance=_provenance("two"),
                prior_red=PriorRedAdmissionContext(prior.admission.certification, catalog, invalid),
            )
        assert _finding_codes(caught.value) == {code}


def test_prior_red_refuses_unchanged_or_diagnostic_catalog_authority() -> None:
    prior = _scenario()
    catalog = _manifest(prior, 1, {"static-quality": "fail"})
    result = catalog.railResults[0]
    false_change = CorrectiveInputChange(
        inputKind="candidate-code-tree",
        inputId="candidate",
        beforeDigest=prior.plan.candidateIdentity.value,
        afterDigest="d" * 64,
    )
    disposition = RedCatalogDisposition(
        rail=result.rail,
        priorStatus="fail",
        priorResultDigest=result.resultDigest,
        correctiveOwner=result.correctiveOwner,
        disposition="direct-repair",
        changedInputs=(false_change,),
        rationale="diagnostic text alone cannot establish this claimed change",
    )
    with pytest.raises(CertificationContractError) as caught:
        compile_lifecycle_admission(
            LifecycleAdmissionAuthorities(
                prior.registry,
                prior.plan,
                prior.profile,
                prior.repository_plan,
                prior.observation,
            ),
            provenance=_provenance("two"),
            prior_red=PriorRedAdmissionContext(
                prior.admission.certification,
                catalog,
                (disposition,),
            ),
        )
    assert _finding_codes(caught.value) == {"prior-red-changed-input-unproven"}

    diagnostic = _scenario(profile_kind="diagnostic")
    diagnostic_catalog = _manifest(diagnostic, 1, {"static-quality": "fail"})
    diagnostic_result = diagnostic_catalog.railResults[0]
    with pytest.raises(CertificationContractError) as caught:
        compile_lifecycle_admission(
            LifecycleAdmissionAuthorities(
                prior.registry,
                prior.plan,
                prior.profile,
                prior.repository_plan,
                prior.observation,
            ),
            provenance=_provenance("two"),
            prior_red=PriorRedAdmissionContext(
                diagnostic.admission.certification,
                diagnostic_catalog,
                (
                    disposition.model_copy(
                        update={
                            "rail": diagnostic_result.rail,
                            "priorResultDigest": diagnostic_result.resultDigest,
                        }
                    ),
                ),
            ),
        )
    assert _finding_codes(caught.value) == {"prior-red-catalog-authority-mismatch"}


def test_recovery_journals_r21_reuse_for_code_memory_and_unchanged_interruption() -> None:
    scenario = _scenario()
    chain = _certificate_chain(scenario)
    unchanged = compile_certification_recovery_record(
        scenario.admission,
        chain,
        (),
        provenance=_provenance(),
        gate_five_inputs=_memory_inputs(),
    )
    assert unchanged.semanticEnvelope.reusePlan.zeroGateStarts
    assert unchanged.semanticEnvelope.reusePlan.firstGateToRun is None
    with pytest.raises(ValueError, match="recovery digest"):
        type(unchanged).model_validate(
            {**unchanged.model_dump(mode="json"), "recoveryDigest": "0" * 64}
        )

    code = compile_certification_recovery_record(
        scenario.admission,
        chain,
        (CertificateInputChange(changeClass="code", reason="code repair"),),
        provenance=_provenance("two"),
        gate_five_inputs=_memory_inputs(),
    )
    assert code.semanticEnvelope.reusePlan.firstGateToRun == 1
    assert not code.semanticEnvelope.reusePlan.reusedCertificates

    memory = compile_certification_recovery_record(
        scenario.admission,
        chain,
        (CertificateInputChange(changeClass="memory-onboarding", reason="memory repair"),),
        provenance=_provenance("two"),
        gate_five_inputs=_memory_inputs(),
    )
    assert memory.semanticEnvelope.reusePlan.firstGateToRun == 5
    assert len(memory.semanticEnvelope.reusePlan.reusedCertificates) == 4


def test_partial_finalization_resumes_exact_leg_with_zero_gate_starts() -> None:
    scenario = _scenario()
    chain = _certificate_chain(scenario)
    inputs = _finalization_inputs(scenario, chain)
    manifest = compile_lifecycle_finalization(inputs, provenance=_provenance())
    assert manifest.semanticEnvelope.nextLeg == "contract-finalization"
    assert manifest.semanticEnvelope.zeroGateStarts
    revalidate_certificate_authority(
        manifest,
        inputs.admission,
        inputs.certificates,
        inputs.currentInputs,
    )
    with pytest.raises(ValueError, match="resume edge"):
        type(manifest.semanticEnvelope).model_validate(
            {**manifest.semanticEnvelope.model_dump(mode="json"), "nextLeg": "code-commit"}
        )
    with pytest.raises(ValueError, match="finalization digest"):
        type(manifest).model_validate(
            {**manifest.model_dump(mode="json"), "finalizationDigest": "0" * 64}
        )
    foreign_envelope = manifest.semanticEnvelope.model_copy(
        update={"certificateAuthorityDigest": "0" * 64}
    )
    foreign_authority = type(manifest).model_validate(
        {
            **manifest.model_dump(mode="json"),
            "semanticEnvelope": foreign_envelope,
            "finalizationDigest": content_digest(foreign_envelope),
        }
    )
    with pytest.raises(CertificationContractError) as caught:
        revalidate_certificate_authority(
            foreign_authority,
            inputs.admission,
            inputs.certificates,
            inputs.currentInputs,
        )
    assert _finding_codes(caught.value) == {"certificate-finalization-authority-mismatch"}
    last_leg = inputs.journal.legs[-1].model_copy(
        update={"state": "intent", "intendedOutputDigest": "4" * 64}
    )
    moved_journal = FinalizationJournalState(
        journalAuthorityDigest=inputs.journal.journalAuthorityDigest,
        legs=(*inputs.journal.legs[:-1], last_leg),
    )
    with pytest.raises(CertificationContractError) as caught:
        validate_lifecycle_finalization_currentness(
            manifest,
            replace(inputs, journal=moved_journal),
        )
    assert _finding_codes(caught.value) == {"lifecycle-finalization-mismatch"}
    with pytest.raises(CertificationContractError) as caught:
        compile_lifecycle_finalization(
            replace(
                inputs,
                currentInputs=inputs.currentInputs.model_copy(
                    update={"taskIntentAuthorityDigest": "0" * 64}
                ),
            ),
            provenance=_provenance(),
        )
    assert _finding_codes(caught.value) == {"finalization-authority-mismatch"}
    assert (
        authorize_finalization_leg(
            manifest,
            "contract-finalization",
            inputs,
        )
        == manifest
    )
    with pytest.raises(CertificationContractError) as caught:
        authorize_finalization_leg(manifest, "code-commit", inputs)
    assert _finding_codes(caught.value) == {"finalization-leg-not-current"}


@pytest.mark.parametrize(
    "observation_update",
    (
        {"topologyIdentityDigest": "f" * 64},
        {"approvalStatus": "lost"},
        {"doorStatus": "stale"},
        {"operationStatus": "cancel-requested"},
    ),
)
def test_finalization_refuses_movement_lost_authority_and_cancellation(
    observation_update: dict[str, str],
) -> None:
    scenario = _scenario()
    chain = _certificate_chain(scenario)
    inputs = _finalization_inputs(scenario, chain)
    manifest = compile_lifecycle_finalization(inputs, provenance=_provenance())
    moved_inputs = FinalizationBoundaryInputs(
        inputs.admission,
        inputs.certificates,
        inputs.currentInputs,
        inputs.observation.model_copy(update=observation_update),
        inputs.journal,
    )
    with pytest.raises(CertificationContractError) as caught:
        validate_lifecycle_finalization_currentness(manifest, moved_inputs)
    assert _finding_codes(caught.value) <= {
        "finalization-authority-mismatch",
        "finalization-boundary-not-current",
        "lifecycle-finalization-mismatch",
    }


def test_finalization_journal_rejects_nonmonotonic_or_ambiguous_progress() -> None:
    with pytest.raises(ValueError, match="progress is not monotonic"):
        FinalizationJournalState(
            journalAuthorityDigest="a" * 64,
            legs=(
                DurableFinalizationLeg(
                    leg="code-commit", state="pending", authorityDigest="1" * 64
                ),
                DurableFinalizationLeg(
                    leg="external-memory-commit",
                    state="proven",
                    authorityDigest="2" * 64,
                    intendedOutputDigest="3" * 64,
                    provenOutputDigest="3" * 64,
                ),
                DurableFinalizationLeg(leg="ledger-commit", state="not-applicable"),
                DurableFinalizationLeg(
                    leg="contract-finalization",
                    state="pending",
                    authorityDigest="4" * 64,
                ),
            ),
        )


class L5QualityAndRecoveryEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = fixture_mod.OrganizationalCompletionFixture()
        self.owner.setUp()

    def tearDown(self) -> None:
        try:
            self.owner.doCleanups()
        finally:
            self.owner.tearDown()

    def test_published_quality_attestation_and_result_failure_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export = root / "export"
            reports = root / "reports"
            export.mkdir()
            result_path = export / "clean-quality-results.json"
            result_path.write_text("not json\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no valid authoritative result"):
                clean_quality_executor._publish_reports(
                    export,
                    reports,
                    candidate_tree="c" * 40,
                    profile_execution=agents_remember_profile_execution(candidate_tree="c" * 40),
                    bindings=clean_quality_executor.ReportBindings(
                        attestation={"id": "one"}, runtime_authority_digest=None
                    ),
                )
            result_path.write_text(
                json.dumps({"status": "failed", "exitCode": 1}) + "\n",
                encoding="utf-8",
            )
            clean_quality_executor._publish_reports(
                export,
                reports,
                candidate_tree="c" * 40,
                profile_execution=agents_remember_profile_execution(candidate_tree="c" * 40),
                bindings=clean_quality_executor.ReportBindings(
                    attestation={"id": "one"}, runtime_authority_digest=None
                ),
            )
            manifest = clean_quality_executor.load_published_quality_manifest(reports)
            with self.assertRaisesRegex(RuntimeError, "did not pass acceptance"):
                clean_quality_executor.certifying_evidence_from_published_manifest(
                    reports,
                    manifest,
                    candidate_tree="c" * 40,
                )

            manifest_path = reports / clean_quality_executor.REPORT_SET_MANIFEST
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("attestation")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no complete Dagger report"):
                clean_quality_executor.published_quality_attestation(reports)
            manifest["attestation"] = "invalid"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no complete Dagger report"):
                clean_quality_executor.published_quality_attestation(reports)

    def test_manifest_shape_is_object_root_and_shared_by_both_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            reports.mkdir()
            manifest_path = reports / clean_quality_executor.REPORT_SET_MANIFEST
            hostile_roots = (
                [],
                None,
                True,
                "generation",
                {"schemaVersion": "2.0", "generation": "a" * 64, "files": {}},
                {"schemaVersion": "1.0", "generation": "a" * 64, "files": []},
                {
                    "schemaVersion": "1.0",
                    "generation": "a" * 64,
                    "files": {"result.json": {"sha256": "a" * 64, "size": "1"}},
                },
            )
            for hostile in hostile_roots:
                with self.subTest(hostile=hostile):
                    manifest_path.write_text(json.dumps(hostile), encoding="utf-8")
                    errors = []
                    for consume in (
                        lambda: clean_quality_executor.published_report_path(
                            reports, "result.json"
                        ),
                        lambda: clean_quality_executor.published_quality_attestation(reports),
                    ):
                        with self.assertRaises(RuntimeError) as raised:
                            consume()
                        errors.append((type(raised.exception), str(raised.exception)))
                    self.assertEqual(errors[0], errors[1])
                    self.assertEqual(
                        errors[0][1],
                        "no complete Dagger report generation is published",
                    )

    def test_recovery_preserves_wrapper_path_and_exposes_published_result_separately(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = selected_code_fixture(root)
            fresh = selected.render()
            recovered = selected.render()
            self.assertEqual(recovered["reportPath"], fresh["reportPath"])
            self.assertEqual(Path(str(fresh["reportPath"])).name, "test-results.md")
            self.assertEqual(recovered["publishedResultPath"], fresh["publishedResultPath"])
            published = Path(str(recovered["publishedResultPath"]))
            self.assertEqual(published.name, "clean-quality-results.json")
            self.assertEqual(json.loads(published.read_bytes())["exitCode"], 0)

            export = root / "malformed-export"
            export.mkdir()
            (export / "clean-quality-results.json").write_text("[]\n", encoding="utf-8")
            candidate_tree = selected.prepared.candidateTree
            with self.assertRaisesRegex(RuntimeError, "must be a JSON object"):
                clean_quality_executor._publish_reports(
                    export,
                    selected.target.worktree_group / "reports",
                    candidate_tree=candidate_tree,
                    profile_execution=agents_remember_profile_execution(
                        candidate_tree=candidate_tree
                    ),
                    bindings=clean_quality_executor.ReportBindings(
                        attestation={"id": "two"}, runtime_authority_digest=None
                    ),
                )

    def test_organizational_gate_refuses_certification_without_a_journal_owner(self) -> None:
        contract = self.owner._certified_contract(final=True)
        plan = completion.preview_organizational_completion(contract)
        assert plan is not None
        with (
            mock.patch.object(quality, "run_strict_code_quality_gate") as gate,
            self.assertRaises(RuntimeError) as raised,
        ):
            quality.run_integration_quality_gate(
                contract,
                completion=plan,
                profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
                owner=None,
            )
        gate.assert_not_called()
        cause = raised.exception.__cause__
        assert isinstance(cause, CertificationContractError)
        self.assertEqual(cause.findings[0]["code"], "integration-certification-owner-missing")

    def test_public_ledger_and_series_prefix_preconditions_refuse_wrong_contracts(self) -> None:
        contract = self.owner._certified_contract(final=True)
        with self.assertRaisesRegex(RuntimeError, "leaf or series"):
            ref_transaction.require_integrated_ledger_mapping(
                replace(contract, kind="invalid"),  # type: ignore[arg-type]
                ref_transaction.IntegratedCommits("a" * 40, "b" * 40, "c" * 40),
                memory_source_commit="d" * 40,
            )
        with self.assertRaisesRegex(RuntimeError, "external-memory series"):
            atomic_series_ledger_prefix(replace(contract, kind="leaf"))

    def test_closeout_wal_cannot_claim_an_integration_quality_failure(self) -> None:
        contract = self.owner._certified_contract(final=True)
        closeout = LifecycleOperationStore(
            operation_record_path(contract.worktree_group, "closeout")
        ).read()
        assert closeout is not None
        with self.assertRaisesRegex(ValueError, "belongs to integration only"):
            LifecycleOperationRecord.model_validate(
                {
                    **closeout.model_dump(mode="json"),
                    "result": {"state": "organizational-completion-gate-failed"},
                }
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
