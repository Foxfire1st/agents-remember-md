"""260713-TES-L4 regression matrix: inbox arrival, acknowledgement, scoped routing.

The R7 matrix: two simultaneous repository architects; replacement mid-flight; explicit
supersession; accepted-at-boundary rows with no retry/nudge/escalation; relay restart
reconciliation; TTL/cap eviction under the hard bounded-store rule; busy-adapter queued
acceptance; plus the new machinery (rebind grace, terminal inspectability, settings
last-good resilience, relay-death surfacing, retire stranded-row surfacing).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

import pytest
from agents_remember.application import operator_inbox_tools as inbox_application
from agents_remember.application.terminal_tools import session_retire_tool
from agents_remember.controlplane import operator_inbox_transitions as inbox_transitions
from agents_remember.controlplane.agent_notifier_signals import AgentNotifierSignalCooldownStore
from agents_remember.controlplane.expectation_rows import ExpectationRowStore
from agents_remember.controlplane.interaction_retention import (
    INBOX_MAX_CURRENT_ROWS,
)
from agents_remember.controlplane.operator_inbox_records import (
    InboxAddress,
    InboxMessage,
    InboxPoster,
    InboxRouting,
    create_operator_inbox_entry,
)
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.controlplane.signal_routing import (
    RoutedOwner,
    StructuralRoutingError,
    derive_architect_owner,
)
from agents_remember.kernel.agentic_settings import load_agentic_settings
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.mcp.tools.operator_inbox import operator_inbox_supersede_payload
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.observer.store import EventStore
from agents_remember.serving import _app_lifespan
from agents_remember.serving import relay_death_watch as relay_module
from agents_remember.serving.agent_notifier import run_agent_notifier_sweep
from agents_remember.serving.agent_notifier_heartbeat import AgentNotifierHeartbeatStore
from agents_remember.serving.agent_notifier_models import (
    AgentNotifierContext,
)
from agents_remember.serving.dispatch_brief import HostedDelivery
from agents_remember.serving.operator_inbox_posts import _is_owner_addressed
from agents_remember.serving.relay_death_watch import (
    DEFAULT_AGENT_NOTIFIER_STALE_CUTOFF_SECONDS,
    RelayDeathMarkerStore,
    _stale_cutoff_seconds,
    _try_deliver,
    post_relay_death_signal,
)
from agents_remember.serving.terminal import TerminalHost
from agents_remember.serving.terminal_catalog import TerminalCatalog, TerminalCatalogEntry
from agents_remember.tasks.document_refs import TaskDocumentTopology
from test_agent_notifier_ladder import LEAF_1_REF, MASTER_REF, SPRINT_REF, _write_topology
from test_compound_idle_relay import OTHER_MASTER_LEAF, _write_other_master

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
REPO_B_SPRINT = TaskDocumentRef(repository="repo-b", path="sprint/task.json")


def _seat(session_id: str, **overrides: object) -> TerminalCatalogEntry:
    base: dict[str, object] = dict(
        id=session_id,
        label=session_id,
        kind="harness",
        harness="codex",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("codex",),
        created_at=NOW.isoformat(),
        last_attached_at=NOW.isoformat(),
        status="running",
    )
    base.update(overrides)
    return TerminalCatalogEntry(**base)  # type: ignore[arg-type]


def _config(root: Path) -> McpRuntimeConfig:
    return McpRuntimeConfig(
        config_path=root / "settings.json",
        coordination_root=root,
        workspace_root=root,
        transcript_root=root / "logs" / "mcp",
    )


@pytest.mark.integration
class ScopedArchitectCustodyTests(unittest.TestCase):
    """R13: repository+sprint-scoped architect custody; global first-match is a test failure."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        _write_topology(root)
        _write_other_master(root)
        self.topology = TaskDocumentTopology(root)
        self.catalog = TerminalCatalog(root / "catalog.json")

    def test_two_simultaneous_repository_architects_resolve_by_scope(self) -> None:
        self.catalog.upsert(
            _seat(
                "arch-a",
                task_document_ref=SPRINT_REF,
                seat_role="architect",
                spawn_role="architect",
            )
        )
        self.catalog.upsert(
            _seat(
                "arch-b",
                task_document_ref=REPO_B_SPRINT,
                seat_role="architect",
                spawn_role="architect",
            )
        )
        owner_a = derive_architect_owner(self.catalog, self.topology, task_document_ref=LEAF_1_REF)
        owner_b = derive_architect_owner(
            self.catalog, self.topology, task_document_ref=OTHER_MASTER_LEAF
        )
        self.assertEqual(owner_a.agent_id, "arch-a")
        self.assertEqual(owner_b.agent_id, "arch-b")

    def test_one_sprint_architect_owns_every_leaf_in_that_sprint(self) -> None:
        self.catalog.upsert(
            _seat(
                "arch-sprint",
                task_document_ref=SPRINT_REF,
                seat_role="architect",
                spawn_role="architect",
            )
        )
        owner = derive_architect_owner(self.catalog, self.topology, task_document_ref=LEAF_1_REF)
        self.assertEqual(owner.agent_id, "arch-sprint")

    def test_global_first_match_is_never_a_fallback(self) -> None:
        self.catalog.upsert(
            _seat(
                "arch-a",
                task_document_ref=SPRINT_REF,
                seat_role="architect",
                spawn_role="architect",
            )
        )
        self.catalog.upsert(
            _seat(
                "arch-b",
                task_document_ref=REPO_B_SPRINT,
                seat_role="architect",
                spawn_role="architect",
            )
        )
        self.catalog.upsert(
            _seat(
                "arch-a2",
                task_document_ref=SPRINT_REF,
                seat_role="architect",
                spawn_role="architect",
            )
        )
        with self.assertRaisesRegex(StructuralRoutingError, "multiple running occupants"):
            derive_architect_owner(self.catalog, self.topology, task_document_ref=LEAF_1_REF)


@pytest.mark.integration
class PostTimeOwnerRebindingTests(unittest.TestCase):
    """N14(a): every owner-addressed post re-derives the current qualified owner."""

    def test_message_to_retired_manager_reaches_the_replacement_at_post_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_topology(root)
            catalog = TerminalCatalog(root / "catalog.json")
            catalog.upsert(
                _seat(
                    "manager-old",
                    status="terminated",
                    terminated_at=(NOW - timedelta(minutes=10)).isoformat(),
                    task_document_ref=MASTER_REF,
                    spawn_role="manager",
                )
            )
            catalog.upsert(
                _seat(
                    "manager-new",
                    task_document_ref=MASTER_REF,
                    spawn_role="manager",
                )
            )
            catalog.upsert(
                _seat(
                    "worker-1",
                    task_document_ref=LEAF_1_REF,
                    spawn_role="worker",
                    spawned_by_session="manager-old",
                )
            )
            posted = inbox_application.operator_inbox_post_tool(
                _config(root),
                address=InboxAddress(
                    lifecycle_id=None, agent_id="manager-old", recipient_role="manager"
                ),
                message=InboxMessage(
                    ask="Please continue the leaf",
                    response="Context attached.",
                    message_kind="message",
                ),
                poster=InboxPoster(
                    created_by="worker-1",
                    created_via="cli",
                    sender_agent_id="worker-1",
                    sender_role="worker",
                ),
                delivery=HostedDelivery(enabled=False, catalog=catalog),
            )
            self.assertEqual(posted["agentId"], "manager-new")
            self.assertEqual(posted["ownerAgentId"], "manager-new")
            self.assertEqual(posted["recipientRole"], "manager")


@pytest.mark.integration
class PostTimeAddressBranchTests(unittest.TestCase):
    """N14(a) address-shape branches: role-only, unknown-seat, lifecycle, peer-preserve."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        _write_topology(root)
        self.catalog = TerminalCatalog(root / "catalog.json")
        self.catalog.upsert(
            _seat(
                "manager-new",
                task_document_ref=MASTER_REF,
                spawn_role="manager",
                lifecycle_id="L-manager",
            )
        )
        self.catalog.upsert(
            _seat(
                "manager-old",
                status="terminated",
                terminated_at=(NOW - timedelta(minutes=10)).isoformat(),
                task_document_ref=MASTER_REF,
                spawn_role="manager",
            )
        )
        self.catalog.upsert(
            _seat(
                "worker-1",
                task_document_ref=LEAF_1_REF,
                spawn_role="worker",
                spawned_by_session="manager-new",
            )
        )
        self.catalog.upsert(
            _seat(
                "worker-2",
                task_document_ref=TaskDocumentRef(
                    repository="repo-a", path="260707_master/leaf-2.json"
                ),
                spawn_role="worker",
                spawned_by_session="manager-new",
                lifecycle_id="L-worker",
            )
        )

    def _post(self, address: InboxAddress) -> dict:
        return inbox_application.operator_inbox_post_tool(
            _config(self.root),
            address=address,
            message=InboxMessage(ask="ask", response="resp", message_kind="message"),
            poster=InboxPoster(
                created_by="worker-1",
                created_via="cli",
                sender_agent_id="worker-1",
                sender_role="worker",
            ),
            delivery=HostedDelivery(enabled=False, catalog=self.catalog),
        )

    def test_role_only_owner_address_resolves_to_current_manager(self) -> None:
        posted = self._post(
            InboxAddress(lifecycle_id=None, agent_id=None, recipient_role="manager")
        )
        self.assertEqual(posted["agentId"], "manager-new")

    def test_unknown_seat_address_resolves_to_current_owner(self) -> None:
        posted = self._post(InboxAddress(lifecycle_id=None, agent_id="manager-retired"))
        self.assertEqual(posted["agentId"], "manager-new")

    def test_existing_owner_seat_address_resolves_without_a_role_hint(self) -> None:
        posted = self._post(
            InboxAddress(lifecycle_id=None, agent_id="manager-old", recipient_role=None)
        )
        self.assertEqual(posted["agentId"], "manager-new")

    def test_lifecycle_address_with_no_matching_seat_resolves(self) -> None:
        posted = self._post(InboxAddress(lifecycle_id="L-dead", agent_id=None))
        self.assertEqual(posted["agentId"], "manager-new")

    def test_lifecycle_address_with_owner_seat_resolves_to_current_owner(self) -> None:
        posted = self._post(InboxAddress(lifecycle_id="L-manager", agent_id=None))
        self.assertEqual(posted["agentId"], "manager-new")

    def test_peer_worker_address_is_preserved_verbatim(self) -> None:
        posted = self._post(
            InboxAddress(lifecycle_id=None, agent_id="worker-2", recipient_role="worker")
        )
        self.assertEqual(posted["agentId"], "worker-2")

    def test_lifecycle_address_with_non_owner_seat_is_preserved(self) -> None:
        posted = self._post(InboxAddress(lifecycle_id="L-worker", agent_id=None))
        self.assertEqual(posted["lifecycleId"], "L-worker")
        self.assertIsNone(posted["agentId"])

    def test_empty_address_helper_returns_false(self) -> None:
        self.assertFalse(
            _is_owner_addressed(
                self.catalog,
                RoutedOwner(role="manager", agent_id="manager-new"),
                InboxAddress(),
            )
        )


@pytest.mark.integration
class ExplicitSupersessionTests(unittest.TestCase):
    """R11/R2: superseded is always explicit and skipped by every retry/evaluation path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = OperatorInboxStore(self.root / "logs" / "observer")
        patcher = mock.patch.object(inbox_application, "_store", return_value=self.store)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _entry(self, entry_id: str = "e1") -> None:
        self.store.append(
            create_operator_inbox_entry(
                InboxMessage(ask="old command", response="supersede me", message_kind="message"),
                entry_id=entry_id,
                now=NOW.isoformat(),
                routing=InboxRouting(address=InboxAddress(lifecycle_id=None, agent_id="worker-1")),
                poster=InboxPoster(created_by="manager-1", created_via="cli"),
            )
        )

    def test_supersede_is_terminal_visible_and_skipped_by_retry(self) -> None:
        self._entry()
        superseded = operator_inbox_supersede_payload(
            None,  # type: ignore[arg-type]
            entry_id="e1",
            reason="overtaken by the newer command",
            superseded_by="developer",
        )
        self.assertTrue(superseded["supersededNow"])
        self.assertEqual(superseded["state"], "superseded")
        row = self.store.current()["e1"]
        self.assertEqual(row.terminalReason, "overtaken by the newer command")
        self.assertEqual(row.supersededBy, "developer")
        self.assertIsNotNone(row.terminalAt)
        self.assertEqual(
            self.store.list_redeliverable(now=NOW + timedelta(hours=1)),
            [],
        )
        # Terminal markers are inspectable via the extended poll surface.
        polled = inbox_application.operator_inbox_poll_tool(
            None,  # type: ignore[arg-type]
            lifecycle_id=None,
            agent_id="worker-1",
            include_terminal=True,
        )
        self.assertEqual(polled["entryCount"], 1)
        self.assertEqual(polled["entries"][0]["state"], "superseded")

    def test_poll_defaults_to_pending_only(self) -> None:
        self._entry("pending-1")
        self._entry("terminal-1")
        operator_inbox_supersede_payload(
            None,  # type: ignore[arg-type]
            entry_id="terminal-1",
            reason="done",
        )
        polled = inbox_application.operator_inbox_poll_tool(
            None,  # type: ignore[arg-type]
            lifecycle_id=None,
            agent_id="worker-1",
        )
        self.assertEqual(polled["entryCount"], 1)
        self.assertEqual(polled["entries"][0]["id"], "pending-1")


@pytest.mark.integration
class TtlAndCapEvictionTests(unittest.TestCase):
    """§9/D4: pending rows resolve terminal before retention; cap drops are counted/surfaced."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.observer_root = root / "logs" / "observer"
        self.catalog = TerminalCatalog(root / "catalog.json")
        self.inbox_store = OperatorInboxStore(self.observer_root)
        self.heartbeat_store = AgentNotifierHeartbeatStore(self.observer_root)
        self.event_store = EventStore(self.observer_root)

    def _ctx(self) -> AgentNotifierContext:
        return AgentNotifierContext(
            catalog=self.catalog,
            host=cast(
                TerminalHost,
                SimpleNamespace(has_session=lambda _n: True, terminate=lambda *_a, **_k: None),
            ),
            paster=cast("object", None),  # type: ignore[arg-type]
            inbox_store=self.inbox_store,
            expectation_store=ExpectationRowStore(self.observer_root),
            signal_cooldown_store=AgentNotifierSignalCooldownStore(self.observer_root),
            event_store=self.event_store,
            heartbeat_store=self.heartbeat_store,
            coordination_root=self.root,
            tmux_name_snapshotter=lambda: self.fail("no tmux probing in these tests"),
        )

    def _entry(
        self,
        entry_id: str,
        *,
        created_at: datetime = NOW,
        terminal: bool = False,
        terminal_at: datetime | None = None,
    ) -> None:
        entry = create_operator_inbox_entry(
            InboxMessage(ask=entry_id, response="body", message_kind="message"),
            entry_id=entry_id,
            now=created_at.isoformat(),
            routing=InboxRouting(
                address=InboxAddress(lifecycle_id=None, agent_id=f"seat-{entry_id}")
            ),
            poster=InboxPoster(created_by="system", created_via="cli"),
        )
        self.inbox_store.append(entry)
        if terminal:
            inbox_transitions.mark_landed(
                self.inbox_store,
                entry_id,
                now=(terminal_at or NOW).isoformat(),
                reason="adapter-accepted-at-turn-boundary",
            )

    def test_pending_row_past_retention_resolves_expired_before_compaction(self) -> None:
        self.catalog.upsert(_seat("live-seat"))
        self.inbox_store.append(
            create_operator_inbox_entry(
                InboxMessage(ask="ancient", response="body", message_kind="message"),
                entry_id="ancient",
                now=(NOW - timedelta(hours=49)).isoformat(),
                routing=InboxRouting(address=InboxAddress(lifecycle_id=None, agent_id="live-seat")),
                poster=InboxPoster(created_by="system", created_via="cli"),
            )
        )
        result = run_agent_notifier_sweep(self._ctx(), now=NOW)
        self.assertTrue(any(f.kind == "inbox-ttl-expired" for f in result.findings))
        row = self.inbox_store.current()["ancient"]
        self.assertEqual(row.state, "expired")
        self.assertEqual(row.terminalReason, "pending-ttl-expired")
        events = {
            event.kind
            for event in self.event_store.read(None)
            if event.kind == "orchestration.agent-notifier.inbox-expired"
        }
        self.assertTrue(events)

    def test_cap_drops_terminal_oldest_first_and_surfaces_the_count(self) -> None:
        for index in range(INBOX_MAX_CURRENT_ROWS):
            # All pending rows stay inside the 5-minute rebind grace so the sweep owns them
            # as pending (no expiry during this cap test).
            self._entry(
                f"live-{index:04d}",
                created_at=NOW - timedelta(seconds=index % 60),
            )
        for index in range(6):
            self._entry(
                f"terminal-{index:04d}",
                created_at=NOW - timedelta(hours=1),
                terminal=True,
                terminal_at=NOW - timedelta(minutes=10 + index),
            )
        result = run_agent_notifier_sweep(self._ctx(), now=NOW)
        current = self.inbox_store.current()
        self.assertEqual(len(current), INBOX_MAX_CURRENT_ROWS)
        # Terminal markers are the eviction class; the pending set survives intact.
        self.assertFalse(any(row.state != "pending" for row in current.values()))
        compact_events = [
            event
            for event in self.event_store.read(None)
            if event.kind == "orchestration.agent-notifier.inbox-compacted"
        ]
        self.assertEqual(compact_events[-1].data["removed"], 6)
        self.assertEqual(compact_events[-1].data["kept"], INBOX_MAX_CURRENT_ROWS)
        self.assertEqual(result.pending_inbox_count, INBOX_MAX_CURRENT_ROWS)


class SettingsResilienceTests(unittest.IsolatedAsyncioTestCase):
    """R7/N5: a failed settings read keeps last-good settings, fails loud, never kills the loop."""

    async def test_loop_continues_on_last_good_settings_after_a_failed_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = load_agentic_settings(root)
            runtime = SimpleNamespace(
                config=SimpleNamespace(coordination_root=root),
                catalog=TerminalCatalog(root / "catalog.json"),
                host=cast(
                    TerminalHost,
                    SimpleNamespace(has_session=lambda _n: True, terminate=lambda *_a, **_k: None),
                ),
                paster=cast("object", None),  # type: ignore[arg-type]
                heartbeat_store=AgentNotifierHeartbeatStore(root / "logs" / "observer"),
                observer_root=root / "logs" / "observer",
                liveness_clock=lambda: NOW,
                liveness_sweeper=SimpleNamespace(refresh=lambda: ()),
                register_inbox_execution_evidence=None,
            )

            async def fake_sleep(_seconds: float) -> None:
                fake_sleep.calls += 1
                if fake_sleep.calls >= 3:
                    raise asyncio.CancelledError

            fake_sleep.calls = 0
            sweeps: list[object] = []
            with (
                mock.patch.object(_app_lifespan, "load_agentic_settings") as load,
                mock.patch.object(_app_lifespan, "run_agent_notifier_sweep") as sweep,
                mock.patch.object(_app_lifespan.asyncio, "sleep", new=fake_sleep),
                self.assertLogs("agents_remember.serving.app", level="ERROR") as logs,
            ):
                load.side_effect = [RuntimeError("settings boom"), good, RuntimeError("again")]
                sweep.side_effect = lambda _ctx, now=None: sweeps.append(_ctx)
                with self.assertRaises(asyncio.CancelledError):
                    await _app_lifespan._agent_notifier_loop(runtime)  # type: ignore[arg-type]

            # First tick skipped (no last-good), then one sweep on good settings, then one
            # sweep on the last-good snapshot after the second failed read.
            self.assertEqual(len(sweeps), 2)
            self.assertIn("settings load failed", logs.output[0])
            self.assertIn("using last-good settings", logs.output[-1])


@pytest.mark.integration
class RelayDeathWatchTests(unittest.TestCase):
    """N5: the relay never relays its own death; a dashboard-side watcher posts instead."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.observer_root = root / "logs" / "observer"
        self.heartbeat_store = AgentNotifierHeartbeatStore(self.observer_root)
        self.catalog = TerminalCatalog(root / "catalog.json")
        self.runtime = SimpleNamespace(
            config=SimpleNamespace(coordination_root=root),
            catalog=self.catalog,
            host=cast(
                TerminalHost,
                SimpleNamespace(has_session=lambda _n: True, terminate=lambda *_a, **_k: None),
            ),
            paster=cast("object", None),  # type: ignore[arg-type]
            heartbeat_store=self.heartbeat_store,
            observer_root=self.observer_root,
        )

    def test_never_ticked_heartbeat_is_silent(self) -> None:
        self.assertFalse(post_relay_death_signal(self.runtime, now=NOW))  # type: ignore[arg-type]

    def test_stale_heartbeat_posts_once_per_tick_identity_then_rearms(self) -> None:
        self.heartbeat_store.tick(now=NOW - timedelta(minutes=10))
        self.assertTrue(post_relay_death_signal(self.runtime, now=NOW))  # type: ignore[arg-type]
        # Dedupe: the same stale tick never posts twice.
        self.assertFalse(post_relay_death_signal(self.runtime, now=NOW))  # type: ignore[arg-type]
        rows = OperatorInboxStore(self.observer_root).current().values()
        self.assertEqual(len(rows), 1)
        row = next(iter(rows))
        self.assertEqual(row.messageKind, "degradation-alert")
        self.assertEqual(row.recipientRole, "developer")
        # A fresh tick re-arms the watcher for the next death.
        self.heartbeat_store.tick(now=NOW + timedelta(minutes=1))
        self.assertFalse(
            post_relay_death_signal(self.runtime, now=NOW + timedelta(minutes=1))  # type: ignore[arg-type]
        )
        self.assertTrue(post_relay_death_signal(self.runtime, now=NOW + timedelta(minutes=11)))  # type: ignore[arg-type]
        self.assertEqual(len(OperatorInboxStore(self.observer_root).current()), 2)

    def test_invalid_marker_content_reads_as_none(self) -> None:
        marker_path = self.observer_root / "workspace" / "agent-notifier-death-watch.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text('{"lastTickAt": 123}', encoding="utf-8")
        self.assertIsNone(RelayDeathMarkerStore(self.observer_root).read())

    def test_settings_failure_falls_back_to_the_default_cutoff(self) -> None:
        with mock.patch.object(
            relay_module, "load_agentic_settings", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(
                _stale_cutoff_seconds(self.root),
                DEFAULT_AGENT_NOTIFIER_STALE_CUTOFF_SECONDS,
            )

    def test_delivery_failure_is_best_effort(self) -> None:
        self.heartbeat_store.tick(now=NOW - timedelta(minutes=10))
        post_relay_death_signal(self.runtime, now=NOW)  # type: ignore[arg-type]
        entry = next(iter(OperatorInboxStore(self.observer_root).current().values()))
        with mock.patch.object(
            relay_module, "deliver_inbox_entry", side_effect=RuntimeError("adapter down")
        ):
            _try_deliver(self.runtime, entry)  # type: ignore[arg-type]  # must not raise


class RelayDeathLoopTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard-side watcher loop survives a failed post (N5)."""

    async def test_loop_fails_loud_and_continues(self) -> None:
        runtime = SimpleNamespace(liveness_clock=lambda: NOW)

        async def fake_sleep(_seconds: float) -> None:
            fake_sleep.calls += 1
            if fake_sleep.calls >= 2:
                raise asyncio.CancelledError

        fake_sleep.calls = 0

        with (
            mock.patch.object(relay_module.asyncio, "sleep", new=fake_sleep),
            mock.patch.object(
                relay_module,
                "post_relay_death_signal",
                side_effect=RuntimeError("boom"),
            ),
            self.assertLogs("agents_remember.serving.app", level="ERROR") as logs,
            self.assertRaises(asyncio.CancelledError),
        ):
            await relay_module.relay_death_watch_loop(runtime)  # type: ignore[arg-type]
        self.assertIn("relay-death watch failed", logs.output[0])


@pytest.mark.integration
class RetireSurfacingTests(unittest.TestCase):
    """N2: session_retire surfaces stranded rows to the retiring authority and never refuses."""

    def test_retire_surfaces_stranded_rows_and_still_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            observer = root / "logs" / "observer"
            catalog = TerminalCatalog(root / "logs" / "dashboard" / "terminal-sessions.json")
            catalog.upsert(_seat("orchestrator-1", spawn_role="orchestrator"))
            catalog.upsert(
                _seat(
                    "worker-1",
                    task_document_ref=LEAF_1_REF,
                    spawn_role="worker",
                    spawned_by_session="orchestrator-1",
                )
            )
            store = OperatorInboxStore(observer)
            for index in range(2):
                store.append(
                    create_operator_inbox_entry(
                        InboxMessage(ask=f"row {index}", response="body", message_kind="message"),
                        entry_id=f"row-{index}",
                        now=NOW.isoformat(),
                        routing=InboxRouting(
                            address=InboxAddress(lifecycle_id=None, agent_id="worker-1")
                        ),
                        poster=InboxPoster(created_by="manager-1", created_via="cli"),
                    )
                )
            result = session_retire_tool(
                config,
                actor_session_id="orchestrator-1",
                session_id="worker-1",
                reason="replaced",
                host=cast(
                    TerminalHost,
                    SimpleNamespace(
                        has_session=lambda _n: True,
                        terminate=lambda *_a, **_k: None,
                    ),
                ),
            )
            self.assertEqual(result["status"], "retired")
            self.assertEqual(result["strandedRowCount"], 2)
            surfaced = OperatorInboxStore(observer).current().get(result["surfacedRowId"])
            self.assertIsNotNone(surfaced)
            assert surfaced is not None
            self.assertEqual(surfaced.agentId, "orchestrator-1")
            self.assertIn("row-0", surfaced.response)
            self.assertIn("row-1", surfaced.response)

    def test_retire_without_pending_rows_surfaces_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            catalog = TerminalCatalog(root / "logs" / "dashboard" / "terminal-sessions.json")
            catalog.upsert(_seat("orchestrator-1", spawn_role="orchestrator"))
            catalog.upsert(
                _seat(
                    "worker-1",
                    task_document_ref=LEAF_1_REF,
                    spawn_role="worker",
                    spawned_by_session="orchestrator-1",
                )
            )
            result = session_retire_tool(
                config,
                actor_session_id="orchestrator-1",
                session_id="worker-1",
                reason="done",
                host=cast(
                    TerminalHost,
                    SimpleNamespace(
                        has_session=lambda _n: True,
                        terminate=lambda *_a, **_k: None,
                    ),
                ),
            )
            self.assertEqual(result["status"], "retired")
            self.assertNotIn("strandedRowIds", result)
            self.assertNotIn("surfacedRowId", result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
