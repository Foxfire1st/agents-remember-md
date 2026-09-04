"""Standalone CCR-R13 diff-coverage closure tests (run-2 gap).

Closes the run-2 python-diff-coverage gap: model-validator refusal cells,
store CAS/in-flight/state guards, projection disposition cells, planning
admission refusals, and executor helper cells that the primary suites leave
untaken.  Each case exercises one uncovered changed unit with no behavior
change to the implementation.  The module is fully standalone and only reuses
builders from the leaf's own new diagnostic test modules.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import agents_remember.certification.diagnostics.store as store_module
import pytest
from agents_remember.certification import canonicalize_registry
from agents_remember.certification.certificate_models import CandidateIdentity
from agents_remember.certification.diagnostics.models import (
    DiagnosticAttemptRecord,
    DiagnosticEnvironmentBinding,
    DiagnosticRunManifest,
    DiagnosticRunResult,
    DiagnosticRunResultDraft,
    DiagnosticRuntimeAuthorityBinding,
    DiagnosticTeardownRecord,
)
from agents_remember.certification.diagnostics.planning import (
    compile_diagnostic_plan,
    diagnostic_scenario_gate,
    scenario_gate_digest,
)
from agents_remember.certification.diagnostics.projection import (
    DiagnosticLaneDisposition,
    DiagnosticLaneProjection,
    project_diagnostic_lane,
)
from agents_remember.certification.diagnostics.store import (
    DiagnosticManifestStore,
    DiagnosticStorePolicy,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    GateId,
    RailEvidenceReference,
    RailRegistry,
)
from agents_remember.certification.planning import compile_certification_plan
from agents_remember.certification.telemetry.models import DiagnosticTerminalPayload
from agents_remember.errors import CertificationContractError
from agents_remember.worktrees.modules.quality import dagger_authority as authority
from agents_remember.worktrees.modules.quality.diagnostic_executor import (
    DiagnosticEngineOptions,
    DiagnosticExecutionEngine,
    ScenarioReplicationRunner,
    require_gates_one_to_three_green,
)
from pydantic import ValidationError

# The leaf's own new focused test modules provide the reusable scenario builders.
from test_diagnostic_executor import (
    DECLARATION_V1,
    FakeInspector,
    RecordingRunner,
    engine_environ,
    make_engine,
    make_spec,
)
from test_diagnostic_executor import (
    green_gates as executor_green_gates,
)
from test_diagnostic_executor import (
    manifest_for as executor_manifest_for,
)
from test_diagnostic_executor import (
    scenario_registry as executor_registry,
)
from test_diagnostic_models import (
    CANDIDATE,
    authority_binding,
    certifying_plan,
    diagnostic_plan,
    environment_binding,
    manifest_for,
    scenario_registry,
)
from test_diagnostic_models import (
    attempt_record as model_attempt,
)
from test_diagnostic_planning import RailSpec as PlanningRailSpec
from test_diagnostic_planning import _rail as planning_rail
from test_diagnostic_planning import registry as planning_registry
from test_diagnostic_projection import make_lane, publish_terminal, scenario
from test_diagnostic_store import attempt as store_attempt
from test_diagnostic_store import terminal_draft as store_terminal_draft

N1 = "1" * 64
N2 = "2" * 64
N3 = "3" * 64
DIGEST64 = "d" * 64


# ---------------------------------------------------------------------- builders


def terminal_attempt(
    candidate: CandidateIdentity,
    number: int,
    nonce: str,
    *,
    state: str = "terminal",
) -> DiagnosticAttemptRecord:
    payload = {
        "schemaVersion": "diagnostic-attempt/v1",
        "candidateIdentity": candidate.model_dump(mode="json"),
        "attemptNumber": number,
        "diagnosticNonce": nonce,
        "registryDigest": DIGEST64,
        "certifyingPlanDigest": "c" * 64,
        "diagnosticPlanDigest": "d" * 64,
        "planVersion": "1.0.0",
        "gate": 4,
        "profileId": "diagnostic-ci",
        "requestedAt": "2026-09-04T00:00:00Z",
        "state": state,
    }
    return DiagnosticAttemptRecord(**payload, attemptDigest=content_digest(payload))


def _draft_payload(
    candidate: CandidateIdentity,
    number: int,
    nonce: str,
    *,
    disposition: str = "aborted",
) -> dict[str, Any]:
    return {
        "schemaVersion": "diagnostic-run-result/v1",
        "attemptNumber": number,
        "candidateIdentity": candidate.model_dump(mode="json"),
        "registryDigest": DIGEST64,
        "certifyingPlanDigest": "c" * 64,
        "diagnosticPlanDigest": "d" * 64,
        "planVersion": "1.0.0",
        "gate": 4,
        "profileId": "diagnostic-ci",
        "diagnosticNonce": nonce,
        "acceptanceEligible": False,
        "certifying": False,
        "disposition": disposition,
        "failure": None,
        "environment": environment_binding().model_dump(mode="json"),
        "runtimeAuthority": authority_binding().model_dump(mode="json"),
        "manifest": None,
        "teardown": {
            "releasedOwner": True,
            "evidence": [
                {
                    "evidenceId": "teardown",
                    "sha256": content_digest({"evidence": "teardown"}),
                    "size": 16,
                    "reference": "evidence://teardown",
                }
            ],
            "at": "2026-09-04T00:10:00Z",
        },
        "artifacts": [],
        "startedAt": "2026-09-04T00:00:00Z",
        "terminalAt": "2026-09-04T00:10:00Z",
    }


def aborted_result(
    candidate: CandidateIdentity,
    number: int,
    nonce: str,
    *,
    predecessor: str = "",
) -> DiagnosticRunResult:
    payload = {**_draft_payload(candidate, number, nonce), "predecessorDigest": predecessor}
    result_id = content_digest(payload)
    final_payload = {**payload, "resultId": result_id}
    return DiagnosticRunResult(**final_payload, resultDigest=content_digest(final_payload))


def manifest_payload(
    attempts: tuple[DiagnosticAttemptRecord, ...],
    results: tuple[DiagnosticRunResult, ...],
    candidate: CandidateIdentity = CANDIDATE,
) -> dict[str, Any]:
    return {
        "schemaVersion": "diagnostic-run-manifest/v1",
        "candidateIdentity": candidate.model_dump(mode="json"),
        "attempts": [item.model_dump(mode="json") for item in attempts],
        "results": [item.model_dump(mode="json") for item in results],
    }


def build_manifest(
    attempts: tuple[DiagnosticAttemptRecord, ...],
    results: tuple[DiagnosticRunResult, ...],
    candidate: CandidateIdentity = CANDIDATE,
) -> DiagnosticRunManifest:
    payload: dict[str, Any] = manifest_payload(attempts, results, candidate)
    return DiagnosticRunManifest(**payload, manifestDigest=content_digest(payload))


def outcome_payload(manifest: object, *, disposition: str) -> dict[str, Any]:
    """One full outcome draft payload embedding a scenario manifest."""
    payload = _draft_payload(CANDIDATE, 1, N1, disposition=disposition)
    payload["manifest"] = manifest
    return payload


class DiagnosticModelClosureTests:
    def test_semantic_text_must_be_nonblank_and_unpadded(self) -> None:
        with pytest.raises(ValidationError, match="nonblank and unpadded"):
            DiagnosticAttemptRecord.model_validate(
                {**model_attempt().model_dump(mode="json"), "requestedAt": " "}
            )

    def test_owner_release_requires_bounded_teardown_evidence(self) -> None:
        with pytest.raises(ValidationError, match="owner release requires"):
            DiagnosticTeardownRecord(
                releasedOwner=True,
                evidence=(),
                at="2026-09-04T00:10:00Z",
            )

    def test_runtime_authority_binding_digest_mismatch_refused(self) -> None:
        valid = authority_binding()
        with pytest.raises(ValidationError, match="binding digest does not match"):
            DiagnosticRuntimeAuthorityBinding.model_validate(
                {**valid.model_dump(mode="json"), "bindingDigest": "0" * 64}
            )

    def test_environment_binding_digest_mismatch_refused(self) -> None:
        valid = environment_binding()
        with pytest.raises(ValidationError, match="environment binding digest"):
            DiagnosticEnvironmentBinding.model_validate(
                {**valid.model_dump(mode="json"), "environmentDigest": "0" * 64}
            )

    def test_outcome_disposition_must_match_manifest_disposition(self) -> None:
        registry = scenario_registry()
        cert = certifying_plan(registry)
        diag = diagnostic_plan(registry, cert)
        green = manifest_for(registry, diag, 4)
        with pytest.raises(ValidationError, match="outcome disposition must match"):
            DiagnosticRunResultDraft.model_validate(outcome_payload(green, disposition="fail"))

    def test_manifest_must_bind_the_exact_candidate_identity(self) -> None:
        registry = scenario_registry()
        cert = certifying_plan(registry)
        diag = diagnostic_plan(registry, cert)
        other = CandidateIdentity(kind="git-tree", value="e" * 40)
        other_cert = compile_certification_plan(
            registry,
            profile_id=cert.profileId,
            candidate_identity=other,
        )
        other_diag = compile_diagnostic_plan(
            registry,
            profile_id=diag.profileId,
            candidate_identity=other,
            certifying_plan=other_cert,
            gate=4,
        )
        other_green = manifest_for(registry, other_diag, 4)
        payload = _draft_payload(CANDIDATE, 1, N1, disposition="pass")
        payload["manifest"] = other_green
        with pytest.raises(ValidationError, match="exact candidate identity"):
            DiagnosticRunResultDraft.model_validate(payload)


class DiagnosticRunManifestClosureTests:
    def test_attempts_bind_the_manifest_candidate(self) -> None:
        other = CandidateIdentity(kind="git-tree", value="e" * 40)
        payload = manifest_payload(
            (terminal_attempt(CANDIDATE, 1, N1), terminal_attempt(other, 2, N2)),
            (),
        )
        with pytest.raises(ValidationError, match="attempts must bind the manifest candidate"):
            DiagnosticRunManifest(**payload, manifestDigest=content_digest(payload))

    def test_attempt_nonces_must_be_unique(self) -> None:
        payload = manifest_payload(
            (terminal_attempt(CANDIDATE, 1, N1), terminal_attempt(CANDIDATE, 2, N1)),
            (),
        )
        with pytest.raises(ValidationError, match="nonces must be unique"):
            DiagnosticRunManifest(**payload, manifestDigest=content_digest(payload))

    def test_results_are_a_gapless_attempt_number_prefix(self) -> None:
        result_two = aborted_result(CANDIDATE, 2, N2)
        payload = manifest_payload(
            (
                terminal_attempt(CANDIDATE, 1, N1),
                terminal_attempt(CANDIDATE, 2, N2),
            ),
            (result_two,),
        )
        with pytest.raises(ValidationError, match="gapless attempt-number prefix"):
            DiagnosticRunManifest(**payload, manifestDigest=content_digest(payload))

    def test_results_bind_the_manifest_candidate(self) -> None:
        other = CandidateIdentity(kind="git-tree", value="e" * 40)
        first = aborted_result(CANDIDATE, 1, N1)
        second = aborted_result(other, 2, N2, predecessor=first.resultId)
        payload = manifest_payload(
            (
                terminal_attempt(CANDIDATE, 1, N1),
                terminal_attempt(CANDIDATE, 2, N2),
            ),
            (first, second),
        )
        with pytest.raises(ValidationError, match="results must bind the manifest candidate"):
            DiagnosticRunManifest(**payload, manifestDigest=content_digest(payload))

    def test_terminal_result_requires_its_attempt_reservation(self) -> None:
        first = aborted_result(CANDIDATE, 1, N1)
        second = aborted_result(CANDIDATE, 2, N2, predecessor=first.resultId)
        payload = manifest_payload(
            (terminal_attempt(CANDIDATE, 1, N1),),
            (first, second),
        )
        with pytest.raises(ValidationError, match="no matching attempt reservation"):
            DiagnosticRunManifest(**payload, manifestDigest=content_digest(payload))

    def test_terminal_result_requires_its_attempt_slot_terminal(self) -> None:
        first = aborted_result(CANDIDATE, 1, N1)
        second = aborted_result(CANDIDATE, 2, N2, predecessor=first.resultId)
        running_two = terminal_attempt(CANDIDATE, 2, N2, state="running")
        payload = manifest_payload(
            (terminal_attempt(CANDIDATE, 1, N1), running_two),
            (first, second),
        )
        with pytest.raises(ValidationError, match="slot to be terminal"):
            DiagnosticRunManifest(**payload, manifestDigest=content_digest(payload))


class DiagnosticPlanningClosureTests:
    def test_invalid_registry_fails_diagnostic_plan_admission(self) -> None:
        invalid_rails = tuple(
            planning_rail(spec)
            for spec in (
                PlanningRailSpec("lint", 1),
                PlanningRailSpec("coverage", 3),
                PlanningRailSpec("e2e", 4),
                PlanningRailSpec("memory", 5),
            )
        )
        raw = RailRegistry(
            registryId="portable-diagnostics",
            repositoryId="sample-repository",
            profiles=planning_registry().registry.profiles,
            rails=invalid_rails,
        )
        invalid = canonicalize_registry(raw)
        cert = certifying_plan(planning_registry())
        with pytest.raises(CertificationContractError, match="diagnostic plan admission failed"):
            compile_diagnostic_plan(
                invalid,
                profile_id="diagnostic-ci",
                candidate_identity=CANDIDATE,
                certifying_plan=cert,
                gate=4,
            )

    def test_scenario_gate_selection_and_digest_helpers(self) -> None:
        registry = scenario_registry()
        cert = certifying_plan(registry)
        diag = diagnostic_plan(registry, cert)
        gate_digest = scenario_gate_digest(diag, 4)
        gate_plan = diagnostic_scenario_gate(diag, 4)
        assert gate_digest == gate_plan.planDigest
        with pytest.raises(CertificationContractError, match="scenario gate selection failed"):
            diagnostic_scenario_gate(diag, cast(GateId, 9))


class DiagnosticProjectionClosureTests:
    def test_lane_projection_validators_fail_closed(self, tmp_path: Path) -> None:
        store = make_lane(tmp_path)
        failed = publish_terminal(scenario(store), "fail", (1, N1))
        passing = publish_terminal(scenario(store), "pass", (2, N2))
        self._expect_projection_error(
            requested=True,
            disposition="not-requested-optional",
            newest=passing,
            blocking=False,
            match="cannot coexist with any requested evidence",
        )
        self._expect_projection_error(
            requested=True,
            disposition="running",
            newest=passing,
            blocking=False,
            match="running lane",
        )
        self._expect_projection_error(
            requested=True,
            disposition="fail",
            newest=passing,
            blocking=True,
            match="must mirror the newest terminal diagnostic result",
        )
        self._expect_projection_error(
            requested=True,
            disposition="fail",
            newest=failed,
            blocking=False,
            match="blockingCertification must follow",
        )
        # tampering with the projection digest fails closed
        payload = {
            "schemaVersion": "diagnostic-lane-projection/v1",
            "candidateIdentity": CANDIDATE.model_dump(mode="json"),
            "requested": False,
            "disposition": "not-requested-optional",
            "currentForPlan": True,
            "newestResult": None,
            "blockingCertification": False,
        }
        with pytest.raises(ValidationError, match="projection digest does not match"):
            DiagnosticLaneProjection.model_validate(
                {**payload, "projectionDigest": content_digest({"tampered": True})}
            )

    def test_lane_never_returns_optional_once_attempts_exist(self, tmp_path: Path) -> None:
        store = make_lane(tmp_path)
        crafted = build_manifest((terminal_attempt(CANDIDATE, 1, N1),), ())
        store._manifest_path(CANDIDATE).parent.mkdir(parents=True, exist_ok=True)
        store._manifest_path(CANDIDATE).write_text(
            json.dumps(crafted.model_dump(mode="json")),
            encoding="utf-8",
        )
        with pytest.raises(CertificationContractError, match="not-requested-optional was erased"):
            project_diagnostic_lane(store, CANDIDATE)

    def _expect_projection_error(
        self,
        *,
        requested: bool,
        disposition: DiagnosticLaneDisposition,
        newest: DiagnosticRunResult | None,
        blocking: bool,
        match: str,
    ) -> None:
        payload = {
            "schemaVersion": "diagnostic-lane-projection/v1",
            "candidateIdentity": CANDIDATE.model_dump(mode="json"),
            "requested": requested,
            "disposition": disposition,
            "currentForPlan": True,
            "newestResult": newest.model_dump(mode="json") if newest is not None else None,
            "blockingCertification": blocking,
        }
        with pytest.raises(ValidationError, match=match):
            DiagnosticLaneProjection(**payload, projectionDigest=content_digest(payload))


class DiagnosticStoreClosureTests:
    def test_publish_requires_the_running_state(self, tmp_path: Path) -> None:
        lane = _plain_store(tmp_path)
        reservation = store_attempt(1, N1, state="reserved")
        lane.reserve(reservation)
        with pytest.raises(CertificationContractError, match="running attempt may publish"):
            lane.publish_terminal(
                reservation,
                store_terminal_draft(1, N1, "aborted"),
            )

    def test_abandon_refuses_a_non_newest_live_slot(self, tmp_path: Path, monkeypatch) -> None:
        lane = _plain_store(tmp_path)
        first = store_attempt(1, N1, state="running")
        second = store_attempt(2, N2, state="running")
        crafted = build_manifest((first, second), ())
        monkeypatch.setattr(lane, "_read_manifest", lambda candidate: crafted)
        with pytest.raises(
            CertificationContractError, match="newest live attempt slot may be abandoned"
        ):
            lane.abandon(first)

    def test_cas_collision_fails_closed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(store_module, "atomic_write_bytes", lambda path, payload: None)
        lane = _plain_store(tmp_path, retries=2)
        with pytest.raises(
            CertificationContractError, match="did not converge on one stable state"
        ):
            lane.reserve(store_attempt(1, N1))

    def test_non_object_manifest_root_fails_closed(self, tmp_path: Path) -> None:
        lane = _plain_store(tmp_path)
        path = lane._manifest_path(CANDIDATE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(CertificationContractError, match="failed closed revalidation"):
            lane.manifest(CANDIDATE)

    def test_empty_forbidden_root_is_skipped(self, tmp_path: Path) -> None:
        lane = DiagnosticManifestStore(
            tmp_path / "diagnostics",
            DiagnosticStorePolicy(forbiddenRoots=(cast(Path, ""),)),
        )
        lane.reserve(store_attempt(1, N1))
        assert lane.next_attempt_number(CANDIDATE) == 2

    def test_candidate_mismatch_is_refused_on_existing_manifest(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        lane = _plain_store(tmp_path)
        other = CandidateIdentity(kind="git-tree", value="e" * 40)
        crafted = build_manifest((), (), candidate=other)
        monkeypatch.setattr(lane, "_read_manifest", lambda candidate: crafted)
        with pytest.raises(CertificationContractError, match="different exact candidate"):
            lane.reserve(store_attempt(1, N1))

    def test_mark_running_readback_refuses_a_non_running_attempt(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A concurrent writer can only ever interleave between the store's write
        # and its readback; the guard is exercised deterministically here by
        # injecting the readback.
        lane = _plain_store(tmp_path)
        reservation = store_attempt(1, N1, state="reserved")
        crafted = build_manifest((reservation,), ())
        path = lane._manifest_path(CANDIDATE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(crafted.model_dump(mode="json")), encoding="utf-8")
        # the update already ran on the reserved slot; a competing writer could
        # only win between write and readback, so the readback guard is checked
        # against the unchanged on-disk state here.
        monkeypatch.setattr(lane, "_update", lambda candidate, transform: None)
        with pytest.raises(CertificationContractError, match="did not transition to running"):
            lane.mark_running(reservation)

    def test_draft_attempt_binding_mismatch_is_refused(self, tmp_path: Path) -> None:
        lane = _plain_store(tmp_path)
        reservation = store_attempt(1, N1)
        lane.reserve(reservation)
        lane.mark_running(reservation)
        with pytest.raises(
            CertificationContractError, match="must bind the exact attempt reservation identity"
        ):
            lane.publish_terminal(reservation, store_terminal_draft(1, N2, "aborted"))

    def test_illegal_attempt_state_transition_is_refused(self, tmp_path: Path) -> None:
        lane = _plain_store(tmp_path)
        reservation = store_attempt(1, N1)
        lane.reserve(reservation)
        lane.mark_running(reservation)
        lane.publish_terminal(reservation, store_terminal_draft(1, N1, "aborted"))
        with pytest.raises(CertificationContractError, match="cannot move from"):
            lane.mark_running(reservation)


class DiagnosticExecutorClosureTests:
    def test_runner_protocol_has_no_default_runner(self) -> None:
        spec = make_spec()
        run_once = ScenarioReplicationRunner.__dict__["run_once"]
        with pytest.raises(NotImplementedError):
            run_once(
                None,
                gate_plan=spec.gatePlan,
                environment={},
                snapshot=_fake_snapshot(),
            )

    def test_abort_emits_aborted_telemetry_terminal(self, tmp_path: Path) -> None:
        events = []
        engine, _, _ = make_engine(tmp_path, events=events)
        spec = make_spec()
        attempt = engine.admit(spec, executor_green_gates(executor_registry()))
        result = engine.abort(attempt, teardownEvidence=(_evidence("teardown"),))
        assert result.disposition == "aborted"
        terminal = cast(DiagnosticTerminalPayload, events[-1].payload)
        assert terminal.disposition == "aborted"

    def test_public_green_gate_requirement_wrapper(self) -> None:
        require_gates_one_to_three_green(CANDIDATE, executor_green_gates(executor_registry()))

    def test_diagnostic_altitude_evidence_is_refused_as_green(self, tmp_path: Path) -> None:
        engine, _, _ = make_engine(tmp_path)
        spec = make_spec()
        registry = executor_registry()
        altitude_manifests = tuple(
            executor_manifest_for(registry, spec.diagnosticPlan, gate) for gate in (1, 2, 3)
        )
        with pytest.raises(
            CertificationContractError, match="must come from the certifying altitude"
        ):
            engine.admit(spec, altitude_manifests)

    def test_default_clock_and_nonce_sources(self, tmp_path: Path) -> None:
        store = DiagnosticManifestStore(
            tmp_path / "diagnostics",
            DiagnosticStorePolicy(),
        )
        registry = authority.AuthorityRegistry(tmp_path / "authority")
        options = DiagnosticEngineOptions(
            environ=engine_environ(DECLARATION_V1),
            inspector=FakeInspector(),
        )
        engine = DiagnosticExecutionEngine(store=store, registry=registry, options=options)
        spec = make_spec()
        attempt = engine.admit(spec, executor_green_gates(executor_registry()))
        result = engine.run(attempt, RecordingRunner(outcome="pass"))
        assert result.disposition == "pass"
        assert len(attempt.record.diagnosticNonce) == 64


def _plain_store(tmp_path: Path, *, retries: int = 5) -> DiagnosticManifestStore:
    return DiagnosticManifestStore(
        tmp_path / "diagnostics",
        DiagnosticStorePolicy(casRetries=retries),
    )


def _fake_snapshot():
    declaration = authority.DaggerHostDeclaration(
        schema_version="dagger-host-declaration/v1",  # type: ignore[arg-type]
        endpoint="container://shared-dagger-engine",
        layer_store="/var/lib/dagger",
        engine_version="v0.21.8",
        source="test://closure",
    )
    inspected = authority.InspectedRuntime(
        engine_id="shared-dagger-engine",
        engine_running=True,
        store_mounted_path="/var/lib/dagger",
        store_source="/var/lib/docker/volumes/x/_data",
        observed_version="dagger 0.21.8",
    )
    return authority.authority_snapshot(declaration, inspected, inspected_at="2026-09-04")


def _evidence(evidence_id: str) -> RailEvidenceReference:
    return RailEvidenceReference(
        evidenceId=evidence_id,
        sha256=content_digest({"evidence": evidence_id}),
        size=16,
        reference=f"evidence://{evidence_id}",
    )
