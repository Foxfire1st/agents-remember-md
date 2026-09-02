"""Exhaustive checker policies and fixed dependency-owner contracts."""

from __future__ import annotations

from dataclasses import dataclass

from agents_remember.memory_quality.check import AVAILABLE_CHECKS, DRIFT_CHECK_NAME
from agents_remember.memory_quality.style.citations import claim_reopen, range_resolution
from agents_remember.memory_quality.style.document_shape import (
    diff_markers,
    entity_catalog_alignment,
    tables,
)
from agents_remember.memory_quality.style.update_history import history_order

from .models import CheckerScopePolicy, EdgeClass, canonical_digest


@dataclass(frozen=True)
class EdgeOwnerContract:
    """The one existing product owner allowed to emit an edge class."""

    authority_namespace: str
    extractor_version: str
    validator_version: str


EDGE_OWNER_CONTRACTS: dict[EdgeClass, EdgeOwnerContract] = {
    "source-to-file-sidecar": EdgeOwnerContract(
        "agents-remember.sidecar-pairing",
        "sidecar-pairing/v1",
        "sidecar-path-confinement/v1",
    ),
    "source-to-governing-route": EdgeOwnerContract(
        "agents-remember.route-index",
        "route-index-governing-chain/v1",
        "route-index/v1",
    ),
    "source-to-citing-memory-document": EdgeOwnerContract(
        "agents-remember.citation-model",
        "citation-source-reference/v1",
        "citation-source-index/v8",
    ),
    "source-to-entity-manifestation": EdgeOwnerContract(
        "agents-remember.entity-catalog",
        "entity-fingerprint-evidence-path/v1",
        "entity-catalog-fingerprint/v1",
    ),
    "route-index-dependency": EdgeOwnerContract(
        "agents-remember.route-index",
        "route-index-input/v1",
        "route-index/v1",
    ),
}

_ALL_EDGE_CLASSES = tuple(EDGE_OWNER_CONTRACTS)

_POLICIES = (
    CheckerScopePolicy(
        checker=DRIFT_CHECK_NAME,
        mode="full-only",
        reason=(
            "the drift summary owns whole onboarding, inline, entity, and Git-history observation; "
            "no complete selected runner exists"
        ),
    ),
    CheckerScopePolicy(
        checker=claim_reopen.CHECK_NAME,
        mode="full-only",
        reason=(
            "claim reopening observes tree-wide provenance and dependency changes; no complete "
            "selected runner exists"
        ),
    ),
    CheckerScopePolicy(
        checker=diff_markers.CHECK_NAME,
        mode="full-only",
        reason="the document-shape checker exposes only a whole-onboarding runner",
    ),
    CheckerScopePolicy(
        checker=entity_catalog_alignment.CHECK_NAME,
        mode="full-only",
        reason="the checker validates the single repository-wide entity catalog as a whole",
    ),
    CheckerScopePolicy(
        checker=history_order.CHECK_NAME,
        mode="full-only",
        reason="the history checker exposes only a whole-onboarding runner",
    ),
    CheckerScopePolicy(
        checker=range_resolution.CHECK_NAME,
        mode="incremental",
        extractorVersion="citation-range-selected-documents/v1",
        edgeClasses=_ALL_EDGE_CLASSES,
        reason=(
            "the checker accepts one confined document with one exact frozen citation-source "
            "index generation"
        ),
    ),
    CheckerScopePolicy(
        checker=tables.CHECK_NAME,
        mode="full-only",
        reason="the table-shape checker exposes only a whole-onboarding runner",
    ),
)


def checker_scope_registry() -> tuple[CheckerScopePolicy, ...]:
    """Return the complete deterministic policy population or refuse source drift."""

    policies = tuple(sorted(_POLICIES, key=lambda item: item.checker))
    declared = {policy.checker for policy in policies}
    current = set(AVAILABLE_CHECKS)
    if declared != current:
        missing = sorted(current - declared)
        stale = sorted(declared - current)
        raise ValueError(
            f"memory checker scope registry is incomplete: missing={missing}, stale={stale}"
        )
    for policy in policies:
        if policy.mode == "incremental" and (
            policy.extractorVersion is None or not policy.edgeClasses
        ):
            raise ValueError(f"incremental checker {policy.checker} lacks an executable policy")
        if policy.mode == "full-only" and (
            policy.extractorVersion is not None or policy.edgeClasses
        ):
            raise ValueError(f"full-only checker {policy.checker} claims incremental semantics")
    return policies


def checker_registry_version() -> str:
    policies = checker_scope_registry()
    return canonical_digest(
        [policy.model_dump(mode="json", exclude_none=False) for policy in policies]
    )


__all__ = [
    "EDGE_OWNER_CONTRACTS",
    "EdgeOwnerContract",
    "checker_registry_version",
    "checker_scope_registry",
]
