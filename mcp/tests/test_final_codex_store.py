"""Standalone CCR-R14 store tests: two-fresh CAS run manifest owner.

Covers reservation (retry disabled for the exact same plan), in-flight
refusal, exact slot ordering, immutable digest chain, terminalization only
after both repetitions publish, namespace isolation, and corrupt-file
fail-closed reads.  Fully standalone: imports only the leaf-local builder
module and the package under test.
"""

from __future__ import annotations

import pytest
from agents_remember.certification.final_codex.store import (
    FinalCodexManifestStore,
    FinalCodexStorePolicy,
)
from agents_remember.errors import CertificationContractError
from test_final_codex_models import (
    CANDIDATE,
    OTHER_CANDIDATE,
    attempt_record,
    certifying_plan,
    content_digest,
    fresh_identities,
    make_draft,
    make_store,
    manifest_for,
    scenario_registry,
    store_codes,
)

DIGEST = "a" * 64


def gate4():
    return manifest_for(scenario_registry(), certifying_plan(scenario_registry()), 4)


def draft(identities, repetition_number: int, disposition: str = "pass"):
    return make_draft(
        repetition_number=repetition_number,
        identity=identities[repetition_number - 1],
        disposition=disposition,
        overrides={"manifest": gate4().model_dump(mode="json")}
        if disposition in {"pass", "fail"}
        else None,
    )


def publish_two(store, identities) -> None:
    attempt = attempt_record(identities=identities)
    store.reserve(attempt)
    running = store.mark_running(attempt)
    store.publish_repetition(running, draft(identities, 1))
    store.publish_repetition(running, draft(identities, 2))


class FinalCodexStoreTests:
    def test_reserve_then_publish_two_repetitions(self, tmp_path) -> None:
        store = make_store(tmp_path)
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        running = store.mark_running(attempt)
        assert running.state == "running"
        assert store.live_attempt(CANDIDATE) is not None
        store.publish_repetition(running, draft(identities, 1))
        partial = store.manifest(CANDIDATE)
        assert partial is not None
        assert partial.complete is False
        assert store.live_attempt(CANDIDATE) is not None
        store.publish_repetition(running, draft(identities, 2))
        manifest = store.manifest(CANDIDATE)
        assert manifest is not None
        assert manifest.complete is True
        assert manifest.attempt.state == "terminal"
        assert manifest.aggregate == "green"
        assert store.live_attempt(CANDIDATE) is None

    def test_retry_disabled_for_the_exact_same_plan(self, tmp_path) -> None:
        store = make_store(tmp_path)
        identities = fresh_identities()
        publish_two(store, identities)
        with pytest.raises(CertificationContractError) as error:
            store.reserve(attempt_record(attempt_number=2, identities=fresh_identities()))
        assert "final-codex-retry-disabled" in store_codes(error.value)

    def test_second_live_attempt_is_refused(self, tmp_path) -> None:
        store = make_store(tmp_path)
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        with pytest.raises(CertificationContractError) as error:
            store.reserve(attempt_record(identities=fresh_identities()))
        assert "final-codex-already-in-flight" in store_codes(error.value)

    def test_slot_publish_must_be_in_fixed_order(self, tmp_path) -> None:
        store = make_store(tmp_path)
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        running = store.mark_running(attempt)
        second = draft(identities, 2)
        with pytest.raises(CertificationContractError) as error:
            store.publish_repetition(running, second)
        assert "final-codex-repetition-out-of-order" in store_codes(error.value)
        first = draft(identities, 1)
        store.publish_repetition(running, first)
        with pytest.raises(CertificationContractError) as error:
            store.publish_repetition(running, first)
        assert "final-codex-repetition-already-published" in store_codes(error.value)

    def test_draft_must_bind_reserved_identity(self, tmp_path) -> None:
        store = make_store(tmp_path)
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        running = store.mark_running(attempt)
        wrong = make_draft(
            repetition_number=1,
            identity=fresh_identities()[1],
            disposition="pass",
            overrides={"manifest": gate4().model_dump(mode="json")},
        )
        with pytest.raises(CertificationContractError) as error:
            store.publish_repetition(running, wrong)
        assert "final-codex-draft-identity-mismatch" in store_codes(error.value)

    def test_corrupt_manifest_fails_closed(self, tmp_path) -> None:
        store = make_store(tmp_path)
        identities = fresh_identities()
        publish_two(store, identities)
        candidate_dir = content_digest({"kind": CANDIDATE.kind, "value": CANDIDATE.value})
        manifest_file = tmp_path / "final-codex" / candidate_dir / "manifest.json"
        manifest_file.write_text('{"broken": true}', encoding="utf-8")
        with pytest.raises(CertificationContractError) as error:
            store.manifest(CANDIDATE)
        assert "final-codex-manifest-corrupt" in store_codes(error.value)

    def test_namespace_collision_is_refused(self, tmp_path) -> None:
        # an ancestor of the store root is a forbidden certifying manifest root
        with pytest.raises(CertificationContractError) as error:
            FinalCodexManifestStore(
                tmp_path / "nested" / "final-codex",
                FinalCodexStorePolicy(forbiddenRoots=(tmp_path,)),
            )
        assert "final-codex-namespace-collision" in store_codes(error.value)

    def test_candidates_keep_separate_manifests(self, tmp_path) -> None:
        store = make_store(tmp_path)
        identities = fresh_identities()
        publish_two(store, identities)
        assert store.manifest(CANDIDATE) is not None
        assert store.manifest(OTHER_CANDIDATE) is None
