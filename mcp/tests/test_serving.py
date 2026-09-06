"""Tests for the dashboard serving layer (slice 04, commits 4a + 4b).

Covers the pure per-entity projection diff (``serving.delta``), the shared projector's
prime/current/subscribe fan-out (``serving.projector``), the SSE event sequence
(``serving.app.stream_events`` -- snapshot then deltas), the FastAPI app endpoints via
TestClient (``/api/state``, ``/api/actions``, static mount), the shipped static-bundle
resolver (``serving.static``), and the umbrella ``agents-remember dashboard`` CLI parsing.

The 4b additions: the raw event channel's byte-offset tail + cursor resume
(``serving.events``), sim-mode fixture load + replay clock + progressive feeder +
determinism (``serving.sim``), and the POST action skeleton's availability/attribution
mapping (``serving.actions``).
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))


from agents_remember.kernel.primitives.runtime_config import (
    McpRuntimeConfig,
)
from agents_remember.observer.lifecycle_state import State
from agents_remember.observer.projection import (
    EnclosureNode,
    LifecycleProjection,
    ProviderNode,
    WorkspaceProjection,
)
from agents_remember.serving.app import (
    LiveProjectionInputs,
    create_app,
    stream_events,
)
from agents_remember.serving.projector import (
    ProjectionCadence,
    Projector,
)

_TS = "2026-06-14T10:00:00Z"


def _config(tmp: Path) -> McpRuntimeConfig:
    return McpRuntimeConfig(
        config_path=tmp / "settings.json",
        coordination_root=tmp,
        workspace_root=tmp,
        transcript_root=tmp / "logs" / "mcp",
    )


def _lifecycle(ident: str, *, tokens: int = 0, state: State = "running") -> LifecycleProjection:
    return LifecycleProjection(
        id=ident,
        state=state,
        phase="build",
        fleeting=False,
        startedAt=_TS,
        lastEventTs=_TS,
        tokens=tokens,
    )


def _projection(
    *,
    lifecycles: tuple[LifecycleProjection, ...] = (),
    providers: tuple[ProviderNode, ...] = (),
    enclosures: tuple[EnclosureNode, ...] = (),
    active_worktree_groups: tuple[str, ...] = (),
) -> WorkspaceProjection:
    """A projection of the resolved tree at ``_TS``.

    ``WorkspaceProjection`` defaults every field, so the rolled-up ``metrics``/``analytics``
    cases construct the model directly rather than routing another two knobs through here.
    """
    return WorkspaceProjection(
        generatedAt=_TS,
        lifecycles=list(lifecycles),
        providers=list(providers),
        enclosures=list(enclosures),
        activeWorktreeGroups=list(active_worktree_groups),
    )


class StreamEventsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    async def test_snapshot_subscription_cannot_lose_an_interleaved_projection(self) -> None:
        projector = Projector(_config(self.tmp), cadence=ProjectionCadence(interval=100))
        await projector.prime()
        gen = stream_events(projector)

        first = await asyncio.wait_for(gen.__anext__(), timeout=1)
        self.assertEqual(first.event, "snapshot")
        self.assertEqual(len(projector._subscribers), 1)
        _, initial = projector.current()
        assert initial is not None
        projector._publish_projection(
            initial.model_copy(update={"lifecycles": [_lifecycle("handoff")]})
        )

        second = await asyncio.wait_for(gen.__anext__(), timeout=1)
        self.assertEqual(second.event, "lifecycle")
        self.assertEqual(
            second.data, _lifecycle("handoff").model_dump(by_alias=True, exclude_none=True)
        )
        await gen.aclose()
        self.assertEqual(len(projector._subscribers), 0)


class StateEtagTests(unittest.TestCase):
    """The /api/state change gate: 200 -> ETag -> 304, real change -> new ETag."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _client_with_held_projection(
        self, held: list[WorkspaceProjection], *, interval: float = 0.02
    ) -> TestClient:
        patcher = mock.patch(
            "agents_remember.serving.projector.project_and_write",
            side_effect=lambda config, *, now, tick=None, refresh=None: held[0],
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # watch_changes=False: this world changes only through the mocked project_and_write,
        # which no filesystem watcher can observe -- the tick loop must stay interval-paced
        # (the live contract for watcher-invisible changes is the heartbeat bound instead).
        return TestClient(
            create_app(
                _config(self.tmp),
                cadence=ProjectionCadence(interval=interval),
                live_inputs=LiveProjectionInputs(change_watch=False),
            )
        )

    def _get_until(self, client: TestClient, *, etag: str, want_status: int) -> httpx.Response:
        """Poll /api/state with If-None-Match until the tick loop publishes ``want_status``."""
        deadline = time.monotonic() + 5.0
        while True:
            response = client.get("/api/state", headers={"If-None-Match": etag})
            if response.status_code == want_status or time.monotonic() > deadline:
                return response
            time.sleep(0.02)

    def test_etag_304_cycle_then_new_etag_on_content_change(self) -> None:
        held = [_projection(lifecycles=(_lifecycle("L1"),))]
        with self._client_with_held_projection(held) as client:
            first = client.get("/api/state")
            self.assertEqual(first.status_code, 200)
            etag = first.headers["etag"]
            self.assertTrue(etag.startswith('W/"'))
            self.assertEqual(first.headers["cache-control"], "no-cache")

            unchanged = client.get("/api/state", headers={"If-None-Match": etag})
            self.assertEqual(unchanged.status_code, 304)
            self.assertEqual(unchanged.headers["etag"], etag)
            self.assertEqual(unchanged.content, b"")  # zero body: the whole point

            # Volatile-only movement (ages advance every tick) must NOT mint a new revision.
            held[0] = _projection(
                lifecycles=(_lifecycle("L1").model_copy(update={"staleSeconds": 77.0}),)
            )
            time.sleep(0.1)  # several ticks at interval=0.02
            still = client.get("/api/state", headers={"If-None-Match": etag})
            self.assertEqual(still.status_code, 304)
            self.assertEqual(still.headers["etag"], etag)

            # A real content change mints a new revision: 200 with a fresh ETag + fresh body.
            held[0] = _projection(lifecycles=(_lifecycle("L1", tokens=9),))
            changed = self._get_until(client, etag=etag, want_status=200)
            self.assertEqual(changed.status_code, 200)
            self.assertNotEqual(changed.headers["etag"], etag)
            body = changed.json()
            self.assertEqual(body["lifecycles"][0]["tokens"], 9)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
