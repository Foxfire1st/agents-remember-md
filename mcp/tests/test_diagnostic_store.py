"""Standalone CCR-R13 durable diagnostic manifest store tests.

The store owns one stable manifest per exact candidate in an isolated
namespace: immutable attempts and results, gapless chain identity, newest
terminal selection, in-flight refusal, and namespace isolation from the
certifying quality-report manifest.  Every case uses temporary directories and
no Dagger or external service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from agents_remember.certification.certificate_models import CandidateIdentity
from agents_remember.certification.diagnostics.models import (
    DiagnosticAttemptRecord,
    DiagnosticEnvironmentBinding,
    DiagnosticFailureRecord,
    DiagnosticRunResultDraft,
    DiagnosticRuntimeAuthorityBinding,
    DiagnosticTeardownRecord,
)
from agents_remember.certification.diagnostics.store import (
    DiagnosticManifestStore,
    DiagnosticStorePolicy,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    RailEvidenceReference,
)
from agents_remember.errors import CertificationContractError

CANDIDATE = CandidateIdentity(kind="git-tree", value="c" * 40)
OTHER_CANDIDATE = CandidateIdentity(kind="git-tree", value="d" * 40)
REGISTRY_DIGEST = "a" * 64
CERTIFYING_PLAN_DIGEST = "b" * 64
DIAGNOSTIC_PLAN_DIGEST = "c" * 64
NONCE_ONE = "1" * 64
NONCE_TWO = "2" * 64


def environment_binding() -> DiagnosticEnvironmentBinding:
    return DiagnosticEnvironmentBinding(
        environmentIdentity="codex-host",
        environmentDigest=content_digest({"environmentIdentity": "codex-host"}),
    )


def authority_binding(snapshot_digest: str = "d" * 64) -> DiagnosticRuntimeAuthorityBinding:
    payload = {
        "schemaVersion": "diagnostic-runtime-authority/v1",
        "snapshotDigest": snapshot_digest,
        "endpoint": "container://shared-dagger-engine",
        "engineId": "shared-dagger-engine",
        "layerStore": "/var/lib/dagger",
        "storeMountedPath": "/var/lib/dagger",
        "observedVersion": "dagger 0.21.8",
    }
    return DiagnosticRuntimeAuthorityBinding(
        snapshotDigest=snapshot_digest,
        endpoint="container://shared-dagger-engine",
        engineId="shared-dagger-engine",
        layerStore="/var/lib/dagger",
        storeMountedPath="/var/lib/dagger",
        observedVersion="dagger 0.21.8",
        bindingDigest=content_digest(payload),
    )


def evidence() -> RailEvidenceReference:
    return RailEvidenceReference(
        evidenceId="e2e-evidence",
        sha256=content_digest({"evidence": "e2e"}),
        size=32,
        reference="evidence://e2e",
    )


def teardown() -> DiagnosticTeardownRecord:
    return DiagnosticTeardownRecord(
        releasedOwner=True,
        evidence=(evidence(),),
        at="2026-09-04T00:10:00Z",
    )


def attempt(
    number: int,
    nonce: str,
    state: Literal["reserved", "running", "terminal"] = "running",
    candidate: CandidateIdentity = CANDIDATE,
) -> DiagnosticAttemptRecord:
    payload = {
        "schemaVersion": "diagnostic-attempt/v1",
        "candidateIdentity": candidate.model_dump(mode="json"),
        "attemptNumber": number,
        "diagnosticNonce": nonce,
        "registryDigest": REGISTRY_DIGEST,
        "certifyingPlanDigest": CERTIFYING_PLAN_DIGEST,
        "diagnosticPlanDigest": DIAGNOSTIC_PLAN_DIGEST,
        "planVersion": "1.0.0",
        "gate": 4,
        "profileId": "diagnostic-ci",
        "requestedAt": "2026-09-04T00:00:00Z",
        "state": state,
    }
    return DiagnosticAttemptRecord(**payload, attemptDigest=content_digest(payload))


def failure_record(
    failure_class: Literal["infrastructure", "parser"] = "infrastructure",
) -> DiagnosticFailureRecord:
    return DiagnosticFailureRecord(
        failureClass=failure_class,
        code="engine-lost",
        detail="the runner died before scenario completion",
        correctiveOwner="dagger-owner",
        evidence=(evidence(),),
    )


def terminal_draft(
    number: int,
    nonce: str,
    disposition: Literal["aborted", "hard-failure"],
    terminal_at: str = "2026-09-04T00:10:00Z",
) -> DiagnosticRunResultDraft:
    failure = None if disposition == "aborted" else failure_record()
    return DiagnosticRunResultDraft(
        attemptNumber=number,
        candidateIdentity=CANDIDATE,
        registryDigest=REGISTRY_DIGEST,
        certifyingPlanDigest=CERTIFYING_PLAN_DIGEST,
        diagnosticPlanDigest=DIAGNOSTIC_PLAN_DIGEST,
        planVersion="1.0.0",
        gate=4,
        profileId="diagnostic-ci",
        diagnosticNonce=nonce,
        acceptanceEligible=False,
        certifying=False,
        disposition=disposition,
        failure=failure,
        environment=environment_binding(),
        runtimeAuthority=authority_binding(),
        manifest=None,
        teardown=teardown(),
        artifacts=(),
        startedAt="2026-09-04T00:00:00Z",
        terminalAt=terminal_at,
    )


def store(tmp_path: Path, root: str = "diagnostics") -> DiagnosticManifestStore:
    return DiagnosticManifestStore(
        tmp_path / root,
        DiagnosticStorePolicy(
            storeId=root,
            forbiddenRoots=(tmp_path / "quality-report-generations",),
        ),
    )


def store_codes(error: CertificationContractError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


class DiagnosticStoreTests:
    def test_optional_lane_is_empty_before_any_request(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        assert lane.manifest(CANDIDATE) is None
        assert lane.newest_terminal(CANDIDATE) is None
        assert lane.live_attempt(CANDIDATE) is None
        assert lane.next_attempt_number(CANDIDATE) == 1
        assert lane.has_attempts(CANDIDATE) is False

    def test_reserve_run_publish_sequence_advances_the_manifest(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        reservation = attempt(1, NONCE_ONE)
        lane.reserve(reservation)
        assert lane.next_attempt_number(CANDIDATE) == 2
        assert lane.live_attempt(CANDIDATE) is not None
        running = lane.mark_running(reservation)
        assert running.state == "running"
        result = lane.publish_terminal(running, terminal_draft(1, NONCE_ONE, "aborted"))
        assert result.resultId == content_digest(
            {
                **terminal_draft(1, NONCE_ONE, "aborted").model_dump(mode="json"),
                "predecessorDigest": "",
            }
        )
        manifest = lane.manifest(CANDIDATE)
        assert manifest is not None
        assert manifest.newestTerminal == result
        assert lane.live_attempt(CANDIDATE) is None
        assert lane.has_attempts(CANDIDATE) is True
        assert result.predecessorDigest == ""
        assert result.resultDigest == content_digest(
            result.model_dump(mode="json", exclude={"resultDigest"})
        )

    def test_in_flight_attempt_blocks_a_fresh_request(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        lane.reserve(attempt(1, NONCE_ONE))
        with pytest.raises(CertificationContractError) as error:
            lane.reserve(attempt(2, NONCE_TWO))
        assert "diagnostic-already-in-flight" in store_codes(error.value)
        # the live reservation is still the exact same record
        live = lane.live_attempt(CANDIDATE)
        assert live is not None
        assert live.diagnosticNonce == NONCE_ONE

    def test_attempt_number_mismatch_is_refused(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        lane.reserve(attempt(1, NONCE_ONE))
        lane.mark_running(attempt(1, NONCE_ONE))
        lane.publish_terminal(attempt(1, NONCE_ONE), terminal_draft(1, NONCE_ONE, "aborted"))
        with pytest.raises(CertificationContractError) as error:
            lane.reserve(attempt(3, NONCE_TWO))
        assert "diagnostic-attempt-number-mismatch" in store_codes(error.value)

    def test_double_terminalization_of_one_attempt_is_refused(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        lane.reserve(attempt(1, NONCE_ONE))
        running = lane.mark_running(attempt(1, NONCE_ONE))
        lane.publish_terminal(running, terminal_draft(1, NONCE_ONE, "aborted"))
        # attempt two is fresh; publishing attempt one again cannot resurface.
        lane.reserve(attempt(2, NONCE_TWO))
        lane.mark_running(attempt(2, NONCE_TWO))
        with pytest.raises(CertificationContractError) as error:
            lane.publish_terminal(attempt(1, NONCE_ONE), terminal_draft(1, NONCE_ONE, "aborted"))
        assert "diagnostic-attempt-not-running" in store_codes(error.value)

    def test_wrong_state_transitions_and_wrong_nonce_are_refused(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        lane.reserve(attempt(1, NONCE_ONE))
        with pytest.raises(CertificationContractError) as error:
            lane.mark_running(attempt(1, NONCE_TWO))
        assert "diagnostic-attempt-nonce-mismatch" in store_codes(error.value)

    def test_newest_terminal_is_selected_and_chain_links_are_immutable(
        self, tmp_path: Path
    ) -> None:
        lane = store(tmp_path)
        lane.reserve(attempt(1, NONCE_ONE))
        first = lane.publish_terminal(
            lane.mark_running(attempt(1, NONCE_ONE)),
            terminal_draft(1, NONCE_ONE, "hard-failure"),
        )
        lane.reserve(attempt(2, NONCE_TWO))
        second = lane.publish_terminal(
            lane.mark_running(attempt(2, NONCE_TWO)),
            terminal_draft(2, NONCE_TWO, "aborted"),
        )
        assert lane.newest_terminal(CANDIDATE) == second
        assert second.predecessorDigest == first.resultId
        manifest = lane.manifest(CANDIDATE)
        assert manifest is not None
        assert len(manifest.results) == 2
        assert len(manifest.attempts) == 2
        # attempt one result bytes are still exactly the published bytes
        assert first.resultDigest == content_digest(
            first.model_dump(mode="json", exclude={"resultDigest"})
        )

    def test_candidates_are_fully_isolated(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        lane.reserve(attempt(1, NONCE_ONE))
        lane.publish_terminal(
            lane.mark_running(attempt(1, NONCE_ONE)),
            terminal_draft(1, NONCE_ONE, "aborted"),
        )
        assert lane.has_attempts(OTHER_CANDIDATE) is False
        assert lane.manifest(OTHER_CANDIDATE) is None
        assert lane.live_attempt(OTHER_CANDIDATE) is None
        assert lane.next_attempt_number(OTHER_CANDIDATE) == 1
        # a different candidate starts its own optional lane from zero
        lane.reserve(attempt(1, NONCE_ONE, candidate=OTHER_CANDIDATE))

    def test_abandon_clears_only_the_newest_never_started_slot(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        first = attempt(1, NONCE_ONE)
        lane.reserve(first)
        running = lane.mark_running(first)
        lane.publish_terminal(running, terminal_draft(1, NONCE_ONE, "aborted"))
        lane.reserve(attempt(2, NONCE_TWO))
        with pytest.raises(CertificationContractError) as error:
            lane.abandon(first)
        assert "diagnostic-attempt-not-live" not in store_codes(error.value)
        lane.abandon(attempt(2, NONCE_TWO))
        assert lane.live_attempt(CANDIDATE) is None
        assert lane.next_attempt_number(CANDIDATE) == 2
        lane.reserve(attempt(2, NONCE_TWO))
        live = lane.live_attempt(CANDIDATE)
        assert live is not None
        assert live.attemptNumber == 2

    def test_manifest_corruption_fails_closed(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        lane.reserve(attempt(1, NONCE_ONE))
        manifest_path = lane._manifest_path(CANDIDATE)
        assert manifest_path.exists()
        manifest_path.write_text("{not-json", encoding="utf-8")
        with pytest.raises(CertificationContractError) as error:
            lane.manifest(CANDIDATE)
        assert "diagnostic-manifest-corrupt" in store_codes(error.value)
        # a tampered digest fails closed revalidation too
        lane_two = store(tmp_path, root="second")
        lane_two.reserve(attempt(1, NONCE_ONE))
        path = lane_two._manifest_path(CANDIDATE)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["manifestDigest"] = "0" * 64
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(CertificationContractError) as error:
            lane_two.manifest(CANDIDATE)
        assert "diagnostic-manifest-corrupt" in store_codes(error.value)

    def test_namespace_collision_with_the_certifying_manifest_is_refused(
        self, tmp_path: Path
    ) -> None:
        quality_root = tmp_path / "quality-report-generations"
        quality_root.mkdir()
        with pytest.raises(CertificationContractError) as error:
            DiagnosticManifestStore(
                quality_root / "inner",
                DiagnosticStorePolicy(forbiddenRoots=(quality_root,)),
            )
        assert "diagnostic-namespace-collision" in store_codes(error.value)
        with pytest.raises(CertificationContractError) as error:
            DiagnosticManifestStore(
                tmp_path / "diagnostics",
                DiagnosticStorePolicy(forbiddenRoots=(tmp_path / "diagnostics",)),
            )
        assert "diagnostic-namespace-collision" in store_codes(error.value)

    def test_store_never_writes_a_not_requested_optional_marker(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        # the empty lane is represented by absence: no manifest file exists
        assert lane.manifest(CANDIDATE) is None
        lane.reserve(attempt(1, NONCE_ONE))
        lane.publish_terminal(
            lane.mark_running(attempt(1, NONCE_ONE)),
            terminal_draft(1, NONCE_ONE, "aborted"),
        )
        manifest = lane.manifest(CANDIDATE)
        assert manifest is not None
        assert manifest.newestTerminal is not None
        assert "not-requested-optional" not in json.dumps(manifest.model_dump(mode="json"))

    def test_store_fail_closed_when_live_attempt_missing_manifest(self, tmp_path: Path) -> None:
        lane = store(tmp_path)
        with pytest.raises(CertificationContractError) as error:
            lane.mark_running(attempt(1, NONCE_ONE))
        assert "diagnostic-manifest-missing" in store_codes(error.value)
        with pytest.raises(CertificationContractError) as error:
            lane.publish_terminal(attempt(1, NONCE_ONE), terminal_draft(1, NONCE_ONE, "aborted"))
        assert "diagnostic-manifest-missing" in store_codes(error.value)
