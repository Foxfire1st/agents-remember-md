"""One cross-operation lease for every task enclosure lifecycle."""

from __future__ import annotations

import fcntl
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agents_remember.models.lifecycles.operation import LifecycleOperationKind
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleOperationLocation,
    lifecycle_operation_locator_path,
    located_lifecycle_operation_store,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract

_ACTIVE = frozenset({"queued", "running", "input-required", "termination-required"})
_TERMINAL = frozenset({"completed", "failed", "cancelled"})


class LifecycleOperationCompatibilityError(RuntimeError):
    """A terminal mutation observed another operation that still owns authority."""

    def __init__(
        self,
        *,
        operation: str,
        blockers: list[LifecycleOperationKind],
    ) -> None:
        self.operation = operation
        self.blockers = tuple(blockers)
        super().__init__(
            f"{operation} cannot proceed while task lifecycle operation(s) are active: "
            f"{', '.join(blockers)}"
        )


def _lease_path(
    contract: WorktreeContract,
    location: LifecycleOperationLocation | None,
) -> Path:
    """Return a transient lock outside the enclosure that terminal cleanup may delete."""

    identity = contract.contract_path.resolve(strict=False).as_posix()
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    locator = lifecycle_operation_locator_path(
        contract.coordination_root,
        contract.contract_path,
    )
    if location is not None and location.locator_path != locator:
        raise RuntimeError("lifecycle lease location disagrees with the stable locator address")
    return locator.parent / "locks" / f"{digest}.lock"


def _active_operation_kinds(
    contract: WorktreeContract,
    *,
    exclude: LifecycleOperationKind | None = None,
    publish_worker_exits: bool,
) -> list[LifecycleOperationKind]:
    from agents_remember.worktrees.integration.lifecycle.worker.state import (  # noqa: PLC0415
        project_worker_exit,
        reconcile_worker_exit,
    )

    active: list[LifecycleOperationKind] = []
    for kind in ("closeout", "integrate", "direct-landing"):
        if kind == exclude:
            continue
        store = located_lifecycle_operation_store(contract, kind)
        record = reconcile_worker_exit(store) if publish_worker_exits else store.read()
        if record is not None and not publish_worker_exits:
            record = project_worker_exit(record)
        if record is not None and (
            record.status in _ACTIVE
            or record.workerPid is not None
            or (record.workerTermination is not None and record.workerTermination.state != "exited")
        ):
            active.append(kind)
    return active


@contextmanager
def contract_lifecycle_lease(
    contract: WorktreeContract,
    *,
    location: LifecycleOperationLocation | None = None,
) -> Iterator[None]:
    """Acquire only the cross-kind filesystem serialization lease for one contract."""

    path = _lease_path(contract, location)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def require_lifecycle_operation_compatible(
    contract: WorktreeContract,
    *,
    operation_kind: LifecycleOperationKind | None,
    publish_worker_exits: bool = True,
) -> None:
    """Decide active-operation compatibility while the caller holds the lease.

    Separating this durable-state decision from lock acquisition lets closeout
    validate untrusted input before any lifecycle record is observed. Integrate,
    cleanup, abandon, and cancellation call the same decision owner immediately
    after acquiring serialization.
    """

    active = _active_operation_kinds(
        contract,
        publish_worker_exits=publish_worker_exits,
    )
    blockers: list[LifecycleOperationKind] = [kind for kind in active if kind != operation_kind]
    if blockers:
        label = "terminal mutation" if operation_kind is None else operation_kind
        raise LifecycleOperationCompatibilityError(
            operation=label,
            blockers=blockers,
        )


def require_legacy_operation_compatible(
    contract: WorktreeContract,
    *,
    target_kind: LifecycleOperationKind,
    publish_worker_exits: bool = True,
) -> None:
    """Reject cross-kind authority without asking the normal reader to parse the target."""
    blockers = _active_operation_kinds(
        contract,
        exclude=target_kind,
        publish_worker_exits=publish_worker_exits,
    )
    if blockers:
        raise RuntimeError(
            "legacy lifecycle repair cannot proceed while another task operation is active: "
            + ", ".join(blockers)
        )
