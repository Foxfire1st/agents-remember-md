"""Forcing tests for the cross-store lock order.

The 2026-08-05 production incident: the liveness sweep held the TerminalCatalog batch lock
(RLock + flock, held for the whole sweep) across the HostedInteractionSynchronizer's
operator-inbox/gate lock acquisitions, while the agent-notifier sweep held the operator-inbox lock
across a catalog read — opposite nestings, one ABBA cycle. The uvicorn event loop then queued
on the same catalog RLock through an ``async def`` route doing a synchronous catalog read, and
the serving daemon stopped accepting entirely (41-deep listen backlog, killed twice in one day).

These tests pin the repair:

1. the synchronizer's store I/O never executes while the calling thread holds the catalog batch;
2. the two real sweep paths — ``TerminalCatalogLivenessSweeper.refresh`` against
   ``run_agent_notifier_sweep`` — run concurrently against shared stores without wedging (this
   test deadlocks by timeout against the pre-fix tree, on daemon threads so the suite survives
   the proof);
3. control/active route resolution and the terminal-image handler run their blocking catalog
   and disk I/O in worker threads, never on the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.controlplane.agent_notifier_signals import AgentNotifierSignalCooldownStore
from agents_remember.controlplane.expectation_rows import ExpectationRowStore
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.errors import HarnessControlError
from agents_remember.kernel import file_lock
from agents_remember.models.conversations.control_wire import (
    AdapterSnapshot,
    ControlIdentity,
)
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
)
from agents_remember.observer.store import EventStore
from agents_remember.serving import _app_terminal_routes as terminal_routes_module
from agents_remember.serving import app as app_module
from agents_remember.serving.agent_notifier import AgentNotifierContext, run_agent_notifier_sweep
from agents_remember.serving.agent_notifier_heartbeat import AgentNotifierHeartbeatStore
from agents_remember.serving.conversation.active.service import ActiveConversationService
from agents_remember.serving.conversation.control.service import ConversationControlService
from agents_remember.serving.conversation.runtime import ConversationRuntime
from agents_remember.serving.hosted_interactions import HostedInteractionSynchronizer
from agents_remember.serving.terminal import TerminalHost
from agents_remember.serving.terminal_catalog import (
    TerminalCatalog,
)
from agents_remember.serving.terminal_liveness import (
    LivenessProbe,
    SnapshotReader,
    TerminalCatalogLivenessConfig,
    TerminalCatalogLivenessSweeper,
    observe_terminal_liveness,
)
from agents_remember.serving.terminal_liveness import (
    TerminalLivenessHost as _LivenessHostProtocol,
)
from agents_remember.serving.terminal_tmux import TmuxProbeResult
from fastapi import Request, UploadFile

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
RENDEZVOUS_TIMEOUT_SECONDS = 10.0
DEADLOCK_DETECTION_SECONDS = 20.0

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

_LIVENESS_HYSTERESIS = TerminalCatalogLivenessConfig(
    failure_threshold=3,
    minimum_failure_window_seconds=5.0,
    pane_gone_failure_threshold=1,
    sweep_interval_seconds=0.0,
)


def _ready_entry(session_id: str) -> TerminalCatalogEntry:
    base = TerminalCatalogEntry(
        id=session_id,
        label=f"Chat {session_id}",
        kind="harness",
        harness="codex",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("codex",),
        created_at="2026-08-05T00:00:00+00:00",
        last_attached_at="2026-08-05T00:00:00+00:00",
        status="running",
    )
    return replace(
        base,
        control_state="ready",
        control_activity="idle",
        control_acceptance="immediate",
        control_endpoint=Path(f"/tmp/{session_id}.sock"),
    )


def _ready_snapshot(entry: TerminalCatalogEntry) -> AdapterSnapshot:
    return AdapterSnapshot(
        identity=ControlIdentity(entry.id, entry.tmux_name, entry.created_at),
        control="ready",
        activity="idle",
        acceptance="immediate",
        vendor_session_id="vendor-1",
        raw={},
    )


class _FakeHost:
    """One host seam for both sweeps: every session probes alive, nothing is owned."""

    def get(self, _sid: str) -> None:
        return None

    def has_session(self, _tmux_name: str) -> bool:
        return True

    def probe_session(self, _tmux_name: str) -> TmuxProbeResult:
        return TmuxProbeResult(exists=True, evidence="alive")

    def terminate(self, _sid: str, *, tmux_name: str | None = None) -> None:
        pass


class _SharedStores:
    """The daemon's real sharing shape: ONE catalog instance and ONE inbox log per process."""

    def __init__(self, root: Path) -> None:
        self.coordination_root = root / "ar-coordination"
        self.observer_root = self.coordination_root / "logs" / "observer"
        self.catalog = TerminalCatalog(root / "catalog.json")
        self.inbox_store = OperatorInboxStore(self.observer_root)
        self.synchronizer = HostedInteractionSynchronizer(self.observer_root)

    def agent_notifier_ctx(self) -> AgentNotifierContext:
        return AgentNotifierContext(
            catalog=self.catalog,
            host=cast(TerminalHost, _FakeHost()),
            paster=mock.Mock(),
            inbox_store=self.inbox_store,
            expectation_store=ExpectationRowStore(self.observer_root),
            signal_cooldown_store=AgentNotifierSignalCooldownStore(self.observer_root),
            event_store=EventStore(self.observer_root),
            heartbeat_store=AgentNotifierHeartbeatStore(self.observer_root),
            coordination_root=self.coordination_root,
            stale_seat_seconds=60.0,
        )

    def sweeper(
        self,
        observe_calls: list[str],
        *,
        snapshot_reader: SnapshotReader = _ready_snapshot,
    ) -> TerminalCatalogLivenessSweeper:
        real_observe = self.synchronizer.observe

        def counting_observe(entry: TerminalCatalogEntry, snapshot: AdapterSnapshot) -> None:
            observe_calls.append(entry.id)
            real_observe(entry, snapshot)

        return TerminalCatalogLivenessSweeper(
            self.catalog,
            cast(_LivenessHostProtocol, _FakeHost()),
            now=lambda: NOW,
            probe=LivenessProbe(
                hysteresis=_LIVENESS_HYSTERESIS,
                pane_capturer=lambda _tmux_name: "",
                snapshot_reader=snapshot_reader,
                on_control_snapshot=counting_observe,
            ),
        )


def _no_transcript(_entry: TerminalCatalogEntry) -> list[object]:
    return []


class CrossStoreLockOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.stores = _SharedStores(Path(self._dir.name))
        self.stores.catalog.upsert(_ready_entry("sess-1"))

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_interaction_sync_never_runs_under_the_catalog_batch_lock(self) -> None:
        """The synchronizer's store locks are never taken while this thread holds the batch.

        Pins the catalog→inbox edge removal: against the pre-fix tree the sweep runs the whole
        synchronizer inside ``catalog.batch()`` and every gate/inbox mutex acquisition below is
        recorded as a violation.
        """
        state = threading.local()
        violations: list[str] = []
        real_batch = TerminalCatalog.batch

        @contextlib.contextmanager
        def flagging_batch(self: TerminalCatalog) -> Iterator[None]:
            with real_batch(self):
                previous = getattr(state, "in_batch", False)
                state.in_batch = True
                try:
                    yield
                finally:
                    state.in_batch = previous

        real_mutex_for = file_lock.thread_mutex_for

        def checking_mutex_for(log_path: Path) -> object:
            if getattr(state, "in_batch", False):
                violations.append(str(log_path))
            return real_mutex_for(log_path)

        observe_calls: list[str] = []
        with (
            mock.patch.object(TerminalCatalog, "batch", flagging_batch),
            mock.patch.object(file_lock, "thread_mutex_for", checking_mutex_for),
            mock.patch(
                "agents_remember.serving.hosted_interactions.read_control_transcript",
                _no_transcript,
            ),
        ):
            self.stores.sweeper(observe_calls).refresh()

        # The sync actually ran — a vacuous pass (observer never invoked) is refused.
        self.assertEqual(observe_calls, ["sess-1"])
        self.assertEqual(violations, [])

    def test_starting_rows_sweep_defers_and_drains_like_the_full_sweep(self) -> None:
        """The starting fast path collects under the batch and drains after the commit.

        Same placement pin as the full sweep, driven through ``_refresh_starting_rows``: only
        starting rows are observed, the synchronizer's store I/O never lands under the catalog
        batch lock, and turn-state events still fire from the committed observations.
        """
        self.stores.catalog.upsert(replace(_ready_entry("sess-2"), control_state="starting"))
        state = threading.local()
        violations: list[str] = []
        real_batch = TerminalCatalog.batch

        @contextlib.contextmanager
        def flagging_batch(self: TerminalCatalog) -> Iterator[None]:
            with real_batch(self):
                previous = getattr(state, "in_batch", False)
                state.in_batch = True
                try:
                    yield
                finally:
                    state.in_batch = previous

        real_mutex_for = file_lock.thread_mutex_for

        def checking_mutex_for(log_path: Path) -> object:
            if getattr(state, "in_batch", False):
                violations.append(str(log_path))
            return real_mutex_for(log_path)

        observe_calls: list[str] = []
        events: list[object] = []
        sweeper = self.stores.sweeper(
            observe_calls,
            # A working snapshot projects a seat claim, so the committed observation flips the
            # fresh starting row's turn state and the event fires.
            snapshot_reader=lambda entry: replace(_ready_snapshot(entry), activity="working"),
        )
        sweeper._on_turn_state_change = events.append
        with (
            mock.patch.object(TerminalCatalog, "batch", flagging_batch),
            mock.patch.object(file_lock, "thread_mutex_for", checking_mutex_for),
            mock.patch(
                "agents_remember.serving.hosted_interactions.read_control_transcript",
                _no_transcript,
            ),
        ):
            sweeper._refresh_starting_rows(NOW)

        # Only the starting row is re-probed on the fast path, its sync really ran outside the
        # batch, and the committed observation still fired its turn-state event.
        self.assertEqual(observe_calls, ["sess-2"])
        self.assertEqual(violations, [])
        self.assertNotEqual(events, [])

    def test_starting_rows_sweep_early_returns(self) -> None:
        """The fast path's cheap exits: nothing starting, busy sweep lock, rate limit."""
        observe_calls: list[str] = []
        sweeper = self.stores.sweeper(observe_calls)

        # No starting rows (sess-1 is already ready) — the plain catalog list comes back.
        self.assertEqual(len(sweeper._refresh_starting_rows(NOW)), 1)
        self.assertEqual(observe_calls, [])

        self.stores.catalog.upsert(replace(_ready_entry("sess-2"), control_state="starting"))

        # A running sweep holds the non-overlapping lock — the fast path bows out untouched.
        sweeper._lock.acquire()
        try:
            self.assertEqual(len(sweeper._refresh_starting_rows(NOW)), 2)
        finally:
            sweeper._lock.release()
        self.assertEqual(observe_calls, [])

        # A real fast-path run, then an immediate second one is starting-rate-limited.
        with mock.patch(
            "agents_remember.serving.hosted_interactions.read_control_transcript",
            _no_transcript,
        ):
            sweeper._refresh_starting_rows(NOW)
            self.assertEqual(observe_calls, ["sess-2"])
            observe_calls.clear()
            sweeper._refresh_starting_rows(NOW)
            self.assertEqual(observe_calls, [])

    def test_direct_observe_runs_the_synchronizer_inline(self) -> None:
        """Direct callers outside a batch keep the legacy inline synchronizer call.

        ``probe.sync_collector`` is None here, so ``_observe_alive`` runs the synchronizer
        immediately — the WS-attach/paste path that never holds the catalog batch.
        """
        calls: list[str] = []

        class _AliveHost:
            def get(self, _sid: str) -> object:
                return SimpleNamespace(is_alive=True)

            def has_session(self, _tmux_name: str) -> bool:
                return True

        entry = _ready_entry("sess-3")
        self.stores.catalog.upsert(entry)
        probe = LivenessProbe(
            hysteresis=_LIVENESS_HYSTERESIS,
            pane_capturer=lambda _tmux_name: "",
            snapshot_reader=_ready_snapshot,
            on_control_snapshot=lambda observed, _snapshot: calls.append(observed.id),
        )
        with mock.patch(
            "agents_remember.serving.hosted_interactions.read_control_transcript",
            _no_transcript,
        ):
            observation = observe_terminal_liveness(
                self.stores.catalog,
                cast(_LivenessHostProtocol, _AliveHost()),
                entry,
                checked_at=NOW,
                probe=probe,
            )

        self.assertEqual(calls, ["sess-3"])
        self.assertTrue(observation.alive)

        # The sweeper's own per-entry observe without a collector keeps the same inline path
        # (probe unchanged), e.g. a caller that never opens a batch.
        observe_calls: list[str] = []
        sweeper = self.stores.sweeper(observe_calls)
        with mock.patch(
            "agents_remember.serving.hosted_interactions.read_control_transcript",
            _no_transcript,
        ):
            sweeper._observe_catalog_entry(entry, checked_at=NOW)
        self.assertEqual(observe_calls, ["sess-3"])

    def test_liveness_sweep_and_agent_notifier_sweep_do_not_abba_deadlock(self) -> None:
        """The two real sweep paths run concurrently against shared stores and both finish.

        Rendezvous wrappers park the liveness sweep INSIDE its catalog batch and the agent-notifier
        INSIDE its inbox transaction, then release both at once — the exact overlap of the
        2026-08-05 incident. Against the pre-fix tree each side then blocks on the other's lock
        and the joins below time out. Daemon threads keep that proof from hanging the suite.
        """
        arrived = {"batch": threading.Event(), "inbox": threading.Event()}

        def rendezvous(which: str) -> None:
            arrived[which].set()
            other = "inbox" if which == "batch" else "batch"
            arrived[other].wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS)

        real_batch = TerminalCatalog.batch

        @contextlib.contextmanager
        def parked_batch(self: TerminalCatalog) -> Iterator[None]:
            with real_batch(self):
                rendezvous("batch")
                yield

        real_access = OperatorInboxStore._exclusive_access

        @contextlib.contextmanager
        def parked_access(self: OperatorInboxStore) -> Iterator[None]:
            with real_access(self):
                rendezvous("inbox")
                yield

        observe_calls: list[str] = []
        results: dict[str, object] = {}
        failures: dict[str, BaseException] = {}

        def run_sweeper() -> None:
            try:
                results["sweeper"] = self.stores.sweeper(observe_calls).refresh()
            except BaseException as exc:  # surfaced as a test failure below
                failures["sweeper"] = exc

        def run_agent_notifier() -> None:
            try:
                results["agent-notifier"] = run_agent_notifier_sweep(
                    self.stores.agent_notifier_ctx(), now=NOW
                )
            except BaseException as exc:  # surfaced as a test failure below
                failures["agent-notifier"] = exc

        with (
            mock.patch.object(TerminalCatalog, "batch", parked_batch),
            mock.patch.object(OperatorInboxStore, "_exclusive_access", parked_access),
            mock.patch(
                "agents_remember.serving.hosted_interactions.read_control_transcript",
                _no_transcript,
            ),
        ):
            # The agent-notifier goes first: its hoisted catalog read must land before the sweeper
            # parks inside the batch, exactly as the post-fix sweep orders it.
            agent_notifier_thread = threading.Thread(
                target=run_agent_notifier, name="agent-notifier-sweep", daemon=True
            )
            agent_notifier_thread.start()
            self.assertTrue(
                arrived["inbox"].wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
                "agent-notifier never reached the inbox transaction",
            )
            sweeper_thread = threading.Thread(
                target=run_sweeper, name="liveness-sweep", daemon=True
            )
            sweeper_thread.start()
            sweeper_thread.join(timeout=DEADLOCK_DETECTION_SECONDS)
            agent_notifier_thread.join(timeout=DEADLOCK_DETECTION_SECONDS)

        self.assertFalse(failures, failures)
        self.assertNotEqual(observe_calls, [])  # the sync actually ran — no vacuous pass
        self.assertIn(
            "sweeper",
            results,
            "liveness sweep wedged against the agent-notifier's inbox lock — the ABBA is live",
        )
        self.assertIn(
            "agent-notifier",
            results,
            "agent-notifier sweep wedged against the catalog batch lock — the ABBA is live",
        )

    def test_control_resolve_entry_runs_off_the_event_loop(self) -> None:
        """The control choke point resolves the catalog row in a worker thread."""
        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []

        def spy_resolve(_runtime: object, _ar_session_id: str) -> TerminalCatalogEntry:
            seen.append(threading.current_thread())
            raise HarnessControlError("stopped at resolution — the thread is the evidence")

        runtime = SimpleNamespace(catalog=mock.Mock(), host=mock.Mock())
        service = ConversationControlService(cast(ConversationRuntime, runtime))
        with (
            mock.patch(
                "agents_remember.serving.conversation.control.service.resolve_running_entry",
                spy_resolve,
            ),
            self.assertRaises(HarnessControlError),
        ):
            asyncio.run(service.resolve_entry("sess-1"))

        self.assertEqual(len(seen), 1)
        self.assertIsNot(seen[0], loop_thread)

    def test_active_projector_resolution_runs_off_the_event_loop(self) -> None:
        """The active-side resolution is offloaded the same way."""
        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []

        def spy_resolve(_runtime: object, _ar_session_id: str) -> TerminalCatalogEntry:
            seen.append(threading.current_thread())
            raise HarnessControlError("stopped at resolution — the thread is the evidence")

        runtime = SimpleNamespace(catalog=mock.Mock(), host=mock.Mock())
        service = ActiveConversationService(cast(ConversationRuntime, runtime))
        with (
            mock.patch(
                "agents_remember.serving.conversation.active.service.resolve_running_entry",
                spy_resolve,
            ),
            self.assertRaises(HarnessControlError),
        ):
            asyncio.run(
                service._projector_for(mock.Mock(), "sess-1", expected_bridge_epoch="epoch-1")
            )

        self.assertEqual(len(seen), 1)
        self.assertIsNot(seen[0], loop_thread)

    def test_terminal_image_response_offloads_catalog_read_and_write(self) -> None:
        """The image handler's catalog read and disk write both run in worker threads."""
        loop_thread = threading.current_thread()
        seen: dict[str, threading.Thread] = {}
        tmp = Path(self._dir.name)

        def catalog_get(_session_id: str) -> None:
            seen["catalog"] = threading.current_thread()

        real_write = terminal_routes_module._write_paste_image

        def spy_write(dest: Path, body: bytes) -> None:
            seen["write"] = threading.current_thread()
            real_write(dest, body)

        runtime = SimpleNamespace(
            host=SimpleNamespace(get=lambda _sid: SimpleNamespace(cwd=tmp)),
            catalog=SimpleNamespace(get=catalog_get),
        )
        request = SimpleNamespace(headers={})

        upload = UploadFile(file=io.BytesIO(_PNG_BYTES), filename="shot.png")
        with mock.patch.object(terminal_routes_module, "_write_paste_image", spy_write):
            response = asyncio.run(
                terminal_routes_module._terminal_image_response(
                    cast(app_module._ServingRuntime, runtime),
                    "sess-1",
                    cast(Request, request),
                    upload,
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen.keys(), {"catalog", "write"})  # both paths actually exercised
        self.assertIsNot(seen["catalog"], loop_thread)
        self.assertIsNot(seen["write"], loop_thread)


if __name__ == "__main__":
    unittest.main()
