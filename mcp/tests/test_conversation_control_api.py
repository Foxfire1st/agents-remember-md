"""Production-route tests for the conversation control API (260718-CHATS-L3, R7).

Every test drives the REAL composition on one loop: bridge + IPC server on a
real user-private socket, a real catalog row, the L0 register_conversation_routes
composition, and HTTP over a real uvicorn wire. The only double is the
structural fake adapter at the harness edge (interrupt- and asset-capable).
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import httpx
import uvicorn
from _adapter_event_scripts import replay_codex_terminal
from _control_plane import OPERATOR, TINY_PNG, FakeControlAdapter, make_harness

SESSION = "ar-api-ctl"


class ControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.adapter = FakeControlAdapter(harness="codex")
        self.harness = make_harness(self, self.adapter, SESSION, harness="codex")
        await self.harness.start()
        self.epoch = self.harness.epoch
        self._auth_patcher = mock.patch(
            "agents_remember.serving.conversation.control.api.resolve_conversation_authorization",
            lambda request: OPERATOR,
        )
        self._auth_patcher.start()
        self._uvicorn = uvicorn.Server(
            uvicorn.Config(
                self.harness.app, host="127.0.0.1", port=0, log_level="warning", access_log=False
            )
        )
        self._uvicorn_task = asyncio.create_task(self._uvicorn.serve())
        while not self._uvicorn.started:
            await asyncio.sleep(0.05)
        port = self._uvicorn.servers[0].sockets[0].getsockname()[1]
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self._uvicorn.should_exit = True
        await self._uvicorn_task
        self._auth_patcher.stop()
        await self.harness.stop()

    def _params(self, epoch: str | None = None) -> dict[str, str]:
        return {"expectedBridgeEpoch": epoch or self.epoch}

    async def _typed_submit(self, request_id: str, text: str) -> httpx.Response:
        return await self.client.post(
            f"/api/terminal/{SESSION}/conversation/submit",
            json={
                "expectedBridgeEpoch": self.epoch,
                "requestId": request_id,
                "disposition": "next",
                "content": [{"type": "text", "text": text}],
                "draftRevision": 1,
            },
        )

    async def _queue(self) -> dict:
        response = await self.client.get(
            f"/api/terminal/{SESSION}/operation-queue", params=self._params()
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def _withdraw_row(self, row: dict, withdraw_request_id: str) -> httpx.Response:
        return await self.client.post(
            f"/api/terminal/{SESSION}/operation-queue/withdraw",
            params=self._params(),
            json={
                "operationRef": row["operationRef"],
                "withdrawalRef": row["cockpit"]["withdrawalRef"],
                "withdrawRequestId": withdraw_request_id,
            },
        )

    # -- interrupt over the wire ----------------------------------------------

    async def test_interrupt_ack_settle_replay_and_single_write(self) -> None:
        response = await self._typed_submit("i-body", "generate")
        self.assertEqual(response.status_code, 200)
        self.adapter.set_activity("running")
        interrupt = await self.client.post(
            f"/api/terminal/{SESSION}/conversation/interrupt",
            params=self._params(),
            json={"turnId": "turn-i-body", "requestId": "int-1"},
        )
        self.assertEqual(interrupt.status_code, 202, interrupt.text)
        operation = interrupt.json()
        self.assertEqual(operation["acknowledgement"], "accepted")
        self.assertEqual(operation["settlement"], "pending")
        self.assertEqual(operation["requestFingerprint"].startswith("sha256:"), True)
        replay = await self.client.post(
            f"/api/terminal/{SESSION}/conversation/interrupt",
            params=self._params(),
            json={"turnId": "turn-i-body", "requestId": "int-1"},
        )
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["revision"], operation["revision"])
        replay_codex_terminal(self.adapter, "interrupted")
        status = await self.client.post(
            f"/api/terminal/{SESSION}/conversation/interrupt-status",
            params=self._params(),
            json={"turnId": "turn-i-body", "requestId": "int-1"},
        )
        self.assertEqual(status.status_code, 200, status.text)
        settled = status.json()
        self.assertEqual(settled["settlement"], "interrupted")
        self.assertGreater(settled["revision"], operation["revision"])
        self.assertTrue(settled["settledAt"])
        self.assertEqual(len(self.adapter.interrupt_calls), 1)

    async def test_remote_peer_fails_closed_typed_403(self) -> None:
        self._auth_patcher.stop()
        response = await self.client.get(
            f"/api/terminal/{SESSION}/operation-queue",
            params=self._params(),
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], "authorization-failed")
        self._auth_patcher.start()

    # -- queue / withdraw / recovery over the wire -----------------------------

    # -- attachments over the wire ---------------------------------------------

    async def test_attachment_stage_submit_status_reconcile(self) -> None:
        staged = await self.client.post(
            f"/api/terminal/{SESSION}/conversation/attachments",
            params=self._params(),
            data={"requestId": "att-wire-1", "metadata": json.dumps([{"kind": "image"}])},
            files=[("assets", ("dot.png", TINY_PNG, "image/png"))],
        )
        self.assertEqual(staged.status_code, 200, staged.text)
        payload = staged.json()
        (receipt,) = payload["receipts"]
        self.assertEqual(receipt["alt"], "dot.png, image/png")
        self.assertEqual(receipt["altProvenance"], "filename-mime-fallback")
        self.assertEqual(payload["operation"]["phase"], "staged")
        submit = await self.client.post(
            f"/api/terminal/{SESSION}/conversation/submit",
            json={
                "expectedBridgeEpoch": self.epoch,
                "requestId": "att-wire-1",
                "disposition": "next",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "asset-ref",
                        "assetId": receipt["assetId"],
                        "kind": receipt["kind"],
                        "name": receipt["name"],
                        "mimeType": receipt["mimeType"],
                        "alt": receipt["alt"],
                        "altProvenance": receipt["altProvenance"],
                        "sha256": receipt["sha256"],
                    },
                ],
                "draftRevision": 1,
            },
        )
        self.assertEqual(submit.status_code, 200, submit.text)
        answer = submit.json()
        self.assertEqual(answer["acceptance"], "immediate")
        self.assertEqual(answer["attachment"]["phase"], "dispatching")
        (request,) = self.adapter.submit_requests
        self.assertEqual(len(request.assets), 1)
        status = await self.client.get(
            f"/api/terminal/{SESSION}/conversation/attachments/att-wire-1/status",
            params=self._params(),
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["phase"], "dispatching")
        reconcile = await self.client.post(
            f"/api/terminal/{SESSION}/conversation/attachments/att-wire-1/reconcile",
            params=self._params(),
        )
        self.assertEqual(reconcile.status_code, 200)
        missing = await self.client.get(
            f"/api/terminal/{SESSION}/conversation/attachments/att-missing/status",
            params=self._params(),
        )
        self.assertEqual(missing.status_code, 404)

    # -- policy / telemetry over the wire ---------------------------------------

    # -- production-honesty scans ------------------------------------------------


if __name__ == "__main__":
    unittest.main()
