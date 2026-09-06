from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Mapping
from copy import deepcopy

import pytest
from agents_remember.errors import (
    CodexAppServerRpcError,
    NativeHistoryLimitExceeded,
    NativeHistoryUnavailable,
)
from agents_remember.models.conversations.control_wire import (
    LaunchSpec,
)
from agents_remember.serving.codex_app_server_history import CodexNativeHistoryReader
from agents_remember.serving.codex_app_server_protocol import JsonObject, RequestId
from agents_remember.serving.harness_control_client import _decode_control_response
from agents_remember.serving.harness_control_ipc import (
    _error_response,
    _raise_control_response_error,
)
from agents_remember.serving.harness_control_models import (
    ShutdownMode,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class HistoryTransport:
    def __init__(self) -> None:
        self.responses: dict[str, deque[JsonObject | Exception]] = defaultdict(deque)
        self.requests: list[tuple[str, JsonObject]] = []

    def queue(self, method: str, response: JsonObject | Exception) -> None:
        self.responses[method].append(response)

    async def start(self, launch: LaunchSpec) -> None:
        del launch

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        before_write: Callable[[], None] | None = None,
    ) -> JsonObject:
        if before_write is not None:
            before_write()
        self.requests.append((method, dict(params)))
        response = self.responses[method].popleft()
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        del method, params

    async def _messages(self) -> AsyncIterator[JsonObject]:
        if False:
            yield {}

    def messages(self) -> AsyncIterator[JsonObject]:
        return self._messages()

    async def respond(self, request_id: RequestId, result: Mapping[str, object]) -> None:
        del request_id, result

    async def respond_error(
        self,
        request_id: RequestId,
        *,
        code: int,
        message: str,
    ) -> None:
        del request_id, code, message

    async def stop(self, mode: ShutdownMode) -> None:
        del mode


def item_page(
    item_id: str,
    *,
    text: str = "content",
    next_cursor: str | None,
) -> JsonObject:
    return {
        "data": [
            {
                "turnId": f"turn-{item_id}",
                "item": {"id": item_id, "type": "agentMessage", "text": text},
            }
        ],
        "nextCursor": next_cursor,
        "backwardsCursor": None,
    }


@pytest.mark.anyio
async def test_bounded_items_are_probed_and_opaque_cursor_consumes_each_source_page_once() -> None:
    transport = HistoryTransport()
    transport.queue("thread/items/list", item_page("item-1", next_cursor="source-1"))
    transport.queue("thread/items/list", item_page("item-2", next_cursor="source-2"))
    transport.queue("thread/items/list", item_page("item-3", next_cursor=None))
    reader = CodexNativeHistoryReader()

    first = await reader.read_page(
        transport,
        thread_id="agent-1",
        cursor=None,
        limit=2,
        byte_budget=48 * 1024,
    )
    assert [frame.native_id for frame in first.frames] == ["item-1", "item-2"]
    assert first.next_cursor is not None
    assert first.next_cursor.startswith("ar-cnh1.")
    second = await reader.read_page(
        transport,
        thread_id="agent-1",
        cursor=first.next_cursor,
        limit=2,
        byte_budget=48 * 1024,
    )
    assert [frame.native_id for frame in second.frames] == ["item-3"]
    assert second.next_cursor is None
    assert [params["cursor"] for method, params in transport.requests if "cursor" in params] == [
        "source-1",
        "source-2",
    ]
    assert all(params["limit"] == 1 for method, params in transport.requests)
    assert not transport.responses["thread/read"]


@pytest.mark.anyio
async def test_two_cursor_cycle_terminates_typed_without_re_requesting_a_source_page() -> None:
    transport = HistoryTransport()
    transport.queue("thread/items/list", item_page("item-0", next_cursor="A"))
    transport.queue("thread/items/list", item_page("item-1", next_cursor="B"))
    transport.queue("thread/items/list", item_page("item-2", next_cursor="A"))
    reader = CodexNativeHistoryReader()

    cursor: str | None = None
    for expected in ("item-0", "item-1", "item-2"):
        page = await reader.read_page(
            transport,
            thread_id="agent-cycle",
            cursor=cursor,
            limit=1,
            byte_budget=4096,
        )
        assert [frame.native_id for frame in page.frames] == [expected]
        cursor = page.next_cursor
        assert cursor is not None

    with pytest.raises(NativeHistoryUnavailable) as raised:
        await reader.read_page(
            transport,
            thread_id="agent-cycle",
            cursor=cursor,
            limit=1,
            byte_budget=4096,
        )
    assert raised.value.code == "source-cursor-cycle"
    assert [
        params.get("cursor")
        for method, params in transport.requests
        if method == "thread/items/list"
    ] == [None, "A", "B"]


@pytest.mark.anyio
async def test_recognized_bounded_rpc_failure_never_silently_falls_back() -> None:
    transport = HistoryTransport()
    transport.queue(
        "thread/items/list",
        CodexAppServerRpcError("thread/items/list", -32600, "thread is not materialized yet"),
    )
    with pytest.raises(NativeHistoryUnavailable) as raised:
        await CodexNativeHistoryReader().read_page(
            transport,
            thread_id="agent-1",
            cursor=None,
            limit=10,
            byte_budget=4096,
        )
    assert raised.value.code == "bounded-rpc-refused"
    assert [method for method, _params in transport.requests] == ["thread/items/list"]


@pytest.mark.anyio
async def test_source_response_over_post_transport_materialization_ceiling_is_typed() -> None:
    transport = HistoryTransport()
    transport.queue(
        "thread/items/list",
        item_page("item-big", text="x" * 4096, next_cursor=None),
    )
    reader = CodexNativeHistoryReader(materialization_ceiling_bytes=1024)
    with pytest.raises(NativeHistoryLimitExceeded) as raised:
        await reader.read_page(
            transport,
            thread_id="agent-1",
            cursor=None,
            limit=10,
            byte_budget=4096,
        )
    assert raised.value.actual_bytes > raised.value.limit_bytes


def test_native_history_limit_outcome_survives_both_control_ipc_clients() -> None:
    error = NativeHistoryLimitExceeded(
        "one selected child item is too large",
        actual_bytes=2048,
        limit_bytes=1024,
    )
    response = _error_response(error)
    assert response["status"] == "native-history-limit-exceeded"

    with pytest.raises(NativeHistoryLimitExceeded) as async_client:
        _raise_control_response_error(response)
    assert async_client.value.actual_bytes == 2048
    assert async_client.value.limit_bytes == 1024

    encoded = json.dumps(response).encode()
    with pytest.raises(NativeHistoryLimitExceeded) as sync_client:
        _decode_control_response(encoded)
    assert sync_client.value.code == "materialization-limit"
