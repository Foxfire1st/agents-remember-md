"""CCR-R08 closed final-certification model and application refusal edges.

Coverage of the closed final_certification.models validators, the bounded
FinalCertificationError.response_fields projection, and the controller's
final-full-catalog pair-identity guard. These are the exact refusal edges of the
Gate-5 typed contracts; every negative construction here asserts a ValueError
(refusal) rather than a fallback. Shared fixtures are imported from
test_final_full_memory_coherence_certification.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents_remember.application.memory_quality import controller
from agents_remember.certification.certificate_models import (
    CoherenceSubrecordIdentity,
    GateCertificateIdentity,
    GateFiveSemanticInputs,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CandidateIdentity
from agents_remember.errors import FinalCertificationError
from agents_remember.memory_quality.final_certification import (
    complete_final_catalog,
    final_catalog_attestation,
)
from agents_remember.memory_quality.final_certification.catalog import ExecutedFinalCatalog
from agents_remember.memory_quality.final_certification.certify import (
    certify_final_full_memory_coherence,
)
from agents_remember.memory_quality.final_certification.models import (
    FinalCatalogItemIdentity,
    FinalCatalogItemResult,
    FinalCertificationResult,
    FinalFullCatalogAttestation,
    FinalFullCatalogPlan,
)
from agents_remember.memory_quality.style.document_shape import tables
from test_final_full_memory_coherence_certification import _evidence, _passing_checks

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))


def _first_item() -> FinalCatalogItemIdentity:
    return complete_final_catalog()[0]


def _subrecord() -> CoherenceSubrecordIdentity:
    return CoherenceSubrecordIdentity(subrecordId="coherence-record", contentDigest="0f" * 32)


def green() -> FinalCertificationResult:
    """One validated green final certification used as the mutation base."""
    return certify_final_full_memory_coherence(_evidence())


def _inputs(memory_kind: str, memory_value: str) -> GateFiveSemanticInputs:
    """One Gate-5 semantic input bundle built without revalidation (edge injection)."""
    return GateFiveSemanticInputs.model_construct(
        memoryTree=CandidateIdentity(kind=memory_kind, value=memory_value),
        affectedClosurePlanDigest="b" * 64,
        memoryCheckerRegistryDigest="c" * 64,
        coherenceSubrecords=(_subrecord(),),
        candidatePairAuthorityDigest="d" * 64,
    )


def _rebuild_plan(
    base_plan: FinalFullCatalogPlan,
    *,
    memory_tree: str | None = None,
    extra_subrecords: tuple[CoherenceSubrecordIdentity, ...] = (),
) -> FinalFullCatalogPlan:
    """A FinalFullCatalogPlan that stays internally valid over altered content."""
    subrecords = (*base_plan.coherenceSubrecords, *extra_subrecords)
    memory = memory_tree or base_plan.memoryTree
    payload = {
        "schemaVersion": base_plan.schemaVersion,
        "candidateCodeTree": base_plan.candidateCodeTree,
        "memoryTree": memory,
        "checkerRegistryDigest": base_plan.checkerRegistryDigest,
        "affectedClosurePlanDigest": base_plan.affectedClosurePlanDigest,
        "candidatePairAuthorityDigest": base_plan.candidatePairAuthorityDigest,
        "catalog": [item.model_dump(mode="json") for item in base_plan.catalog],
        "coherenceSubrecords": [item.model_dump(mode="json") for item in subrecords],
    }
    return FinalFullCatalogPlan(
        candidateCodeTree=base_plan.candidateCodeTree,
        memoryTree=memory,
        checkerRegistryDigest=base_plan.checkerRegistryDigest,
        affectedClosurePlanDigest=base_plan.affectedClosurePlanDigest,
        candidatePairAuthorityDigest=base_plan.candidatePairAuthorityDigest,
        catalog=base_plan.catalog,
        coherenceSubrecords=subrecords,
        planDigest=content_digest(payload),
    )


def _red(
    plan: FinalFullCatalogPlan,
    attestation: FinalFullCatalogAttestation,
    *,
    inputs: GateFiveSemanticInputs | None = None,
    finalization_eligible: bool = False,
) -> FinalCertificationResult:
    return FinalCertificationResult(
        state="red",
        reason="edge",
        plan=plan,
        attestation=attestation,
        gateFiveInputs=inputs,
        coherenceRecordDigest=None,
        reusedGateOneToFour=(),
        certificateReusePlanDigest=None,
        finalizationEligible=finalization_eligible,
    )


# ---------------------------------------------------------------------------
# FinalCatalogItemResult status-shape refusals
# ---------------------------------------------------------------------------


def test_item_result_refuses_blocked_without_blocked_by() -> None:
    with pytest.raises(ValueError, match="blocked"):
        FinalCatalogItemResult(
            item=_first_item(),
            status="blocked",
            findingCount=0,
            subresultDigest="a" * 64,
        )


def test_item_result_refuses_passing_with_findings() -> None:
    with pytest.raises(ValueError, match="passing final-catalog item cannot carry findings"):
        FinalCatalogItemResult(
            item=_first_item(),
            status="pass",
            findingCount=1,
            subresultDigest="a" * 64,
        )


def test_item_result_refuses_not_applicable_with_findings() -> None:
    with pytest.raises(ValueError, match="not-applicable final-catalog item cannot carry findings"):
        FinalCatalogItemResult(
            item=_first_item(),
            status="not-applicable",
            findingCount=1,
            subresultDigest="a" * 64,
        )


# ---------------------------------------------------------------------------
# FinalFullCatalogPlan canonicality and digest refusals
# ---------------------------------------------------------------------------


def test_catalog_plan_refuses_wrong_plan_digest() -> None:
    with pytest.raises(ValueError, match="digest does not match its content"):
        FinalFullCatalogPlan(
            candidateCodeTree="c" * 40,
            memoryTree="7" * 40,
            checkerRegistryDigest="a" * 64,
            affectedClosurePlanDigest="b" * 64,
            candidatePairAuthorityDigest="d" * 64,
            catalog=(_first_item(),),
            coherenceSubrecords=(_subrecord(),),
            planDigest="0" * 64,
        )


def test_catalog_plan_refuses_duplicate_items() -> None:
    item = _first_item()
    with pytest.raises(ValueError, match="unique and canonical"):
        FinalFullCatalogPlan(
            candidateCodeTree="c" * 40,
            memoryTree="7" * 40,
            checkerRegistryDigest="a" * 64,
            affectedClosurePlanDigest="b" * 64,
            candidatePairAuthorityDigest="d" * 64,
            catalog=(item, item),
            coherenceSubrecords=(_subrecord(),),
            planDigest="0" * 64,
        )


# ---------------------------------------------------------------------------
# FinalFullCatalogAttestation population refusals
# ---------------------------------------------------------------------------


def test_attestation_refuses_inexact_planned_population() -> None:
    base = green()
    dump = base.attestation.model_dump(mode="json")
    extra = FinalCatalogItemIdentity(itemId="unknown.extra", version="1.0.0")
    dump["plannedCatalog"] = [*dump["plannedCatalog"], extra.model_dump(mode="json")]
    with pytest.raises(ValueError, match="exhaust exactly its planned population"):
        FinalFullCatalogAttestation.model_validate(dump)


def test_attestation_refuses_derived_status_counts_mismatch() -> None:
    base = green()
    dump = base.attestation.model_dump(mode="json")
    dump["statusCounts"] = {**dump["statusCounts"], "pass": dump["statusCounts"]["pass"] + 1}
    with pytest.raises(ValueError, match="status counts must derive"):
        FinalFullCatalogAttestation.model_validate(dump)


def test_attestation_refuses_ok_without_red_or_blocked() -> None:
    base = green()
    dump = base.attestation.model_dump(mode="json")
    dump["ok"] = False
    with pytest.raises(ValueError, match="ok must reflect every red or blocked item"):
        FinalFullCatalogAttestation.model_validate(dump)


# ---------------------------------------------------------------------------
# FinalCertificationResult memory binding refusals
# ---------------------------------------------------------------------------


def test_result_refuses_attested_memory_tree_mismatch() -> None:
    base = green()
    plan = _rebuild_plan(base.plan, memory_tree="9" * 40)
    with pytest.raises(ValueError, match="exact attested memory tree"):
        _red(plan, base.attestation)


def test_result_refuses_plan_digest_mismatch() -> None:
    base = green()
    plan = _rebuild_plan(
        base.plan,
        extra_subrecords=(
            CoherenceSubrecordIdentity(subrecordId="zz.secondary", contentDigest="aa" * 32),
        ),
    )
    with pytest.raises(ValueError, match="bind its exact catalog plan"):
        _red(plan, base.attestation)


def test_result_refuses_non_git_tree_memory_input() -> None:
    base = green()
    with pytest.raises(ValueError, match="exact Git tree"):
        _red(base.plan, base.attestation, inputs=_inputs("git-blob", base.plan.memoryTree))


def test_result_refuses_unbound_memory_input_value() -> None:
    base = green()
    with pytest.raises(ValueError, match="exact bound tree"):
        _red(base.plan, base.attestation, inputs=_inputs("git-tree", "f" * 40))


def test_result_refuses_red_finalization_eligibility() -> None:
    base = green()
    with pytest.raises(ValueError, match="only a green final certification"):
        _red(base.plan, base.attestation, finalization_eligible=True)


# ---------------------------------------------------------------------------
# FinalCertificationResult green-state refusals
# ---------------------------------------------------------------------------


_GREEN_BASE = object()


def _green_variant(
    *,
    attestation: FinalFullCatalogAttestation | object = _GREEN_BASE,
    inputs: GateFiveSemanticInputs | object = _GREEN_BASE,
    coherence_digest: str | object = _GREEN_BASE,
    reused: tuple[GateCertificateIdentity, ...] | object = _GREEN_BASE,
    finalization_eligible: bool = True,
) -> FinalCertificationResult:
    """A green FinalCertificationResult over one targeted field override."""
    base = green()
    return FinalCertificationResult(
        state="green",
        reason=None,
        plan=base.plan,
        attestation=(
            base.attestation
            if attestation is _GREEN_BASE
            else cast(FinalFullCatalogAttestation, attestation)
        ),
        gateFiveInputs=(
            base.gateFiveInputs
            if inputs is _GREEN_BASE
            else cast(GateFiveSemanticInputs | None, inputs)
        ),
        coherenceRecordDigest=(
            base.coherenceRecordDigest
            if coherence_digest is _GREEN_BASE
            else cast(str | None, coherence_digest)
        ),
        reusedGateOneToFour=(
            base.reusedGateOneToFour
            if reused is _GREEN_BASE
            else cast(tuple[GateCertificateIdentity, ...], reused)
        ),
        certificateReusePlanDigest=base.certificateReusePlanDigest,
        finalizationEligible=finalization_eligible,
    )


def test_green_result_refuses_non_passing_catalog() -> None:
    base = green()
    checks = _passing_checks()
    checks[tables.CHECK_NAME] = {"ok": False, "findingCount": 1}
    red_attestation = final_catalog_attestation(
        base.plan,
        ExecutedFinalCatalog(
            executed_checks=checks,
            coherence_record_digest=base.coherenceRecordDigest,
        ),
    )
    assert red_attestation.ok is False
    with pytest.raises(ValueError, match="fully passing catalog"):
        _green_variant(attestation=red_attestation)


def test_green_result_requires_assembled_gate_five_inputs() -> None:
    with pytest.raises(ValueError, match="assembled Gate-5 inputs"):
        _green_variant(inputs=None)


def test_green_result_requires_current_coherence_record() -> None:
    with pytest.raises(ValueError, match="current coherence record"):
        _green_variant(coherence_digest=None)


def test_green_result_requires_reused_gate_one_to_four() -> None:
    with pytest.raises(ValueError, match="green Gate 1-4 prefix"):
        _green_variant(reused=())


def test_green_result_requires_finalization_eligibility() -> None:
    with pytest.raises(ValueError, match="finalization-eligible"):
        _green_variant(finalization_eligible=False)


# ---------------------------------------------------------------------------
# FinalCertificationError bounded response projection
# ---------------------------------------------------------------------------


def test_final_certification_error_response_fields_bounded() -> None:
    plain = FinalCertificationError("gate-five-prefix-stale", "detail text").response_fields()
    assert plain["certificationStatus"] == "gate-five-prefix-stale"
    assert plain["detail"] == "detail text"
    assert plain["nextAction"] == "memory_quality_check"
    assert "expected" not in plain
    assert "observed" not in plain

    expected = FinalCertificationError("s", "d", expected={"a": 1}).response_fields()
    assert expected["expected"] == {"a": 1}
    assert "observed" not in expected

    observed = FinalCertificationError("s", "d", observed={"b": 2}).response_fields()
    assert observed["observed"] == {"b": 2}
    assert "expected" not in observed

    both = FinalCertificationError("s", "d", expected={"a": 1}, observed={"b": 2}).response_fields()
    assert both["expected"] == {"a": 1}
    assert both["observed"] == {"b": 2}


# ---------------------------------------------------------------------------
# Controller final-full-catalog pair-identity guard
# ---------------------------------------------------------------------------


def test_controller_projection_requires_exact_pair_identity() -> None:
    scope = cast(Any, SimpleNamespace(pair_identity=None))
    with pytest.raises(RuntimeError, match="requires the exact pair identity"):
        controller._attach_final_full_catalog(  # pyright: ignore[reportPrivateUsage]
            scope,
            {},
            candidate_inputs=cast(Any, None),
            missing_onboarding={},
            stale_route_indexes=[],
        )
