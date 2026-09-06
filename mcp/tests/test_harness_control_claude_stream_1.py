from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from agents_remember.errors import HarnessControlError
from agents_remember.serving.harness_control_bridge import HarnessControlBridge
from agents_remember.serving.harness_control_models import (
    InteractionResponse,
)
from agents_remember.serving.harness_launch import ResolvedLaunch
from test_harness_control_claude import (
    NOW,
    SESSION_ID,
    _adapter,
    _FakeClaudeTransport,
    _identity,
    _launch,
    _load_fixture,
    _replay,
    _result,
    _settle,
    _wait_for_activity,
)


class ClaudeStreamJsonAdapterTests1(unittest.IsolatedAsyncioTestCase):
    async def test_discover_uses_only_token_free_bootstrap_and_list_models(self) -> None:
        fixture_frames = _load_fixture("initialization.jsonl")
        transport = _FakeClaudeTransport(fixture_frames)
        adapter = _adapter(transport)

        advertised = await adapter.discover(_launch())

        self.assertEqual(advertised.selected_model_key, "sonnet")
        user_frames = [frame for frame in transport.writes if frame["type"] == "user"]
        self.assertEqual(len(user_frames), 1)
        self.assertEqual(user_frames[0]["shouldQuery"], False)
        bootstrap_result = next(frame for frame in fixture_frames if frame["type"] == "result")
        self.assertEqual(bootstrap_result["num_turns"], 0)
        self.assertEqual(bootstrap_result["total_cost_usd"], 0)
        assert transport.argv is not None
        self.assertEqual(transport.argv[:9], _launch().argv)
        self.assertEqual(
            transport.argv[9:12],
            ("--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"),
        )
        self.assertEqual(transport.stop_modes, ["forced"])

    async def test_launch_preserves_arguments_environment_and_requires_structured_init(
        self,
    ) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        adapter = _adapter(transport)

        handshake = await adapter.start(_launch())
        try:
            self.assertEqual(handshake.snapshot.control, "ready")
            self.assertEqual(handshake.snapshot.vendor_session_id, SESSION_ID)
            self.assertEqual(handshake.raw["claudeCodeVersion"], "2.1.210")
            assert transport.argv is not None
            self.assertEqual(
                transport.argv[:9],
                _launch().argv,
            )
            for required in (
                "-p",
                "--input-format",
                "--output-format",
                "--verbose",
                "--replay-user-messages",
                "--permission-prompt-tool",
            ):
                self.assertIn(required, transport.argv)
            # 2.1.210 is below the probed floor, so the flag
            # is omitted fail-closed — one launch, no re-launch, and the exact reason.
            self.assertNotIn("--forward-subagent-text", transport.argv)
            self.assertEqual(len(transport.start_argvs), 1)
            note = str(handshake.snapshot.raw["subagentTextForwarding"])
            self.assertIn("unverified", note)
            self.assertIn("2.1.210", note)
            self.assertIn("2.1.220", note)
            self.assertNotIn("--mcp-config", transport.argv)
            self.assertNotIn("--strict-mcp-config", transport.argv)
            self.assertEqual(transport.env, _launch().env)
            self.assertNotIn("AUTH_TOKEN_FOR_TEST", json.dumps(handshake.raw))
            self.assertEqual(
                [frame["type"] for frame in transport.writes],
                ["control_request", "user", "control_request"],
            )
            self.assertEqual(transport.writes[1]["shouldQuery"], False)
            list_request = transport.writes[2]
            self.assertEqual(list_request["request"], {"subtype": "list_models"})
            advertised = adapter.advertise()
            self.assertEqual(advertised.selected_model_key, "sonnet")
            self.assertIsNone(advertised.selected_effort)
            self.assertEqual(
                [option.key for option in advertised.models[0].effort_options],
                ["low", "medium", "high", "xhigh", "max"],
            )
            self.assertEqual(advertised.models[1].effort_options, ())
            self.assertFalse(advertised.models[2].selectable)
            self.assertEqual(len(transport.writes), 3)
        finally:
            await adapter.stop("forced")

    async def test_missing_protocol_capability_fails_loudly(self) -> None:
        frames = _load_fixture("initialization.jsonl")
        response = frames[0]["response"]
        assert isinstance(response, dict)
        payload = response["response"]
        assert isinstance(payload, dict)
        payload.pop("commands")
        incompatible_transport = _FakeClaudeTransport(frames)
        incompatible = _adapter(incompatible_transport)
        handshake = await incompatible.start(_launch())
        self.assertEqual(handshake.snapshot.control, "unsupported")
        self.assertEqual(incompatible_transport.stop_modes, ["forced"])
        self.assertIn("command capabilities", str(handshake.raw["detail"]))

    async def test_expected_launch_model_mismatch_closes_and_propagates_as_failure(self) -> None:
        frames = _load_fixture("initialization.jsonl")
        system_init = frames[1]
        system_init["model"] = "haiku"
        transport = _FakeClaudeTransport(frames)
        adapter = _adapter(
            transport,
            expected_launch=ResolvedLaunch("claude", "sonnet", "high", Path("/workspace")),
        )

        with self.assertRaisesRegex(
            HarnessControlError,
            "selected model 'sonnet'.*running harness reported 'haiku'",
        ):
            await adapter.start(_launch())

        self.assertEqual(transport.stop_modes, ["forced"])

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_harness_control_claude_stream_1.py:391).
    async def test_correlated_acceptance_retry_activity_and_terminal_result_are_distinct(  # pragma: no cover
        self,
    ) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        adapter = _adapter(transport)
        bridge = HarnessControlBridge(_identity(), adapter, clock=lambda: NOW)
        await bridge.start(_launch())
        submission = asyncio.create_task(
            bridge.submissions().submit(
                bridge.prompt("first prompt", source="durable", request_id="request-1")
            )
        )
        try:
            await transport.wait_for_writes(4)
            turn = _load_fixture("turn.jsonl")
            transport.feed(turn[0])
            receipt = await asyncio.wait_for(submission, timeout=1.0)
            self.assertEqual(receipt.acceptance, "immediate")
            self.assertEqual(bridge.snapshot().activity, "running")
            self.assertFalse(
                any(entry.terminal_result is not None for entry in bridge.transcript())
            )

            transport.feed(turn[1])
            transport.feed(turn[2])
            await _settle()
            self.assertEqual(bridge.snapshot().activity, "settling")

            transport.feed(turn[3])
            await _settle()
            self.assertEqual(bridge.snapshot().activity, "idle")
            transcript = bridge.transcript()
            self.assertEqual([entry.role for entry in transcript], ["user", "assistant", "result"])
            self.assertEqual(transcript[0].text, "first prompt")
            self.assertEqual(transcript[-1].request_id, "request-1")
            assert transcript[-1].terminal_result is not None
            self.assertEqual(transcript[-1].terminal_result.outcome, "completed")
        finally:
            if not submission.done():
                submission.cancel()
                await asyncio.gather(submission, return_exceptions=True)
            await bridge.stop("forced")

    async def test_permissions_and_ask_user_question_use_durable_interaction_response(self) -> None:
        transport = _FakeClaudeTransport(_load_fixture("initialization.jsonl"))
        adapter = _adapter(transport)
        bridge = HarnessControlBridge(_identity(), adapter, clock=lambda: NOW)
        await bridge.start(_launch())
        permission, question = _load_fixture("interactions.jsonl")
        try:
            active = asyncio.create_task(
                bridge.submissions().submit(
                    bridge.prompt("interaction turn", source="terminal", request_id="interaction")
                )
            )
            await transport.wait_for_writes(4)
            transport.feed(_replay(transport.writes[3]))
            self.assertEqual((await active).acceptance, "immediate")
            transport.feed(permission)
            await _settle()
            pending = bridge.snapshot().pending_interaction
            assert pending is not None
            self.assertEqual((pending.kind, pending.choices), ("permission", ("allow", "deny")))
            await bridge.submissions().respond(InteractionResponse("permission-1", "allow", NOW))
            permission_response = transport.writes[-1]["response"]
            assert isinstance(permission_response, dict)
            result = permission_response["response"]
            assert isinstance(result, dict)
            self.assertEqual(result["behavior"], "allow")
            self.assertEqual(bridge.snapshot().activity, "settling")

            transport.feed(question)
            await _settle()
            pending = bridge.snapshot().pending_interaction
            assert pending is not None
            self.assertEqual(pending.kind, "user-input")
            self.assertIn("Which mode", pending.prompt)
            await bridge.submissions().respond(
                InteractionResponse(
                    "question-1",
                    json.dumps({"Which mode should be used?": "Safe"}),
                    NOW,
                )
            )
            question_response = transport.writes[-1]["response"]
            assert isinstance(question_response, dict)
            result = question_response["response"]
            assert isinstance(result, dict)
            updated = result["updatedInput"]
            assert isinstance(updated, dict)
            self.assertEqual(updated["answers"], {"Which mode should be used?": "Safe"})
            transport.feed(_result("interaction done"))
            await _wait_for_activity(adapter, "idle")
        finally:
            await bridge.stop("forced")
