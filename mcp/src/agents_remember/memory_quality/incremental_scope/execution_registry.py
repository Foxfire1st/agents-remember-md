"""Exhaustive R07 execution contracts for R06 incremental checker policies."""

from __future__ import annotations

from agents_remember.memory_quality.style.citations import range_resolution

from .affected_models import CheckerExecutionPolicy
from .models import canonical_digest
from .registry import checker_scope_registry

_EXECUTION_POLICIES = (
    CheckerExecutionPolicy(
        checker=range_resolution.CHECK_NAME,
        validatorVersion="citation-range-resolution/v1",
        runtimeVersion="python-3.13-memory-quality/v1",
        correctiveOwner="memory-curator",
    ),
)


def checker_execution_registry() -> tuple[CheckerExecutionPolicy, ...]:
    """Return one execution contract for every and only incremental R06 policy."""

    policies = tuple(sorted(_EXECUTION_POLICIES, key=lambda item: item.checker))
    declared = {item.checker for item in policies}
    expected = {item.checker for item in checker_scope_registry() if item.mode == "incremental"}
    if declared != expected or len(declared) != len(policies):
        missing = sorted(expected - declared)
        stale = sorted(declared - expected)
        raise ValueError(
            f"incremental checker execution registry is incomplete: missing={missing}, "
            f"stale={stale}"
        )
    return policies


def checker_execution_registry_version() -> str:
    return canonical_digest([item.model_dump(mode="json") for item in checker_execution_registry()])


__all__ = ["checker_execution_registry", "checker_execution_registry_version"]
