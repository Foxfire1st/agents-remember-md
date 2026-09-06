"""CCR-R16-v3 adapter traces and durable boundary/gate projections."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from agents_remember.certification import (
    build_rail_result,
    canonicalize_registry,
    classify_certificate_invalidation,
    compile_certification_plan,
    compile_finalization_authority,
    compile_gate_certificate,
    compile_gate_result_manifest,
    plan_certificate_reuse,
)
from agents_remember.certification.certificate_admission import gate_semantic_digest
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    CertificateReusePlan,
)
from agents_remember.certification.certificate_models import (
    AdmissionGateIdentity,
    CertificateRailInventory,
    CertificationAdmissionManifest,
    CertificationAdmissionSemanticEnvelope,
    CoherenceSubrecordIdentity,
    CreationProvenance,
    FinalizationCurrentInputs,
    GateCertificate,
    GateCertificateIdentity,
    GateCertificateIssuanceContext,
    GateCertificateSemanticEnvelope,
    GateFiveSemanticInputs,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.lifecycle_models import LifecycleAdmissionManifest
from agents_remember.certification.models import (
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationPlan,
    GatePlan,
    GateResultAdmission,
    GateResultManifest,
    RailAdapterDefinition,
    RailApplicability,
    RailArtifactResult,
    RailDefinition,
    RailEvidenceContract,
    RailEvidenceReference,
    RailRegistry,
    RailResult,
    RailRuntimeInputs,
    RailTerminalObservation,
    RegistryProfile,
)
from agents_remember.certification.repository_profiles.models import (
    ArtifactDeclaration,
)
from agents_remember.certification.telemetry import (
    TelemetryEvent,
    TelemetryExecutionContext,
    compile_admission_started,
    compile_candidate_admitted,
    compile_certificate_invalidated,
    compile_diagnostic_started,
    compile_diagnostic_terminal,
    compile_finalization_boundary_resumed,
    compile_finalization_completed,
    compile_finalization_started,
    compile_gate_blocked,
    compile_gate_catalog_complete,
    compile_gate_fail,
    compile_gate_pass_published,
    compile_gate_pass_reused,
    compile_gate_started,
    compile_operation_terminal,
    compile_rail_started,
    compile_rail_terminal,
    compile_reuse_dependency_decision,
    project_execution_telemetry,
    validate_execution_telemetry,
)
from agents_remember.certification.telemetry.models import (
    GateCitation,
    PredecessorBoundary,
    R21DependencyDecision,
    RailStartedPayload,
)
from agents_remember.models.certification.base import GateId, RailIdentity

_DIGEST = "a" * 64
_CANDIDATE = CandidateIdentity(kind="content-digest", value="c" * 64)
_CLASS_BY_GATE: dict[int, str] = {
    1: "pre-test-quality",
    2: "ordinary-test-suite",
    3: "post-test-quality",
    4: "integration-test",
    5: "memory-quality",
}

_TIMESTAMP = "2026-09-03T00:00:00+02:00"
_PROFILE_CI = "portable-ci"
_PROFILE_TARGETED = "closeout-targeted"
_GIT_CANDIDATE = CandidateIdentity(kind="git-tree", value="c" * 40)
_GATE_IDS: tuple[int, ...] = (1, 2, 3, 4, 5)


@dataclass(frozen=True)
class _Scenario:
    """One consistent registry/plan; admission is present only when certification is."""

    registry: CanonicalRailRegistry
    plan: CertificationPlan
    admission: CertificationAdmissionManifest | None


@dataclass
class _Trace:
    events: list[TelemetryEvent] = field(default_factory=list)
    profile_id: str = _PROFILE_CI
    candidate: object = _CANDIDATE

    def ctx(self) -> TelemetryExecutionContext:
        return TelemetryExecutionContext(
            executionKind="closeout-generation",
            executionId="gen-13-closeout",
            eventRevision=len(self.events) + 1,
            operationKind="closeout",
            generation=13,
            candidate=self.candidate,  # type: ignore[arg-type]
            profileId=self.profile_id,
            occurredAt=_TIMESTAMP,
        )

    def push(self, event: TelemetryEvent) -> None:
        self.events.append(event)


@dataclass(frozen=True)
class _RailSpec:
    rail_id: str
    gate: GateId
    prerequisites: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    output_artifacts: tuple[str, ...] = ()


def _identity(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def _portable_rail(spec: _RailSpec) -> RailDefinition:
    return RailDefinition(
        identity=_identity(spec.rail_id),
        gate=spec.gate,
        railClass=_CLASS_BY_GATE[spec.gate],  # type: ignore[arg-type]
        authority="memory-domain" if spec.gate == 5 else "repository-profile",
        ownerClass="portable-owner",
        correctiveOwner="portable-owner",
        posture="enforcing",
        orderKey=spec.rail_id,
        prerequisites=tuple(_identity(item) for item in spec.prerequisites),
        requiredArtifacts=spec.required_artifacts,
        adapter=RailAdapterDefinition(
            adapterKind="process-adapter",
            adapterId=f"{spec.rail_id}-adapter",
            configurationDigest=_DIGEST,
            executionEvidence=f"adapter://{spec.rail_id}",
        ),
        runtimeInputs=RailRuntimeInputs(runtimeIdentity="portable-runtime"),
        applicability=(
            RailApplicability(
                profileId="portable-ci",
                status="applicable",
                selectionIdentity=f"selection:{spec.rail_id}",
                population="portable population",
            ),
        ),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId=f"{spec.rail_id}-evidence",
                mediaType="application/json",
                maxBytes=128,
            ),
        ),
        outputArtifacts=tuple(
            ArtifactDeclaration(
                artifactId=artifact_id,
                schemaVersion="artifact/v1",
                mediaType="application/json",
            )
            for artifact_id in spec.output_artifacts
        ),
    )


def _portable_specs() -> tuple[_RailSpec, ...]:
    return (
        _RailSpec("lint", 1),
        _RailSpec("types", 1),
        _RailSpec("package", 1, prerequisites=("lint",)),
        _RailSpec("suite", 2, prerequisites=("types",), output_artifacts=("suite-data",)),
        _RailSpec(
            "coverage",
            3,
            prerequisites=("suite",),
            required_artifacts=("suite-data",),
        ),
        _RailSpec("clean-room", 4, prerequisites=("coverage",)),
        _RailSpec("memory", 5, prerequisites=("clean-room",)),
    )


def _portable_registry() -> RailRegistry:
    return RailRegistry(
        registryId="portable-closeout",
        repositoryId="sample-repository",
        profiles=(
            RegistryProfile(
                profileId="portable-ci",
                kind="certifying",
                gates=(5, 3, 1, 4, 2),
            ),
        ),
        rails=tuple(_portable_rail(spec) for spec in _portable_specs()),
    )


def _portable_plan(registry: RailRegistry | None = None) -> CertificationPlan:
    canonical = canonicalize_registry(registry or _portable_registry())
    return compile_certification_plan(
        canonical,
        profile_id=canonical.registry.profiles[0].profileId,
        candidate_identity=_CANDIDATE,
    )


def _portable() -> _Scenario:
    registry = canonicalize_registry(_portable_registry())
    plan = _portable_plan(registry.registry)
    return _Scenario(registry, plan, None)


def _provenance(marker: str = "telemetry") -> CreationProvenance:
    return CreationProvenance(
        createdAt=_TIMESTAMP,
        producer=f"test-{marker}",
        evidenceRef=f"evidence://{marker}",
    )


def _certificate_scenario() -> _Scenario:
    """One registry/plan/admission triple under the portable-certifying rails."""

    registry = canonicalize_registry(_portable_registry())
    plan = compile_certification_plan(
        registry,
        profile_id=registry.registry.profiles[0].profileId,
        candidate_identity=_GIT_CANDIDATE,
    )
    admission = _admission_for(registry, plan)
    return _Scenario(registry, plan, admission)


def _admission_for(
    registry: CanonicalRailRegistry, plan: CertificationPlan
) -> CertificationAdmissionManifest:
    gates = tuple(
        AdmissionGateIdentity(
            gate=gate_plan.gate,
            gatePlanDigest=gate_plan.planDigest,
            gateSemanticDigest=gate_semantic_digest(gate_plan),
            repositoryGatePlanDigest=_DIGEST if gate_plan.gate <= 4 else None,
            semanticInputs=(),
        )
        for gate_plan in plan.gates
    )
    envelope = CertificationAdmissionSemanticEnvelope(
        repositoryId=registry.registry.repositoryId,
        candidateCodeTree=_GIT_CANDIDATE,
        profileId=plan.profileId,
        certificationPlanDigest=plan.planDigest,
        admittedProfileDigest=_DIGEST,
        registryDigest=registry.registryDigest,
        gates=gates,
    )
    return CertificationAdmissionManifest(
        semanticEnvelope=envelope,
        admissionDigest=content_digest(envelope),
        provenance=_provenance(),
    )


def _gate_plan(scenario: _Scenario, gate: int) -> GatePlan:
    return next(item for item in scenario.plan.gates if item.gate == gate)


def _scenario_manifest(
    scenario: _Scenario,
    gate: int,
    *,
    statuses: dict[str, str] | None = None,
    altitude: Literal["certifying", "diagnostic"] = "certifying",
) -> GateResultManifest:
    gate_plan = _gate_plan(scenario, gate)
    results = _results(scenario, gate, statuses=statuses)
    plan = scenario.plan
    return compile_gate_result_manifest(
        scenario.registry,
        plan,
        gate_plan,
        results,
        GateResultAdmission(
            profileId=plan.profileId,
            candidateIdentity=plan.candidateIdentity,
            altitude=altitude,
        ),
    )


def _results(
    scenario: _Scenario,
    gate: int,
    *,
    statuses: dict[str, str] | None = None,
) -> tuple[RailResult, ...]:
    gate_plan = _gate_plan(scenario, gate)
    results = []
    for compiled in gate_plan.rails:
        status = (statuses or {}).get(compiled.identity.railId, "pass")
        blocked_by = (
            tuple(
                item.identity
                for item in gate_plan.rails
                if any(
                    prerequisite.railId == item.identity.railId
                    for prerequisite in compiled.prerequisites
                )
            )
            if status == "blocked"
            else ()
        )
        artifacts = (
            tuple(
                RailArtifactResult(
                    artifactId=item.artifactId,
                    sha256=content_digest({"artifact": item.artifactId}),
                    size=32,
                    evidenceRef=f"artifact://{item.artifactId}",
                )
                for item in compiled.outputArtifacts
            )
            if status == "pass"
            else ()
        )
        observation = RailTerminalObservation(
            rail=compiled.identity,
            status=status,  # type: ignore[arg-type]
            code=f"{compiled.identity.railId}-{status}",
            blockedBy=blocked_by,
            artifacts=artifacts,
            evidence=(
                RailEvidenceReference(
                    evidenceId=compiled.evidenceContract[0].evidenceId,
                    sha256=_DIGEST,
                    size=16,
                    reference=f"evidence://{compiled.identity.railId}",
                ),
            ),
        )
        results.append(build_rail_result(gate_plan, observation))
    return tuple(results)


@dataclass(frozen=True)
class _GateRun:
    """Optional knobs one gate run inside a trace may vary."""

    attempt: int = 1
    catalog_revision: int = 1
    statuses: dict[str, str] | None = None
    green_prefix: Sequence[GateCertificateIdentity] = ()


_DEFAULT_GATE_RUN = _GateRun()


def _run_gate(
    trace: _Trace,
    scenario: _Scenario,
    gate: int,
    run: _GateRun = _DEFAULT_GATE_RUN,
) -> GateResultManifest:
    gate_plan = _gate_plan(scenario, gate)
    trace.push(
        compile_gate_started(
            trace.ctx(),
            gate_plan=gate_plan,
            attempt=run.attempt,
            green_predecessors=run.green_prefix,
        )
    )
    for compiled in gate_plan.rails:
        trace.push(
            compile_rail_started(
                trace.ctx(),
                gate_plan=gate_plan,
                rail=compiled.identity,
                attempt=run.attempt,
                repetition=2 if gate == 4 else None,
            )
        )
    for result in _results(scenario, gate, statuses=run.statuses):
        trace.push(
            compile_rail_terminal(
                trace.ctx(),
                gate_plan=gate_plan,
                result=result,
                attempt=run.attempt,
            )
        )
    manifest = _scenario_manifest(scenario, gate, statuses=run.statuses)
    trace.push(
        compile_gate_catalog_complete(
            trace.ctx(),
            manifest=manifest,
            attempt=run.attempt,
            catalog_revision=run.catalog_revision,
        )
    )
    return manifest


def _certify_through(
    scenario: _Scenario,
    final_gate: int,
    *,
    memory_inputs: GateFiveSemanticInputs | None = None,
) -> tuple[GateCertificate, ...]:
    assert scenario.admission is not None
    certificates: list[GateCertificate] = []
    for gate in _GATE_IDS[:final_gate]:
        certificates.append(
            compile_gate_certificate(
                scenario.admission,
                _gate_plan(scenario, gate),
                _scenario_manifest(scenario, gate),
                certificates,
                GateCertificateIssuanceContext(
                    provenance=_provenance(),
                    gateFiveInputs=memory_inputs if gate == 5 else None,
                ),
            )
        )
    return tuple(certificates)


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


def _catalog_id(trace: _Trace, gate: int) -> str:
    for event in reversed(trace.events):
        if event.eventKind == "gate-catalog-complete" and event.gate == gate:
            assert event.gateResultManifestId is not None
            return event.gateResultManifestId
    raise AssertionError(f"no gate {gate} catalog in trace")


def _decision(
    scenario: _Scenario,
    certificates: Sequence[GateCertificate],
    *,
    final: bool = False,
) -> R21DependencyDecision:
    del scenario
    plan = CertificateReusePlan(
        reusedCertificates=tuple(item.identity for item in certificates),
        invalidatedGates=(),
        firstGateToRun=cast("GateId | None", None if final else len(certificates) + 1),
        finalizationRevalidationRequired=False,
        zeroGateStarts=final,
    )
    return compile_reuse_dependency_decision(plan)


def _synthetic_prefix(gate: int) -> tuple[GateCertificateIdentity, ...]:
    return tuple(
        GateCertificateIdentity(gate=cast("GateId", item), certificateDigest=_DIGEST)
        for item in range(1, gate)
    )


def _admit(scenario: _Scenario, trace: _Trace) -> None:
    trace.push(
        compile_admission_started(
            trace.ctx(),
            predecessor=PredecessorBoundary(zeroStart=True),
        )
    )
    admission = scenario.admission
    if admission is None:
        admission = cast(
            "CertificationAdmissionManifest",
            type("CertificationAdmissionManifest", (), {"admissionDigest": _DIGEST})(),
        )
    trace.push(
        compile_candidate_admitted(
            trace.ctx(),
            lifecycle_admission=_lifecycle_admission(),
            certification_admission=admission,
            gate_one_plan=_gate_plan(scenario, 1),  # type: ignore[arg-type]
        )
    )


def _lifecycle_admission() -> LifecycleAdmissionManifest:
    return cast(
        "LifecycleAdmissionManifest",
        type("LifecycleAdmissionManifest", (), {"admissionDigest": _DIGEST})(),
    )


def _diagnostic_ctx(trace: _Trace) -> TelemetryExecutionContext:
    return TelemetryExecutionContext(
        executionKind="diagnostic-run",
        executionId="diag-gate-four",
        eventRevision=len(trace.events) + 1,
        diagnosticNonce="f" * 32,
        candidate=_CANDIDATE,
        profileId=_PROFILE_CI,
        occurredAt=_TIMESTAMP,
    )


def test_multi_failure_gate_one_projects_failed_and_zero_start() -> None:
    scenario = _portable()
    trace = _Trace()
    trace.push(
        compile_admission_started(
            trace.ctx(),
            predecessor=PredecessorBoundary(priorGeneration=12),
        )
    )
    _admit(scenario, trace)
    _run_gate(
        trace,
        scenario,
        1,
        _GateRun(statuses={"lint": "fail", "package": "blocked"}),
    )
    trace.push(
        compile_gate_fail(
            trace.ctx(),
            manifest=cast("GateResultManifest", _gate_plan(scenario, 1)),
            citation=GateCitation(
                attempt=1,
                catalog_revision=1,
                catalog_manifest_id=_catalog_id(trace, 1),
            ),
            stable_cause="multi-failure gate one",
        )
    )
    for gate in (2, 3, 4, 5):
        trace.push(
            compile_gate_blocked(
                trace.ctx(),
                gate=gate,
                red_predecessor_gate=1,
            )
        )
    assert validate_execution_telemetry(trace.events).ok
    projection = project_execution_telemetry(trace.events)
    assert projection.boundary.admissionState == "admitted"
    assert projection.gates[0].state == "failed"
    for index in (1, 2, 3, 4):
        gate_projection = projection.gates[index]
        assert gate_projection.state == "blocked"
        assert gate_projection.blockedBy == ("1",)
        assert gate_projection.rails == ()


def test_gate_two_red_barrier_blocks_later_gates() -> None:
    scenario = _portable()
    trace = _Trace()
    _admit(scenario, trace)
    _run_gate(trace, scenario, 1)
    trace.push(
        compile_gate_pass_reused(
            trace.ctx(),
            prior_certificate=_synthetic_certificate(scenario, 1),
            citation=GateCitation(
                attempt=1,
                catalog_revision=1,
                catalog_manifest_id=_catalog_id(trace, 1),
            ),
            dependency_decision=_decision(scenario, (), final=True),
        )
    )
    _run_gate(
        trace,
        scenario,
        2,
        _GateRun(statuses={"suite": "fail"}, green_prefix=_synthetic_prefix(2)),
    )
    trace.push(
        compile_gate_fail(
            trace.ctx(),
            manifest=cast("GateResultManifest", _gate_plan(scenario, 2)),
            citation=GateCitation(
                attempt=1,
                catalog_revision=1,
                catalog_manifest_id=_catalog_id(trace, 2),
            ),
            stable_cause="gate two red barrier",
        )
    )
    for gate in (3, 4, 5):
        trace.push(
            compile_gate_blocked(
                trace.ctx(),
                gate=gate,
                red_predecessor_gate=2,
            )
        )
    assert validate_execution_telemetry(trace.events).ok
    projection = project_execution_telemetry(trace.events)
    assert projection.gates[0].state == "reused"
    assert projection.gates[1].state == "failed"
    assert projection.gates[2].state == "blocked"


def _synthetic_certificate(scenario: _Scenario, gate: int) -> GateCertificate:
    envelope = GateCertificateSemanticEnvelope(
        gate=cast("GateId", gate),
        repositoryId=getattr(scenario.plan, "profileId", "sample-repository"),
        candidateCodeTree=CandidateIdentity(kind="git-tree", value="c" * 40),
        admissionDigest=getattr(scenario.admission, "admissionDigest", _DIGEST)
        if scenario.admission is not None
        else _DIGEST,
        admittedProfileDigest=_DIGEST,
        registryDigest=scenario.registry.registryDigest,
        gatePlanDigest=_DIGEST,
        gateSemanticDigest=_DIGEST,
        repositoryGatePlanDigest=_DIGEST if gate <= 4 else None,
        directPredecessors=_synthetic_prefix(gate),
        semanticInputs=(),
        consumedArtifacts=(),
        resultManifestDigest=_DIGEST,
        terminalDisposition="green",
        railInventory=(
            CertificateRailInventory(
                rail=RailIdentity(railId="synthetic-rail", version="1.0.0"),
                resultDigest=_DIGEST,
                status="pass",
                code="synthetic-pass",
                correctiveOwner="synthetic-owner",
            ),
        ),
        artifactInventory=(),
        evidenceInventory=(),
    )
    return GateCertificate(
        semanticEnvelope=envelope,
        certificateDigest=content_digest(envelope),
        provenance=_provenance("synthetic"),
    )


def test_gate_three_artifact_failure_projects_failed_gate() -> None:
    scenario = _portable()
    trace = _Trace()
    _admit(scenario, trace)
    for gate in (1, 2):
        _run_gate(
            trace,
            scenario,
            gate,
            _GateRun(green_prefix=_synthetic_prefix(gate)),
        )
        trace.push(
            compile_gate_pass_reused(
                trace.ctx(),
                prior_certificate=_synthetic_certificate(scenario, gate),
                citation=GateCitation(
                    attempt=1,
                    catalog_revision=1,
                    catalog_manifest_id=_catalog_id(trace, gate),
                ),
                dependency_decision=_decision(scenario, (), final=True),
            )
        )
    _run_gate(
        trace,
        scenario,
        3,
        _GateRun(
            statuses={"coverage": "fail"},
            green_prefix=_synthetic_prefix(3),
        ),
    )
    trace.push(
        compile_gate_fail(
            trace.ctx(),
            manifest=cast("GateResultManifest", _gate_plan(scenario, 3)),
            citation=GateCitation(
                attempt=1,
                catalog_revision=1,
                catalog_manifest_id=_catalog_id(trace, 3),
            ),
            stable_cause="gate three artifact failure",
        )
    )
    for gate in (4, 5):
        trace.push(
            compile_gate_blocked(
                trace.ctx(),
                gate=gate,
                red_predecessor_gate=3,
            )
        )
    assert validate_execution_telemetry(trace.events).ok
    projection = project_execution_telemetry(trace.events)
    assert projection.gates[2].state == "failed"


def test_gate_four_certifying_e2e_carries_repetition_and_diagnostics_stay_separate() -> None:
    scenario = _portable()
    trace = _Trace()
    _admit(scenario, trace)
    for gate in (1, 2, 3, 4):
        _run_gate(
            trace,
            scenario,
            gate,
            _GateRun(green_prefix=_synthetic_prefix(gate)),
        )
        trace.push(
            compile_gate_pass_reused(
                trace.ctx(),
                prior_certificate=_synthetic_certificate(scenario, gate),
                citation=GateCitation(
                    attempt=1,
                    catalog_revision=1,
                    catalog_manifest_id=_catalog_id(trace, gate),
                ),
                dependency_decision=_decision(scenario, (), final=gate == 4),
            )
        )
    gate_four_rail_started = [
        event for event in trace.events if event.eventKind == "rail-started" and event.gate == 4
    ]
    assert gate_four_rail_started
    for event in gate_four_rail_started:
        payload = event.payload
        assert isinstance(payload, RailStartedPayload)
        assert payload.repetition == 2
    assert all(
        isinstance(event.payload, RailStartedPayload) and event.payload.certifying is True
        for event in gate_four_rail_started
    )
    assert validate_execution_telemetry(trace.events).ok
    diagnostic = _Trace()
    diagnostic.push(
        compile_diagnostic_started(
            _diagnostic_ctx(diagnostic),
            plan_digest=_DIGEST,
            plan_version="scenario-2.1.0",
            rail_count=2,
        )
    )
    diagnostic.push(
        compile_diagnostic_terminal(
            _diagnostic_ctx(diagnostic),
            result_id=_DIGEST,
            disposition="aborted",
        )
    )
    assert validate_execution_telemetry(diagnostic.events).ok
    diag_projection = project_execution_telemetry(diagnostic.events)
    assert diag_projection.diagnostics[0].disposition == "aborted"
    assert diag_projection.gates[0].state == "not-started"
    assert diag_projection.operationKind is None


def test_memory_only_gate_five_reuse_invalidates_then_reuses() -> None:
    scenario = _certificate_scenario()
    memory_inputs = _memory_inputs()
    chain = _certify_through(scenario, 5, memory_inputs=memory_inputs)
    trace = _Trace(profile_id=_PROFILE_TARGETED, candidate=_GIT_CANDIDATE)
    _admit(scenario, trace)
    for gate in _GATE_IDS:
        _run_gate(
            trace,
            scenario,
            gate,
            _GateRun(green_prefix=tuple(item.identity for item in chain[: gate - 1])),
        )
        trace.push(
            compile_gate_pass_published(
                trace.ctx(),
                certificate=chain[gate - 1],
                citation=GateCitation(
                    attempt=1,
                    catalog_revision=1,
                    catalog_manifest_id=_catalog_id(trace, gate),
                ),
                dependency_decision=_decision(scenario, chain[: gate - 1], final=gate == 5),
            )
        )
    gate_five_catalog = _catalog_id(trace, 5)
    changes = (
        CertificateInputChange(
            changeClass="memory-onboarding",
            reason="memory onboarding moved",
        ),
    )
    decision = classify_certificate_invalidation(changes)
    assert decision.invalidatedGates == (5,)
    trace.push(
        compile_certificate_invalidated(
            trace.ctx(),
            certificate=chain[4],
            citation=GateCitation(
                attempt=1,
                catalog_revision=1,
                catalog_manifest_id=gate_five_catalog,
            ),
            decision=decision,
            changes=changes,
        )
    )
    assert scenario.admission is not None
    reuse = plan_certificate_reuse(
        scenario.admission,
        chain,
        changes,
        gate_five_inputs=memory_inputs,
    )
    assert reuse.firstGateToRun == 5
    assert reuse.zeroGateStarts is False
    assert validate_execution_telemetry(trace.events).ok
    reused_decision = compile_reuse_dependency_decision(reuse)
    _run_gate(
        trace,
        scenario,
        5,
        _GateRun(
            attempt=2,
            catalog_revision=2,
            green_prefix=tuple(item.identity for item in chain[:4]),
        ),
    )
    trace.push(
        compile_gate_pass_reused(
            trace.ctx(),
            prior_certificate=chain[4],
            citation=GateCitation(
                attempt=2,
                catalog_revision=2,
                catalog_manifest_id=_catalog_id(trace, 5),
            ),
            dependency_decision=reused_decision,
        )
    )
    assert validate_execution_telemetry(trace.events).ok
    projection = project_execution_telemetry(trace.events)
    assert projection.gates[4].state == "reused"
    assert projection.gates[4].certificateId == chain[4].certificateDigest
    assert projection.gates[4].certificateDisposition == "reused"


def test_code_invalidation_records_changed_input_and_full_closure() -> None:
    scenario = _certificate_scenario()
    chain = _certify_through(scenario, 1)
    trace = _Trace(profile_id=_PROFILE_TARGETED, candidate=_GIT_CANDIDATE)
    _admit(scenario, trace)
    _run_gate(trace, scenario, 1)
    trace.push(
        compile_gate_pass_published(
            trace.ctx(),
            certificate=chain[0],
            citation=GateCitation(
                attempt=1,
                catalog_revision=1,
                catalog_manifest_id=_catalog_id(trace, 1),
            ),
            dependency_decision=_decision(scenario, (), final=True),
        )
    )
    decision = classify_certificate_invalidation(
        (
            CertificateInputChange(
                changeClass="code",
                reason="candidate code tree changed",
            ),
        )
    )
    assert decision.invalidatedGates == (1, 2, 3, 4, 5)
    trace.push(
        compile_certificate_invalidated(
            trace.ctx(),
            certificate=chain[0],
            citation=GateCitation(
                attempt=1,
                catalog_revision=1,
                catalog_manifest_id=_catalog_id(trace, 1),
            ),
            decision=decision,
            changes=(
                CertificateInputChange(
                    changeClass="code",
                    reason="candidate code tree changed",
                ),
            ),
        )
    )
    assert validate_execution_telemetry(trace.events).ok
    projection = project_execution_telemetry(trace.events)
    assert projection.gates[0].state == "invalidated"
    assert projection.gates[0].certificateDisposition == "invalidated"


def test_finalization_resume_trace_projects_boundary_and_terminal() -> None:
    scenario = _certificate_scenario()
    memory_inputs = _memory_inputs()
    chain = _certify_through(scenario, 5, memory_inputs=memory_inputs)
    assert scenario.admission is not None
    authority = compile_finalization_authority(
        scenario.admission,
        chain,
        FinalizationCurrentInputs(
            gateFiveInputs=memory_inputs,
            taskIntentAuthorityDigest=_DIGEST,
            journalAuthorityDigest=_DIGEST,
        ),
        _provenance(),
    )
    trace = _Trace(profile_id=_PROFILE_TARGETED, candidate=_GIT_CANDIDATE)
    _admit(scenario, trace)
    for gate in _GATE_IDS:
        _run_gate(
            trace,
            scenario,
            gate,
            _GateRun(green_prefix=tuple(item.identity for item in chain[: gate - 1])),
        )
        trace.push(
            compile_gate_pass_published(
                trace.ctx(),
                certificate=chain[gate - 1],
                citation=GateCitation(
                    attempt=1,
                    catalog_revision=1,
                    catalog_manifest_id=_catalog_id(trace, gate),
                ),
                dependency_decision=_decision(scenario, chain[: gate - 1], final=gate == 5),
            )
        )
    trace.push(
        compile_finalization_started(
            trace.ctx(),
            gate_five_certificate=chain[4],
            authority=authority,
        )
    )
    trace.push(
        compile_finalization_boundary_resumed(
            trace.ctx(),
            journal_leg="code-commit",
            journal_state_digest=_DIGEST,
            predecessor_revision=len(trace.events),
        )
    )
    trace.push(
        compile_finalization_completed(
            trace.ctx(),
            authority=authority,
            finalization_digest=authority.authorityDigest,
        )
    )
    trace.push(
        compile_operation_terminal(
            trace.ctx(),
            terminal_id=content_digest({"terminal": "success"}),
            terminal_result_class="success",
        )
    )
    assert validate_execution_telemetry(trace.events).ok
    projection = project_execution_telemetry(trace.events)
    assert projection.boundary.finalizationState == "completed"
    assert projection.boundary.finalizationAuthorityDigest == authority.authorityDigest
    assert projection.operationTerminal is not None
    assert projection.operationTerminal.terminalResultClass == "success"
    assert projection.gates[4].state == "passed"


def test_operation_terminal_gate_result_class_binds_available_manifest() -> None:
    scenario = _portable()
    trace = _Trace()
    _admit(scenario, trace)
    _run_gate(trace, scenario, 1)
    manifest_id = _catalog_id(trace, 1)
    trace.push(
        compile_gate_pass_reused(
            trace.ctx(),
            prior_certificate=_synthetic_certificate(scenario, 1),
            citation=GateCitation(
                attempt=1,
                catalog_revision=1,
                catalog_manifest_id=manifest_id,
            ),
            dependency_decision=_decision(scenario, (), final=True),
        )
    )
    trace.push(
        compile_operation_terminal(
            trace.ctx(),
            terminal_id=_DIGEST,
            terminal_result_class="gate-result",
            gate_result_manifest_id=manifest_id,
        )
    )
    assert validate_execution_telemetry(trace.events).ok
    projection = project_execution_telemetry(trace.events)
    assert projection.operationTerminal is not None
    assert projection.operationTerminal.terminalResultClass == "gate-result"


def test_two_repository_profiles_produce_the_same_generic_schema() -> None:
    registry = canonicalize_registry(_portable_registry())
    plan_ci = _portable_plan(registry.registry)
    extended_rails = tuple(
        rail.model_copy(
            update={
                "applicability": (
                    *rail.applicability,
                    RailApplicability(
                        profileId="portable-full",
                        status="applicable",
                        selectionIdentity=rail.applicability[0].selectionIdentity,
                        population=rail.applicability[0].population,
                    ),
                )
            }
        )
        for rail in registry.registry.rails
    )
    full_registry = canonicalize_registry(
        registry.registry.model_copy(
            update={
                "profiles": (
                    *registry.registry.profiles,
                    type(registry.registry.profiles[0])(
                        profileId="portable-full",
                        kind="certifying",
                        gates=(1, 2, 3, 4, 5),
                    ),
                ),
                "rails": extended_rails,
            }
        )
    )
    plan_full = compile_certification_plan(
        full_registry,
        profile_id="portable-full",
        candidate_identity=_CANDIDATE,
    )
    ctx_ci = TelemetryExecutionContext(
        executionKind="closeout-generation",
        executionId="gen-13-closeout",
        eventRevision=1,
        operationKind="closeout",
        generation=13,
        candidate=_CANDIDATE,
        profileId=_PROFILE_CI,
        occurredAt=_TIMESTAMP,
    )
    ctx_full = TelemetryExecutionContext(
        executionKind="closeout-generation",
        executionId="gen-13-closeout",
        eventRevision=1,
        operationKind="closeout",
        generation=13,
        candidate=_CANDIDATE,
        profileId="portable-full",
        occurredAt=_TIMESTAMP,
    )
    event_ci = compile_gate_started(
        ctx_ci,
        gate_plan=next(item for item in plan_ci.gates if item.gate == 1),
        attempt=1,
        green_predecessors=[],
    )
    event_full = compile_gate_started(
        ctx_full,
        gate_plan=next(item for item in plan_full.gates if item.gate == 1),
        attempt=1,
        green_predecessors=[],
    )
    assert event_ci.schemaVersion == event_full.schemaVersion == "closeout-telemetry-event/v1"
    assert event_ci.eventKind == event_full.eventKind == "gate-started"
    assert type(event_ci.payload) is type(event_full.payload)
    assert sorted(event_ci.model_dump(mode="json")) == sorted(event_full.model_dump(mode="json"))
    assert event_full.profileId == "portable-full"
