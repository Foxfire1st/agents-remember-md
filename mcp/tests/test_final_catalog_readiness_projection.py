"""CCR-R08 repair-loop readiness projection (non-certifying Gate 5 surface).

final_catalog_readiness projection determinism and blocked/fail status
contracts for the repair loop. Split from the original single module
(repository file-size hard limit); the shared fixture scaffold is imported from
test_final_full_memory_coherence_certification.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.errors import FinalCertificationError
from agents_remember.memory_quality.final_certification import (
    FinalCatalogItemIdentity,
    complete_final_catalog,
    final_catalog_readiness,
)
from agents_remember.memory_quality.final_certification.catalog import ReadinessProjectionInput
from agents_remember.memory_quality.style.document_shape import tables
from test_final_full_memory_coherence_certification import _MEMORY_TREE

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))


# ---------------------------------------------------------------------------
# Repair-loop readiness projection
# ---------------------------------------------------------------------------

_PROJECTION_BASE: dict[str, Any] = {
    "executed_checks": {},
    "missing_onboarding_count": 0,
    "stale_route_index_count": 0,
    "candidate_pair_authority_digest": "d" * 64,
    "affected_closure_plan_digest": None,
    "memory_tree": _MEMORY_TREE,
}


def _projection(**overrides: Any) -> dict[str, object]:
    inputs = {**_PROJECTION_BASE}
    inputs.update(overrides)
    return final_catalog_readiness(ReadinessProjectionInput(**inputs))


def _projection_items(
    projection: dict[str, object],
) -> dict[str, dict[str, object]]:
    return {
        cast("dict[str, str]", item["item"])["itemId"]: item
        for item in cast("list[dict[str, object]]", projection["items"])
    }


def test_final_catalog_readiness_projection_is_deterministic_and_non_certifying() -> None:
    scattered = _projection(coherence_status="current", coherence_record_digest="z" * 64)

    assert scattered["finalizationEligible"] is False
    assert scattered["fullFinalRequired"] is True
    items = _projection_items(scattered)
    assert items["affected.closure"]["status"] == "blocked"
    assert items["affected.closure"]["blockedBy"] == ["affected-closure-plan-not-provided"]
    assert items["coherence.record"]["status"] == "pass"
    assert items["candidate.code_memory_pair"]["status"] == "pass"
    assert scattered["coherenceRecordDigest"] == "z" * 64

    again = _projection(coherence_status="current", coherence_record_digest="z" * 64)
    assert json.dumps(scattered, sort_keys=True, separators=(",", ":")) == json.dumps(
        again, sort_keys=True, separators=(",", ":")
    )


def test_final_catalog_readiness_blocks_without_current_coherence() -> None:
    projection = _projection(coherence_status=None, coherence_record_digest=None)
    items = _projection_items(projection)
    assert items["coherence.record"]["status"] == "blocked"
    assert items["coherence.record"]["blockedBy"] == ["coherence-record-not-current"]


def test_final_catalog_readiness_reports_failing_executed_check() -> None:
    failing = {tables.CHECK_NAME: {"ok": False, "findingCount": 2}}
    projection = _projection(
        executed_checks=failing,
        coherence_status="current",
        coherence_record_digest="z" * 64,
    )
    items = _projection_items(projection)
    assert items[tables.CHECK_NAME]["status"] == "fail"
    assert items[tables.CHECK_NAME]["findingCount"] == 2
    assert items[tables.CHECK_NAME]["subresultDigest"] == content_digest(
        {"ok": False, "findingCount": 2}
    )


def test_final_catalog_readiness_projects_present_affected_plan() -> None:
    projection = _projection(affected_closure_plan_digest="b" * 64)
    items = _projection_items(projection)
    assert items["affected.closure"]["status"] == "pass"
    assert items["affected.closure"]["blockedBy"] == []


def test_final_catalog_readiness_refuses_unknown_catalog_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = complete_final_catalog()
    extra = FinalCatalogItemIdentity(itemId="unknown.extra", version="1.0.0")

    monkeypatch.setattr(
        "agents_remember.memory_quality.final_certification.catalog.complete_final_catalog",
        lambda: (*real, extra),
    )
    with pytest.raises(FinalCertificationError) as caught:
        _projection()
    assert caught.value.status == "gate-five-catalog-item-unknown"
