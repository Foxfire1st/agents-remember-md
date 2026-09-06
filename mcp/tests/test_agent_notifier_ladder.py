from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
from _scaling import assert_bounded_count
from agents_remember.controlplane import operator_inbox_transitions as inbox_transitions
from agents_remember.controlplane.agent_notifier_signals import AgentNotifierSignalCooldownStore
from agents_remember.controlplane.expectation_rows import (
    Expectation,
    ExpectationRowStore,
    ExpectationSubject,
    write_expectation_row,
)
from agents_remember.controlplane.operator_inbox_records import (
    InboxAddress,
    InboxMessage,
    InboxPoster,
    InboxRouting,
    InboxSubject,
    OperatorInboxEntry,
    create_operator_inbox_entry,
)
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.models.conversations.control_wire import SubmissionReceipt
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.observer.store import EventStore
from agents_remember.serving import agent_notifier as agent_notifier_module
from agents_remember.serving._agent_notifier_evaluation import PERSISTENT_FAILURE_ATTEMPTS
from agents_remember.serving.agent_notifier import (
    AgentNotifierContext,
    AgentNotifierFinding,
    _inactivity_signal_chain_progressed,
    evaluate_dead_upstream_findings,
    run_agent_notifier_sweep,
)
from agents_remember.serving.agent_notifier_heartbeat import AgentNotifierHeartbeatStore
from agents_remember.serving.hosted_session_runtime import HostedSessionRuntime
from agents_remember.serving.inbox_delivery import InboxDeliveryLog, deliver_inbox_entry
from agents_remember.serving.terminal import TerminalHost
from agents_remember.serving.terminal_catalog import TerminalCatalog
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.tasks.document_refs import TaskDocumentTopology
from test_agent_notifier import NOW, _entry, _fake_paster, _FakeHost

SPRINT_REF = TaskDocumentRef(repository="repo-a", path="sprint/task.json")
MASTER_REF = TaskDocumentRef(repository="repo-a", path="260707_master/task.json")
LEAF_1_REF = TaskDocumentRef(repository="repo-a", path="260707_master/leaf-1.json")
LEAF_9_REF = TaskDocumentRef(repository="repo-a", path="260707_master/leaf-9.json")


def _task_doc(**values: object) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": values.pop("id"),
            "slug": values.pop("slug"),
            "title": values.pop("title"),
            "kind": values.pop("kind"),
            "repo": "repo-a",
            "createdAt": "2026-07-07T00:00",
            **values,
        }
    )


def _write_topology(root: Path) -> TaskDocumentTopology:
    """Create one sprint/master and enough real leaf documents for every ladder fixture."""
    leaf_names = tuple(f"leaf-{index}" for index in range(60))
    task_root = root / "tasks" / "repo-a"
    write_task_doc(
        task_root / "sprint",
        _task_doc(
            id="SPRINT",
            slug="sprint",
            title="Sprint",
            kind="master",
            orchestrates=["260707_master"],
        ),
    )
    write_task_doc(
        task_root / "260707_master",
        _task_doc(
            id="MASTER",
            slug="260707_master",
            title="Master",
            kind="master",
            subTasks=[
                {
                    "number": leaf,
                    "name": leaf,
                    "file": f"{leaf}.md",
                    "status": "inProgress",
                }
                for leaf in leaf_names
            ],
        ),
    )
    for leaf in leaf_names:
        write_task_doc(
            task_root / "260707_master",
            _task_doc(
                id=leaf,
                slug=leaf,
                title=leaf,
                kind="subTask",
                master="task.md",
            ),
        )
    return TaskDocumentTopology(root)


def _leaf_ref(index: int) -> TaskDocumentRef:
    return TaskDocumentRef(repository="repo-a", path=f"260707_master/leaf-{index}.json")


class DeadUpstreamPredicateTests(unittest.TestCase):
    def test_worker_with_dead_manager_fires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            topology = _write_topology(root)
            catalog = TerminalCatalog(root / "catalog.json")
            catalog.upsert(
                replace(
                    _entry("orchestrator-1", task_document_ref=SPRINT_REF),
                    spawn_role="orchestrator",
                )
            )
            catalog.upsert(
                replace(
                    _entry("manager-1", task_document_ref=MASTER_REF),
                    status="terminated",
                    spawn_role="manager",
                )
            )
            catalog.upsert(
                replace(
                    _entry("worker-1", task_document_ref=LEAF_1_REF),
                    spawn_role="worker",
                    spawned_by_session="manager-1",
                )
            )
            findings = evaluate_dead_upstream_findings(catalog, topology)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].kind, "dead-upstream")
            self.assertEqual(findings[0].session_id, "worker-1")

    def test_live_owner_does_not_fire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            topology = _write_topology(root)
            catalog = TerminalCatalog(root / "catalog.json")
            catalog.upsert(
                replace(
                    _entry("orchestrator-1", task_document_ref=SPRINT_REF),
                    spawn_role="orchestrator",
                )
            )
            catalog.upsert(
                replace(
                    _entry("manager-1", task_document_ref=MASTER_REF),
                    spawn_role="manager",
                )
            )
            catalog.upsert(
                replace(
                    _entry("worker-1", task_document_ref=LEAF_1_REF),
                    spawn_role="worker",
                    spawned_by_session="manager-1",
                )
            )
            self.assertEqual(evaluate_dead_upstream_findings(catalog, topology), [])

    def test_no_provenance_at_all_does_not_fire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            topology = _write_topology(root)
            catalog = TerminalCatalog(root / "catalog.json")
            catalog.upsert(replace(_entry("worker-1"), spawn_role="worker"))
            self.assertEqual(evaluate_dead_upstream_findings(catalog, topology), [])


class InactivityChainProgressTests(unittest.TestCase):
    """Chain-progress suppression on relay-authored inactivity rows (rename-window F1)."""

    def test_inactivity_chain_progress_suppresses_legacy_and_current_ask_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            topology = _write_topology(root)
            catalog = TerminalCatalog(root / "catalog.json")
            catalog.upsert(
                replace(
                    _entry("manager-current", task_document_ref=MASTER_REF),
                    spawn_role="manager",
                )
            )
            catalog.upsert(
                replace(
                    _entry("worker-1", task_document_ref=LEAF_9_REF),
                    spawn_role="worker",
                    spawned_by_session="manager-current",
                )
            )
            catalog.upsert(
                replace(
                    _entry("reviewer-1", status="landed"),
                    spawn_role="reviewer",
                    spawned_by_session="manager-current",
                    replacement_for_task_document_ref=LEAF_9_REF,
                    landed_at=(NOW - timedelta(minutes=1)).isoformat(),
                )
            )

            def inactivity_row(*, entry_id: str, ask: str, created_by: str) -> OperatorInboxEntry:
                return create_operator_inbox_entry(
                    InboxMessage(
                        ask=ask,
                        response="worker-1 inactive",
                        message_kind="escalation",
                        subject=InboxSubject(
                            task_document_ref=LEAF_9_REF,
                            seat_role="worker",
                            agent_id="worker-1",
                        ),
                    ),
                    entry_id=entry_id,
                    now=(NOW - timedelta(minutes=10)).isoformat(),
                    routing=InboxRouting(
                        address=InboxAddress(
                            lifecycle_id=None,
                            agent_id="manager-current",
                            recipient_role="manager",
                        )
                    ),
                    poster=InboxPoster(created_by=created_by, created_via="cli"),
                )

            legacy = inactivity_row(
                entry_id="e-legacy",
                ask="Supervisor observed seat-liveness: turn-state-stale",
                created_by="supervisor",
            )
            current = inactivity_row(
                entry_id="e-current",
                ask="Agent notifier observed seat-liveness: turn-state-stale",
                created_by="agent-notifier",
            )
            self.assertTrue(_inactivity_signal_chain_progressed(catalog, topology, legacy))
            self.assertTrue(_inactivity_signal_chain_progressed(catalog, topology, current))


@pytest.mark.integration
class LadderWalkIntegrationTests(unittest.TestCase):
    """R6 fixtures: silent seat, dead intermediate, dead manager with live workers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.coordination_root = root / "ar-coordination"
        _write_topology(self.coordination_root)
        observer_root = self.coordination_root / "logs" / "observer"
        self.catalog = TerminalCatalog(root / "catalog.json")
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

    def _events(self) -> set[str]:
        return {event.kind for event in self.event_store.read(None)}

    def test_delivered_dispatch_never_rebinds(self) -> None:
        self.catalog.upsert(
            replace(_entry("manager-1", task_document_ref=MASTER_REF), spawn_role="manager")
        )
        self.catalog.upsert(
            replace(
                _entry("worker-1"),
                status="terminated",
                terminated_at=(NOW - timedelta(minutes=10)).isoformat(),
                spawn_role="worker",
                spawned_by_session="manager-1",
            )
        )
        entry = create_operator_inbox_entry(
            InboxMessage(ask="brief", response="work", message_kind="dispatch-brief"),
            entry_id="dispatch-1",
            now=(NOW - timedelta(minutes=10)).isoformat(),
            routing=InboxRouting(
                address=InboxAddress(
                    lifecycle_id=None, agent_id="worker-1", recipient_role="worker"
                )
            ),
            poster=InboxPoster(created_by="manager-1", created_via="cli"),
        ).model_copy(
            update={
                "deliveryState": "delivered",
                "deliveryDetail": "harness-log-confirmed",
            }
        )
        self.inbox_store.append(entry)

        result = run_agent_notifier_sweep(self._ctx(), now=NOW)

        self.assertEqual(
            [f for f in result.findings if f.kind in ("rebind-due", "rebind-expired")],
            [],
        )
        current = self.inbox_store.current()[entry.id]
        # Exact-pinned: even a dead addressee never rebinds or expires via the rebind path; a
        # fresh brief comes from the owner.
        self.assertEqual(current.state, "pending")
        self.assertEqual(current.agentId, "worker-1")
        self.assertEqual([a for a in result.actions if a.action in ("rebind", "expire")], [])

    def test_silent_live_seat_reaches_unresolved_after_attempt_ceiling(self) -> None:
        self.catalog.upsert(replace(_entry("orchestrator-1"), spawn_role="orchestrator"))
        self.catalog.upsert(
            replace(_entry("manager-1"), spawn_role="manager", spawned_by_session="orchestrator-1")
        )
        self.catalog.upsert(
            replace(_entry("worker-1"), spawn_role="worker", spawned_by_session="manager-1")
        )
        entry = create_operator_inbox_entry(
            InboxMessage(ask="ask", response="resp", message_kind="escalation"),
            entry_id="e1",
            now=(NOW - timedelta(minutes=5)).isoformat(),
            routing=InboxRouting(
                address=InboxAddress(
                    lifecycle_id=None, agent_id="worker-1", recipient_role="worker"
                )
            ),
            poster=InboxPoster(created_by="system", created_via="cli"),
        )
        self.inbox_store.append(entry)

        ctx = self._ctx()
        now = NOW
        for _ in range(PERSISTENT_FAILURE_ATTEMPTS):
            run_agent_notifier_sweep(ctx, now=now)
            current = self.inbox_store.current()["e1"]
            if current.state != "pending":
                break
            assert current.nextAttemptAt is not None
            now = datetime.fromisoformat(current.nextAttemptAt)

        # N3: five attempts resolve the live-but-silent row terminal ``unresolved`` with its
        # delivery evidence intact -- no ladder rung, no escalation, no sibling rows.
        final = self.inbox_store.current()["e1"]
        self.assertEqual(final.attemptCount, PERSISTENT_FAILURE_ATTEMPTS)
        self.assertEqual(final.state, "unresolved")
        self.assertEqual(final.terminalReason, "attempt-limit")
        self.assertNotIn("orchestration.escalation.rung", self._events())
        self.assertIn("orchestration.agent-notifier.unresolved", self._events())
        self.assertEqual(len(self.inbox_store.current()), 1)
        self.assertEqual(final.ask, "ask")
        self.assertEqual(final.recipientRole, "worker")
        self.assertEqual(final.agentId, "worker-1")

    def test_duplicate_rebind_findings_cannot_rebind_twice_in_one_sweep(self) -> None:
        self.catalog.upsert(replace(_entry("manager-1"), spawn_role="manager"))
        self.catalog.upsert(
            replace(
                _entry("manager-1", task_document_ref=MASTER_REF),
                status="terminated",
                terminated_at=(NOW - timedelta(minutes=10)).isoformat(),
                spawn_role="manager",
            )
        )
        self.catalog.upsert(
            replace(
                _entry("worker-1", task_document_ref=LEAF_1_REF),
                spawn_role="worker",
                spawned_by_session="manager-1",
            )
        )
        self.catalog.upsert(
            replace(
                _entry("manager-2", task_document_ref=MASTER_REF),
                spawn_role="manager",
            )
        )
        self.inbox_store.append(
            create_operator_inbox_entry(
                InboxMessage(
                    ask="ask",
                    response="resp",
                    message_kind="escalation",
                    subject=InboxSubject(
                        task_document_ref=LEAF_1_REF,
                        seat_role="worker",
                        agent_id="worker-1",
                    ),
                ),
                entry_id="e1",
                now=(NOW - timedelta(minutes=5)).isoformat(),
                routing=InboxRouting(
                    address=InboxAddress(
                        lifecycle_id=None, agent_id="manager-1", recipient_role="manager"
                    )
                ),
                poster=InboxPoster(
                    created_by="worker-1",
                    created_via="cli",
                    sender_agent_id="worker-1",
                    sender_role="worker",
                ),
            )
        )
        duplicate = AgentNotifierFinding(
            kind="rebind-due",
            detail="replacement-owner",
            session_id="manager-1",
            source_id="e1",
        )
        with mock.patch.object(
            agent_notifier_module, "evaluate_predicates", return_value=[duplicate, duplicate]
        ):
            result = run_agent_notifier_sweep(self._ctx(), now=NOW)

        rebound = self.inbox_store.current()["e1"]
        self.assertEqual(rebound.agentId, "manager-2")
        self.assertEqual(result.actions[1].outcome, "skipped")

    def test_dead_manager_row_rebinds_to_replacement_within_grace(self) -> None:
        self.catalog.upsert(
            replace(
                _entry("orchestrator-1", task_document_ref=SPRINT_REF),
                spawn_role="orchestrator",
            )
        )
        self.catalog.upsert(
            replace(
                _entry("manager-1"),
                status="terminated",
                terminated_at=(NOW - timedelta(minutes=10)).isoformat(),
                spawn_role="manager",
                spawned_by_session="orchestrator-1",
                task_document_ref=MASTER_REF,
            )
        )
        self.catalog.upsert(
            replace(
                _entry("worker-1", task_document_ref=LEAF_1_REF),
                spawn_role="worker",
                spawned_by_session="manager-1",
            )
        )
        self.catalog.upsert(
            replace(
                _entry("manager-2", task_document_ref=MASTER_REF),
                spawn_role="manager",
            )
        )
        entry = create_operator_inbox_entry(
            InboxMessage(
                ask="ask",
                response="resp",
                message_kind="escalation",
                subject=InboxSubject(
                    task_document_ref=LEAF_1_REF,
                    seat_role="worker",
                    agent_id="worker-1",
                ),
            ),
            entry_id="e1",
            now=(NOW - timedelta(minutes=5)).isoformat(),
            routing=InboxRouting(
                address=InboxAddress(
                    lifecycle_id=None, agent_id="manager-1", recipient_role="manager"
                )
            ),
            poster=InboxPoster(
                created_by="worker-1",
                created_via="cli",
                sender_agent_id="worker-1",
                sender_role="worker",
            ),
        )
        self.inbox_store.append(entry)

        run_agent_notifier_sweep(self._ctx(), now=NOW)
        rebound = self.inbox_store.current()["e1"]
        self.assertEqual(rebound.agentId, "manager-2")
        self.assertEqual(rebound.ownerAgentId, "manager-2")
        self.assertEqual(rebound.attemptCount, 0)
        rebind_events = [
            event
            for event in self.event_store.read(None)
            if event.kind == "orchestration.agent-notifier.rebind"
        ]
        self.assertEqual(len(rebind_events), 1)
        self.assertEqual(rebind_events[0].data["toAgentId"], "manager-2")

    def test_dead_manager_without_replacement_expires_to_architect_mailbox(self) -> None:
        self.catalog.upsert(
            replace(
                _entry("orchestrator-1", task_document_ref=SPRINT_REF),
                spawn_role="orchestrator",
            )
        )
        self.catalog.upsert(
            replace(
                _entry("manager-1"),
                status="terminated",
                terminated_at=(NOW - timedelta(minutes=10)).isoformat(),
                spawn_role="manager",
                spawned_by_session="orchestrator-1",
                task_document_ref=MASTER_REF,
            )
        )
        self.catalog.upsert(
            replace(
                _entry("worker-1", task_document_ref=LEAF_1_REF),
                spawn_role="worker",
                spawned_by_session="manager-1",
            )
        )
        entry = create_operator_inbox_entry(
            InboxMessage(
                ask="ask",
                response="resp",
                message_kind="escalation",
                subject=InboxSubject(
                    task_document_ref=LEAF_1_REF,
                    seat_role="worker",
                    agent_id="worker-1",
                ),
            ),
            entry_id="e1",
            now=(NOW - timedelta(minutes=10)).isoformat(),
            routing=InboxRouting(
                address=InboxAddress(
                    lifecycle_id=None, agent_id="manager-1", recipient_role="manager"
                )
            ),
            poster=InboxPoster(
                created_by="worker-1",
                created_via="cli",
                sender_agent_id="worker-1",
                sender_role="worker",
            ),
        )
        self.inbox_store.append(entry)

        result = run_agent_notifier_sweep(self._ctx(), now=NOW)

        self.assertNotIn("redeliver", {action.action for action in result.actions})
        expired = self.inbox_store.current()["e1"]
        self.assertEqual(expired.state, "expired")
        self.assertEqual(expired.terminalReason, "rebind-grace-expired")
        # N3: the dead owner chain surfaces to the scoped architect mailbox -- a mailbox, not
        # a ladder rung.
        self.assertEqual(expired.recipientRole, "architect")
        self.assertIsNone(expired.agentId)
        self.assertIn("orchestration.agent-notifier.rebind-expired", self._events())
        # Workers stay their own seats -- never re-parented, never absorbing the dead role.
        worker1 = self.catalog.get("worker-1")
        assert worker1 is not None
        self.assertEqual(worker1.status, "running")

    def test_landed_row_produces_no_retry_nudge_or_escalation_ever(self) -> None:
        """N16 regression: the delivered-but-unconsumed class is dissolved -- a landed row
        must produce no further retry, nudge, or escalation of any kind."""
        self.catalog.upsert(replace(_entry("manager-1"), spawn_role="manager"))
        entry = create_operator_inbox_entry(
            InboxMessage(ask="signal", response="worker done", message_kind="state-signal"),
            entry_id="e1",
            now=(NOW - timedelta(minutes=5)).isoformat(),
            routing=InboxRouting(
                address=InboxAddress(
                    lifecycle_id=None, agent_id="manager-1", recipient_role="manager"
                )
            ),
            poster=InboxPoster(
                created_by="agent-notifier", created_via="cli", sender_role="system"
            ),
        )
        self.inbox_store.append(entry)
        inbox_transitions.mark_landed(
            self.inbox_store,
            "e1",
            now=NOW.isoformat(),
            reason="adapter-accepted-at-turn-boundary",
        )

        for offset in (0, 10, 30):
            result = run_agent_notifier_sweep(self._ctx(), now=NOW + timedelta(seconds=offset))
            self.assertFalse(any(f.source_id == "e1" for f in result.findings))
            self.assertFalse(any(a.finding.source_id == "e1" for a in result.actions))
        self.assertEqual(self.inbox_store.current()["e1"].state, "landed")
        self.assertEqual(
            self.inbox_store.list_redeliverable(now=NOW + timedelta(hours=1)),
            [],
        )

    def test_relay_restart_reconciles_by_request_id_without_duplicate_submission(self) -> None:
        """R7: a relay kill/restart mid-redelivery replays the SAME correlated request -- the
        row fires exactly once more at the boundary and lands, never duplicating the push."""
        self.catalog.upsert(replace(_entry("architect-1"), spawn_role="architect"))
        self.catalog.upsert(
            replace(
                _entry("worker-1"),
                turn_state="working",
                turn_state_changed_at=NOW.isoformat(),
                control_state="ready",
                control_endpoint=Path("/tmp/worker.sock"),
                control_protocol="ar-harness-control/v1",
            )
        )
        entry = create_operator_inbox_entry(
            InboxMessage(ask="dispatch", response="work", message_kind="message"),
            entry_id="e1",
            now=(NOW - timedelta(minutes=30)).isoformat(),
            routing=InboxRouting(
                address=InboxAddress(
                    lifecycle_id=None, agent_id="worker-1", recipient_role="worker"
                )
            ),
            poster=InboxPoster(created_by="manager-1", created_via="cli"),
        )
        self.inbox_store.append(entry)

        with mock.patch(
            "agents_remember.serving.inbox_delivery.submit_control_prompt",
            side_effect=lambda _target, _text, submission: SubmissionReceipt(
                request_id=submission.request_id,
                acceptance="immediate",
                submitted_at=NOW.isoformat(),
                accepted_at=NOW.isoformat(),
            ),
        ) as submit:
            first = deliver_inbox_entry(
                InboxDeliveryLog(store=self.inbox_store, entry=entry, at=NOW.isoformat()),
                sessions=HostedSessionRuntime(
                    catalog=self.catalog, host=cast(TerminalHost, _FakeHost())
                ),
                paster=_fake_paster(),
            )
        # Accepted mid-turn: delivered evidence, but NOT landed (N1 gate).
        self.assertEqual(first.adapterDeliveryState, "accepted")
        self.assertEqual(first.state, "pending")
        self.assertEqual(submit.call_count, 1)

        # The relay restarts; the worker reaches a turn boundary; the SAME request id is
        # reconciled (never resubmitted) and the correlated acceptance now lands the row.
        worker = self.catalog.get("worker-1")
        assert worker is not None
        self.catalog.upsert(
            replace(
                worker,
                turn_state="turn-ended",
                turn_state_changed_at=(NOW + timedelta(minutes=1)).isoformat(),
            )
        )
        redelivered = deliver_inbox_entry(
            InboxDeliveryLog(
                store=self.inbox_store,
                entry=self.inbox_store.current()["e1"],
                at=(NOW + timedelta(minutes=1)).isoformat(),
            ),
            sessions=HostedSessionRuntime(
                catalog=self.catalog, host=cast(TerminalHost, _FakeHost())
            ),
            paster=_fake_paster(),
        )
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(redelivered.state, "landed")
        self.assertEqual(redelivered.attemptCount, 2)

    def test_unacked_backlog_reaches_a_fixed_point_with_absent_developer(self) -> None:
        """The 2026-07-09 meltdown regression (quiescence probe the HFX2-L12 audit lacked):
        with NO acks, NO live seats, and hours of sweeps, the inbox must reach a fixed point --
        exactly the seeded root-cause rows, each terminally resolved by the rebind-grace expiry,
        on-disk log bounded near folded size. The pre-fix ladder diverged here: every rung
        transition minted a new ladder-eligible pending row, so an absent developer grew
        67k lines / 227 MB overnight."""
        seeded = 9
        for index in range(seeded):
            self.inbox_store.append(
                create_operator_inbox_entry(
                    InboxMessage(
                        ask=f"turn report {index}",
                        response="resp",
                        message_kind="turn-report",
                        subject=InboxSubject(
                            task_document_ref=_leaf_ref(index),
                            seat_role="worker",
                            agent_id=f"dead-seat-{index}",
                        ),
                    ),
                    entry_id=f"root-{index}",
                    now=NOW.isoformat(),
                    routing=InboxRouting(
                        address=InboxAddress(
                            lifecycle_id=None,
                            agent_id=f"dead-seat-{index}",
                            recipient_role="manager",
                        )
                    ),
                    poster=InboxPoster(created_by="system", created_via="cli"),
                )
            )

        ctx = self._ctx()
        moment = NOW
        for _ in range(50):  # 50 sweeps x 6 min = 5 hours of absent developer
            moment += timedelta(minutes=6)
            run_agent_notifier_sweep(ctx, now=moment)
            current = self.inbox_store.current()
            self.assertLessEqual(len(current), seeded)
            self.assertTrue(
                all(entry.state in {"pending", "expired"} for entry in current.values())
            )

        final = self.inbox_store.current()
        self.assertEqual(len(final), seeded)
        self.assertEqual(sorted(final), [f"root-{index}" for index in range(seeded)])
        # Every row resolved terminal through the grace path -- never an escalation rung.
        self.assertTrue(all(entry.state == "expired" for entry in final.values()))
        self.assertNotIn("orchestration.escalation.rung", self._events())
        # Per-sweep compaction keeps the on-disk log within one sweep's appends of folded size.
        lines = [
            line
            for line in self.inbox_store.log_path().read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        # One row reaches a nine-snapshot fixed point (initial row, delivery/rung/escalation marks);
        # the divergent pre-fix shape produced THOUSANDS of lines here.
        self.assertLessEqual(len(lines), seeded * 9)

    def test_manager_replacement_keeps_worker_structurally_connected(self) -> None:
        self.catalog.upsert(
            replace(
                _entry("orchestrator-1", task_document_ref=SPRINT_REF),
                spawn_role="orchestrator",
            )
        )
        self.catalog.upsert(
            replace(
                _entry("manager-1", task_document_ref=MASTER_REF),
                status="terminated",
                spawn_role="manager",
                spawned_by_session="orchestrator-1",
            )
        )
        self.catalog.upsert(
            replace(
                _entry("worker-1", task_document_ref=LEAF_1_REF),
                spawn_role="worker",
                spawned_by_session="manager-1",
            )
        )
        self.catalog.upsert(
            replace(
                _entry("manager-2", task_document_ref=MASTER_REF),
                spawn_role="manager",
            )
        )
        ctx = self._ctx()
        result = run_agent_notifier_sweep(ctx, now=NOW)
        dead_upstream_findings = [f for f in result.findings if f.kind == "dead-upstream"]
        self.assertEqual(dead_upstream_findings, [])
        events = [
            event
            for event in self.event_store.read(None)
            if event.kind == "orchestration.agent-notifier.dead-upstream"
        ]
        legacy_events = [
            event
            for event in self.event_store.read(None)
            if event.kind == "orchestration.supervisor.dead-upstream"
        ]
        self.assertEqual(events, [])
        self.assertEqual(legacy_events, [])


@pytest.mark.integration
class Cs6SweepScalingTests(unittest.TestCase):
    """260707-HFX2-L12 CS-6 sweep regressions: the store reads + escalation emission a single
    sweep does must NOT scale with the finding count (the L7 accidental-quadratic floor)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.coordination_root = root / "ar-coordination"
        _write_topology(self.coordination_root)
        observer_root = self.coordination_root / "logs" / "observer"
        self.catalog = TerminalCatalog(root / "catalog.json")
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

    def _wrap_reads(self, store: object) -> dict[str, int]:
        counter = {"count": 0}
        original = store.read  # type: ignore[attr-defined]

        def counting_read(*args, **kwargs):  # type: ignore[no-untyped-def]
            counter["count"] += 1
            return original(*args, **kwargs)

        store.read = counting_read  # type: ignore[attr-defined]
        return counter

    def _seed_stale_workers(self, count: int) -> None:
        self.catalog.upsert(
            replace(_entry("manager-1", task_document_ref=MASTER_REF), spawn_role="manager")
        )
        for index in range(count):
            self.catalog.upsert(
                replace(
                    _entry(f"worker-{index}", task_document_ref=_leaf_ref(index)).with_turn_state(
                        "stale", changed_at=(NOW - timedelta(minutes=5)).isoformat()
                    ),
                    spawn_role="worker",
                    spawned_by_session="manager-1",
                )
            )

    def test_signal_cooldown_store_read_at_most_once_per_sweep_regardless_of_findings(self) -> None:
        """Z1: the seeded finding. F seat-liveness findings -> F cooldown checks, but the signal
        log is read ONCE per sweep (in compact) via the threaded snapshot -- not once per finding
        (the O(F x L) freeze the L9 reviewer flagged)."""
        for worker_count in (2, 40):
            with self.subTest(workers=worker_count):
                self.setUp()
                self._seed_stale_workers(worker_count)
                counter = self._wrap_reads(self.signal_cooldown_store)
                result = run_agent_notifier_sweep(self._ctx(), now=NOW)
                signal_emits = [a for a in result.actions if a.action == "signal-emit"]
                self.assertEqual(len(signal_emits), worker_count)  # every finding hit in_cooldown
                assert_bounded_count(
                    counter["count"], 1, label=f"signal reads/sweep at F={worker_count}"
                )
                heartbeat = self.heartbeat_store.read()
                assert heartbeat is not None
                self.assertEqual(heartbeat.sweepCount, 1)

    def test_owner_signal_emissions_are_load_shed_by_escalation_budget(self) -> None:
        """The preserved escalationBudget is a per-sweep load-shed cap: F seat-liveness findings
        with F > budget emit at most budget owner signals this sweep; the rest re-fire next sweep
        (level-triggered), so nothing is lost and no judgment is added."""
        for worker_count, budget, expected in ((60, 10, 10), (60, 250, 60)):
            with self.subTest(workers=worker_count, budget=budget):
                self.setUp()
                self._seed_stale_workers(worker_count)
                ctx = self._ctx(escalation_budget=budget)
                result = run_agent_notifier_sweep(ctx, now=NOW)
                signal_emits = [a for a in result.actions if a.action == "signal-emit"]
                self.assertEqual(len(signal_emits), expected)
                self.assertEqual(
                    len([f for f in result.findings if f.kind == "seat-liveness"]),
                    expected,
                )

    def test_expectation_store_compacted_once_with_zero_findings_per_sweep(self) -> None:
        """Z4b replacement: expectation rows are an owner-visible surface, never evaluated --
        K overdue rows produce ZERO findings and the store is still read exactly once per sweep
        (the compaction pass), so reads stay flat instead of growing by K."""
        reads_by_k: dict[int, int] = {}
        for overdue_count in (2, 40):
            self.setUp()
            for index in range(overdue_count):
                write_expectation_row(
                    self.expectation_store,
                    Expectation(
                        kind="verdict-by",
                        source_id=f"seat-{index}",
                        subject=ExpectationSubject(
                            agent_id="worker-1",
                            task_document_ref=LEAF_1_REF,
                            seat_role="worker",
                        ),
                    ),
                    row_id=f"exp-{index}",
                    now=NOW - timedelta(minutes=10),
                    sla_seconds=60.0,
                )
            counter = self._wrap_reads(self.expectation_store)
            result = run_agent_notifier_sweep(self._ctx(), now=NOW)
            self.assertEqual([f for f in result.findings if f.kind == "expectation-overdue"], [])
            self.assertFalse(any(a.action == "auto-nudge" for a in result.actions))
            reads_by_k[overdue_count] = counter["count"]
        # Reads are flat in K: one compaction pass per sweep, never per row.
        self.assertEqual(
            reads_by_k[40],
            reads_by_k[2],
            f"expectation reads scaled with row count: {reads_by_k}",
        )
        assert_bounded_count(reads_by_k[40], 6, label="expectation reads/sweep")

    def test_dead_seat_expiry_emission_is_exactly_one_per_row_per_sweep(self) -> None:
        """Z17 replacement: a backlog of dead-seat rows emits exactly one rebind-expired
        finding+action per row per sweep (linear, never quadratic), and level-triggered
        re-fires stop because the rows resolve terminal."""
        for pending_count in (20, 60):
            with self.subTest(pending=pending_count):
                self.setUp()
                for index in range(pending_count):
                    self.inbox_store.append(
                        create_operator_inbox_entry(
                            InboxMessage(
                                ask="ask",
                                response="resp",
                                message_kind="escalation",
                                subject=InboxSubject(
                                    task_document_ref=_leaf_ref(index),
                                    seat_role="worker",
                                    agent_id=f"worker-{index}",
                                ),
                            ),
                            entry_id=f"esc-{index}",
                            now=(NOW - timedelta(minutes=10)).isoformat(),
                            routing=InboxRouting(
                                address=InboxAddress(lifecycle_id=None, agent_id=f"worker-{index}")
                            ),
                            poster=InboxPoster(created_by="system", created_via="cli"),
                        )
                    )
                result = run_agent_notifier_sweep(self._ctx(), now=NOW)
                expiry_findings = [f for f in result.findings if f.kind == "rebind-expired"]
                assert_bounded_count(
                    len(expiry_findings),
                    pending_count,
                    label=f"expiry findings at N={pending_count}",
                )
                self.assertEqual(len(expiry_findings), pending_count)
                self.assertEqual(
                    len([a for a in result.actions if a.action == "expire"]),
                    pending_count,
                )
                # Terminal now: a second sweep re-fires nothing.
                again = run_agent_notifier_sweep(self._ctx(), now=NOW + timedelta(seconds=1))
                self.assertEqual(
                    [f for f in again.findings if f.kind == "rebind-expired"],
                    [],
                )
                heartbeat = self.heartbeat_store.read()
                assert heartbeat is not None
                self.assertEqual(heartbeat.sweepCount, 2)
