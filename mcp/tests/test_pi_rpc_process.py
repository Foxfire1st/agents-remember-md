"""Deterministic child-process coverage for Pi RPC JSONL transport behavior."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

from agents_remember.errors import HarnessAdapterDisconnectedError
from agents_remember.models.conversations.control_wire import (
    ControlIdentity,
    LaunchSpec,
)
from agents_remember.serving.pi_rpc_process import PiRpcSubprocess


def _child_launch(script: str) -> LaunchSpec:
    return LaunchSpec(
        identity=ControlIdentity(
            ar_session_id="pi-process-test",
            tmux_name="pi-process-test",
            created_at="2026-07-14T09:00:00+00:00",
        ),
        harness_id="pi-test-child",
        cwd=Path.cwd(),
        argv=(sys.executable, "-u", "-c", script),
    )


class PiRpcSubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_correlates_response_streams_event_and_stops_cleanly(self) -> None:
        script = """
import json
import sys

sys.stderr.write("child-ready")
sys.stderr.flush()
for line in sys.stdin:
    command = json.loads(line)
    if command["type"] == "ping":
        print(json.dumps({"type": "queue_update", "steering": [], "followUp": []}))
        print(json.dumps({
            "type": "response",
            "id": command["id"],
            "command": "ping",
            "success": True,
        }))
        sys.stdout.flush()
"""
        transport = PiRpcSubprocess()
        await transport.start(_child_launch(script))
        events = transport.events()
        try:
            response = await transport.request({"id": "ping-1", "type": "ping"})
            self.assertEqual(response["id"], "ping-1")
            self.assertEqual(response["command"], "ping")
            event = await anext(events)
            self.assertEqual(event["type"], "queue_update")
            self.assertIn("child-ready", transport.stderr_tail)
        finally:
            await transport.stop("graceful")
        with self.assertRaises(HarnessAdapterDisconnectedError):
            await anext(events)

    async def test_eof_during_correlated_request_is_ambiguous(self) -> None:
        script = """
import sys

sys.stdin.readline()
"""
        transport = PiRpcSubprocess()
        await transport.start(_child_launch(script))
        try:
            with self.assertRaises(HarnessAdapterDisconnectedError) as raised:
                await transport.request({"id": "lost-1", "type": "prompt"})
            self.assertTrue(raised.exception.may_have_sent)
            self.assertEqual(raised.exception.vendor_correlation_id, "lost-1")
        finally:
            await transport.stop("forced")

    async def test_cancelled_request_ignores_late_response_and_next_reader_survives(self) -> None:
        script = """
import json
import sys

first = json.loads(sys.stdin.readline())
second = json.loads(sys.stdin.readline())
for command in (first, second):
    print(json.dumps({
        "type": "response",
        "id": command["id"],
        "command": command["type"],
        "success": True,
    }), flush=True)
"""
        transport = PiRpcSubprocess()
        await transport.start(_child_launch(script))
        try:
            cancelled = asyncio.create_task(
                transport.request({"id": "cancelled-1", "type": "slow"})
            )
            await asyncio.sleep(0.01)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            response = await transport.request({"id": "next-1", "type": "ping"})
            self.assertEqual(response["id"], "next-1")
        finally:
            await transport.stop("forced")


if __name__ == "__main__":
    unittest.main()
