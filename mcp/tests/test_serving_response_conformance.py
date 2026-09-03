"""Conformance tests for the declared HTTP response contract (``serving.response_contract``).

The sibling of ``test_served_state_conformance.py``, widened from one route to all 63. That
suite proved ``/api/state``'s assembled body validates against ``ServedWorkspaceProjection``;
this one does the same job for every other route, against the model that route now declares.

**Why the declaration cannot be the gate, and this file has to be.** FastAPI applies
``response_model`` only to values it serializes itself -- a handler that returns a ``Response``
instance is handed back untouched and never reaches ``serialize_response``. 59 of the 63
handlers do exactly that and two more are async-generator SSE routes, so on 59 of them the
decorator buys an OpenAPI schema and validates nothing at runtime. A suite that only asserted
"every route declares a model" would have gone green the moment the decorators landed and
would have caught no drift on those 59 routes ever.

So the tests below **drive the real routes and validate what actually came back**:

* :class:`ServingRouteInventoryTests` -- no HTTP route may lack a declaration, and no
  registration form may escape the walk. The websocket is exempt *by route class*, not by a
  path skip-list: an ``APIWebSocketRoute`` has no ``response_model`` attribute at all, and the
  test asserts that, so the exemption cannot quietly widen to cover a future undeclared HTTP
  route. The walk happens inside a started app, because the lifespan can register routes.
* :class:`RouteWalkerTests` -- each registration form FastAPI accepts, registered and then
  served against a throwaway app, so the inventory's "these are all the routes" clause is
  itself under test rather than assumed.
* :class:`ValidatedRouteHazardTests` -- the two routes FastAPI genuinely validates, where a
  drifted payload is a runtime 500 and not a red test, so their producer's key set is asserted
  against the model's directly.
* :class:`ServingResponseConformanceTests` -- one real request per route through the real app,
  each answer validated against the model declared *for the status that actually came back*
  (``responses[status]["model"]`` when there is one, ``response_model`` otherwise). Every model
  is ``extra="forbid"``, so an undeclared key fails -- and validation is alias-strict, so a
  body that arrived in field-name form fails too (see :func:`validate_wire`).
* :class:`ConversationSuccessConformanceTests` -- real 200/202 bodies off a real control
  bridge for the conversation surface, whose success shapes no refusal path can reach, plus
  the library/open bodies, which need a conversation key this app's own authority will sign.
* :class:`ConversationCompositionRefusalTests` -- the one control refusal ``create_app``
  cannot produce, because it always composes a complete runtime.
* :class:`StreamContractTests` -- the branches a body-shaped model cannot express: the bare
  ``304``, the SSE frames off the generators, and both SSE routes over a real socket.
* :class:`DeclaredSurfaceCoverageTests` -- the score. Every declared ``(method, path, status)``
  is either driven above or listed in :data:`UNDRIVEN_DECLARATIONS` with a reason, asserted
  exactly, so an undriven declaration is a counted number rather than a discovery.
"""

import ast
import asyncio
import contextlib
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import (
    AsyncIterator,
    Iterator,
)
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Any,
    NamedTuple,
    cast,
)

from fastapi import (
    APIRouter,
    FastAPI,
)
from fastapi.routing import (
    APIRoute,
    APIWebSocketRoute,
)
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from starlette.routing import (
    Mount,
    Route,
)

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

import httpx
import uvicorn
from agents_remember.kernel.agentic_settings import agentic_settings_path
from agents_remember.kernel.primitives.runtime_config import (
    McpRuntimeConfig,
    RepositoryScope,
)
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
)
from agents_remember.serving.app import (
    ServingCollaborators,
    create_app,
)
from agents_remember.serving.projector import ProjectionCadence
from agents_remember.serving.response_contract import TerminalCatalogEntryWire
from agents_remember.serving.terminal_catalog import (
    TerminalCatalog,
    terminal_catalog_path,
)
from agents_remember.serving.terminal_pty import TerminalSession
from agents_remember.tasks import (
    TaskDocument,
    write_task_doc,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.worktree_contract import (
    WorktreeContract,
    write_contract,
)
from pydantic import TypeAdapter

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006000557bfabd4"
    "0000000049454e44ae426082"
)


# --- route index -----------------------------------------------------------------------------


class WalkedRoute(NamedTuple):
    """One dispatchable route and the full path it answers on, prefixes already applied."""

    path: str
    route: Any


def walk_routes(routes: Any, prefix: str = "") -> Iterator[WalkedRoute]:
    """Every route the app can dispatch, through every registration form it accepts.

    Four forms reach ``app.routes``, and a walker that models only one of them is a hole in
    every assertion built on top of it:

    * ``include_router`` -- FastAPI keeps the included ``APIRouter`` behind one opaque
      ``_IncludedRouter`` and resolves it at dispatch time. The 25 conversation routes live
      inside one, so a test reading ``app.routes`` alone would see 38 of the 63 and would have
      declared victory over a surface it never looked at. ``include_context.prefix`` is the
      prefix that router was mounted under and the inner ``route.path`` does **not** carry it:
      reporting the bare path would key the index -- and the websocket path assertion -- on a
      path the app does not serve.
    * ``app.mount`` -- a starlette ``Mount`` whose ``.routes`` is the mounted app's own route
      table. Not hypothetical: ``serving/static.py`` mounts the dashboard bundle at ``/``, so a
      sub-application is an established registration form in the very module this inventory
      claims to cover.
    * ``app.router.add_route`` / ``app.add_api_route`` -- plain entries in ``app.routes``. The
      walker yields them; :class:`ServingRouteInventoryTests` is what refuses to ignore a kind
      it does not model.
    * anything registered from inside the lifespan, which is why the inventory walks a
      **started** app rather than the freshly constructed one.
    """

    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            # An ``_IncludedRouter`` dispatches nothing itself -- it is a prefix plus children.
            yield from walk_routes(inner.routes, prefix + route.include_context.prefix)
            continue
        path = prefix + getattr(route, "path", "")
        yield WalkedRoute(path, route)
        if isinstance(route, Mount):
            yield from walk_routes(route.routes, path)


def route_index(app: FastAPI) -> dict[tuple[str, str], APIRoute]:
    """Every HTTP route keyed by ``(METHOD, path)``."""

    index: dict[tuple[str, str], APIRoute] = {}
    for walked in walk_routes(app.routes):
        if isinstance(walked.route, APIRoute):
            for method in walked.route.methods or ():
                index[(method, walked.path)] = walked.route
    return index


def declared_model(route: APIRoute, status: int) -> Any:
    """The model this route declares for exactly this status.

    A ``responses`` entry wins when there is one -- that is the whole point of declaring the
    refusal shapes separately from the success shape.
    """

    entry = route.responses.get(status)
    if entry is not None and "model" in entry:
        return entry["model"]
    return route.response_model


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_serving_response_conformance.py:189).
def declared_statuses(route: APIRoute) -> set[int]:  # pragma: no cover
    """Every status this route declares a body for: the success one plus its ``responses``."""

    statuses = {int(status) for status in route.responses}
    if route.response_model is not None:
        statuses.add(route.status_code or 200)
    return statuses


def declared_pairs(app: FastAPI) -> set[tuple[str, str, int]]:
    """The whole declared contract as ``(method, path, status)`` triples.

    This is the denominator :class:`DeclaredSurfaceCoverageTests` measures the driven table
    against. A declaration nothing drives enforces nothing, so the size of this set minus the
    size of what the suite drives is the honest score of this file.
    """

    return {
        (method, walked.path, status)
        for walked in walk_routes(app.routes)
        if isinstance(walked.route, APIRoute)
        for method in walked.route.methods or ()
        for status in declared_statuses(walked.route)
    }


def validate_wire(model: Any, body: Any) -> Any:
    """Validate a body against its declaration **in alias form only**.

    This is the axis a plain ``TypeAdapter(...).validate_python(body)`` cannot see. Both
    ``WireResponse`` and ``conversation/models.WireModel`` set ``populate_by_name=True``, so
    validation accepts ``identity_digest`` exactly as happily as ``identityDigest``: flip one
    handler's ``model_dump(by_alias=True)`` to ``by_alias=False`` and every key on that route
    goes snake_case -- a total break for the cockpit, which reads the camelCase names -- while
    every model still validates and the suite still reports all green.

    ``by_name=False`` closes it. Only the alias is accepted, so a key that arrived in
    field-name form is an undeclared key against ``extra="forbid"`` and fails here. The two
    neighbouring axes were never the blind ones: a *third* name fails as an extra key either
    way, and a single-word field has no alias to diverge from.

    Models with no alias generator at all (``HttpDetailRefusal``, and the non-wire models the
    unions reach) are unaffected -- with no alias to prefer, pydantic still matches the field
    name, which for them IS the wire name.
    """

    return TypeAdapter(model).validate_python(body, by_alias=True, by_name=False)


def field_name_form(value: Any) -> Any:
    """The same body with every camelCase key rewritten to the field name it aliases.

    Used to *prove* :func:`validate_wire` is load-bearing rather than decorative: the rewritten
    body still validates the old way and must not validate this way.
    """

    if isinstance(value, dict):
        return {
            re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): field_name_form(inner)
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [field_name_form(item) for item in value]
    return value


# --- what the table actually drove ------------------------------------------------------------
#
# Every ``_check`` in this file records the ``(method, path, status)`` it drove here, and
# :class:`DeclaredSurfaceCoverageTests` measures that against :func:`declared_pairs`. Before
# this existed the driving tests kept a ``self.checked`` set that nothing ever read: 88 of the
# 286 declared pairs were driven, the other 198 enforced nothing, and no test said so. Seven
# declared models could be made mathematically unsatisfiable -- a required ``str`` retyped to
# ``int`` -- and the suite stayed green.

DRIVEN: set[tuple[str, str, int]] = set()
"""Filled as the driving tests run; read by the coverage test."""

COMPLETED: set[str] = set()
"""``Class.test_method`` for every driving test that has finished in this process."""


@contextlib.asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """The app on a real uvicorn, as a loopback client.

    ``TestClient`` cannot drive an SSE route: the stream never ends, so a read from inside the
    portal thread cannot be closed from outside it. A real socket can be, which is what lets
    ``/api/stream`` and ``/api/events`` be conformance-checked as the *routes* they are and
    not only as the generators behind them.
    """

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", access_log=False)
    )
    task = asyncio.create_task(server.serve())
    while not server.started:  # pragma: no branch - one spin in practice
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    try:
        yield client
    finally:
        await client.aclose()
        server.should_exit = True
        server.force_exit = True
        await task


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_serving_response_conformance.py:298).
async def first_sse_frame(response: httpx.Response) -> tuple[str, Any]:  # pragma: no cover
    """The first complete ``event:``/``data:`` frame off a live event stream."""

    event = ""
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            return event, json.loads(line.removeprefix("data:").strip())
    raise AssertionError("the stream closed before it framed anything")


# --- fixtures --------------------------------------------------------------------------------


def _config(tmp: Path, **repos: Path) -> McpRuntimeConfig:
    return McpRuntimeConfig(
        config_path=tmp / "settings.json",
        coordination_root=tmp,
        workspace_root=tmp,
        transcript_root=tmp / "logs" / "mcp",
        repositories={
            name: RepositoryScope(repo_id=name, path=path) for name, path in repos.items()
        },
    )


class _LivePaneHost:
    """A terminal host whose panes are all alive, so the liveness sweep keeps its rows.

    Without it ``GET /api/terminal/sessions`` sweeps every seeded row away and the one route
    FastAPI genuinely validates would be conformance-tested against an empty list. The
    signatures mirror ``TerminalHost``'s exactly -- an argument this double ignores is still
    an argument production passes, so it stays named.
    """

    def __init__(self) -> None:
        self.terminated: list[str] = []

    def get(self, session_id: str) -> TerminalSession | None:  # noqa: ARG002
        return None

    def has_session(self, tmux_name: str) -> bool:  # noqa: ARG002
        return True

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_serving_response_conformance.py:343).
    def open(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002  # pragma: no cover
        raise AssertionError("the conformance fixture never spawns a real pane")

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_serving_response_conformance.py:346).
    def ensure(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002  # pragma: no cover
        raise AssertionError("the conformance fixture never spawns a real pane")

    def terminate(self, session_id: str, *, tmux_name: str | None = None) -> None:  # noqa: ARG002
        self.terminated.append(session_id)

    def shutdown(self) -> None:
        return None


def _entry(session: str, tmp: Path, **overrides: Any) -> TerminalCatalogEntry:
    fields: dict[str, Any] = {
        "id": session,
        "label": "Worker",
        "kind": "harness",
        "harness": "claude",
        "lifecycle_id": "L1",
        "cwd": tmp,
        "tmux_name": f"ar-{session}",
        "command": ("claude",),
        "created_at": "2026-06-14T10:00:00+00:00",
        "last_attached_at": "2026-06-14T10:05:00+00:00",
        "status": "running",
    }
    fields.update(overrides)
    return TerminalCatalogEntry(**fields)


def _make_repo(root: Path) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    onboarding = root / "ar-memory" / "onboarding" / "pkg"
    onboarding.mkdir(parents=True, exist_ok=True)
    (onboarding / "mod.py.md").write_text(
        "# mod.py\n\n| key | value |\n| --- | --- |\n"
        "| lastVerifiedCommitHash | abc1234 |\n| lastVerifiedCommitDate | 2026-06-01 |\n",
        encoding="utf-8",
    )
    (root / "ar-memory" / "onboarding" / "overview.md").write_text("# overview\n", encoding="utf-8")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _seed_changeset(tmp: Path, code: Path) -> None:
    """A real git enclosure so the change-set routes answer with real diffs, not refusals."""

    _git(code, "init", "-q", "-b", "main")
    (code / "f.py").write_text("one\n", encoding="utf-8")
    _git(code, "add", "-A")
    _git(code, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=code, check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(code, "branch", "master", "main")
    _git(code, "branch", "work", "master")
    (code / "f.py").write_text("one\ntwo\n", encoding="utf-8")
    task_root = tmp / "tasks" / "R" / "t"
    series_path = task_root / "series-contract.md"
    series_contract = WorktreeContract(
        task_id="T",
        task_name="t",
        repo_name="R",
        workflow_kind="light-task",
        memory_mode="disabled",
        coordination_root=tmp,
        task_root=task_root,
        contract_path=series_path,
        task_artifact=task_root / "task.md",
        worktree_group=tmp / "worktrees" / "R" / "t-series",
        code_repo_path=code,
        code_source_branch="main",
        code_work_branch="master",
        code_base_commit=base,
        code_worktree=code,
        kind="series",
    )
    write_contract(series_path, series_contract)
    publish_new_lifecycle_operation_location(
        series_contract,
        contract_text=series_path.read_text(encoding="utf-8"),
    )
    contract_path = tmp / "tasks" / "R" / "t" / "enclosures" / "leaf-1" / "series-contract.md"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    leaf_contract = WorktreeContract(
        task_id="T",
        task_name="t",
        repo_name="R",
        workflow_kind="light-task",
        memory_mode="disabled",
        coordination_root=tmp,
        task_root=task_root,
        contract_path=contract_path,
        task_artifact=tmp / "tasks" / "R" / "t" / "task.md",
        worktree_group=tmp / "worktrees" / "R" / "t-leaf-1",
        code_repo_path=code,
        code_source_branch="master",
        code_work_branch="work",
        code_base_commit=base,
        code_worktree=code,
        kind="leaf",
        leaf_id="leaf-1",
        parent_task_name="t",
        parent_contract_path=series_path,
    )
    write_contract(contract_path, leaf_contract)
    publish_new_lifecycle_operation_location(
        leaf_contract,
        contract_text=contract_path.read_text(encoding="utf-8"),
    )


def _seed_task_doc(tmp: Path) -> None:
    """A real master + leaf pair, so a leaf REF resolves and the document route has a body."""

    task_root = tmp / "tasks" / "R" / "t"
    write_task_doc(
        task_root,
        TaskDocument.model_validate(
            {
                "id": "T",
                "slug": "task",
                "title": "Master",
                "kind": "master",
                "repo": "R",
                "createdAt": "2026-07-31T10:00",
                "subTasks": [
                    {
                        "number": "leaf-1",
                        "name": "Leaf",
                        "file": "leaf-1.md",
                        "status": "inProgress",
                    }
                ],
            }
        ),
    )
    write_task_doc(
        task_root,
        TaskDocument.model_validate(
            {
                "id": "leaf-1",
                "slug": "leaf-1",
                "title": "The leaf under test",
                "kind": "subTask",
                "repo": "R",
                "createdAt": "2026-07-31T10:01",
                "master": "task.md",
                "objective": "Give the conformance suite a real document to serve.",
                "requirements": ["the declared contract is exercised against a real body"],
            }
        ),
    )


def _seed_notes(tmp: Path) -> None:
    notes = tmp / "tasks" / "R" / "t" / "notes"
    (notes / "reports").mkdir(parents=True, exist_ok=True)
    (notes / "design.md").write_text("# design\n", encoding="utf-8")
    (notes / "reports" / "worker.md").write_text("# report\n", encoding="utf-8")


def _seed_requirements(tmp: Path) -> None:
    requirements = tmp / "tasks" / "R" / "t" / "requirements"
    requirements.mkdir(parents=True, exist_ok=True)
    (requirements / "R1.md").write_text("# R1\n", encoding="utf-8")


# --- inventory --------------------------------------------------------------------------------


class ServingRouteInventoryTests(unittest.TestCase):
    """Nothing on the HTTP surface may be undeclared, and the one exemption is structural."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)
        self.app = create_app(_config(self.tmp), cadence=ProjectionCadence(interval=100))
        # The walk happens inside a STARTED app. ``add_api_route`` is legal from the lifespan,
        # and a walk in ``setUp`` alone runs before startup: such a route would serve real
        # traffic while every assertion below passed without ever having seen it.
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(TestClient(self.app))
        self.walked = list(walk_routes(self.app.routes))
        self.http = [w for w in self.walked if isinstance(w.route, APIRoute)]

    def test_every_http_route_declares_a_response_model(self) -> None:
        undeclared = [
            f"{sorted(walked.route.methods or ())} {walked.path}"
            for walked in self.http
            if walked.route.response_model is None
        ]
        self.assertEqual(undeclared, [])

    def test_the_websocket_is_exempt_because_it_structurally_cannot_declare_one(self) -> None:
        # The exemption is not "we skipped this path". An ``APIWebSocketRoute`` has no
        # ``response_model`` slot at all, because a websocket has no response body -- it
        # upgrades and then frames bytes both ways. Asserting the ABSENCE of the attribute is
        # what stops this from becoming a skip-list that would swallow the next undeclared
        # HTTP route.
        sockets = [w for w in self.walked if isinstance(w.route, APIWebSocketRoute)]
        self.assertEqual([w.path for w in sockets], ["/api/terminal/{session}"])
        for socket in sockets:
            self.assertFalse(hasattr(socket.route, "response_model"))
            self.assertNotIsInstance(socket.route, APIRoute)

    def test_the_declared_surface_is_the_whole_surface(self) -> None:
        # 64 route decorators: 63 HTTP + 1 websocket. Pinned so a new route cannot be added
        # without this suite being told to exercise it.
        sockets = [w for w in self.walked if isinstance(w.route, APIWebSocketRoute)]
        self.assertEqual(len(self.http), 63)
        self.assertEqual(len(sockets), 1)

    def test_no_registration_form_escapes_the_walker(self) -> None:
        # The counting assertions above are only as wide as the kinds the walker models. A
        # route registered as a plain starlette ``Route`` -- ``app.router.add_route(...)`` --
        # is neither an ``APIRoute`` nor an ``APIWebSocketRoute``, so it would serve HTTP 200
        # JSON while every ``isinstance(..., APIRoute)`` filter above stepped straight over it.
        # Refusing a kind this file does not model is what makes those filters total.
        #
        # FastAPI's own documentation routes are plain ``Route``s registered by ``FastAPI``
        # itself, so they are excluded by the URLs the app reports for them -- derived from the
        # app, never a hard-coded path list.
        generated = {
            self.app.openapi_url,
            self.app.docs_url,
            self.app.redoc_url,
            self.app.swagger_ui_oauth2_redirect_url,
        }
        unmodelled = [
            f"{type(walked.route).__name__} {walked.path}"
            for walked in self.walked
            if not isinstance(walked.route, (APIRoute, APIWebSocketRoute, Mount))
            and walked.path not in generated
        ]
        self.assertEqual(unmodelled, [])

    def test_the_mounted_surface_is_pinned(self) -> None:
        # A ``Mount`` is the one form whose interior this file cannot always enumerate: a bare
        # ASGI callable has no ``routes`` to read. The walker descends when there is something
        # to descend into (a mounted ``FastAPI`` app is walked like any other), and pinning the
        # mount list is what makes the remaining case a deliberate act rather than a blind spot
        # -- ``serving/static.py`` already mounts at ``/``, so this form is in live use here.
        mounts = sorted(walked.path for walked in self.walked if isinstance(walked.route, Mount))
        self.assertEqual(mounts, [""])

    def test_every_declared_refusal_status_names_a_model(self) -> None:
        # A ``responses`` entry that documents a status without a model would let a refusal
        # shape drift unchecked while still looking declared.
        #
        # Carrying a ``content`` key does not excuse it. This check used to skip any entry with
        # one, which meant a future ``responses={409: {"content": {...}}}`` -- a refusal status
        # naming a media type and no shape -- passed silently, and ``declared_model`` would
        # then fall back to the route's SUCCESS model for that status. The one legitimate
        # modelless-with-content entry is the SSE media type on the route's own success status,
        # where ``response_model`` really is the shape of a frame's ``data``, so that is the
        # exemption spelled out, and nothing wider.
        bare = [
            f"{walked.path} {status}"
            for walked in self.http
            for status, entry in walked.route.responses.items()
            if "model" not in entry
            and status != 304
            and not (
                list(entry.get("content", {})) == ["text/event-stream"]
                and status == (walked.route.status_code or 200)
                and walked.route.response_model is not None
            )
        ]
        self.assertEqual(bare, [])

    def test_a_modelless_responses_entry_is_a_304_or_a_declared_sse_media_type(self) -> None:
        # Exactly which entries the check above lets through, named. Two kinds earn it and
        # nothing else does:
        #
        #   * ``/api/state``'s 304 -- a conditional GET answers with no body at all, so there
        #     is nothing to model;
        #   * the three SSE entries, which exist to name ``text/event-stream`` as the media
        #     type. Each route still declares the model of one frame's ``data``, which is what
        #     the ``response_model`` assertion below re-checks -- so these are declared, not
        #     exempt.
        modelless = sorted(
            (walked.path, status)
            for walked in self.http
            for status, entry in walked.route.responses.items()
            if "model" not in entry
        )
        self.assertEqual(
            modelless,
            [
                ("/api/events", 200),
                ("/api/state", 304),
                ("/api/stream", 200),
                ("/api/terminal/{ar_session_id}/conversation/events", 200),
            ],
        )
        for walked in self.http:
            for status, entry in walked.route.responses.items():
                if "model" in entry or status == 304:
                    continue
                self.assertEqual(list(entry["content"]), ["text/event-stream"], walked.path)
                self.assertIsNotNone(walked.route.response_model, walked.path)


class ValidatedRouteHazardTests(unittest.TestCase):
    """The two routes FastAPI validates for real, and the one place that is a hazard.

    ``GET /api/terminal/sessions`` and ``GET /api/harnesses`` return a bare ``dict``, so unlike
    the other 59 they are validated at runtime -- and a payload that gains a key, loses a
    required one or changes a type is answered as **HTTP 500**, not passed through as it was
    before these routes declared a model. On ``/api/harnesses`` that is three required keys
    written by one function. On ``/api/terminal/sessions`` it is a 68-key body assembled by
    hand from a 58-optional-field dataclass that is actively grown, and a leaf that adds a
    field to ``to_json`` and forgets ``TerminalCatalogEntryWire`` takes the cockpit's session
    list down rather than degrading it.

    So the producer's key set is asserted against the model's directly. This fires when the
    field is added -- earlier than the runtime 500, and earlier than a conformance run, which
    only sees the fields its fixture happens to populate.
    """

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_serving_response_conformance.py:648).
    def _emitted_keys(self) -> set[str]:  # pragma: no cover
        """Every key ``TerminalCatalogEntry.to_json`` can write, read off its own source.

        An AST scan and not a fully-populated instance, because ``to_json`` is *conditional*:
        every optional key goes through ``_present_fields`` and is absent when ``None``, so no
        single constructed entry proves the full key set. The scan reads exactly the two forms
        that method uses -- string keys of a dict literal, and ``data["x"] = ...``.
        """

        source = (MCP_SRC / "agents_remember" / "models" / "terminal_catalog.py").read_text()
        entry = next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.ClassDef) and node.name == "TerminalCatalogEntry"
        )
        to_json = next(
            node
            for node in entry.body
            if isinstance(node, ast.FunctionDef) and node.name == "to_json"
        )
        keys: set[str] = set()
        for node in ast.walk(to_json):
            if isinstance(node, ast.Dict):
                keys.update(
                    key.value
                    for key in node.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "data"
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                    ):
                        keys.add(target.slice.value)
        return keys

    def test_the_catalog_wire_model_covers_every_key_to_json_emits(self) -> None:
        declared = {
            field.alias or name for name, field in TerminalCatalogEntryWire.model_fields.items()
        }
        emitted = self._emitted_keys()
        # Set EQUALITY in both directions. A key the producer writes and the model does not
        # declare is the 500; a key the model declares and the producer cannot write is a
        # contract that describes a body nothing emits.
        self.assertEqual(sorted(emitted - declared), [])
        self.assertEqual(sorted(declared - emitted), [])
        # Pinned, because the scan reading zero keys would satisfy the equality above.
        self.assertEqual(len(emitted), 68)


class RouteWalkerTests(unittest.TestCase):
    """Every registration form that can serve a body must be visible to the walker.

    The inventory above is an argument of the form "these are all the routes, and all of them
    declare a model". Its first clause is a claim about :func:`walk_routes`, and a registration
    form the walker steps over makes the whole argument vacuous for that route -- it serves
    real traffic and no assertion in this file has an opinion about it. So each form gets
    driven here against a throwaway app: registered, then *served*, then found by the walker at
    the path it actually answered on.
    """

    def _walk(self, app: FastAPI) -> dict[str, Any]:
        with TestClient(app):
            return {
                walked.path: walked.route
                for walked in walk_routes(app.routes)
                if isinstance(walked.route, (APIRoute, Route))
            }

    def test_an_included_router_is_reported_at_its_prefixed_path(self) -> None:
        # The latent one: ``include_router(prefix=...)`` leaves the inner ``route.path``
        # unprefixed, so a walker that reported it would key the route index -- and the
        # websocket path assertion -- on a path the app does not serve.
        inner = APIRouter()

        @inner.get("/leaf", response_model=None)
        def leaf() -> JSONResponse:
            return JSONResponse({"ok": True})

        outer = APIRouter()
        outer.include_router(inner, prefix="/nested")
        app = FastAPI()
        app.include_router(outer, prefix="/api/pre")
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/pre/nested/leaf").status_code, 200)
        self.assertIn("/api/pre/nested/leaf", self._walk(app))

    def test_a_mounted_sub_application_is_walked(self) -> None:
        sub = FastAPI()

        @sub.get("/thing", response_model=None)
        def thing() -> JSONResponse:
            return JSONResponse({"ok": True})

        app = FastAPI()
        app.mount("/api/plugin", sub)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/plugin/thing").status_code, 200)
        self.assertIn("/api/plugin/thing", self._walk(app))

    def test_a_plain_starlette_route_is_walked_and_is_not_an_api_route(self) -> None:
        # ``add_route`` serves HTTP 200 JSON while carrying no ``response_model`` slot at all,
        # so the walker must surface it and the inventory must refuse the kind. Being visible
        # is not the same as being declared, and this route is the proof of the difference.
        async def plain(_request: Any) -> JSONResponse:
            return JSONResponse({"ok": True})

        app = FastAPI()
        app.router.add_route("/api/plain", plain, methods=["GET"])
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/plain").status_code, 200)
        walked = self._walk(app)
        self.assertIn("/api/plain", walked)
        self.assertNotIsInstance(walked["/api/plain"], APIRoute)

    def test_a_route_registered_inside_the_lifespan_is_walked_after_startup(self) -> None:
        @contextlib.asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            app.add_api_route("/api/late", late, methods=["GET"], response_model=None)
            yield

        def late() -> JSONResponse:
            return JSONResponse({"ok": True})

        app = FastAPI(lifespan=lifespan)
        self.assertNotIn("/api/late", {walked.path for walked in walk_routes(app.routes)})
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/late").status_code, 200)
            started = {walked.path for walked in walk_routes(app.routes)}
        self.assertIn("/api/late", started)


# --- the driven conformance table --------------------------------------------------------------


def _await_projector_ready(client: TestClient):
    """Cross the one deterministic readiness boundary before asserting served models."""

    response = client.get("/api/state")
    for _attempt in range(100):
        if response.status_code != 503:
            return response
        time.sleep(0.01)
        response = client.get("/api/state")
    return response


class ServingResponseConformanceTests(unittest.TestCase):
    """One real request per route; the answer must validate against what the route declares."""

    maxDiff = None

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)
        settings = agentic_settings_path(self.tmp)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"orchestration": {"agentNotifier": {"enabled": False}}}), encoding="utf-8"
        )
        self.code = self.tmp / "ws" / "R"
        self.code.mkdir(parents=True, exist_ok=True)
        _make_repo(self.code)
        _seed_changeset(self.tmp, self.code)
        _seed_notes(self.tmp)
        _seed_task_doc(self.tmp)
        _seed_requirements(self.tmp)
        # A SECOND repo with no memory root at all. ``FileScope.onboarding_root`` is ``None``
        # only when ``resolve_coordination_context`` raises ``MissingMemoryError``, and that is
        # the sole input that reaches ``OnboardingPartnerNone`` -- the fifth of the five shapes
        # ``GET /api/files/onboarding`` declares, and one no repo carrying ``ar-memory/`` can
        # ever produce.
        self.bare = self.tmp / "ws" / "N"
        (self.bare / "pkg").mkdir(parents=True, exist_ok=True)
        (self.bare / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.config = _config(self.tmp, R=self.code, N=self.bare)
        self.catalog = TerminalCatalog(terminal_catalog_path(self.tmp))
        # ``live`` carries a control endpoint: without one every control route answers 409
        # ``unsupported`` and the success half of that family would go unexercised.
        self.catalog.upsert(
            _entry(
                "live",
                self.tmp,
                control_endpoint=self.tmp / "control.sock",
                control_state="ready",
                control_protocol="ar-harness-control/v1",
            )
        )
        self.catalog.upsert(_entry("landed", self.tmp, status="landed"))
        self.catalog.upsert(_entry("plain", self.tmp, kind="terminal", harness=None))
        # A live harness seat with NO control endpoint -- the legacy shape, and the only input
        # that reaches ``/paste``'s 409 ``unsupported`` leg.
        self.catalog.upsert(_entry("legacy", self.tmp))
        self.host = _LivePaneHost()
        self.app = create_app(
            self.config,
            cadence=ProjectionCadence(interval=100),
            collaborators=ServingCollaborators(
                terminal_host=cast(Any, self.host), terminal_catalog=self.catalog
            ),
        )
        self.routes = route_index(self.app)

    @contextmanager
    def _client(self, *, peer: tuple[str, int] = ("127.0.0.1", 50000)):
        """A LOOPBACK peer: conversation authorization is loopback-only by design, so the
        default ``testclient`` host would turn every conversation route into the same 403 and
        the refusal surface would never be exercised past its first gate.

        ``peer`` exists so the 403 leg can be driven deliberately, from a host that is not
        loopback -- see ``test_the_conversation_authorization_refusal_conforms``."""

        with TestClient(self.app, client=peer) as client:
            response = _await_projector_ready(client)
            self.assertEqual(response.status_code, 200, response.text)
            yield client

    def _check(
        self,
        client: TestClient,
        method: str,
        path: str,
        *,
        status: int,
        route: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Drive one route for real, then validate its body against the declared model."""

        key = (method, route or path)
        self.assertIn(key, self.routes, f"no such registered route: {key}")
        DRIVEN.add((method, route or path, status))
        response = client.request(method, path, **kwargs)
        self.assertEqual(response.status_code, status, f"{method} {path}: {response.text}")
        model = declared_model(self.routes[key], status)
        self.assertIsNotNone(model, f"{method} {path} declares nothing for {status}")
        body = response.json()
        try:
            validate_wire(model, body)
        except Exception as exc:  # pragma: no cover - only on a real contract breach
            raise AssertionError(
                f"{method} {path} -> {status} violates its contract: {exc}\n{body}"
            ) from exc
        return body

    def tearDown(self) -> None:
        COMPLETED.add(f"{type(self).__name__}.{self._testMethodName}")

    # -- the shapes a declaration named and nothing drove ---------------------------------

    # -- the wire is camelCase, and that is a thing a model alone cannot pin --------------

    # -- the read surface ----------------------------------------------------------------

    # -- the write surface ---------------------------------------------------------------

    # -- the harness control surface -----------------------------------------------------

    # -- the conversation surface (refusal legs; success legs live in the class below) ----


# --- the conversation success shapes -----------------------------------------------------------


# --- the branches no body-shaped model can express ---------------------------------------------


# --- the score ---------------------------------------------------------------------------------


UNDRIVEN_DECLARATIONS: dict[tuple[str, str], frozenset[int]] = {
    # --- one shared table, seventeen routes -------------------------------------------------
    # ``CONTROL_RESPONSES`` / ``CONVERSATION_RESPONSES`` declare six statuses on every
    # conversation route. Reaching each of them means driving a REAL control bridge into each
    # typed failure -- a stale epoch, a rejected operation, a dead socket mid-write -- and the
    # bridge fixture models the harness edge, not those failures. What is driven instead is the
    # 404 (no such seat) on every one of them, the 403 on one, and every success shape off the
    # live bridge; the rest of each row is the same ``StatusRefusal`` shape reached by a
    # different exception, through one mapper (``_map_typed_error``) that IS driven.
    ("GET", "/api/terminal/{ar_session_id}/conversation"): frozenset({400, 409, 422, 503}),
    ("POST", "/api/terminal/{ar_session_id}/conversation/agents/{agent_id}/history"): frozenset(
        {400, 403, 409, 422, 503}
    ),
    ("POST", "/api/terminal/{ar_session_id}/conversation/attachments"): frozenset(
        {400, 403, 409, 422, 503}
    ),
    ("POST", "/api/terminal/{ar_session_id}/conversation/attachments/rebind"): frozenset(
        {200, 400, 403, 409, 422, 503}
    ),
    (
        "POST",
        "/api/terminal/{ar_session_id}/conversation/attachments/{request_id}/reconcile",
    ): frozenset({200, 400, 403, 409, 422, 503}),
    (
        "GET",
        "/api/terminal/{ar_session_id}/conversation/attachments/{request_id}/status",
    ): frozenset({400, 403, 409, 422, 503}),
    # The conversation SSE route: 200 is a stream, and unlike ``/api/stream`` it needs a live
    # bridge AND a live uvicorn at once, which no fixture here stands up together.
    ("GET", "/api/terminal/{ar_session_id}/conversation/events"): frozenset(
        {200, 403, 404, 409, 422, 503}
    ),
    ("POST", "/api/terminal/{ar_session_id}/conversation/interrupt"): frozenset(
        {200, 202, 400, 403, 409, 503}
    ),
    ("POST", "/api/terminal/{ar_session_id}/conversation/interrupt-reconcile"): frozenset(
        {200, 202, 400, 403, 409, 422, 503}
    ),
    ("POST", "/api/terminal/{ar_session_id}/conversation/interrupt-status"): frozenset(
        {200, 202, 400, 403, 409, 503}
    ),
    ("GET", "/api/terminal/{ar_session_id}/conversation/policy"): frozenset(
        {400, 403, 409, 422, 503}
    ),
    ("POST", "/api/terminal/{ar_session_id}/conversation/submit"): frozenset(
        {202, 400, 403, 409, 422, 503}
    ),
    ("GET", "/api/terminal/{ar_session_id}/conversation/telemetry"): frozenset(
        {400, 403, 409, 422, 503}
    ),
    ("GET", "/api/terminal/{ar_session_id}/operation-queue"): frozenset({400, 403, 409, 422}),
    (
        "GET",
        "/api/terminal/{ar_session_id}/operation-queue/pending-withdrawal-recoveries",
    ): frozenset({400, 403, 409, 422, 503}),
    ("POST", "/api/terminal/{ar_session_id}/operation-queue/withdraw"): frozenset(
        {200, 202, 400, 403, 409, 422, 503}
    ),
    ("POST", "/api/terminal/{ar_session_id}/operation-queue/withdraw-reconcile"): frozenset(
        {200, 400, 403, 409, 422, 503}
    ),
    ("POST", "/api/terminal/{ar_session_id}/operation-queue/withdraw-recovery"): frozenset(
        {200, 400, 403, 409, 422, 503}
    ),
    ("POST", "/api/terminal/{ar_session_id}/operation-queue/withdraw-recovery-ack"): frozenset(
        {200, 400, 403, 409, 422, 503}
    ),
    ("POST", "/api/terminal/{ar_session_id}/operation-queue/withdraw-status"): frozenset(
        {200, 400, 403, 409, 422, 503}
    ),
    # --- the library surface ----------------------------------------------------------------
    # 200 on list/read needs a harness whose native history store is installed and probeable,
    # which is a real vendor binary. The capability-gate 422 IS driven, and so is every open
    # outcome the gate can settle without one.
    ("GET", "/api/harnesses/{harness_id}/conversations"): frozenset({200, 400, 403, 409, 503}),
    ("GET", "/api/harnesses/{harness_id}/conversations/{conversation_key}"): frozenset(
        {200, 400, 403, 409, 503}
    ),
    # 201 ``opened`` / 202 ``pending`` / 503 ``launch-failed`` need a harness that actually
    # resumes; the 422 ``unsupported`` outcome and the typed refusals on the same statuses are
    # driven, which is what pins the two families the merged table used to collapse.
    ("POST", "/api/harnesses/{harness_id}/conversations/{conversation_key}/open"): frozenset(
        {201, 202, 503}
    ),
    (
        "POST",
        "/api/harnesses/{harness_id}/conversations/{conversation_key}/open-status",
    ): frozenset({201, 202, 400, 403, 409, 503}),
    (
        "POST",
        "/api/harnesses/{harness_id}/conversations/{conversation_key}/open-reconcile",
    ): frozenset({201, 202, 400, 403, 409, 503}),
    # --- the projection surface -------------------------------------------------------------
    # 503 is "the projection has not primed yet", a startup race the fixtures deliberately do
    # not have -- ``ProjectionCadence`` primes before the client is handed over.
    ("POST", "/api/actions/{action}"): frozenset({503}),
    ("GET", "/api/state"): frozenset({503}),
    ("GET", "/api/task-document"): frozenset({503}),
    # --- the harness control surface ---------------------------------------------------------
    # 503 is the control bridge refusing or unreachable mid-call. The seat resolution legs (404
    # unknown seat, 409 unsupported seat) and every success leg are driven; this last one needs
    # a bridge that accepts a connection and then fails, which the doubles do not model.
    ("GET", "/api/harnesses/{harness}/capabilities"): frozenset({200, 503}),
    ("GET", "/api/terminal/{session}/capabilities"): frozenset({503}),
    ("GET", "/api/terminal/{session}/submission-authority"): frozenset({503}),
    ("POST", "/api/terminal/{session}/set-model"): frozenset({503}),
    ("POST", "/api/terminal/{session}/set-effort"): frozenset({503}),
    ("POST", "/api/terminal/{session}/submission-status"): frozenset({503}),
    ("POST", "/api/terminal/{session}/withdraw"): frozenset({503}),
    ("POST", "/api/terminal/{session}/submit"): frozenset({503}),
    ("POST", "/api/terminal/{session}/reconcile"): frozenset({503}),
    ("POST", "/api/terminal/{session}/interaction-response"): frozenset({200, 503}),
    # 200 needs an actor seat that holds retire authority over another seat, which is a seat
    # role fixture rather than a request; the 403 and both 404s are driven.
    ("POST", "/api/terminal/{session}/retire"): frozenset({200}),
}
"""Every declared ``(method, path, status)`` this suite does not drive, and why.

This is a ledger, not a suppression list: :class:`DeclaredSurfaceCoverageTests` asserts it
EXACTLY, so a declaration that stops being driven has to be added here by hand, and a leg that
becomes drivable has to be removed. The number it carries is the honest score of this file.
"""


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
