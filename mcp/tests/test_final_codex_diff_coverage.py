"""Standalone CCR-R14 run-2 diff-coverage closure tests.

Closes the run-1 python-diff-coverage gap (85.58%) for the changed
final-codex modules: every uncovered line and untaken branch of
certification/final_codex/{models,store,planning,projection,certificate}.py
and worktrees/modules/quality/final_codex_executor.py is exercised here with
real model construction, store round-trips, and engine admissions - never by
changing production code.  The module is fully standalone: it imports only
the package under test, stdlib/pytest, and the leaf-own shared builders that
already live in the leaf's own new test modules.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import agents_remember.certification.final_codex.models as models_module
import agents_remember.certification.final_codex.planning as planning_module
import agents_remember.certification.final_codex.projection as projection_module
import agents_remember.certification.final_codex.store as store_module
import agents_remember.worktrees.modules.quality.final_codex_executor as executor_module
import pytest
from agents_remember.certification.certificate_models import GateCertificateIdentity
from agents_remember.certification.digests import content_digest
from agents_remember.certification.final_codex.certificate import (
    FinalCodexCertificateEnvelope,
    FinalCodexGateFourCertificate,
    compile_gate_four_certificate,
)
from agents_remember.certification.final_codex.models import (
    FinalCodexAttemptRecord,
    FinalCodexEnvironmentBinding,
    FinalCodexFailureRecord,
    FinalCodexRepetitionResult,
    FinalCodexRunManifest,
    FinalCodexTeardownRecord,
)
from agents_remember.certification.final_codex.planning import (
    compile_final_codex_plan_record,
)
from agents_remember.certification.final_codex.projection import FinalCodexLaneProjection
from agents_remember.certification.final_codex.store import (
    FinalCodexManifestStore,
    FinalCodexStorePolicy,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    CanonicalRailRegistry,
    RailEvidenceReference,
    RailRegistry,
    RegistryProfile,
)
from agents_remember.certification.planning import compile_certification_plan
from agents_remember.errors import CertificationContractError
from agents_remember.worktrees.modules.quality.final_codex_executor import (
    FinalCodexRunOptions,
    FinalCodexScenarioRunner,
    build_final_codex_run_spec,
)
from test_final_codex_executor import (
    RecordingRunner,
)
from test_final_codex_executor import (
    green_gates as executor_green_gates,
)
from test_final_codex_executor import (
    make_engine as executor_make_engine,
)
from test_final_codex_executor import (
    make_spec as executor_make_spec,
)
from test_final_codex_models import (
    CANDIDATE,
    CERT_PROFILE,
    OTHER_CANDIDATE,
    SCENARIO_VERSION,
    attempt_record,
    authority_binding,
    certifying_plan,
    environment_binding,
    finalize_result,
    fresh_identities,
    gate4_manifest,
    green_gates,
    make_aborted_draft,
    make_draft,
    make_pass_draft,
    make_store,
    manifest_for,
    plan_record,
    publish_run,
    scenario_failure,
    scenario_registry,
    store_codes,
)

NOW = "2026-09-04T12:00:00+00:00"
TERMINAL = "2026-09-04T12:10:00+00:00"


def teardown_evidence() -> tuple[RailEvidenceReference, ...]:
    return (
        RailEvidenceReference(
            evidenceId="teardown",
            sha256=content_digest({"evidence": "teardown"}),
            size=16,
            reference="evidence://teardown",
        ),
    )


def two_pass_results() -> tuple[FinalCodexRepetitionResult, ...]:
    identities = fresh_identities()
    one = make_draft(
        repetition_number=1,
        identity=identities[0],
        disposition="pass",
        overrides={"manifest": gate4_manifest().model_dump(mode="json")},
    )
    two = make_draft(
        repetition_number=2,
        identity=identities[1],
        disposition="pass",
        overrides={"manifest": gate4_manifest().model_dump(mode="json")},
    )
    return (finalize_result(one), finalize_result(two))


def manifest_dict(
    attempt: FinalCodexAttemptRecord,
    repetitions: tuple[FinalCodexRepetitionResult, ...],
    *,
    candidate: CandidateIdentity | None = None,
) -> dict[str, Any]:
    resolved = candidate if candidate is not None else attempt.candidateIdentity
    return {
        "schemaVersion": "final-codex-run-manifest/v1",
        "candidateIdentity": resolved.model_dump(mode="json"),
        "attempt": attempt.model_dump(mode="json"),
        "repetitions": [item.model_dump(mode="json") for item in repetitions],
        "manifestDigest": "d" * 64,
    }


def green_manifest_dict() -> dict[str, Any]:
    store = make_store(Path(tempfile.mkdtemp(prefix="l14-green-")) / "store")
    identities = fresh_identities()
    attempt = attempt_record(identities=identities)
    store.reserve(attempt)
    running = store.mark_running(attempt)
    store.publish_repetition(running, make_pass_draft(1, identities))
    store.publish_repetition(running, make_pass_draft(2, identities))
    manifest = store.manifest(CANDIDATE)
    assert manifest is not None
    return manifest.model_dump(mode="json")


def rebound_result(
    result: FinalCodexRepetitionResult, **updates: Any
) -> FinalCodexRepetitionResult:
    """Rebind a repetition result after a field mutation keeps its digest exact."""
    payload = result.model_copy(update=updates)
    dumped = payload.model_dump(mode="json", exclude={"resultDigest"})
    return payload.model_copy(update={"resultDigest": content_digest(dumped)})


def predecessor_identities() -> tuple[GateCertificateIdentity, ...]:
    return tuple(
        GateCertificateIdentity(gate=gate, certificateDigest=("abcdef" + "0" * 58)[:64])
        for gate in (1, 2, 3)
    )


class FinalCodexModelRefusalClosureTests:
    def test_semantic_timestamp_rejects_padded_text(self) -> None:
        attempt = attempt_record()
        with pytest.raises(ValueError, match="semantic text must be nonblank and unpadded"):
            FinalCodexAttemptRecord.model_validate(
                {**attempt.model_dump(mode="json"), "requestedAt": "  2026-09-04T12:00:00+00:00  "}
            )

    def test_scenario_failure_requires_the_exact_rail(self) -> None:
        with pytest.raises(ValueError, match="scenario failure must name the exact"):
            FinalCodexFailureRecord.model_validate(
                {
                    "failureClass": "scenario",
                    "code": "boom",
                    "detail": "missing checkpoint rail",
                    "correctiveOwner": "portable-owner",
                    "evidence": [],
                }
            )

    def test_infrastructure_failure_requires_evidence(self) -> None:
        with pytest.raises(ValueError, match="infrastructure and parser failures require"):
            FinalCodexFailureRecord.model_validate(
                {
                    "failureClass": "infrastructure",
                    "code": "boom",
                    "detail": "engine lost",
                    "correctiveOwner": "portable-owner",
                    "evidence": [],
                }
            )

    def test_owner_release_requires_teardown_evidence(self) -> None:
        with pytest.raises(ValueError, match="owner release requires"):
            FinalCodexTeardownRecord.model_validate(
                {
                    "releasedOwner": True,
                    "processClean": True,
                    "evidence": [],
                    "at": TERMINAL,
                }
            )

    def test_process_failure_requires_teardown_evidence(self) -> None:
        with pytest.raises(ValueError, match="process-cleanliness failure requires"):
            FinalCodexTeardownRecord.model_validate(
                {
                    "releasedOwner": False,
                    "processClean": False,
                    "evidence": [],
                    "at": TERMINAL,
                }
            )

    def test_environment_binding_digest_mismatch_refused(self) -> None:
        valid = environment_binding()
        with pytest.raises(ValueError, match="environment binding digest does not match"):
            FinalCodexEnvironmentBinding.model_validate(
                {**valid.model_dump(mode="json"), "environmentDigest": "0" * 64}
            )

    def test_attempt_digest_mismatch_refused(self) -> None:
        attempt = attempt_record()
        with pytest.raises(ValueError, match="attempt digest does not match its content"):
            FinalCodexAttemptRecord.model_validate(
                {**attempt.model_dump(mode="json"), "attemptDigest": "0" * 64}
            )

    # ---------------------------------------------------------- carrier cells

    def test_pass_without_manifest_refused(self) -> None:
        identities = fresh_identities()
        with pytest.raises(ValueError, match="pass and fail outcomes require"):
            make_draft(
                repetition_number=1,
                identity=identities[0],
                disposition="pass",
            )

    def test_pass_carrier_rejects_a_red_manifest(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry)
        red_manifest = manifest_for(registry, plan, 4, red=True)
        identities = fresh_identities()
        with pytest.raises(ValueError, match="outcome disposition must match"):
            make_draft(
                repetition_number=1,
                identity=identities[0],
                disposition="pass",
                overrides={"manifest": red_manifest.model_dump(mode="json")},
            )

    def test_pass_carrier_rejects_diagnostic_altitude(self) -> None:
        identities = fresh_identities()
        draft = make_draft(
            repetition_number=1,
            identity=identities[0],
            disposition="pass",
            overrides={"manifest": gate4_manifest().model_dump(mode="json")},
        )
        diagnostic = gate4_manifest().model_copy(
            update={"altitude": "diagnostic", "profileKind": "diagnostic"}
        )
        foreign = draft.model_copy(update={"manifest": diagnostic})
        with pytest.raises(ValueError, match="certifying-altitude manifests"):
            models_module._require_manifest_carrier(foreign)

    def test_pass_carrier_rejects_a_foreign_gate_manifest(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry)
        gate_three = manifest_for(registry, plan, 3)
        identities = fresh_identities()
        with pytest.raises(ValueError, match="only embed the Gate-4 result manifest"):
            make_draft(
                repetition_number=1,
                identity=identities[0],
                disposition="pass",
                overrides={"manifest": gate_three.model_dump(mode="json")},
            )

    def test_aborted_carrier_never_embeds_a_manifest(self) -> None:
        identities = fresh_identities()
        with pytest.raises(ValueError, match="never embed a result manifest"):
            make_draft(
                repetition_number=1,
                identity=identities[0],
                disposition="aborted",
                overrides={"manifest": gate4_manifest().model_dump(mode="json")},
            )

    def test_fail_carrier_requires_its_exact_scenario_failure(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry)
        red_manifest = manifest_for(registry, plan, 4, red=True)
        identities = fresh_identities()
        with pytest.raises(ValueError, match="requires its exact scenario failure"):
            make_draft(
                repetition_number=1,
                identity=identities[0],
                disposition="fail",
                overrides={"manifest": red_manifest.model_dump(mode="json")},
            )

    def test_hard_failure_carrier_requires_a_typed_failure(self) -> None:
        identities = fresh_identities()
        with pytest.raises(ValueError, match="hard failures require a typed"):
            make_draft(
                repetition_number=1,
                identity=identities[0],
                disposition="hard-failure",
            )

    def test_aborted_carrier_rejects_a_failure_record(self) -> None:
        identities = fresh_identities()
        with pytest.raises(ValueError, match="carry no failure record"):
            make_draft(
                repetition_number=1,
                identity=identities[0],
                disposition="aborted",
                overrides={"failure": scenario_failure().model_dump(mode="json")},
            )

    # ------------------------------------------------ manifest-binding cells

    def test_binding_requires_the_exact_gate(self) -> None:
        identities = fresh_identities()
        draft = make_draft(
            repetition_number=1,
            identity=identities[0],
            disposition="pass",
            overrides={"manifest": gate4_manifest().model_dump(mode="json")},
        )
        registry = scenario_registry()
        plan = certifying_plan(registry)
        foreign = draft.model_copy(update={"manifest": manifest_for(registry, plan, 3)})
        with pytest.raises(ValueError, match="exact Gate-4 plan"):
            models_module._require_manifest_binding(foreign)

    def test_binding_requires_the_exact_candidate(self) -> None:
        identities = fresh_identities()
        draft = make_draft(
            repetition_number=1,
            identity=identities[0],
            disposition="pass",
            overrides={"manifest": gate4_manifest().model_dump(mode="json")},
        )
        foreign_manifest = gate4_manifest().model_copy(
            update={"candidateIdentity": OTHER_CANDIDATE}
        )
        foreign = draft.model_copy(update={"manifest": foreign_manifest})
        with pytest.raises(ValueError, match="exact candidate identity"):
            models_module._require_manifest_binding(foreign)

    def test_binding_requires_the_exact_profile(self) -> None:
        identities = fresh_identities()
        draft = make_draft(
            repetition_number=1,
            identity=identities[0],
            disposition="pass",
            overrides={"manifest": gate4_manifest().model_dump(mode="json")},
        )
        foreign_manifest = gate4_manifest().model_copy(update={"profileId": "other-profile"})
        foreign = draft.model_copy(update={"manifest": foreign_manifest})
        with pytest.raises(ValueError, match="exact repository profile"):
            models_module._require_manifest_binding(foreign)


class FinalCodexRunManifestClosureTests:
    def test_partial_manifest_aggregate_is_red(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "partial")
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        running = store.mark_running(attempt)
        store.publish_repetition(running, make_pass_draft(1, identities))
        manifest = store.manifest(CANDIDATE)
        assert manifest is not None
        assert manifest.complete is False
        assert manifest.aggregate == "red"

    def test_aggregate_reds_a_retried_repetition(self) -> None:
        results = two_pass_results()
        retried = results[0].model_copy(update={"retryCount": 1})
        manifest = FinalCodexRunManifest.model_construct(
            schemaVersion="final-codex-run-manifest/v1",
            candidateIdentity=CANDIDATE,
            attempt=attempt_record(state="terminal", identities=fresh_identities()),
            repetitions=(retried, results[1]),
            manifestDigest="0" * 64,
        )
        assert manifest.aggregate == "red"

    def test_manifest_digest_mismatch_refused(self) -> None:
        payload = green_manifest_dict()
        with pytest.raises(ValueError, match="run manifest digest does not match"):
            FinalCodexRunManifest.model_validate({**payload, "manifestDigest": "0" * 64})

    def test_attempt_must_bind_the_manifest_candidate(self) -> None:
        foreign = attempt_record(candidate=OTHER_CANDIDATE, state="terminal")
        payload = manifest_dict(foreign, two_pass_results(), candidate=CANDIDATE)
        with pytest.raises(ValueError, match="attempt must bind the manifest candidate"):
            FinalCodexRunManifest.model_validate(payload)

    def test_terminal_attempt_requires_both_repetitions(self) -> None:
        terminal = attempt_record(state="terminal")
        payload = manifest_dict(terminal, (two_pass_results()[0],))
        with pytest.raises(ValueError, match="terminal attempt requires both"):
            FinalCodexRunManifest.model_validate(payload)

    def test_reserved_attempt_cannot_carry_results(self) -> None:
        reserved = attempt_record(state="reserved")
        payload = manifest_dict(reserved, two_pass_results())
        with pytest.raises(ValueError, match="reserved attempt cannot carry"):
            FinalCodexRunManifest.model_validate(payload)

    def test_repetitions_must_be_a_gapless_prefix(self) -> None:
        results = two_pass_results()
        reordered = (results[1], results[0])
        payload = manifest_dict(attempt_record(state="terminal"), reordered)
        with pytest.raises(ValueError, match="gapless prefix"):
            FinalCodexRunManifest.model_validate(payload)

    def test_repetition_candidate_mismatch_refused(self) -> None:
        registry = scenario_registry()
        foreign_plan = compile_certification_plan(
            registry,
            profile_id=CERT_PROFILE,
            candidate_identity=OTHER_CANDIDATE,
        )
        foreign_manifest = manifest_for(registry, foreign_plan, 4)
        one, two = two_pass_results()
        foreign = rebound_result(
            one,
            candidateIdentity=OTHER_CANDIDATE,
            manifest=foreign_manifest,
        )
        payload = manifest_dict(attempt_record(state="terminal"), (foreign, two))
        with pytest.raises(ValueError, match="bind the manifest candidate"):
            FinalCodexRunManifest.model_validate(payload)

    def test_repetition_plan_identity_mismatch_refused(self) -> None:
        one = two_pass_results()[0]
        identities = fresh_identities()
        stale = make_draft(
            repetition_number=2,
            identity=identities[1],
            disposition="pass",
            record=plan_record(scenario_version="2.0.0"),
            overrides={"manifest": gate4_manifest().model_dump(mode="json")},
        )
        foreign_two = finalize_result(stale)
        payload = manifest_dict(attempt_record(state="terminal"), (one, foreign_two))
        with pytest.raises(ValueError, match="exact attempt plan identity"):
            FinalCodexRunManifest.model_validate(payload)

    def test_repetition_identity_slot_order_mismatch_refused(self) -> None:
        identities = fresh_identities()
        swapped_one = make_draft(
            repetition_number=1,
            identity=identities[1],
            disposition="pass",
            overrides={"manifest": gate4_manifest().model_dump(mode="json")},
        )
        swapped_two = make_draft(
            repetition_number=2,
            identity=identities[0],
            disposition="pass",
            overrides={"manifest": gate4_manifest().model_dump(mode="json")},
        )
        payload = manifest_dict(
            attempt_record(state="terminal"),
            (finalize_result(swapped_one), finalize_result(swapped_two)),
        )
        with pytest.raises(ValueError, match="exact slot order"):
            FinalCodexRunManifest.model_validate(payload)

    def test_repetitions_must_share_one_environment(self) -> None:
        one, two = two_pass_results()
        foreign_env = environment_binding("other-host")
        foreign_two = rebound_result(two, environment=foreign_env)
        payload = manifest_dict(attempt_record(state="terminal"), (one, foreign_two))
        with pytest.raises(ValueError, match="share one exact environment"):
            FinalCodexRunManifest.model_validate(payload)


class FinalCodexStoreClosureTests:
    def test_live_attempt_on_empty_store_is_none(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "empty")
        assert store.live_attempt(CANDIDATE) is None

    def test_successor_attempt_requires_the_next_number(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "number")
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        publish_run(
            store,
            attempt=attempt,
            drafts=(make_pass_draft(1, identities), make_pass_draft(2, identities)),
        )
        with pytest.raises(CertificationContractError, match="exact next attempt number"):
            store.reserve(
                attempt_record(
                    attempt_number=3,
                    identities=fresh_identities(),
                    record=plan_record(scenario_version="2.0.0"),
                )
            )

    def test_first_attempt_must_occupy_number_one(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "first")
        with pytest.raises(CertificationContractError, match="attempt number one"):
            store.reserve(attempt_record(attempt_number=2, identities=fresh_identities()))

    def test_a_repair_plan_admits_a_successor_run(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "successor")
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        publish_run(
            store,
            attempt=attempt,
            drafts=(make_pass_draft(1, identities), make_pass_draft(2, identities)),
        )
        successor = attempt_record(
            attempt_number=2,
            identities=fresh_identities(),
            record=plan_record(scenario_version="2.0.0"),
        )
        store.reserve(successor)
        manifest = store.manifest(CANDIDATE)
        assert manifest is not None
        assert manifest.attempt.attemptNumber == 2
        assert manifest.attempt.state == "running"

    def test_mark_running_readback_refusal(self, tmp_path: Path, monkeypatch: Any) -> None:
        store = make_store(tmp_path / "readback")
        identities = fresh_identities()
        attempt = attempt_record(state="reserved", identities=identities)
        store.reserve(attempt)
        monkeypatch.setattr(store, "_update", lambda candidate, transform: None)
        with pytest.raises(CertificationContractError, match="did not transition to running"):
            store.mark_running(attempt)

    def test_publish_requires_the_running_attempt(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "running")
        identities = fresh_identities()
        attempt = attempt_record(state="reserved", identities=identities)
        store.reserve(attempt)
        with pytest.raises(CertificationContractError, match="exact running attempt may publish"):
            store.publish_repetition(attempt, make_aborted_draft(1, identities))

    def test_draft_must_bind_the_reserved_attempt_plan(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "binding")
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        running = store.mark_running(attempt)
        foreign = make_draft(
            repetition_number=1,
            identity=identities[0],
            disposition="pass",
            record=plan_record(scenario_version="2.0.0"),
            overrides={"manifest": gate4_manifest().model_dump(mode="json")},
        )
        with pytest.raises(CertificationContractError, match="exact attempt reservation identity"):
            store.publish_repetition(running, foreign)

    def test_mark_running_refuses_an_attempt_number_mismatch(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "attempt-number")
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        wrong = attempt_record(attempt_number=2, identities=fresh_identities())
        with pytest.raises(CertificationContractError, match="reserved attempt number"):
            store.mark_running(wrong)

    def test_mark_running_refuses_a_terminal_transition(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "transition")
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        running = store.mark_running(attempt)
        store.publish_repetition(running, make_pass_draft(1, identities))
        store.publish_repetition(running, make_pass_draft(2, identities))
        with pytest.raises(CertificationContractError, match="cannot move from"):
            store.mark_running(attempt)

    def test_mark_running_requires_an_existing_manifest(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "missing")
        with pytest.raises(CertificationContractError, match="existing stable run manifest"):
            store.mark_running(attempt_record(identities=fresh_identities()))

    def test_cas_collision_fails_closed(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(store_module, "atomic_write_bytes", lambda path, payload: None)
        store = make_store(tmp_path / "cas")
        with pytest.raises(CertificationContractError, match="did not converge"):
            store.reserve(attempt_record(identities=fresh_identities()))

    def test_corrupt_manifest_root_fails_closed(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "corrupt")
        store._manifest_path(CANDIDATE).parent.mkdir(parents=True, exist_ok=True)
        store._manifest_path(CANDIDATE).write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(CertificationContractError, match="failed closed revalidation"):
            store.manifest(CANDIDATE)

    def test_empty_forbidden_root_is_skipped(self, tmp_path: Path) -> None:
        store = FinalCodexManifestStore(
            tmp_path / "namespace" / "store",
            FinalCodexStorePolicy(
                storeId="final-codex",
                forbiddenRoots=("", tmp_path / "namespace" / "quality-report-generations"),  # type: ignore[arg-type]
            ),
        )
        assert store._root is not None

    def test_candidate_mismatch_is_refused(self, tmp_path: Path) -> None:
        store = make_store(tmp_path / "candidate")
        store.reserve(attempt_record(candidate=OTHER_CANDIDATE, identities=fresh_identities()))
        other_path = store._manifest_path(OTHER_CANDIDATE)
        store._manifest_path(CANDIDATE).parent.mkdir(parents=True, exist_ok=True)
        store._manifest_path(CANDIDATE).write_bytes(other_path.read_bytes())
        with pytest.raises(CertificationContractError, match="different exact candidate"):
            store.reserve(attempt_record(identities=fresh_identities()))


class FinalCodexProjectionClosureTests:
    def _projection_payload(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schemaVersion": "final-codex-lane-projection/v1",
            "candidateIdentity": CANDIDATE.model_dump(mode="json"),
            "disposition": "not-started",
            "manifest": None,
            "certificateReady": False,
            "projectionDigest": "0" * 64,
        }
        payload.update(overrides)
        return payload

    def test_not_started_cannot_carry_a_manifest(self) -> None:
        manifest = green_manifest_dict()
        with pytest.raises(ValueError, match="not-started lane cannot carry"):
            FinalCodexLaneProjection.model_validate(
                self._projection_payload(disposition="not-started", manifest=manifest)
            )

    def test_running_lane_requires_an_incomplete_manifest(self) -> None:
        with pytest.raises(ValueError, match="running lane requires an incomplete"):
            FinalCodexLaneProjection.model_validate(self._projection_payload(disposition="running"))

    def test_projection_digest_mismatch_refused(self) -> None:
        with pytest.raises(ValueError, match="projection digest does not match"):
            FinalCodexLaneProjection.model_validate(self._projection_payload())

    def test_terminal_lane_requires_a_complete_manifest(self) -> None:
        with pytest.raises(ValueError, match="complete terminal run manifest"):
            FinalCodexLaneProjection.model_validate(self._projection_payload(disposition="red"))

    def test_two_fresh_pass_requires_readiness_and_green(self) -> None:
        manifest = green_manifest_dict()
        with pytest.raises(ValueError, match="two-fresh-pass requires"):
            FinalCodexLaneProjection.model_validate(
                self._projection_payload(
                    disposition="two-fresh-pass",
                    manifest=manifest,
                    certificateReady=False,
                )
            )

    def test_red_lane_is_never_certificate_ready(self) -> None:
        manifest = green_manifest_dict()
        with pytest.raises(ValueError, match="can never be certificate-ready"):
            FinalCodexLaneProjection.model_validate(
                self._projection_payload(
                    disposition="red",
                    manifest=manifest,
                    certificateReady=True,
                )
            )

    def test_unknown_terminal_disposition_is_refused(self) -> None:
        manifest = FinalCodexRunManifest.model_validate(green_manifest_dict())
        projection = FinalCodexLaneProjection.model_construct(
            schemaVersion="final-codex-lane-projection/v1",
            candidateIdentity=CANDIDATE,
            disposition="weird",
            manifest=manifest,
            certificateReady=False,
            projectionDigest="0" * 64,
        )
        with pytest.raises(ValueError, match="unknown terminal lane disposition"):
            projection_module._require_terminal_projection(projection)


class FinalCodexPlanningClosureTests:
    def _compile(self, registry: CanonicalRailRegistry, plan: Any) -> None:
        compile_final_codex_plan_record(
            registry,
            certifying_plan=plan,
            candidate_identity=CANDIDATE,
            scenario_version=SCENARIO_VERSION,
            plan_version="1.0.0",
        )

    def test_invalid_registry_refuses_plan_admission(self) -> None:
        registry = scenario_registry()
        profile = registry.registry.profiles[0]
        duplicated = RailRegistry(
            registryId=registry.registry.registryId,
            repositoryId=registry.registry.repositoryId,
            profiles=(profile, profile),
            rails=registry.registry.rails,
        )
        invalid = CanonicalRailRegistry(
            registry=duplicated,
            registryDigest=content_digest({"registry": duplicated.model_dump(mode="json")}),
        )
        with pytest.raises(CertificationContractError):
            self._compile(invalid, certifying_plan(registry))

    def test_incomplete_or_unsorted_profile_prefix_refused(self) -> None:
        registry = scenario_registry()
        profile = registry.registry.profiles[0]
        unsorted = RegistryProfile(
            profileId=profile.profileId,
            kind=profile.kind,
            gates=(1, 2, 3, 5, 4),
        )
        rerouted = RailRegistry(
            registryId=registry.registry.registryId,
            repositoryId=registry.registry.repositoryId,
            profiles=(unsorted,),
            rails=registry.registry.rails,
        )
        reordered = CanonicalRailRegistry(
            registry=rerouted,
            registryDigest=content_digest({"registry": rerouted.model_dump(mode="json")}),
        )
        with pytest.raises(CertificationContractError) as error:
            self._compile(reordered, certifying_plan(reordered))
        assert "final-codex-gate-prefix-incomplete" in store_codes(error.value)

    def test_non_certifying_plan_copy_refused(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry).model_copy(update={"profileKind": "diagnostic"})
        with pytest.raises(CertificationContractError) as error:
            self._compile(registry, plan)
        assert "final-codex-not-certifying" in store_codes(error.value)

    def test_foreign_plan_bytes_refused(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry).model_copy(update={"registryDigest": "0" * 64})
        with pytest.raises(CertificationContractError, match="not the exact canonical registry"):
            self._compile(registry, plan)

    def test_gate_plan_candidate_mismatch_refused(self) -> None:
        registry = scenario_registry()
        foreign = compile_certification_plan(
            registry,
            profile_id=CERT_PROFILE,
            candidate_identity=OTHER_CANDIDATE,
        )
        with pytest.raises(CertificationContractError) as error:
            planning_module.final_codex_gate_plan(plan_record(), foreign)
        assert "final-codex-gate-candidate-mismatch" in store_codes(error.value)

    def test_gate_plan_profile_mismatch_refused(self) -> None:
        record = plan_record().model_copy(update={"profileId": "other-profile"})
        registry = scenario_registry()
        with pytest.raises(CertificationContractError) as error:
            planning_module.final_codex_gate_plan(record, certifying_plan(registry))
        assert "final-codex-gate-profile-mismatch" in store_codes(error.value)

    def test_gate_plan_registry_mismatch_refused(self) -> None:
        record = plan_record().model_copy(update={"registryDigest": "0" * 64})
        registry = scenario_registry()
        with pytest.raises(CertificationContractError) as error:
            planning_module.final_codex_gate_plan(record, certifying_plan(registry))
        assert "final-codex-gate-registry-mismatch" in store_codes(error.value)

    def test_gate_plan_digest_mismatch_refused(self) -> None:
        record = plan_record().model_copy(update={"gatePlanDigest": "0" * 64})
        registry = scenario_registry()
        with pytest.raises(CertificationContractError) as error:
            planning_module.final_codex_gate_plan(record, certifying_plan(registry))
        assert "final-codex-gate-plan-mismatch" in store_codes(error.value)

    def test_gates_1_3_must_bind_the_frozen_certifying_plan(self) -> None:
        registry = scenario_registry()
        green = green_gates(registry)
        stale = green[2].model_copy(update={"certificationPlanDigest": "0" * 64})
        with pytest.raises(CertificationContractError, match="exact certifying plan"):
            planning_module.require_gates_one_to_three_green(
                CANDIDATE,
                (green[0], green[1], stale),
                certifying_plan=certifying_plan(registry),
            )

    def test_unknown_profile_is_refused(self) -> None:
        registry = scenario_registry()
        with pytest.raises(CertificationContractError) as error:
            planning_module._selected_profile(registry, "ghost-profile")
        assert "final-codex-profile-unknown" in store_codes(error.value)

    def test_diagnostic_profile_kind_is_refused(self) -> None:
        registry = scenario_registry()
        profile = registry.registry.profiles[0]
        diagnostic = RegistryProfile(
            profileId=profile.profileId,
            kind="diagnostic",
            gates=(1, 2, 3, 4, 5),
        )
        rerouted = RailRegistry(
            registryId=registry.registry.registryId,
            repositoryId=registry.registry.repositoryId,
            profiles=(diagnostic,),
            rails=registry.registry.rails,
        )
        diag_registry = CanonicalRailRegistry(
            registry=rerouted,
            registryDigest=content_digest({"registry": rerouted.model_dump(mode="json")}),
        )
        plan = compile_certification_plan(
            diag_registry,
            profile_id=CERT_PROFILE,
            candidate_identity=CANDIDATE,
        )
        with pytest.raises(CertificationContractError) as error:
            self._compile(diag_registry, plan)
        assert "final-codex-profile-kind-mismatch" in store_codes(error.value)

    def test_plan_must_select_the_same_profile(self) -> None:
        registry = scenario_registry()
        profile = registry.registry.profiles[0]
        plan = certifying_plan(registry).model_copy(update={"profileId": "ghost-profile"})
        with pytest.raises(CertificationContractError) as error:
            planning_module._require_certifying_profile(profile, plan)
        assert "final-codex-plan-profile-mismatch" in store_codes(error.value)


class FinalCodexCertificateClosureTests:
    def _green_manifest(self) -> FinalCodexRunManifest:
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        drafts = (make_pass_draft(1, identities), make_pass_draft(2, identities))
        store = make_store(Path(tempfile.mkdtemp(prefix="l14-cert-")) / "store")
        publish_run(store, attempt=attempt, drafts=drafts)
        manifest = store.manifest(CANDIDATE)
        assert manifest is not None
        return manifest

    def _envelope(self) -> FinalCodexCertificateEnvelope:
        certificate = compile_gate_four_certificate(
            plan_record(),
            self._green_manifest(),
            predecessor_identities(),
            repository_id="sample-repository",
        )
        return certificate.semanticEnvelope

    def test_envelope_requires_the_ordered_gate_prefix(self) -> None:
        envelope = self._envelope()
        payload = envelope.model_dump(mode="json")
        preds = list(payload["directPredecessors"])
        preds[1], preds[2] = preds[2], preds[1]
        payload["directPredecessors"] = preds
        with pytest.raises(ValueError, match="Gate-4 predecessors must be the exact ordered"):
            FinalCodexCertificateEnvelope.model_validate(payload)

    def test_envelope_requires_ordered_repetition_slots(self) -> None:
        envelope = self._envelope()
        payload = envelope.model_dump(mode="json")
        results = list(payload["repetitionResults"])
        payload["repetitionResults"] = (results[1], results[0])
        with pytest.raises(ValueError, match="exact ordered slots one and two"):
            FinalCodexCertificateEnvelope.model_validate(payload)

    def test_envelope_rejects_a_retried_repetition(self) -> None:
        envelope = self._envelope()
        manifest = self._green_manifest()
        one, two = manifest.repetitions
        retried = two.model_copy(update={"retryCount": 1})
        crafted = FinalCodexCertificateEnvelope.model_construct(
            schemaVersion=envelope.schemaVersion,
            certificateVersion="1.0.0",
            gate=4,
            candidateIdentity=CANDIDATE,
            repositoryId="sample-repository",
            profileId=envelope.profileId,
            certificationPlanDigest=envelope.certificationPlanDigest,
            registryDigest=envelope.registryDigest,
            gatePlanDigest=envelope.gatePlanDigest,
            scenarioVersion=envelope.scenarioVersion,
            planVersion=envelope.planVersion,
            directPredecessors=predecessor_identities(),
            resultManifestDigests=envelope.resultManifestDigests,
            runtimeAuthority=manifest.repetitions[0].runtimeAuthority,
            repetitionResults=(one, retried),
        )
        with pytest.raises(ValueError, match="never bind a retried repetition"):
            FinalCodexCertificateEnvelope.__dict__["_verify_envelope"](crafted)

    def test_envelope_rejects_a_non_certifying_repetition(self) -> None:
        envelope = self._envelope()
        manifest = self._green_manifest()
        one, two = manifest.repetitions
        downgraded = two.model_copy(update={"acceptanceEligible": False})
        crafted = FinalCodexCertificateEnvelope.model_construct(
            schemaVersion=envelope.schemaVersion,
            certificateVersion="1.0.0",
            gate=4,
            candidateIdentity=CANDIDATE,
            repositoryId="sample-repository",
            profileId=envelope.profileId,
            certificationPlanDigest=envelope.certificationPlanDigest,
            registryDigest=envelope.registryDigest,
            gatePlanDigest=envelope.gatePlanDigest,
            scenarioVersion=envelope.scenarioVersion,
            planVersion=envelope.planVersion,
            directPredecessors=predecessor_identities(),
            resultManifestDigests=envelope.resultManifestDigests,
            runtimeAuthority=manifest.repetitions[0].runtimeAuthority,
            repetitionResults=(one, downgraded),
        )
        with pytest.raises(ValueError, match="only certifying acceptance-eligible"):
            FinalCodexCertificateEnvelope.__dict__["_verify_envelope"](crafted)

    def test_envelope_authority_must_match_both_repetitions(self) -> None:
        envelope = self._envelope()
        payload = envelope.model_dump(mode="json")
        payload["runtimeAuthority"] = authority_binding(snapshot_digest="0" * 64).model_dump(
            mode="json"
        )
        with pytest.raises(ValueError, match="certificate authority must match"):
            FinalCodexCertificateEnvelope.model_validate(payload)

    def test_certificate_digest_mismatch_refused(self) -> None:
        envelope = self._envelope()
        with pytest.raises(ValueError, match="certificate digest does not match"):
            FinalCodexGateFourCertificate.model_validate(
                {
                    "semanticEnvelope": envelope.model_dump(mode="json"),
                    "certificateDigest": "0" * 64,
                }
            )

    def test_compile_refuses_a_stale_plan_record(self) -> None:
        manifest = self._green_manifest()
        stale = plan_record(scenario_version="2.0.0")
        with pytest.raises(CertificationContractError, match="exact frozen plan record"):
            compile_gate_four_certificate(
                stale,
                manifest,
                predecessor_identities(),
                repository_id="sample-repository",
            )

    def test_compile_refuses_a_manifest_missing_result_manifests(self) -> None:
        manifest = self._green_manifest()
        one, two = manifest.repetitions
        stripped = one.model_copy(update={"manifest": None})
        degraded = manifest.model_copy(update={"repetitions": (stripped, two)})
        with pytest.raises(CertificationContractError, match="complete result manifests"):
            compile_gate_four_certificate(
                plan_record(),
                degraded,
                predecessor_identities(),
                repository_id="sample-repository",
            )


class FinalCodexExecutorClosureTests:
    def test_runner_protocol_default_refuses_execution(self) -> None:
        with pytest.raises(NotImplementedError):
            FinalCodexScenarioRunner.__dict__["run_once"](
                object(),
                gate_plan=None,
                environment=None,
                snapshot=None,
                repetitionIdentity=None,
            )

    def test_run_spec_reports_its_rail_count(self) -> None:
        registry = scenario_registry()
        plan = certifying_plan(registry)
        spec = build_final_codex_run_spec(
            registry,
            certifying_plan=plan,
            candidate_identity=CANDIDATE,
            options=FinalCodexRunOptions(
                environment_identity="codex-host",
                scenario_version=SCENARIO_VERSION,
                plan_version="1.0.0",
            ),
        )
        assert spec.railCount == len(spec.gatePlan.rails)

    def test_continuation_snapshot_is_readmitted_on_the_same_authority(
        self, tmp_path: Path
    ) -> None:
        engine, registry, _ = executor_make_engine(tmp_path)
        spec = executor_make_spec()
        attempt = engine.admit(spec, executor_green_gates())
        snapshot = attempt.snapshot
        with pytest.raises(CertificationContractError) as error:
            engine.admit(spec, executor_green_gates(), continuation_snapshot=snapshot)
        assert "final-codex-already-in-flight" in store_codes(error.value)
        assert registry.census(snapshot.snapshot_digest) == 1

    def test_abort_refuses_an_already_terminal_run(self, tmp_path: Path) -> None:
        engine, _, _ = executor_make_engine(tmp_path)
        spec = executor_make_spec()
        attempt = engine.admit(spec, executor_green_gates())
        engine.run(attempt, RecordingRunner(outcome="pass"))
        with pytest.raises(CertificationContractError) as error:
            engine.abort(attempt, teardownEvidence=(), processClean=False)
        assert "final-codex-attempt-not-running" in store_codes(error.value)

    def test_abort_continues_past_an_already_published_slot(self, tmp_path: Path) -> None:
        engine, registry, _ = executor_make_engine(tmp_path)
        spec = executor_make_spec()
        attempt = engine.admit(spec, executor_green_gates())
        engine._run_repetition(attempt, runner=RecordingRunner(outcome="pass"), repetition_number=1)
        manifest = engine.abort(
            attempt,
            teardownEvidence=teardown_evidence(),
            processClean=False,
        )
        assert manifest.complete is True
        assert manifest.aggregate == "red"
        assert manifest.repetitions[0].disposition == "pass"
        assert manifest.repetitions[1].disposition == "aborted"
        assert registry.census(attempt.snapshot.snapshot_digest) == 0

    def test_default_clock_and_nonce_helpers(self) -> None:
        stamp = executor_module._utc_now()
        assert stamp.endswith("+00:00")
        entropy = executor_module._entropy()
        assert len(entropy) == 64
        assert all(character in "0123456789abcdef" for character in entropy)
