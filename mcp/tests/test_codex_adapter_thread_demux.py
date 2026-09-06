"""Codex thread notification builders shared by adapter regression tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

import pytest
from agents_remember.models.conversations.control_wire import (
    AdapterSnapshot,
)
from agents_remember.serving.codex_app_server_adapter import CodexAppServerAdapter


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_codex_adapter_thread_demux.py:65).
async def eventually(predicate: Callable[[], bool]) -> None:  # pragma: no cover
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def live_snapshot(adapter: CodexAppServerAdapter) -> AdapterSnapshot:
    """The adapter's current snapshot for synchronous predicates (message pump is async)."""

    snap = adapter._snapshot
    assert snap is not None
    return snap


def agent_registry(adapter: CodexAppServerAdapter) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], live_snapshot(adapter).raw["agentRegistry"])


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_codex_adapter_thread_demux.py:763).


# --- the registry on its own -------------------------------------------------------------
#
# Everything above drives the demux through a live adapter and a transport, which is the
# right shape for "interleaved traffic must not kill the seat". These reach
# `CodexThreadRegistry` directly, because the cases below are about what the registry does
# with a frame the adapter would never manufacture: an identity that is not established
# yet, a thread that turns out to be the seat's own after it was registered as a stranger,
# and item/delta frames whose shapes are malformed. Producing those through the transport
# would mean asserting on a fake vendor rather than on the demux.


# --- the bounded queue on its own ---------------------------------------------------------
#
# The flood tests above prove the policy end to end through a live adapter. These reach
# `CodexEventQueue` and `_load_shed_notice` directly, because they are about the two moments
# a live seat cannot be made to reproduce on demand: the close sentinel arriving while the
# queue is over its sentinel headroom, and a shed happening before the adapter has a
# snapshot to sequence the notice against.
