"""Take the exact journal CAS observation after immutable evidence readback."""

from __future__ import annotations

from agents_remember.errors import CertificationContractError
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)


def observe_certification_publication(
    store: LifecycleOperationStore, verified: LifecycleOperationRecord
) -> LifecycleOperationRecord:
    """Allow only a concurrent heartbeat before the single strict owner CAS.

    Evidence verification may be long. Heartbeat writes may continue while it runs;
    they cannot select certificates or change any authority. Every other field must
    still equal the verified observation. The caller must CAS this returned complete
    record without another evidence scan or a retry after losing that CAS.
    """
    current = store.read()
    heartbeat_fields = ("recordRevision", "heartbeatAt", "currentCommand")
    if (
        current is None
        or current.model_copy(update={name: getattr(verified, name) for name in heartbeat_fields})
        != verified
    ):
        raise CertificationContractError(
            "certification publication owner changed during evidence readback",
            (
                {
                    "code": "certification-selection-observation-moved",
                    "path": str(store.path),
                    "expected": {
                        "operationKey": verified.operationKey,
                        "generation": verified.generation,
                        "recordRevision": verified.recordRevision,
                    },
                    "observed": None
                    if current is None
                    else {
                        "operationKey": current.operationKey,
                        "generation": current.generation,
                        "recordRevision": current.recordRevision,
                    },
                    "gateStarts": 0,
                },
            ),
        )
    return current
