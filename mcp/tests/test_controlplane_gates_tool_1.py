from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from agents_remember.application import gate_tools as gates
from agents_remember.application.gate_tools import GateRaise, GateWait
from agents_remember.controlplane.records import GateAnchor, GateVerdict, decide_gate
from test_controlplane_gates import (
    MANAGER_CLOSEOUT_POLICY,
    MANAGER_CLOSEOUT_WITH_VERDICT_POLICY,
    T2,
    GateToolTests,
)


class GateToolTests1(GateToolTests):
    def test_create_then_decide_records_attribution(self) -> None:
        gate_id = self._create()
        decided = gates.gate_decide_tool(
            None,  # type: ignore[arg-type]
            gate_id=gate_id,
            lifecycle_id="L1",
            verdict=GateVerdict(decision="approve", by="model", via="cli"),
        )
        self.assertEqual(decided["state"], "approved")
        self.assertEqual(decided["decidedBy"], "model")
        self.assertEqual(decided["decidedVia"], "cli")

    def test_orchestration_decision_rejects_owner_self_approval(self) -> None:
        gate_id = self._create()
        config = SimpleNamespace(orchestration=SimpleNamespace(gate_policy=MANAGER_CLOSEOUT_POLICY))

        with self.assertRaisesRegex(ValueError, "owning lifecycle"):
            gates.gate_decide_tool(
                config,  # type: ignore[arg-type]
                gate_id=gate_id,
                lifecycle_id="L1",
                verdict=GateVerdict(
                    decision="approve", by="L1", via="orchestration", deciding_role="manager"
                ),
            )

        self.assertEqual(self.store.current("L1")[gate_id].state, "open")

    def test_orchestration_decision_requires_verdict_when_policy_requires_it(self) -> None:
        gate_id = self._create()
        config = SimpleNamespace(
            orchestration=SimpleNamespace(gate_policy=MANAGER_CLOSEOUT_WITH_VERDICT_POLICY)
        )

        with self.assertRaisesRegex(ValueError, "requires reviewer verdict evidence"):
            gates.gate_decide_tool(
                config,  # type: ignore[arg-type]
                gate_id=gate_id,
                lifecycle_id="L1",
                verdict=GateVerdict(
                    decision="approve", by="L-manager", via="orchestration", deciding_role="manager"
                ),
            )

        self.assertEqual(self.store.current("L1")[gate_id].state, "open")

    def test_create_expires_previous_open_lifecycle_gate(self) -> None:
        first = self._create("agent-question")
        second = self._create("closeout-approval")
        current = self.store.current("L1")
        self.assertEqual(current[first].state, "expired")
        self.assertEqual(current[second].state, "open")

        result = gates.gate_wait_tool(
            None,  # type: ignore[arg-type]
            gate_id=first,
            lifecycle_id="L1",
            wait=GateWait(timeout_seconds=30.0, poll_seconds=1.0, sleep=lambda _s: None),
        )
        self.assertFalse(result["timedOut"])
        self.assertEqual(result["state"], "expired")

    def test_lifecycle_gate_default_returns_after_developer_decision(self) -> None:
        blocked = SimpleNamespace(id="L1", state="blocked", phase="build")
        lifecycle = SimpleNamespace(
            current=SimpleNamespace(id="L1", state="running"),
            block=mock.Mock(return_value=blocked),
        )

        def approve_on_sleep(_seconds: float) -> None:
            open_gates = [
                gate for gate in self.store.current("L1").values() if gate.state == "open"
            ]
            self.assertEqual(len(open_gates), 1)
            self.store.append(
                decide_gate(
                    open_gates[0],
                    GateVerdict(
                        decision="approve",
                        by="developer",
                        via="dashboard",
                        note="approved from dashboard",
                    ),
                    now=T2,
                )
            )

        with mock.patch.object(gates, "require_ambient", return_value=lifecycle):
            result = gates.lifecycle_gate_tool(
                None,  # type: ignore[arg-type]
                GateRaise(
                    kind="plan-approval",
                    anchor=GateAnchor(lifecycle_id="L1"),
                    ask={"kind": "decision", "prompt": "Approve?", "options": ["approve"]},
                ),
                wait=GateWait(timeout_seconds=None, sleep=approve_on_sleep),
            )

        self.assertEqual(result["gate"]["state"], "approved")
        self.assertFalse(result["wait"]["timedOut"])
        self.assertEqual(result["wait"]["state"], "approved")
        self.assertEqual(result["wait"]["decidedBy"], "developer")
        self.assertEqual(result["wait"]["decisionNote"], "approved from dashboard")

    def test_lifecycle_gate_rejects_explicit_lifecycle_mismatch(self) -> None:
        lifecycle = SimpleNamespace(current=SimpleNamespace(id="L1", state="running"))

        with (
            mock.patch.object(gates, "require_ambient", return_value=lifecycle),
            self.assertRaisesRegex(Exception, "does not match active lifecycle"),
        ):
            gates.lifecycle_gate_tool(
                None,  # type: ignore[arg-type]
                GateRaise(kind="plan-approval", anchor=GateAnchor(lifecycle_id="other")),
            )

        self.assertEqual(self.store.current("L1"), {})
