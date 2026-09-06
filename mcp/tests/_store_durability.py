"""Force an append into the compaction window of eight real JSONL stores.

Forked processes use disposable roots and independent receipt-versus-disk
accounting. Bounded rendezvous and joins preserve termination when locking
correctly serializes the append behind compaction.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agents_remember.controlplane.agent_notifier_signals import (
    AgentNotifierSignalCooldownStore,
    AgentNotifierSignalRecord,
)
from agents_remember.controlplane.attention_dismissals import (
    AttentionDismissalRecord,
    AttentionDismissalStore,
)
from agents_remember.controlplane.expectation_rows import ExpectationRow, ExpectationRowStore
from agents_remember.controlplane.operator_inbox_records import (
    OperatorInboxEntry,
    OperatorInboxState,
)
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.controlplane.orchestration_nudges import (
    OrchestrationNudgeRecord,
    OrchestrationNudgeStore,
    replace_records,
)
from agents_remember.controlplane.records import (
    GateRecord,
)
from agents_remember.controlplane.store import GateStore
from agents_remember.models.structural.gates import GateState
from agents_remember.providers.degradation import (
    DEGRADATION_EVENT_SCHEMA,
    ProviderDegradationStore,
)
from agents_remember.providers.metrics import (
    PROVIDER_METRICS_SCHEMA,
    ContainerSample,
    MetricsSnapshot,
    ProviderMetricsStore,
)

# Survivors are what the harness accounts for. The anchor is a record that is never prunable and
# never counted: it keeps the reclaimed set non-empty so a reclaim pass exercises the tmp +
# ``os.replace`` rewrite rather than removing the empty log, which would
# otherwise obscure the read-to-replace race this scenario forces.
SURVIVOR_PREFIX = "survivor-"
ANCHOR_ID = "anchor-keepalive"
DECOY_PREFIX = "decoy-"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


# --------------------------------------------------------------------------------------------
# Per-store adapters
# --------------------------------------------------------------------------------------------


class StoreAdapter:
    """One control-plane store expressed as the four operations the harness needs.

    ``write`` is whatever that store calls "record this fact" (an ``append``, or -- for the
    attention store, which has no append at all -- a read-modify-write ``dismiss``); ``reclaim``
    is that store's own real reclaim entry point, never a reimplementation of it, so the harness
    measures shipped behaviour and not a model of it.
    """

    name = ""
    log_name = ""
    id_field = "id"

    def open(self, root: Path) -> Any:  # pragma: no cover
        raise NotImplementedError

    def write(self, store: Any, record_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def write_decoy(self, store: Any, tick: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def reclaim_now(self, store: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def log_path(self, root: Path) -> Path:
        return root / "workspace" / self.log_name

    def seed(self, root: Path) -> None:
        store = self.open(root)
        self.write(store, ANCHOR_ID)
        self.write_decoy(store, -1)


class GateAdapter(StoreAdapter):
    name = "gate"
    log_name = "gates.jsonl"

    def open(self, root: Path) -> Any:
        return GateStore(root)

    def _record(self, record_id: str, state: GateState) -> GateRecord:
        return GateRecord(id=record_id, ts=_iso(_now()), kind="closeout-approval", state=state)

    def write(self, store: Any, record_id: str) -> None:
        store.append(self._record(record_id, "approved"))

    def write_decoy(self, store: Any, tick: int) -> None:
        # ``expired`` is in PRUNE_IMMEDIATE_GATE_STATES, so the next compact drops it.
        # NOT ``applied``: an applied snapshot of a consumed-approval kind is retained forever
        # (260731-EFA-L5 R1 -- it is the record that stops one approval being spent twice), so a
        # decoy in that state would leave nothing prunable and the reclaim tick would never
        # rewrite, quietly turning this whole harness into a no-op.
        store.append(self._record(f"{DECOY_PREFIX}{os.getpid()}-{tick}", "expired"))

    def reclaim_now(self, store: Any) -> None:  # pragma: no cover
        store.compact(None, now=_now())


class ExpectationAdapter(StoreAdapter):
    name = "expectation"
    log_name = "expectation-rows.jsonl"
    RETAIN_SECONDS = 60.0

    def open(self, root: Path) -> Any:
        return ExpectationRowStore(root)

    def write(self, store: Any, record_id: str) -> None:
        stamp = _iso(_now())
        store.append(
            ExpectationRow(
                id=record_id,
                ts=stamp,
                kind="ack-by",
                state="pending",
                createdAt=stamp,
                dueAt=stamp,
                sourceId="durability-harness",
            )
        )

    def write_decoy(self, store: Any, tick: int) -> None:
        # A terminal row whose terminal stamp is older than the retention window: prunable.
        stale = _iso(_now() - timedelta(seconds=self.RETAIN_SECONDS * 60))
        store.append(
            ExpectationRow(
                id=f"{DECOY_PREFIX}{os.getpid()}-{tick}",
                ts=stale,
                kind="ack-by",
                state="met",
                createdAt=stale,
                dueAt=stale,
                sourceId="durability-harness",
                metAt=stale,
            )
        )

    def reclaim_now(self, store: Any) -> None:  # pragma: no cover
        store.compact(now=_now(), retain_seconds=self.RETAIN_SECONDS)


class AttentionAdapter(StoreAdapter):
    name = "attention"
    log_name = "attention-dismissals.jsonl"
    id_field = "itemId"
    # ``dismiss`` is a whole-file read-modify-write, not an append -- there is no ``"a"`` handle
    # to strand in an unlinked inode, so only the lost-update mechanism applies here.
    LIVE_LIFECYCLE = "live-lifecycle"

    def open(self, root: Path) -> Any:
        return AttentionDismissalStore(root)

    def write(self, store: Any, record_id: str) -> None:
        store.dismiss(
            AttentionDismissalRecord(
                itemId=record_id,
                dismissedAt=_iso(_now()),
                kind="gate-open",
                lifecycleId=self.LIVE_LIFECYCLE,
            )
        )

    def write_decoy(self, store: Any, tick: int) -> None:
        # A dismissal for a lifecycle that is not in the live set: dropped by prune_lifecycles.
        store.dismiss(
            AttentionDismissalRecord(
                itemId=f"{DECOY_PREFIX}{os.getpid()}-{tick}",
                dismissedAt=_iso(_now()),
                kind="gate-open",
                lifecycleId="retired-lifecycle",
            )
        )

    def reclaim_now(self, store: Any) -> None:  # pragma: no cover
        store.prune_lifecycles({self.LIVE_LIFECYCLE})


class OperatorInboxAdapter(StoreAdapter):
    name = "operator_inbox"
    log_name = "operator-inbox.jsonl"

    def open(self, root: Path) -> Any:
        return OperatorInboxStore(root)

    def _entry(self, record_id: str, state: OperatorInboxState) -> OperatorInboxEntry:
        stamp = _iso(_now())
        return OperatorInboxEntry(
            id=record_id,
            ts=stamp,
            state=state,
            lifecycleId="live-lifecycle",
            ask="durability harness",
            response="durability harness",
            createdAt=stamp,
            createdBy="durability-harness",
            createdVia="cli",
        )

    def write(self, store: Any, record_id: str) -> None:
        store.append(self._entry(record_id, "pending"))

    def write_decoy(self, store: Any, tick: int) -> None:
        # ``ladder-resolved`` rows drop immediately in ``_keep_inbox_entry``.
        store.append(self._entry(f"{DECOY_PREFIX}{os.getpid()}-{tick}", "ladder-resolved"))

    def reclaim_now(self, store: Any) -> None:  # pragma: no cover
        store.compact(now=_now())


class NudgeAdapter(StoreAdapter):
    name = "nudge"
    log_name = "orchestration-nudges.jsonl"

    def open(self, root: Path) -> Any:
        return OrchestrationNudgeStore(root)

    def _record(self, record_id: str) -> OrchestrationNudgeRecord:
        return OrchestrationNudgeRecord(
            id=record_id,
            ts=_iso(_now()),
            state="sent",
            reason="manual",
            message="durability harness",
        )

    def write(self, store: Any, record_id: str) -> None:
        store.append(self._record(record_id))

    def write_decoy(self, store: Any, tick: int) -> None:
        store.append(self._record(f"{DECOY_PREFIX}{os.getpid()}-{tick}"))

    @staticmethod
    @contextlib.contextmanager
    def _reclaim_lock(path: Path) -> Any:  # pragma: no cover
        """Hold the nudge log's lock across read + filter + rewrite, if the tree has one.

        This store is the only one of the six with no ``compact`` method: ``replace_records`` is
        its documented rewrite entry point, so the read-filter half of a reclaim belongs to the
        caller -- and a caller that reads outside the lock reintroduces the exact lost update the
        lock exists to prevent. The harness therefore reclaims the way a correct compaction owner
        would, so a failure here is the store's and not the harness's.
        """
        try:
            # Deliberately local: this module also runs against a `git archive` of the leaf's
            # base commit, where `durable_store` does not exist yet. A top-level import would
            # make the harness unable to measure the very tree it exists to measure.
            from agents_remember.controlplane.durable_store import (  # noqa: PLC0415
                ORCHESTRATION_NUDGE_OWNERSHIP,
                exclusive_access,
            )
        except ImportError:
            yield  # base commit: there is no lock to hold
            return
        with exclusive_access(path, ORCHESTRATION_NUDGE_OWNERSHIP):
            yield

    def reclaim_now(self, store: Any) -> None:  # pragma: no cover
        path = store.log_path()
        with self._reclaim_lock(path):
            kept = [record for record in store.read() if not record.id.startswith(DECOY_PREFIX)]
            replace_records(path, kept)


class AgentNotifierSignalAdapter(StoreAdapter):
    name = "agent_notifier_signal"
    log_name = "supervisor-signals.jsonl"
    RETAIN_SECONDS = 300.0

    def open(self, root: Path) -> Any:
        return AgentNotifierSignalCooldownStore(root)

    def _record(self, record_id: str, stamp: str) -> AgentNotifierSignalRecord:
        return AgentNotifierSignalRecord(
            id=record_id,
            ts=stamp,
            state="sent",
            findingKind="durability-harness",
            detail="durability harness",
            deliveryState="queued",
        )

    def write(self, store: Any, record_id: str) -> None:
        store.append(self._record(record_id, _iso(_now())))

    def write_decoy(self, store: Any, tick: int) -> None:
        stale = _iso(_now() - timedelta(seconds=self.RETAIN_SECONDS * 60))
        store.append(self._record(f"{DECOY_PREFIX}{os.getpid()}-{tick}", stale))

    def reclaim_now(self, store: Any) -> None:  # pragma: no cover
        store.compact(now=_now(), retain_seconds=self.RETAIN_SECONDS)


class ProviderStoreAdapter(StoreAdapter):
    """The two ``providers/`` logs, which sit under a coordination root rather than an observer one.

    ``StoreAdapter.log_path`` resolves ``<root>/workspace/<log>`` because that is where the six
    control-plane logs live. Both provider stores build their own directory from a COORDINATION
    root instead (``<root>/logs/observer/providers``), so the harness root means the same thing to
    them -- a throwaway tree -- and only the path under it differs.
    """

    def log_path(self, root: Path) -> Path:
        return root / "logs" / "observer" / "providers" / self.log_name


class ProviderMetricsAdapter(ProviderStoreAdapter):
    """``providers/metrics.py`` -- the store the leaf's first pass left outside ``controlplane/``.

    The pairing measured here is the shipped one, not a constructed one. ``write`` is
    ``record_index_state``, which the MCP process appends through on its provider-setup thread
    (``providers/provider_setup.py`` ``_record_index_state``, reached from ``worktree_start`` /
    ``runtime_install``); ``reclaim`` is the dashboard's ``_metrics_loop`` in the order that loop
    runs it -- ``record`` and then ``compact`` (``serving/app.py``).

    ``max_bytes=0`` holds the byte-budget guard open so every tick really rewrites, and
    ``retain_rows`` is set far above anything a run can produce so RETENTION never drops a
    survivor. A missing record is then a lost update and cannot be a truncation.
    """

    name = "provider_metrics"
    log_name = "metrics.jsonl"
    RETAIN_ROWS = 1_000_000

    def open(self, root: Path) -> Any:
        return ProviderMetricsStore(root)

    def write(self, store: Any, record_id: str) -> None:
        store.record_index_state({"id": record_id, "provider": "durability-harness"})

    def write_decoy(self, store: Any, tick: int) -> None:
        store.record(
            MetricsSnapshot(
                schema=PROVIDER_METRICS_SCHEMA,
                sampledAt=_iso(_now()),
                containers=[
                    ContainerSample(
                        name=f"{DECOY_PREFIX}{os.getpid()}-{tick}",
                        provider="durability-harness",
                        instance="harness",
                        running=True,
                        restarts=0,
                    )
                ],
            )
        )

    def reclaim_now(self, store: Any) -> None:  # pragma: no cover
        store.compact(retain_rows=self.RETAIN_ROWS, max_bytes=0)


class ProviderDegradationAdapter(ProviderStoreAdapter):
    """``providers/degradation.py`` -- the provider degradation audit log.

    ``compact_events`` reclaims by ROW COUNT and skips the rewrite entirely while the log is
    under ``retain_rows``, so the seed pre-fills the log to exactly that many rows: from the first
    tick onward every reclaim rewrites, and the newest ``retain_rows`` always covers every record
    the run appends, so retention can never be mistaken for loss.

    The seeded backlog and decoy rows precede the anchor and appended survivor,
    keeping both within the retained window while forcing a real rewrite.
    """

    name = "provider_degradation"
    log_name = "degradation-events.jsonl"
    RETAIN_ROWS = 1_500

    def open(self, root: Path) -> Any:
        return ProviderDegradationStore(root)

    def _event(self, record_id: str) -> dict[str, Any]:
        return {
            "schema": DEGRADATION_EVENT_SCHEMA,
            "id": record_id,
            "at": _iso(_now()),
            "from": "healthy",
            "to": "degraded",
        }

    def write(self, store: Any, record_id: str) -> None:
        store.append_event(self._event(record_id))

    def write_decoy(self, store: Any, tick: int) -> None:
        store.append_event(self._event(f"{DECOY_PREFIX}{os.getpid()}-{tick}"))

    def reclaim_now(self, store: Any) -> None:  # pragma: no cover
        store.compact_events(retain_rows=self.RETAIN_ROWS)

    def seed(self, root: Path) -> None:
        # Bulk-written rather than appended one call at a time: the seed is setup, not
        # measurement, and 2,000 locked-and-fsynced appends would cost more than the run.
        path = self.log_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for index in range(self.RETAIN_ROWS):
                handle.write(json.dumps(self._event(f"seed-{index}"), sort_keys=True) + "\n")
        super().seed(root)


CONTROLPLANE_ADAPTERS: tuple[type[StoreAdapter], ...] = (
    GateAdapter,
    ExpectationAdapter,
    AttentionAdapter,
    OperatorInboxAdapter,
    NudgeAdapter,
    AgentNotifierSignalAdapter,
)
PROVIDER_ADAPTERS: tuple[type[StoreAdapter], ...] = (
    ProviderMetricsAdapter,
    ProviderDegradationAdapter,
)
ADAPTERS: dict[str, type[StoreAdapter]] = {
    adapter.name: adapter for adapter in (*CONTROLPLANE_ADAPTERS, *PROVIDER_ADAPTERS)
}
# ``CASES`` stays the six control-plane stores and ``PROVIDER_CASES`` is separate, so adding the
# provider stores to the shared instrument does not silently widen what the control-plane
# contract test asserts -- each test file names the stores it speaks for.
CASES: tuple[str, ...] = tuple(adapter.name for adapter in CONTROLPLANE_ADAPTERS)
PROVIDER_CASES: tuple[str, ...] = tuple(adapter.name for adapter in PROVIDER_ADAPTERS)


# --------------------------------------------------------------------------------------------
# Raw on-disk accounting (independent of any store's own read policy)
# --------------------------------------------------------------------------------------------


def surviving_ids(path: Path, id_field: str) -> tuple[set[str], int]:  # pragma: no cover
    """Survivor ids physically present in the log, plus a count of unparseable lines.

    Deliberately NOT the store's own ``read``: a store that raises on a torn line would turn a
    durability measurement into an exception, and a store that skips one would let a torn line be
    reported as a lost record. Loss and tearing are different defects and are counted separately.
    """
    if not path.exists():
        return set(), 0
    found: set[str] = set()
    torn = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            torn += 1
            continue
        if not isinstance(payload, dict):
            torn += 1
            continue
        record_id = payload.get(id_field)
        if isinstance(record_id, str) and record_id.startswith(SURVIVOR_PREFIX):
            found.add(record_id)
    return found, torn


# --------------------------------------------------------------------------------------------
# Child process entry points
# --------------------------------------------------------------------------------------------


@contextlib.contextmanager
def parked_rewrite(ready: Any, released: Any, seconds: float) -> Any:  # pragma: no cover
    """Park the next in-process log rewrite between its read and its commit.

    Two interposition points, whichever the implementation reaches first: ``Path.write_text``
    (every one of these stores materialises its temp file that way) and ``os.replace`` (the last
    instruction of every rewrite). Hooking both means the pause lands in the read->commit window
    even if the rewrite above it is restructured -- e.g. a fix that swaps ``write_text`` for an
    explicit handle it can ``fsync`` still commits through ``os.replace``.

    Pausing at ``write_text`` rather than only at ``os.replace`` also matters for correctness of
    the *measurement*: these stores name their temp file after the log with no pid in it, so two
    processes parked at ``os.replace`` collide on one temp path and the scenario degenerates into
    a ``FileNotFoundError`` instead of the silent lost update it is meant to expose.

    ``ready`` fires when the rewrite is parked; the pause then lasts until ``released`` is set or
    ``seconds`` elapse. The timeout is what keeps this terminating: an implementation that
    serialises the other process out (the fix) never sets ``released``, so the rewrite resumes on
    its own instead of deadlocking.
    """
    real_write_text = Path.write_text
    real_replace = os.replace
    armed = [True]

    def park() -> None:  # pragma: no cover
        if armed[0]:
            armed[0] = False
            ready.set()
            released.wait(seconds)

    def hooked_write_text(self: Path, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        park()
        return real_write_text(self, *args, **kwargs)

    def hooked_replace(src: Any, dst: Any) -> None:  # pragma: no cover
        park()
        real_replace(src, dst)

    Path.write_text = hooked_write_text  # type: ignore[assignment]
    os.replace = hooked_replace  # type: ignore[assignment]
    try:
        yield
    finally:
        Path.write_text = real_write_text  # type: ignore[assignment]
        os.replace = real_replace  # type: ignore[assignment]
        ready.set()


def _forced_reclaimer_main(
    spec: dict[str, Any], ready: Any, appended: Any
) -> None:  # pragma: no cover
    """Reclaim once, parked inside the rewrite so an append can interleave.

    The decoy is written BEFORE arming, because on a read-modify-write store (attention
    dismissals) writing the decoy is itself a rewrite and would spring the hook early.
    """
    adapter = ADAPTERS[spec["case"]]()
    store = adapter.open(Path(spec["root"]))
    adapter.write_decoy(store, 0)
    try:
        with parked_rewrite(ready, appended, spec["handoff_seconds"]):
            adapter.reclaim_now(store)
    except Exception as exc:
        Path(spec["errors"]).write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")


def _forced_appender_main(
    spec: dict[str, Any], ready: Any, appended: Any
) -> None:  # pragma: no cover
    """Append one survivor once the reclaimer is parked mid-rewrite."""
    adapter = ADAPTERS[spec["case"]]()
    store = adapter.open(Path(spec["root"]))
    ready.wait(spec["handoff_seconds"] * 3)
    try:
        adapter.write(store, spec["record_id"])
        Path(spec["receipt"]).write_text(spec["record_id"], encoding="utf-8")
    except Exception as exc:
        Path(spec["errors"]).write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
    finally:
        appended.set()


# --------------------------------------------------------------------------------------------
# Scenarios (parent side)
# --------------------------------------------------------------------------------------------


def _context() -> Any:
    return multiprocessing.get_context("fork")


def _join(processes: list[Any], timeout: float) -> list[str]:  # pragma: no cover
    deadline = time.monotonic() + timeout
    stragglers: list[str] = []
    for process in processes:
        process.join(max(0.0, deadline - time.monotonic()))
        if process.is_alive():
            stragglers.append(process.name)
            process.kill()
            process.join(5.0)
    return stragglers


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def harness_work_dir(root: Path) -> Path:
    """Keep receipts separate from store bytes and unique to each disposable root."""
    return root.with_name(root.name + "-harness")


def _prepared_work_dir(root: Path) -> Path:
    work = harness_work_dir(root)
    work.mkdir(parents=True, exist_ok=True)
    return work


def _forced_result(
    scenario: str, root: Path, adapter: StoreAdapter, stragglers: list[str]
) -> dict[str, Any]:
    case = adapter.name
    work = harness_work_dir(root)
    claimed = set(_read_lines(work / "forced.id"))
    present, torn = surviving_ids(adapter.log_path(root), adapter.id_field)
    lost = sorted(claimed - present)
    return {
        "case": case,
        "scenario": scenario,
        "attempted": len(claimed),
        "surviving": len(claimed & present),
        "lost": len(lost),
        "lost_sample": lost,
        "torn_lines": torn,
        "reclaim_errors": _read_lines(work / "forced-reclaimer.err"),
        "append_errors": _read_lines(work / "forced-appender.err"),
        "stragglers": stragglers,
    }


def run_forced_lost_update(case: str, root: Path) -> dict[str, Any]:
    """One append forced into the reclaim rewrite's read->replace window. Deterministic."""
    adapter = ADAPTERS[case]()
    work = _prepared_work_dir(root)
    adapter.seed(root)

    ctx = _context()
    ready = ctx.Event()
    appended = ctx.Event()
    handoff = 2.0
    reclaimer_spec = {
        "case": case,
        "root": str(root),
        "handoff_seconds": handoff,
        "errors": str(work / "forced-reclaimer.err"),
    }
    appender_spec = {
        "case": case,
        "root": str(root),
        "record_id": f"{SURVIVOR_PREFIX}forced",
        "handoff_seconds": handoff,
        "receipt": str(work / "forced.id"),
        "errors": str(work / "forced-appender.err"),
    }
    reclaimer = ctx.Process(
        target=_forced_reclaimer_main, args=(reclaimer_spec, ready, appended), name="reclaimer"
    )
    appender = ctx.Process(
        target=_forced_appender_main, args=(appender_spec, ready, appended), name="appender"
    )
    reclaimer.start()
    appender.start()
    stragglers = _join([reclaimer, appender], 60.0)
    return _forced_result("forced_lost_update", root, adapter, stragglers)
