"""Standalone CCR-R13 diagnostic contract tests: models and invariants.

Covers the closed vocabulary: structural acceptanceEligible/certifying false
literals, outcome/manifest shape rules, immutable attempt/manifest chains,
namespace isolation, and promotion refusal.  Fully standalone: it imports
only the package under test and stdlib/pytest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

import pytest
from agents_remember.certification import canonicalize_registry
from agents_remember.certification.certificate_models import CandidateIdentity
from agents_remember.certification.diagnostics.models import (
    DiagnosticArtifact,
    DiagnosticAttemptRecord,
    DiagnosticEnvironmentBinding,
    DiagnosticFailureRecord,
    DiagnosticPlanRecord,
    DiagnosticRunManifest,
    DiagnosticRunResult,
    DiagnosticRunResultDraft,
    DiagnosticRuntimeAuthorityBinding,
    DiagnosticTeardownRecord,
)
from agents_remember.certification.diagnostics.planning import compile_diagnostic_plan
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    ArtifactDeclaration,
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
from agents_remember.certification.planning import compile_certification_plan
from agents_remember.certification.results import (
    build_rail_result,
    compile_gate_result_manifest,
)
from pydantic import ValidationError

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
NONCE = "1a" * 32
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
    applicable: bool = True


_RAIL_SPECS = (
    RailSpec("advisory", 1, posture="report-only"),
    RailSpec("lint", 1),
    RailSpec("suite", 2),
    RailSpec("coverage", 3),
    RailSpec("e2e", 4),
    RailSpec("memory", 5),
)


def _identity(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def _rail(spec: RailSpec) -> RailDefinition:
    profile_rows: list[RailApplicability] = []
    for profile_id in (CERT_PROFILE, DIAG_PROFILE):
        if spec.gate == 5 and profile_id == DIAG_PROFILE:
            continue
        applicable = spec.applicable and spec.gate != 5
        profile_rows.append(
            RailApplicability(
                profileId=profile_id,
                status="applicable" if applicable else "not-applicable",
                selectionIdentity=f"selection:{spec.rail_id}",
                population="exact population" if applicable else None,
                reason=None if applicable else "profile excludes this rail",
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
        applicability=tuple(profile_rows),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId=f"{spec.rail_id}-evidence",
                mediaType="application/json",
                maxBytes=256,
            ),
        ),
        outputArtifacts=outputs,
    )


def scenario_registry() -> CanonicalRailRegistry:
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
            rails=tuple(_rail(spec) for spec in _RAIL_SPECS),
        )
    )


def certifying_plan(
    registry: CanonicalRailRegistry | None = None,
    candidate: CandidateIdentity = CANDIDATE,
) -> CertificationPlan:
    resolved = registry or scenario_registry()
    return compile_certification_plan(
        resolved,
        profile_id=CERT_PROFILE,
        candidate_identity=candidate,
    )


def diagnostic_plan(
    registry: CanonicalRailRegistry | None = None,
    certifying: CertificationPlan | None = None,
    gate: GateId = 4,
) -> CertificationPlan:
    resolved = registry or scenario_registry()
    return compile_diagnostic_plan(
        resolved,
        profile_id=DIAG_PROFILE,
        candidate_identity=CANDIDATE,
        certifying_plan=certifying or certifying_plan(resolved),
        gate=gate,
    )


def manifest_for(
    registry: CanonicalRailRegistry,
    plan: CertificationPlan,
    gate: GateId,
    *,
    red: bool = False,
) -> GateResultManifest:
    gate_plan = next(item for item in plan.gates if item.gate == gate)
    results = []
    for rail in gate_plan.rails:
        status = "not-applicable" if rail.applicability.status == "not-applicable" else "pass"
        if red and rail.posture == "enforcing":
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
            profileId=plan.profileId,
            candidateIdentity=plan.candidateIdentity,
            altitude=plan.profileKind,
        ),
    )


def evidence(
    rail_id: str,
    *,
    size: int = 32,
) -> RailEvidenceReference:
    return RailEvidenceReference(
        evidenceId=f"{rail_id}-evidence",
        sha256=content_digest({"evidence": rail_id}),
        size=size,
        reference=f"evidence://{rail_id}",
    )


def teardown_record(at: str = "2026-09-04T00:00:00Z") -> DiagnosticTeardownRecord:
    return DiagnosticTeardownRecord(
        releasedOwner=True,
        evidence=(evidence("e2e"),),
        at=at,
    )


def environment_binding() -> DiagnosticEnvironmentBinding:
    return DiagnosticEnvironmentBinding(
        environmentIdentity="codex-host",
        environmentDigest=content_digest({"environmentIdentity": "codex-host"}),
    )


def authority_binding(
    snapshot_digest: str = "b" * 64,
) -> DiagnosticRuntimeAuthorityBinding:
    return DiagnosticRuntimeAuthorityBinding(
        snapshotDigest=snapshot_digest,
        endpoint="container://shared-dagger-engine",
        engineId="shared-dagger-engine",
        layerStore="/var/lib/dagger",
        storeMountedPath="/var/lib/dagger",
        observedVersion="dagger 0.21.8",
        bindingDigest=content_digest(
            {
                "schemaVersion": "diagnostic-runtime-authority/v1",
                "snapshotDigest": snapshot_digest,
                "endpoint": "container://shared-dagger-engine",
                "engineId": "shared-dagger-engine",
                "layerStore": "/var/lib/dagger",
                "storeMountedPath": "/var/lib/dagger",
                "observedVersion": "dagger 0.21.8",
            }
        ),
    )


def plan_record(
    plan: CertificationPlan | None = None,
    gate: GateId = 4,
) -> DiagnosticPlanRecord:
    registry = scenario_registry()
    certifying = certifying_plan(registry)
    diagnostic = plan or diagnostic_plan(registry, certifying, gate)
    gate_plan = next(item for item in diagnostic.gates if item.gate == gate)
    payload = {
        "schemaVersion": "diagnostic-plan-record/v1",
        "candidateIdentity": CANDIDATE.model_dump(mode="json"),
        "registryDigest": registry.registryDigest,
        "certifyingPlanDigest": certifying.planDigest,
        "diagnosticPlanDigest": diagnostic.planDigest,
        "profileId": DIAG_PROFILE,
        "gate": gate,
        "gatePlanDigest": gate_plan.planDigest,
        "planVersion": "1.0.0",
    }
    return DiagnosticPlanRecord(**payload, planDigest=content_digest(payload))


def attempt_record(
    number: int = 1,
    nonce: str = NONCE,
    registry: CanonicalRailRegistry | None = None,
    diagnostic: CertificationPlan | None = None,
    state: Literal["reserved", "running", "terminal"] = "running",
) -> DiagnosticAttemptRecord:
    resolved = registry or scenario_registry()
    certifying = certifying_plan(resolved)
    diag = diagnostic or diagnostic_plan(resolved, certifying)
    payload = {
        "schemaVersion": "diagnostic-attempt/v1",
        "candidateIdentity": CANDIDATE.model_dump(mode="json"),
        "attemptNumber": number,
        "diagnosticNonce": nonce,
        "registryDigest": resolved.registryDigest,
        "certifyingPlanDigest": certifying.planDigest,
        "diagnosticPlanDigest": diag.planDigest,
        "planVersion": "1.0.0",
        "gate": 4,
        "profileId": DIAG_PROFILE,
        "requestedAt": "2026-09-04T00:00:00Z",
        "state": state,
    }
    return DiagnosticAttemptRecord(**payload, attemptDigest=content_digest(payload))


def draft(
    disposition: Literal["pass", "fail", "aborted", "hard-failure"],
    *,
    number: int = 1,
    manifest: GateResultManifest | None = None,
    failure: DiagnosticFailureRecord | None = None,
) -> DiagnosticRunResultDraft:
    return DiagnosticRunResultDraft(
        attemptNumber=number,
        candidateIdentity=CANDIDATE,
        registryDigest=scenario_registry().registryDigest,
        certifyingPlanDigest=certifying_plan().planDigest,
        diagnosticPlanDigest=diagnostic_plan().planDigest,
        planVersion="1.0.0",
        gate=4,
        profileId=DIAG_PROFILE,
        diagnosticNonce=NONCE,
        acceptanceEligible=False,
        certifying=False,
        disposition=disposition,
        failure=failure,
        environment=environment_binding(),
        runtimeAuthority=authority_binding(),
        manifest=manifest,
        teardown=teardown_record(),
        artifacts=(),
        startedAt="2026-09-04T00:00:00Z",
        terminalAt="2026-09-04T00:10:00Z",
    )


def finalize(draft_value: DiagnosticRunResultDraft, predecessor: str = "") -> DiagnosticRunResult:
    payload = {**draft_value.model_dump(mode="json"), "predecessorDigest": predecessor}
    result_id = content_digest(payload)
    final_payload = {**payload, "resultId": result_id}
    return DiagnosticRunResult(**final_payload, resultDigest=content_digest(final_payload))


class ModelContractTests:
    def test_result_is_structural_non_certifying_and_binds_r16_nonce(self) -> None:
        result = finalize(draft("aborted"))
        assert result.acceptanceEligible is False
        assert result.certifying is False
        assert result.diagnosticNonce == NONCE
        assert len(result.diagnosticNonce) == 64
        assert result.disposition == "aborted"
        assert result.manifest is None
        assert result.teardown.releasedOwner is True
        assert result.resultDigest == content_digest(
            result.model_dump(mode="json", exclude={"resultDigest"})
        )

    def test_extra_fields_are_forbidden_on_every_record(self) -> None:
        with pytest.raises(ValidationError):
            DiagnosticAttemptRecord.model_validate(
                {
                    **attempt_record().model_dump(mode="json"),
                    "deliveryAttemptId": "worker-delivery-1",
                }
            )
        with pytest.raises(ValidationError):
            DiagnosticRunResult.model_validate(
                {
                    **finalize(draft("aborted")).model_dump(mode="json"),
                    "closeoutGeneration": 7,
                }
            )

    def test_promotion_to_acceptance_or_certifying_literals_is_refused(self) -> None:
        payload = finalize(draft("aborted")).model_dump(mode="json")
        with pytest.raises(ValidationError):
            DiagnosticRunResult.model_validate({**payload, "certifying": True})
        with pytest.raises(ValidationError):
            DiagnosticRunResult.model_validate({**payload, "acceptanceEligible": True})

    def test_pass_and_fail_require_the_complete_diagnostic_altitude_manifest(self) -> None:
        with pytest.raises(ValidationError):
            draft("pass")
        with pytest.raises(ValidationError):
            draft("fail")

    def test_fail_outcome_requires_its_exact_scenario_failure(self) -> None:
        registry = scenario_registry()
        diag = diagnostic_plan(registry, certifying_plan(registry))
        red = manifest_for(registry, diag, 4, red=True)
        with pytest.raises(ValidationError):
            draft("fail", manifest=red)
        failed = DiagnosticFailureRecord(
            failureClass="scenario",
            code="checkpoint-mismatch",
            detail="queued completion was never observed",
            rail=_identity("e2e"),
            correctiveOwner="portable-owner",
            evidence=(evidence("e2e"),),
        )
        with_failure = draft("fail", manifest=red, failure=failed)
        assert with_failure.disposition == "fail"
        assert with_failure.manifest is not None
        assert with_failure.manifest.disposition == "red"

    def test_scenario_failure_must_name_the_exact_checkpoint_rail(self) -> None:
        with pytest.raises(ValidationError):
            DiagnosticFailureRecord(
                failureClass="scenario",
                code="checkpoint-mismatch",
                detail="no checkpoint named",
                correctiveOwner="portable-owner",
                evidence=(),
            )

    def test_hard_failures_are_typed_infrastructure_or_parser_only(self) -> None:
        for failure_class in ("infrastructure", "parser"):
            failure = DiagnosticFailureRecord(
                failureClass=failure_class,
                code=f"{failure_class}-broken",
                detail="runner died before parsing",
                correctiveOwner="dagger-owner",
                evidence=(evidence("e2e"),),
            )
            hard = draft("hard-failure", failure=failure)
            assert hard.manifest is None
            with pytest.raises(ValidationError):
                draft(
                    "hard-failure",
                    failure=failure,
                    manifest=manifest_for(scenario_registry(), diagnostic_plan(), 4),
                )
        wrong = DiagnosticFailureRecord(
            failureClass="scenario",
            code="checkpoint-mismatch",
            detail="scenario failure",
            rail=_identity("e2e"),
            correctiveOwner="portable-owner",
            evidence=(evidence("e2e"),),
        )
        with pytest.raises(ValidationError):
            draft("hard-failure", failure=wrong)

    def test_hard_failure_requires_bounded_evidence(self) -> None:
        with pytest.raises(ValidationError):
            DiagnosticFailureRecord(
                failureClass="parser",
                code="strict-parser",
                detail="no evidence",
                correctiveOwner="parser-owner",
                evidence=(),
            )

    def test_aborted_and_pass_outcomes_carry_no_failure_record(self) -> None:
        failure = DiagnosticFailureRecord(
            failureClass="infrastructure",
            code="dagger-down",
            detail="engine lost",
            correctiveOwner="dagger-owner",
            evidence=(evidence("e2e"),),
        )
        with pytest.raises(ValidationError):
            draft("aborted", failure=failure)

    def test_manifest_altitude_must_be_diagnostic_and_gate_must_match(self) -> None:
        registry = scenario_registry()
        certifying = certifying_plan(registry)
        diag = diagnostic_plan(registry, certifying)
        certifying_gate_two = manifest_for(registry, certifying, 2)
        with pytest.raises(ValidationError):
            DiagnosticRunResultDraft.model_validate(
                {
                    **draft("aborted").model_dump(mode="json"),
                    "manifest": certifying_gate_two.model_dump(mode="json"),
                }
            )
        # certifying manifest embedded into a pass outcome must also be refused
        with pytest.raises(ValidationError):
            draft("pass", manifest=certifying_gate_two)
        gate_three = diag.gates[2].gate
        wrong_gate = manifest_for(registry, diag, gate_three)
        with pytest.raises(ValidationError):
            draft("pass", manifest=wrong_gate)

    def test_green_manifest_pass_is_still_not_acceptance_eligible(self) -> None:
        registry = scenario_registry()
        diag = diagnostic_plan(registry, certifying_plan(registry))
        green = manifest_for(registry, diag, 4)
        passing = finalize(draft("pass", manifest=green))
        assert passing.disposition == "pass"
        assert passing.certifying is False
        assert passing.acceptanceEligible is False
        assert passing.resultDigest == content_digest(
            passing.model_dump(mode="json", exclude={"resultDigest"})
        )

    def test_result_digest_and_attempt_digest_are_content_bound(self) -> None:
        result = finalize(draft("aborted"))
        tampered = result.model_copy(update={"resultDigest": "d" * 64})
        with pytest.raises(ValidationError):
            DiagnosticRunResult.model_validate(tampered.model_dump(mode="json"))
        attempt = attempt_record()
        with pytest.raises(ValidationError):
            DiagnosticAttemptRecord.model_validate(
                {**attempt.model_dump(mode="json"), "attemptDigest": "e" * 64}
            )

    def test_attempt_nonce_pattern_and_attempt_numbers(self) -> None:
        with pytest.raises(ValidationError):
            attempt_record(nonce="not-hex")
        with pytest.raises(ValidationError):
            attempt_record(number=0)
        with pytest.raises(ValidationError):
            DiagnosticAttemptRecord.model_validate(
                {**attempt_record().model_dump(mode="json"), "attemptNumber": 5}
            )

    def test_artifacts_live_in_the_diagnostic_namespace_only(self) -> None:
        with pytest.raises(ValidationError):
            DiagnosticArtifact.model_validate(
                {
                    "artifactId": "scenario-log",
                    "sha256": "b" * 64,
                    "size": 128,
                    "evidenceRef": "quality-report://clean-results.json",
                    "namespace": "certifying",
                }
            )
        artifact = DiagnosticArtifact(
            artifactId="scenario-log",
            sha256="b" * 64,
            size=128,
            evidenceRef="diagnostic://scenario-log.json",
        )
        assert artifact.namespace == "diagnostic"

    def _terminal_result(
        self,
        number: int,
        nonce: str,
        predecessor: str = "",
    ):
        payload = draft("aborted", number=number).model_dump(mode="json")
        bound = DiagnosticRunResultDraft(**{**payload, "diagnosticNonce": nonce})
        return finalize(bound, predecessor=predecessor)

    def test_run_manifest_chain_is_gapless_and_immutable(self) -> None:
        first = self._terminal_result(1, "1" + "a" * 63)
        second = self._terminal_result(2, "2" + "a" * 63, predecessor=first.resultId)
        terminal_attempts = tuple(
            attempt_record(number=number, nonce=f"{number}{'a' * 63}", state="terminal")
            for number in (1, 2)
        )
        manifest_payload = {
            "schemaVersion": "diagnostic-run-manifest/v1",
            "candidateIdentity": CANDIDATE.model_dump(mode="json"),
            "attempts": [item.model_dump(mode="json") for item in terminal_attempts],
            "results": [item.model_dump(mode="json") for item in (first, second)],
        }
        manifest = DiagnosticRunManifest(
            **manifest_payload, manifestDigest=content_digest(manifest_payload)
        )
        assert manifest.newestTerminal == second
        assert manifest.results[1].predecessorDigest == first.resultId
        # a duplicate scenario cannot fabricate two results under one attempt
        assert len(manifest.results) == len(manifest.attempts)

    def test_run_manifest_refuses_broken_chain_and_mixed_nonces(self) -> None:
        first = self._terminal_result(1, "1" + "a" * 63)
        broken = self._terminal_result(2, "2" + "a" * 63, predecessor="f" * 64)
        terminal = tuple(
            attempt_record(number=number, nonce=f"{number}{'a' * 63}", state="terminal")
            for number in (1, 2)
        )
        payload = {
            "schemaVersion": "diagnostic-run-manifest/v1",
            "candidateIdentity": CANDIDATE.model_dump(mode="json"),
            "attempts": [item.model_dump(mode="json") for item in terminal],
            "results": [item.model_dump(mode="json") for item in (first, broken)],
        }
        with pytest.raises(ValidationError):
            DiagnosticRunManifest(**payload, manifestDigest=content_digest(payload))
        # A result whose nonce does not match its terminal attempt is refused.
        mismatched = self._terminal_result(1, "0" + "c" * 63)
        payload_two = {
            "schemaVersion": "diagnostic-run-manifest/v1",
            "candidateIdentity": CANDIDATE.model_dump(mode="json"),
            "attempts": [item.model_dump(mode="json") for item in (terminal[0],)],
            "results": [mismatched.model_dump(mode="json")],
        }
        with pytest.raises(ValidationError):
            DiagnosticRunManifest(**payload_two, manifestDigest=content_digest(payload_two))

    def test_run_manifest_refuses_gapless_prefix_violations(self) -> None:
        terminal = attempt_record(number=2, nonce="2" + "a" * 63, state="terminal")
        payload = {
            "schemaVersion": "diagnostic-run-manifest/v1",
            "candidateIdentity": CANDIDATE.model_dump(mode="json"),
            "attempts": [terminal.model_dump(mode="json")],
            "results": [],
        }
        with pytest.raises(ValidationError):
            DiagnosticRunManifest(**payload, manifestDigest=content_digest(payload))

    def test_plan_record_digest_is_verified(self) -> None:
        record = plan_record()
        assert record.gate == 4
        assert record.planDigest == content_digest(
            record.model_dump(mode="json", exclude={"planDigest"})
        )
        with pytest.raises(ValidationError):
            DiagnosticPlanRecord.model_validate(
                {**record.model_dump(mode="json"), "planDigest": "0" * 64}
            )

    def test_json_dump_has_no_delivery_or_closeout_identity_fields(self) -> None:
        result = finalize(draft("aborted")).model_dump(mode="json")
        rendered = json.dumps(result, sort_keys=True)
        assert "deliveryAttempt" not in rendered
        assert "closeoutGeneration" not in rendered
        assert "operationKind" not in rendered
        assert rendered.count('"acceptanceEligible": false') == 1
        assert rendered.count('"certifying": false') == 1


class ForbiddenOverreachTests:
    def test_no_certifying_plan_can_be_promoted_to_diagnostic_altitude(self) -> None:
        # result construction through the closed model refuses any manifest that
        # is not diagnostic altitude even when the caller tries to smuggle it.
        registry = scenario_registry()
        certifying = certifying_plan(registry)
        red_certifying = manifest_for(registry, certifying, 4, red=True)
        diag = diagnostic_plan(registry, certifying)
        try:
            DiagnosticRunResultDraft(
                attemptNumber=1,
                candidateIdentity=CANDIDATE,
                registryDigest=registry.registryDigest,
                certifyingPlanDigest=certifying.planDigest,
                diagnosticPlanDigest=diag.planDigest,
                planVersion="1.0.0",
                gate=4,
                profileId=DIAG_PROFILE,
                diagnosticNonce=NONCE,
                acceptanceEligible=False,
                certifying=False,
                disposition="fail",
                failure=DiagnosticFailureRecord(
                    failureClass="scenario",
                    code="checkpoint-mismatch",
                    detail="smuggled certifying manifest",
                    rail=_identity("e2e"),
                    correctiveOwner="portable-owner",
                    evidence=(evidence("e2e"),),
                ),
                environment=environment_binding(),
                runtimeAuthority=authority_binding(),
                manifest=red_certifying,
                teardown=teardown_record(),
                artifacts=(),
                startedAt="2026-09-04T00:00:00Z",
                terminalAt="2026-09-04T00:10:00Z",
            )
        except ValidationError:
            pass
        else:
            raise AssertionError("certifying-altitude manifest was promoted into a diagnostic")
