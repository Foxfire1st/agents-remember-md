"""Forcing proofs for classified closeout-projection source planes."""

from __future__ import annotations

import inspect
from dataclasses import replace

from agents_remember.worktrees.queue import closeout_projection
from agents_remember.worktrees.queue import closeout_projection_members as members
from agents_remember.worktrees.queue import closeout_projection_source_facts as source_facts
from test_closeout_projection_member_helpers import _documents


def test_task_source_fact_uses_only_address_and_completion_readiness() -> None:
    _, _, candidate = _documents()
    baseline = source_facts.task_source_fact(candidate)
    route_review = candidate.document.routeReview
    assert route_review is not None
    display_and_evidence_changed = replace(
        candidate,
        document=candidate.document.model_copy(
            update={
                "title": "Display-only rename",
                "createdAt": "2026-09-01T00:00:00+00:00",
                "routeReview": route_review.model_copy(
                    update={"reviewedAt": "2026-09-01T00:00:00+00:00"}
                ),
            }
        ),
    )
    readiness_changed = replace(
        candidate,
        document=candidate.document.model_copy(update={"status": "Completed"}),
    )

    assert source_facts.task_source_fact(display_and_evidence_changed) == baseline
    assert source_facts.task_source_fact(readiness_changed) != baseline
    assert set(baseline) == {"address", "state", "completionReadiness"}
    assert "document" not in baseline


def test_source_routes_forbid_whole_document_private_table_and_v1_reintroduction() -> None:
    projection_source = inspect.getsource(closeout_projection)
    member_source = inspect.getsource(members)
    fact_source = inspect.getsource(source_facts)

    assert "resolved.document.model_dump" not in projection_source
    assert "_require_schema_owned_topology_membership" not in member_source
    assert "_require_topology_fields" not in member_source
    assert "fields_with_effect" not in member_source
    assert "document.model_dump" not in fact_source
    assert "task_source_fact(" in projection_source
    assert "semantic_topology_source_fact(" in projection_source
    assert "semantic-topology/v1" not in member_source
