"""Canonical same-generation transitions for the readiness vocabulary."""

from __future__ import annotations

from typing import Never

from agents_remember.certification.models import CertificationContractFinding
from agents_remember.certification.readiness_models import (
    ReadinessTransitionDomain,
    ReadinessTransitionRule,
)
from agents_remember.errors import CloseoutReadinessContractError

CANONICAL_READINESS_TRANSITIONS: tuple[ReadinessTransitionRule, ...] = (
    ReadinessTransitionRule(
        domain="lifecycle",
        before="admission-pending",
        after=("admission-refused", "admitted"),
    ),
    ReadinessTransitionRule(
        domain="lifecycle",
        before="admission-refused",
        after=("admission-pending", "admitted"),
    ),
    ReadinessTransitionRule(
        domain="lifecycle",
        before="admitted",
        after=("finalization-pending",),
    ),
    ReadinessTransitionRule(
        domain="lifecycle",
        before="finalization-pending",
        after=("finalization-running", "finalization-refused"),
    ),
    ReadinessTransitionRule(
        domain="lifecycle",
        before="finalization-running",
        after=("finalization-refused", "finalized"),
    ),
    ReadinessTransitionRule(
        domain="lifecycle",
        before="finalization-refused",
        after=("finalization-pending", "finalization-running"),
    ),
    ReadinessTransitionRule(domain="lifecycle", before="finalized", after=()),
    ReadinessTransitionRule(
        domain="gate",
        before="not-started",
        after=("blocked", "running", "invalidated"),
    ),
    ReadinessTransitionRule(
        domain="gate",
        before="blocked",
        after=("not-started", "running", "invalidated"),
    ),
    ReadinessTransitionRule(
        domain="gate",
        before="running",
        after=("passed", "failed", "invalidated"),
    ),
    ReadinessTransitionRule(domain="gate", before="passed", after=("invalidated",)),
    ReadinessTransitionRule(domain="gate", before="failed", after=("invalidated",)),
    ReadinessTransitionRule(
        domain="gate",
        before="invalidated",
        after=("not-started", "running"),
    ),
    ReadinessTransitionRule(
        domain="certificate",
        before="absent",
        after=("current-green", "invalidated", "unavailable"),
    ),
    ReadinessTransitionRule(
        domain="certificate",
        before="current-green",
        after=("stale", "invalidated", "unavailable"),
    ),
    ReadinessTransitionRule(
        domain="certificate",
        before="stale",
        after=("absent", "current-green", "invalidated", "unavailable"),
    ),
    ReadinessTransitionRule(
        domain="certificate",
        before="invalidated",
        after=("absent", "current-green", "unavailable"),
    ),
    ReadinessTransitionRule(
        domain="certificate",
        before="unavailable",
        after=("absent", "current-green", "invalidated"),
    ),
    ReadinessTransitionRule(
        domain="profile",
        before="unresolved",
        after=("invalid", "admitted-current"),
    ),
    ReadinessTransitionRule(
        domain="profile",
        before="invalid",
        after=("unresolved", "admitted-current"),
    ),
    ReadinessTransitionRule(
        domain="profile",
        before="admitted-current",
        after=("changed", "invalid"),
    ),
    ReadinessTransitionRule(
        domain="profile",
        before="changed",
        after=("unresolved", "invalid", "admitted-current"),
    ),
)


def require_readiness_transition(
    domain: ReadinessTransitionDomain,
    before: str,
    after: str,
) -> None:
    """Fail closed when one same-generation transition is outside the canonical table."""

    rule = next(
        (
            item
            for item in CANONICAL_READINESS_TRANSITIONS
            if item.domain == domain and item.before == before
        ),
        None,
    )
    if rule is None or after not in rule.after:
        _raise(
            "readiness-transition-invalid",
            f"transitions.{domain}.{before}",
            f"{before!r} cannot transition to {after!r} within one generation",
        )


def _raise(code: str, path: str, detail: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=detail)
    raise CloseoutReadinessContractError(
        "closeout readiness transition failed",
        (finding.model_dump(mode="json"),),
    )


__all__ = ["CANONICAL_READINESS_TRANSITIONS", "require_readiness_transition"]
