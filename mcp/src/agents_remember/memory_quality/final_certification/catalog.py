"""Deterministic complete final catalog plan and executed attestation (CCR-R08).

The complete Gate-5 catalog is the exhaustion surface: every applicable memory check
returns pass, fail, blocked, or not-applicable, and the final attestation proves it ran
exactly the planned population -- never a weakened incremental-only acceptance. The
catalog spans the existing memory checkers (drift + style), the missing-onboarding and
route-index alignment owners, the R07 affected-closure plan, the canonical
curator-coherence record, and the exact code/memory candidate pair.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from agents_remember.certification.certificate_models import CoherenceSubrecordIdentity
from agents_remember.certification.digests import content_digest
from agents_remember.errors import FinalCertificationError
from agents_remember.memory_quality.check import AVAILABLE_CHECKS, DRIFT_CHECK_NAME
from agents_remember.memory_quality.incremental_scope.affected_models import (
    AffectedClosurePlan,
)
from agents_remember.memory_quality.incremental_scope.registry import (
    checker_registry_version,
    checker_scope_registry,
)
from agents_remember.memory_quality.style.citations import claim_reopen, range_resolution
from agents_remember.memory_quality.style.document_shape import (
    diff_markers,
    entity_catalog_alignment,
    tables,
)
from agents_remember.memory_quality.style.update_history import history_order

from .models import (
    FinalCatalogItemIdentity,
    FinalCatalogItemResult,
    FinalFullCatalogAttestation,
    FinalFullCatalogPlan,
    FinalItemStatus,
)

FINAL_FULL_CATALOG_VERSION = "1.0.0"

MISSING_ONBOARDING_ITEM_ID = "integrity.missing_onboarding"
ROUTE_INDEX_ALIGNMENT_ITEM_ID = "route_index.alignment"
AFFECTED_CLOSURE_ITEM_ID = "affected.closure"
COHERENCE_RECORD_ITEM_ID = "coherence.record"
CANDIDATE_PAIR_ITEM_ID = "candidate.code_memory_pair"

_STANDARD_CHECK_IDS: tuple[str, ...] = (
    DRIFT_CHECK_NAME,
    claim_reopen.CHECK_NAME,
    diff_markers.CHECK_NAME,
    entity_catalog_alignment.CHECK_NAME,
    history_order.CHECK_NAME,
    range_resolution.CHECK_NAME,
    tables.CHECK_NAME,
)
_STANDARD_CHECK_ID_SET = frozenset(_STANDARD_CHECK_IDS)


@dataclass(frozen=True)
class ExecutedFinalCatalog:
    """One executed complete catalog plus the authority statuses it observed."""

    executed_checks: Mapping[str, Mapping[str, Any]]
    missing_onboarding_count: int = 0
    stale_route_index_count: int = 0
    affected_closure_status: FinalItemStatus = "pass"
    coherence_status: FinalItemStatus = "pass"
    pair_status: FinalItemStatus = "pass"
    full_only_rerun: bool = True
    coherence_record_digest: str | None = None


@dataclass(frozen=True)
class ReadinessProjectionInput:
    """The repair-loop view of one full run; never certification-eligible."""

    executed_checks: Mapping[str, Mapping[str, Any]]
    missing_onboarding_count: int = 0
    stale_route_index_count: int = 0
    coherence_status: str | None = None
    coherence_record_digest: str | None = None
    candidate_pair_authority_digest: str = "0" * 64
    affected_closure_plan_digest: str | None = None
    memory_tree: str | None = None


def complete_final_catalog() -> tuple[FinalCatalogItemIdentity, ...]:
    """Return the closed deterministic Gate-5 catalog population (canonical order)."""

    declared = {policy.checker for policy in checker_scope_registry()}
    if declared != set(AVAILABLE_CHECKS):
        raise FinalCertificationError(
            "gate-five-catalog-incomplete",
            "the final full catalog must cover the complete current memory checker registry",
            expected={"checkerRegistry": sorted(AVAILABLE_CHECKS)},
            observed={"checkerRegistry": sorted(declared)},
        )
    items = [
        *(
            FinalCatalogItemIdentity(itemId=check_id, version=FINAL_FULL_CATALOG_VERSION)
            for check_id in _STANDARD_CHECK_IDS
        ),
        FinalCatalogItemIdentity(
            itemId=MISSING_ONBOARDING_ITEM_ID, version=FINAL_FULL_CATALOG_VERSION
        ),
        FinalCatalogItemIdentity(
            itemId=ROUTE_INDEX_ALIGNMENT_ITEM_ID, version=FINAL_FULL_CATALOG_VERSION
        ),
        FinalCatalogItemIdentity(
            itemId=AFFECTED_CLOSURE_ITEM_ID, version=FINAL_FULL_CATALOG_VERSION
        ),
        FinalCatalogItemIdentity(
            itemId=COHERENCE_RECORD_ITEM_ID, version=FINAL_FULL_CATALOG_VERSION
        ),
        FinalCatalogItemIdentity(itemId=CANDIDATE_PAIR_ITEM_ID, version=FINAL_FULL_CATALOG_VERSION),
    ]
    return tuple(sorted(items, key=lambda item: item.key))


def compile_final_catalog_plan(
    *,
    candidate_code_tree: str,
    memory_tree: str,
    affected_closure: AffectedClosurePlan,
    coherence_subrecords: Sequence[CoherenceSubrecordIdentity],
    candidate_pair_authority_digest: str,
) -> FinalFullCatalogPlan:
    """Compile the exact deterministic plan bound to one candidate pair."""

    _require_affected_closure_bound(affected_closure, candidate_code_tree, memory_tree)
    catalog = complete_final_catalog()
    _require_pending_full_only_population(affected_closure, catalog)
    subrecords = tuple(coherence_subrecords)
    if not subrecords:
        raise FinalCertificationError(
            "gate-five-coherence-subrecords-missing",
            "the final full catalog requires at least one canonical coherence subrecord",
            next_action="curator_coherence",
        )
    payload = {
        "schemaVersion": "memory-final-full-catalog-plan/v1",
        "candidateCodeTree": candidate_code_tree,
        "memoryTree": memory_tree,
        "checkerRegistryDigest": checker_registry_version(),
        "affectedClosurePlanDigest": affected_closure.planDigest,
        "candidatePairAuthorityDigest": candidate_pair_authority_digest,
        "catalog": [item.model_dump(mode="json") for item in catalog],
        "coherenceSubrecords": [item.model_dump(mode="json") for item in subrecords],
    }
    return FinalFullCatalogPlan(
        candidateCodeTree=candidate_code_tree,
        memoryTree=memory_tree,
        checkerRegistryDigest=payload["checkerRegistryDigest"],
        affectedClosurePlanDigest=affected_closure.planDigest,
        candidatePairAuthorityDigest=candidate_pair_authority_digest,
        catalog=catalog,
        coherenceSubrecords=subrecords,
        planDigest=content_digest(payload),
    )


def final_catalog_attestation(
    plan: FinalFullCatalogPlan,
    run: ExecutedFinalCatalog,
) -> FinalFullCatalogAttestation:
    """Map one executed complete catalog into the exhaustive typed attestation."""

    _require_executed_population(run.executed_checks, plan)
    results: list[FinalCatalogItemResult] = []
    for item in plan.catalog:
        item_id = item.itemId
        if item_id in _STANDARD_CHECK_ID_SET:
            results.append(_standard_result(item, run.executed_checks[item_id]))
        elif item_id == MISSING_ONBOARDING_ITEM_ID:
            results.append(_count_result(item, run.missing_onboarding_count, "missing-onboarding"))
        elif item_id == ROUTE_INDEX_ALIGNMENT_ITEM_ID:
            results.append(_count_result(item, run.stale_route_index_count, "stale-route-index"))
        elif item_id == AFFECTED_CLOSURE_ITEM_ID:
            blocked_by = ("final-full-only-rerun-missing",) if not run.full_only_rerun else ()
            results.append(
                _authority_result(
                    item,
                    run.affected_closure_status,
                    plan.affectedClosurePlanDigest,
                    blocked_by=blocked_by,
                )
            )
        elif item_id == COHERENCE_RECORD_ITEM_ID:
            digest = run.coherence_record_digest
            blocked_by = ("coherence-record-unavailable",) if digest is None else ()
            results.append(
                _authority_result(
                    item,
                    run.coherence_status,
                    digest or plan.affectedClosurePlanDigest,
                    blocked_by=blocked_by,
                )
            )
        elif item_id == CANDIDATE_PAIR_ITEM_ID:
            results.append(
                _authority_result(
                    item,
                    run.pair_status,
                    plan.candidatePairAuthorityDigest,
                    blocked_by=(),
                )
            )
        else:
            _refuse_unknown_item(item_id)
    finding_count = sum(item.findingCount for item in results)
    counts = {
        status: sum(1 for item in results if item.status == status)
        for status in ("pass", "fail", "blocked", "not-applicable")
    }
    payload = {
        "schemaVersion": "memory-final-full-catalog-attestation/v1",
        "planDigest": plan.planDigest,
        "codeTree": plan.candidateCodeTree,
        "memoryTree": plan.memoryTree,
        "plannedCatalog": [item.model_dump(mode="json") for item in plan.catalog],
        "catalog": [item.model_dump(mode="json") for item in results],
        "findingCount": finding_count,
        "statusCounts": counts,
        "ok": counts["fail"] == 0 and counts["blocked"] == 0,
        "fullFinalCompleted": True,
    }
    return FinalFullCatalogAttestation.model_validate(payload)


def final_catalog_readiness(inputs: ReadinessProjectionInput) -> dict[str, object]:
    """Deterministic Gate-5 surface projection for the repair-loop entry point.

    The interactive memory_quality_check full run is the execution surface; it cannot
    hold the R21 certificates or the R07 affected-closure plan, so it projects the exact
    complete catalog population and every item's status without claiming certification
    eligibility. blocked items name the exact missing authority. The projection is
    byte-deterministic so the certification executor can compare it with the attested
    final catalog.
    """

    catalog = complete_final_catalog()
    items: list[dict[str, object]] = []
    for item in catalog:
        item_id = item.itemId
        if item_id in _STANDARD_CHECK_ID_SET:
            items.append(
                _project_item(_standard_result(item, inputs.executed_checks.get(item_id, {})))
            )
        elif item_id == MISSING_ONBOARDING_ITEM_ID:
            items.append(
                _project_item(
                    _count_result(item, inputs.missing_onboarding_count, "missing-onboarding")
                )
            )
        elif item_id == ROUTE_INDEX_ALIGNMENT_ITEM_ID:
            items.append(
                _project_item(
                    _count_result(item, inputs.stale_route_index_count, "stale-route-index")
                )
            )
        elif item_id == AFFECTED_CLOSURE_ITEM_ID:
            if inputs.affected_closure_plan_digest is None:
                items.append(
                    _project_item(
                        _authority_result(
                            item,
                            "blocked",
                            "0" * 64,
                            blocked_by=("affected-closure-plan-not-provided",),
                        )
                    )
                )
            else:
                items.append(
                    _project_item(
                        _authority_result(
                            item,
                            "pass",
                            inputs.affected_closure_plan_digest,
                            blocked_by=(),
                        )
                    )
                )
        elif item_id == COHERENCE_RECORD_ITEM_ID:
            current = (
                inputs.coherence_status == "current" and inputs.coherence_record_digest is not None
            )
            status: FinalItemStatus = "pass" if current else "blocked"
            items.append(
                _project_item(
                    _authority_result(
                        item,
                        status,
                        inputs.coherence_record_digest or "0" * 64,
                        blocked_by=(() if current else ("coherence-record-not-current",)),
                    )
                )
            )
        elif item_id == CANDIDATE_PAIR_ITEM_ID:
            items.append(
                _project_item(
                    _authority_result(
                        item,
                        "pass",
                        inputs.candidate_pair_authority_digest,
                        blocked_by=(),
                    )
                )
            )
        else:
            _refuse_unknown_item(item_id)
    return {
        "schemaVersion": "memory-final-full-catalog-readiness/v1",
        "catalog": [item.model_dump(mode="json") for item in catalog],
        "items": items,
        "checkerRegistryDigest": checker_registry_version(),
        "candidatePairAuthorityDigest": inputs.candidate_pair_authority_digest,
        "affectedClosurePlanDigest": inputs.affected_closure_plan_digest,
        "memoryTree": inputs.memory_tree,
        "coherenceRecordDigest": inputs.coherence_record_digest,
        "finalizationEligible": False,
        "fullFinalRequired": True,
    }


def _standard_result(
    item: FinalCatalogItemIdentity,
    result: Mapping[str, Any],
) -> FinalCatalogItemResult:
    finding_count = int(result.get("findingCount", 0) or 0)
    ok = bool(result.get("ok", False))
    status: FinalItemStatus = "pass" if ok else "fail"
    return FinalCatalogItemResult(
        item=item,
        status=status,
        findingCount=finding_count,
        subresultDigest=content_digest(dict(result)),
        reference=result.get("reportPath"),
    )


def _count_result(
    item: FinalCatalogItemIdentity,
    count: int,
    code: str,
) -> FinalCatalogItemResult:
    status: FinalItemStatus = "pass" if count == 0 else "fail"
    return FinalCatalogItemResult(
        item=item,
        status=status,
        findingCount=count,
        subresultDigest=content_digest({"count": count, "item": item.itemId, "code": code}),
    )


def _authority_result(
    item: FinalCatalogItemIdentity,
    status: FinalItemStatus,
    authority_digest: str,
    *,
    blocked_by: tuple[str, ...],
) -> FinalCatalogItemResult:
    if status == "blocked":
        return FinalCatalogItemResult(
            item=item,
            status="blocked",
            findingCount=0,
            subresultDigest=content_digest(
                {"item": item.itemId, "authorityDigest": authority_digest, "status": "blocked"}
            ),
            blockedBy=blocked_by,
        )
    return FinalCatalogItemResult(
        item=item,
        status=status,
        findingCount=0 if status == "pass" else 1,
        subresultDigest=content_digest(
            {"item": item.itemId, "authorityDigest": authority_digest, "status": status}
        ),
    )


def _project_item(result: FinalCatalogItemResult) -> dict[str, object]:
    return result.model_dump(mode="json")


def _require_affected_closure_bound(
    affected_closure: AffectedClosurePlan,
    candidate_code_tree: str,
    memory_tree: str,
) -> None:
    if affected_closure.codeTree != candidate_code_tree:
        raise FinalCertificationError(
            "gate-five-affected-closure-code-tree-mismatch",
            "the affected-closure plan is bound to a different code tree",
            expected={"codeTree": candidate_code_tree},
            observed={"codeTree": affected_closure.codeTree},
            next_action="memory_quality_check",
        )
    if affected_closure.memoryTree != memory_tree:
        raise FinalCertificationError(
            "gate-five-affected-closure-memory-tree-mismatch",
            "the affected-closure plan is bound to a different memory tree",
            expected={"memoryTree": memory_tree},
            observed={"memoryTree": affected_closure.memoryTree},
            next_action="memory_quality_check",
        )
    if affected_closure.acceptanceEligible or not affected_closure.fullFinalRequired:
        raise FinalCertificationError(
            "gate-five-affected-closure-disposition-invalid",
            "the affected-closure plan must stay non-certifying and full-final-required",
            next_action="memory_quality_check",
        )


def _require_pending_full_only_population(
    affected_closure: AffectedClosurePlan,
    catalog: Sequence[FinalCatalogItemIdentity],
) -> None:
    """The final full catalog must cover every R07 pending full-only checker."""

    declared_full_only = {
        policy.checker for policy in checker_scope_registry() if policy.mode == "full-only"
    }
    planned_ids = {item.itemId for item in catalog}
    pending = {item.checker for item in affected_closure.pendingFinalFull}
    if pending != declared_full_only or not pending <= planned_ids:
        raise FinalCertificationError(
            "gate-five-full-only-population-incomplete",
            "the final full catalog must cover every pending full-only checker",
            expected={"pendingFinalFull": sorted(declared_full_only)},
            observed={"pendingFinalFull": sorted(pending)},
        )


def _require_executed_population(
    executed_checks: Mapping[str, Mapping[str, Any]],
    plan: FinalFullCatalogPlan,
) -> None:
    planned = {item.itemId for item in plan.catalog if item.itemId in _STANDARD_CHECK_ID_SET}
    if not planned:
        _refuse("gate-five-executed-population-empty", "the final catalog has no standard checks")
    missing = sorted(planned - set(executed_checks))
    if missing:
        raise FinalCertificationError(
            "gate-five-executed-population-incomplete",
            "the executed final catalog must cover every planned standard check",
            expected={"checks": sorted(planned)},
            observed={"checks": missing},
        )


def _refuse(status: str, detail: str) -> NoReturn:
    raise FinalCertificationError(status, detail)


def _refuse_unknown_item(item_id: str) -> NoReturn:
    raise FinalCertificationError(
        "gate-five-catalog-item-unknown",
        f"the final catalog does not own item {item_id}",
        next_action="memory_quality_check",
    )


__all__ = [
    "AFFECTED_CLOSURE_ITEM_ID",
    "CANDIDATE_PAIR_ITEM_ID",
    "COHERENCE_RECORD_ITEM_ID",
    "FINAL_FULL_CATALOG_VERSION",
    "MISSING_ONBOARDING_ITEM_ID",
    "ROUTE_INDEX_ALIGNMENT_ITEM_ID",
    "ExecutedFinalCatalog",
    "ReadinessProjectionInput",
    "compile_final_catalog_plan",
    "complete_final_catalog",
    "final_catalog_attestation",
    "final_catalog_readiness",
]
