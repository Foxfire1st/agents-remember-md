from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

from agents_remember.controlplane.agent_notifier_signals import AgentNotifierSignalCooldownStore
from agents_remember.controlplane.expectation_rows import ExpectationRowStore
from agents_remember.controlplane.operator_inbox_records import (
    InboxAddress,
    InboxMessage,
    InboxPoster,
    InboxRouting,
    create_operator_inbox_entry,
)
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.observer.store import EventStore
from agents_remember.serving.agent_notifier import (
    AgentNotifierContext,
    run_agent_notifier_sweep,
)
from agents_remember.serving.agent_notifier_heartbeat import AgentNotifierHeartbeatStore
from agents_remember.serving.terminal import TerminalHost
from agents_remember.serving.terminal_catalog import TerminalCatalog
from test_agent_notifier import NOW, _entry, _fake_paster, _FakeHost
from test_agent_notifier_ladder import MASTER_REF, SPRINT_REF, _leaf_ref, _write_topology


class SweepIntegrationTests(unittest.TestCase):
    """Seeded drift across every predicate family -> the expected action set."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.coordination_root = root / "ar-coordination"
        _write_topology(self.coordination_root)
        observer_root = self.coordination_root / "logs" / "observer"
        self.catalog = TerminalCatalog(root / "catalog.json")
        self.catalog.upsert(
            replace(
                _entry("orchestrator-1", task_document_ref=SPRINT_REF),
                spawn_role="orchestrator",
            )
        )
        self.inbox_store = OperatorInboxStore(observer_root)
        self.expectation_store = ExpectationRowStore(observer_root)
        self.signal_cooldown_store = AgentNotifierSignalCooldownStore(observer_root)
        self.event_store = EventStore(observer_root)
        self.heartbeat_store = AgentNotifierHeartbeatStore(observer_root)

    def _ctx(self, **overrides: object) -> AgentNotifierContext:
        base: dict[str, object] = dict(
            catalog=self.catalog,
            host=cast(TerminalHost, _FakeHost()),
            paster=_fake_paster(),
            inbox_store=self.inbox_store,
            expectation_store=self.expectation_store,
            signal_cooldown_store=self.signal_cooldown_store,
            event_store=self.event_store,
            heartbeat_store=self.heartbeat_store,
            coordination_root=self.coordination_root,
            stale_seat_seconds=60.0,
        )
        base.update(overrides)
        return AgentNotifierContext(**base)  # type: ignore[arg-type]

    def test_seeded_drift_produces_expected_actions_and_ticks_heartbeat(self) -> None:
        # A worker seat spawned by a manager seat -- the routing edge signal-emit walk.
        self.catalog.upsert(
            replace(_entry("manager-1", task_document_ref=MASTER_REF), spawn_role="manager")
        )
        worker = replace(
            _entry("worker-1", task_document_ref=_leaf_ref(9)),
            spawn_role="worker",
            spawned_by_session="manager-1",
        )
        self.catalog.upsert(worker)

        # R2e: a seat gone stale past the cutoff -- exercises the signal-emit action path.
        stale_seat = replace(
            _entry("stale-1").with_turn_state(
                "stale", changed_at=(NOW - timedelta(minutes=5)).isoformat()
            ),
            spawn_role="worker",
            spawned_by_session="manager-1",
            cwd=Path("/workspace"),
            replacement_for_task_document_ref=_leaf_ref(10),
        )
        self.catalog.upsert(stale_seat)

        # R2d: an unacked inbox row, immediately redeliverable.
        inbox_entry = create_operator_inbox_entry(
            InboxMessage(ask="ask", response="resp"),
            entry_id="inbox-1",
            now=NOW.isoformat(),
            routing=InboxRouting(address=InboxAddress(lifecycle_id=None, agent_id="worker-1")),
            poster=InboxPoster(created_by="system", created_via="cli"),
        )
        self.inbox_store.append(inbox_entry)

        ctx = self._ctx()
        result = run_agent_notifier_sweep(ctx, now=NOW)

        finding_kinds = sorted(f.kind for f in result.findings)
        self.assertIn("inbox-redeliverable", finding_kinds)
        self.assertIn("seat-liveness", finding_kinds)

        action_kinds = {a.action for a in result.actions}
        self.assertIn("redeliver", action_kinds)
        self.assertIn("signal-emit", action_kinds)

        # The signal-emit action routed to the stale seat's manager. This legacy fixture has no
        # protocol endpoint, so delivery is loudly unsupported instead of raw-pasted.
        signal_actions = [a for a in result.actions if a.action == "signal-emit"]
        self.assertEqual(signal_actions[0].outcome, "unconfirmed")

        # The pre-existing row follows the same no-fallback contract.
        redeliver_actions = [a for a in result.actions if a.action == "redeliver"]
        self.assertEqual(redeliver_actions[0].outcome, "unconfirmed")

        # R5: the heartbeat ticked exactly once for this sweep.
        heartbeat = self.heartbeat_store.read()
        assert heartbeat is not None
        self.assertEqual(heartbeat.sweepCount, 1)
        self.assertEqual(heartbeat.lastTickAt, NOW.isoformat())

        # R4e: every action is logged as an orchestration.agent-notifier.* (or reused nudge) event.
        events = self.event_store.read(None)
        kinds = {event.kind for event in events}
        self.assertTrue(
            kinds
            & {
                "orchestration.agent-notifier.redeliver",
                "orchestration.agent-notifier.signal",
            }
        )

    def test_redeliver_budget_limits_attempts_and_heartbeat_reports_backlog(self) -> None:
        for index in range(3):
            self.catalog.upsert(_entry(f"seat-{index}"))
        for index in range(3):
            self.inbox_store.append(
                create_operator_inbox_entry(
                    InboxMessage(ask="ask", response="resp"),
                    entry_id=f"row-{index}",
                    now=NOW.isoformat(),
                    routing=InboxRouting(
                        address=InboxAddress(lifecycle_id=None, agent_id=f"seat-{index}")
                    ),
                    poster=InboxPoster(created_by="system", created_via="cli"),
                )
            )

        result = run_agent_notifier_sweep(self._ctx(redeliver_budget=1), now=NOW)

        redeliver_actions = [action for action in result.actions if action.action == "redeliver"]
        self.assertEqual(len(redeliver_actions), 1)
        self.assertEqual(result.pending_inbox_count, 3)
        self.assertEqual(result.redeliverable_inbox_count, 3)
        heartbeat = self.heartbeat_store.read()
        assert heartbeat is not None
        self.assertEqual(heartbeat.pendingInboxCount, 3)
        self.assertEqual(heartbeat.redeliverableInboxCount, 3)
        self.assertIsNotNone(heartbeat.lastSweepDurationSeconds)

    def test_repeated_seat_liveness_sweeps_coalesce_into_one_signal_row(self) -> None:
        self.catalog.upsert(
            replace(_entry("manager-1", task_document_ref=MASTER_REF), spawn_role="manager")
        )
        self.catalog.upsert(
            replace(
                _entry("worker-1", task_document_ref=_leaf_ref(3)).with_turn_state(
                    "stale", changed_at=(NOW - timedelta(minutes=5)).isoformat()
                ),
                spawn_role="worker",
                spawned_by_session="manager-1",
            )
        )
        ctx = self._ctx(signal_cooldown_seconds=900.0)

        run_agent_notifier_sweep(ctx, now=NOW)
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=10))

        signal_rows = [
            entry
            for entry in self.inbox_store.current().values()
            if entry.messageKind == "escalation"
        ]
        self.assertEqual(len(signal_rows), 1)
        self.assertEqual(signal_rows[0].agentId, "manager-1")
        first = signal_rows[0]

        run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=901))
        signal_rows = [
            entry
            for entry in self.inbox_store.current().values()
            if entry.messageKind == "escalation"
        ]
        # Ruled invariant (developer, 2026-07-09): past the cooldown the re-fired condition
        # RENEWS its one existing row (same id, bumped ts) instead of appending a duplicate.
        self.assertEqual(len(signal_rows), 1)
        self.assertEqual(signal_rows[0].id, first.id)
        self.assertGreater(signal_rows[0].ts, first.ts)

    def test_pending_backlog_does_not_burst_redeliver_before_floor_after_restart(self) -> None:
        for index in range(3):
            entry = create_operator_inbox_entry(
                InboxMessage(ask="ask", response="resp"),
                entry_id=f"row-{index}",
                now=NOW.isoformat(),
                routing=InboxRouting(
                    address=InboxAddress(lifecycle_id=None, agent_id=f"worker-{index}")
                ),
                poster=InboxPoster(created_by="system", created_via="cli"),
            ).model_copy(
                update={
                    "deliveryState": "delivered",
                    "attemptCount": 1,
                    "lastAttemptAt": NOW.isoformat(),
                    "nextAttemptAt": (NOW + timedelta(seconds=900)).isoformat(),
                }
            )
            self.inbox_store.append(entry)

        restarted_ctx = self._ctx()
        result = run_agent_notifier_sweep(restarted_ctx, now=NOW + timedelta(seconds=60))

        self.assertEqual([a for a in result.actions if a.action == "redeliver"], [])
        self.assertEqual(result.redeliverable_inbox_count, 0)
