"""CCR-R16-v3 exhaustive-matrix and cardinality validator contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import pytest
from agents_remember.certification import (
    build_rail_result,
    canonicalize_registry,
    compile_certification_plan,
    compile_gate_result_manifest,
)
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    CertificateInvalidationDecision,
    CertificateReusePlan,
)
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    FinalizationCertificateAuthority,
    GateCertificate,
    GateCertificateIdentity,
)
from agents_remember.certification.lifecycle_models import LifecycleAdmissionManifest
from agents_remember.certification.models import (
    ArtifactDeclaration,
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationContractFinding,
    CertificationPlan,
    GateId,
    GatePlan,
    GateResultAdmission,
    GateResultManifest,
    RailAdapterDefinition,
    RailApplicability,
    RailDefinition,
    RailEvidenceContract,
    RailEvidenceReference,
    RailIdentity,
    RailRegistry,
    RailResult,
    RailRuntimeInputs,
    RailTerminalObservation,
    RegistryProfile,
)
from agents_remember.certification.telemetry import (
    GateCatalogCompletePayload,
    R21DependencyDecision,
    TelemetryReadiness,
    TelemetryValidationReport,
    compile_admission_started,
    compile_candidate_admitted,
    compile_certificate_invalidated,
    compile_certificate_refused,
    compile_diagnostic_started,
    compile_diagnostic_terminal,
    compile_execution_disposition,
    compile_gate_blocked,
    compile_gate_catalog_complete,
    compile_gate_fail,
    compile_gate_pass_published,
    compile_gate_started,
    compile_operation_terminal,
    compile_rail_started,
    compile_rail_terminal,
    compile_reuse_dependency_decision,
    compile_telemetry_readiness,
    validate_execution_telemetry,
)
from agents_remember.certification.telemetry.adapters import TelemetryExecutionContext
from agents_remember.certification.telemetry.models import (
    ExecutionDispositionPayload,
    GateCitation,
    PredecessorBoundary,
    TelemetryEvent,
)

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
_PROFILE = "portable-ci"


@dataclass(frozen=True)
class _RailSpec:
    rail_id: str
    gate: GateId
    prerequisites: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    output_artifacts: tuple[str, ...] = ()


def _identity(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def _rail(spec: _RailSpec) -> RailDefinition:
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
        rails=tuple(_rail(spec) for spec in _portable_specs()),
    )


def _registry() -> RailRegistry:
    return _portable_registry()


def _canonical_registry(registry: RailRegistry | None = None) -> CanonicalRailRegistry:
    return canonicalize_registry(registry or _portable_registry())


def _plan(registry: RailRegistry | None = None) -> CertificationPlan:
    canonical = _canonical_registry(registry)
    return compile_certification_plan(
        canonical,
        profile_id=canonical.registry.profiles[0].profileId,
        candidate_identity=_CANDIDATE,
    )


def _gate(plan: CertificationPlan, gate: int) -> GatePlan:
    return next(item for item in plan.gates if item.gate == gate)


def _manifest(
    plan: CertificationPlan,
    gate_plan: GatePlan,
    results: list[RailResult],
    *,
    altitude: str,
    registry: RailRegistry | None = None,
) -> GateResultManifest:
    canonical = _canonical_registry(registry)
    admission = GateResultAdmission(
        profileId=canonical.registry.profiles[0].profileId,
        candidateIdentity=_CANDIDATE,
        altitude=altitude,  # type: ignore[arg-type]
    )
    return compile_gate_result_manifest(canonical, plan, gate_plan, results, admission)


@dataclass
class _Trace:
    plan: CertificationPlan
    events: list[TelemetryEvent] = field(default_factory=list)

    def ctx(self, kind: str = "closeout-generation") -> TelemetryExecutionContext:
        return TelemetryExecutionContext(
            executionKind=kind,  # type: ignore[arg-type]
            executionId="gen-13-closeout",
            eventRevision=len(self.events) + 1,
            operationKind="closeout" if kind == "closeout-generation" else None,
            generation=13 if kind == "closeout-generation" else None,
            diagnosticNonce=None if kind == "closeout-generation" else "f" * 32,
            candidate=_CANDIDATE,
            profileId=_PROFILE,
            occurredAt=_TIMESTAMP,
        )

    def push(self, event: TelemetryEvent) -> None:
        self.events.append(event)


def _new_trace() -> _Trace:
    return _Trace(plan=_plan(_registry()))


def _admit(trace: _Trace) -> None:
    trace.push(
        compile_admission_started(
            trace.ctx(),
            predecessor=PredecessorBoundary(zeroStart=True),
        )
    )
    trace.push(
        compile_candidate_admitted(
            trace.ctx(),
            lifecycle_admission=_lifecycle_admission(),
            certification_admission=_certification_admission(),
            gate_one_plan=_gate_plan(trace, 1),
        )
    )


def _lifecycle_admission() -> LifecycleAdmissionManifest:
    return cast("LifecycleAdmissionManifest", _stub({"admissionDigest": _DIGEST}))


def _certification_admission() -> CertificationAdmissionManifest:
    return cast(
        "CertificationAdmissionManifest",
        _stub({"admissionDigest": _DIGEST}),
    )


def _stub(attrs: dict[str, object]) -> object:
    return type("Stub", (), attrs)()


def _gate_plan(trace: _Trace, gate: int) -> GatePlan:
    return _gate(trace.plan, gate)  # type: ignore[arg-type]


@dataclass(frozen=True)
class _GateRunOptions:
    """Optional knobs one gate run inside a trace may vary."""

    status: str = "pass"
    attempt: int = 1
    catalog_revision: int = 1
    fail_rails: list[str] | None = None
    blocked_rails: list[str] | None = None
    with_evidence: bool = True
    include_catalog: bool = True


_DEFAULT_GATE_RUN_OPTIONS = _GateRunOptions()


def _run_gate(
    trace: _Trace,
    gate: int,
    options: _GateRunOptions = _DEFAULT_GATE_RUN_OPTIONS,
) -> GateResultManifest | None:
    gate_plan = _gate_plan(trace, gate)
    trace.push(
        compile_gate_started(
            trace.ctx(),
            gate_plan=gate_plan,
            attempt=options.attempt,
            green_predecessors=[],
        )
    )
    statuses = {item: "fail" for item in (options.fail_rails or [])}
    for item in options.blocked_rails or []:
        statuses[item] = "blocked"
    results = []
    for compiled in gate_plan.rails:
        rail_status = statuses.get(compiled.identity.railId, options.status)
        blocked_by = (
            tuple(
                item.identity
                for item in gate_plan.rails
                if any(
                    prerequisite.railId == item.identity.railId
                    for prerequisite in compiled.prerequisites
                )
            )
            if rail_status == "blocked"
            else ()
        )
        observation = RailTerminalObservation(
            rail=compiled.identity,
            status=rail_status,  # type: ignore[arg-type]
            code=f"{compiled.identity.railId}-{rail_status}",
            blockedBy=blocked_by,
            artifacts=(),
            evidence=(
                (
                    RailEvidenceReference(
                        evidenceId=compiled.evidenceContract[0].evidenceId,
                        sha256=_DIGEST,
                        size=16,
                        reference=f"evidence://{compiled.identity.railId}",
                    ),
                )
                if options.with_evidence
                else ()
            ),
        )
        rail_result = build_rail_result(gate_plan, observation)
        results.append(rail_result)
        trace.push(
            compile_rail_started(
                trace.ctx(),
                gate_plan=gate_plan,
                rail=compiled.identity,
                attempt=options.attempt,
                repetition=2 if gate == 4 else None,
            )
        )
        trace.push(
            compile_rail_terminal(
                trace.ctx(),
                gate_plan=gate_plan,
                result=rail_result,
                attempt=options.attempt,
            )
        )
    if not options.include_catalog:
        return None
    manifest = _manifest(trace.plan, gate_plan, results, altitude="certifying")  # type: ignore[arg-type]
    trace.push(
        compile_gate_catalog_complete(
            trace.ctx(),
            manifest=manifest,
            attempt=options.attempt,
            catalog_revision=options.catalog_revision,
        )
    )
    return manifest


def _red_gate_one(trace: _Trace) -> GateResultManifest:
    manifest = _run_gate(
        trace,
        1,
        _GateRunOptions(status="fail", fail_rails=["lint"], blocked_rails=["package"]),
    )
    assert manifest is not None
    return manifest


def _results(gate_plan: GatePlan) -> tuple[RailResult, ...]:
    results = []
    for compiled in gate_plan.rails:
        observation = RailTerminalObservation(
            rail=compiled.identity,
            status="pass",
            code=f"{compiled.identity.railId}-pass",
            blockedBy=(),
            artifacts=(),
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


def _codes(trace: _Trace) -> set[str]:
    return _codes_of(trace.events)


def _codes_of(events: list[TelemetryEvent]) -> set[str]:
    report = validate_execution_telemetry(events)
    return {item.code for item in report.findings}


def test_complete_green_gate_one_trace_is_valid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    report = validate_execution_telemetry(trace.events)
    assert report.ok
    assert report.findings == ()
    assert report.eventCount == len(trace.events)


def test_multi_failure_gate_one_catalog_carries_every_finding() -> None:
    trace = _new_trace()
    _admit(trace)
    catalog = _red_gate_one(trace)
    assert catalog.disposition == "red"
    failures = [item for item in catalog.railResults if item.status in {"fail", "blocked"}]
    assert failures
    assert validate_execution_telemetry(trace.events).ok


def test_generation_thirteen_zero_start_pattern_is_valid() -> None:
    trace = _new_trace()
    _admit(trace)
    _red_gate_one(trace)
    manifest_id = next(
        event.gateResultManifestId
        for event in reversed(trace.events)
        if event.eventKind == "gate-catalog-complete"
    )
    assert manifest_id is not None
    trace.push(
        compile_gate_fail(
            trace.ctx(),
            manifest=cast("GateResultManifest", _gate_plan(trace, 1)),
            citation=GateCitation(
                attempt=1,
                catalog_revision=1,
                catalog_manifest_id=manifest_id,
            ),
            stable_cause="gate-one file-size failure",
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


def test_rail_terminal_without_rail_start_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    removed = [event for event in trace.events if event.eventKind != "rail-started"]
    assert "rail-terminal-without-start" in _codes_of(removed)


def test_duplicate_rail_terminal_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    terminal = next(event for event in reversed(trace.events) if event.eventKind == "rail-terminal")
    trace.push(terminal)
    codes = _codes(trace)
    assert "duplicate-rail-terminal" in codes
    assert "duplicate-catalog-terminal" in codes


def test_rail_pass_without_evidence_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(
        trace,
        1,
        _GateRunOptions(with_evidence=False, include_catalog=False),
    )
    assert "rail-pass-without-evidence" in _codes(trace)


def test_catalog_missing_terminal_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    terminal = next(event for event in reversed(trace.events) if event.eventKind == "rail-terminal")
    removed_index = next(index for index, event in enumerate(trace.events) if event is terminal)
    removed = [event for index, event in enumerate(trace.events) if index != removed_index]
    assert "catalog-terminal-missing" in _codes_of(removed)


def test_catalog_later_terminal_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    gate_plan = _gate_plan(trace, 1)
    trace.push(
        compile_gate_started(trace.ctx(), gate_plan=gate_plan, attempt=1, green_predecessors=[])
    )
    for compiled in gate_plan.rails:
        trace.push(
            compile_rail_started(
                trace.ctx(), gate_plan=gate_plan, rail=compiled.identity, attempt=1
            )
        )
    results = _results(gate_plan)
    manifest = _manifest(trace.plan, gate_plan, results, altitude="certifying")  # type: ignore[arg-type]
    trace.push(
        compile_gate_catalog_complete(trace.ctx(), manifest=manifest, attempt=1, catalog_revision=1)
    )
    for result in results:
        trace.push(
            compile_rail_terminal(trace.ctx(), gate_plan=gate_plan, result=result, attempt=1)
        )
    assert "catalog-later-terminal" in _codes(trace)


def test_catalog_cross_attempt_terminal_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    red_manifest = _run_gate(
        trace,
        1,
        _GateRunOptions(status="fail", fail_rails=["lint"], blocked_rails=["package"]),
    )
    assert red_manifest is not None
    _run_gate(trace, 1, _GateRunOptions(attempt=2, catalog_revision=2))
    gate_plan = _gate_plan(trace, 1)
    trace.push(
        compile_gate_started(
            trace.ctx(),
            gate_plan=gate_plan,
            attempt=3,
            green_predecessors=[],
        )
    )
    for compiled in gate_plan.rails:
        trace.push(
            compile_rail_started(
                trace.ctx(),
                gate_plan=gate_plan,
                rail=compiled.identity,
                attempt=3,
            )
        )
    trace.push(
        compile_gate_catalog_complete(
            trace.ctx(),
            manifest=red_manifest,
            attempt=3,
            catalog_revision=3,
        )
    )
    codes = _codes(trace)
    assert "catalog-cross-attempt-terminal" in codes


def test_catalog_cross_candidate_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    crossed = [
        item.model_copy(update={"candidate": other_candidate()})
        if item.eventKind == "rail-terminal"
        else item
        for item in trace.events
    ]
    assert "catalog-cross-candidate-terminal" in _codes_of(crossed)


def test_catalog_cross_plan_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    crossed = [
        item.model_copy(update={"gatePlanDigest": "b" * 64})
        if item.eventKind == "rail-terminal"
        else item
        for item in trace.events
    ]
    assert "catalog-cross-plan-terminal" in _codes_of(crossed)


def test_catalog_citation_stale_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(
        trace,
        1,
        _GateRunOptions(status="fail", fail_rails=["lint"], blocked_rails=["package"]),
    )
    _run_gate(trace, 1, _GateRunOptions(catalog_revision=2))
    manifest_id = next(
        event.gateResultManifestId
        for event in reversed(trace.events)
        if event.eventKind == "gate-catalog-complete"
    )
    assert manifest_id is not None
    trace.push(
        compile_gate_fail(
            trace.ctx(),
            manifest=cast("GateResultManifest", _gate_plan(trace, 1)),
            citation=GateCitation(
                attempt=1,
                catalog_revision=1,
                catalog_manifest_id=manifest_id,
            ),
            stable_cause="stale citation",
        )
    )
    assert "catalog-citation-stale" in _codes(trace)


def test_gate_blocked_with_started_later_gate_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _red_gate_one(trace)
    trace.push(
        compile_gate_started(
            trace.ctx(),
            gate_plan=_gate_plan(trace, 2),
            attempt=1,
            green_predecessors=(GateCertificateIdentity(gate=1, certificateDigest=_DIGEST),),
        )
    )
    trace.push(
        compile_gate_blocked(
            trace.ctx(),
            gate=2,
            red_predecessor_gate=1,
        )
    )
    assert "blocked-gate-started" in _codes(trace)


def test_gate_blocked_without_red_predecessor_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    trace.push(
        compile_gate_blocked(
            trace.ctx(),
            gate=2,
            red_predecessor_gate=1,
        )
    )
    assert "blocked-gate-red-predecessor-missing" in _codes(trace)


def test_diagnostic_envelope_accepts_only_diagnostic_and_control_events() -> None:
    trace = _Trace(plan=_plan(_registry()))
    trace.push(
        compile_diagnostic_started(
            trace.ctx(kind="diagnostic-run"),
            plan_digest=_DIGEST,
            plan_version="scenario-2.1.0",
            rail_count=2,
        )
    )
    trace.push(
        compile_diagnostic_terminal(
            trace.ctx(kind="diagnostic-run"),
            result_id=_DIGEST,
            disposition="pass",
        )
    )
    trace.push(
        compile_execution_disposition(
            trace.ctx(kind="diagnostic-run"),
            event_kind="execution-cancelled",
            disposition=ExecutionDispositionPayload(
                cause="stop",
                producer="worker",
                evidenceRef="evidence://stop",
                predecessorRevision=2,
            ),
        )
    )
    assert validate_execution_telemetry(trace.events).ok
    with pytest.raises(ValueError):
        compile_gate_blocked(
            trace.ctx(kind="diagnostic-run"),
            gate=2,
            red_predecessor_gate=1,
        )


def test_gate_started_before_admission_is_invalid() -> None:
    trace = _new_trace()
    trace.push(
        compile_gate_started(
            trace.ctx(),
            gate_plan=_gate_plan(trace, 1),
            attempt=1,
            green_predecessors=[],
        )
    )
    assert "gate-started-before-admission" in _codes(trace)


def test_operation_terminal_must_be_final() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    terminal = compile_operation_terminal(
        trace.ctx(),
        terminal_id=_DIGEST,
        terminal_result_class="success",
    )
    trace.events.insert(2, terminal)
    assert "operation-terminal-not-final" in _codes(trace)


def test_duplicate_operation_terminal_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    trace.push(
        compile_operation_terminal(
            trace.ctx(),
            terminal_id=_DIGEST,
            terminal_result_class="success",
        )
    )
    trace.push(
        compile_operation_terminal(
            trace.ctx(),
            terminal_id=_DIGEST,
            terminal_result_class="cancelled",
        )
    )
    assert "duplicate-operation-terminal" in _codes(trace)


def test_cross_generation_identity_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    shifted = [event.model_copy(deep=True) for event in trace.events]
    shifted[0] = shifted[0].model_copy(update={"generation": 14})
    assert "cross-generation-identity" in _codes_of(shifted)


def test_event_revision_gap_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    gap = [event for event in trace.events if event.eventRevision != 2]
    assert "event-revision-gap" in _codes_of(gap)


def test_empty_stream_is_invalid() -> None:
    report = validate_execution_telemetry([])
    assert not report.ok
    assert {item.code for item in report.findings} == {"missing-execution-events"}


def test_telemetry_readiness_is_red_on_invalid_stream() -> None:
    trace = _new_trace()
    trace.push(
        compile_gate_started(
            trace.ctx(),
            gate_plan=_gate_plan(trace, 1),
            attempt=1,
            green_predecessors=[],
        )
    )
    readiness = compile_telemetry_readiness(trace.events)
    assert readiness.state == "red"
    assert readiness.findings
    assert "gate-started-before-admission" in {item.code for item in readiness.findings}


def test_telemetry_readiness_is_green_on_valid_stream() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    readiness = compile_telemetry_readiness(trace.events)
    assert readiness.state == "green"
    assert readiness.findings == ()


def other_candidate() -> object:
    return type("CandidateIdentity", (), {"kind": "git-tree", "value": "d" * 40})()  # type: ignore[return-value]


# --- CCR-R16-v3 residual validation branches (leaf 260831-ccr-l16-ar) ---------


def _revision_ctx(trace: _Trace, revision: int) -> TelemetryExecutionContext:
    del trace
    return TelemetryExecutionContext(
        executionKind="closeout-generation",
        executionId="gen-13-closeout",
        eventRevision=revision,
        operationKind="closeout",
        generation=13,
        candidate=_CANDIDATE,
        profileId=_PROFILE,
        occurredAt=_TIMESTAMP,
    )


def _catalog_id(trace: _Trace, gate: int) -> str:
    for event in reversed(trace.events):
        if event.eventKind == "gate-catalog-complete" and event.gate == gate:
            assert event.gateResultManifestId is not None
            return event.gateResultManifestId
    raise AssertionError(f"no gate {gate} catalog in trace")


def _certificate_stub(gate: int, digest: str) -> GateCertificate:
    return cast(
        "GateCertificate",
        _stub({"semanticEnvelope": _stub({"gate": gate}), "certificateDigest": digest}),
    )


def _authority_stub() -> FinalizationCertificateAuthority:
    envelope = _stub(
        {
            "certificates": tuple(_stub({"certificateDigest": _DIGEST}) for _ in range(5)),
            "candidatePairAuthorityDigest": _DIGEST,
            "taskIntentAuthorityDigest": _DIGEST,
            "journalAuthorityDigest": _DIGEST,
        }
    )
    return cast(
        "FinalizationCertificateAuthority",
        _stub({"semanticEnvelope": envelope, "authorityDigest": _DIGEST}),
    )


def _dependency_decision() -> R21DependencyDecision:
    plan = CertificateReusePlan(
        reusedCertificates=(),
        invalidatedGates=(),
        firstGateToRun=None,
        finalizationRevalidationRequired=False,
        zeroGateStarts=True,
    )
    return compile_reuse_dependency_decision(plan)


def _red_gate_one_run(trace: _Trace) -> None:
    _run_gate(
        trace, 1, _GateRunOptions(status="fail", fail_rails=["lint"], blocked_rails=["package"])
    )


def _invalidation_change() -> CertificateInputChange:
    return CertificateInputChange(changeClass="code", reason="inputs moved")


def _invalidation_decision() -> CertificateInvalidationDecision:
    return cast(
        "CertificateInvalidationDecision",
        _stub(
            {
                "invalidatedGates": (1,),
                "affectedGateFiveSubrecords": (),
                "finalizationRevalidationRequired": False,
            }
        ),
    )


def _tampered_catalog_event(
    trace: _Trace, *, rename_results: bool = False, trim: bool = False
) -> TelemetryEvent:
    catalog_event = next(
        event for event in trace.events if event.eventKind == "gate-catalog-complete"
    )
    payload = catalog_event.payload
    assert isinstance(payload, GateCatalogCompletePayload)
    records = payload.railResults
    if rename_results:
        records = tuple(record.model_copy(update={"resultId": "b" * 64}) for record in records)
    if trim:
        records = records[:1]
    return catalog_event.model_copy(
        update={"payload": payload.model_copy(update={"railResults": records})}
    )


def _swap(trace: _Trace, replacement: TelemetryEvent) -> list[TelemetryEvent]:
    return [
        replacement if event.eventKind == "gate-catalog-complete" else event
        for event in trace.events
    ]


def test_report_and_readiness_reject_inconsistent_state_shape() -> None:
    finding = CertificationContractFinding(code="x", path="execution", detail="boom")
    with pytest.raises(ValueError):
        TelemetryValidationReport(
            executionId="gen-13-closeout", ok=True, eventCount=0, findings=(finding,)
        )
    with pytest.raises(ValueError):
        TelemetryValidationReport(
            executionId="gen-13-closeout", ok=False, eventCount=0, findings=()
        )
    with pytest.raises(ValueError):
        TelemetryReadiness(executionId="gen-13-closeout", state="green", findings=(finding,))
    with pytest.raises(ValueError):
        TelemetryReadiness(executionId="gen-13-closeout", state="red", findings=())


def test_cross_execution_id_and_kind_mixing_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    crossed = [event.model_copy(deep=True) for event in trace.events]
    crossed[2] = crossed[2].model_copy(update={"executionId": "gen-14-closeout"})
    crossed[3] = crossed[3].model_copy(update={"executionKind": "diagnostic-run"})
    codes = _codes_of(crossed)
    assert "cross-execution-id" in codes
    assert "cross-execution-kind" in codes


def test_cross_diagnostic_nonce_is_invalid() -> None:
    trace = _Trace(plan=_plan(_registry()))
    trace.push(
        compile_diagnostic_started(
            trace.ctx(kind="diagnostic-run"),
            plan_digest=_DIGEST,
            plan_version="scenario-2.1.0",
            rail_count=2,
        )
    )
    trace.push(
        compile_diagnostic_terminal(
            trace.ctx(kind="diagnostic-run"), result_id=_DIGEST, disposition="pass"
        )
    )
    mixed = [event.model_copy(deep=True) for event in trace.events]
    mixed[1] = mixed[1].model_copy(update={"diagnosticNonce": "e" * 32})
    assert "cross-diagnostic-nonce" in _codes_of(mixed)


def test_diagnostic_run_cannot_promote_gate_authority() -> None:
    trace = _Trace(plan=_plan(_registry()))
    trace.push(
        compile_diagnostic_started(
            trace.ctx(kind="diagnostic-run"),
            plan_digest=_DIGEST,
            plan_version="scenario-2.1.0",
            rail_count=2,
        )
    )
    promoted = [
        *trace.events,
        compile_gate_started(
            trace.ctx(), gate_plan=_gate_plan(trace, 1), attempt=1, green_predecessors=[]
        ).model_copy(update={"executionKind": "diagnostic-run", "diagnosticNonce": "f" * 32}),
    ]
    assert "diagnostic-authority-promotion" in _codes_of(promoted)


def test_rail_started_without_gate_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    lint = next(
        item.identity for item in _gate_plan(trace, 1).rails if item.identity.railId == "lint"
    )
    trace.push(
        compile_rail_started(trace.ctx(), gate_plan=_gate_plan(trace, 1), rail=lint, attempt=1)
    )
    assert "rail-started-without-gate" in _codes(trace)


def test_rail_terminal_zero_earlier_start_cardinality_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    gate_plan = _gate_plan(trace, 1)
    lint = next(item.identity for item in gate_plan.rails if item.identity.railId == "lint")
    result = build_rail_result(
        gate_plan,
        RailTerminalObservation(
            rail=lint,
            status="pass",
            code="lint-pass",
            blockedBy=(),
            artifacts=(),
            evidence=(
                RailEvidenceReference(
                    evidenceId="lint-evidence", sha256=_DIGEST, size=16, reference="evidence://lint"
                ),
            ),
        ),
    )
    events = [
        compile_gate_started(
            _revision_ctx(trace, 1), gate_plan=gate_plan, attempt=1, green_predecessors=[]
        ),
        compile_rail_started(_revision_ctx(trace, 100), gate_plan=gate_plan, rail=lint, attempt=1),
        compile_rail_terminal(
            _revision_ctx(trace, 2), gate_plan=gate_plan, result=result, attempt=1
        ),
    ]
    assert "rail-terminal-start-cardinality" in _codes_of(events)


def test_catalog_revision_stale_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _red_gate_one_run(trace)
    _run_gate(trace, 1, _GateRunOptions(attempt=2, catalog_revision=1))
    assert "catalog-revision-stale" in _codes(trace)


def test_catalog_terminal_result_id_mismatch_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    assert "terminal-result-mismatch" in _codes_of(
        _swap(trace, _tampered_catalog_event(trace, rename_results=True))
    )


def test_catalog_extra_terminal_is_invalid() -> None:
    trace = _new_trace()
    _admit(trace)
    _run_gate(trace, 1)
    assert "catalog-extra-terminal" in _codes_of(
        _swap(trace, _tampered_catalog_event(trace, trim=True))
    )


def test_gate_decision_disposition_mismatches_are_invalid() -> None:
    red = _new_trace()
    _admit(red)
    _red_gate_one_run(red)
    red.push(
        compile_certificate_refused(
            red.ctx(),
            gate=1,
            refusal_code="stale-result",
            refusal_detail="green refusal on a red catalog",
            citation=GateCitation(
                attempt=1, catalog_revision=1, catalog_manifest_id=_catalog_id(red, 1)
            ),
        )
    )
    green = _new_trace()
    _admit(green)
    _run_gate(green, 1)
    green.push(
        compile_gate_fail(
            green.ctx(),
            manifest=cast("GateResultManifest", _gate_plan(green, 1)),
            citation=GateCitation(
                attempt=1, catalog_revision=1, catalog_manifest_id=_catalog_id(green, 1)
            ),
            stable_cause="red decision on a green catalog",
        )
    )
    assert "catalog-citation-disposition-mismatch" in _codes(red)
    assert "catalog-citation-disposition-mismatch" in _codes(green)


def test_catalog_citation_missing_and_multiple_are_invalid() -> None:
    missing = _new_trace()
    _admit(missing)
    missing.push(
        compile_gate_fail(
            missing.ctx(),
            manifest=cast("GateResultManifest", _gate_plan(missing, 1)),
            citation=GateCitation(attempt=1, catalog_revision=1, catalog_manifest_id=_DIGEST),
            stable_cause="no catalog was ever produced",
        )
    )
    multiple = _new_trace()
    _admit(multiple)
    _run_gate(multiple, 1, _GateRunOptions(attempt=1, catalog_revision=1))
    _run_gate(multiple, 1, _GateRunOptions(attempt=2, catalog_revision=1))
    multiple.push(
        compile_gate_fail(
            multiple.ctx(),
            manifest=cast("GateResultManifest", _gate_plan(multiple, 1)),
            citation=GateCitation(
                attempt=1, catalog_revision=1, catalog_manifest_id=_catalog_id(multiple, 1)
            ),
            stable_cause="two catalogs share the cited revision",
        )
    )
    assert "catalog-citation-missing" in _codes(missing)
    assert "catalog-citation-multiple" in _codes(multiple)


def test_invalidation_prior_certificate_missing_and_manifest_mismatch_are_invalid() -> None:
    missing = _new_trace()
    _admit(missing)
    _run_gate(missing, 1)
    missing.push(
        compile_certificate_invalidated(
            missing.ctx(),
            certificate=_certificate_stub(1, "3" * 64),
            citation=GateCitation(attempt=1, catalog_revision=1, catalog_manifest_id="2" * 64),
            decision=_invalidation_decision(),
            changes=(_invalidation_change(),),
        )
    )
    mismatched = _new_trace()
    _admit(mismatched)
    _run_gate(mismatched, 1)
    cert_id = "1" * 64
    mismatched.push(
        compile_gate_pass_published(
            mismatched.ctx(),
            certificate=_certificate_stub(1, cert_id),
            citation=GateCitation(
                attempt=1, catalog_revision=1, catalog_manifest_id=_catalog_id(mismatched, 1)
            ),
            dependency_decision=_dependency_decision(),
        )
    )
    mismatched.push(
        compile_certificate_invalidated(
            mismatched.ctx(),
            certificate=_certificate_stub(1, cert_id),
            citation=GateCitation(attempt=1, catalog_revision=1, catalog_manifest_id="2" * 64),
            decision=_invalidation_decision(),
            changes=(_invalidation_change(),),
        )
    )
    assert "invalidation-prior-certificate-missing" in _codes(missing)
    assert "invalidation-manifest-mismatch" in _codes(mismatched)
