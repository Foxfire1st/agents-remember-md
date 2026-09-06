from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agents_remember.errors import HarnessControlError
from agents_remember.models.conversations.control_wire import (
    SubmissionReceipt,
)
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
)
from agents_remember.serving.conversation.authorization import LocalOperatorAuthorizationResolver
from agents_remember.serving.conversation.runtime import ConversationRuntime, ConversationScope
from agents_remember.serving.harness_capability_catalog import HarnessCapabilityCatalog
from agents_remember.serving.harness_control_api import register_harness_control_routes
from agents_remember.serving.harness_control_bridge import HarnessControlBridge
from agents_remember.serving.harness_control_client import (
    ControlPlaneClient,
    ControlSubmission,
    read_submission_authority,
    read_submission_status,
    reconcile_control_prompt,
    submit_control_prompt,
    withdraw_control_submission,
)
from agents_remember.serving.harness_control_ipc import (
    HarnessControlClient,
    HarnessControlServer,
    LocalControlEndpoint,
)
from agents_remember.serving.harness_control_models import (
    CONTROL_PROTOCOL_VERSION,
)
from agents_remember.serving.terminal import TerminalHost, TerminalHostSeams
from agents_remember.serving.terminal_catalog import (
    TerminalCatalog,
)
from agents_remember.serving.terminal_liveness import (
    TerminalCatalogLivenessConfig,
    TerminalLivenessObservation,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_harness_control import (
    _BlockingSubmitAdapter,
    _ControlledEntry,
    _DropFirstSubmitResponseServer,
    _FakeAdapter,
    _identity,
    _launch,
)


class HarnessControlIpcTests(unittest.IsolatedAsyncioTestCase):
    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_harness_control_ipc.py:78).
    async def test_private_lifecycle_status_and_withdraw_round_trip(
        self,
    ) -> None:  # pragma: no cover
        with tempfile.TemporaryDirectory() as tmp_str:
            identity = _identity("lifecycle-ipc")
            adapter = _BlockingSubmitAdapter()
            bridge = HarnessControlBridge(identity, adapter)
            await bridge.start(_launch(identity))
            endpoint = LocalControlEndpoint.for_session(Path(tmp_str), identity)
            server = HarnessControlServer(endpoint, bridge)
            await server.start()
            entry = _ControlledEntry(
                identity.ar_session_id,
                identity.tmux_name,
                identity.created_at,
                endpoint.path,
            )
            first_task: asyncio.Task[SubmissionReceipt] | None = None
            try:
                descriptor = await asyncio.to_thread(read_submission_authority, entry)
                first_task = asyncio.create_task(
                    asyncio.to_thread(
                        submit_control_prompt,
                        entry,
                        "hold the ordinary lane",
                        ControlSubmission(source="durable", request_id="active-durable"),
                    )
                )
                await asyncio.wait_for(adapter.submit_started.wait(), timeout=1.0)
                queued = await asyncio.to_thread(
                    submit_control_prompt,
                    entry,
                    "withdraw this exact text",
                    ControlSubmission(
                        source="cockpit",
                        request_id="cockpit-queued",
                        expected_bridge_epoch=descriptor.bridge_epoch,
                    ),
                )
                self.assertEqual(queued.acceptance, "queued")

                status = await asyncio.to_thread(
                    read_submission_status,
                    entry,
                    expected_bridge_epoch=descriptor.bridge_epoch,
                    request_ids=("cockpit-queued", "missing"),
                )
                queued_status = status.submissions[0].submission
                self.assertIsNotNone(queued_status)
                assert queued_status is not None
                self.assertEqual(
                    (queued_status.state, queued_status.withdrawable), ("queued", True)
                )
                self.assertEqual(status.submissions[1].outcome, "not-found")

                withdrawn = await asyncio.to_thread(
                    withdraw_control_submission,
                    entry,
                    expected_bridge_epoch=descriptor.bridge_epoch,
                    request_id="cockpit-queued",
                )
                self.assertEqual((withdrawn.outcome, withdrawn.state), ("withdrawn", "withdrawn"))
            finally:
                adapter.release_submit.set()
                if first_task is not None:
                    await first_task
                await server.close()
                await bridge.stop("forced")

    async def test_outer_socket_lost_receipt_reconciles_retained_known_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            identity = _identity("outer-loss")
            adapter = _FakeAdapter()
            bridge = HarnessControlBridge(identity, adapter)
            await bridge.start(_launch(identity))
            endpoint = LocalControlEndpoint.for_session(Path(tmp_str), identity)
            server = _DropFirstSubmitResponseServer(endpoint, bridge)
            await server.start()
            entry = _ControlledEntry(
                identity.ar_session_id,
                identity.tmux_name,
                identity.created_at,
                endpoint.path,
            )
            try:
                receipt = await asyncio.to_thread(
                    submit_control_prompt,
                    entry,
                    "one complete message",
                    ControlSubmission(source="durable", request_id="outer-loss-request"),
                )
                self.assertEqual(receipt.acceptance, "unknown")
                self.assertEqual(receipt.request_id, "outer-loss-request")
                await asyncio.wait_for(server.dropped.wait(), timeout=1.0)

                reconciled = await asyncio.to_thread(
                    reconcile_control_prompt, entry, "outer-loss-request"
                )

                self.assertEqual(reconciled.state, "accepted")
                self.assertEqual(
                    reconciled.vendor_correlation_id,
                    "vendor-outer-loss-request",
                )
                self.assertEqual(len(adapter.submissions), 1)
                self.assertEqual(adapter.reconciliation_requests, [])
            finally:
                await server.close()
                await bridge.stop("forced")

    async def test_public_duplicate_returns_retained_result_with_one_adapter_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            root = Path(tmp_str)
            identity = _identity("api-idempotent")
            adapter = _BlockingSubmitAdapter()
            bridge = HarnessControlBridge(identity, adapter)
            await bridge.start(_launch(identity))
            bridge_epoch = bridge.submissions().bridge_epoch
            endpoint = LocalControlEndpoint.for_session(root / "control", identity)
            server = HarnessControlServer(endpoint, bridge)
            await server.start()
            catalog = TerminalCatalog(root / "terminal-sessions.json")
            entry = TerminalCatalogEntry(
                id=identity.ar_session_id,
                label="Worker",
                kind="harness",
                harness="claude",
                lifecycle_id=None,
                cwd=root,
                tmux_name=identity.tmux_name,
                command=("claude",),
                created_at=identity.created_at,
                last_attached_at=identity.created_at,
                status="running",
                control_state="ready",
                control_endpoint=endpoint.path,
                control_protocol=CONTROL_PROTOCOL_VERSION,
            )
            catalog.upsert(entry)
            app = FastAPI()
            register_harness_control_routes(
                app,
                ConversationRuntime(
                    scope=ConversationScope(workspace_root=root, coordination_root=root),
                    harness_registry=lambda: (),
                    catalog=catalog,
                    control_plane=ControlPlaneClient(),
                    host=TerminalHost(TerminalHostSeams(tmux_probe=lambda _name: True)),
                    liveness_clock=lambda: datetime(2026, 7, 16, 8, 0, tzinfo=UTC),
                    liveness_config=TerminalCatalogLivenessConfig(),
                    capability_catalog=HarnessCapabilityCatalog(root),
                    authorization=LocalOperatorAuthorizationResolver.for_workspace(root),
                ),
            )
            try:
                with (
                    mock.patch(
                        "agents_remember.serving.harness_control_api.observe_terminal_liveness",
                        return_value=TerminalLivenessObservation(entry, True),
                    ),
                    TestClient(app) as client,
                ):
                    first_call = asyncio.create_task(
                        asyncio.to_thread(
                            client.post,
                            f"/api/terminal/{identity.ar_session_id}/submit",
                            json={
                                "requestId": "same-id",
                                "text": "first payload",
                                "expectedBridgeEpoch": bridge_epoch,
                            },
                        )
                    )
                    await asyncio.wait_for(adapter.submit_started.wait(), timeout=5.0)
                    duplicate_call = asyncio.create_task(
                        asyncio.to_thread(
                            client.post,
                            f"/api/terminal/{identity.ar_session_id}/submit",
                            json={
                                "requestId": "same-id",
                                "text": "first payload",
                                "expectedBridgeEpoch": bridge_epoch,
                            },
                        )
                    )
                    duplicate = await asyncio.wait_for(duplicate_call, timeout=5.0)
                    adapter.release_submit.set()
                    first = await first_call
                    reconciled = await asyncio.to_thread(
                        client.post,
                        f"/api/terminal/{identity.ar_session_id}/reconcile",
                        json={
                            "requestId": "same-id",
                            "expectedBridgeEpoch": bridge_epoch,
                        },
                    )

                self.assertEqual((first.status_code, duplicate.status_code), (200, 200))
                self.assertEqual(first.json()["acceptance"], "immediate")
                self.assertEqual(duplicate.json()["acceptance"], "unknown")
                self.assertEqual(reconciled.status_code, 200)
                self.assertEqual(reconciled.json()["state"], "accepted")
                self.assertEqual(reconciled.json()["vendorCorrelationId"], "vendor-same-id")
                self.assertEqual(len(adapter.submissions), 1)
                self.assertEqual(adapter.submissions[0].text, "first payload")
                self.assertEqual(adapter.reconciliation_requests, [])
            finally:
                adapter.release_submit.set()
                await server.close()
                await bridge.stop("forced")

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_harness_control_ipc.py:566).

    async def test_private_endpoint_exact_identity_and_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            identity = _identity()
            adapter = _FakeAdapter()
            bridge = HarnessControlBridge(identity, adapter)
            await bridge.start(_launch(identity))
            endpoint = LocalControlEndpoint.for_session(Path(tmp_str) / "control", identity)
            server = HarnessControlServer(endpoint, bridge)
            await server.start()
            try:
                self.assertEqual(stat.S_IMODE(endpoint.path.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(endpoint.path.stat().st_mode), 0o600)

                client = HarnessControlClient(endpoint)
                handshake = await client.request("handshake")
                assert isinstance(handshake, dict)
                self.assertEqual(handshake["protocol"], CONTROL_PROTOCOL_VERSION)
                result = await client.request(
                    "submit",
                    {
                        "requestId": "ipc-request-1",
                        "source": "durable",
                        "text": "hello",
                        "submittedAt": "2026-07-13T18:00:00+00:00",
                    },
                )
                assert isinstance(result, dict)
                self.assertEqual(result["acceptance"], "immediate")

                wrong = HarnessControlClient(
                    LocalControlEndpoint(path=endpoint.path, identity=_identity("wrong"))
                )
                with self.assertRaisesRegex(HarnessControlError, "identity"):
                    await wrong.request("snapshot")
            finally:
                await server.close()
                await bridge.stop("forced")

    async def test_malformed_ipc_request_is_rejected_without_control_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            identity = _identity()
            adapter = _FakeAdapter()
            bridge = HarnessControlBridge(identity, adapter)
            await bridge.start(_launch(identity))
            endpoint = LocalControlEndpoint.for_session(Path(tmp_str), identity)
            server = HarnessControlServer(endpoint, bridge)
            await server.start()
            try:
                reader, writer = await asyncio.open_unix_connection(endpoint.path)
                writer.write(b"not-json\n")
                await writer.drain()
                response = json.loads(await reader.readline())
                writer.close()
                await writer.wait_closed()
                self.assertFalse(response["ok"])
                self.assertEqual(adapter.submissions, [])
            finally:
                await server.close()
                await bridge.stop("forced")
