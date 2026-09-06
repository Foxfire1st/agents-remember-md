"""Disposable event and workspace builders for observer projection checks."""

import sys
import unittest
from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.observer.events import Event
from agents_remember.observer.ulid import new_ulid

T0 = "2026-06-13T18:00:00+00:00"
FRESH = datetime(2026, 6, 13, 18, 0, 30, tzinfo=UTC)  # 30s after T0  (< STALE)
STALE = datetime(2026, 6, 13, 18, 10, 0, tzinfo=UTC)  # 600s after T0 (> STALE, < TTL)
DORMANT = datetime(2026, 6, 13, 19, 30, 0, tzinfo=UTC)  # 5400s after T0 (> TTL)


@dataclass(frozen=True)
class Attribution:
    """Who produced an event and how far it can be trusted.

    The observer never reads one without the other: ``declared`` from a model is a claim,
    ``observed`` from the system is a measurement, and the projection's trust ladder grades
    the pair. Naming the four combinations the suite uses keeps a case from inventing a
    fifth by accident.
    """

    trust: str = "declared"
    actor: str = "model"


DECLARED_BY_MODEL = Attribution()


@dataclass(frozen=True)
class EnclosureRef:
    """The enclosure an event points at: its contract path and the repo that contract governs.

    A promotion carries both or neither -- the path identifies the enclosure and the repo id
    is how the projection joins it to a repository -- so they are one reference.
    """

    path: str
    repo_id: str


def _event(
    kind: str,
    *,
    lifecycle_id: str = "LC1",
    ts: str = T0,
    by: Attribution = DECLARED_BY_MODEL,
    enclosure: EnclosureRef | None = None,
    **data: object,
) -> Event:
    return Event(
        id=new_ulid(),
        ts=ts,
        kind=kind,
        trust=by.trust,  # type: ignore[arg-type]
        actor=by.actor,  # type: ignore[arg-type]
        lifecycleId=lifecycle_id,
        enclosure=enclosure.path if enclosure else None,
        repoId=enclosure.repo_id if enclosure else None,
        data=dict(data),
    )


def _started(
    *, ts: str = T0, fleeting: bool = True, phase: str = "request", lifecycle_id: str = "LC1"
) -> Event:
    return _event(
        "lifecycle.started", ts=ts, lifecycle_id=lifecycle_id, fleeting=fleeting, phase=phase
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
