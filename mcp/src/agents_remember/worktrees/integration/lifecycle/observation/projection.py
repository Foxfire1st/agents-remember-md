"""Read-only projection of durable lifecycle operation journals."""

from __future__ import annotations

from pathlib import Path

from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationKind,
    LifecycleOperationProjection,
    LifecycleOperationRecord,
)
from agents_remember.worktrees.integration.closeout.door import (
    DoorContractReadFailure,
    classify_door_publication,
)
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    derive_closeout_recovery_commits,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleOperationLocation,
    LifecycleOperationLocationError,
    located_lifecycle_operation_store,
    require_contract_matches_lifecycle_operation_location,
    resolve_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    OperationProjectionContext,
    bind_projection_decision,
    operation_projection,
    operation_projection_identity,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_read_decision import (
    lifecycle_journal_read_decision,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationReadError,
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)
from agents_remember.worktrees.integration.mutation_evidence import reconcile_closeout_mutations
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract


def observe_operation(
    contract_path: Path, kind: LifecycleOperationKind
) -> LifecycleOperationProjection | None:
    contract = load_contract(contract_path)
    try:
        store = located_lifecycle_operation_store(contract, kind)
        current = _project_observed_record(store.read())
    except LifecycleOperationReadError as error:
        return lifecycle_journal_read_decision(kind, error).projection()
    return None if current is None else operation_projection(current, contract=contract)


def latest_operation_projection(contract_path: Path) -> LifecycleOperationProjection | None:
    contract = load_contract(contract_path)
    records: list[LifecycleOperationRecord] = []
    for kind in ("closeout", "integrate", "direct-landing"):
        try:
            store = located_lifecycle_operation_store(contract, kind)
            record = _project_observed_record(store.read())
        except LifecycleOperationLocationError:
            # Structural enclosure projection must remain total across pre-locator
            # contracts and damaged locator state. The task-addressed operation
            # surfaces still return the exact adoption/repair refusal; this aggregate
            # reader can only state that no operation is safely projectable.
            return None
        except LifecycleOperationReadError as error:
            return lifecycle_journal_read_decision(kind, error).projection()
        if record is not None:
            records.append(record)
    if not records:
        return None
    active = [
        record
        for record in records
        if record.status in {"queued", "running", "input-required", "termination-required"}
    ]
    selected = max(active or records, key=_record_sort_stamp)
    return operation_projection(selected, contract=contract)


def current_operation_projections(
    contract_path: Path,
    *,
    allow_completed_disposition: bool = False,
    caller: DeclaredCaller | None = None,
    contract: WorktreeContract | None = None,
    location: LifecycleOperationLocation | None = None,
) -> list[LifecycleOperationProjection]:
    """Return every current operation kind; task status must not hide actionable siblings."""

    contract = contract or load_contract(contract_path)
    try:
        location = location or resolve_lifecycle_operation_location(
            contract.coordination_root,
            contract.contract_path,
        )
    except LifecycleOperationLocationError:
        return []
    records: list[LifecycleOperationRecord] = []
    decisions: list[LifecycleOperationProjection] = []
    for kind in ("closeout", "integrate", "direct-landing"):
        try:
            record = _project_observed_record(
                LifecycleOperationStore(location.journal_path(kind)).read()
            )
        except LifecycleOperationReadError as error:
            decisions.append(lifecycle_journal_read_decision(kind, error).projection())
            continue
        if record is not None:
            records.append(record)
    try:
        require_contract_matches_lifecycle_operation_location(contract, location)
    except LifecycleOperationLocationError as exc:
        return [
            *[_operation_location_decision(record, exc) for record in records],
            *decisions,
        ]
    projected = [
        operation_projection(
            record,
            contract=contract,
            context=OperationProjectionContext(
                allow_completed_disposition=allow_completed_disposition,
                caller=caller,
            ),
        )
        for record in sorted(records, key=lambda item: item.operationKind)
    ]
    return sorted([*projected, *decisions], key=lambda item: item.kind)


def _operation_location_decision(
    record: LifecycleOperationRecord,
    error: LifecycleOperationLocationError,
) -> LifecycleOperationProjection:
    result = {
        "state": error.status,
        "developerDecisionRequired": True,
        "decisionSurface": error.detail,
        "nextAction": "developer-decision",
        "expected": error.expected,
        "observed": error.observed,
    }
    return bind_projection_decision(
        operation_projection(record),
        result,
        error.detail,
    )


def unreadable_contract_operation_projections(
    location: LifecycleOperationLocation,
    *,
    error_type: str,
    name: str,
) -> list[LifecycleOperationProjection]:
    """Project every retained exact-path generation while contract authority is unreadable."""

    projections: list[LifecycleOperationProjection] = []
    contract_path = location.contract_path.resolve(strict=False)
    for kind in ("closeout", "integrate", "direct-landing"):
        store = LifecycleOperationStore(location.journal_path(kind))
        try:
            # Contract-invalid status cannot safely run Git reconciliation: that
            # reconciliation deliberately revalidates repository authority by
            # reading the contract. Retain only the read-only worker-exit
            # projection here, then expose zero controls below.
            record = _project_worker_observed_record(store.read())
        except LifecycleOperationReadError as error:
            projections.append(lifecycle_journal_read_decision(kind, error).projection())
            continue
        if (
            record is None
            or record.operationKind != kind
            or Path(record.contractPath).resolve(strict=False) != contract_path
        ):
            continue
        publication = record.doorPublication
        if kind == "closeout" and publication is not None and publication.state == "intent":
            observation = classify_door_publication(
                publication,
                DoorContractReadFailure(error_type, ""),
            )
            projections.append(
                operation_projection(
                    record,
                    context=OperationProjectionContext(
                        door=observation,
                        doorIdentity=operation_projection_identity(record),
                    ),
                )
            )
            continue
        surface = "the canonical task contract is unreadable for this retained operation"
        result = {
            "state": f"{kind}-contract-invalid",
            "developerDecisionRequired": True,
            "decisionSurface": surface,
            "nextAction": "developer-decision",
            "expected": {
                "contractPath": contract_path.as_posix(),
                "worktreeGroup": location.worktree_group.as_posix(),
                "operationKind": kind,
                "generation": record.generation,
            },
            "observed": public_failure_evidence(
                stage="contract-read",
                side="contract",
                name=name,
                error_type=error_type,
                observed={"state": "unreadable"},
            ),
        }
        projections.append(bind_projection_decision(operation_projection(record), result, surface))
    return projections


def _project_observed_record(
    record: LifecycleOperationRecord | None,
) -> LifecycleOperationRecord | None:
    projected = _project_worker_observed_record(record)
    if projected is None:
        return None
    if projected.operationKind not in {"closeout", "direct-landing"}:
        return projected
    reconciled = reconcile_closeout_mutations(projected, temporary_indices=True)
    recovery_commits = derive_closeout_recovery_commits(projected, mutations=reconciled)
    if reconciled == projected.mutationEvidence and recovery_commits == projected.recoveryCommits:
        return projected
    return projected.model_copy(
        update={
            "mutationEvidence": reconciled,
            "recoveryCommits": recovery_commits,
            "irreversibleBoundaryEntered": (
                projected.irreversibleBoundaryEntered
                or any(item.state == "commit-proven" for item in reconciled.values())
            ),
        }
    )


def _project_worker_observed_record(
    record: LifecycleOperationRecord | None,
) -> LifecycleOperationRecord | None:
    if record is None:
        return None
    from agents_remember.worktrees.integration.lifecycle.worker.state import (  # noqa: PLC0415
        project_worker_exit,
    )

    return project_worker_exit(record)


def observed_operation_projection(
    record: LifecycleOperationRecord,
    *,
    contract: WorktreeContract | None = None,
    context: OperationProjectionContext | None = None,
) -> LifecycleOperationProjection | None:
    """Project one exact durable journal read through the shared status pipeline.

    CCR-R18/R15: a status-change wait snapshot must be the same coherent envelope a
    task status read returns for the exact record whose durable meaningful revision
    the waiter compared, so the returned cursor and envelope never splice facts from
    different journal revisions. _project_observed_record performs only read-only
    reconciliation and never writes the journal.
    """

    observed = _project_observed_record(record)
    if observed is None:
        return None
    return operation_projection(observed, contract=contract, context=context)


def _record_sort_stamp(record: LifecycleOperationRecord) -> str:
    return record.finishedAt or record.heartbeatAt or record.startedAt or record.queuedAt
