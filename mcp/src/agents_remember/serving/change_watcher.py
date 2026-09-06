"""Change-driven projection pacing: the debounced input watcher + the wake scheduler.

260712-PTS-L3: the projector used to re-project the whole world unconditionally every
``--interval`` seconds (production 1.0s) even when nothing changed -- ~160% steady CPU on
a quiet-but-large tree. This module makes waking *change-driven*: a filesystem watcher
(``watchfiles``, inotify-backed) over the projection's actual input surfaces feeds a
small debounce/coalesce state machine (:class:`ChangePacer`), and a slow heartbeat
covers everything a watcher cannot see. Each accepted path now carries its reader domain
through the pacer so unrelated inputs stay retained instead of rebuilding the whole world.

The authoritative input list (R1), derived reader-by-reader from
``observer.projection_store.project_and_write``:

========================================  =====================================================
watched root                              readers it feeds
========================================  =====================================================
``<coord>/tasks``                         read_enclosures, read_task_documents,
                                          read_series_documents, read_engine_process_facts
                                          (leaf enclosure contracts)
``<obs>/lifecycles``                      read_lifecycle_logs (``events.jsonl`` +
                                          ``heartbeat.json`` sidecars), per-lifecycle
                                          ``gates.jsonl`` (read_gates)
``<obs>/workspace``                       workspace ``gates.jsonl``, attention dismissals,
                                          operator inbox (read_agent_pickups), expectation rows
``<obs>/drift``                           read_drift_snapshots
``<coord>/logs/providers/status``         workspace provider ``current.json`` (read_providers)
``<coord>/logs/providers/setup``          read_setup_summaries
``<coord>/temp/worktree-start``           read_start_progress_entries
``<coord>/temp/tool-reports``             read_tool_reports
========================================  =====================================================

(``<obs>`` = ``<coord>/logs/observer``.) Deliberately NOT watched -- heartbeat-covered
blind spots:

* the per-repo memory roots (sidecar staleness / route coverage / ledgers): they sit
  behind ``REPO_SURFACE_REFRESH_TTL_SECONDS`` (15s) already, and their onboarding trees
  are large, so their freshness bound is TTL + heartbeat;
* landing state: an in-memory 30s git-derived refresher with no file signal;
* the entire ``worktrees/`` tree: the checkouts are ~50k dirs (recursive watching would
  re-create the scan cost this change removes) and each task's ``provider-runtime`` holds
  live container data (Postgres/grepai) that is unreadable to the daemon user and churns on
  every container write -- watching it crashed the watcher on permission-denied and would
  have re-projected on WAL writes. Worktree ``provider-state.json`` / ``setup-progress.json``
  changes are infrequent and heartbeat-covered; central provider status is watched via
  ``logs/providers``.

Self-trigger safety: the projection's own per-tick outputs (``latest-state.json`` /
``latest-metrics.json`` and their ``*.tmp`` siblings) live at the observer root, *outside*
every watched subdirectory, so a tick cannot re-wake itself. Inside ``<obs>/workspace``
the non-input churn (the raw event river + cursor/lock, the agent-notifier heartbeat) is
name-filtered, and the control-plane lockfiles are suffix-filtered in every watched
directory (they are the busiest writes in the tree -- every durable-store append and every
rewrite opens one ``a+b`` -- and none is an input). TTL-gated writers that run inside a tick
(gate-log compaction, event
retention, provider current-state refresh) cost at most one debounced echo tick per TTL
window, whose diff emits nothing.

Freshness bounds (R5, unchanged SSE semantics): a write into a quiet world becomes an SSE
delta within ``debounce + projection time``; under sustained writes projections are floored
to one per ``--interval`` and bounded by ``max_delay = interval`` (R2 -- the busy world keeps
the former 1s cadence); with no writes at all, ``/api/state`` staleness -- and every
time-*derived* field or state flip (``ageSeconds``/``staleSeconds``, stale/overdue decays) --
is bounded by the heartbeat (R3/R4; default :data:`DEFAULT_HEARTBEAT_SECONDS`). The
volatile age fields were already stripped from the delta stream and advanced client-side
(``dashboard/src/data/servedAges.ts``), so heartbeat-cadence refresh does not change what
an SSE client displays between emissions.

Failure posture (R7): ``watchfiles`` missing, zero watchable roots, or a crashed watch
degrades LOUDLY (error log) to fixed ``--interval`` ticking -- exactly today's behaviour --
and keeps retrying; fail-open, never fail-silent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agents_remember.kernel.file_lock import lock_path_for
from agents_remember.observer.store import WORKSPACE_CURSOR_FILE, WORKSPACE_LOCK_FILE
from agents_remember.serving.agent_notifier_heartbeat import agent_notifier_heartbeat_path
from agents_remember.serving.projections.paths import drift_snapshot_dir, observer_logs_root
from agents_remember.serving.projections.projection_inputs import (
    ALL_PROJECTION_DOMAINS,
    ProjectionDomain,
)
from agents_remember.serving.projections.projection_store import LATEST_METRICS, LATEST_STATE

if TYPE_CHECKING:
    from collections.abc import Callable

    from watchfiles import Change

    from agents_remember.kernel.primitives.runtime_config import (
        McpRuntimeConfig,
    )

try:  # R7: a missing wheel must degrade the daemon loudly, never crash it at import time.
    import watchfiles
except ImportError:  # pragma: no cover - exercised via the injected-None tests
    watchfiles = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Idle re-projection cadence (R3): the self-heal bound for watcher blind spots and the
#: resolution of time-derived projection fields (R4). Configurable via ``--heartbeat``.
DEFAULT_HEARTBEAT_SECONDS = 15.0

#: Settle window after the last observed change before projecting (R2): long enough to
#: coalesce a multi-file write burst, short enough to feel immediate on the dashboard.
DEBOUNCE_SECONDS = 0.1

#: How the raw watchfiles batches are grouped before they reach the pacer. The library
#: default (1600ms) could delay first detection beyond the 1s max-delay bound under
#: sustained writes, so batching is kept well under ``--interval``.
_AWATCH_DEBOUNCE_MS = 200
_AWATCH_STEP_MS = 50

#: Cadence for re-deriving the watch-root set (new worktree groups / newly created input
#: dirs) and for retrying after a watch failure. Gaps in between are heartbeat-covered.
WATCH_REFRESH_SECONDS = 30.0

#: Basenames excluded everywhere (defensive: the projection's own atomic outputs).
_EXCLUDED_NAMES = frozenset({LATEST_STATE, LATEST_METRICS})

#: ``<obs>/workspace`` files that are NOT projection inputs: the raw event river (served
#: by ``/api/events``, never read by ``project_and_write``) with its cursor + lock, and the
#: agent-notifier's own heartbeat (written every sweep on its own cadence). The control-plane
#: lockfiles used to be listed here by name; they are derived instead -- see
#: :data:`_DURABLE_LOG_LOCK_SUFFIX`.
_EXCLUDED_WORKSPACE_NAMES = frozenset(
    {
        "events.jsonl",
        WORKSPACE_CURSOR_FILE,
        WORKSPACE_LOCK_FILE,
        agent_notifier_heartbeat_path(Path(".")).name,
    }
)

#: The suffix ``kernel.file_lock.lock_path_for`` gives a ``.jsonl`` control-plane log,
#: DERIVED from that function rather than spelled out, because spelling it out is what broke:
#: this filter carried the literal ``"operator-inbox.lock"`` while ``lock_path_for`` had moved to
#: ``operator-inbox.jsonl.lock``, so the exclusion silently stopped matching anything.
#:
#: Every append and every rewrite of the six durable logs opens its lockfile ``a+b`` (including
#: each projection tick's own ``read_agent_pickups``), so these are the highest-frequency writes
#: in the watched tree and none of them is a projection input. Matched by SUFFIX and in EVERY
#: watched directory rather than by name under ``workspace/``, which is what the old list could
#: not express: five of the six logs live only under ``workspace/``, but ``gates.jsonl`` lives
#: there AND once per lifecycle under ``<obs>/lifecycles/<id>/``, so ``gates.jsonl.lock`` appears
#: in every lifecycle directory too. A suffix rule needs no list to keep in step with the stores,
#: and no projection input is named ``*.jsonl.lock``: the inputs are the ``.jsonl`` logs
#: themselves and ``.json`` sidecars.
_DURABLE_LOG_LOCK_SUFFIX = lock_path_for(Path("log.jsonl")).name.removeprefix("log")


def projection_input_roots(config: McpRuntimeConfig) -> list[Path]:
    """The currently-existing watch roots, derived from what ``project_and_write`` reads.

    Only existing directories are returned (``watchfiles`` refuses missing paths); dirs
    that appear later are picked up by the periodic watch-set refresh. See the module
    docstring for the reader-by-reader derivation and the deliberate omissions.
    """
    coordination_root = config.coordination_root
    observer = observer_logs_root(coordination_root)
    candidates: list[Path] = [
        coordination_root / "tasks",
        observer / "lifecycles",
        observer / "workspace",
        drift_snapshot_dir(coordination_root),
        coordination_root / "logs" / "providers" / "status",
        coordination_root / "logs" / "providers" / "setup",
        coordination_root / "temp" / "worktree-start",
        coordination_root / "temp" / "tool-reports",
    ]
    # Deliberately NOT under coordination_root/worktrees: those trees carry each task's
    # live provider-runtime (Postgres data, grepai indexes) which the daemon user cannot
    # read and which churn on container writes -- watching them recursively both crashed
    # the watcher (permission-denied on the container data dir) and would have re-projected
    # on every WAL write. Worktree provider-state.json changes are infrequent and are
    # heartbeat-covered; central provider status is already watched via logs/providers.
    return [path for path in candidates if path.is_dir()]


def is_projection_input_event(path: str) -> bool:
    """Filter one raw watch event down to genuine projection inputs.

    Drops atomic-write temp files, dot-prefixed sidecar temps, the projection's own outputs,
    every control-plane lockfile (:data:`_DURABLE_LOG_LOCK_SUFFIX`, in any watched directory),
    and the ``workspace/`` non-input churn (see :data:`_EXCLUDED_WORKSPACE_NAMES`).
    """
    name = os.path.basename(path.rstrip("/\\"))
    if name.endswith(".tmp") or name.startswith("."):
        return False
    if name.endswith(_DURABLE_LOG_LOCK_SUFFIX):
        return False
    if name in _EXCLUDED_NAMES:
        return False
    if name in _EXCLUDED_WORKSPACE_NAMES:
        parent = os.path.basename(os.path.dirname(path))
        if parent == "workspace":
            return False
    return True


def projection_domains_for_paths(
    config: McpRuntimeConfig, paths: set[str]
) -> frozenset[ProjectionDomain]:
    """Map accepted watcher paths to the reader domains they invalidate.

    Watch roots are intentionally explicit. If an accepted path does not match one of
    them, return every domain: watcher reconciliation must fail open to a full refresh,
    never silently retain an input whose source mapping was missed.
    """
    coordination_root = config.coordination_root.resolve()
    observer = observer_logs_root(coordination_root).resolve()
    roots = (
        (coordination_root / "tasks", ProjectionDomain.TASKS),
        (observer / "lifecycles", ProjectionDomain.LIFECYCLES),
        (observer / "workspace", ProjectionDomain.WORKSPACE),
        (drift_snapshot_dir(coordination_root), ProjectionDomain.DRIFT),
        (
            coordination_root / "logs" / "providers" / "status",
            ProjectionDomain.PROVIDERS,
        ),
        (
            coordination_root / "logs" / "providers" / "setup",
            ProjectionDomain.PROVIDERS,
        ),
        (
            coordination_root / "temp" / "worktree-start",
            ProjectionDomain.START_PROGRESS,
        ),
        (
            coordination_root / "temp" / "tool-reports",
            ProjectionDomain.TOOL_REPORTS,
        ),
    )
    domains: set[ProjectionDomain] = set()
    for raw_path in paths:
        path = Path(raw_path).resolve()
        matched = False
        for root, domain in roots:
            if path == root or root in path.parents:
                domains.add(domain)
                matched = True
                break
        if not matched:
            return ALL_PROJECTION_DOMAINS
    return frozenset(domains)


@dataclass(frozen=True)
class ProjectionWake:
    """One due projection and the reader domains accumulated for it."""

    reason: str
    domains: frozenset[ProjectionDomain]


class WakeTarget(Protocol):
    """What a watcher drives: the pacer's change/health inputs (structural, test-fakeable)."""

    def notify_change(
        self, domains: frozenset[ProjectionDomain] = ALL_PROJECTION_DOMAINS
    ) -> None: ...

    def set_watcher_healthy(self, healthy: bool) -> None: ...


class ChangeWatch(Protocol):
    """Structural contract for projection change watchers (the projector's seam).

    Mirrors ``LandingStateRefresh``: the projector owns the task lifecycle, tests inject
    fakes, and :class:`ProjectionInputWatcher` is the live implementation.
    """

    async def run(self, pacer: WakeTarget) -> None: ...


class ChangePacer:
    """The wake scheduler: change-or-heartbeat waiting with debounce, max-delay and floor.

    One instance belongs to one projector run loop. The watcher task feeds it
    (:meth:`notify_change` / :meth:`set_watcher_healthy`); the run loop awaits
    :meth:`wait` once per tick. Scheduling rules (all monotonic-clock):

    * **floor** -- never two projections closer than ``interval`` apart (``--interval``
      keeps its meaning as the fast-path cadence floor, so ``interval=100`` test
      projectors stay quiet exactly as before);
    * **debounce** -- a change projects ``debounce`` after the *last* change of its burst;
    * **max delay** -- a sustained burst still projects within ``max_delay`` (= ``interval``)
      of its *first* change (R2: the busy world keeps the former cadence);
    * **heartbeat** -- with no changes, project every ``heartbeat`` seconds (R3/R4);
    * **degraded** -- while the watcher is unhealthy, tick at the fixed ``interval``
      exactly like the pre-adaptive loop (R7 fail-open).
    """

    def __init__(
        self,
        *,
        interval: float,
        heartbeat: float = DEFAULT_HEARTBEAT_SECONDS,
        debounce: float = DEBOUNCE_SECONDS,
        max_delay: float | None = None,
    ) -> None:
        self._interval = max(interval, 0.0)
        self._heartbeat = max(heartbeat, self._interval)
        self._debounce = min(debounce, self._interval) if self._interval > 0 else debounce
        self._max_delay = max_delay if max_delay is not None else self._interval
        self._event = asyncio.Event()
        self._first_pending: float | None = None
        self._last_pending: float | None = None
        self._pending_domains: set[ProjectionDomain] = set()
        # Start degraded: until the watcher reports its watches established, pace at the
        # fixed interval so there is no detection blind spot at boot (heartbeat would be
        # too slow a floor for changes racing the watcher startup).
        self._watcher_healthy = False
        self._last_wake = time.monotonic()

    def notify_change(self, domains: frozenset[ProjectionDomain] = ALL_PROJECTION_DOMAINS) -> None:
        """Record a (batch of) input change(s); wakes :meth:`wait` to reschedule."""
        now = time.monotonic()
        if self._first_pending is None:
            self._first_pending = now
        self._last_pending = now
        self._pending_domains.update(domains)
        self._event.set()

    def set_watcher_healthy(self, healthy: bool) -> None:
        """Flip between change-driven and fixed-interval (degraded) pacing."""
        self._watcher_healthy = healthy
        self._event.set()

    def _next_deadline(self) -> tuple[float, str]:
        """Pure scheduling core: (monotonic deadline, wake reason) for the current state."""
        floor = self._last_wake + self._interval
        if not self._watcher_healthy:
            return floor, "interval"
        if self._first_pending is not None and self._last_pending is not None:
            settle = self._last_pending + self._debounce
            bound = self._first_pending + self._max_delay
            return min(max(settle, floor), max(bound, floor)), "change"
        return self._last_wake + self._heartbeat, "heartbeat"

    async def wait(self) -> ProjectionWake:
        """Sleep until the next projection is due; return its reason and domains.

        Pending changes are consumed at wake: changes observed *during* the following
        projection accumulate for the next cycle, so nothing is ever lost to a tick.
        """
        while True:
            now = time.monotonic()
            deadline, reason = self._next_deadline()
            if now >= deadline:
                domains = (
                    frozenset(self._pending_domains)
                    if reason == "change"
                    else ALL_PROJECTION_DOMAINS
                    if reason == "interval"
                    else frozenset()
                )
                self._first_pending = None
                self._last_pending = None
                self._pending_domains.clear()
                self._event.clear()
                self._last_wake = now
                return ProjectionWake(reason=reason, domains=domains)
            self._event.clear()
            try:
                async with asyncio.timeout(deadline - now):
                    await self._event.wait()
            except TimeoutError:
                pass


class ProjectionInputWatcher:
    """Watch the projection-input roots and drive a :class:`ChangePacer`.

    Lifecycle mirrors the landing refresher: created by ``create_app`` for live serving
    (never for ``--sim`` -- replay must stay time-driven, its feeder only writes *inside*
    a tick), started/cancelled by ``Projector.run``. One watch failure never kills the
    projector: the pacer drops to fixed-interval ticking (loudly) and the watch is
    retried every :data:`WATCH_REFRESH_SECONDS`.
    """

    def __init__(
        self,
        config: McpRuntimeConfig,
        *,
        refresh_seconds: float = WATCH_REFRESH_SECONDS,
    ) -> None:
        self._config = config
        self._refresh_seconds = refresh_seconds

    async def run(self, pacer: WakeTarget) -> None:
        """Watch forever, feeding ``pacer``; degrade loudly on any failure (R7)."""
        if watchfiles is None:
            logger.error(
                "watchfiles is not installed: change-driven projection is DISABLED and the "
                "projector falls back to fixed-interval ticking (install the 'watchfiles' "
                "dependency to restore adaptive pacing)"
            )
            pacer.set_watcher_healthy(False)
            return
        established_before = False
        while True:
            try:
                # Root derivation sits INSIDE the retry guard: a transient stat/glob failure
                # must follow the same loud degrade-and-retry path as a watch failure -- not
                # escape run() and kill the task for good (losing the periodic self-heal).
                roots = await asyncio.to_thread(projection_input_roots, self._config)
                if not roots:
                    # A fresh/empty coordination tree: nothing to watch yet. Not an error --
                    # fixed-interval pacing until the first input dir appears.
                    pacer.set_watcher_healthy(False)
                    await asyncio.sleep(self._refresh_seconds)
                    continue
                # Returns when the root set changed (restart with the fresh set).
                await self._watch_once(roots, pacer, reconcile=established_before)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "projection input watcher FAILED; falling back to fixed-interval "
                    "ticking until the watch re-establishes (retry in %.0fs)",
                    self._refresh_seconds,
                )
                pacer.set_watcher_healthy(False)
                await asyncio.sleep(self._refresh_seconds)
            established_before = True

    async def _watch_once(self, roots: list[Path], pacer: WakeTarget, *, reconcile: bool) -> None:
        """One watch generation over a fixed root set; returns when the set moved."""
        assert watchfiles is not None
        stop = asyncio.Event()
        refresher = asyncio.create_task(self._stop_when_roots_change(roots, stop))
        try:
            pacer.set_watcher_healthy(True)
            if reconcile:
                # inotify has no replay: reconcile whatever happened while the watch
                # was down or being re-registered with one debounced projection.
                pacer.notify_change(ALL_PROJECTION_DOMAINS)
            async for changes in watchfiles.awatch(
                *roots,
                watch_filter=self._watch_filter(),
                debounce=_AWATCH_DEBOUNCE_MS,
                step=_AWATCH_STEP_MS,
                stop_event=stop,
                recursive=True,
                # Defence-in-depth: the watched roots are deliberately container-free, but an
                # unreadable subdir must degrade to skipping it, never crash the whole watch.
                ignore_permission_denied=True,
            ):
                if changes:
                    domains = projection_domains_for_paths(
                        self._config, {path for _change, path in changes}
                    )
                    pacer.notify_change(domains)
        finally:
            refresher.cancel()
            try:
                await refresher
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("watch-root refresher failed during watcher shutdown")

    def _watch_filter(self) -> Callable[[Change, str], bool]:
        assert watchfiles is not None
        default = watchfiles.DefaultFilter()

        def _filter(change: Change, path: str) -> bool:
            return default(change, path) and is_projection_input_event(path)

        return _filter

    async def _stop_when_roots_change(self, current: list[Path], stop: asyncio.Event) -> None:
        """Re-derive the root set periodically; stop the watch (to restart) when it moved."""
        while True:
            await asyncio.sleep(self._refresh_seconds)
            fresh = await asyncio.to_thread(projection_input_roots, self._config)
            if fresh != current:
                stop.set()
                return
