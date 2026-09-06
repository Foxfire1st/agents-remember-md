from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Mapping

from agents_remember.errors import HarnessControlError
from agents_remember.serving.harness_control_bridge import HarnessControlBridge
from agents_remember.serving.harness_control_claude import (
    ClaudeAdapterLimits,
)
from test_harness_control_claude import (
    INTERRUPT_FIXTURE_ROOT,
    NOW,
    _adapter,
    _FakeClaudeTransport,
    _identity,
    _launch,
    _load_fixture,
    _replay,
    _result,
    _settle,
)


class ClaudeInterruptTests(unittest.IsolatedAsyncioTestCase):
    """Native stream-json interrupt, probe-locked on the installed claude 2.1.217 fixture."""

    @staticmethod
    def _interrupt_frames() -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (INTERRUPT_FIXTURE_ROOT / "interrupt.jsonl").read_text().splitlines()
        ]

    async def _active_turn(self, transport: _FakeClaudeTransport) -> HarnessControlBridge:
        adapter = _adapter(transport)
        bridge = HarnessControlBridge(_identity(), adapter, clock=lambda: NOW)
        await bridge.start(_launch())
        submission = asyncio.create_task(
            bridge.submissions().submit(
                bridge.prompt("write an essay", source="terminal", request_id="req-int-1")
            )
        )
        await transport.wait_for_writes(4)
        transport.feed(_replay(transport.writes[3]))
        receipt = await asyncio.wait_for(submission, timeout=1.0)
        assert receipt.acceptance == "immediate"
        self.assertEqual(bridge.snapshot().activity, "running")
        return bridge

    def _result_evidence(self, bridge: HarnessControlBridge) -> list[Mapping[str, object]]:
        return [
            frame.raw
            for frame in bridge.evidence().frames
            if frame.kind == "completed" and frame.raw.get("type") == "result"
        ]

    def _terminal_outcome(self, bridge: HarnessControlBridge) -> str:
        results = [entry for entry in bridge.transcript() if entry.role == "result"]
        self.assertEqual(len(results), 1)
        assert results[0].terminal_result is not None
        return results[0].terminal_result.outcome

    async def test_accepted_interrupt_settles_interrupted_not_failed(self) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        bridge = await self._active_turn(transport)
        try:
            epoch = bridge.submissions().bridge_epoch
            interrupt_task = asyncio.create_task(bridge.interrupt(epoch))
            await transport.wait_for_writes(5)
            self.assertEqual(
                transport.writes[4],
                {
                    "type": "control_request",
                    "request_id": "ar-claude-interrupt-1",
                    "request": {"subtype": "interrupt"},
                },
            )
            control_response, aborted, marker, result = self._interrupt_frames()
            transport.feed(control_response)
            acknowledgement = await asyncio.wait_for(interrupt_task, timeout=1.0)
            self.assertEqual(acknowledgement.acknowledgement, "accepted")
            self.assertEqual(acknowledgement.bridge_epoch, epoch)
            self.assertEqual(acknowledgement.vendor_correlation_id, "ar-claude-interrupt-1")
            assert acknowledgement.operation is not None
            self.assertEqual(acknowledgement.operation.kind, "prompt")
            self.assertEqual(
                acknowledgement.detail,
                "native interrupt acknowledged for the exact active Claude turn",
            )

            transport.feed(aborted)
            transport.feed(marker)
            transport.feed(result)
            await _settle()
            self.assertEqual(bridge.snapshot().activity, "idle")
            # The accepted-interrupt correlation, not the native error shape, settles the turn.
            self.assertEqual(self._terminal_outcome(bridge), "cancelled")
            evidence = self._result_evidence(bridge)
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["arTerminalOutcome"], "cancelled")
            self.assertEqual(evidence[0]["subtype"], "error_during_execution")
            self.assertIs(evidence[0]["is_error"], True)
        finally:
            await bridge.stop("forced")

    async def test_interrupt_replays_first_acknowledgement_without_a_second_write(self) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        bridge = await self._active_turn(transport)
        try:
            epoch = bridge.submissions().bridge_epoch
            interrupt_task = asyncio.create_task(bridge.interrupt(epoch))
            await transport.wait_for_writes(5)
            control_response, _, _, _ = self._interrupt_frames()
            transport.feed(control_response)
            first = await asyncio.wait_for(interrupt_task, timeout=1.0)
            replay = await bridge.interrupt(epoch)
            self.assertEqual(replay, first)
            self.assertEqual(len(transport.writes), 5)
        finally:
            await bridge.stop("forced")

    async def test_interrupt_guards_reject_before_any_native_write(self) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        bridge = await self._active_turn(transport)
        try:
            epoch = bridge.submissions().bridge_epoch
            with self.assertRaisesRegex(HarnessControlError, "does not accept turn identity"):
                await bridge.interrupt(epoch, turn_id="turn-1")
            with self.assertRaisesRegex(
                HarnessControlError, "does not match the active Claude operation"
            ):
                await bridge.interrupt(epoch, expected_operation_id="op-stale")
            self.assertEqual(len(transport.writes), 4)
        finally:
            await bridge.stop("forced")

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_harness_control_claude_interrupt.py:166).

    async def _accept_interrupt(
        self, bridge: HarnessControlBridge, transport: _FakeClaudeTransport
    ) -> None:
        """Drive one native interrupt to an ``accepted`` acknowledgement, no result fed yet."""

        epoch = bridge.submissions().bridge_epoch
        interrupt_task = asyncio.create_task(bridge.interrupt(epoch))
        await transport.wait_for_writes(5)
        control_response, _, _, _ = self._interrupt_frames()
        transport.feed(control_response)
        acknowledgement = await asyncio.wait_for(interrupt_task, timeout=1.0)
        self.assertEqual(acknowledgement.acknowledgement, "accepted")

    async def test_accepted_interrupt_racing_a_rate_limit_error_stays_failed(self) -> None:
        """An accepted interrupt does NOT relabel a 429 that races the accept window.

        m1: the accepted-interrupt remap is conjunctive — it fires only for the abort shape
        (``terminal_reason == "aborted_streaming"``). A genuine rate-limit whose result lands
        after the interrupt is accepted carries ``terminal_reason == "api_error"`` and must keep
        its ``failed`` meaning, never be reported as a clean user cut. Reverting the fix (an
        accepted interrupt remapping ANY error) settles this ``cancelled`` and fails the test.
        """
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        bridge = await self._active_turn(transport)
        try:
            await self._accept_interrupt(bridge, transport)
            # The rate-limit result RACES the accepted interrupt: subtype/is_error match the
            # abort shape, but terminal_reason is api_error, not aborted_streaming.
            transport.feed(
                {
                    **_result("usage limit reached"),
                    "is_error": True,
                    "terminal_reason": "api_error",
                    "api_error_status": 429,
                    "stop_reason": "stop_sequence",
                }
            )
            await _settle()
            self.assertEqual(self._terminal_outcome(bridge), "failed")
            evidence = self._result_evidence(bridge)
            self.assertEqual(evidence[0]["arTerminalOutcome"], "failed")
        finally:
            await bridge.stop("forced")

    async def test_natural_completion_after_an_accepted_interrupt_stays_completed(self) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        bridge = await self._active_turn(transport)
        try:
            epoch = bridge.submissions().bridge_epoch
            interrupt_task = asyncio.create_task(bridge.interrupt(epoch))
            await transport.wait_for_writes(5)
            control_response, _, _, _ = self._interrupt_frames()
            transport.feed(control_response)
            acknowledgement = await asyncio.wait_for(interrupt_task, timeout=1.0)
            self.assertEqual(acknowledgement.acknowledgement, "accepted")
            # The interrupt raced a natural completion: the native success keeps its meaning.
            transport.feed(_result("essay done"))
            await _settle()
            self.assertEqual(self._terminal_outcome(bridge), "completed")
            evidence = self._result_evidence(bridge)
            self.assertEqual(evidence[0]["arTerminalOutcome"], "completed")
        finally:
            await bridge.stop("forced")

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_harness_control_claude_interrupt.py:376).
    async def test_lost_acknowledgement_is_unknown_and_a_late_success_still_correlates(  # pragma: no cover
        self,
    ) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        adapter = _adapter(transport, limits=ClaudeAdapterLimits(acceptance_timeout_seconds=0.05))
        bridge = HarnessControlBridge(_identity(), adapter, clock=lambda: NOW)
        await bridge.start(_launch())
        submission = asyncio.create_task(
            bridge.submissions().submit(
                bridge.prompt("write an essay", source="terminal", request_id="req-int-1")
            )
        )
        try:
            await transport.wait_for_writes(4)
            transport.feed(_replay(transport.writes[3]))
            receipt = await asyncio.wait_for(submission, timeout=1.0)
            assert receipt.acceptance == "immediate"
            epoch = bridge.submissions().bridge_epoch
            # No control_response arrives inside the acknowledgement bound: the bytes were
            # sent, so the honest answer is unknown — never rejected, never accepted.
            acknowledgement = await bridge.interrupt(epoch)
            self.assertEqual(acknowledgement.acknowledgement, "unknown")
            self.assertEqual(len(transport.writes), 5)
            # A late success still records the correlation before settlement, so the turn's
            # error-shaped result settles interrupted even though the ack was lost first.
            control_response, aborted, marker, result = self._interrupt_frames()
            transport.feed(control_response)
            transport.feed(aborted)
            transport.feed(marker)
            transport.feed(result)
            await _settle()
            self.assertEqual(self._terminal_outcome(bridge), "cancelled")
            evidence = self._result_evidence(bridge)
            self.assertEqual(evidence[0]["arTerminalOutcome"], "cancelled")
        finally:
            if not submission.done():
                submission.cancel()
                await asyncio.gather(submission, return_exceptions=True)
            await bridge.stop("forced")
