"""260713-TES-L4 rebind/expiry mechanics: branch and idempotence coverage."""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from agents_remember.controlplane import operator_inbox_transitions as inbox_transitions
from agents_remember.controlplane.agent_notifier_signals import AgentNotifierSignalCooldownStore
from agents_remember.controlplane.expectation_rows import ExpectationRowStore
from agents_remember.controlplane.operator_inbox_records import (
    InboxAddress,
    InboxMessage,
    InboxPoster,
    InboxRouting,
    OperatorInboxEntry,
    create_operator_inbox_entry,
)
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.controlplane.operator_inbox_transitions import ExpiryOptions
from agents_remember.controlplane.signal_routing import StructuralRoutingError, derive_row_owner
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.mcp.tools.operator_inbox import operator_inbox_supersede_payload
from agents_remember.models.conversations.control_wire import SubmissionReceipt
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.observer.store import EventStore
from agents_remember.serving import inbox_delivery as inbox_delivery_module
from agents_remember.serving.agent_notifier import (
    run_agent_notifier_sweep,
)
from agents_remember.serving.agent_notifier_heartbeat import AgentNotifierHeartbeatStore
from agents_remember.serving.agent_notifier_models import (
    AgentNotifierContext,
)
from agents_remember.serving.inbox_reclamation import TmuxSessionNameSnapshot
from agents_remember.serving.terminal import TerminalHost
from agents_remember.serving.terminal_catalog import TerminalCatalog
from test_inbox_arrival_guarantee import NOW, _seat

SPRINT_REF = TaskDocumentRef(repository="repo-a", path="sprint/task.json")
MASTER_REF = TaskDocumentRef(repository="repo-a", path="master/task.json")
LEAF_REF = TaskDocumentRef(repository="repo-a", path="master/leaf-1.json")


class _Topology:
    def parent(self, ref: TaskDocumentRef) -> TaskDocumentRef | None:
        return {LEAF_REF: MASTER_REF, MASTER_REF: SPRINT_REF, SPRINT_REF: None}[ref]

    def altitude(self, ref: TaskDocumentRef) -> str:
        return {LEAF_REF: "leaf", MASTER_REF: "master", SPRINT_REF: "sprint"}[ref]


class TransitionIdempotenceTests(unittest.TestCase):
    """Terminal transitions are idempotent: the second call appends nothing (level-triggered
    sweeps re-decide the same finding on every pass)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = OperatorInboxStore(Path(self.tmp.name) / "logs" / "observer")
        self.entry = create_operator_inbox_entry(
            InboxMessage(ask="ask", response="resp", message_kind="message"),
            entry_id="e1",
            now=NOW.isoformat(),
            routing=InboxRouting(address=InboxAddress(lifecycle_id=None, agent_id="worker-1")),
            poster=InboxPoster(created_by="system", created_via="cli"),
        )
        self.store.append(self.entry)

    def test_landed_superseded_unresolved_expired_and_rebind_are_idempotent(self) -> None:
        transitions = (
            lambda: inbox_transitions.mark_landed(
                self.store, "e1", now=NOW.isoformat(), reason="x"
            ),
            lambda: inbox_transitions.mark_superseded(
                self.store, "e1", now=NOW.isoformat(), reason="x"
            ),
            lambda: inbox_transitions.mark_unresolved(
                self.store, "e1", now=NOW.isoformat(), reason="x"
            ),
            lambda: inbox_transitions.mark_expired(
                self.store, "e1", now=NOW.isoformat(), options=ExpiryOptions(reason="x")
            ),
        )
        for index, transition in enumerate(transitions):
            with self.subTest(transition=index):
                self.setUp()
                first, first_now = transition()
                self.assertTrue(first_now)
                rows_after_first = len(self.store.read())
                again, again_now = transition()
                self.assertFalse(again_now)
                self.assertEqual(len(self.store.read()), rows_after_first)
                self.assertEqual(again.state, first.state)


class RowOwnerDerivationTests(unittest.TestCase):
    """N14 rows re-resolve through task containment, never spawn provenance."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = TerminalCatalog(Path(self.tmp.name) / "catalog.json")
        self.topology = _Topology()

    def _row(self, **updates: object) -> OperatorInboxEntry:
        return create_operator_inbox_entry(
            InboxMessage(ask="ask", response="resp", message_kind="escalation"),
            entry_id="e1",
            now=NOW.isoformat(),
            routing=InboxRouting(address=InboxAddress(agent_id="manager-old")),
            poster=InboxPoster(created_by="system", created_via="cli"),
        ).model_copy(update=updates)

    def test_ambiguous_structural_owner_refuses_instead_of_role_mailbox_guess(self) -> None:
        for session_id in ("orch-a", "orch-b"):
            self.catalog.upsert(
                _seat(
                    session_id,
                    task_document_ref=SPRINT_REF,
                    seat_role="orchestrator",
                )
            )
        row = self._row(seatRole="manager", subjectTaskDocumentRef=MASTER_REF)
        with self.assertRaises(StructuralRoutingError):
            derive_row_owner(self.catalog, self.topology, row)

            # The two newest terminal markers fill the two remaining cap slots.


class StaleSnapshotTerminalAuthorityTests(unittest.TestCase):
    """F1: every terminal transition verifies the LATEST folded state at append time.

    A stale act-phase snapshot must never overwrite a different terminal truth: concurrent
    supersede stays ``superseded``, concurrent landed stays ``landed``, concurrent expired
    stays ``expired``, and a stale unresolved after landed appends nothing. The final
    FOLDED STORE STATE is the assertion, not the action strings.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = OperatorInboxStore(Path(self.tmp.name) / "logs" / "observer")
        self.store.append(
            create_operator_inbox_entry(
                InboxMessage(ask="ask", response="resp", message_kind="message"),
                entry_id="e1",
                now=NOW.isoformat(),
                routing=InboxRouting(address=InboxAddress(lifecycle_id=None, agent_id="worker-1")),
                poster=InboxPoster(created_by="system", created_via="cli"),
            )
        )
        self.stale = self.store.current()

    def _stale_landing(self) -> OperatorInboxEntry:
        return inbox_transitions.record_delivery(
            self.store,
            "e1",
            inbox_transitions.DeliveryAttempt(
                delivery_state="delivered",
                landed=True,
                adapter=inbox_transitions.AdapterReceipt(delivery_state="accepted"),
            ),
            now=(NOW + timedelta(seconds=2)).isoformat(),
            floor=inbox_transitions.RedeliveryFloor(current=self.stale, seconds=0.0),
        )

    def test_concurrent_supersede_survives_a_stale_landing_append(self) -> None:
        inbox_transitions.mark_superseded(
            self.store,
            "e1",
            now=(NOW + timedelta(seconds=1)).isoformat(),
            reason="explicit",
            superseded_by="owner",
        )
        returned = self._stale_landing()
        self.assertEqual(returned.state, "superseded")
        self.assertEqual(self.store.current()["e1"].state, "superseded")
        # Nothing appended: pending + superseded only.
        self.assertEqual(len(self.store.read()), 2)

    def test_concurrent_landed_survives_a_stale_unresolved(self) -> None:
        inbox_transitions.mark_landed(
            self.store, "e1", now=(NOW + timedelta(seconds=1)).isoformat(), reason="boundary"
        )
        latest, changed = inbox_transitions.mark_unresolved(
            self.store, "e1", now=(NOW + timedelta(seconds=2)).isoformat(), reason="attempt-limit"
        )
        self.assertFalse(changed)
        self.assertEqual(latest.state, "landed")
        self.assertEqual(self.store.current()["e1"].state, "landed")
        self.assertEqual(len(self.store.read()), 2)


class SupersedeDuringInFlightDeliveryTests(unittest.TestCase):
    """F1 e2e: an explicit supersede during an in-flight sweep delivery is never overwritten
    by the sweep's landing (no false ack of an explicitly superseded command)."""

    def test_supersede_during_in_flight_delivery_wins_over_landing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observer = root / "logs" / "observer"
            catalog = TerminalCatalog(root / "logs" / "dashboard" / "terminal-sessions.json")
            catalog.upsert(
                _seat(
                    "manager-1",
                    spawn_role="manager",
                    turn_state="turn-ended",
                    turn_state_changed_at=(NOW - timedelta(minutes=1)).isoformat(),
                    control_state="ready",
                    control_endpoint=Path("/tmp/x.sock"),
                    control_protocol="ar-harness-control/v1",
                )
            )
            store = OperatorInboxStore(observer)
            store.append(
                create_operator_inbox_entry(
                    InboxMessage(ask="dispatch", response="work", message_kind="message"),
                    entry_id="e1",
                    now=(NOW - timedelta(minutes=30)).isoformat(),
                    routing=InboxRouting(
                        address=InboxAddress(
                            lifecycle_id=None, agent_id="manager-1", recipient_role="manager"
                        )
                    ),
                    poster=InboxPoster(
                        created_by="agent-notifier", created_via="cli", sender_role="system"
                    ),
                ).model_copy(
                    update={
                        "deliveryState": "delivered",
                        "adapterDeliveryState": "accepted",
                        "lastAttemptAt": (NOW - timedelta(minutes=16)).isoformat(),
                        "nextAttemptAt": (NOW - timedelta(minutes=15)).isoformat(),
                    }
                )
            )
            ctx = AgentNotifierContext(
                catalog=catalog,
                host=cast(
                    TerminalHost,
                    SimpleNamespace(
                        has_session=lambda _n: True,
                        terminate=lambda *_a, **_k: None,
                    ),
                ),
                paster=cast("object", None),  # type: ignore[arg-type]
                inbox_store=store,
                expectation_store=ExpectationRowStore(observer),
                signal_cooldown_store=AgentNotifierSignalCooldownStore(observer),
                event_store=EventStore(observer),
                heartbeat_store=AgentNotifierHeartbeatStore(observer),
                coordination_root=root,
                tmux_name_snapshotter=lambda: TmuxSessionNameSnapshot(
                    frozenset(), "tmux-no-server"
                ),
            )
            submitted = threading.Event()
            release = threading.Event()

            def blocking_submit(_target, _text, submission):
                submitted.set()
                if not release.wait(timeout=5):
                    raise AssertionError("release not set")
                return SubmissionReceipt(
                    request_id=submission.request_id,
                    acceptance="immediate",
                    submitted_at=NOW.isoformat(),
                    accepted_at=NOW.isoformat(),
                )

            sweep_done = threading.Event()
            sweep_error: list[BaseException] = []

            def run_sweep() -> None:
                try:
                    run_agent_notifier_sweep(ctx, now=NOW)
                except BaseException as exc:
                    sweep_error.append(exc)
                finally:
                    sweep_done.set()

            with mock.patch.object(
                inbox_delivery_module,
                "submit_control_prompt",
                side_effect=blocking_submit,
            ):
                thread = threading.Thread(target=run_sweep)
                thread.start()
                self.assertTrue(submitted.wait(timeout=5), "delivery did not start")
                operator_inbox_supersede_payload(
                    McpRuntimeConfig(
                        config_path=root / "settings.json",
                        coordination_root=root,
                        workspace_root=root,
                        transcript_root=root / "logs" / "mcp",
                    ),
                    entry_id="e1",
                    reason="overtaken",
                    superseded_by="manager-1",
                )
                release.set()
                sweep_done.wait(timeout=10)
                thread.join(timeout=10)
            if sweep_error:
                raise sweep_error[0]
            final = store.current()["e1"]
            self.assertEqual(final.state, "superseded")
            self.assertEqual(final.terminalReason, "overtaken")


class ReboundDeliveryToReplacementTests(unittest.TestCase):
    """F3: after a sweep-time rebind to manager B, a subsequent sweep actually DELIVERS the
    row to B (B's session receives the push), not only re-addresses it."""

    def test_rebound_row_is_delivered_to_replacement_in_next_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observer = root / "logs" / "observer"
            catalog = TerminalCatalog(root / "catalog.json")
            catalog.upsert(
                _seat(
                    "orchestrator-1",
                    task_document_ref=SPRINT_REF,
                    seat_role="orchestrator",
                )
            )
            catalog.upsert(
                _seat(
                    "manager-old",
                    status="terminated",
                    terminated_at=(NOW - timedelta(minutes=10)).isoformat(),
                    task_document_ref=MASTER_REF,
                    seat_role="manager",
                    spawned_by_session="orchestrator-1",
                )
            )
            catalog.upsert(
                _seat(
                    "worker-1",
                    task_document_ref=LEAF_REF,
                    seat_role="worker",
                    spawned_by_session="manager-old",
                )
            )
            catalog.upsert(
                _seat(
                    "manager-new",
                    task_document_ref=MASTER_REF,
                    seat_role="manager",
                    turn_state="turn-ended",
                    turn_state_changed_at=(NOW - timedelta(minutes=1)).isoformat(),
                    control_state="ready",
                    control_endpoint=Path("/tmp/manager-new.sock"),
                    control_protocol="ar-harness-control/v1",
                )
            )
            store = OperatorInboxStore(observer)
            store.append(
                create_operator_inbox_entry(
                    InboxMessage(ask="ask", response="resp", message_kind="escalation"),
                    entry_id="e1",
                    now=(NOW - timedelta(minutes=5)).isoformat(),
                    routing=InboxRouting(
                        address=InboxAddress(
                            lifecycle_id=None, agent_id="manager-old", recipient_role="manager"
                        )
                    ),
                    poster=InboxPoster(
                        created_by="worker-1",
                        created_via="cli",
                        sender_agent_id="worker-1",
                        sender_role="worker",
                    ),
                ).model_copy(
                    update={
                        "subjectTaskDocumentRef": LEAF_REF,
                        "subjectAgentId": "worker-1",
                        "seatRole": "worker",
                    }
                )
            )
            ctx = AgentNotifierContext(
                catalog=catalog,
                host=cast(
                    TerminalHost,
                    SimpleNamespace(
                        has_session=lambda _n: True,
                        terminate=lambda *_a, **_k: None,
                    ),
                ),
                paster=cast("object", None),  # type: ignore[arg-type]
                inbox_store=store,
                expectation_store=ExpectationRowStore(observer),
                signal_cooldown_store=AgentNotifierSignalCooldownStore(observer),
                event_store=EventStore(observer),
                heartbeat_store=AgentNotifierHeartbeatStore(observer),
                coordination_root=root,
                tmux_name_snapshotter=lambda: TmuxSessionNameSnapshot(
                    frozenset(), "tmux-no-server"
                ),
            )
            with (
                mock.patch(
                    "agents_remember.serving._agent_notifier_actions.TaskDocumentTopology",
                    return_value=_Topology(),
                ),
                mock.patch(
                    "agents_remember.serving._agent_notifier_evaluation.TaskDocumentTopology",
                    return_value=_Topology(),
                ),
                mock.patch(
                    "agents_remember.serving.inbox_delivery.submit_control_prompt",
                    side_effect=lambda _target, _text, submission: SubmissionReceipt(
                        request_id=submission.request_id,
                        acceptance="immediate",
                        submitted_at=NOW.isoformat(),
                        accepted_at=NOW.isoformat(),
                    ),
                ) as submit,
            ):
                run_agent_notifier_sweep(ctx, now=NOW)
                self.assertEqual(store.current()["e1"].agentId, "manager-new")
                e1_calls = [
                    call for call in submit.call_args_list if call.args[2].request_id == "e1"
                ]
                self.assertEqual(e1_calls, [])
                run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=1))
            e1_calls = [call for call in submit.call_args_list if call.args[2].request_id == "e1"]
            self.assertEqual(len(e1_calls), 1)
            self.assertEqual(e1_calls[0].args[0].id, "manager-new")
            final = store.current()["e1"]
            self.assertEqual(final.state, "landed")
            self.assertEqual(final.deliveredToSession, "manager-new")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
