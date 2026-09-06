from __future__ import annotations

import asyncio
import unittest

from agents_remember.serving.harness_control_bridge import HarnessControlBridge
from agents_remember.serving.harness_control_claude import (
    ClaudeAdapterLimits,
)
from test_harness_control_claude import (
    NOW,
    _adapter,
    _FakeClaudeTransport,
    _identity,
    _launch,
    _load_fixture,
    _replay,
    _result,
    _set_model,
    _settle,
    _wire_text,
)


class ClaudeStreamJsonAdapterTests2(unittest.IsolatedAsyncioTestCase):
    async def test_model_and_effort_set_require_terminal_echo_and_update_model_gate(self) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        adapter = _adapter(
            transport,
            correlations=["set-haiku", "set-sonnet", "set-low"],
        )
        bridge = HarnessControlBridge(_identity(), adapter, clock=lambda: NOW)
        await bridge.start(_launch())
        try:
            model_task = asyncio.create_task(bridge.submissions().set_model("haiku"))
            await transport.wait_for_writes(4)
            self.assertEqual(_wire_text(transport.writes[3]), "/model haiku")
            transport.feed(_replay(transport.writes[3]))
            transport.feed(_result("Set model to Haiku for this session only"))
            model = await asyncio.wait_for(model_task, timeout=1.0)
            self.assertEqual(
                (model.ok, model.acceptance, model.effective_value),
                (True, "echo-verified", "haiku"),
            )
            self.assertEqual(adapter.advertise().selected_model_key, "haiku")

            write_count = len(transport.writes)
            refused_effort = await bridge.submissions().set_effort("low")
            self.assertEqual(
                (refused_effort.ok, refused_effort.acceptance),
                (False, "unsupported"),
            )
            self.assertEqual(len(transport.writes), write_count)

            sonnet_task = asyncio.create_task(bridge.submissions().set_model("sonnet"))
            await transport.wait_for_writes(5)
            transport.feed(_replay(transport.writes[4]))
            transport.feed(_result("Set model to Sonnet for this session only"))
            self.assertEqual(
                (await asyncio.wait_for(sonnet_task, timeout=1.0)).acceptance,
                "echo-verified",
            )

            effort_task = asyncio.create_task(bridge.submissions().set_effort("low"))
            await transport.wait_for_writes(6)
            self.assertEqual(_wire_text(transport.writes[5]), "/effort low")
            transport.feed(_replay(transport.writes[5]))
            transport.feed(
                _result("Set effort level to low (this session only): Quick implementation")
            )
            effort = await asyncio.wait_for(effort_task, timeout=1.0)
            self.assertEqual(
                (effort.ok, effort.acceptance, effort.effective_value),
                (True, "echo-verified", "low"),
            )
            self.assertEqual(adapter.advertise().selected_effort, "low")
        finally:
            await bridge.stop("forced")

    async def test_set_timeout_neutralizes_late_replay_before_a_clean_retry(self) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        adapter = _adapter(
            transport,
            correlations=["expired-correlation", "retry-correlation"],
            # Keep the production 30-second bound compressed while leaving enough event-loop
            # budget for the fake reader to consume replay + result under a loaded xdist worker.
            limits=ClaudeAdapterLimits(acceptance_timeout_seconds=0.05),
        )
        await adapter.start(_launch())
        try:
            expired = await _set_model(adapter, "haiku")
            self.assertEqual((expired.ok, expired.acceptance), (False, "unknown"))
            expired_frame = transport.writes[3]

            blocked = await _set_model(adapter, "haiku")
            self.assertEqual((blocked.ok, blocked.acceptance), (False, "unknown"))
            self.assertEqual(len(transport.writes), 4)

            transport.feed(_replay(expired_frame))
            transport.feed(_result("Set model to Haiku for this session only"))
            await _settle()
            self.assertEqual(adapter.advertise().selected_model_key, "sonnet")

            transport.feed(_replay(expired_frame))
            await _settle()

            retry_task = asyncio.create_task(_set_model(adapter, "haiku"))
            await transport.wait_for_writes(5)
            transport.feed(_replay(transport.writes[4]))
            transport.feed(_result("Set model to Haiku for this session only"))
            retry = await asyncio.wait_for(retry_task, timeout=1.0)
            self.assertEqual(retry.acceptance, "echo-verified")
        finally:
            await adapter.stop("forced")

    async def test_disconnect_reconciliation_stays_unknown_and_never_resends(self) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        adapter = _adapter(transport)
        bridge = HarnessControlBridge(_identity(), adapter, clock=lambda: NOW)
        await bridge.start(_launch())
        submission = asyncio.create_task(
            bridge.submissions().submit(
                bridge.prompt("ambiguous", source="durable", request_id="ambiguous")
            )
        )
        try:
            await transport.wait_for_writes(4)
            transport.disconnect()
            receipt = await asyncio.wait_for(submission, timeout=1.0)
            self.assertEqual(receipt.acceptance, "unknown")
            await _settle()
            self.assertEqual(bridge.snapshot().control, "disconnected")
            write_count = len(transport.writes)
            reconciliation = await bridge.submissions().reconcile("ambiguous")
            self.assertEqual(reconciliation.state, "unresolved")
            self.assertIn("was not resent", reconciliation.detail or "")
            self.assertEqual(len(transport.writes), write_count)
            blocked = await bridge.submissions().submit(
                bridge.prompt("must not resend", source="durable", request_id="after-exit")
            )
            self.assertEqual(blocked.acceptance, "queued")
            self.assertEqual(len(transport.writes), write_count)
        finally:
            await bridge.stop("forced")

    async def test_late_replay_reconciles_unknown_from_structured_history_without_resend(
        self,
    ) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        adapter = _adapter(
            transport,
            limits=ClaudeAdapterLimits(acceptance_timeout_seconds=0.001),
        )
        bridge = HarnessControlBridge(_identity(), adapter, clock=lambda: NOW)
        await bridge.start(_launch())
        try:
            receipt = await bridge.submissions().submit(
                bridge.prompt("late replay", source="durable", request_id="late")
            )
            self.assertEqual(receipt.acceptance, "unknown")
            write_count = len(transport.writes)

            transport.feed(_replay(transport.writes[-1]))
            await _settle()
            reconciliation = await bridge.submissions().reconcile("late")
            self.assertEqual(reconciliation.state, "accepted")
            self.assertIn("replay-user-message", reconciliation.detail or "")
            self.assertEqual(len(transport.writes), write_count)
        finally:
            await bridge.stop("forced")

    async def test_nonzero_process_exit_maps_to_failed(self) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        adapter = _adapter(transport)
        bridge = HarnessControlBridge(_identity(), adapter)
        await bridge.start(_launch())
        try:
            transport.disconnect(returncode=7)
            await _settle()
            self.assertEqual(bridge.snapshot().control, "failed")
            self.assertIn("status 7", str(bridge.snapshot().raw["disconnect"]))
        finally:
            await bridge.stop("forced")

    async def test_forced_stop_reclaims_a_reader_blocked_by_full_event_queue(self) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        adapter = _adapter(
            transport,
            limits=ClaudeAdapterLimits(event_queue_limit=1),
        )
        await adapter.start(_launch())
        transport.feed({"type": "notification", "subtype": "first"})
        transport.feed({"type": "notification", "subtype": "second"})
        await _settle()
        await asyncio.wait_for(adapter.stop("forced"), timeout=1.0)
