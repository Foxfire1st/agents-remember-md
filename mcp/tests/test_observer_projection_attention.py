from __future__ import annotations

import unittest

from agents_remember.controlplane.attention_dismissals import AttentionDismissalRecord
from agents_remember.observer.events import Event
from agents_remember.observer.projection import (
    DriftSnapshotNode,
    ProviderNode,
)
from agents_remember.observer.reducer import (
    AnalyticalInputs,
    WorkspaceStructure,
    build_attention_queue,
    project_workspace,
)
from test_observer_projection import FRESH, _event, _started


class AttentionQueueTests(unittest.TestCase):
    def test_blocked_and_provider_down_rank_alarm_first(self) -> None:
        proj = project_workspace(
            [
                [_started(lifecycle_id="LC1")],
                [
                    _started(lifecycle_id="LC2"),
                    _event(
                        "lifecycle.blocked",
                        lifecycle_id="LC2",
                        ts="2026-06-13T18:00:05+00:00",
                        ask={"kind": "gate", "question": "Approve the plan?"},
                    ),
                ],
            ],
            structure=WorkspaceStructure(
                enclosures=[], providers=[ProviderNode(id="cgc", state="stopped", ok=False)]
            ),
            now=FRESH,
        )
        queue = proj.analytics.attentionQueue
        self.assertEqual(queue[0].kind, "provider-down")  # alarm sorts above warn
        blocked = next(item for item in queue if item.kind == "blocked-gate")
        self.assertEqual((blocked.lifecycleId, blocked.detail), ("LC2", "Approve the plan?"))


class AttentionDismissalTests(unittest.TestCase):
    """Leaf-28 S5.2: a lifecycle acknowledgement suppresses one current occurrence,
    and a newer triggering signal re-surfaces it."""

    AWAIT_TS = "2026-06-13T18:00:05+00:00"
    DISMISS_TS = "2026-06-13T18:00:10+00:00"  # >= AWAIT_TS / blocked ts / T0

    def _await_log(self) -> list[Event]:
        return [
            _started(lifecycle_id="LC1"),
            _event(
                "lifecycle.awaiting-developer",
                lifecycle_id="LC1",
                ts=self.AWAIT_TS,
                summary="Drafted the plan; awaiting your review.",
            ),
        ]

    def _queue_for(self, logs, *, now, dismissals=None, **kw):  # type: ignore[no-untyped-def]
        return project_workspace(
            logs,
            structure=WorkspaceStructure(enclosures=[], providers=kw.get("providers", [])),
            now=now,
            given=AnalyticalInputs(
                gates=kw.get("gates") or [], attention_dismissals=dismissals or {}
            ),
        ).analytics.attentionQueue

    def _dismissal(
        self,
        item_id: str,
        *,
        lifecycle_id: str | None = "LC1",
        kind: str | None = None,
        dismissed_at: str | None = None,
    ) -> dict[str, AttentionDismissalRecord]:
        return {
            item_id: AttentionDismissalRecord(
                itemId=item_id,
                kind=kind,
                lifecycleId=lifecycle_id,
                dismissedAt=dismissed_at or self.DISMISS_TS,
            )
        }

    def test_dismiss_suppresses_actionable_drift_until_newer_snapshot(self) -> None:
        dismissed = self._dismissal(
            "actionable-drift:repo-a:main",
            lifecycle_id=None,
            kind="actionable-drift",
            dismissed_at="2026-06-13T18:00:10+00:00",
        )
        old_snapshot = DriftSnapshotNode(
            repository="repo-a",
            branch="main",
            actionableCount=1,
            checkedAt="2026-06-13T18:00:00+00:00",
        )
        self.assertEqual(
            build_attention_queue(
                [],
                [],
                AnalyticalInputs(
                    drift_snapshots=[old_snapshot],
                    setup_progress=[],
                    attention_dismissals=dismissed,
                ),
            ),
            [],
        )

        newer_snapshot = DriftSnapshotNode(
            repository="repo-a",
            branch="main",
            actionableCount=1,
            checkedAt="2026-06-13T18:00:11+00:00",
        )
        queue = build_attention_queue(
            [],
            [],
            AnalyticalInputs(
                drift_snapshots=[newer_snapshot], setup_progress=[], attention_dismissals=dismissed
            ),
        )
        self.assertEqual([item.kind for item in queue], ["actionable-drift"])

    def test_newer_turn_end_supersedes_dismissal(self) -> None:
        # A fresh turn-end re-enters awaiting (a newer stateEnteredAt) and re-surfaces the
        # item despite an older dismissal -- a dismissal acknowledges THIS occurrence only.
        re_log = [
            _started(lifecycle_id="LC1"),
            _event(
                "lifecycle.awaiting-developer", lifecycle_id="LC1", ts=self.AWAIT_TS, summary="1"
            ),
            _event("lifecycle.resumed", lifecycle_id="LC1", ts="2026-06-13T18:00:07+00:00"),
            _event(
                "lifecycle.awaiting-developer",
                lifecycle_id="LC1",
                ts="2026-06-13T18:00:08+00:00",
                summary="2",
            ),
        ]
        queue = self._queue_for(
            [re_log],
            now=FRESH,
            dismissals=self._dismissal(
                "awaiting-developer:LC1",
                kind="awaiting-developer",
                dismissed_at="2026-06-13T18:00:06+00:00",
            ),
        )
        kinds = [i.kind for i in queue]
        self.assertIn("awaiting-developer", kinds)  # :08 signal supersedes the :06 dismissal
