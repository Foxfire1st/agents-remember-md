"""Standalone CCR-R14 run-control tests: two-fresh no-retry certifying runs.

Covers admission ordering (R12 Gates 1-3 green first against the exact frozen
plan), the trusted R12 authority freeze with live-owner registration, two fresh
certifying repetitions with distinct client/process identities and retryCount
zero, no-compensation red aggregates, typed hard failures, teardown and
process-cleanliness records, abort, exact-owner release, in-flight and
authority-transition refusals, and the retry-disabled same-plan barrier.
Every scenario runs zero Dagger commands: engine inspection is a fake and the
authority registry is a temporary directory.  Fully standalone.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path
from typing import Literal

import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.certification.final_codex.projection import (
    project_final_codex_lane,
)
from agents_remember.certification.models import (
    GatePlan,
    RailEvidenceReference,
    RailTerminalObservation,
)
from agents_remember.errors import CertificationContractError
from agents_remember.worktrees.modules.quality import dagger_authority as authority
from agents_remember.worktrees.modules.quality.final_codex_executor import (
    FinalCodexAdmissionRefused,
    FinalCodexEngineOptions,
    FinalCodexExecutionEngine,
    FinalCodexHardFailure,
    FinalCodexRunOptions,
    FinalCodexScenarioEvidence,
    build_final_codex_run_spec,
)
from test_final_codex_models import (
    CANDIDATE,
    DECLARATION_V1,
    OTHER_ENGINE_DECLARATION_V1,
    SCENARIO_VERSION,
    FakeInspector,
    certifying_plan,
    engine_environ,
    make_store,
    manifest_for,
    scenario_registry,
    store_codes,
)

NOW = "2026-09-04T12:00:00+00:00"


class RecordingRunner:
    def __init__(
        self,
        *,
        outcome: Literal["pass", "fail"] = "pass",
        hard: FinalCodexHardFailure | None = None,
    ) -> None:
        self.outcome = outcome
        self.hard = hard
        self.calls = 0
        self.last_environment: dict[str, str] = {}
        self.last_snapshot_digest = ""
        self.identities: list[str] = []

    def run_once(self, *, gate_plan, environment, snapshot, repetitionIdentity):
        self._record_call(environment, snapshot, repetitionIdentity)
        if self.hard is not None:
            raise self.hard
        return self._evidence(gate_plan, self.outcome)

    def _record_call(self, environment, snapshot, repetitionIdentity) -> None:
        self.calls += 1
        self.last_environment = dict(environment)
        self.last_snapshot_digest = snapshot.snapshot_digest
        self.identities.append(repetitionIdentity.clientIdentity)

    def _evidence(self, gate_plan: GatePlan, outcome: str) -> FinalCodexScenarioEvidence:
        observations = []
        for rail in gate_plan.rails:
            status = "pass"
            if outcome == "fail" and rail.posture == "enforcing":
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
        return FinalCodexScenarioEvidence(
            checkpointObservations=tuple(observations),
            teardownEvidence=teardown_evidence,
            processClean=True,
        )


class ScriptedRunner(RecordingRunner):
    """One fresh repetition per outcome in the supplied sequence."""

    def __init__(self, *outcomes: Literal["pass", "fail"]) -> None:
        super().__init__(outcome="pass")
        self.outcomes: tuple[Literal["pass", "fail"], ...] = tuple(outcomes)
        self.hard = None

    def run_once(self, *, gate_plan, environment, snapshot, repetitionIdentity):
        self._record_call(environment, snapshot, repetitionIdentity)
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        return self._evidence(gate_plan, outcome)
        self.calls += 1
        self.last_environment = dict(environment)
        self.last_snapshot_digest = snapshot.snapshot_digest
        self.identities.append(repetitionIdentity.clientIdentity)
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
        return FinalCodexScenarioEvidence(
            checkpointObservations=tuple(observations),
            teardownEvidence=teardown_evidence,
            processClean=True,
        )


def hard_failure(failure_class: Literal["infrastructure", "parser"]) -> FinalCodexHardFailure:
    return FinalCodexHardFailure(
        "runner infrastructure failure",
        failure=__import__(
            "agents_remember.certification.final_codex.models",
            fromlist=["FinalCodexFailureRecord"],
        ).FinalCodexFailureRecord(
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
        process_clean=False,
    )


def make_engine(tmp_path: Path):
    store = make_store(tmp_path)
    registry = authority.AuthorityRegistry(tmp_path / "authority")
    nonces = iter(f"nonce-{index:08d}" for index in itertools.count(1))
    options = FinalCodexEngineOptions(
        environ=engine_environ(DECLARATION_V1),
        inspector=FakeInspector(),
        clock=lambda: NOW,
        nonce_source=lambda: next(nonces),
    )
    engine = FinalCodexExecutionEngine(store=store, registry=registry, options=options)
    return engine, registry, store


def make_spec():
    registry = scenario_registry()
    certifying = certifying_plan(registry)
    return build_final_codex_run_spec(
        registry,
        certifying_plan=certifying,
        candidate_identity=CANDIDATE,
        options=FinalCodexRunOptions(
            environment_identity="codex-host",
            scenario_version=SCENARIO_VERSION,
            plan_version="1.0.0",
        ),
    )


def green_gates():
    registry = scenario_registry()
    certifying = certifying_plan(registry)
    return tuple(manifest_for(registry, certifying, gate) for gate in (1, 2, 3))


class FinalCodexExecutorAdmissionTests:
    def test_admission_runs_only_after_gates_1_3_are_green(self, tmp_path: Path) -> None:
        engine, _, _ = make_engine(tmp_path)
        spec = make_spec()
        registry = scenario_registry()
        certifying = certifying_plan(registry)
        red_three = manifest_for(registry, certifying, 3, red=True)
        for manifests, expected in (
            ((red_three,), "final-codex-prerequisite-incomplete"),
            (
                (
                    *(manifest_for(registry, certifying, gate) for gate in (1, 2)),
                    red_three,
                ),
                "final-codex-prerequisite-not-green",
            ),
        ):
            with pytest.raises(CertificationContractError) as error:
                engine.admit(spec, manifests)
            assert expected in store_codes(error.value)

    def test_missing_host_declaration_is_a_typed_admission_refusal(self, tmp_path: Path) -> None:
        environ = {
            key: value for key, value in os.environ.items() if key != authority.HOST_DECLARATION_ENV
        }
        store = make_store(tmp_path)
        registry = authority.AuthorityRegistry(tmp_path / "authority")
        options = FinalCodexEngineOptions(
            environ=environ,
            inspector=FakeInspector(),
            clock=lambda: NOW,
            nonce_source=lambda: "nonce-entropy",
        )
        engine = FinalCodexExecutionEngine(store=store, registry=registry, options=options)
        spec = make_spec()
        with pytest.raises(FinalCodexAdmissionRefused) as error:
            engine.admit(spec, green_gates())
        assert "runtime-authority-missing" in store_codes(error.value)

    def test_ambient_conflict_is_refused_before_the_scenario_starts(self, tmp_path: Path) -> None:
        conflicting = engine_environ(DECLARATION_V1)
        conflicting[authority.DAGGER_HOST_ENV] = "container://some-other-engine"
        store = make_store(tmp_path)
        registry = authority.AuthorityRegistry(tmp_path / "authority")
        engine = FinalCodexExecutionEngine(
            store=store,
            registry=registry,
            options=FinalCodexEngineOptions(
                environ=conflicting,
                inspector=FakeInspector(),
                clock=lambda: NOW,
                nonce_source=lambda: "nonce-entropy",
            ),
        )
        spec = make_spec()
        with pytest.raises(FinalCodexAdmissionRefused) as error:
            engine.admit(spec, green_gates())
        assert "runtime-authority-ambient-conflict" in store_codes(error.value)

    def test_authority_transition_barrier_blocks_a_fresh_run(self, tmp_path: Path) -> None:
        engine, registry, _ = make_engine(tmp_path)
        spec = make_spec()
        attempt = engine.admit(spec, green_gates())
        barrier_engine, _, _ = _engine_with(tmp_path, OTHER_ENGINE_DECLARATION_V1)
        with pytest.raises(FinalCodexAdmissionRefused) as error:
            barrier_engine.admit(spec, green_gates())
        assert "runtime-authority-transition-barrier" in store_codes(error.value)
        assert registry.census(attempt.snapshot.snapshot_digest) == 1

    def test_in_flight_run_blocks_a_second_attempt_for_the_candidate(self, tmp_path: Path) -> None:
        engine, _, _ = make_engine(tmp_path)
        spec = make_spec()
        engine.admit(spec, green_gates())
        with pytest.raises(CertificationContractError) as error:
            engine.admit(spec, green_gates())
        assert "final-codex-already-in-flight" in store_codes(error.value)

    def test_fresh_admission_releases_its_owner_when_reservation_refused(
        self, tmp_path: Path
    ) -> None:
        engine, registry, _ = make_engine(tmp_path)
        spec = make_spec()
        first = engine.admit(spec, green_gates())
        assert registry.census(first.snapshot.snapshot_digest) == 1
        with pytest.raises(CertificationContractError):
            engine.admit(spec, green_gates())
        assert registry.census(first.snapshot.snapshot_digest) == 1


class FinalCodexExecutorRunTests:
    def test_two_fresh_runs_bind_the_frozen_authority(self, tmp_path: Path) -> None:
        engine, registry, store = make_engine(tmp_path)
        spec = make_spec()
        attempt = engine.admit(spec, green_gates())
        runner = RecordingRunner(outcome="pass")
        manifest = engine.run(attempt, runner)

        assert runner.calls == 2
        assert runner.last_snapshot_digest == attempt.snapshot.snapshot_digest
        assert runner.last_environment[authority.DAGGER_HOST_ENV] == attempt.snapshot.endpoint
        assert runner.last_environment[authority.RUNTIME_AUTHORITY_DIGEST_ENV] == (
            attempt.snapshot.snapshot_digest
        )
        assert manifest.aggregate == "green"
        assert manifest.complete is True
        assert len(manifest.repetitions) == 2
        assert all(item.disposition == "pass" for item in manifest.repetitions)
        assert all(item.retryCount == 0 for item in manifest.repetitions)
        assert len(runner.identities) == 2
        assert len(set(runner.identities)) == 2
        assert all(
            item.runtimeAuthority.snapshotDigest == attempt.snapshot.snapshot_digest
            for item in manifest.repetitions
        )
        assert registry.census(attempt.snapshot.snapshot_digest) == 0
        projection = project_final_codex_lane(store, CANDIDATE)
        assert projection.disposition == "two-fresh-pass"

    def test_one_pass_one_fail_is_red_and_cannot_certify(self, tmp_path: Path) -> None:
        engine, registry, store = make_engine(tmp_path)
        spec = make_spec()
        attempt = engine.admit(spec, green_gates())
        runner = ScriptedRunner("fail", "pass")
        manifest = engine.run(attempt, runner)
        assert manifest.aggregate == "red"
        assert manifest.complete is True
        assert registry.census(attempt.snapshot.snapshot_digest) == 0
        assert project_final_codex_lane(store, CANDIDATE).disposition == "red"

    def test_hard_failure_is_typed_and_still_releases_the_exact_owner(self, tmp_path: Path) -> None:
        spec = make_spec()
        for index, failure_class in enumerate(("infrastructure", "parser")):
            engine, registry, store = make_engine(tmp_path / f"case-{index}")
            attempt = engine.admit(spec, green_gates())
            result = engine.run(attempt, RecordingRunner(hard=hard_failure(failure_class)))
            assert result.complete is True
            assert result.aggregate == "red"
            assert registry.census(attempt.snapshot.snapshot_digest) == 0
            assert project_final_codex_lane(store, CANDIDATE).disposition == "red"

    def test_abort_has_teardown_evidence_no_pass_and_releases_owner(self, tmp_path: Path) -> None:
        engine, registry, store = make_engine(tmp_path)
        spec = make_spec()
        attempt = engine.admit(spec, green_gates())
        manifest = engine.abort(
            attempt,
            teardownEvidence=(
                RailEvidenceReference(
                    evidenceId="teardown",
                    sha256=content_digest({"evidence": "teardown"}),
                    size=16,
                    reference="evidence://teardown",
                ),
            ),
            processClean=False,
        )
        assert manifest.complete is True
        assert manifest.aggregate == "red"
        assert all(item.disposition == "aborted" for item in manifest.repetitions)
        assert all(item.teardown.releasedOwner for item in manifest.repetitions)
        assert all(item.manifest is None for item in manifest.repetitions)
        assert registry.census(attempt.snapshot.snapshot_digest) == 0
        assert project_final_codex_lane(store, CANDIDATE).disposition == "red"

    def test_terminalization_releases_only_the_final_codex_owner(self, tmp_path: Path) -> None:
        engine, registry, _ = make_engine(tmp_path)
        spec = make_spec()
        attempt = engine.admit(spec, green_gates())
        snapshot_digest = attempt.snapshot.snapshot_digest
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
        engine.run(attempt, RecordingRunner(outcome="pass"))
        assert registry.census(snapshot_digest) == 1
        remaining = [r for r in registry.owners() if r.get("authorityDigest") == snapshot_digest]
        assert len(remaining) == 1
        assert remaining[0]["operationKind"] == "closeout"
        assert admitted_foreign.snapshot.layer_store == DECLARATION_V1["layerStore"]

    def test_retry_is_disabled_for_the_exact_same_plan(self, tmp_path: Path) -> None:
        engine, registry, _ = make_engine(tmp_path)
        spec = make_spec()
        first = engine.admit(spec, green_gates())
        engine.run(first, RecordingRunner(outcome="pass"))
        # a terminal run exists for the exact same plan; a fresh run is refused
        with pytest.raises(CertificationContractError) as error:
            engine.admit(spec, green_gates())
        assert "final-codex-retry-disabled" in store_codes(error.value)
        # the refused fresh admission released its newly frozen owner
        assert registry.census(first.snapshot.snapshot_digest) == 0

    def test_no_private_runner_or_store_is_ever_created(self, tmp_path: Path) -> None:
        engine, registry, _ = make_engine(tmp_path)
        spec = make_spec()
        attempt = engine.admit(spec, green_gates())
        assert attempt.snapshot.endpoint == DECLARATION_V1["endpoint"]
        assert attempt.snapshot.layer_store == DECLARATION_V1["layerStore"]
        assert attempt.snapshot.engine_id == "shared-dagger-engine"
        assert registry.census(attempt.snapshot.snapshot_digest) == 1
        engine.run(attempt, RecordingRunner(outcome="pass"))
        assert registry.census(attempt.snapshot.snapshot_digest) == 0


def _engine_with(tmp_path: Path, declaration: dict[str, object]):
    store = make_store(tmp_path)
    registry = authority.AuthorityRegistry(tmp_path / "authority")
    nonces = iter(f"nonce-{index:08d}" for index in itertools.count(1))
    engine = FinalCodexExecutionEngine(
        store=store,
        registry=registry,
        options=FinalCodexEngineOptions(
            environ=engine_environ(declaration),
            inspector=FakeInspector(),
            clock=lambda: NOW,
            nonce_source=lambda: next(nonces),
        ),
    )
    return engine, registry, store
