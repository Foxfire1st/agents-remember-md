"""Fake-transport conformance for the strict Pi RPC adapter."""

from __future__ import annotations

import asyncio
import unittest
from collections import deque
from collections.abc import (
    AsyncIterator,
    Callable,
    Mapping,
)
from pathlib import Path
from typing import cast

from agents_remember.errors import (
    HarnessAdapterDisconnectedError,
    HarnessControlError,
)
from agents_remember.models.conversations.control_wire import (
    ControlIdentity,
    ControlOperationKind,
    ControlOperationRef,
    LaunchSpec,
    SubmissionReceipt,
    SubmissionSource,
)
from agents_remember.serving.harness_capabilities import SetResult
from agents_remember.serving.harness_control_models import (
    PromptRequest,
    ShutdownMode,
)
from agents_remember.serving.pi_rpc_adapter import PiRpcAdapter
from agents_remember.serving.pi_rpc_protocol import (
    PiRpcJsonlDecoder,
    parse_pi_models,
)


class _FakePiTransport:
    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_pi_rpc_adapter.py:48).
    def __init__(  # pragma: no cover
        self,
        *,
        session_id: str = "pi-session-1",
        session_file: str | None = "/sessions/pi-session-1.jsonl",
        entries: list[dict[str, object]] | None = None,
        leaf_id: str | None = "entry-0",
    ) -> None:
        self.models = [
            {
                "id": "claude-test",
                "name": "Claude Test",
                "api": "anthropic-messages",
                "provider": "anthropic",
                "baseUrl": "https://api.anthropic.test",
                "reasoning": True,
                "input": ["text"],
                "contextWindow": 200000,
                "maxTokens": 32000,
                "cost": {"input": 1, "output": 2},
                "thinkingLevelMap": {
                    "off": "off",
                    "minimal": "minimal",
                    "low": "low",
                    "medium": "medium",
                    "high": "high",
                    "max": "max",
                },
            },
            {
                "id": "chat-test",
                "name": "Chat Test",
                "api": "openai-completions",
                "provider": "local",
                "baseUrl": "http://localhost:8080",
                "reasoning": False,
                "input": ["text"],
                "contextWindow": 32000,
                "maxTokens": 8000,
                "cost": {"input": 0, "output": 0},
            },
        ]
        self.session = {
            "model": {**self.models[0], "headers": {"Authorization": "secret-test-value"}},
            "thinkingLevel": "high",
            "isStreaming": False,
            "isCompacting": False,
            "steeringMode": "all",
            "followUpMode": "one-at-a-time",
            "sessionFile": session_file,
            "sessionId": session_id,
            "autoCompactionEnabled": True,
            "messageCount": 0,
            "pendingMessageCount": 0,
        }
        if session_file is None:
            self.session.pop("sessionFile")
        self.entries = entries or []
        self.leaf_id = leaf_id
        self.launches: list[LaunchSpec] = []
        self.commands: list[dict[str, object]] = []
        self.stop_modes: list[ShutdownMode] = []
        self.prompt_failures: deque[HarnessAdapterDisconnectedError] = deque()
        self.prompt_refusals: deque[str] = deque()
        self.thinking_clamps: dict[str, str] = {}
        self.command_failures: dict[str, deque[Exception]] = {}
        self.command_hangs: dict[str, int] = {}
        self.hide_selected_model_after_set = False
        self.before_write_hook: Callable[[Mapping[str, object]], None] | None = None
        self._event_token = 0
        self.event_queue: asyncio.Queue[
            Mapping[str, object] | HarnessControlError | HarnessAdapterDisconnectedError | None
        ] = asyncio.Queue()

    async def start(self, launch: LaunchSpec) -> None:
        self.launches.append(launch)

    @property
    def event_token(self) -> int:
        return self._event_token

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_pi_rpc_adapter.py:129).
    async def request(  # pragma: no cover
        self,
        command: Mapping[str, object],
        *,
        before_write: Callable[[], None] | None = None,
    ) -> Mapping[str, object]:
        """Record the command, apply whatever the test armed, then answer it.

        The two halves are separate on purpose: the arming hooks (write hooks, injected
        failures, hangs) are per-transport and apply to every command, while the answers
        below are per-command-type and are what each Pi RPC verb actually returns.
        """
        command_type = cast(str, command["type"])
        await self._record_and_apply_arming(command, command_type, before_write)
        reply = self._replies().get(command_type)
        if reply is None:
            raise AssertionError(f"unexpected fake command: {command_type}")
        return reply(cast(str, command["id"]), command)

    async def _record_and_apply_arming(
        self,
        command: Mapping[str, object],
        command_type: str,
        before_write: Callable[[], None] | None,
    ) -> None:
        """Everything a test can arm ahead of a command, in the order Pi would hit it.

        A prompt failure that could not have reached Pi is raised *before* the command is
        recorded, because such a command never went out; every other arming applies after.
        """
        copied = dict(command)
        if self.before_write_hook is not None:
            self.before_write_hook(copied)
        if before_write is not None:
            before_write()
        if command_type == "prompt" and self.prompt_failures:
            failure = self.prompt_failures[0]
            if not failure.may_have_sent:
                raise self.prompt_failures.popleft()
        self.commands.append(copied)
        remaining_hangs = self.command_hangs.get(command_type, 0)
        if remaining_hangs:
            self.command_hangs[command_type] = remaining_hangs - 1
            await asyncio.Future()
        failures = self.command_failures.get(command_type)
        if failures:
            raise failures.popleft()

    def _reply_get_state(
        self, request_id: str, _command: Mapping[str, object]
    ) -> Mapping[str, object]:
        return _success(request_id, "get_state", dict(self.session))

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_pi_rpc_adapter.py:182).
    def _reply_get_entries(  # pragma: no cover
        self, request_id: str, command: Mapping[str, object]
    ) -> Mapping[str, object]:
        entries = self.entries
        since = command.get("since")
        if since is not None:
            index = next(
                (position for position, entry in enumerate(entries) if entry.get("id") == since),
                None,
            )
            if index is None:
                return _failure(request_id, "get_entries", f"Entry not found: {since}")
            entries = entries[index + 1 :]
        return _success(
            request_id,
            "get_entries",
            {"entries": entries, "leafId": self.leaf_id},
        )

    def _reply_get_available_models(
        self, request_id: str, _command: Mapping[str, object]
    ) -> Mapping[str, object]:
        return _success(request_id, "get_available_models", {"models": self.models})

    def _reply_set_model(
        self, request_id: str, command: Mapping[str, object]
    ) -> Mapping[str, object]:
        key = f"{command.get('provider')}/{command.get('modelId')}"
        selected = next(
            (model for model in self.models if f"{model['provider']}/{model['id']}" == key),
            None,
        )
        if selected is None:
            return _failure(request_id, "set_model", f"Model not found: {key}")
        self.session["model"] = dict(selected)
        if selected.get("reasoning") is False:
            self.session["thinkingLevel"] = "off"
        if self.hide_selected_model_after_set:
            self.models.remove(selected)
        return _acknowledgement(request_id, "set_model")

    def _reply_set_thinking_level(
        self, request_id: str, command: Mapping[str, object]
    ) -> Mapping[str, object]:
        requested = cast(str, command["level"])
        self.session["thinkingLevel"] = self.thinking_clamps.get(requested, requested)
        return _acknowledgement(request_id, "set_thinking_level")

    def _reply_prompt(
        self, request_id: str, _command: Mapping[str, object]
    ) -> Mapping[str, object]:
        if self.prompt_failures:
            raise self.prompt_failures.popleft()
        if self.prompt_refusals:
            # Pi answered the write; the answer is a refusal. Distinct from
            # `prompt_failures`, which are transport-level and never produce a frame.
            return _failure(request_id, "prompt", self.prompt_refusals.popleft())
        return _acknowledgement(request_id, "prompt")

    def _replies(self) -> Mapping[str, Callable[[str, Mapping[str, object]], Mapping[str, object]]]:
        """The verbs this fake answers. A command type absent here is a test asking for
        something Pi's RPC surface does not have, which ``request`` reports as such."""
        return {
            "get_state": self._reply_get_state,
            "get_entries": self._reply_get_entries,
            "get_available_models": self._reply_get_available_models,
            "set_model": self._reply_set_model,
            "set_thinking_level": self._reply_set_thinking_level,
            "prompt": self._reply_prompt,
        }

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_pi_rpc_adapter.py:253).
    async def send(  # pragma: no cover
        self,
        command: Mapping[str, object],
        *,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        if self.before_write_hook is not None:
            self.before_write_hook(command)
        if before_write is not None:
            before_write()
        self.commands.append(dict(command))

    async def _events(self) -> AsyncIterator[Mapping[str, object]]:
        while True:
            item = await self.event_queue.get()
            if item is None:
                raise HarnessAdapterDisconnectedError(
                    "fake Pi transport stopped", may_have_sent=False
                )
            if isinstance(item, HarnessControlError):
                raise item
            yield item

    def events(self) -> AsyncIterator[Mapping[str, object]]:
        return self._events()

    async def stop(self, mode: ShutdownMode) -> None:
        self.stop_modes.append(mode)
        self.event_queue.put_nowait(None)

    def emit(self, frame: Mapping[str, object]) -> None:
        self._event_token += 1
        self.event_queue.put_nowait(frame)

    def fail_events(self, error: HarnessControlError) -> None:
        self.event_queue.put_nowait(error)


class _TransportSequence:
    def __init__(self, *transports: _FakePiTransport) -> None:
        self.transports = deque(transports)

    def __call__(self) -> _FakePiTransport:
        return self.transports.popleft()


def _success(request_id: str, command: str, data: object) -> dict[str, object]:
    return {
        "id": request_id,
        "type": "response",
        "command": command,
        "success": True,
        "data": data,
    }


def _acknowledgement(request_id: str, command: str) -> dict[str, object]:
    """A success frame for a verb Pi acknowledges without a data payload."""
    return {
        "id": request_id,
        "type": "response",
        "command": command,
        "success": True,
    }


def _failure(request_id: str, command: str, error: str) -> dict[str, object]:
    return {
        "id": request_id,
        "type": "response",
        "command": command,
        "success": False,
        "error": error,
    }


def _identity() -> ControlIdentity:
    return ControlIdentity(
        ar_session_id="ar-session-pi",
        tmux_name="ar-pi",
        created_at="2026-07-14T09:00:00+00:00",
    )


def _launch(*, persistent: bool = True) -> LaunchSpec:
    session_args = ("--session-dir", "/sessions") if persistent else ("--no-session",)
    return LaunchSpec(
        identity=_identity(),
        harness_id="pi",
        cwd=Path("/workspace/project"),
        argv=(
            "pi",
            "--provider",
            "anthropic",
            "--model",
            "anthropic/claude-test",
            "--thinking",
            "high",
            *session_args,
            "--no-extensions",
        ),
        env={"PATH": "/tools", "PI_CONFIG": "/settings/pi.json"},
    )


def _operation(
    operation_id: str,
    kind: ControlOperationKind = "prompt",
    *,
    sequence: int = 1,
) -> ControlOperationRef:
    return ControlOperationRef(
        bridge_epoch="pi-test-epoch",
        sequence=sequence,
        operation_id=operation_id,
        kind=kind,
    )


def _prompt(
    request_id: str,
    *,
    source: SubmissionSource = "durable",
    operation: ControlOperationRef | None = None,
) -> PromptRequest:
    return PromptRequest(
        request_id=request_id,
        source=source,
        text=f"prompt {request_id}",
        submitted_at="2026-07-14T09:01:00+00:00",
        operation=operation,
    )


async def _direct_submit(
    adapter: PiRpcAdapter,
    request_id: str,
    *,
    source: SubmissionSource = "durable",
) -> SubmissionReceipt:
    operation = _operation(request_id)
    await adapter.preflight_operation(operation)
    return await adapter.submit(_prompt(request_id, source=source, operation=operation))


async def _direct_set_model(
    adapter: PiRpcAdapter,
    model_key: str,
    *,
    sequence: int = 1,
) -> SetResult:
    operation = _operation(f"set-model-{sequence}", "set-model", sequence=sequence)
    await adapter.preflight_operation(operation)
    return await adapter.set_model(model_key, operation=operation)


async def _direct_set_effort(
    adapter: PiRpcAdapter,
    effort: str,
    *,
    sequence: int = 1,
) -> SetResult:
    operation = _operation(f"set-effort-{sequence}", "set-effort", sequence=sequence)
    await adapter.preflight_operation(operation)
    return await adapter.set_effort(effort, operation=operation)


class PiRpcProtocolTests(unittest.TestCase):
    def test_lf_only_decoder_preserves_unicode_separators_and_accepts_crlf(self) -> None:
        decoder = PiRpcJsonlDecoder()
        first = '{"type":"event","text":"left\u2028right\u2029done"}'.encode()
        frames = decoder.feed(first[:13]) + decoder.feed(first[13:] + b"\r\n")
        self.assertEqual(frames[0]["text"], "left\u2028right\u2029done")
        self.assertEqual(decoder.finish(), ())

    def test_malformed_and_overlong_frames_refuse_loudly(self) -> None:
        with self.assertRaisesRegex(HarnessControlError, "malformed Pi RPC JSONL frame"):
            PiRpcJsonlDecoder().feed(b'{"type":]\n')
        with self.assertRaisesRegex(HarnessControlError, "exceeds 4 bytes"):
            PiRpcJsonlDecoder(max_frame_bytes=4).feed(b"12345")
        with self.assertRaisesRegex(HarnessControlError, "exceeds 4 bytes"):
            PiRpcJsonlDecoder(max_frame_bytes=4).feed(b"12345\n")

    def test_available_models_preserve_provider_identity_and_model_gated_thinking(self) -> None:
        models = parse_pi_models(
            {
                "data": {
                    "models": [
                        {
                            "provider": "provider-a",
                            "id": "shared/id",
                            "name": "Reasoning A",
                            "reasoning": True,
                            "thinkingLevelMap": {
                                "low": None,
                                "xhigh": None,
                                "max": "provider-max",
                            },
                        },
                        {
                            "provider": "provider-b",
                            "id": "shared/id",
                            "name": "Chat B",
                            "reasoning": False,
                        },
                    ]
                }
            }
        )

        self.assertEqual(
            [model.key for model in models],
            ["provider-a/shared/id", "provider-b/shared/id"],
        )
        self.assertEqual(
            [option.key for option in models[0].effort_options],
            ["off", "minimal", "medium", "high", "max"],
        )
        self.assertFalse(models[1].supports_effort)
        self.assertEqual([option.key for option in models[1].effort_options], ["off"])
