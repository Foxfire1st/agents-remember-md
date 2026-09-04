"""Focused public contracts for immutable quality results and closeout readiness."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.certification import (
    READINESS_SURFACES,
    build_rail_result,
    canonicalize_registry,
    compile_certification_plan,
    compile_closeout_readiness,
    compile_finalization_authority,
    compile_gate_certificate,
    compile_gate_result_manifest,
    readiness_projection_bytes,
    require_readiness_transition,
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
    ReadinessRevision,
)
from agents_remember.certification.repository_profiles.models import (
    RepositoryGatePlan,
    RepositoryProfilePlan,
    repository_gate_plan_digest,
)
from agents_remember.errors import CloseoutReadinessContractError
from agents_remember.models.tools.tool_response import finalize_tool_response
from agents_remember.models.worktree import WorktreeIntegrateResponse
from agents_remember.worktrees.modules.quality import clean_executor as clean_quality_executor
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from pydantic import ValidationError
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    REPOSITORY_ROOT,
    agents_remember_profile_execution,
)


class QualityGatePublicContractTests(unittest.TestCase):
    def test_recovery_refuses_same_id_decoder_byte_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_tree = "c" * 40
            export = root / "export"
            reports = root / "reports"
            export.mkdir()
            (export / "clean-quality-results.json").write_text(
                json.dumps({"status": "passed", "exitCode": 0}) + "\n",
                encoding="utf-8",
            )
            clean_quality_executor._publish_reports(
                export,
                reports,
                candidate_tree=candidate_tree,
                profile_execution=agents_remember_profile_execution(candidate_tree=candidate_tree),
                bindings=clean_quality_executor.ReportBindings(
                    attestation={"id": "decoder-drift"}, runtime_authority_digest=None
                ),
            )
            pointer = reports / clean_quality_executor.REPORT_SET_MANIFEST
            manifest = json.loads(pointer.read_text(encoding="utf-8"))
            manifest["resultDecoder"]["passedValue"] = "accepted"
            pointer.write_text(json.dumps(manifest), encoding="utf-8")
            target = code_quality_gate.QualityGateTarget(
                REPOSITORY_ROOT,
                root,
                "agents-remember",
                AGENTS_REMEMBER_PROFILE_REFERENCE,
            )

            with mock.patch.object(
                code_quality_gate,
                "require_git",
                return_value=candidate_tree,
            ):
                recovered = code_quality_gate.recover_strict_code_quality_gate(
                    target,
                    diff_base="a" * 40,
                    plan=code_quality_gate.QualityGatePlan(mode="full"),
                    attestation={"id": "decoder-drift"},
                )

            self.assertIsNone(recovered)

    def test_recovery_uses_one_manifest_generation_when_the_pointer_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_tree = "c" * 40
            export = root / "export"
            reports = root / "reports"
            export.mkdir()
            result_path = export / "clean-quality-results.json"
            result_path.write_text(
                json.dumps({"status": "passed", "exitCode": 0, "generation": "a"}) + "\n",
                encoding="utf-8",
            )
            generation_a = clean_quality_executor._publish_reports(
                export,
                reports,
                candidate_tree=candidate_tree,
                profile_execution=agents_remember_profile_execution(candidate_tree=candidate_tree),
                bindings=clean_quality_executor.ReportBindings(
                    attestation={"id": "a"}, runtime_authority_digest=None
                ),
            )["generation"]
            real_loader = code_quality_gate.load_published_quality_manifest

            def rotate_after_snapshot(destination: Path):
                snapshot = real_loader(destination)
                result_path.write_text(
                    json.dumps({"status": "failed", "exitCode": 1, "generation": "b"}) + "\n",
                    encoding="utf-8",
                )
                clean_quality_executor._publish_reports(
                    export,
                    reports,
                    candidate_tree=candidate_tree,
                    profile_execution=agents_remember_profile_execution(
                        candidate_tree=candidate_tree
                    ),
                    bindings=clean_quality_executor.ReportBindings(
                        attestation={"id": "b"}, runtime_authority_digest=None
                    ),
                )
                return snapshot

            target = code_quality_gate.QualityGateTarget(
                REPOSITORY_ROOT,
                root,
                "agents-remember",
                AGENTS_REMEMBER_PROFILE_REFERENCE,
            )
            plan = code_quality_gate.QualityGatePlan(mode="full")
            with (
                mock.patch.object(
                    code_quality_gate,
                    "load_published_quality_manifest",
                    side_effect=rotate_after_snapshot,
                ) as loader,
                mock.patch.object(
                    code_quality_gate,
                    "require_git",
                    return_value=candidate_tree,
                ),
            ):
                recovered = code_quality_gate.recover_strict_code_quality_gate(
                    target,
                    diff_base="a" * 40,
                    plan=plan,
                    attestation={"id": "a"},
                )

            assert recovered is not None
            self.assertEqual(loader.call_count, 1)
            published = Path(str(recovered["publishedResultPath"]))
            self.assertEqual(published.parent.name, generation_a)
            self.assertEqual(
                json.loads(published.read_text(encoding="utf-8"))["generation"],
                "a",
            )
            current = real_loader(reports)
            self.assertNotEqual(current.generation, generation_a)
            self.assertEqual(dict(current.attestation or {}), {"id": "b"})

    def test_public_worktree_response_models_and_retains_both_quality_paths(self) -> None:
        quality_result = {
            "required": True,
            "status": "enforced",
            "passed": True,
            "command": "dagger call quality",
            "diffBase": "a" * 40,
            "mode": "full",
            "executor": "dagger",
            "reportPath": "/enclosure/reports/test-results.md",
            "publishedResultPath": (
                "/enclosure/reports/.quality-report-generations/"
                f"{'b' * 64}/clean-quality-results.json"
            ),
        }

        payload = finalize_tool_response(
            "worktree_integrate",
            {
                "ok": True,
                "operation": "worktree_integrate",
                "quality_gate": quality_result,
            },
        )

        self.assertEqual(payload["quality_gate"]["reportPath"], quality_result["reportPath"])
        self.assertEqual(
            payload["quality_gate"]["publishedResultPath"],
            quality_result["publishedResultPath"],
        )
        self.assertGreater(payload["tokens"], 0)
        schema = WorktreeIntegrateResponse.model_json_schema()
        quality_schema = json.dumps(schema["$defs"]["QualityGateResult"], sort_keys=True)
        self.assertIn('"reportPath"', quality_schema)
        self.assertIn('"publishedResultPath"', quality_schema)

        with self.assertRaises(ValidationError):
            finalize_tool_response(
                "worktree_integrate",
                {
                    "ok": True,
                    "operation": "worktree_integrate",
                    "quality_gate": {**quality_result, "unmodeledPath": "/private"},
                },
            )


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


def _readiness_registry(*, diagnostic: bool = False) -> RailRegistry:
    rails = (
        _readiness_rail("advisory", 1, posture="report-only"),
        _readiness_rail("lint", 1),
        _readiness_rail("suite", 2, prerequisite="lint"),
        _readiness_rail("coverage", 3, prerequisite="suite"),
        _readiness_rail("e2e", 4, prerequisite="coverage"),
        _readiness_rail("memory", 5, prerequisite="e2e", applicable=False),
    )
    return RailRegistry(
        registryId="portable-readiness",
        repositoryId="sample-repository",
        profiles=(
            RegistryProfile(
                profileId=_PROFILE,
                kind="diagnostic" if diagnostic else "certifying",
                gates=(1,) if diagnostic else (1, 2, 3, 4, 5),
            ),
        ),
        rails=rails[:2] if diagnostic else rails,
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
) -> GateResultManifest:
    gate_plan = plan.gates[gate - 1]
    results = []
    for rail in gate_plan.rails:
        status = "not-applicable" if rail.applicability.status == "not-applicable" else "pass"
        if enforcing_failure and rail.posture == "enforcing":
            status = "fail"
        if rail.posture == "report-only":
            status = "fail"
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
            candidateIdentity=_CANDIDATE,
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


def test_closeout_readiness_is_lossless_on_every_surface() -> None:
    source = _complete_readiness_input(_readiness_scenario())
    projection = compile_closeout_readiness(source)

    assert projection.certificationReady is True
    assert projection.lifecycle.state == "finalized"
    assert [gate.state for gate in projection.gates] == ["passed"] * 5
    assert projection.gates[0].rails[0].state == "report-only-fail"
    assert projection.gates[4].rails[0].state == "not-applicable"
    rendered = {
        readiness_projection_bytes(source, surface=surface) for surface in READINESS_SURFACES
    }
    assert len(rendered) == 1
    assert source.finalizationAuthority is not None
    assert source.finalizationAuthority.authorityDigest.encode() in next(iter(rendered))


def test_diagnostic_readiness_stays_non_certifying_with_matching_rails() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)
    diagnostic_registry = canonicalize_registry(_readiness_registry(diagnostic=True))
    diagnostic_plan = compile_certification_plan(
        diagnostic_registry,
        profile_id=_PROFILE,
        candidate_identity=_CANDIDATE,
    )
    diagnostic = DiagnosticReadinessObservation(
        revision=_REVISION,
        plan=diagnostic_plan,
        resultManifest=_readiness_manifest(diagnostic_registry, diagnostic_plan, 1),
    )
    projection = compile_closeout_readiness(
        source.model_copy(update={"diagnostics": (diagnostic,)})
    )

    assert projection.diagnostics[0].rails == projection.gates[0].rails
    assert projection.diagnostics[0].certificationPlanDigest != projection.certificationPlanDigest
    assert projection.certificationReady is True


def test_stale_certificates_and_invalid_profile_remain_non_green() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)
    invalidated = tuple(
        gate.model_copy(
            update={
                "state": "invalidated",
                "resultManifest": None,
                "certificateState": "stale" if gate.gate == 2 else "invalidated",
            }
        )
        for gate in source.gates[1:]
    )
    stale_source = source.model_copy(
        update={
            "lifecycle": LifecycleReadinessObservation(revision=_REVISION, state="admitted"),
            "gates": (source.gates[0], *invalidated),
            "finalizationAuthority": None,
            "finalizationInputs": None,
        }
    )
    stale = compile_closeout_readiness(stale_source)
    assert stale.certificationReady is False
    assert stale.gates[1].state == "invalidated"
    assert stale.gates[1].certificateState == "stale"

    empty_gates = tuple(
        GateReadinessObservation(revision=_REVISION, gate=gate, state="not-started")
        for gate in (1, 2, 3, 4, 5)
    )
    invalid = compile_closeout_readiness(
        stale_source.model_copy(
            update={
                "profile": ProfileReadinessObservation(revision=_REVISION, state="invalid"),
                "gates": empty_gates,
            }
        )
    )
    assert invalid.profile.state == "invalid"
    assert invalid.certificationReady is False


def test_admission_authority_and_lifecycle_contradictions_fail_closed() -> None:
    source = _complete_readiness_input(_readiness_scenario())
    not_started = tuple(
        GateReadinessObservation(revision=_REVISION, gate=gate, state="not-started")
        for gate in (1, 2, 3, 4, 5)
    )
    pre_admission = source.model_copy(
        update={
            "profile": ProfileReadinessObservation(revision=_REVISION, state="unresolved"),
            "lifecycle": LifecycleReadinessObservation(
                revision=_REVISION,
                state="admission-pending",
            ),
            "gates": not_started,
            "admission": None,
            "gateFiveInputs": None,
            "finalizationAuthority": None,
            "finalizationInputs": None,
        }
    )

    pending = compile_closeout_readiness(pre_admission)
    assert pending.lifecycle.state == "admission-pending"
    assert pending.certificationReady is False

    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            pre_admission.model_copy(
                update={
                    "lifecycle": LifecycleReadinessObservation(
                        revision=_REVISION,
                        state="admitted",
                    )
                }
            )
        )
    assert "admission-authority-missing" in _readiness_codes(caught.value)

    running = not_started[0].model_copy(update={"state": "running"})
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            pre_admission.model_copy(update={"gates": (running, *not_started[1:])})
        )
    assert "gate-started-before-admission" in _readiness_codes(caught.value)

    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(source.model_copy(update={"repositoryId": "other-repository"}))
    assert "admission-plan-mismatch" in _readiness_codes(caught.value)

    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(
                update={
                    "lifecycle": LifecycleReadinessObservation(
                        revision=_REVISION,
                        state="admission-pending",
                    )
                }
            )
        )
    assert "admission-lifecycle-contradiction" in _readiness_codes(caught.value)


def test_red_gate_generic_replacement_and_mixed_revision_fail_closed() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)
    red_manifest = _readiness_manifest(
        scenario.registry,
        scenario.plan,
        1,
        enforcing_failure=True,
    )
    red_gate = GateReadinessObservation(
        revision=_REVISION,
        gate=1,
        state="failed",
        resultManifest=red_manifest,
    )
    running_gate = GateReadinessObservation(revision=_REVISION, gate=2, state="running")
    later = tuple(
        GateReadinessObservation(
            revision=_REVISION,
            gate=gate,
            state="blocked",
            blockedBy=("gate-1",),
        )
        for gate in (3, 4, 5)
    )
    red_source = source.model_copy(
        update={
            "lifecycle": LifecycleReadinessObservation(revision=_REVISION, state="admitted"),
            "gates": (red_gate, running_gate, *later),
            "finalizationAuthority": None,
            "finalizationInputs": None,
        }
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(red_source)
    assert "red-gate-barrier-violated" in _readiness_codes(caught.value)

    generic = red_gate.model_copy(update={"genericTerminalReplacement": True})
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            red_source.model_copy(update={"gates": (generic, *red_source.gates[1:])})
        )
    assert "generic-terminal-replacement" in _readiness_codes(caught.value)

    mixed = source.gates[0].model_copy(
        update={"revision": ReadinessRevision(generationId="generation-10", revision=1)}
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(source.model_copy(update={"gates": (mixed, *source.gates[1:])}))
    assert "mixed-generation-readiness" in _readiness_codes(caught.value)


def test_catalog_and_diagnostic_promotion_fail_closed() -> None:
    scenario = _readiness_scenario()
    source = _complete_readiness_input(scenario)
    missing = scenario.manifests[0].model_copy(
        update={"railResults": scenario.manifests[0].railResults[1:]}
    )
    bad_gate = source.gates[0].model_copy(update={"resultManifest": missing})
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(
            source.model_copy(update={"gates": (bad_gate, *source.gates[1:])})
        )
    assert "gate-manifest-catalog-mismatch" in _readiness_codes(caught.value)

    promoted = DiagnosticReadinessObservation(
        revision=_REVISION,
        plan=scenario.plan,
        resultManifest=scenario.manifests[0],
    )
    with pytest.raises(CloseoutReadinessContractError) as caught:
        compile_closeout_readiness(source.model_copy(update={"diagnostics": (promoted,)}))
    assert "diagnostic-promotion" in _readiness_codes(caught.value)


def test_readiness_states_and_transitions_refuse_translation() -> None:
    require_readiness_transition("gate", "running", "passed")
    require_readiness_transition("certificate", "current-green", "invalidated")
    with pytest.raises(CloseoutReadinessContractError) as caught:
        require_readiness_transition("gate", "failed", "passed")
    assert "readiness-transition-invalid" in _readiness_codes(caught.value)

    payload = GateReadinessObservation(
        revision=_REVISION,
        gate=1,
        state="not-started",
    ).model_dump(mode="json")
    payload["state"] = "skipped"
    with pytest.raises(ValidationError):
        GateReadinessObservation.model_validate(payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
