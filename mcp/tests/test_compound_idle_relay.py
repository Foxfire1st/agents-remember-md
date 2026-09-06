"""Compound-idle relay to orchestrators: forcing tests.

Manager + ALL workers idle -> exactly one durable state-signal to the owning
orchestrator; partial sets, unknown members and retired rows never fire;
flap re-arm; busy-orchestrator boundary hold with exactly one landing;
dedupe across re-projection; set-member identity via spawn provenance and
binding; manager non-reaction residue relayed one level up.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
from agents_remember.controlplane.agent_notifier_signals import AgentNotifierSignalCooldownStore
from agents_remember.controlplane.expectation_rows import ExpectationRowStore
from agents_remember.controlplane.operator_inbox_records import (
    InboxAddress,
    InboxMessage,
    InboxPoster,
    InboxRouting,
    OperatorInboxEntry,
    create_operator_inbox_entry,
    state_signal_landed,
)
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.models.conversations.control_wire import SubmissionReceipt
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.observer.store import EventStore
from agents_remember.serving._agent_notifier_actions import act_on_finding
from agents_remember.serving.agent_notifier import AgentNotifierContext, run_agent_notifier_sweep
from agents_remember.serving.agent_notifier_heartbeat import AgentNotifierHeartbeatStore
from agents_remember.serving.agent_notifier_models import (
    AgentNotifierActionResult,
    AgentNotifierFinding,
)
from agents_remember.serving.seat_turn_truth import record_compound_idle_emitted
from agents_remember.serving.state_signals import (
    compound_idle_sets,
    compound_idle_signature,
    evaluate_compound_idle_findings,
)
from agents_remember.serving.terminal import TerminalHost
from agents_remember.serving.terminal_catalog import TerminalCatalog, TerminalCatalogEntry
from agents_remember.serving.terminal_paste import PasteResult, TerminalPaster
from agents_remember.serving.terminal_tmux import TmuxProbeResult
from agents_remember.tasks import TaskDocument, write_task_doc
from test_agent_notifier_ladder import MASTER_REF, SPRINT_REF, _leaf_ref, _write_topology

NOW = datetime(2026, 7, 13, 15, 41, 0, tzinfo=UTC)
LEAF_A = _leaf_ref(9)
LEAF_B = _leaf_ref(10)
OTHER_MASTER_LEAF = TaskDocumentRef(repository="repo-b", path="other_master/leaf-11.json")


def _write_other_master(root: Path) -> None:
    task_root = root / "tasks" / "repo-b"
    common = {"repo": "repo-b", "createdAt": "2026-07-07T00:00"}
    write_task_doc(
        task_root / "sprint",
        TaskDocument.model_validate(
            {
                **common,
                "id": "OTHER-SPRINT",
                "slug": "other-sprint",
                "title": "Other Sprint",
                "kind": "master",
                "orchestrates": ["other_master"],
            }
        ),
    )
    write_task_doc(
        task_root / "other_master",
        TaskDocument.model_validate(
            {
                **common,
                "id": "OTHER-MASTER",
                "slug": "other_master",
                "title": "Other Master",
                "kind": "master",
                "subTasks": [
                    {
                        "number": "leaf-11",
                        "name": "leaf-11",
                        "file": "leaf-11.md",
                        "status": "inProgress",
                    }
                ],
            }
        ),
    )
    write_task_doc(
        task_root / "other_master",
        TaskDocument.model_validate(
            {
                **common,
                "id": "leaf-11",
                "slug": "leaf-11",
                "title": "Leaf 11",
                "kind": "subTask",
                "master": "task.md",
            }
        ),
    )


def _entry(
    session_id: str,
    *,
    task_document_ref: TaskDocumentRef | None = None,
    **overrides: object,
) -> TerminalCatalogEntry:
    return TerminalCatalogEntry(
        id=session_id,
        label=f"Chat {session_id}",
        kind="harness",
        harness="codex",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("codex",),
        created_at="2026-07-13T00:00:00+00:00",
        last_attached_at="2026-07-13T00:00:00+00:00",
        status="running",
        task_document_ref=task_document_ref,
        **overrides,  # type: ignore[arg-type]
    )


def _orchestrator(session_id: str = "orchestrator-1", **overrides: object) -> TerminalCatalogEntry:
    state: dict[str, object] = {
        "turn_state": "turn-ended",
        "turn_state_changed_at": NOW.isoformat(),
    }
    state.update(overrides)
    return replace(
        _entry(
            session_id,
            task_document_ref=SPRINT_REF,
            spawn_role="orchestrator",
            control_endpoint=Path("/tmp/orchestrator.sock"),
            control_state="ready",
        ),
        **state,
    )


def _manager(session_id: str = "manager-1", **overrides: object) -> TerminalCatalogEntry:
    state: dict[str, object] = {
        "turn_state": "turn-ended",
        "turn_state_changed_at": NOW.isoformat(),
    }
    state.update(overrides)
    return replace(
        _entry(
            session_id,
            task_document_ref=MASTER_REF,
            spawn_role="manager",
            spawned_by_session="orchestrator-1",
            spawned_by_lifecycle="L-orch",
        ),
        **state,
    )


def _idle_worker(
    session_id: str,
    *,
    task_document_ref: TaskDocumentRef | None = LEAF_A,
    **overrides: object,
) -> TerminalCatalogEntry:
    state: dict[str, object] = {
        "turn_state": "turn-ended",
        "turn_state_changed_at": NOW.isoformat(),
    }
    state.update(overrides)
    return replace(
        _entry(
            session_id,
            task_document_ref=task_document_ref,
            spawn_role="worker",
            spawned_by_session="manager-1",
        ),
        **state,
    )


def _idle_subordinate(session_id: str, role: str, **overrides: object) -> TerminalCatalogEntry:
    task_document_ref = overrides.pop("task_document_ref", LEAF_A)
    assert isinstance(task_document_ref, TaskDocumentRef) or task_document_ref is None
    return replace(
        _idle_worker(session_id, task_document_ref=task_document_ref, **overrides),
        spawn_role=role,
        seat_role=role,
    )


class _FakeHost:
    def has_session(self, _tmux_name: str) -> bool:
        return True

    def probe_session(self, _tmux_name: str) -> TmuxProbeResult:
        return TmuxProbeResult(exists=True, evidence="alive")

    def get(self, _sid: str) -> None:
        return None


def _accepted_paster() -> TerminalPaster:
    class _AcceptedPaster:
        def paste(
            self,
            _tmux_name: str,
            _text: str,
            *,
            submit: bool = False,
            **_kwargs: object,
        ) -> PasteResult:
            return PasteResult(delivered=True, submitted=submit)

    return cast(TerminalPaster, _AcceptedPaster())


@pytest.mark.integration
class CompoundIdleRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.coordination_root = root / "ar-coordination"
        self.topology = _write_topology(self.coordination_root)
        _write_other_master(self.coordination_root)
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
            paster=_accepted_paster(),
            inbox_store=self.inbox_store,
            expectation_store=self.expectation_store,
            signal_cooldown_store=self.signal_cooldown_store,
            event_store=self.event_store,
            heartbeat_store=self.heartbeat_store,
            coordination_root=self.coordination_root,
            stale_seat_seconds=60.0,
            redeliver_rate_limit_seconds=900.0,
        )
        base.update(overrides)
        return AgentNotifierContext(**base)  # type: ignore[arg-type]

    def _state_signals(self) -> list[OperatorInboxEntry]:
        return [
            entry
            for entry in self.inbox_store.current().values()
            if entry.messageKind == "state-signal"
        ]

    def _accepted_receipt(self, request_id: str) -> SubmissionReceipt:
        return SubmissionReceipt(
            request_id=request_id,
            acceptance="immediate",
            submitted_at=NOW.isoformat(),
            accepted_at=NOW.isoformat(),
        )

    def test_compound_idle_positive_exactly_one_orchestrator_signal(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        self.catalog.upsert(_idle_worker("worker-2", task_document_ref=LEAF_B))
        ctx = self._ctx()
        with mock.patch(
            "agents_remember.serving.inbox_delivery.submit_control_prompt",
            side_effect=lambda _target, _text, submission: self._accepted_receipt(
                submission.request_id
            ),
        ) as submit:
            result = run_agent_notifier_sweep(ctx, now=NOW)
        signals = self._state_signals()
        self.assertEqual(len(signals), 1, result.actions)
        signal = signals[0]
        self.assertEqual(signal.agentId, "orchestrator-1")
        self.assertEqual(signal.taskDocumentRef, SPRINT_REF)
        self.assertEqual(signal.subjectTaskDocumentRef, MASTER_REF)
        self.assertEqual(signal.seatRole, "manager")
        self.assertEqual(signal.subjectAgentId, "manager-1")
        self.assertTrue(
            signal.ask.startswith("Agent notifier observed state-signal: compound-idle")
        )
        self.assertIn(MASTER_REF.path, signal.response)
        self.assertIn(LEAF_A.path, signal.response)
        self.assertIn(LEAF_B.path, signal.response)
        self.assertTrue(state_signal_landed(signal))
        self.assertEqual(submit.call_count, 1)
        manager = self.catalog.get("manager-1")
        assert manager is not None
        self.assertIsNotNone(manager.compound_idle_emitted_for)

        # Re-projection with the same idle snapshot must not mint a second row.
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=10))
        self.assertEqual(len(self._state_signals()), 1)

    def test_partial_set_active_worker_no_signal(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        self.catalog.upsert(
            _idle_worker(
                "worker-2",
                task_document_ref=LEAF_B,
                turn_state="working",
                turn_state_changed_at=NOW.isoformat(),
            )
        )
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        self.assertEqual(self._state_signals(), [])

    def test_owned_reviewer_and_curator_share_the_compound_idle_episode_and_rearm_once(
        self,
    ) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        self.catalog.upsert(_idle_subordinate("reviewer-1", "reviewer", task_document_ref=LEAF_B))
        self.catalog.upsert(_idle_subordinate("curator-1", "curator", task_document_ref=LEAF_B))
        ctx = self._ctx()
        run_agent_notifier_sweep(ctx, now=NOW)
        self.assertEqual(len(self._state_signals()), 1)
        self.assertIn("as reviewer", self._state_signals()[0].response)
        self.assertIn("as curator", self._state_signals()[0].response)

        reviewer = self.catalog.get("reviewer-1")
        assert reviewer is not None
        self.catalog.upsert(
            replace(
                reviewer,
                turn_state="working",
                turn_state_changed_at=(NOW + timedelta(seconds=1)).isoformat(),
            )
        )
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=1))
        self.assertEqual(len(self._state_signals()), 1)

        self.catalog.upsert(
            replace(
                reviewer,
                turn_state="turn-ended",
                turn_state_changed_at=(NOW + timedelta(seconds=2)).isoformat(),
            )
        )
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=2))
        self.assertEqual(len(self._state_signals()), 2)

    def test_owned_curator_working_blocks_then_rearms_one_compound_idle_wake(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        curator = _idle_subordinate("curator-1", "curator", turn_state="working")
        self.catalog.upsert(curator)
        ctx = self._ctx()

        run_agent_notifier_sweep(ctx, now=NOW)
        self.assertEqual(self._state_signals(), [])
        self.catalog.upsert(
            replace(
                curator,
                turn_state="turn-ended",
                turn_state_changed_at=(NOW + timedelta(seconds=1)).isoformat(),
            )
        )
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=1))
        self.assertEqual(len(self._state_signals()), 1)
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=2))
        self.assertEqual(len(self._state_signals()), 1)

    def test_unsupported_leaf_role_neither_blocks_nor_joins(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        future = _idle_subordinate(
            "analyst-1",
            "analyst",
            task_document_ref=LEAF_B,
            turn_state="working",
        )
        self.catalog.upsert(future)
        ctx = self._ctx()
        run_agent_notifier_sweep(ctx, now=NOW)
        self.assertEqual(len(self._state_signals()), 1)

        self.catalog.upsert(
            replace(
                future,
                turn_state="awaiting-input",
                turn_state_changed_at=(NOW + timedelta(seconds=1)).isoformat(),
            )
        )
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=1))
        self.assertEqual(len(self._state_signals()), 1)
        self.assertNotIn("as analyst", self._state_signals()[0].response)
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=2))
        self.assertEqual(len(self._state_signals()), 1)

    def test_owner_tier_and_cross_master_children_are_structurally_excluded(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        self.catalog.upsert(
            _idle_subordinate("architect-child", "architect", task_document_ref=LEAF_B)
        )
        self.catalog.upsert(
            _idle_subordinate("other-master-child", "analyst", task_document_ref=OTHER_MASTER_LEAF)
        )

        run_agent_notifier_sweep(self._ctx(), now=NOW)
        signals = self._state_signals()
        self.assertEqual(len(signals), 1)
        self.assertIn(LEAF_A.path, signals[0].response)
        self.assertNotIn("architect-child", signals[0].response)
        self.assertNotIn("other-master-child", signals[0].response)

    def test_unknown_member_fail_closed_no_signal(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-known"))
        self.catalog.upsert(
            _idle_worker("worker-unknown", task_document_ref=LEAF_B, turn_state=None)
        )
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        self.assertEqual(self._state_signals(), [])

    def test_unknown_manager_fail_closed_no_signal(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager(turn_state=None))
        self.catalog.upsert(_idle_worker("worker-1"))
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        self.assertEqual(self._state_signals(), [])

    def test_awaiting_input_member_counts_as_idle(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        self.catalog.upsert(
            _idle_worker(
                "worker-2",
                task_document_ref=LEAF_B,
                turn_state="awaiting-input",
                turn_state_changed_at=NOW.isoformat(),
            )
        )
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        signals = self._state_signals()
        self.assertEqual(len(signals), 1)
        self.assertIn(LEAF_B.path, signals[0].response)

    def test_flap_rearms_after_a_seat_returns_to_activity(self) -> None:
        self.catalog.upsert(_orchestrator())
        manager = _manager()
        self.catalog.upsert(manager)
        self.catalog.upsert(_idle_worker("worker-1"))
        worker2 = _idle_worker("worker-2", task_document_ref=LEAF_B)
        self.catalog.upsert(worker2)
        ctx = self._ctx()
        with mock.patch(
            "agents_remember.serving.inbox_delivery.submit_control_prompt",
            side_effect=lambda _target, _text, submission: self._accepted_receipt(
                submission.request_id
            ),
        ):
            run_agent_notifier_sweep(ctx, now=NOW)
        self.assertEqual(len(self._state_signals()), 1)

        # worker-2 returns to activity, then to idle again: the set re-arms and re-fires.
        self.catalog.upsert(
            replace(
                worker2,
                turn_state="working",
                turn_state_changed_at=(NOW + timedelta(minutes=1)).isoformat(),
            )
        )
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(minutes=2))
        self.assertEqual(len(self._state_signals()), 1)
        self.catalog.upsert(
            replace(
                worker2,
                turn_state="turn-ended",
                turn_state_changed_at=(NOW + timedelta(minutes=3)).isoformat(),
            )
        )
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(minutes=4))
        signals = self._state_signals()
        self.assertEqual(len(signals), 2)
        self.assertNotEqual(signals[0].id, signals[1].id)
        manager = self.catalog.get("manager-1")
        assert manager is not None
        self.assertIsNotNone(manager.compound_idle_emitted_for)

    def test_busy_orchestrator_holds_at_boundary_then_lands_exactly_once(self) -> None:
        orchestrator = replace(
            _orchestrator(),
            turn_state="working",
            turn_state_changed_at=(NOW - timedelta(minutes=1)).isoformat(),
        )
        self.catalog.upsert(orchestrator)
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        ctx = self._ctx()
        with mock.patch(
            "agents_remember.serving.inbox_delivery.submit_control_prompt",
            side_effect=lambda _target, _text, submission: self._accepted_receipt(
                submission.request_id
            ),
        ) as submit:
            # Tick 1: the compound-idle signal is durable immediately; the gate holds it.
            run_agent_notifier_sweep(ctx, now=NOW)
            signals = self._state_signals()
            self.assertEqual(len(signals), 1)
            held = signals[0]
            self.assertEqual(held.deliveryState, "queued")
            self.assertEqual(held.adapterDeliveryState, "queued")
            self.assertIsNotNone(held.nextAttemptAt)
            self.assertEqual(submit.call_count, 0)

            # Tick 2: still working and not yet due -- one row, no landing.
            run_agent_notifier_sweep(ctx, now=NOW + timedelta(minutes=2))
            self.assertEqual(len(self._state_signals()), 1)
            self.assertEqual(submit.call_count, 0)

            # Tick 3: past the escalation SLA (300 s) with the orchestrator still working --
            # the held row must NOT be escalated or pushed mid-turn.
            run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=301))
            self.assertEqual(submit.call_count, 0)
            self.assertEqual(self._state_signals()[0].rung, 0)
            self.assertEqual(self._state_signals()[0].deliveryState, "queued")

            # Tick 4: past the redelivery floor (900 s) with the orchestrator still working --
            # the held row must NOT be redelivered mid-turn either.
            run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=901))
            self.assertEqual(submit.call_count, 0)
            self.assertEqual(self._state_signals()[0].rung, 0)
            self.assertEqual(self._state_signals()[0].deliveryState, "queued")

            # Tick 5: the orchestrator reaches a turn boundary -> boundary drain lands it once.
            self.catalog.upsert(
                replace(
                    orchestrator,
                    turn_state="turn-ended",
                    turn_state_changed_at=(NOW + timedelta(minutes=16)).isoformat(),
                )
            )
            run_agent_notifier_sweep(ctx, now=NOW + timedelta(minutes=17))
            landed = self._state_signals()[0]
            self.assertEqual(landed.deliveryState, "delivered")
            self.assertEqual(landed.adapterDeliveryState, "accepted")
            self.assertTrue(state_signal_landed(landed))
            self.assertIsNone(landed.nextAttemptAt)
            self.assertEqual(submit.call_count, 1)

            # Tick 6: landed is terminal on this path -- no retry, no second landing.
            run_agent_notifier_sweep(ctx, now=NOW + timedelta(minutes=19))
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(len(self._state_signals()), 1)

    def test_same_master_membership_ignores_spawn_provenance(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        # Runtime spawn provenance is private history; the leaf's parent master owns the seat.
        self.catalog.upsert(
            _idle_worker(
                "worker-bound",
                task_document_ref=LEAF_A,
                spawned_by_session="other-manager",
            )
        )
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        signals = self._state_signals()
        self.assertEqual(len(signals), 1)
        self.assertIn(LEAF_A.path, signals[0].response)

    def test_member_identity_other_master_worker_not_in_set(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        # An ACTIVE worker of another master must neither block nor join the set.
        self.catalog.upsert(
            _idle_worker(
                "worker-other",
                task_document_ref=OTHER_MASTER_LEAF,
                spawned_by_session=None,
                turn_state="working",
                turn_state_changed_at=NOW.isoformat(),
            )
        )
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        signals = self._state_signals()
        self.assertEqual(len(signals), 1)
        self.assertNotIn(OTHER_MASTER_LEAF.path, signals[0].response)

    def test_foreign_master_worker_active_does_not_block(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        # Spawned by the manager but bound to another master: not a member, so an
        # ACTIVE foreign worker must not suppress the manager's compound signal.
        self.catalog.upsert(
            _idle_worker(
                "worker-foreign",
                task_document_ref=OTHER_MASTER_LEAF,
                spawned_by_session="manager-1",
                turn_state="working",
                turn_state_changed_at=NOW.isoformat(),
            )
        )
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        signals = self._state_signals()
        self.assertEqual(len(signals), 1)
        self.assertNotIn(OTHER_MASTER_LEAF.path, signals[0].response)

    def test_foreign_master_worker_idle_does_not_join(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        # An IDLE foreign worker must neither join the set nor be named in the payload.
        self.catalog.upsert(
            _idle_worker(
                "worker-foreign",
                task_document_ref=OTHER_MASTER_LEAF,
                spawned_by_session="manager-1",
            )
        )
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        signals = self._state_signals()
        self.assertEqual(len(signals), 1)
        self.assertIn(LEAF_A.path, signals[0].response)
        self.assertNotIn(OTHER_MASTER_LEAF.path, signals[0].response)

    def test_zero_worker_manager_does_not_signal(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        self.assertEqual(self._state_signals(), [])

    def test_unbound_manager_never_forms_set(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(
            _manager(task_document_ref=None, replacement_for_task_document_ref=None)
        )
        self.catalog.upsert(_idle_worker("worker-1"))
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        self.assertEqual(self._state_signals(), [])

    def test_unbound_worker_never_joins_or_blocks(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        # A worker with no master identity at all (spawned by the manager but bound
        # to nothing) neither blocks the set nor joins it.
        self.catalog.upsert(
            _idle_worker(
                "worker-unbound",
                task_document_ref=None,
                replacement_for_task_document_ref=None,
                turn_state="working",
                turn_state_changed_at=NOW.isoformat(),
            )
        )
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        signals = self._state_signals()
        self.assertEqual(len(signals), 1)
        self.assertNotIn("- as worker", signals[0].response)

    def test_retired_rows_never_count_status_first(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        # A retired row keeps stale turn-ended state; it must never count as an idle member.
        self.catalog.upsert(
            _idle_worker(
                "worker-retired",
                task_document_ref=LEAF_B,
                status="terminated",
                terminated_at=NOW.isoformat(),
            )
        )
        # An exited row with stale turn state is likewise not a running member.
        self.catalog.upsert(
            _idle_worker(
                "worker-exited",
                task_document_ref=LEAF_B,
                status="exited",
            )
        )
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        signals = self._state_signals()
        self.assertEqual(len(signals), 1)
        self.assertIn(LEAF_A.path, signals[0].response)
        self.assertNotIn(LEAF_B.path, signals[0].response)

    def test_structural_owner_routes_without_spawn_provenance(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager(spawned_by_session=None, spawned_by_lifecycle=None))
        self.catalog.upsert(_idle_worker("worker-1"))
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        self.assertEqual(len(self._state_signals()), 1)

    def test_manager_non_reaction_residue_relays_to_orchestrator(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(
            replace(
                _manager(),
                turn_state_changed_at=(NOW - timedelta(minutes=10)).isoformat(),
            )
        )
        landed = create_operator_inbox_entry(
            InboxMessage(ask="state-signal", response="resp"),
            entry_id="manager-landed-1",
            now=(NOW - timedelta(minutes=10)).isoformat(),
            routing=InboxRouting(address=InboxAddress(agent_id="manager-1")),
            poster=InboxPoster(created_by="agent-notifier", created_via="cli"),
        ).model_copy(
            update={
                "state": "landed",
                "deliveryState": "delivered",
                "adapterDeliveryState": "accepted",
                "deliveredToSession": "manager-1",
                "adapterAcceptedAt": (NOW - timedelta(minutes=10)).isoformat(),
            }
        )
        self.inbox_store.append(landed)
        ctx = self._ctx()
        run_agent_notifier_sweep(ctx, now=NOW)
        signals = self._state_signals()
        # The manager has no workers, so no compound-idle fact; the distinct
        # non-reaction residue fact is the one relay to the orchestrator.
        self.assertEqual(len(signals), 1)
        residue = signals[0]
        self.assertIn("non-reaction", residue.ask)
        self.assertEqual(residue.agentId, "orchestrator-1")
        self.assertIn(MASTER_REF.path, residue.response)
        self.assertIn("manager-landed-1", residue.response)
        manager = self.catalog.get("manager-1")
        assert manager is not None
        self.assertEqual(manager.non_reaction_emitted_for, "manager-landed-1")

        # Same episode: no second relay.
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(minutes=1))
        self.assertEqual(len(self._state_signals()), 1)

    def test_manager_residue_routes_structurally_without_spawn_provenance(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(
            _manager(
                spawned_by_session=None,
                spawned_by_lifecycle=None,
                turn_state_changed_at=(NOW - timedelta(minutes=10)).isoformat(),
            )
        )
        landed = create_operator_inbox_entry(
            InboxMessage(ask="state-signal", response="resp"),
            entry_id="manager-landed-2",
            now=(NOW - timedelta(minutes=10)).isoformat(),
            routing=InboxRouting(address=InboxAddress(agent_id="manager-1")),
            poster=InboxPoster(created_by="agent-notifier", created_via="cli"),
        ).model_copy(
            update={
                "state": "landed",
                "deliveryState": "delivered",
                "adapterDeliveryState": "accepted",
                "deliveredToSession": "manager-1",
                "adapterAcceptedAt": (NOW - timedelta(minutes=10)).isoformat(),
            }
        )
        self.inbox_store.append(landed)
        run_agent_notifier_sweep(self._ctx(), now=NOW)
        self.assertEqual(len(self._state_signals()), 1)
        self.assertEqual(self._state_signals()[0].agentId, "orchestrator-1")

    def test_compound_idle_marker_guard_suppresses_repeat_record(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        ctx = self._ctx()
        run_agent_notifier_sweep(ctx, now=NOW)
        manager = self.catalog.get("manager-1")
        assert manager is not None
        signature = manager.compound_idle_emitted_for
        self.assertIsNotNone(signature)
        # The already-emitted guard is a no-op write (crash/re-entrancy defense).
        record_compound_idle_emitted(self.catalog, "manager-1", signature or "")
        manager_after = self.catalog.get("manager-1")
        assert manager_after is not None
        self.assertEqual(manager_after.compound_idle_emitted_for, signature)
        run_agent_notifier_sweep(ctx, now=NOW + timedelta(seconds=10))
        self.assertEqual(len(self._state_signals()), 1)

    def _act_compound(
        self,
        session_id: str,
        *,
        source_id: str | None = "trigger",
    ) -> AgentNotifierActionResult:
        finding = AgentNotifierFinding(
            kind="compound-idle-due",
            detail="compound-idle",
            session_id=session_id,
            task_document_ref=MASTER_REF,
            seat_role="manager",
            source_id=source_id,
        )
        return act_on_finding(self._ctx(), finding, now=NOW)

    def test_emit_skips_no_seat_row(self) -> None:
        result = self._act_compound("ghost")
        self.assertEqual(result.action, "compound-idle")
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(result.detail, "no seat row")

    def test_emit_skips_finding_without_session_id(self) -> None:
        finding = AgentNotifierFinding(
            kind="compound-idle-due",
            detail="compound-idle",
            session_id=None,
            seat_role="manager",
            source_id="trigger",
        )
        result = act_on_finding(self._ctx(), finding, now=NOW)
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(result.detail, "no seat row")

    def test_emit_skips_already_emitted(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        ctx = self._ctx()
        run_agent_notifier_sweep(ctx, now=NOW)
        self.assertEqual(len(self._state_signals()), 1)
        result = act_on_finding(
            ctx,
            AgentNotifierFinding(
                kind="compound-idle-due",
                detail="compound-idle",
                session_id="manager-1",
                task_document_ref=MASTER_REF,
                seat_role="manager",
                source_id="stale-trigger",
            ),
            now=NOW + timedelta(seconds=10),
        )
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(result.detail, "already emitted")
        self.assertEqual(len(self._state_signals()), 1)

    def test_emit_skips_no_longer_idle_at_action_time(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        ctx = self._ctx()
        findings = evaluate_compound_idle_findings(self.catalog, self.topology)
        self.assertEqual(len(findings), 1)
        worker = self.catalog.get("worker-1")
        assert worker is not None
        self.catalog.upsert(
            replace(
                worker,
                turn_state="working",
                turn_state_changed_at=(NOW + timedelta(minutes=1)).isoformat(),
            )
        )
        result = act_on_finding(ctx, findings[0], now=NOW)
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(result.detail, "no longer idle")
        self.assertEqual(self._state_signals(), [])

    def test_action_time_signature_replaces_stale_evaluation_signature(self) -> None:
        self.catalog.upsert(_orchestrator())
        self.catalog.upsert(_manager())
        self.catalog.upsert(_idle_worker("worker-1"))
        ctx = self._ctx()
        findings = evaluate_compound_idle_findings(self.catalog, self.topology)
        self.assertEqual(len(findings), 1)
        stale = findings[0]
        # A concurrent catalog write moves the worker's boundary while the set stays
        # idle: the emitter must post and record the ACTION-time signature, never the
        # stale evaluation-time one.
        self.catalog.upsert(
            replace(
                _idle_worker("worker-1"),
                turn_state_changed_at=(NOW + timedelta(seconds=5)).isoformat(),
            )
        )
        with mock.patch(
            "agents_remember.serving.inbox_delivery.submit_control_prompt",
            side_effect=lambda _target, _text, submission: self._accepted_receipt(
                submission.request_id
            ),
        ):
            result = act_on_finding(ctx, stale, now=NOW)
        self.assertEqual(result.outcome, "delivered")
        signals = self._state_signals()
        self.assertEqual(len(signals), 1)
        fresh = compound_idle_signature(
            compound_idle_sets(self.catalog, self.topology)["manager-1"]
        )
        self.assertNotEqual(fresh, stale.source_id)
        self.assertIn(fresh, signals[0].ask)
        manager = self.catalog.get("manager-1")
        assert manager is not None
        self.assertEqual(manager.compound_idle_emitted_for, fresh)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
