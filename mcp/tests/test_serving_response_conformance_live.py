from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, get_args
from unittest import mock

import httpx
import uvicorn
from _control_plane import OPERATOR, FakeControlAdapter, drive_activity, make_harness
from agents_remember.errors import HarnessControlError
from agents_remember.kernel.agentic_settings import agentic_settings_path
from agents_remember.models.conversations.opening import (
    OpenConversationOperation,
)
from agents_remember.observer.projection import LifecycleProjection
from agents_remember.serving.app import create_app, stream_events
from agents_remember.serving.conversation.control.api import router as control_router
from agents_remember.serving.conversation.library.api import _OPEN_STATUS_BY_OUTCOME
from agents_remember.serving.conversation.library.factories import library_shared
from agents_remember.serving.conversation.library.scope import canonical_library_scope
from agents_remember.serving.delta import DeltaEvent
from agents_remember.serving.events import stream_raw_events
from agents_remember.serving.projector import ProjectionCadence, Projector
from agents_remember.serving.served_state import SERVED_TAIL_FIELDS
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_serving_response_conformance import (
    _PNG,
    COMPLETED,
    DRIVEN,
    UNDRIVEN_DECLARATIONS,
    _await_projector_ready,
    _config,
    declared_model,
    declared_pairs,
    first_sse_frame,
    route_index,
    serve,
    validate_wire,
)
from test_serving_response_conformance_cases_1 import ServingResponseConformance1
from test_serving_response_conformance_cases_2 import ServingResponseConformance2


class ConversationSuccessConformanceTests(unittest.IsolatedAsyncioTestCase):
    """Real 200/202 bodies off a real control bridge, validated against the declared models.

    The refusal legs above prove the ``responses`` tables; only a live bridge can prove the
    success models, which are exactly the models these handlers dump. Real uvicorn (not
    ``TestClient``) because the bridge must live on this test's own event loop.
    """

    async def asyncSetUp(self) -> None:
        self.adapter = FakeControlAdapter(harness="codex")
        self.harness = make_harness(self, self.adapter, "ar-conf", harness="codex")
        await self.harness.start()
        self.addAsyncCleanup(self.harness.stop)
        self.epoch = self.harness.epoch
        for module in ("control", "active"):
            patch = mock.patch(
                f"agents_remember.serving.conversation.{module}.api"
                ".resolve_conversation_authorization",
                lambda request: OPERATOR,
            )
            patch.start()
            self.addCleanup(patch.stop)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.harness.app, host="127.0.0.1", port=0, log_level="warning", access_log=False
            )
        )
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
        self.routes = route_index(self.harness.app)

    async def asyncTearDown(self) -> None:
        COMPLETED.add(f"{type(self).__name__}.{self._testMethodName}")
        await self.client.aclose()
        self.server.should_exit = True
        await self.task

    async def _check(
        self, method: str, path: str, *, route: str, status: int, **kwargs: Any
    ) -> Any:
        DRIVEN.add((method, route, status))
        response = await self.client.request(method, path, **kwargs)
        self.assertEqual(response.status_code, status, f"{method} {path}: {response.text}")
        model = declared_model(self.routes[(method, route)], status)
        body = response.json()
        try:
            validate_wire(model, body)
        except Exception as exc:  # pragma: no cover - only on a real contract breach
            raise AssertionError(f"{method} {path} -> {status}: {exc}\n{body}") from exc
        return body

    async def test_the_conversation_success_bodies_conform(self) -> None:
        session = "ar-conf"
        base = f"/api/terminal/{session}"
        params = {"expectedBridgeEpoch": self.epoch}
        await self._check(
            "GET",
            base + "/conversation",
            route="/api/terminal/{ar_session_id}/conversation",
            status=200,
            params=params,
        )
        await self._check(
            "POST",
            base + "/conversation/agents/agent-1/history",
            route="/api/terminal/{ar_session_id}/conversation/agents/{agent_id}/history",
            status=200,
            params=params,
        )
        await self._check(
            "GET",
            base + "/conversation/policy",
            route="/api/terminal/{ar_session_id}/conversation/policy",
            status=200,
            params=params,
        )
        await self._check(
            "GET",
            base + "/conversation/telemetry",
            route="/api/terminal/{ar_session_id}/conversation/telemetry",
            status=200,
            params=params,
        )
        await self._check(
            "GET",
            base + "/operation-queue",
            route="/api/terminal/{ar_session_id}/operation-queue",
            status=200,
            params=params,
        )
        await self._check(
            "GET",
            base + "/operation-queue/pending-withdrawal-recoveries",
            route="/api/terminal/{ar_session_id}/operation-queue/pending-withdrawal-recoveries",
            status=200,
            params=params,
        )
        # The staging answer and the submit answer are the two bodies assembled at the route,
        # so they are the two the conversation surface had no model for at all.
        await self._check(
            "POST",
            base + "/conversation/attachments",
            route="/api/terminal/{ar_session_id}/conversation/attachments",
            status=200,
            params=params,
            data={"requestId": "att-1", "metadata": json.dumps([{"kind": "image"}])},
            files=[("assets", ("dot.png", _PNG, "image/png"))],
        )
        await self._check(
            "GET",
            base + "/conversation/attachments/att-1/status",
            route="/api/terminal/{ar_session_id}/conversation/attachments/{request_id}/status",
            status=200,
            params=params,
        )
        await self._check(
            "POST",
            base + "/conversation/submit",
            route="/api/terminal/{ar_session_id}/conversation/submit",
            status=200,
            json={
                "expectedBridgeEpoch": self.epoch,
                "requestId": "sub-1",
                "disposition": "next",
                "draftRevision": 1,
                "content": [{"type": "text", "text": "generate"}],
            },
        )
        # An interrupt REJECTED by the harness is still the operation's own body, on 422 --
        # ``interrupt_http_status`` picks the status off the acknowledgement. That is the leg
        # the shared refusal table got wrong, so it is the leg worth driving.
        await drive_activity(self.harness, "running")
        self.adapter.interrupt_error = HarnessControlError(
            "synthetic native refusal for the declared 422 operation body"
        )
        await self._check(
            "POST",
            base + "/conversation/interrupt",
            route="/api/terminal/{ar_session_id}/conversation/interrupt",
            status=422,
            params=params,
            json={"turnId": "turn-1", "requestId": "int-1"},
        )
        await self._check(
            "POST",
            base + "/conversation/interrupt-status",
            route="/api/terminal/{ar_session_id}/conversation/interrupt-status",
            status=422,
            params=params,
            json={"turnId": "turn-1", "requestId": "int-1"},
        )

    def _conversation_key(self, vendor: str = "thread-1") -> tuple[str, str]:
        """A conversation key this app's OWN cursor authority will re-authorize.

        The signing key is minted per runtime, so a key has to come from the app under test;
        there is no fixture value that works. Without one the whole library surface answers
        404 ``unknown-harness`` at the first gate, which is exactly why the open trio's own
        body shapes had never been driven.
        """

        runtime = self.harness.runtime
        shared = library_shared(runtime)
        authorization = runtime.authorization.resolve(client_host="127.0.0.1")
        scope = canonical_library_scope(
            authorization, "codex", None, workspace_root=runtime.scope.workspace_root
        )
        digest = shared.cursor_authority.identity_digest(
            "codex", vendor, scope.canonical_project_scope
        )
        key = shared.cursor_authority.mint_conversation_key(
            scope,
            vendor_conversation_id=vendor,
            identity_digest=digest,
            catalog_generation=1,
        )
        return key.root, digest

    async def test_the_library_and_open_bodies_conform(self) -> None:
        """The five library routes, past the 404 that was the only leg anything drove.

        This is the surface the merged ``responses`` table got wrong. The open trio answer
        409/422/503 with TWO unrelated families -- the operation's own outcome, and
        ``_error_response``'s typed refusals -- and a dict merge kept only one of them. Both
        are driven here, on the same status, so a table that declares one and not the other
        fails.
        """

        base = "/api/harnesses/codex/conversations"
        listing = "/api/harnesses/{harness_id}/conversations"
        one = listing + "/{conversation_key}"
        # ``LIBRARY_RESPONSES[422]``: the capability gate, and the only library refusal that is
        # not the shared ``{status, detail}`` envelope. This fixture's harness registry is
        # empty, so the history gate reports the harness unavailable.
        refusal = await self._check("GET", base, route=listing, status=422)
        self.assertEqual(refusal["status"], "capability-unavailable")
        self.assertEqual(refusal["capabilityState"], "unavailable")
        key, digest = self._conversation_key()
        await self._check("GET", f"{base}/{key}", route=one, status=422)

        # The open trio's own body, on a status the shared table also claims. The resume gate
        # refuses on this fixture, so the operation settles ``unsupported`` -- an OUTCOME, with
        # a full ``OpenConversationOperation`` on the wire, not a refusal envelope.
        opened = await self._check(
            "POST",
            f"{base}/{key}/open",
            route=one + "/open",
            status=422,
            json={"requestId": "o1", "expectedIdentityDigest": digest},
        )
        self.assertEqual(opened["outcome"], "unsupported")
        for suffix in ("open-status", "open-reconcile"):
            replayed = await self._check(
                "POST",
                f"{base}/{key}/{suffix}",
                route=f"{one}/{suffix}",
                status=422,
                json={"requestId": "o1"},
            )
            self.assertEqual(replayed["outcome"], "unsupported")

        # ...and the OTHER family on the open trio's statuses: a typed refusal, same route,
        # same 409 the outcome table also claims. Under a table that merged instead of
        # unioning, this body had no declared model at all.
        stale = await self._check(
            "POST",
            f"{base}/{key}/open",
            route=one + "/open",
            status=409,
            json={"requestId": "o2", "expectedIdentityDigest": "sha256:not-the-row"},
        )
        self.assertEqual(stale["status"], "stale-identity")
        scoped = await self._check(
            "POST",
            f"{base}/{key}/open",
            route=one + "/open",
            status=403,
            json={"requestId": "o3", "expectedIdentityDigest": digest, "cwd": "/"},
        )
        self.assertEqual(scoped["status"], "scope-denied")
        malformed = await self._check(
            "POST",
            f"{base}/{key}/open",
            route=one + "/open",
            status=400,
            json={"requestId": "o4"},
        )
        self.assertEqual(malformed["status"], "invalid-cursor")


class ConversationCompositionRefusalTests(unittest.TestCase):
    """``CONTROL_RESPONSES[503]``: declared on all 17 control routes, driven on none.

    ``create_app`` always composes a complete ``ConversationRuntime``, so the composition
    refusal is unreachable through it -- which is precisely why this status sat declared and
    unexercised on seventeen routes. It is not unreachable in general: the routers are
    independently mountable (``register_conversation_routes`` is a separate call), and a router
    mounted without its runtime is the state the refusal exists for. Driving the real route
    function through the real declaration is what makes the declaration falsifiable.
    """

    def test_a_control_route_without_its_runtime_answers_its_declared_503(self) -> None:
        app = FastAPI()
        app.include_router(control_router)
        routes = route_index(app)
        route = "/api/terminal/{ar_session_id}/operation-queue"
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            response = client.get(
                "/api/terminal/s1/operation-queue", params={"expectedBridgeEpoch": "e"}
            )
        self.assertEqual(response.status_code, 503, response.text)
        body = response.json()
        self.assertEqual(body["status"], "composition-unavailable")
        validate_wire(declared_model(routes[("GET", route)], 503), body)
        DRIVEN.add(("GET", route, 503))

    def tearDown(self) -> None:
        COMPLETED.add(f"{type(self).__name__}.{self._testMethodName}")


class StreamContractTests(unittest.IsolatedAsyncioTestCase):
    """The 304 with no body, and the SSE frames whose ``data`` the declaration names.

    The generator legs below drive the real generators rather than the HTTP route, for the same
    reason ``test_served_state_conformance.py`` does: an SSE route never ends, so a
    ``TestClient`` stream cannot be closed from inside the portal thread. The generator IS the
    route body -- ``api_stream``/``api_events`` do nothing but yield from it -- and driving it
    directly is the only way to reach the second frame, which is where the snapshot/delta
    asymmetry lives.

    That is a good reason to drive the generator and was never a good reason to drive ONLY the
    generator: it leaves the route itself -- its status, its media type, its serializer -- with
    no test at all. ``test_the_sse_routes_answer_over_http`` closes that over a real socket,
    where a stream can be cancelled from the outside.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)
        settings = agentic_settings_path(self.tmp)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"orchestration": {"agentNotifier": {"enabled": False}}}), encoding="utf-8"
        )
        self.config = _config(self.tmp)
        self.app = create_app(self.config, cadence=ProjectionCadence(interval=100))
        self.routes = route_index(self.app)

    def tearDown(self) -> None:
        COMPLETED.add(f"{type(self).__name__}.{self._testMethodName}")

    async def test_the_sse_routes_answer_over_http_with_their_declared_first_frame(self) -> None:
        # Both SSE routes, driven as routes: 200, ``text/event-stream``, and a first frame
        # whose ``data`` validates against what the route declares. Neither had ever been
        # requested over HTTP by any test in this repository.
        async with serve(self.app) as client:
            for path, expected in (("/api/stream", "snapshot"), ("/api/events", "ready")):
                async with client.stream("GET", path, timeout=10) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(
                        response.headers["content-type"].startswith("text/event-stream"),
                        response.headers["content-type"],
                    )
                    event, data = await asyncio.wait_for(first_sse_frame(response), timeout=10)
                self.assertEqual(event, expected)
                route = self.routes[("GET", path)]
                validate_wire(route.response_model, data)
                DRIVEN.add(("GET", path, 200))

    def test_the_304_branch_declares_a_body_less_response(self) -> None:
        # ``/api/state`` cannot become a model-returning handler because of this branch, so the
        # contract has to say the 304 carries no model -- and the route has to keep proving it.
        entry = self.routes[("GET", "/api/state")].responses[304]
        self.assertNotIn("model", entry)
        with TestClient(self.app) as client:
            first = _await_projector_ready(client)
            self.assertEqual(first.status_code, 200, first.text)
            cached = client.get("/api/state", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b"")
        DRIVEN.add(("GET", "/api/state", 304))

    async def test_the_state_stream_snapshot_and_delta_split_the_declaration(self) -> None:
        route = self.routes[("GET", "/api/stream")]
        self.assertEqual(list(route.responses[200]["content"]), ["text/event-stream"])
        projector = Projector(self.config, cadence=ProjectionCadence(interval=100))
        await projector.prime()
        stream = stream_events(projector)
        try:
            snapshot = await asyncio.wait_for(stream.__anext__(), timeout=5)
            self.assertEqual(snapshot.event, "snapshot")
            # The declared model is what a ``snapshot`` frame's data is.
            validate_wire(route.response_model, snapshot.data)
            pending = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0.02)
            projector._broadcast(
                (
                    1,
                    DeltaEvent(
                        "lifecycle",
                        LifecycleProjection(
                            id="L1",
                            state="running",
                            phase="build",
                            fleeting=False,
                            startedAt="2026-06-14T10:00:00Z",
                            lastEventTs="2026-06-14T10:00:00Z",
                        ),
                    ),
                )
            )
            delta = await asyncio.wait_for(pending, timeout=5)
        finally:
            await stream.aclose()
        # The asymmetry the declaration rests on: a delta is one bare projection node, so it
        # is NOT the declared whole-state body and must not carry the serve-time tail.
        self.assertEqual(delta.event, "lifecycle")
        assert isinstance(delta.data, dict)
        self.assertEqual(set(delta.data) & set(SERVED_TAIL_FIELDS), set())
        LifecycleProjection.model_validate(delta.data)

    async def test_the_raw_river_ready_marker_validates_against_its_declaration(self) -> None:
        # The river's ``event`` frames are verbatim observer records, so the marker is the one
        # frame this route mints -- and therefore the only one it can honestly declare.
        route = self.routes[("GET", "/api/events")]
        self.assertEqual(list(route.responses[200]["content"]), ["text/event-stream"])
        stream = stream_raw_events(self.config, interval=100)
        try:
            frame = await asyncio.wait_for(stream.__anext__(), timeout=5)
        finally:
            await stream.aclose()
        self.assertEqual(frame.event, "ready")
        validate_wire(route.response_model, frame.data)


def _grouped(pairs: set[tuple[str, str, int]]) -> dict[tuple[str, str], frozenset[int]]:
    grouped: dict[tuple[str, str], set[int]] = {}
    for method, path, status in pairs:
        grouped.setdefault((method, path), set()).add(status)
    return {key: frozenset(value) for key, value in grouped.items()}


_DRIVING_CLASSES: tuple[type[unittest.TestCase], ...] = (
    ServingResponseConformance1,
    ServingResponseConformance2,
    ConversationSuccessConformanceTests,
    ConversationCompositionRefusalTests,
    StreamContractTests,
)


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_serving_response_conformance_live.py:457).
def _driven_pairs() -> set[tuple[str, str, int]]:  # pragma: no cover
    """What the driving tests actually drove, this process.

    ``DRIVEN`` is filled as they run, which makes it free on a whole-module run and wrong on a
    partial one -- and a coverage number computed from a partial run is the same silent
    absence this whole class exists to end. So when a driver has not run, it is run here.
    """

    expected = {
        f"{cls.__name__}.{name}"
        for cls in _DRIVING_CLASSES
        for name in unittest.TestLoader().getTestCaseNames(cls)
    }
    if not expected <= COMPLETED:
        suite = unittest.TestSuite()
        loader = unittest.TestLoader()
        for cls in _DRIVING_CLASSES:
            suite.addTests(loader.loadTestsFromTestCase(cls))
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        if not result.wasSuccessful():  # pragma: no cover - the drivers report their own
            raise AssertionError(
                "the conformance drivers failed while measuring coverage; run the module"
            )
    return set(DRIVEN)


class DeclaredSurfaceCoverageTests(unittest.TestCase):
    """The conformance table must account for the whole declared surface.

    A declaration nothing drives enforces nothing. That was not a hypothetical: the driving
    tests kept a ``self.checked`` set that no assertion ever read, 88 of 286 declared
    ``(method, path, status)`` pairs were driven, and seven declared models could be made
    mathematically unsatisfiable -- a required field retyped to a type no body could ever
    carry -- without one test going red.

    The gap is smaller now and it is not zero, because some legs need a real vendor harness or
    a bridge that fails mid-write. What changed is that the remainder is *counted*: it is
    listed in :data:`UNDRIVEN_DECLARATIONS` with a reason, asserted exactly, and a declaration
    that quietly stops being exercised fails here instead of going unnoticed.
    """

    maxDiff = None

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)
        self.app = create_app(_config(self.tmp), cadence=ProjectionCadence(interval=100))

    def test_the_conformance_table_accounts_for_every_declared_pair(self) -> None:
        declared = declared_pairs(self.app)
        driven = _driven_pairs()
        # Nothing may be driven that is not declared: a request answering on a status the route
        # never declared would otherwise be validated against the success model by fallback.
        self.assertEqual(sorted(driven - declared), [])
        self.assertEqual(_grouped(declared - driven), UNDRIVEN_DECLARATIONS)
        # The headline, pinned so neither side can move without a decision: 292 declared pairs,
        # 139 driven against a real body, 153 declared-and-undriven with a reason each.
        self.assertEqual(len(declared), 292)
        self.assertEqual(len(driven), 139)
        self.assertEqual(len(declared) - len(driven), 153)

    def test_every_route_has_at_least_one_driven_status(self) -> None:
        # The weaker claim, but the one that has to hold without exception: a route no request
        # ever reaches is a route whose declaration is pure decoration. Every one of the 63 is
        # driven on at least one status, which is what makes the ledger above a list of
        # unexercised *legs* rather than of unexercised routes.
        driven = {(method, path) for method, path, _ in _driven_pairs()}
        never = sorted(
            f"{method} {path}"
            for method, path, _ in declared_pairs(self.app)
            if (method, path) not in driven
        )
        self.assertEqual(never, [])

    def test_the_open_status_map_is_total_over_the_declared_outcomes(self) -> None:
        # ``_open_call`` indexes ``_OPEN_STATUS_BY_OUTCOME`` directly, so this equality is what
        # makes that safe -- and it is also what removed an undeclared 500: the old ``.get(...,
        # 500)`` answered an unmapped outcome with a full operation body on a status no
        # ``responses`` table names and no test could reach.
        self.assertEqual(
            set(_OPEN_STATUS_BY_OUTCOME),
            set(get_args(OpenConversationOperation.model_fields["outcome"].annotation)),
        )
