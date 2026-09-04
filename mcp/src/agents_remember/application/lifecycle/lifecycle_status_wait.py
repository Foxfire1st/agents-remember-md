"""Application controller for the read-only lifecycle status-change wait (CCR-R15).

The MCP worktree_status_wait tool is addressed by canonical contract,
operation kind, expected public generation, and an opaque typed
afterRevision obtained from a prior worktree_status snapshot.  The
controller resolves the exact task-owned journal location, runs the bounded
read-only wait loop, projects the R18 coherent envelope from the exact durable
record the wait compared, and translates every typed outcome into the public
response.  It never writes the journal and never acquires lifecycle, queue,
gate, or worker authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agents_remember.application.lifecycle.lifecycle_operation_location import (
    configured_lifecycle_operation_location,
    location_decision_payload,
)
from agents_remember.errors import AuthorityError
from agents_remember.kernel.authority import require_within_coordination
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.models.lifecycles.operation import LifecycleOperationKind
from agents_remember.models.lifecycles.operation_wait import (
    OUTCOME_CHANGED,
    OUTCOME_JOURNAL_REPLACED,
    OUTCOME_JOURNAL_UNREADABLE,
    OUTCOME_NO_OPERATION,
    OUTCOME_PROJECTION_INCOHERENT,
    OUTCOME_SUCCESSOR,
    OUTCOME_UNCHANGED,
    OUTCOME_WRONG_CURSOR,
    OUTCOME_WRONG_GENERATION,
    LifecycleWaitOutcome,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleOperationLocationError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_read_decision import (
    lifecycle_journal_read_decision,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.lifecycle.observation.projection import (
    observed_operation_projection,
)
from agents_remember.worktrees.integration.lifecycle.observation.status_wait import (
    LifecycleWaitDecision,
    wait_for_lifecycle_change,
)
from agents_remember.worktrees.worktree_contract import ContractError, load_contract

OPERATION = "worktree_status_wait"

# A wait refusal always names the exact next read-only snapshot action: the
# caller re-observes the same task address with worktree_status (CCR-R15 never
# recommends a mutating action from a wait refusal).
_NEXT_SNAPSHOT_TOOL = "worktree_status"


class LifecycleStatusWaitRequest(BaseModel):
    """One typed read-only wait request addressed by the public lifecycle surface."""

    model_config = ConfigDict(extra="forbid")

    contract_path: str = Field(min_length=1, max_length=4096)
    operation_kind: LifecycleOperationKind
    expected_generation: int = Field(ge=1)
    # 0 is admitted so a caller without a prior snapshot receives the typed
    # wrong-cursor refusal instead of a schema error; positive cursors are the
    # only meaningful wait baselines (CCR-R15).
    after_revision: int = Field(ge=0)
    timeout_seconds: float = Field(default=30.0, ge=0.0)


def worktree_status_wait_tool(
    config: McpRuntimeConfig,
    request: LifecycleStatusWaitRequest,
) -> dict[str, Any]:
    """Run one bounded read-only wait and return its typed public outcome."""
    try:
        confined = require_within_coordination(config, request.contract_path, "contract_path")
    except AuthorityError as error:
        return _address_refusal(
            "the canonical worktree contract path is not confined to coordination_root",
            str(error),
        )
    try:
        _, location = configured_lifecycle_operation_location(config, confined)
    except LifecycleOperationLocationError as error:
        return {"operation": OPERATION, **location_decision_payload(error)}
    store = LifecycleOperationStore(location.journal_path(request.operation_kind))
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=request.expected_generation,
        after_revision=request.after_revision,
        timeout_seconds=request.timeout_seconds,
    )
    return _decision_payload(
        confined,
        operation_kind=request.operation_kind,
        decision=decision,
        timeout_seconds=request.timeout_seconds,
    )


def _decision_payload(
    contract_path: Path,
    *,
    operation_kind: LifecycleOperationKind,
    decision: LifecycleWaitDecision,
    timeout_seconds: float,
) -> dict[str, Any]:
    if decision.outcome in {OUTCOME_CHANGED, OUTCOME_UNCHANGED, OUTCOME_SUCCESSOR}:
        return _coherent_wait_payload(
            contract_path,
            operation_kind=operation_kind,
            decision=decision,
            timeout_seconds=timeout_seconds,
        )
    return _refusal_wait_payload(
        contract_path,
        operation_kind=operation_kind,
        decision=decision,
        timeout_seconds=timeout_seconds,
    )


def _coherent_wait_payload(
    contract_path: Path,
    *,
    operation_kind: LifecycleOperationKind,
    decision: LifecycleWaitDecision,
    timeout_seconds: float,
) -> dict[str, Any]:
    record = decision.record
    if record is None:
        raise RuntimeError("a coherent wait decision must carry its durable record")
    projection = observed_operation_projection(
        record,
        contract=_loaded_contract(contract_path),
    )
    if projection is not None and projection.status == "incoherent":
        # CCR-R18: an incoherent projection returns a typed read-only refusal and
        # no recommended mutating action; it is never returned as a valid snapshot.
        return _projection_incoherent_payload(
            contract_path,
            operation_kind=operation_kind,
            decision=decision,
            projection=projection,
            timeout_seconds=timeout_seconds,
        )
    payload: dict[str, Any] = {
        "ok": True,
        "operation": OPERATION,
        "state": decision.outcome,
        "status": decision.outcome,
        "outcome": decision.outcome,
        "contractPath": contract_path.as_posix(),
        "operationKind": operation_kind,
        "generation": record.generation,
        "meaningfulRevision": record.meaningfulRevision,
        "timeoutSeconds": timeout_seconds,
        "elapsedSeconds": round(decision.elapsedSeconds, 3),
    }
    if decision.outcome == OUTCOME_SUCCESSOR:
        payload["successorGeneration"] = decision.successorGeneration
    if projection is not None:
        payload["lifecycleOperation"] = projection.model_dump(mode="json", exclude_none=True)
    return payload


def _loaded_contract(contract_path: Path):
    """Load the canonical contract when readable; the envelope stays coherent without it."""
    try:
        return load_contract(contract_path)
    except (ContractError, OSError, UnicodeError, ValueError):
        return None


def _refusal_wait_payload(
    contract_path: Path,
    *,
    operation_kind: LifecycleOperationKind,
    decision: LifecycleWaitDecision,
    timeout_seconds: float,
) -> dict[str, Any]:
    record = decision.record
    detail = decision.detail or _refusal_detail(decision.outcome)
    payload: dict[str, Any] = {
        "ok": False,
        "operation": OPERATION,
        "state": "refused",
        "status": decision.outcome,
        "outcome": decision.outcome,
        "detail": detail,
        "contractPath": contract_path.as_posix(),
        "operationKind": operation_kind,
        "timeoutSeconds": timeout_seconds,
        "elapsedSeconds": round(decision.elapsedSeconds, 3),
        "nextAction": "snapshot",
        "nextTool": _NEXT_SNAPSHOT_TOOL,
        "nextArgs": {"contract_path": contract_path.as_posix()},
    }
    if decision.outcome == OUTCOME_JOURNAL_UNREADABLE and decision.readError is not None:
        read_decision = lifecycle_journal_read_decision(operation_kind, decision.readError)
        payload["status"] = read_decision.status
        payload["expected"] = read_decision.expected
        payload["observed"] = read_decision.observed
        payload["summary"] = read_decision.detail
    else:
        payload["expected"] = _refusal_expected(decision)
        payload["observed"] = _refusal_observed(decision)
    if record is not None:
        payload["generation"] = record.generation
        payload["meaningfulRevision"] = record.meaningfulRevision
    return payload


def _projection_incoherent_payload(
    contract_path: Path,
    *,
    operation_kind: LifecycleOperationKind,
    decision: LifecycleWaitDecision,
    projection: Any,
    timeout_seconds: float,
) -> dict[str, Any]:
    record = decision.record
    assert record is not None
    return {
        "ok": False,
        "operation": OPERATION,
        "state": "refused",
        "status": OUTCOME_PROJECTION_INCOHERENT,
        "outcome": OUTCOME_PROJECTION_INCOHERENT,
        "summary": (
            "the lifecycle journal advanced but its public projection is incoherent; "
            "reread the exact generation with worktree_status"
        ),
        "detail": (
            "the lifecycle observation does not bind one coherent journal revision; "
            "no mutating action is recommended"
        ),
        "contractPath": contract_path.as_posix(),
        "operationKind": operation_kind,
        "generation": record.generation,
        "meaningfulRevision": record.meaningfulRevision,
        "timeoutSeconds": timeout_seconds,
        "elapsedSeconds": round(decision.elapsedSeconds, 3),
        "nextAction": "snapshot",
        "nextTool": _NEXT_SNAPSHOT_TOOL,
        "nextArgs": {"contract_path": contract_path.as_posix()},
        "lifecycleOperation": projection.model_dump(mode="json", exclude_none=True),
    }


def _refusal_detail(outcome: LifecycleWaitOutcome) -> str:
    return {
        OUTCOME_NO_OPERATION: (
            "no lifecycle operation journal exists for this contract and operation kind"
        ),
        OUTCOME_WRONG_GENERATION: (
            "the expected public generation does not match the current journal generation"
        ),
        OUTCOME_WRONG_CURSOR: (
            "the wait cursor must be a positive meaningful revision from a prior snapshot"
        ),
        OUTCOME_JOURNAL_REPLACED: (
            "the journal was replaced outside the store and is behind the waited cursor"
        ),
        OUTCOME_JOURNAL_UNREADABLE: (
            "the canonical strict lifecycle journal is unreadable or invalid"
        ),
        OUTCOME_PROJECTION_INCOHERENT: (
            "the lifecycle journal advanced but its public projection is incoherent"
        ),
    }.get(outcome, "the read-only lifecycle wait could not return a snapshot")


def _address_refusal(detail: str, observed: str) -> dict[str, Any]:
    return {
        "ok": False,
        "operation": OPERATION,
        "state": "refused",
        "status": "wrong-contract",
        "outcome": "wrong-contract",
        "detail": detail,
        "expected": {"contractPath": "inside coordination_root"},
        "observed": {"reason": observed},
        "nextAction": "snapshot",
        "nextTool": _NEXT_SNAPSHOT_TOOL,
    }


def _refusal_expected(decision: LifecycleWaitDecision) -> dict[str, Any]:
    expected: dict[str, Any] = {
        "operationKind": "same-kind strict journal",
        "generation": "waited generation",
        "meaningfulRevision": ">= waited cursor",
    }
    if decision.outcome == OUTCOME_JOURNAL_REPLACED:
        expected["meaningfulRevision"] = ">= waited cursor on the same generation"
    if decision.outcome == OUTCOME_SUCCESSOR:
        expected["generation"] = "waited generation + 1 with archived successor proof"
    return expected


def _refusal_observed(decision: LifecycleWaitDecision) -> dict[str, Any]:
    record = decision.record
    observed: dict[str, Any] = {"detail": decision.detail or ""}
    if record is not None:
        observed["generation"] = record.generation
        observed["meaningfulRevision"] = record.meaningfulRevision
    return observed
