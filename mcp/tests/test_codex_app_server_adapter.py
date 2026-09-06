from __future__ import annotations

import asyncio
import json
from collections import (
    defaultdict,
    deque,
)
from collections.abc import (
    AsyncIterator,
    Callable,
    Mapping,
)
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from agents_remember.models.conversations.control_wire import (
    ControlIdentity,
    ControlOperationRef,
    LaunchSpec,
)
from agents_remember.serving.codex_app_server_adapter import (
    CodexAppServerAdapter,
    CodexAppServerSettings,
)
from agents_remember.serving.codex_app_server_protocol import (
    JsonObject,
    RequestId,
)
from agents_remember.serving.harness_control_models import (
    AdapterEvent,
    PromptRequest,
    ShutdownMode,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "codex_app_server_0_144_3.json"


@pytest.fixture
# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_codex_app_server_adapter.py:40).
def anyio_backend() -> str:  # pragma: no cover
    return "asyncio"


class FakeCodexTransport:
    def __init__(self) -> None:
        self.responses: dict[str, deque[JsonObject | Exception]] = defaultdict(deque)
        self.requests: list[tuple[str, JsonObject]] = []
        self.notifications: list[tuple[str, JsonObject]] = []
        self.server_responses: list[tuple[RequestId, JsonObject]] = []
        self.server_errors: list[tuple[RequestId, int, str]] = []
        self.launches: list[LaunchSpec] = []
        self.stop_modes: list[ShutdownMode] = []
        self.incoming: asyncio.Queue[JsonObject | Exception | None] = asyncio.Queue()
        self.before_write_hook: Callable[[str, Mapping[str, object]], None] | None = None

    async def start(self, launch: LaunchSpec) -> None:
        self.launches.append(launch)

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_codex_app_server_adapter.py:59).
    async def request(  # pragma: no cover
        self,
        method: str,
        params: Mapping[str, object],
        *,
        before_write: Callable[[], None] | None = None,
    ) -> JsonObject:
        if self.before_write_hook is not None:
            self.before_write_hook(method, params)
        if before_write is not None:
            before_write()
        self.requests.append((method, dict(params)))
        response = self.responses[method].popleft()
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        self.notifications.append((method, dict(params)))

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_codex_app_server_adapter.py:79).
    async def _messages(self) -> AsyncIterator[JsonObject]:  # pragma: no cover
        while True:
            message = await self.incoming.get()
            if message is None:
                return
            if isinstance(message, Exception):
                raise message
            yield message

    def messages(self) -> AsyncIterator[JsonObject]:
        return self._messages()

    async def respond(self, request_id: RequestId, result: Mapping[str, object]) -> None:
        self.server_responses.append((request_id, dict(result)))

    async def respond_error(self, request_id: RequestId, *, code: int, message: str) -> None:
        self.server_errors.append((request_id, code, message))

    async def stop(self, mode: ShutdownMode) -> None:
        self.stop_modes.append(mode)
        self.incoming.put_nowait(None)

    def queue_response(self, method: str, response: JsonObject | Exception) -> None:
        self.responses[method].append(response)

    def emit(self, message: JsonObject) -> None:
        self.incoming.put_nowait(deepcopy(message))


class BlockingTurnStartTransport(FakeCodexTransport):
    def __init__(self) -> None:
        super().__init__()
        self.turn_start_requested = asyncio.Event()
        self.release_turn_start = asyncio.Event()

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_codex_app_server_adapter.py:114).
    async def request(  # pragma: no cover
        self,
        method: str,
        params: Mapping[str, object],
        *,
        before_write: Callable[[], None] | None = None,
    ) -> JsonObject:
        if method != "turn/start":
            return await super().request(method, params, before_write=before_write)
        if self.before_write_hook is not None:
            self.before_write_hook(method, params)
        if before_write is not None:
            before_write()
        self.requests.append((method, dict(params)))
        self.turn_start_requested.set()
        await self.release_turn_start.wait()
        response = self.responses[method].popleft()
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def fixture() -> JsonObject:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def fixture_object(data: Mapping[str, object], *path: str) -> JsonObject:
    value: object = data
    for key in path:
        assert isinstance(value, dict)
        value = value[key]
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def fixture_list(data: Mapping[str, object], *path: str) -> list[object]:
    value: object = data
    for key in path:
        assert isinstance(value, dict)
        value = value[key]
    assert isinstance(value, list)
    return value


def add_model(
    data: JsonObject,
    *,
    model: str = "gpt-5.6-mini",
    efforts: tuple[str, ...] = ("low", "medium"),
    default_effort: str = "medium",
) -> None:
    fixture_list(data, "modelListResult", "data").append(
        {
            "id": f"model-{model}",
            "model": model,
            "displayName": "GPT-5.6 Mini",
            "description": "Second fixture model",
            "hidden": False,
            "isDefault": False,
            "defaultReasoningEffort": default_effort,
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort, "description": effort.title()} for effort in efforts
            ],
        }
    )


def identity() -> ControlIdentity:
    return ControlIdentity(
        ar_session_id="ar-session-1",
        tmux_name="ar-codex-1",
        created_at="2026-07-14T12:00:00+00:00",
    )


def launch() -> LaunchSpec:
    return LaunchSpec(
        identity=identity(),
        harness_id="codex",
        cwd=Path("/workspace"),
        argv=("codex", "app-server"),
        env={"PRESERVE_INSTALLED_AUTH": "1"},
    )


def request(request_id: str, text: str = "hello") -> PromptRequest:
    return PromptRequest(
        request_id=request_id,
        source="durable",
        text=text,
        submitted_at="2026-07-14T12:01:00+00:00",
        operation=ControlOperationRef(
            bridge_epoch="codex-test-epoch",
            sequence=1,
            operation_id=request_id,
            kind="prompt",
        ),
    )


def prime_start(
    transport: FakeCodexTransport,
    data: JsonObject,
    *,
    resume: bool = False,
) -> None:
    transport.queue_response("initialize", fixture_object(data, "initializeResult"))
    transport.queue_response("model/list", fixture_object(data, "modelListResult"))
    method = "thread/resume" if resume else "thread/start"
    key = "threadResumeResult" if resume else "threadStartResult"
    transport.queue_response(method, fixture_object(data, key))


TEST_SETTINGS = CodexAppServerSettings(
    reasoning_effort="xhigh",
    model="gpt-5.6-sol",
    ephemeral=True,
)
"""The settings every adapter test starts from; vary one with ``replace(TEST_SETTINGS, ...)``.

``CodexAppServerSettings`` already *is* the object these tests were spelling out one field at
a time -- it carries the resume thread, the approval policy and reviewer, the sandbox and its
turn policy, the config map and the submission limit -- so the fixture threads the settings
object itself rather than re-declaring a parallel parameter list that can drift from it.
"""


def make_adapter(
    transport: FakeCodexTransport,
    settings: CodexAppServerSettings = TEST_SETTINGS,
) -> CodexAppServerAdapter:
    return CodexAppServerAdapter(
        settings,
        transport_factory=lambda: transport,
        clock=lambda: "2026-07-14T12:02:00+00:00",
    )


async def settle() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def drain_events(adapter: CodexAppServerAdapter) -> int:
    """Empty the adapter's event queue and return the sequence to compare against.

    A duplicate notification is proved inert by the sequence not moving, which only reads
    as evidence from a known-empty queue.
    """
    adapter._events.drain()
    return adapter._event_sequence


async def assert_notification_is_inert(
    adapter: CodexAppServerAdapter,
    transport: FakeCodexTransport,
    notification: JsonObject,
) -> None:
    """Emit a notification the adapter has already settled and prove nothing moved."""
    event_sequence = drain_events(adapter)
    transport.emit(notification)
    await settle()
    assert adapter._event_sequence == event_sequence


async def next_event_of_kind(events: AsyncIterator[AdapterEvent], kind: str) -> AdapterEvent:
    """The next event of ``kind``, skipping the ones the adapter emits on the way there."""
    while True:
        event = await asyncio.wait_for(anext(events), 1)
        if event.kind == kind:
            return event


def turn_start_result(data: JsonObject, turn_id: str, status: str = "inProgress") -> JsonObject:
    result = deepcopy(fixture_object(data, "turnStartResult"))
    turn = fixture_object(result, "turn")
    turn["id"] = turn_id
    turn["status"] = status
    return result


def turn_completed_notification(data: JsonObject, turn_id: str) -> JsonObject:
    notification = deepcopy(fixture_object(data, "notifications", "completed"))
    fixture_object(notification, "params", "turn")["id"] = turn_id
    return notification
