"""``ar-durable-store/1.0`` contract for control-plane JSONL stores.

Contract:
- records are append-only JSONL with ``schemaVersion=1.0`` and fold by id;
- every append and rewrite holds a per-log in-process mutex and POSIX ``flock``;
- rewrites publish through ``kernel.atomic_write`` and never unlink an empty log;
- compaction ownership is advisory, while the lock is unconditional;
- the MCP and dashboard processes must share a local POSIX filesystem.

``exclusive_access`` probes each lockfile. A double success is refused with
``UnsafeLockFilesystemError``.

Read policy is part of each store's authority contract:

- Gate, expectation-row, and operator-inbox stores read strictly because malformed or
  skipped records could change whether an approval or mutation is permitted. Their
  rewrites must use strict input.
- Attention-dismissal, orchestration-nudge, and agent-notifier-cooldown stores read
  tolerantly for projection availability. Their rewrites may permanently drop malformed
  rows, which is acceptable only while those rows carry no mutation authority.

Before using a tolerant log to suppress or permit a mutation, move its read/rewrite path
to strict validation in the same change.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, field_validator

from agents_remember.errors import LockCapabilityError
from agents_remember.kernel import file_lock
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.kernel.primitives import checkout_coordination

DURABLE_STORE_CONTRACT = "ar-durable-store/1.0"
"""The contract these stores implement, as declared in the front matter above."""

SCHEMA_VERSION = "1.0"
"""The ``schemaVersion`` stamped on every record written through this contract.

``MAJOR.MINOR``. A reader REJECTS an unknown major (the record means something it cannot be
trusted to interpret) and ACCEPTS an unknown minor (additive, and pydantic's ``extra="forbid"``
is the separate guard against a field nobody declared). This is the whole versioning story:
there is deliberately no migration framework, because nothing exists to migrate yet and the
capability that is cheap now and unbuildable later is telling an old record from a new one.
"""

SUPPORTED_SCHEMA_MAJOR = 1

ProcessRole = Literal["mcp", "dashboard", "lifecycle-operation"]
"""Every declared execution process that can write a shared durable store."""

CompactionRole = Literal["mcp", "dashboard"]


class DurableStoreError(RuntimeError):
    """A durable-store contract violation. Never downgraded to a warning or a no-op."""


class CompactionOwnerError(DurableStoreError):
    """A process that is not a log's declared compaction owner tried to rewrite it."""


class UnsafeLockFilesystemError(DurableStoreError):
    """``flock`` on the coordination root does not actually exclude, so it cannot be relied on."""


# Kernel owns execution mode; this module exposes the shared-store writer view.


def declare_process_role(role: ProcessRole) -> None:
    """Declare which concurrent shared-store writer this process is.

    ``mcp/server.py::main`` and ``cli/dashboard.py::{run,_dev_app}`` call this at their process
    entry points. Detached lifecycle workers declare their own mode before touching operation,
    gate, or queue stores. Factories used in-process do not declare a role. The reload worker
    declares in ``_dev_app`` because it starts in a fresh interpreter. Undeclared CLI and test
    processes skip advisory writer checks; unconditional log locking still protects their writes.
    """
    checkout_coordination.declare_execution_mode(role)


def declared_process_role() -> ProcessRole | None:
    """Return a declared shared-store writer; CLI and explicit test modes have no role."""

    mode = checkout_coordination.declared_execution_mode()
    return cast(ProcessRole, mode) if mode in {"mcp", "dashboard", "lifecycle-operation"} else None


@dataclass(frozen=True)
class StoreOwnership:
    """Declared writers, compaction owner, and ownership rationale for one durable log.

    ``writers`` names roles permitted to append. ``compaction_owner`` names the role that runs
    destructive reclaim, or ``None`` when no single role owns it. Every store locks regardless of
    ownership; there is no per-store serialization switch. Declared daemon roles are checked at
    each write entry point, while undeclared CLI/test processes remain protected by the lock.
    """

    store: str
    writers: tuple[ProcessRole, ...]
    compaction_owner: CompactionRole | None
    rationale: str

    def check_declared_writer(self) -> None:
        """ADVISORY. Refuse a write from a DECLARED role that is not one of this log's writers.

        Silent in any process that never called :func:`declare_process_role` -- which is every
        CLI invocation, script and test. It catches a new writer appearing in the MCP server or
        the dashboard, where the writer set is a claim this contract makes; it guarantees nothing
        anywhere else, and the log's lock is what keeps those processes safe.
        """
        role = declared_process_role()
        if role is not None and role not in self.writers:
            raise CompactionOwnerError(
                f"{self.store}: written by {'/'.join(self.writers)}, not by the {role} process. "
                f"Adding a writer changes this store's concurrency contract -- update its "
                f"StoreOwnership so the lock decision is re-made with the new writer in view."
            )

    def is_compaction_owner(self) -> bool:
        """Whether this process should run the log's reclaim pass.

        Shared reclaim call sites ask this predicate before rewriting. It returns true for an
        undeclared CLI/test process, a store without a single owner, or the declared owner role.
        Long-lived MCP/dashboard entry points, including the dashboard reload worker, must declare
        their role. Ownership selects who reclaims; unconditional locking provides write safety.
        """
        role = declared_process_role()
        return role is None or self.compaction_owner is None or role == self.compaction_owner


# Ownership register for all six stores. Writer and compaction roles are advisory daemon checks;
# every store takes its log lock unconditionally.

GATE_OWNERSHIP = StoreOwnership(
    store="gate",
    writers=("mcp", "dashboard", "lifecycle-operation"),
    compaction_owner="mcp",
    rationale=(
        "The MCP process mints, decides, applies and deletes gates (application/gate_tools.py, "
        "worktrees/modules/closeout.py), so it owns reclamation. The dashboard appends too -- "
        "serving/hosted_interactions.py raises and resolves agent-question gates for "
        "adapter-owned sessions -- so the rewrite is serialized against it. Compaction used to "
        "run on the dashboard's 30s projection tick, which owned nothing here; that is the "
        "reclaim pass this ownership moved."
    ),
)

EXPECTATION_ROW_OWNERSHIP = StoreOwnership(
    store="expectation-rows",
    writers=("mcp", "dashboard"),
    compaction_owner="dashboard",
    rationale=(
        "The agent-notifier sweep (serving/agent_notifier.py) is the only reclamation pass this log has "
        "and it needs the folded snapshot it produces, so the dashboard owns compaction. Every "
        "dispatch surface appends, and half of them are MCP tools (gate open, inbox post), so "
        "the rewrite is serialized against those appends."
    ),
)

ATTENTION_DISMISSAL_OWNERSHIP = StoreOwnership(
    store="attention-dismissals",
    writers=("dashboard",),
    compaction_owner="dashboard",
    rationale=(
        "Single writer: the dashboard both records dismissals (serving/app.py) and prunes them "
        "on the projection pass (observer/projection_store.py); nothing in the MCP process "
        "touches this log. This store has no plain append -- both of its writes are whole-file "
        "rewrites -- so check_declared_writer is called from BOTH of them (dismiss and "
        "prune_lifecycles) rather than from a single append, which is what keeps the "
        "single-writer claim checkable inside the daemons. "
        "Locked all the same -- a dismissal is a whole-file read-modify-write, so the "
        "HTTP dismiss route and the projection sweep are themselves a lost-update pair, and a "
        "draft that left this log unlocked on the strength of single-writer measured 31.45% "
        "loss."
    ),
)

OPERATOR_INBOX_OWNERSHIP = StoreOwnership(
    store="operator-inbox",
    writers=("mcp", "dashboard"),
    compaction_owner=None,
    rationale=(
        "THE DECLARED EXCEPTION (leaf 260731-EFA-L5 R2). This is the one store that genuinely "
        "cannot be given a single compaction owner: both processes must physically remove rows, "
        "not merely append them. The MCP deletes the inbox rows tied to a cancelled gate "
        "(application/gate_tools.py delete_by_gate) at the moment the gate is cancelled, while the "
        "dashboard's agent-notifier sweep must resolve and compact under one held lock "
        "(reconcile_and_compact) so that a consume which won the lock stays terminal. Neither "
        "can be moved to the other process without moving the decision it implements. It is "
        "therefore the one log where the lock is the whole mechanism rather than the backstop "
        "behind an owner -- which is what it already was before this leaf, and why its "
        "pre-existing flock was the right call kept rather than a habit inherited."
    ),
)

ORCHESTRATION_NUDGE_OWNERSHIP = StoreOwnership(
    store="orchestration-nudges",
    writers=("mcp", "dashboard"),
    compaction_owner="dashboard",
    rationale=(
        "Appended by the MCP nudge tool and by the agent-notifier sweep. No production reclaim pass "
        "exists yet; replace_records is the store's declared rewrite entry point, and the "
        "agent-notifier is the only sweep that could ever drive it, so the dashboard is named owner "
        "now rather than left to be decided by whoever writes the reclaim pass."
    ),
)

AGENT_NOTIFIER_SIGNAL_OWNERSHIP = StoreOwnership(
    # Retained legacy store identity during the rename window (lock derivation + error text);
    # removal rides the cooldown-log schema migration.
    store="supervisor-signals",
    writers=("dashboard",),
    compaction_owner="dashboard",
    rationale=(
        "Single writer: the agent-notifier sweep is the only thing that appends a signal and the "
        "only thing that compacts the cooldown log. Locked all the same, for the same reason as "
        "attention-dismissals -- the claim is about today's callers, the lock is about the file."
    ),
)


def schema_version_supported(value: str) -> bool:
    """Whether a record's ``schemaVersion`` is one this build may interpret.

    Rejects an unknown major, accepts an unknown minor. An unparseable version is rejected:
    a record that cannot say what it is cannot be trusted to be what we assume.

    UNKNOWN MEANS "NOT THIS ONE", IN BOTH DIRECTIONS -- the comparison is equality, not ``<=``.
    An earlier form accepted any major at or below :data:`SUPPORTED_SCHEMA_MAJOR`, which let
    ``"0.9"`` through while every docstring in this module and in ``worktrees/worktree_contract``
    said an unknown major is refused. Equality is the honest rule and it is also the correct one:
    there has never been a 0.x record. :data:`SCHEMA_VERSION` has been ``1.0`` since this contract
    existed and a record with no version field defaults to it, so nothing this tree can read was
    ever written under an older major. A row claiming one is corruption or a foreign artifact, and
    the reason to refuse it is the same as the reason to refuse ``"2.0"``: the record says it means
    something this code was not written to interpret. If a major 0 format is ever genuinely
    introduced, accepting it is a deliberate change here plus the migration to go with it, not a
    comparison operator that already quietly said yes.
    """
    major, _, minor = value.partition(".")
    if not major.isdigit() or not minor.isdigit():
        return False
    return int(major) == SUPPORTED_SCHEMA_MAJOR


class DurableRecord(BaseModel):
    """The fields and validation every record written through this contract shares.

    ``extra="forbid"`` is inherited rather than repeated per store (all six already declared
    it), and ``schemaVersion`` is validated on the way in, which is what gives the two read
    policies their behaviour for free: an unknown major raises ``ValidationError``, so a strict
    reader fails loudly and a tolerant reader skips the row, with no version check written into
    either of them.
    """

    model_config = ConfigDict(extra="forbid")

    schemaVersion: str = SCHEMA_VERSION

    @field_validator("schemaVersion")
    @classmethod
    def _reject_unknown_major(cls, value: str) -> str:
        if not schema_version_supported(value):
            raise ValueError(
                f"unsupported schemaVersion {value!r}: this build implements "
                f"{DURABLE_STORE_CONTRACT} (major {SUPPORTED_SCHEMA_MAJOR}). A newer major means "
                f"the record says something this code cannot be trusted to read."
            )
        return value


def migrate_jsonl_records[RecordT: BaseModel](
    log_path: Path,
    ownership: StoreOwnership,
    model: type[RecordT],
    transform: Callable[[dict[str, Any]], dict[str, Any]],
) -> int:
    """Apply one explicit schema migration under the log's existing lock.

    The transform receives each raw JSON object before current-schema validation. The whole log
    is validated before one atomic replacement, so a failed row leaves the original file intact.
    Re-running after migration is a no-op; this is a bounded migration, not a parallel reader.
    """

    ownership.check_declared_writer()
    with exclusive_access(log_path, ownership):
        source = [line for line in read_log_text(log_path).splitlines() if line.strip()]
        if not source:
            return 0
        changed = 0
        records: list[RecordT] = []
        for line in source:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError(f"{ownership.store}: durable record is not a JSON object")
            migrated = transform(raw)
            if migrated != raw:
                changed += 1
            records.append(model.model_validate(migrated))
        if changed:
            rewrite_lines(
                log_path,
                [record.model_dump_json(by_alias=True, exclude_none=True) for record in records],
                ownership,
            )
        return changed


@contextmanager
def exclusive_access(log_path: Path, ownership: StoreOwnership) -> Iterator[None]:
    """Serialize appends and rewrites of one log against every other writer of that log.

    Unconditional -- there is no store and no process this is skipped for. ``ownership`` never
    decides WHETHER to lock; it names the log in the refusal when the filesystem turns out not
    to support locking at all.

    Held across a whole read-filter-rewrite, never around the rewrite alone: a list of records
    chosen by a read that happened outside the lock is already stale, and rewriting from it is
    the lost update under a different name -- see :func:`require_lock_held`.

    Re-entrant within one thread, because ``flock`` treats two file descriptions on one path as
    separate holders and would deadlock a nested acquisition against itself. See
    :class:`kernel.file_lock._LockDepth` for why the counter is per-thread and not per-process.

    TWO locks, taken in ONE order, always: the in-process mutex first and the log's ``flock``
    inside it. :func:`kernel.file_lock.thread_mutex_for` says what the mutex adds over ``flock``
    and what it deliberately does not claim to add. The order is what makes the pair safe -- taking ``flock``
    first would leave a thread holding a lock every other PROCESS waits on while it queues behind
    a thread of its own, and it would put the once-per-path capability probe (which takes that
    same ``flock`` twice) behind a hold this process already has. Mutex first, so a thread that
    is going to wait waits before it holds anything another host-local process needs.

    The nested acquisition returns BEFORE either lock, so the outermost frame on a thread owns
    the mutex for the whole nest and a re-entrant call cannot queue behind itself.

    ONE ORDER ACROSS STORES, TOO: no thread may hold one store's lock --
    mutex, RLock, or flock -- while acquiring another store's lock. Evidence a transaction
    needs from a second store is gathered BEFORE entering this one, or the side effect runs
    AFTER leaving it; never nested. The two nestings this rule now forbids were each locally
    documented (the liveness sweep's catalog batch across the synchronizer's inbox/gate locks;
    the agent-notifier's inbox transaction across a catalog read) and their unexamined composition
    deadlocked the serving daemon in production on 2026-08-05 -- an ABBA no single store's own
    ordering rule could see, because each store's order was internally correct.
    """
    checkout_coordination.require_durable_write_target(log_path)
    try:
        with file_lock.exclusive_file_lock(log_path, ownership.store):
            yield
    except LockCapabilityError as error:
        raise UnsafeLockFilesystemError(str(error)) from error


def require_lock_held(log_path: Path, store: str) -> None:
    """Refuse a rewrite whose caller is not holding the log's lock. The invariant, checked.

    A rewrite takes a list of records somebody chose. If that choice came from a read outside
    the lock, every record appended in between is about to be discarded -- the lost update this
    contract exists to remove, reached through code that looked safe because it locked its own
    write. Holding the lock across the read AND the rewrite is therefore the invariant, and this
    is the one place it is checked rather than remembered: :func:`rewrite_lines` calls it, so no
    store can rewrite a log it has not locked, however the call was reached.

    Unlike the ownership predicate this DOES raise, and it can afford to: it asks about this
    thread's own lock rather than about a process-wide declaration, so it is true or false for
    real in every process, test hosts included.
    """
    if not file_lock.lock_held(log_path):
        raise DurableStoreError(
            f"{store}: rewrite attempted without holding {log_path.name}'s lock. Hold "
            f"exclusive_access across the read AND the rewrite, or use the store's compact()."
        )


def read_log_text(log_path: Path) -> str:
    """The log's raw text, or ``""`` when it does not exist yet. The one read both policies share."""
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8")


def append_line(log_path: Path, line: str) -> None:
    """Append one record. The caller holds :func:`exclusive_access`; this only writes.

    ``fsync`` before the handle closes: an append-only log whose last records are still in the
    page cache is not durable across a host loss, and every store here exists to survive exactly
    the restart that a durable timer would not.
    """
    _prepare_append_target(log_path)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def append_lines(log_path: Path, lines: list[str]) -> None:
    """Append several records with one flush while the caller holds the store lock.

    A transition batch is one durability unit: either every validated snapshot reaches the
    append before the single fsync, or the caller raises before writing any of them. This avoids
    turning a bounded sweep into one full log fold and one disk blocker per row.
    """
    if not lines:
        return
    _prepare_append_target(log_path)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("".join(f"{line}\n" for line in lines))
        handle.flush()
        os.fsync(handle.fileno())


def rewrite_lines(log_path: Path, lines: list[str], ownership: StoreOwnership) -> None:
    """Rewrite a locked log through the shared atomic-publish owner.

    The caller must already hold this log's exclusive store lock. An empty record set writes
    an empty file and never unlinks the destination.
    """
    _require_rewrite_access(log_path, ownership.store)
    atomic_write_text(log_path, "".join(f"{line}\n" for line in lines))


def _prepare_append_target(log_path: Path) -> None:
    checkout_coordination.require_durable_write_target(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)


def _require_rewrite_access(log_path: Path, store: str) -> None:
    checkout_coordination.require_durable_write_target(log_path)
    require_lock_held(log_path, store)
