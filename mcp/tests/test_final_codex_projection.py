"""Standalone CCR-R14 lane-projection tests.

Covers the readiness projection: an empty lane is not-started, a live attempt
with no complete run is running, only the exact two-fresh-pass green run is
certificate-ready, a one-pass/one-fail run stays red, and a changed plan marks
a terminal run stale until a newer run binds the current plan.  Fully
standalone: imports only the leaf-local builder module and the package under
test.
"""

from __future__ import annotations

from agents_remember.certification.final_codex.projection import (
    final_codex_certificate_ready,
    project_final_codex_lane,
)
from test_final_codex_models import (
    CANDIDATE,
    OTHER_CANDIDATE,
    attempt_record,
    fresh_identities,
    gate4_manifest,
    make_draft,
    make_store,
    plan_record,
    scenario_failure,
)


def two_fresh(store, *, fail_one: bool = False) -> None:
    identities = fresh_identities()
    attempt = attempt_record(identities=identities)
    store.reserve(attempt)
    running = store.mark_running(attempt)
    one = make_draft(
        repetition_number=1,
        identity=identities[0],
        disposition="fail" if fail_one else "pass",
        overrides={
            "manifest": gate4_manifest(red=fail_one).model_dump(mode="json"),
            "failure": scenario_failure().model_dump(mode="json") if fail_one else None,
        },
    )
    two = make_draft(
        repetition_number=2,
        identity=identities[1],
        disposition="pass",
        overrides={"manifest": gate4_manifest().model_dump(mode="json")},
    )
    store.publish_repetition(running, one)
    store.publish_repetition(running, two)


class FinalCodexProjectionTests:
    def test_empty_lane_is_not_started(self, tmp_path) -> None:
        store = make_store(tmp_path)
        projection = project_final_codex_lane(store, CANDIDATE)
        assert projection.disposition == "not-started"
        assert projection.certificateReady is False
        assert projection.manifest is None

    def test_live_attempt_projects_running(self, tmp_path) -> None:
        store = make_store(tmp_path)
        identities = fresh_identities()
        attempt = attempt_record(identities=identities)
        store.reserve(attempt)
        store.mark_running(attempt)
        projection = project_final_codex_lane(store, CANDIDATE)
        assert projection.disposition == "running"
        assert projection.certificateReady is False

    def test_two_fresh_pass_is_certificate_ready(self, tmp_path) -> None:
        store = make_store(tmp_path)
        two_fresh(store)
        projection = project_final_codex_lane(store, CANDIDATE)
        assert projection.disposition == "two-fresh-pass"
        assert projection.certificateReady is True
        assert final_codex_certificate_ready(store, CANDIDATE) is True

    def test_one_pass_one_fail_stays_red(self, tmp_path) -> None:
        store = make_store(tmp_path)
        two_fresh(store, fail_one=True)
        projection = project_final_codex_lane(store, CANDIDATE)
        assert projection.disposition == "red"
        assert projection.certificateReady is False
        assert final_codex_certificate_ready(store, CANDIDATE) is False

    def test_plan_change_stales_the_terminal_run(self, tmp_path) -> None:
        store = make_store(tmp_path)
        two_fresh(store)
        changed = plan_record(scenario_version="2.0.0")
        projection = project_final_codex_lane(store, CANDIDATE, current_plan=changed)
        assert projection.disposition == "stale"
        assert projection.certificateReady is False
        current = plan_record()
        assert (
            project_final_codex_lane(store, CANDIDATE, current_plan=current).disposition
            == "two-fresh-pass"
        )

    def test_candidates_keep_separate_lane_projections(self, tmp_path) -> None:
        store = make_store(tmp_path)
        two_fresh(store)
        assert project_final_codex_lane(store, CANDIDATE).certificateReady is True
        assert project_final_codex_lane(store, OTHER_CANDIDATE).disposition == "not-started"
