from __future__ import annotations

from agents_remember.application import gate_tools as gates
from agents_remember.application.gate_tools import GateWait
from agents_remember.controlplane.operator_inbox_records import (
    InboxAddress,
    InboxMessage,
    InboxPoster,
    InboxRouting,
    create_operator_inbox_entry,
)
from agents_remember.controlplane.records import GateAnchor, GateVerdict, create_gate
from test_controlplane_gates import T1, T2, GateToolTests


class GateToolTests2(GateToolTests):
    def test_cancel_deletes_gate_and_pending_inbox_entries(self) -> None:
        gate_id = self._create("agent-question")
        self.inbox.append(
            create_operator_inbox_entry(
                InboxMessage(ask="Continue?", response="Never mind.", gate_id=gate_id),
                entry_id="I1",
                now=T2,
                routing=InboxRouting(address=InboxAddress(lifecycle_id="L1", agent_id=None)),
                poster=InboxPoster(created_by="developer", created_via="dashboard"),
            )
        )

        decided = gates.gate_decide_tool(
            None,  # type: ignore[arg-type]
            gate_id=gate_id,
            lifecycle_id="L1",
            verdict=GateVerdict(decision="cancel", by="developer", via="dashboard"),
        )

        self.assertEqual(decided["state"], "cancelled")
        self.assertNotIn(gate_id, self.store.current("L1"))
        self.assertEqual(self.inbox.read(), [])

    def test_wait_times_out_while_open(self) -> None:
        gate_id = self._create("agent-question")
        clock = iter([0.0, 0.0, 99.0])  # deadline calc, first check, past-deadline check
        result = gates.gate_wait_tool(
            None,  # type: ignore[arg-type]
            gate_id=gate_id,
            lifecycle_id="L1",
            wait=GateWait(
                timeout_seconds=10.0, sleep=lambda _s: None, monotonic=lambda: next(clock)
            ),
        )
        self.assertTrue(result["timedOut"])
        self.assertEqual(result["state"], "open")

    def test_response_wait_returns_matching_inbox_entry_without_consuming(self) -> None:
        gate_id = self._create("agent-question")
        self.inbox.append(
            create_operator_inbox_entry(
                InboxMessage(
                    ask="Continue?", response="Use a clearer commit message first.", gate_id=gate_id
                ),
                entry_id="I1",
                now=T2,
                routing=InboxRouting(address=InboxAddress(lifecycle_id="L1", agent_id=None)),
                poster=InboxPoster(created_by="developer", created_via="dashboard"),
            )
        )

        result = gates.gate_response_wait_tool(
            None,  # type: ignore[arg-type]
            gate_id=gate_id,
            lifecycle_id="L1",
            wait=GateWait(timeout_seconds=10.0, sleep=lambda _s: None),
        )

        self.assertFalse(result["timedOut"])
        self.assertEqual(result["state"], "open")
        self.assertEqual(result["entryCount"], 1)
        self.assertEqual(result["entries"][0]["response"], "Use a clearer commit message first.")
        self.assertEqual(len(self.inbox.list_pending(lifecycle_id="L1", agent_id=None)), 1)

    def test_decide_for_lifecycle_rejects_stale_expected_gate(self) -> None:
        self.store.append(
            create_gate("agent-question", gate_id="A", now=T1, anchor=GateAnchor(lifecycle_id="L1"))
        )
        self.store.append(
            create_gate(
                "closeout-approval", gate_id="B", now=T2, anchor=GateAnchor(lifecycle_id="L1")
            )
        )
        with self.assertRaisesRegex(KeyError, "current open gate"):
            gates.gate_decide_for_lifecycle_tool(
                None,  # type: ignore[arg-type]
                lifecycle_id="L1",
                expected_gate_id="A",
                verdict=GateVerdict(decision="approve", by="developer", via="dashboard"),
            )
