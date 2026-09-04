"""Read-only bounded wait on lifecycle meaningful-state changes (CCR-R15).

The wait is addressed by canonical contract, operation kind, expected public
generation, and an opaque typed afterRevision (the durable meaningful-state
cursor of a prior snapshot).  It never accepts an operation key or PID, never
acquires the lifecycle/queue/gate/worker authority, and never writes the
journal: every read goes through LifecycleOperationStore.read (lock-free),
so waiters can neither block writers nor mutate state.

Cursor semantics follow CCR-R15 exactly:

- meaningfulRevision advances only for generation, status/phase, disposition,
  approval claim, irreversible boundary, mutation evidence, typed actionable
  failure, result, cancellation/recovery, or finalization changes;
- heartbeat age, unchanged current command, log growth, queue changes, and
  repeated snapshots advance only recordRevision and never wake a waiter;
- a generation successor wakes an old-generation wait with explicit successor
  information (proved against the archived predecessor's successor fingerprint);
- wrong generation/cursor and unreadable journals refuse typed; timeout is a
  normal unchanged outcome, never a failure.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.models.lifecycles.operation_wait import (
    OUTCOME_CHANGED,
    OUTCOME_JOURNAL_REPLACED,
    OUTCOME_JOURNAL_UNREADABLE,
    OUTCOME_NO_OPERATION,
    OUTCOME_SUCCESSOR,
    OUTCOME_UNCHANGED,
    OUTCOME_WRONG_CURSOR,
    OUTCOME_WRONG_GENERATION,
    LifecycleWaitOutcome,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationReadError,
    LifecycleOperationStore,
)

# The default poll cadence of the read-only wait loop.  The loop is a bounded
# long-poll transport realization (CCR-R15 leaves long-poll versus condition/
# event implementation open); the journal is durable and the loop never prompts,
# retries, cancels, or mutates.
DEFAULT_POLL_SECONDS = 0.05

# Hard cap on how long one wait call may run regardless of the requested bound,
# so the transport can never be coerced into an unbounded server-side sleep.
MAX_WAIT_SECONDS = 60.0

# Cap on how large an archived predecessor journal the successor proof will read.
_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class LifecycleWaitClock:
    """Injectable poll cadence and time source for deterministic wait tests."""

    poll_seconds: float = DEFAULT_POLL_SECONDS
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class LifecycleWaitDecision:
    """One typed wait result addressed by contract/kind/generation/cursor."""

    outcome: LifecycleWaitOutcome
    record: LifecycleOperationRecord | None = None
    successorGeneration: int | None = None
    readError: LifecycleOperationReadError | None = None
    detail: str = ""
    elapsedSeconds: float = 0.0


# One strict journal read either yields a typed refusal or a durable record.
_WaitSnapshot = LifecycleWaitDecision | LifecycleOperationRecord


def validate_wait_cursor(after_revision: int) -> LifecycleWaitDecision | None:
    """Refuse a missing or non-positive cursor before any journal read.

    CCR-R15: the caller must address the wait with an opaque/typed afterRevision
    obtained from a prior snapshot; a missing/mismatched cursor fails typed and
    never scans for a similar task.
    """
    if after_revision < 1:
        return LifecycleWaitDecision(
            outcome=OUTCOME_WRONG_CURSOR,
            detail=(
                "the wait cursor must be a positive meaningful revision from a prior "
                "snapshot; obtain one from worktree_status before waiting"
            ),
        )
    return None


def wait_for_lifecycle_change(
    store: LifecycleOperationStore,
    *,
    expected_generation: int,
    after_revision: int,
    timeout_seconds: float,
    clock: LifecycleWaitClock | None = None,
) -> LifecycleWaitDecision:
    """Wait up to timeout_seconds for one meaningful journal change.

    Read-only and bounded.  On change it returns the current durable record plus
    its next cursor (the caller projects the R18 envelope from that exact record);
    on timeout it returns the unchanged snapshot and cursor without claiming
    failure.  Heartbeats and unchanged polling state never advance the cursor.
    """
    invalid = validate_wait_cursor(after_revision)
    if invalid is not None:
        return invalid
    clock = clock or LifecycleWaitClock()
    started = clock.monotonic()
    deadline = started + min(max(0.0, timeout_seconds), MAX_WAIT_SECONDS)
    while True:
        snapshot = _read_wait_snapshot(store, started)
        if isinstance(snapshot, LifecycleWaitDecision):
            return snapshot
        now = clock.monotonic()
        if snapshot.generation != expected_generation:
            return _generation_mismatch_decision(
                store,
                record=snapshot,
                expected_generation=expected_generation,
                elapsed_seconds=now - started,
            )
        decision = _cursor_wait_decision(
            snapshot,
            after_revision=after_revision,
            timed_out=now >= deadline,
            elapsed_seconds=now - started,
        )
        if decision is not None:
            return decision
        clock.sleep(clock.poll_seconds)


def _read_wait_snapshot(
    store: LifecycleOperationStore,
    started: float,
) -> _WaitSnapshot:
    """Read the current journal once; a strict-read failure refuses typed."""
    try:
        record = store.read()
    except LifecycleOperationReadError as error:
        return LifecycleWaitDecision(
            outcome=OUTCOME_JOURNAL_UNREADABLE,
            readError=error,
            elapsedSeconds=time.monotonic() - started,
        )
    if record is None:
        return LifecycleWaitDecision(
            outcome=OUTCOME_NO_OPERATION,
            elapsedSeconds=time.monotonic() - started,
        )
    return record


def _cursor_wait_decision(
    record: LifecycleOperationRecord,
    *,
    after_revision: int,
    timed_out: bool,
    elapsed_seconds: float,
) -> LifecycleWaitDecision | None:
    """Map one same-generation snapshot against the waited cursor."""
    if record.meaningfulRevision < after_revision:
        return LifecycleWaitDecision(
            outcome=OUTCOME_JOURNAL_REPLACED,
            record=record,
            detail=(
                "the current generation's meaningful revision is behind the waited "
                "cursor; the journal was replaced outside the store"
            ),
            elapsedSeconds=elapsed_seconds,
        )
    if record.meaningfulRevision > after_revision:
        return LifecycleWaitDecision(
            outcome=OUTCOME_CHANGED,
            record=record,
            elapsedSeconds=elapsed_seconds,
        )
    if not timed_out:
        return None
    return LifecycleWaitDecision(
        outcome=OUTCOME_UNCHANGED,
        record=record,
        elapsedSeconds=elapsed_seconds,
    )


def _generation_mismatch_decision(
    store: LifecycleOperationStore,
    *,
    record: LifecycleOperationRecord,
    expected_generation: int,
    elapsed_seconds: float,
) -> LifecycleWaitDecision:
    """Wake an old-generation wait only with explicit successor information.

    A successor is proven when the current generation is exactly one ahead of the
    waited generation AND the archived predecessor's successor fingerprint equals
    the current record fingerprint.  Anything else fails typed (wrong generation)
    rather than silently watching another generation or scanning for a similar
    task.
    """
    if record.generation == expected_generation + 1:
        archive = store.path.with_name(f"{store.path.stem}.generation-{expected_generation}.json")
        proof = _archived_successor_fingerprint(archive)
        if proof == record.fingerprint:
            return LifecycleWaitDecision(
                outcome=OUTCOME_SUCCESSOR,
                record=record,
                successorGeneration=record.generation,
                detail=(
                    "the waited generation is terminal and its exact successor is "
                    "published; re-observe the successor generation"
                ),
                elapsedSeconds=elapsed_seconds,
            )
        return LifecycleWaitDecision(
            outcome=OUTCOME_WRONG_GENERATION,
            record=record,
            detail=(
                "the current generation is one ahead of the waited generation but its "
                "successor proof is missing or mismatched"
            ),
            elapsedSeconds=elapsed_seconds,
        )
    return LifecycleWaitDecision(
        outcome=OUTCOME_WRONG_GENERATION,
        record=record,
        detail=("the waited generation no longer matches the current journal generation"),
        elapsedSeconds=elapsed_seconds,
    )


def _archived_successor_fingerprint(archive: Path) -> str | None:
    """Read the successor fingerprint from one archived predecessor generation.

    Returns None when the archive is absent, unreadable, oversized, or does
    not carry a schema-3 successor fingerprint, so a wait can never wake on an
    unproven successor claim.
    """
    try:
        raw = archive.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if len(raw.encode("utf-8")) > _MAX_ARCHIVE_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    fingerprint = payload.get("successorFingerprint")
    return fingerprint if isinstance(fingerprint, str) and fingerprint else None
