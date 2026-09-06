from __future__ import annotations

import logging
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.controlplane.operator_inbox_records import (
    InboxAddress,
    InboxMessage,
    InboxPoster,
    InboxRouting,
    create_operator_inbox_entry,
)
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.controlplane.records import GateRecord, GateVerdict, decide_gate
from agents_remember.controlplane.store import GateStore
from agents_remember.errors import HarnessControlClientError, HarnessControlError
from agents_remember.models.conversations.control_wire import (
    AdapterSnapshot,
    ControlIdentity,
    PendingInteraction,
)
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
)
from agents_remember.serving.hosted_interactions import (
    INTERACTION_DELIVERY_ATTEMPT_LIMIT,
    HostedInteractionSynchronizer,
)

NOW = "2026-07-14T10:00:00+00:00"


def _entry(root: Path) -> TerminalCatalogEntry:
    return TerminalCatalogEntry(
        id="worker-1",
        label="Worker",
        kind="harness",
        harness="codex",
        lifecycle_id="L1",
        cwd=root,
        tmux_name="ar-worker-1",
        command=("codex",),
        created_at=NOW,
        last_attached_at=NOW,
        status="running",
        control_state="ready",
        control_endpoint=root / "control.sock",
        control_protocol="ar-harness-control/v1",
    )


def _snapshot(entry: TerminalCatalogEntry) -> AdapterSnapshot:
    return AdapterSnapshot(
        identity=ControlIdentity(entry.id, entry.tmux_name, entry.created_at),
        control="ready",
        activity="blocked",
        acceptance="rejected",
        pending_interaction=PendingInteraction(
            interaction_id="approval-1",
            kind="approval",
            prompt="Allow this action?",
            created_at=NOW,
            choices=("allow", "deny"),
        ),
    )


def test_pending_interaction_round_trips_through_durable_gate(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    synchronizer = HostedInteractionSynchronizer(tmp_path)
    synchronizer.observe(entry, _snapshot(entry))
    store = GateStore(tmp_path)
    gate = next(iter(store.current("L1").values()))
    assert gate.kind == "agent-question"
    assert gate.packet["adapterInteraction"]["interactionId"] == "approval-1"

    store.append(
        decide_gate(
            gate,
            GateVerdict(decision="approve", by="developer", via="dashboard", note="allow"),
            now=NOW,
        )
    )
    with mock.patch(
        "agents_remember.serving.hosted_interactions.respond_control_interaction"
    ) as respond:
        synchronizer.observe(entry, _snapshot(entry))
    respond.assert_called_once_with(entry, interaction_id="approval-1", response="allow")
    assert store.current("L1")[gate.id].state == "applied"


def test_failed_respond_reopens_the_gate_instead_of_silently_swallowing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _entry(tmp_path)
    synchronizer = HostedInteractionSynchronizer(tmp_path)
    store = GateStore(tmp_path)
    synchronizer.observe(entry, _snapshot(entry))
    gate = next(iter(store.current("L1").values()))
    store.append(
        decide_gate(
            gate,
            GateVerdict(decision="approve", by="developer", via="dashboard", note="allow"),
            now=NOW,
        )
    )
    with (
        mock.patch(
            "agents_remember.serving.hosted_interactions.respond_control_interaction",
            side_effect=HarnessControlError("control endpoint unavailable"),
        ) as respond,
        caplog.at_level(logging.WARNING, logger="agents_remember.serving.hosted_interactions"),
    ):
        synchronizer.observe(entry, _snapshot(entry))
    respond.assert_called_once_with(entry, interaction_id="approval-1", response="allow")
    reopened = store.current("L1")[gate.id]
    assert reopened.state == "open"
    assert reopened.decisionNote is None
    assert "reopening the gate" in caplog.text
    # A control error with no may_have_sent certificate cannot prove the answer stayed home, so
    # the gate is handed back with the delivery recorded as unknown -- never retried behind the
    # operator's back, never silently stripped of what they decided.
    failure = reopened.packet["adapterDecisionFailure"]
    assert failure["delivery"] == "unknown"
    assert failure["decisionNote"] == "allow"
    assert failure["decidedBy"] == "developer"


def _decided_gate(
    store: GateStore,
    synchronizer: HostedInteractionSynchronizer,
    entry: TerminalCatalogEntry,
    *,
    note: str,
) -> GateRecord:
    """Raise the interaction gate and decide it, returning the freshly decided snapshot."""
    synchronizer.observe(entry, _snapshot(entry))
    gate = next(
        candidate for candidate in store.current("L1").values() if candidate.state == "open"
    )
    decided = decide_gate(
        gate, GateVerdict(decision="approve", by="developer", via="dashboard", note=note), now=NOW
    )
    store.append(decided)
    return decided


def test_pre_write_failure_keeps_the_decision_and_retries_until_the_budget_runs_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure that certifies zero bytes were sent must cost a sweep, not the decision."""
    entry = _entry(tmp_path)
    synchronizer = HostedInteractionSynchronizer(tmp_path)
    store = GateStore(tmp_path)
    gate = _decided_gate(store, synchronizer, entry, note="allow")
    with (
        mock.patch(
            "agents_remember.serving.hosted_interactions.respond_control_interaction",
            side_effect=HarnessControlClientError(
                "control endpoint unavailable before write", may_have_sent=False
            ),
        ) as respond,
        caplog.at_level(logging.WARNING, logger="agents_remember.serving.hosted_interactions"),
    ):
        synchronizer.observe(entry, _snapshot(entry))
        first = store.current("L1")[gate.id]
        assert first.state == "approved"
        assert first.decisionNote == "allow"
        assert first.packet["adapterDecisionFailure"]["attempts"] == 1

        # The gate is still decided, so the next liveness sweep re-offers the SAME decision.
        synchronizer.observe(entry, _snapshot(entry))
        assert respond.call_count == 2
        assert store.current("L1")[gate.id].state == "approved"

        synchronizer.observe(entry, _snapshot(entry))
        assert respond.call_count == INTERACTION_DELIVERY_ATTEMPT_LIMIT

        # Budget spent: the operator is told rather than left with a gate that looks decided.
        exhausted = store.current("L1")[gate.id]
        assert exhausted.state == "open"
        assert exhausted.decisionNote is None
        assert exhausted.packet["adapterDecisionFailure"] == {
            "delivery": "not-sent",
            "attempts": INTERACTION_DELIVERY_ATTEMPT_LIMIT,
            "reason": "control endpoint unavailable before write",
            "observedAt": mock.ANY,
            "decision": "approved",
            "decidedAt": NOW,
            "decidedBy": "developer",
            "decidedVia": "dashboard",
            "decisionNote": "allow",
        }

        # And an open gate is never re-offered: only a fresh decision may be delivered.
        synchronizer.observe(entry, _snapshot(entry))
        assert respond.call_count == INTERACTION_DELIVERY_ATTEMPT_LIMIT
    assert "retrying on the next sweep" in caplog.text


def test_post_write_failure_hands_the_decision_back_without_re_answering(
    tmp_path: Path,
) -> None:
    """A respond that may already have reached the vendor must never be sent twice."""
    entry = _entry(tmp_path)
    synchronizer = HostedInteractionSynchronizer(tmp_path)
    store = GateStore(tmp_path)
    gate = _decided_gate(store, synchronizer, entry, note="allow")
    with mock.patch(
        "agents_remember.serving.hosted_interactions.respond_control_interaction",
        side_effect=HarnessControlClientError(
            "control endpoint unavailable after request bytes were sent", may_have_sent=True
        ),
    ) as respond:
        synchronizer.observe(entry, _snapshot(entry))
        reopened = store.current("L1")[gate.id]
        assert reopened.state == "open"
        assert reopened.decidedBy is None
        assert reopened.packet["adapterDecisionFailure"]["delivery"] == "unknown"
        assert reopened.packet["adapterDecisionFailure"]["decisionNote"] == "allow"
        assert reopened.packet["adapterInteraction"]["interactionId"] == "approval-1"

        synchronizer.observe(entry, _snapshot(entry))
    assert respond.call_count == 1


def test_null_request_id_completion_rejects_ambiguous_vendor_correlation(
    tmp_path: Path,
) -> None:
    entry = _entry(tmp_path)
    inbox = OperatorInboxStore(tmp_path)
    for entry_id in ("inbox-1", "inbox-2"):
        inbox.append(
            create_operator_inbox_entry(
                InboxMessage(ask="Continue", response="Please continue"),
                entry_id=entry_id,
                now=NOW,
                routing=InboxRouting(address=InboxAddress(lifecycle_id="L1", agent_id="worker-1")),
                poster=InboxPoster(created_by="manager-1", created_via="cli"),
            ).model_copy(
                update={
                    "deliveryState": "delivered",
                    "deliveredToSession": "worker-1",
                    "adapterDeliveryState": "accepted",
                    "adapterRequestId": entry_id,
                    "adapterVendorCorrelationId": "vendor-1",
                }
            )
        )
    transcript = (
        {
            "sequence": 1,
            "role": "result",
            "text": "done",
            "createdAt": NOW,
            "requestId": None,
            "vendorCorrelationId": "vendor-1",
            "terminalResult": {"outcome": "completed", "completedAt": NOW},
        },
    )
    with (
        mock.patch(
            "agents_remember.serving.hosted_interactions.read_control_transcript",
            return_value=transcript,
        ),
        pytest.raises(HarnessControlError, match="matches multiple inbox rows"),
    ):
        HostedInteractionSynchronizer(tmp_path).observe(
            entry,
            AdapterSnapshot(
                identity=ControlIdentity(entry.id, entry.tmux_name, entry.created_at),
                control="ready",
                activity="settling",
                acceptance="queued",
            ),
        )
    assert {row.adapterDeliveryState for row in inbox.current().values()} == {"accepted"}
