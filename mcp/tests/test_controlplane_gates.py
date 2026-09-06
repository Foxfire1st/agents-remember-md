"""Tests for the gate control-plane substrate (slice 6a)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents_remember.application import gate_tools as gates
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.controlplane.records import (
    GateAnchor,
    GateRequest,
    GateVerdict,
    create_gate,
    decide_gate,
)
from agents_remember.controlplane.store import GateStore
from agents_remember.kernel.primitives.gate_policy import (
    GatePolicyRule,
    make_gate_policy,
)

T1 = "2026-06-18T10:00:00+00:00"
T2 = "2026-06-18T10:05:00+00:00"
MANAGER_CLOSEOUT_POLICY = make_gate_policy(
    [GatePolicyRule(kind="closeout-approval", delegated_role="manager")]
)
MANAGER_CLOSEOUT_WITH_VERDICT_POLICY = make_gate_policy(
    [
        GatePolicyRule(
            kind="closeout-approval",
            delegated_role="manager",
            require_reviewer_verdict=True,
        )
    ]
)


class GateRecordTests(unittest.TestCase):
    def test_create_and_decide_are_pure_snapshots(self) -> None:
        gate = create_gate(
            "closeout-approval",
            gate_id="01H",
            now=T1,
            anchor=GateAnchor(lifecycle_id="L1"),
            request=GateRequest(packet={"paths": 3}),
        )
        self.assertEqual(gate.state, "open")
        decided = decide_gate(
            gate,
            GateVerdict(decision="approve", by="developer", via="dashboard", note="lgtm"),
            now=T2,
        )
        self.assertEqual(decided.id, gate.id)  # same gate id
        self.assertEqual(decided.ts, T2)  # new snapshot time
        self.assertEqual(decided.state, "approved")
        self.assertEqual(decided.decidedBy, "developer")
        self.assertEqual(decided.decidedVia, "dashboard")
        self.assertEqual(gate.state, "open")  # original snapshot untouched


class GateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_append_keeps_history_and_current_folds_last_wins(self) -> None:
        store = GateStore(self.root)
        gate = create_gate(
            "closeout-approval", gate_id="01H", now=T1, anchor=GateAnchor(lifecycle_id="L1")
        )
        store.append(gate)
        store.append(
            decide_gate(
                gate,
                GateVerdict(decision="approve", by="developer", via="dashboard", note=None),
                now=T2,
            )
        )
        self.assertEqual(len(store.read("L1")), 2)  # history preserved
        self.assertEqual(store.current("L1")["01H"].state, "approved")  # last-wins


class GateToolTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = GateStore(self.root)
        self.inbox = OperatorInboxStore(self.root)
        patcher = mock.patch.object(gates, "_store", return_value=self.store)
        self.addCleanup(patcher.stop)
        patcher.start()
        inbox_patcher = mock.patch.object(gates, "_inbox_store", return_value=self.inbox)
        self.addCleanup(inbox_patcher.stop)
        inbox_patcher.start()

    def _create(self, kind: str = "closeout-approval") -> str:
        created = gates.gate_create_tool(
            None,  # type: ignore[arg-type]  # the store is patched; config is unused here
            kind=kind,
            anchor=GateAnchor(lifecycle_id="L1"),
        )
        self.assertEqual(created["state"], "open")
        return created["gateId"]
