"""CCR-R08 gate-five final catalog plan, attestation, and semantic inputs.

Complete final catalog registry coverage, compile_final_catalog_plan /
final_catalog_attestation population contracts, gate-five semantic-input
assembly, and coherence-subrecord coverage. Split from the original single
module (repository file-size hard limit); the shared fixture scaffold is
imported from test_final_full_memory_coherence_certification.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from agents_remember.certification.certificate_models import CoherenceSubrecordIdentity
from agents_remember.errors import FinalCertificationError
from agents_remember.memory_quality.final_certification import (
    coherence_subrecords,
    compile_final_catalog_plan,
    final_catalog_attestation,
)
from agents_remember.memory_quality.final_certification.catalog import ExecutedFinalCatalog
from agents_remember.memory_quality.final_certification.models import (
    FinalFullCatalogPlan,
)
from agents_remember.memory_quality.style.document_shape import tables
from test_final_full_memory_coherence_certification import (
    _CODE_TREE,
    _MEMORY_TREE,
    _affected_plan,
    _coherence,
    _pair,
    _passing_checks,
)

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))


# ---------------------------------------------------------------------------
# Complete final catalog plan and attestation
# ---------------------------------------------------------------------------


def _catalog_plan() -> FinalFullCatalogPlan:
    _candidate, plan = _affected_plan()
    pair = _pair()
    return compile_final_catalog_plan(
        candidate_code_tree=_CODE_TREE,
        memory_tree=_MEMORY_TREE,
        affected_closure=plan,
        coherence_subrecords=(
            CoherenceSubrecordIdentity(subrecordId="coherence-record", contentDigest="0f" * 32),
        ),
        candidate_pair_authority_digest=pair.contractDigest,
    )


def test_attestation_must_exhaust_the_planned_population() -> None:
    catalog_plan = _catalog_plan()
    checks = _passing_checks()
    del checks[tables.CHECK_NAME]

    with pytest.raises(FinalCertificationError) as caught:
        final_catalog_attestation(
            catalog_plan,
            ExecutedFinalCatalog(
                executed_checks=checks,
                coherence_record_digest="z" * 64,
            ),
        )
    assert caught.value.status == "gate-five-executed-population-incomplete"


def test_attestation_green_and_red_and_blocked() -> None:
    catalog_plan = _catalog_plan()
    base = ExecutedFinalCatalog(
        executed_checks=_passing_checks(),
        coherence_record_digest="z" * 64,
    )

    green = final_catalog_attestation(catalog_plan, base)
    assert green.ok is True
    assert green.statusCounts == {
        "pass": len(catalog_plan.catalog),
        "fail": 0,
        "blocked": 0,
        "not-applicable": 0,
    }
    assert green.fullFinalCompleted is True

    red_checks = _passing_checks()
    red_checks[tables.CHECK_NAME] = {"ok": False, "findingCount": 1}
    red = final_catalog_attestation(
        catalog_plan,
        ExecutedFinalCatalog(
            executed_checks=red_checks,
            missing_onboarding_count=1,
            coherence_record_digest="z" * 64,
        ),
    )
    assert red.ok is False
    assert red.statusCounts["fail"] == 2

    blocked = final_catalog_attestation(
        catalog_plan,
        ExecutedFinalCatalog(
            executed_checks=_passing_checks(),
            affected_closure_status="blocked",
            full_only_rerun=False,
            coherence_record_digest="z" * 64,
        ),
    )
    assert blocked.ok is False
    assert blocked.statusCounts["blocked"] == 1
    affected_result = next(
        item for item in blocked.catalog if item.item.itemId == "affected.closure"
    )
    assert affected_result.blockedBy == ("final-full-only-rerun-missing",)


def test_plan_refuses_affected_closure_bound_to_another_memory_tree() -> None:
    _candidate, plan = _affected_plan()
    pair = _pair()

    with pytest.raises(FinalCertificationError) as caught:
        compile_final_catalog_plan(
            candidate_code_tree=_CODE_TREE,
            memory_tree="9" * 40,
            affected_closure=plan,
            coherence_subrecords=(
                CoherenceSubrecordIdentity(subrecordId="coherence-record", contentDigest="0f" * 32),
            ),
            candidate_pair_authority_digest=pair.contractDigest,
        )
    assert caught.value.status == "gate-five-affected-closure-memory-tree-mismatch"


# ---------------------------------------------------------------------------
# Gate-5 semantic-input assembly and coherence subrecords
# ---------------------------------------------------------------------------


def test_coherence_subrecords_require_affected_coverage() -> None:
    pair = _pair()
    coherence = _coherence(pair)
    subrecords = coherence_subrecords(
        record_digest=coherence.record_digest,
        record=coherence.record,
        affected_subrecords=(),
    )
    assert tuple(item.subrecordId for item in subrecords) == ("coherence-record",)

    with pytest.raises(FinalCertificationError) as caught:
        coherence_subrecords(
            record_digest=coherence.record_digest,
            record=coherence.record,
            affected_subrecords=("memory:missing.md",),
        )
    assert caught.value.status == "gate-five-affected-coherence-subrecords-uncovered"
    assert caught.value.observed["uncoveredSubrecords"] == ["memory:missing.md"]


# ---------------------------------------------------------------------------
# Complete catalog plan and attestation refusal edges
# ---------------------------------------------------------------------------
