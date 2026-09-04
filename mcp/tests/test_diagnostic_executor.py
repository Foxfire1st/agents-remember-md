"""Standalone CCR-R13 diagnostic run-control tests.

Covers admission ordering (R12 Gates 1-3 green first), the trusted R12
authority freeze with live-owner registration, one-replication terminalization
(pass/fail/aborted/hard-failure), exact-owner release, in-flight and
authority-transition refusals, frozen-snapshot retry, R16 diagnostic telemetry
envelopes, and the isolation of the diagnostic namespace.  Every scenario runs
zero Dagger commands: engine inspection is a fake and the authority registry is
a temporary directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import pytest
from agents_remember.certification import canonicalize_registry
from agents_remember.certification.certificate_models import CandidateIdentity
from agents_remember.certification.diagnostics.models import DiagnosticFailureRecord
from agents_remember.certification.diagnostics.projection import project_diagnostic_lane
from agents_remember.certification.diagnostics.store import (
    DiagnosticManifestStore,
    DiagnosticStorePolicy,
)
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
from agents_remember.certification.telemetry.models import (
    DiagnosticStartedPayload,
    DiagnosticTerminalPayload,
    TelemetryEvent,
)
from agents_remember.errors import CertificationContractError
from agents_remember.worktrees.modules.quality import dagger_authority as authority
from agents_remember.worktrees.modules.quality.diagnostic_executor import (
    DiagnosticAdmissionRefused,
    DiagnosticEngineOptions,
    DiagnosticExecutionEngine,
    DiagnosticHardFailure,
    DiagnosticRunOptions,
    ScenarioReplicationEvidence,
    build_diagnostic_run_spec,
)

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
NOW = "2026-09-04T12:00:00+00:00"
TERMINAL = "2026-09-04T12:10:00+00:00"
CLASS_BY_GATE: dict[GateId, RailClass] = {
    1: "pre-test-quality",
    2: "ordinary-test-suite",
    3: "post-test-quality",
    4: "integration-test",
    5: "memory-quality",
}

DECLARATION_V1: dict[str, object] = {
    "schemaVersion": "dagger-host-declaration/v1",
    "endpoint": "container://shared-dagger-engine",
    "layerStore": "/var/lib/dagger",
    "engineVersion": "v0.21.8",
}

OTHER_ENGINE_DECLARATION_V1: dict[str, object] = {
    **DECLARATION_V1,
    "endpoint": "container://another-dagger-engine",
}


@dataclass(frozen=True)
class RailSpec:
    rail_id: str
    gate: GateId
    posture: RailPosture = "enforcing"


def _identity(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def _rail(spec: RailSpec) -> RailDefinition:
    rows: list[RailApplicability] = []
    for profile_id in (CERT_PROFILE, DIAG_PROFILE):
        if spec.gate == 5 and profile_id == DIAG_PROFILE:
            continue
        applicable = spec.gate != 5
        rows.append(
            RailApplicability(
                profileId=profile_id,
                status="applicable" if applicable else "not-applicable",
                selectionIdentity=f"selection:{spec.rail_id}",
                population="exact population" if applicable else None,
                reason=None if applicable else "profile excludes gate-5 memory",
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
        applicability=tuple(rows),
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
            rails=tuple(
                _rail(spec)
                for spec in (
                    RailSpec("advisory", 1, posture="report-only"),
                    RailSpec("lint", 1),
                    RailSpec("suite", 2),
                    RailSpec("coverage", 3),
                    RailSpec("e2e", 4),
                    RailSpec("memory", 5),
                )
            ),
        )
    )


def certifying_plan(resolved: CanonicalRailRegistry) -> CertificationPlan:
    return compile_certification_plan(
        resolved,
        profile_id=CERT_PROFILE,
        candidate_identity=CANDIDATE,
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


def green_gates(registry: CanonicalRailRegistry) -> tuple[GateResultManifest, ...]:
    certifying = certifying_plan(registry)
    return tuple(manifest_for(registry, certifying, gate) for gate in (1, 2, 3))


class FakeInspector:
    def __init__(self, *, running: bool = True, store_mounted: bool = True) -> None:
        self.running = running
        self.store_mounted = store_mounted
        self.engine_id = "shared-dagger-engine"

    def inspect(self, declaration: authority.DaggerHostDeclaration):
        if not self.running:
            return authority.InspectedRuntime(
                engine_id=self.engine_id,
                engine_running=False,
                store_mounted_path=declaration.layer_store,
                store_source="/var/lib/docker/volumes/x/_data",
                observed_version=None,
            )
        if not self.store_mounted:
            return authority.InspectedRuntime(
                engine_id=self.engine_id,
                engine_running=True,
                store_mounted_path="/var/lib/dagger-other",
                store_source="/var/lib/docker/volumes/other/_data",
                observed_version="dagger 0.21.8",
            )
        return authority.InspectedRuntime(
            engine_id=self.engine_id,
            engine_running=True,
            store_mounted_path=declaration.layer_store,
            store_source="/var/lib/docker/volumes/shared-layer-store/_data",
            observed_version="dagger 0.21.8",
        )


def engine_environ(declaration: dict[str, object]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in (
            authority.HOST_DECLARATION_ENV,
            authority.HOST_REGISTRY_ROOT_ENV,
            authority.DAGGER_HOST_ENV,
        )
    }
    env[authority.HOST_DECLARATION_ENV] = json.dumps(declaration)
    return env


class RecordingRunner:
    def __init__(
        self,
        *,
        outcome: Literal["pass", "fail"] = "pass",
        hard: DiagnosticHardFailure | None = None,
    ) -> None:
        self.outcome = outcome
        self.hard = hard
        self.calls = 0
        self.last_environment: dict[str, str] = {}
        self.last_snapshot_digest = ""

    def run_once(
        self,
        *,
        gate_plan,
        environment,
        snapshot,
    ) -> ScenarioReplicationEvidence:
        self.calls += 1
        self.last_environment = dict(environment)
        self.last_snapshot_digest = snapshot.snapshot_digest
        if self.hard is not None:
            raise self.hard
        observations = []
        for rail in gate_plan.rails:
            status = "pass"
            if self.outcome == "fail" and rail.posture == "enforcing":
                status = "fail"
            observations.append(
                RailTerminalObservation(
                    rail=rail.identity,
                    status=status,
                    code=f"{rail.identity.railId}-{status}",
                    artifacts=(),
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
            )
        teardown_evidence = (
            RailEvidenceReference(
                evidenceId="teardown",
                sha256=content_digest({"evidence": "teardown"}),
                size=16,
                reference="evidence://teardown",
            ),
        )
        return ScenarioReplicationEvidence(
            checkpointObservations=tuple(observations),
            teardownEvidence=teardown_evidence,
        )


def hard_failure(
    failure_class: Literal["infrastructure", "parser"],
) -> DiagnosticHardFailure:
    return DiagnosticHardFailure(
        "runner infrastructure failure",
        failure=DiagnosticFailureRecord(
            failureClass=failure_class,
            code="strict-runner-failure",
            detail="runner failed before parsing scenario evidence",
            correctiveOwner="dagger-owner",
            evidence=(
                RailEvidenceReference(
                    evidenceId="runner-evidence",
                    sha256=content_digest({"evidence": "runner"}),
                    size=16,
                    reference="evidence://runner",
                ),
            ),
        ),
        teardown_evidence=(
            RailEvidenceReference(
                evidenceId="teardown",
                sha256=content_digest({"evidence": "teardown"}),
                size=16,
                reference="evidence://teardown",
            ),
        ),
    )


def make_engine(
    tmp_path: Path,
    *,
    events: list[TelemetryEvent] | None = None,
    environ: dict[str, str] | None = None,
    inspector: FakeInspector | None = None,
) -> tuple[DiagnosticExecutionEngine, authority.AuthorityRegistry, DiagnosticManifestStore]:
    store = DiagnosticManifestStore(
        tmp_path / "diagnostics",
        DiagnosticStorePolicy(
            storeId="diagnostic",
            forbiddenRoots=(tmp_path / "quality-report-generations",),
        ),
    )
    registry = authority.AuthorityRegistry(tmp_path / "authority")
    options = DiagnosticEngineOptions(
        environ=environ if environ is not None else engine_environ(DECLARATION_V1),
        inspector=inspector or FakeInspector(),
        clock=lambda: NOW,
        nonce_source=lambda: "nonce-entropy",
        on_telemetry=(events.append if events is not None else None),
    )
    engine = DiagnosticExecutionEngine(store=store, registry=registry, options=options)
    return engine, registry, store


def make_spec(registry: CanonicalRailRegistry | None = None):
    resolved = registry or scenario_registry()
    certifying = certifying_plan(resolved)
    return build_diagnostic_run_spec(
        resolved,
        profile_id=DIAG_PROFILE,
        certifying_plan=certifying,
        candidate_identity=CANDIDATE,
        options=DiagnosticRunOptions(environment_identity="codex-host", plan_version="1.0.0"),
    )


def store_codes(error: CertificationContractError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


class DiagnosticExecutorAdmissionTests:
    def test_admission_runs_only_after_gates_1_3_are_green(self, tmp_path: Path) -> None:
        engine, _, _ = make_engine(tmp_path)
        spec = make_spec()
        registry = scenario_registry()
        certifying = certifying_plan(registry)
        red_three = manifest_for(registry, certifying, 3, red=True)
        for manifests, expected in (
            ((red_three,), "diagnostic-prerequisite-incomplete"),
            (
                (
                    *(manifest_for(registry, certifying, gate) for gate in (1, 2)),
                    red_three,
                ),
                "diagnostic-prerequisite-not-green",
            ),
        ):
            with pytest.raises(CertificationContractError) as error:
                engine.admit(spec, manifests)
            assert expected in store_codes(error.value)

    def test_admission_refuses_diagnostic_altitude_green_evidence(self, tmp_path: Path) -> None:
        engine, _, _ = make_engine(tmp_path)
        spec = make_spec()
        registry = scenario_registry()
        diagnostic_gate_one = manifest_for(registry, spec.diagnosticPlan, 1)
        with pytest.raises(CertificationContractError) as error:
            engine.admit(spec, (diagnostic_gate_one,) * 3)
        assert "diagnostic-prerequisite-incomplete" in store_codes(error.value)

    def test_admission_refuses_candidate_mismatch_in_green_evidence(self, tmp_path: Path) -> None:
        engine, _, _ = make_engine(tmp_path)
        spec = make_spec()
        registry = scenario_registry()
        other = CandidateIdentity(kind="git-tree", value="d" * 40)
        other_plan = compile_certification_plan(
            registry,
            profile_id=CERT_PROFILE,
            candidate_identity=other,
        )
        other_manifests = tuple(manifest_for(registry, other_plan, gate) for gate in (1, 2, 3))
        with pytest.raises(CertificationContractError) as error:
            engine.admit(spec, other_manifests)
        assert "diagnostic-prerequisite-candidate-mismatch" in store_codes(error.value)

    def test_missing_host_declaration_is_a_typed_admission_refusal(self, tmp_path: Path) -> None:
        environ = {
            key: value for key, value in os.environ.items() if key != authority.HOST_DECLARATION_ENV
        }
        engine, _, _ = make_engine(tmp_path, environ=environ)
        spec = make_spec()
        green = green_gates(scenario_registry())
        with pytest.raises(DiagnosticAdmissionRefused) as error:
            engine.admit(spec, green)
        assert "runtime-authority-missing" in store_codes(error.value)

    def test_ambient_conflict_is_refused_before_the_scenario_starts(self, tmp_path: Path) -> None:
        conflicting = engine_environ(DECLARATION_V1)
        conflicting[authority.DAGGER_HOST_ENV] = "container://some-other-engine"
        engine, _, _ = make_engine(tmp_path, environ=conflicting)
        spec = make_spec()
        with pytest.raises(DiagnosticAdmissionRefused) as error:
            engine.admit(spec, green_gates(scenario_registry()))
        assert "runtime-authority-ambient-conflict" in store_codes(error.value)

    def test_authority_transition_barrier_blocks_a_fresh_diagnostic(self, tmp_path: Path) -> None:
        engine, registry, _ = make_engine(tmp_path)
        spec = make_spec()
        green = green_gates(scenario_registry())
        attempt = engine.admit(spec, green)
        barrier_engine, _, _ = make_engine(
            tmp_path,
            environ=engine_environ(OTHER_ENGINE_DECLARATION_V1),
        )
        with pytest.raises(DiagnosticAdmissionRefused) as error:
            barrier_engine.admit(spec, green)
        assert "runtime-authority-transition-barrier" in store_codes(error.value)
        findings = list(error.value.findings)
        assert findings[0]["liveOwnerCensus"] == 1
        # the live diagnostic owner still owns the frozen authority
        assert registry.census(attempt.snapshot.snapshot_digest) == 1

    def test_in_flight_diagnostic_blocks_a_second_attempt_for_the_candidate(
        self, tmp_path: Path
    ) -> None:
        engine, _, _ = make_engine(tmp_path)
        spec = make_spec()
        green = green_gates(scenario_registry())
        engine.admit(spec, green)
        with pytest.raises(CertificationContractError) as error:
            engine.admit(spec, green)
        assert "diagnostic-already-in-flight" in store_codes(error.value)

    def test_fresh_diagnostic_releases_its_owner_when_reservation_refused(
        self, tmp_path: Path
    ) -> None:
        engine, registry, _ = make_engine(tmp_path)
        spec = make_spec()
        green = green_gates(scenario_registry())
        first = engine.admit(spec, green)
        assert registry.census(first.snapshot.snapshot_digest) == 1
        # same candidate still live: the second freeze registers its owner, the
        # reservation refuses, and only that fresh owner is released.
        with pytest.raises(CertificationContractError):
            engine.admit(spec, green)
        assert registry.census(first.snapshot.snapshot_digest) == 1


class DiagnosticExecutorRunTests:
    def test_one_pass_replication_binds_the_frozen_authority(self, tmp_path: Path) -> None:
        events: list[TelemetryEvent] = []
        engine, registry, store = make_engine(tmp_path, events=events)
        spec = make_spec()
        green = green_gates(scenario_registry())
        attempt = engine.admit(spec, green)
        runner = RecordingRunner(outcome="pass")
        result = engine.run(attempt, runner)

        assert runner.calls == 1
        assert runner.last_snapshot_digest == attempt.snapshot.snapshot_digest
        assert runner.last_environment[authority.DAGGER_HOST_ENV] == attempt.snapshot.endpoint
        assert runner.last_environment[authority.RUNTIME_AUTHORITY_DIGEST_ENV] == (
            attempt.snapshot.snapshot_digest
        )
        assert result.disposition == "pass"
        assert result.certifying is False
        assert result.acceptanceEligible is False
        assert result.runtimeAuthority.snapshotDigest == attempt.snapshot.snapshot_digest
        assert result.runtimeAuthority.endpoint == DECLARATION_V1["endpoint"]
        assert result.runtimeAuthority.engineId == "shared-dagger-engine"
        assert result.runtimeAuthority.layerStore == DECLARATION_V1["layerStore"]
        assert result.manifest is not None
        assert result.manifest.disposition == "green"
        assert result.teardown.releasedOwner is True
        # exact-owner release: the diagnostic is gone from the census
        assert registry.census(attempt.snapshot.snapshot_digest) == 0
        # one stable manifest, newest terminal selected
        assert store.newest_terminal(CANDIDATE) == result

        # R16 diagnostic-run telemetry: started + terminal, same nonce, no
        # operation kind or closeout generation ever appears.
        assert [event.executionKind for event in events] == ["diagnostic-run", "diagnostic-run"]
        assert [event.eventRevision for event in events] == [1, 2]
        assert all(event.diagnosticNonce == attempt.record.diagnosticNonce for event in events)
        assert all(event.operationKind is None for event in events)
        assert all(event.generation is None for event in events)
        started = cast(DiagnosticStartedPayload, events[0].payload)
        terminal = cast(DiagnosticTerminalPayload, events[1].payload)
        assert started.kind == "diagnostic-started"
        assert started.railCount == len(spec.gatePlan.rails)
        assert terminal.kind == "diagnostic-terminal"
        assert terminal.resultId == result.resultId
        assert terminal.disposition == "pass"

    def test_scenario_failure_is_non_certifying_and_blocks_the_lane(self, tmp_path: Path) -> None:
        engine, registry, store = make_engine(tmp_path)
        spec = make_spec()
        attempt = engine.admit(spec, green_gates(scenario_registry()))
        result = engine.run(attempt, RecordingRunner(outcome="fail"))
        assert result.disposition == "fail"
        assert result.manifest is not None
        assert result.manifest.disposition == "red"
        assert result.failure is not None
        assert result.failure.failureClass == "scenario"
        assert result.failure.rail is not None
        assert result.failure.rail.railId == "e2e"
        assert result.failure.correctiveOwner == "portable-owner"
        assert registry.census(attempt.snapshot.snapshot_digest) == 0
        projection = project_diagnostic_lane(store, CANDIDATE)
        assert projection.blockingCertification is True
        assert projection.disposition == "fail"

    def test_hard_failures_are_typed_and_still_release_the_exact_owner(
        self, tmp_path: Path
    ) -> None:
        spec = make_spec()
        for failure_class in ("infrastructure", "parser"):
            events: list[TelemetryEvent] = []
            engine_two, registry_two, store_two = make_engine(tmp_path, events=events)
            attempt = engine_two.admit(spec, green_gates(scenario_registry()))
            result = engine_two.run(
                attempt,
                RecordingRunner(hard=hard_failure(failure_class)),
            )
            assert result.disposition == "hard-failure"
            assert result.manifest is None
            assert result.failure is not None
            assert result.failure.failureClass == failure_class
            assert result.teardown.releasedOwner is True
            assert registry_two.census(attempt.snapshot.snapshot_digest) == 0
            assert store_two.newest_terminal(CANDIDATE) == result
            assert project_diagnostic_lane(store_two, CANDIDATE).blockingCertification is True
            terminal = cast(DiagnosticTerminalPayload, events[-1].payload)
            assert terminal.disposition == "fail"

    def test_abort_has_teardown_evidence_no_pass_and_blocks(self, tmp_path: Path) -> None:
        engine, registry, store = make_engine(tmp_path)
        spec = make_spec()
        attempt = engine.admit(spec, green_gates(scenario_registry()))
        result = engine.abort(
            attempt,
            teardownEvidence=(
                RailEvidenceReference(
                    evidenceId="teardown",
                    sha256=content_digest({"evidence": "teardown"}),
                    size=16,
                    reference="evidence://teardown",
                ),
            ),
        )
        assert result.disposition == "aborted"
        assert result.manifest is None
        assert result.failure is None
        assert result.teardown.releasedOwner is True
        assert result.teardown.evidence
        assert registry.census(attempt.snapshot.snapshot_digest) == 0
        projection = project_diagnostic_lane(store, CANDIDATE)
        assert projection.blockingCertification is True
        # abort creates no closeout generation and no delivery identity
        rendered = result.model_dump(mode="json")
        assert "generation" not in rendered and "delivery" not in str(rendered)

    def test_one_replication_per_attempt_is_enforced(self, tmp_path: Path) -> None:
        engine, _, _ = make_engine(tmp_path)
        spec = make_spec()
        green = green_gates(scenario_registry())
        attempt = engine.admit(spec, green)
        runner = RecordingRunner(outcome="pass")
        engine.run(attempt, runner)
        with pytest.raises(CertificationContractError) as error:
            engine.run(attempt, runner)
        assert "diagnostic-attempt-not-running" in store_codes(error.value)
        assert runner.calls == 1

    def test_retry_appends_a_new_result_on_the_frozen_snapshot(self, tmp_path: Path) -> None:
        engine, registry, store = make_engine(tmp_path)
        spec = make_spec()
        green = green_gates(scenario_registry())
        failed_attempt = engine.admit(spec, green)
        failed_result = engine.run(failed_attempt, RecordingRunner(outcome="fail"))
        frozen = failed_attempt.snapshot
        assert registry.census(frozen.snapshot_digest) == 0

        events: list[TelemetryEvent] = []
        engine_two, _, _ = make_engine(tmp_path, events=events)
        retried = engine_two.admit(spec, green, continuation_snapshot=frozen)
        assert retried.snapshot.snapshot_digest == frozen.snapshot_digest
        passed_result = engine_two.run(retried, RecordingRunner(outcome="pass"))

        assert failed_result.attemptNumber == 1
        assert passed_result.attemptNumber == 2
        assert passed_result.predecessorDigest == failed_result.resultId
        assert store.newest_terminal(CANDIDATE) == passed_result
        # the original failure is retained immutably; a later pass unblocks
        manifest = store.manifest(CANDIDATE)
        assert manifest is not None
        assert len(manifest.results) == 2
        assert project_diagnostic_lane(store, CANDIDATE).blockingCertification is False
        # the retry consumed the exact originally admitted authority snapshot
        assert passed_result.runtimeAuthority.snapshotDigest == frozen.snapshot_digest

    def test_terminalization_releases_only_the_diagnostic_owner(self, tmp_path: Path) -> None:
        engine, registry, store = make_engine(tmp_path)
        spec = make_spec()
        green = green_gates(scenario_registry())
        attempt = engine.admit(spec, green)
        snapshot_digest = attempt.snapshot.snapshot_digest
        # a foreign closeout operation holds the same frozen authority
        admitted_foreign = authority.admit_dagger_authority(
            environ=engine_environ(DECLARATION_V1),
            inspector=FakeInspector(),
            registry=registry,
            owner_factory=lambda snap: authority.DaggerOwner(
                owner_id=authority.owner_identity(
                    operation_kind="closeout",
                    scope="foreign-scope",
                    generation="gen-7",
                    authority_digest=snap.snapshot_digest,
                ),
                authority_digest=snap.snapshot_digest,
                operation_kind="closeout",
                scope="foreign-scope",
                generation="gen-7",
                pid=os.getpid(),
                process_fingerprint=authority.current_process_fingerprint(),
                started_at=NOW,
            ),
        )
        assert registry.census(snapshot_digest) == 2
        result = engine.run(attempt, RecordingRunner(outcome="pass"))
        assert result.teardown.releasedOwner is True
        # only the diagnostic owner was released; the foreign closeout remains
        # live on the shared runner/store, which is never retired or deleted.
        assert registry.census(snapshot_digest) == 1
        remaining = [r for r in registry.owners() if r.get("authorityDigest") == snapshot_digest]
        assert len(remaining) == 1
        assert remaining[0]["operationKind"] == "closeout"
        # layer store untouched: authority root state only carries owners
        assert admitted_foreign.snapshot.layer_store == DECLARATION_V1["layerStore"]
        assert store.newest_terminal(CANDIDATE) == result

    def test_no_private_runner_or_store_is_ever_created(self, tmp_path: Path) -> None:
        engine, registry, _ = make_engine(tmp_path)
        spec = make_spec()
        attempt = engine.admit(spec, green_gates(scenario_registry()))
        # the admitted snapshot names exactly the declared host engine/store
        assert attempt.snapshot.endpoint == DECLARATION_V1["endpoint"]
        assert attempt.snapshot.layer_store == DECLARATION_V1["layerStore"]
        assert attempt.snapshot.engine_id == "shared-dagger-engine"
        # the diagnostic registered as one live owner of that authority
        assert registry.census(attempt.snapshot.snapshot_digest) == 1
        engine.run(attempt, RecordingRunner(outcome="pass"))
        assert registry.census(attempt.snapshot.snapshot_digest) == 0
        # nothing inside the store or registry root references a private volume
        rendered = json.dumps(registry.state().__dict__, sort_keys=True)
        assert "private" not in rendered
