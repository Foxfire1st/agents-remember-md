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
from types import SimpleNamespace
from typing import cast

import pytest
from agents_remember.certification.certificate_models import CoherenceSubrecordIdentity
from agents_remember.certification.digests import content_digest
from agents_remember.errors import FinalCertificationError
from agents_remember.memory_quality.check import AVAILABLE_CHECKS
from agents_remember.memory_quality.final_certification import (
    assemble_gate_five_inputs,
    coherence_subrecords,
    compile_final_catalog_plan,
    complete_final_catalog,
    final_catalog_attestation,
)
from agents_remember.memory_quality.final_certification.catalog import ExecutedFinalCatalog
from agents_remember.memory_quality.final_certification.models import (
    FinalCatalogItemIdentity,
    FinalFullCatalogPlan,
)
from agents_remember.memory_quality.incremental_scope.affected_models import PendingFinalFullCheck
from agents_remember.memory_quality.style.document_shape import tables
from agents_remember.models.lifecycles.curator_coherence import CuratorCoherenceRecord
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


def test_complete_final_catalog_covers_the_whole_memory_checker_registry() -> None:
    catalog = complete_final_catalog()

    check_ids = {item.itemId for item in catalog}
    assert set(AVAILABLE_CHECKS) <= check_ids
    for item in catalog:
        assert item.version == "1.0.0"
    assert [item.key for item in catalog] == sorted(item.key for item in catalog)


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


def test_assemble_gate_five_inputs_binds_exact_certificate_inputs() -> None:
    inputs = assemble_gate_five_inputs(
        memory_tree=_MEMORY_TREE,
        affected_closure_plan_digest="b" * 64,
        checker_registry_digest="c" * 64,
        coherence_subrecords=(
            CoherenceSubrecordIdentity(subrecordId="coherence-record", contentDigest="0f" * 32),
            CoherenceSubrecordIdentity(
                subrecordId="judgment-evidence"
                "-" + "1e" * 18 + "1e",  # deterministic digest-based id
                contentDigest="1e" * 32,
            ),
        ),
        candidate_pair_authority_digest="d" * 64,
    )

    assert inputs.memoryTree.kind == "git-tree"
    assert inputs.memoryTree.value == _MEMORY_TREE
    assert inputs.affectedClosurePlanDigest == "b" * 64
    assert inputs.memoryCheckerRegistryDigest == "c" * 64
    assert len(inputs.coherenceSubrecords) == 2
    assert inputs.coherenceSubrecords[0].subrecordId == "coherence-record"
    assert inputs.coherenceSubrecords[1].subrecordId == "judgment-evidence-" + "1e" * 19
    assert inputs.candidatePairAuthorityDigest == "d" * 64


def test_assemble_gate_five_inputs_refuses_duplicate_subrecords() -> None:
    with pytest.raises(FinalCertificationError) as caught:
        assemble_gate_five_inputs(
            memory_tree=_MEMORY_TREE,
            affected_closure_plan_digest="b" * 64,
            checker_registry_digest="c" * 64,
            coherence_subrecords=(
                CoherenceSubrecordIdentity(subrecordId="coherence-record", contentDigest="0f" * 32),
                CoherenceSubrecordIdentity(subrecordId="coherence-record", contentDigest="1e" * 32),
            ),
            candidate_pair_authority_digest="d" * 64,
        )
    assert caught.value.status == "gate-five-inputs-invalid"


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


def _plan_with_catalog(items: tuple[FinalCatalogItemIdentity, ...]) -> FinalFullCatalogPlan:
    base = _catalog_plan()
    payload = {
        "schemaVersion": "memory-final-full-catalog-plan/v1",
        "candidateCodeTree": base.candidateCodeTree,
        "memoryTree": base.memoryTree,
        "checkerRegistryDigest": base.checkerRegistryDigest,
        "affectedClosurePlanDigest": base.affectedClosurePlanDigest,
        "candidatePairAuthorityDigest": base.candidatePairAuthorityDigest,
        "catalog": [item.model_dump(mode="json") for item in items],
        "coherenceSubrecords": [item.model_dump(mode="json") for item in base.coherenceSubrecords],
    }
    return FinalFullCatalogPlan(
        candidateCodeTree=base.candidateCodeTree,
        memoryTree=base.memoryTree,
        checkerRegistryDigest=base.checkerRegistryDigest,
        affectedClosurePlanDigest=base.affectedClosurePlanDigest,
        candidatePairAuthorityDigest=base.candidatePairAuthorityDigest,
        catalog=items,
        coherenceSubrecords=base.coherenceSubrecords,
        planDigest=content_digest(payload),
    )


def test_complete_catalog_refuses_uncovered_checker_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agents_remember.memory_quality.final_certification.catalog.checker_scope_registry",
        lambda: (),
    )
    with pytest.raises(FinalCertificationError) as caught:
        complete_final_catalog()
    assert caught.value.status == "gate-five-catalog-incomplete"


def test_compile_plan_refuses_empty_coherence_subrecords() -> None:
    _candidate, plan = _affected_plan()
    pair = _pair()
    with pytest.raises(FinalCertificationError) as caught:
        compile_final_catalog_plan(
            candidate_code_tree=_CODE_TREE,
            memory_tree=_MEMORY_TREE,
            affected_closure=plan,
            coherence_subrecords=(),
            candidate_pair_authority_digest=pair.contractDigest,
        )
    assert caught.value.status == "gate-five-coherence-subrecords-missing"


def test_compile_plan_refuses_affected_closure_code_tree_mismatch() -> None:
    _candidate, plan = _affected_plan()
    pair = _pair()
    with pytest.raises(FinalCertificationError) as caught:
        compile_final_catalog_plan(
            candidate_code_tree="f" * 40,
            memory_tree=_MEMORY_TREE,
            affected_closure=plan,
            coherence_subrecords=(
                CoherenceSubrecordIdentity(subrecordId="coherence-record", contentDigest="0f" * 32),
            ),
            candidate_pair_authority_digest=pair.contractDigest,
        )
    assert caught.value.status == "gate-five-affected-closure-code-tree-mismatch"


def test_compile_plan_refuses_non_accepting_affected_disposition() -> None:
    _candidate, plan = _affected_plan()
    pair = _pair()
    invalid = plan.model_copy(update={"acceptanceEligible": True})
    with pytest.raises(FinalCertificationError) as caught:
        compile_final_catalog_plan(
            candidate_code_tree=_CODE_TREE,
            memory_tree=_MEMORY_TREE,
            affected_closure=invalid,
            coherence_subrecords=(
                CoherenceSubrecordIdentity(subrecordId="coherence-record", contentDigest="0f" * 32),
            ),
            candidate_pair_authority_digest=pair.contractDigest,
        )
    assert caught.value.status == "gate-five-affected-closure-disposition-invalid"


def test_compile_plan_refuses_uncovered_pending_full_only_population() -> None:
    _candidate, plan = _affected_plan()
    pair = _pair()
    extra = PendingFinalFullCheck(checker="edge.full-only-extra", reason="edge coverage")
    invalid = plan.model_copy(update={"pendingFinalFull": (*plan.pendingFinalFull, extra)})
    with pytest.raises(FinalCertificationError) as caught:
        compile_final_catalog_plan(
            candidate_code_tree=_CODE_TREE,
            memory_tree=_MEMORY_TREE,
            affected_closure=invalid,
            coherence_subrecords=(
                CoherenceSubrecordIdentity(subrecordId="coherence-record", contentDigest="0f" * 32),
            ),
            candidate_pair_authority_digest=pair.contractDigest,
        )
    assert caught.value.status == "gate-five-full-only-population-incomplete"


def test_attestation_refuses_plan_owning_unknown_item() -> None:
    base = _catalog_plan()
    extra = FinalCatalogItemIdentity(itemId="unknown.extra", version="1.0.0")
    plan = _plan_with_catalog((*base.catalog, extra))
    with pytest.raises(FinalCertificationError) as caught:
        final_catalog_attestation(
            plan,
            ExecutedFinalCatalog(
                executed_checks=_passing_checks(),
                coherence_record_digest="z" * 64,
            ),
        )
    assert caught.value.status == "gate-five-catalog-item-unknown"


def test_attestation_refuses_plan_without_standard_checks() -> None:
    extra = FinalCatalogItemIdentity(itemId="unknown.extra", version="1.0.0")
    plan = _plan_with_catalog((extra,))
    with pytest.raises(FinalCertificationError) as caught:
        final_catalog_attestation(
            plan,
            ExecutedFinalCatalog(
                executed_checks=_passing_checks(),
                coherence_record_digest="z" * 64,
            ),
        )
    assert caught.value.status == "gate-five-executed-population-empty"


def test_coherence_subrecords_cover_judgment_evidence() -> None:
    record = cast(
        CuratorCoherenceRecord,
        SimpleNamespace(
            judgments=(
                SimpleNamespace(evidenceRef="evidence://judgment/one", evidenceSha256="aa" * 32),
                SimpleNamespace(evidenceRef="evidence://judgment/two", evidenceSha256="bb" * 32),
            )
        ),
    )
    subrecords = coherence_subrecords(
        record_digest="a" * 64,
        record=record,
        affected_subrecords=(),
    )
    identities = tuple(item.subrecordId for item in subrecords)
    assert identities[0] == "coherence-record"
    assert len(identities) == 3
    assert all(item.startswith("judgment-evidence-") for item in identities[1:])
    assert identities == tuple(sorted(identities))


def test_assemble_gate_five_inputs_refuses_empty_subrecords() -> None:
    with pytest.raises(FinalCertificationError) as caught:
        assemble_gate_five_inputs(
            memory_tree=_MEMORY_TREE,
            affected_closure_plan_digest="b" * 64,
            checker_registry_digest="c" * 64,
            coherence_subrecords=(),
            candidate_pair_authority_digest="d" * 64,
        )
    assert caught.value.status == "gate-five-coherence-subrecords-missing"
