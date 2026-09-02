"""Application boundary for the direct landing operation."""

from __future__ import annotations

from pathlib import Path

from agents_remember.kernel.authority import require_within_coordination
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.worktrees.closeout_input import CloseoutInputError
from agents_remember.worktrees.direct_landing import (
    DirectLandingError,
    DirectLandingRequest,
    direct_landing,
    require_direct_landing_enabled,
)
from agents_remember.worktrees.integration.direct_landing.direct_landing_operation import (
    direct_landing_store,
)
from agents_remember.worktrees.integration.direct_landing.direct_landing_recovery_state import (
    classify_direct_landing_recovery,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_projection import (
    legal_operation_controls,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleOperationLocation,
    LifecycleOperationLocationError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_read_decision import (
    lifecycle_journal_read_decision,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationReadError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)
from agents_remember.worktrees.integration.lifecycle.observation.projection import (
    unreadable_contract_operation_projections,
)
from agents_remember.worktrees.worktree_contract import ContractError, load_contract

from .configured_contract_admission import (
    ConfiguredContractRefused,
    admit_configured_contract,
    execute_configured_contract_operation,
    project_configured_contract_refusal,
)
from .lifecycle_operation_location import (
    LifecycleOperationPublicAddress,
    configured_lifecycle_operation_location,
)


def direct_landing_tool(
    config: McpRuntimeConfig,
    request: DirectLandingRequest,
) -> dict[str, object]:
    """Run one policy-gated, branch-addressed direct landing."""
    try:
        require_direct_landing_enabled(config)
    except DirectLandingError as exc:
        return _direct_error_payload(exc, recovery={})
    configured = admit_configured_contract(config, request.contract_path)
    if isinstance(configured, ConfiguredContractRefused):
        return _direct_landing_refusal(
            project_configured_contract_refusal(
                configured,
                operation="direct_landing",
                address=LifecycleOperationPublicAddress("direct_landing", "direct-landing"),
            )
        )
    try:
        execution = execute_configured_contract_operation(
            configured,
            lambda: direct_landing(config, request, configured.contract),
        )
    except CloseoutInputError as exc:
        return {
            "ok": False,
            "operation": "direct_landing",
            "state": "refused",
            "status": exc.status,
            "detail": "direct landing input is invalid; use the corrected call",
            **exc.response_fields(),
        }
    except LifecycleOperationReadError as exc:
        return _journal_read_refusal(exc)
    except DirectLandingError as exc:
        return _direct_error_payload(
            exc,
            recovery=_direct_recovery_action(config, request),
        )
    return (
        _direct_landing_refusal(
            project_configured_contract_refusal(
                execution,
                operation="direct_landing",
                address=LifecycleOperationPublicAddress("direct_landing", "direct-landing"),
            )
        )
        if isinstance(execution, ConfiguredContractRefused)
        else execution
    )


def _direct_landing_refusal(payload: dict[str, object]) -> dict[str, object]:
    """Keep generic refusal detail outside the closed landing-outcome field."""

    result = dict(payload)
    projected_state = result.get("state")
    if projected_state != "refused":
        if not isinstance(result.get("status"), str) and isinstance(projected_state, str):
            result["status"] = projected_state
        result["state"] = "refused"
    return result


def _direct_error_payload(
    error: DirectLandingError,
    *,
    recovery: dict[str, object],
) -> dict[str, object]:
    recovery_fields = {
        key: value
        for key, value in recovery.items()
        if key not in {"ok", "operation", "state", "status", "detail"}
    }
    task_intent_recovery = (
        {"nextAction": error.next_action} if error.next_action is not None else {}
    )
    return {
        "ok": False,
        "operation": "direct_landing",
        "state": "refused",
        "status": error.status,
        "detail": error.detail,
        "expected": error.expected,
        "observed": error.observed,
        **recovery_fields,
        **task_intent_recovery,
    }


def _direct_recovery_action(
    config: McpRuntimeConfig,
    request: DirectLandingRequest,
) -> dict[str, object]:
    path = require_within_coordination(config, request.contract_path, "contract_path")
    try:
        contract = load_contract(path)
    except (ContractError, OSError, UnicodeError, ValueError) as exc:
        return _unreadable_direct_decision(config, path, exc)
    try:
        record = direct_landing_store(contract).read()
    except LifecycleOperationReadError as error:
        decision = lifecycle_journal_read_decision("direct-landing", error)
        return {
            **{key: value for key, value in decision.payload().items() if key != "state"},
            "lifecycleOperation": decision.projection().model_dump(mode="json", exclude_none=True),
        }
    if record is None:
        return {}
    controls = legal_operation_controls(contract, record)
    if not controls:
        classification = classify_direct_landing_recovery(contract, record)
        if classification.state == "developer-decision":
            return {
                "expected": dict(classification.expected or {}),
                "observed": dict(classification.observed or {}),
                "nextAction": "developer-decision",
                "developerDecisionRequired": True,
                "decisionSurface": classification.detail,
            }
        return {
            "nextAction": "developer-decision",
            "developerDecisionRequired": True,
            "decisionSurface": "the direct-landing generation is not mechanically recoverable",
        }
    next_row = controls[0]
    return {
        "nextAction": next_row["action"],
        "nextTool": next_row["tool"],
        "nextArgs": next_row["arguments"],
    }


def _unreadable_direct_decision(
    config: McpRuntimeConfig,
    contract_path: Path,
    error: Exception,
) -> dict[str, object]:
    """Use the exact locator and strict current journal without parsing the contract."""

    location, decision = _direct_operation_location(config, contract_path)
    if decision is not None:
        return decision
    assert location is not None
    projected = _unreadable_direct_projection(location, contract_path, error)
    if projected is not None:
        return projected
    return _missing_direct_generation_decision(location, contract_path, error)


def _direct_operation_location(
    config: McpRuntimeConfig,
    contract_path: Path,
) -> tuple[LifecycleOperationLocation | None, dict[str, object] | None]:
    try:
        _, location = configured_lifecycle_operation_location(config, contract_path)
    except LifecycleOperationLocationError as exc:
        return None, {
            "expected": exc.expected,
            "observed": exc.observed,
            "nextAction": "developer-decision",
            "developerDecisionRequired": True,
            "decisionSurface": exc.detail,
        }
    return location, None


def _unreadable_direct_projection(
    location: LifecycleOperationLocation,
    contract_path: Path,
    error: Exception,
) -> dict[str, object] | None:
    projection = _first_unreadable_direct_projection(location, contract_path, error)
    if projection is None:
        return None
    if isinstance(projection.result, dict):
        return dict(projection.result)
    return None


def _first_unreadable_direct_projection(
    location: LifecycleOperationLocation,
    contract_path: Path,
    error: Exception,
):
    projections = unreadable_contract_operation_projections(
        location,
        error_type=type(error).__name__,
        name=contract_path.name,
    )
    return next((row for row in projections if row.kind == "direct-landing"), None)


def _missing_direct_generation_decision(
    location: LifecycleOperationLocation,
    contract_path: Path,
    error: Exception,
) -> dict[str, object]:
    detail = "the unreadable contract has no exact retained direct-landing generation"
    return {
        "expected": {
            "contractPath": location.contract_path.as_posix(),
            "operationKind": "direct-landing",
        },
        "observed": public_failure_evidence(
            stage="contract-read",
            side="contract",
            name=contract_path.name,
            error_type=type(error).__name__,
            observed={"state": "unreadable"},
        ),
        "nextAction": "developer-decision",
        "developerDecisionRequired": True,
        "decisionSurface": detail,
    }


def _journal_read_refusal(error: LifecycleOperationReadError) -> dict[str, object]:
    decision = lifecycle_journal_read_decision("direct-landing", error)
    payload = decision.payload()
    return {
        "ok": False,
        "operation": "direct_landing",
        "state": "refused",
        "status": decision.status,
        "detail": decision.detail,
        **{key: value for key, value in payload.items() if key != "state"},
    }


__all__ = [
    "DirectLandingError",
    "DirectLandingRequest",
    "direct_landing_tool",
]
