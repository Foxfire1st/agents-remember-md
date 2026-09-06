"""Registered library route tests over real ASGI with a loopback peer (260718-CHATS-L2).

Drives the actual FastAPI composition (root router + L0 runtime) through its ASGI interface so
routing, validation, camel-case wire shape, and the precise O4 error-status ladder are all
covered on the production path. Native boundaries (ports, opener, proof, retire) are doubled;
the installed-runtime suite covers them live.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlencode

from agents_remember.models.conversations.capabilities import (
    CapabilityEvidence,
    FeatureCapability,
    HistoryCapabilities,
)
from agents_remember.models.conversations.content import (
    ConversationItem,
    TextBlock,
)
from agents_remember.models.conversations.control_wire import (
    AdapterSnapshot,
    ControlIdentity,
    SubmissionAuthorityDescriptor,
)
from agents_remember.models.conversations.history import (
    ConversationLibraryPage,
    ConversationLibraryPageScope,
    ConversationLibraryRow,
    HistoricalConversationPage,
)
from agents_remember.models.conversations.identity import (
    HarnessId,
    NativeConversationRef,
    ProvenanceEvidence,
)
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
)
from agents_remember.serving.conversation.authorization import (
    LocalOperatorAuthorizationResolver,
)
from agents_remember.serving.conversation.library import api as library_api
from agents_remember.serving.conversation.library import factories
from agents_remember.serving.conversation.library import open_service as open_module
from agents_remember.serving.conversation.library.cursor import (
    LibraryCursorAuthority,
    mint_signing_key,
)
from agents_remember.serving.conversation.library.factories import LibraryShared
from agents_remember.serving.conversation.library.open_service import (
    LibraryBinding,
    OpenOperationLedger,
)
from agents_remember.serving.conversation.library.scope import canonical_library_scope
from agents_remember.serving.conversation.library.service import ConversationLibraryService
from agents_remember.serving.conversation.router import register_conversation_routes
from agents_remember.serving.conversation.runtime import ConversationRuntime, ConversationScope
from agents_remember.serving.harness_capability_catalog import HarnessCapabilityCatalog
from agents_remember.serving.harness_control_client import ControlPlaneClient
from agents_remember.serving.hosted_readiness import HostedReadinessResult
from agents_remember.serving.terminal_catalog import (
    TerminalCatalog,
)
from agents_remember.serving.terminal_liveness import TerminalCatalogLivenessConfig, utc_now
from agents_remember.serving.terminal_opener import OpenTerminalResult
from fastapi import FastAPI
from starlette.types import Message, Scope


def _snapshot(vendor: str | None):
    return AdapterSnapshot(
        identity=ControlIdentity(ar_session_id="x", tmux_name="t", created_at="c"),
        control="ready",  # type: ignore[arg-type]
        activity="idle",  # type: ignore[arg-type]
        acceptance="immediate",  # type: ignore[arg-type]
        vendor_session_id=vendor,
    )


class _Host:
    def __init__(self) -> None:
        self.sessions: set[str] = set()

    def has_session(self, tmux_name: str) -> bool:
        return tmux_name in self.sessions

    def terminate(self, sid: str, *, tmux_name: str | None = None) -> None:
        self.sessions.discard(tmux_name or sid)


class _Gates:
    def __init__(self, resume_state: str = "supported") -> None:
        self.resume_state = resume_state

    async def history_capabilities(self, _harness_id: str):
        if self.resume_state == "supported":
            feature = FeatureCapability(
                state="supported",
                reason="gate passed",
                evidence_tier="runtime-fixture",
                evidence=CapabilityEvidence(
                    runtime_version="0.80.7",
                    fixture_id="gate-test",
                    observed_at="2026-07-18T00:00:00Z",
                ),
            )
        else:
            feature = FeatureCapability(
                state=self.resume_state,  # type: ignore[arg-type]
                reason="resume target has no production launch seam",
                evidence_tier="none",
            )
        return HistoryCapabilities(
            list=feature,
            read=feature,
            resume=feature,
            completeness=feature,
            tool_completeness=feature,
        )


def _supported_caps():
    feature = FeatureCapability(
        state="supported",
        reason="gate passed",
        evidence_tier="runtime-fixture",
        evidence=CapabilityEvidence(
            runtime_version="0.80.7",
            fixture_id="gate-test",
            observed_at="2026-07-18T00:00:00Z",
        ),
    )
    return HistoryCapabilities(
        list=feature,
        read=feature,
        resume=feature,
        completeness=feature,
        tool_completeness=feature,
    )


_SUPPORTED_CAPS = _supported_caps()


LOOPBACK = ("127.0.0.1", 5000)


@dataclass(frozen=True)
class AsgiClient:
    """The app under test together with the peer address it will see.

    These routes are loopback-guarded, so "who is calling" belongs to the connection, not to
    any single request: the guard test keeps the requests identical and moves only the peer.
    Binding the two together means a call site cannot accidentally describe a request as
    coming from somewhere the app was never handed.
    """

    app: FastAPI
    peer: tuple[str, int] | None = LOOPBACK


async def asgi_request(
    client: AsgiClient,
    method: str,
    path: str,
    *,
    query: Mapping[str, str] | None = None,
    body: Mapping[str, object] | None = None,
) -> tuple[int, object]:
    app = client.app
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = [(b"host", b"testserver")]
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": urlencode(query or {}).encode("ascii"),
        "headers": headers,
        "client": client.peer,
        "server": ("testserver", 80),
        "app": app,
        "state": {},
    }
    messages: list[Message] = []
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(scope, receive, send)
    status = next(
        int(message["status"]) for message in messages if message["type"] == "http.response.start"
    )
    raw = b"".join(
        bytes(message.get("body", b""))  # type: ignore[arg-type]
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(raw) if raw else None


class _FakePort:
    """Scriptable port double enforcing the same scope/cursor checks as the real ports."""

    def __init__(
        self,
        harness_id: HarnessId,
        cursor: LibraryCursorAuthority,
        caller,
        workspace: Path,
    ):
        self.harness_id: HarnessId = harness_id
        self.cursor = cursor
        self.caller = caller
        self.workspace = workspace
        self.list_calls: list[dict[str, Any]] = []
        self.read_calls: list[dict[str, Any]] = []
        self.resolve_calls: list[NativeConversationRef] = []

    async def list(self, scope, *, cursor, limit):
        self.list_calls.append({"scope": scope, "cursor": cursor, "limit": limit})
        digest = self.cursor.identity_digest(
            self.harness_id, "vendor-1", scope.canonical_project_scope
        )
        return ConversationLibraryPage(
            scope=ConversationLibraryPageScope(
                harness_id=self.harness_id,  # type: ignore[arg-type]
                canonical_project_scope=scope.canonical_project_scope,
                query_digest=scope.query_digest,
            ),
            rows=(
                ConversationLibraryRow(
                    conversation_key=self.cursor.mint_conversation_key(
                        scope,
                        vendor_conversation_id="vendor-1",
                        identity_digest=digest,
                        catalog_generation=2,
                    ),
                    identity_digest=digest,
                    title="fake row",
                    safe_native_id_suffix="dor-1",
                    last_activity_at="2026-07-18T00:00:00Z",
                    capabilities=_SUPPORTED_CAPS,
                ),
            ),
            next_cursor=None,
        )

    async def read(self, ref, *, before, limit):
        self.read_calls.append({"ref": ref, "before": before, "limit": limit})
        return HistoricalConversationPage(
            ref=ref,
            items=(
                ConversationItem(
                    item_id="i1",
                    revision=1,
                    global_ordinal=1,
                    lane="unknown-input",
                    source="native-history",
                    provenance=ProvenanceEvidence(strength="native-only", origin="fake"),
                    role="user",
                    kind="message",
                    phase="completed",
                    blocks=(TextBlock(block_id="i1:b0", text="hi"),),
                ),
            ),
            older_cursor=None,
            has_older=False,
            total_items=1,
            historical_capabilities=_SUPPORTED_CAPS,
        )

    async def resolve_resume_target(self, ref):
        self.resolve_calls.append(ref)
        scope = canonical_library_scope(
            self.caller, self.harness_id, None, workspace_root=self.workspace
        )
        return self.cursor.mint_resume_target(
            scope,
            vendor_conversation_id=ref.vendor_conversation_id,
            identity_digest=ref.identity_digest,
            catalog_generation=2,
            launch={"kind": "argv", "args": ["--session", "/x.jsonl"]},
        )


class LibraryApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.catalog = TerminalCatalog(self.tmp / "terminal-sessions.json")
        self.host = _Host()
        self.resolver = LocalOperatorAuthorizationResolver.for_workspace(self.tmp)
        self.runtime = ConversationRuntime(
            scope=ConversationScope(workspace_root=self.tmp, coordination_root=self.tmp),
            catalog=self.catalog,
            control_plane=ControlPlaneClient(),
            host=self.host,  # type: ignore[arg-type]
            harness_registry=lambda: (),
            liveness_clock=utc_now,
            liveness_config=TerminalCatalogLivenessConfig(),
            capability_catalog=HarnessCapabilityCatalog(self.tmp),
            authorization=self.resolver,
        )
        self.cursor = LibraryCursorAuthority(mint_signing_key())
        self.caller = self.resolver.resolve(client_host="127.0.0.1")
        self.gates = _Gates()
        self.shared = LibraryShared(
            cursor_authority=self.cursor,
            gates=self.gates,  # type: ignore[arg-type]
            helper_host=None,  # type: ignore[arg-type]
            open_ledger=OpenOperationLedger(),
        )
        self.port = _FakePort("pi", self.cursor, self.caller, self.tmp)
        self.opener_calls: list[Mapping[str, object]] = []
        self.app = FastAPI()
        register_conversation_routes(self.app, self.runtime)
        self.client = AsgiClient(self.app)
        self._patches = [
            mock.patch.object(factories, "library_shared", lambda _runtime: self.shared),
            mock.patch.object(factories, "build_port", lambda *_a, **_k: self.port),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._tmpdir.cleanup()

    # -- list / read ---------------------------------------------------------

    async def test_list_route_returns_wire_page_and_authorizes_scope(self) -> None:
        status, body = await asgi_request(
            self.client, "GET", "/api/harnesses/pi/conversations", query={"limit": "5"}
        )
        assert status == 200, body
        assert body["scope"]["harnessId"] == "pi"  # type: ignore[index]
        row = body["rows"][0]  # type: ignore[index]
        assert row["conversationKey"].startswith("ar-lck1.")
        assert row["identityDigest"].startswith("sha256:")
        assert row["capabilities"]["list"]["state"] == "supported"
        assert body["nextCursor"] is None  # type: ignore[index]
        call = self.port.list_calls[0]
        assert call["limit"] == 5
        assert call["scope"].canonical_project_scope == str(self.tmp.resolve())

    async def test_list_route_rejects_scope_escapes_and_unknown_harness(self) -> None:
        for query in ({"cwd": ".."}, {"cwd": "/etc"}, {"cwd": "missing"}):
            status, body = await asgi_request(
                self.client, "GET", "/api/harnesses/pi/conversations", query=query
            )
            assert status == 403, (query, body)
            assert body["status"] == "scope-denied"  # type: ignore[index]
        status, body = await asgi_request(self.client, "GET", "/api/harnesses/tmux/conversations")
        assert status == 404
        assert body["status"] == "unknown-harness"  # type: ignore[index]

    # -- open -----------------------------------------------------------------

    def _mint_key(self) -> str:
        scope = canonical_library_scope(self.caller, "pi", None, workspace_root=self.tmp)
        digest = self.cursor.identity_digest("pi", "vendor-1", scope.canonical_project_scope)
        return str(
            self.cursor.mint_conversation_key(
                scope,
                vendor_conversation_id="vendor-1",
                identity_digest=digest,
                catalog_generation=2,
            )
        )

    def _digest(self) -> str:
        scope = canonical_library_scope(self.caller, "pi", None, workspace_root=self.tmp)
        return self.cursor.identity_digest("pi", "vendor-1", scope.canonical_project_scope)

    def _opener(self, **kwargs: object) -> OpenTerminalResult:
        self.opener_calls.append(kwargs)
        session_id = str(kwargs["session_id"])
        tmux_name = f"tmux-{session_id}"
        self.host.sessions.add(tmux_name)
        entry = TerminalCatalogEntry(
            id=session_id,
            label="open",
            kind="harness",
            harness="pi",
            lifecycle_id=None,
            cwd=self.tmp,
            tmux_name=tmux_name,
            command=("pi",),
            created_at="2026-07-18T00:00:00Z",
            last_attached_at="2026-07-18T00:00:00Z",
            status="running",
            control_endpoint=Path("/tmp/endpoint.sock"),
        )
        self.catalog.upsert(entry)
        return OpenTerminalResult(status="opened", entry=entry, kind="harness")

    def _open_service_patches(self, vendor: str | None = "vendor-1"):
        def _readiness(*_args, **kwargs):
            session_id = kwargs.get("session_id", "x")
            return HostedReadinessResult(
                "ready",
                session_id,
                entry=self.catalog.get(session_id),
                snapshot=_snapshot(vendor),
            )

        return [
            mock.patch.object(open_module, "hosted_session_readiness", _readiness),
            mock.patch.object(
                open_module,
                "_read_submission_authority",
                lambda _runtime, _entry: SubmissionAuthorityDescriptor(bridge_epoch="epoch-1"),
            ),
            mock.patch.object(
                library_api,
                "build_open_service",
                lambda runtime, authorization: open_module.ConversationOpenService(
                    LibraryBinding(
                        runtime=runtime, shared=self.shared, authorization=authorization
                    ),
                    library=ConversationLibraryService(
                        runtime=runtime,
                        shared=self.shared,
                        authorization=authorization,
                        port_builder=lambda _h: self.port,  # type: ignore[arg-type]
                    ),
                    port_builder=lambda _h: self.port,
                    opener=self._opener,
                    proof_wait_seconds=0.01,
                ),
            ),
        ]

    async def test_open_created_replays_and_focuses_only_proven_identity(self) -> None:
        key = self._mint_key()
        patches = self._open_service_patches()
        for patch in patches:
            patch.start()
        try:
            payload = {"requestId": "req-1", "expectedIdentityDigest": self._digest()}
            status, body = await asgi_request(
                self.client, "POST", f"/api/harnesses/pi/conversations/{key}/open", body=payload
            )
            assert status == 201, body
            assert body["outcome"] == "opened"  # type: ignore[index]
            assert body["phase"] == "opened"  # type: ignore[index]
            assert body["identity"]["vendorConversationId"] == "vendor-1"  # type: ignore[index]
            assert body["identity"]["bridgeEpoch"] == "epoch-1"  # type: ignore[index]
            assert body["rollback"] == "not-needed"  # type: ignore[index]
            assert len(self.opener_calls) == 1

            replay = await asgi_request(
                self.client, "POST", f"/api/harnesses/pi/conversations/{key}/open", body=payload
            )
            assert replay == (status, body)
            assert len(self.opener_calls) == 1

            conflict = await asgi_request(
                self.client,
                "POST",
                f"/api/harnesses/pi/conversations/{key}/open",
                body={
                    **payload,
                    "launchContext": {
                        "taskDocumentRef": {
                            "repository": "repo",
                            "path": "master/other.json",
                        },
                        "seatRole": "worker",
                    },
                },
            )
            assert conflict[0] == 409
            assert conflict[1]["status"] == "request-conflict"  # type: ignore[index]
            assert len(self.opener_calls) == 1
        finally:
            for patch in patches:
                patch.stop()


if __name__ == "__main__":
    unittest.main()
