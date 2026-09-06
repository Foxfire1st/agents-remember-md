"""Attachment lifecycle, policy, and telemetry contract tests (260718-CHATS-L3, R4/R5/R6/R7).

Real composition up to the harness edge (bridge + IPC + real authority + the
L2E asset channel + the user-private spool); the only double is the structural
fake adapter. Bytes are tiny; every limit is exercised at its boundary.
"""

from __future__ import annotations

import asyncio
import unittest

from _control_plane import OPERATOR, TINY_PNG, FakeControlAdapter, drive_activity, make_harness
from agents_remember.models.conversations.submissions import (
    AssetSubmitBlock,
    ConversationSubmitRequest,
    TextSubmitBlock,
)
from agents_remember.models.conversations.withdrawals import (
    WithdrawnQueueResponse,
)
from agents_remember.serving.conversation.control import (
    attachments,
    queue_projection,
    withdrawals,
)
from agents_remember.serving.conversation.control.capabilities import (
    control_capabilities_for,
)
from agents_remember.serving.conversation.control.service import (
    CapabilityRefusedError,
    ControlRequest,
    OperationConflictError,
    OperationRejectedError,
)

SESSION = "ar-attach-1"


def _png(name: str = "dot.png", alt: str | None = None, data: bytes = TINY_PNG):
    return attachments.StagedUpload(
        kind="image", name=name, mime_type="image/png", alt=alt, data=data
    )


def _submit_body(
    request_id: str, epoch: str, receipt, text: str = "describe this"
) -> ConversationSubmitRequest:
    return ConversationSubmitRequest(
        expected_bridge_epoch=epoch,
        request_id=request_id,
        disposition="next",
        content=(
            TextSubmitBlock(type="text", text=text),
            AssetSubmitBlock(
                type="asset-ref",
                asset_id=receipt.asset_id,
                kind=receipt.kind,
                name=receipt.name,
                mime_type=receipt.mime_type,
                alt=receipt.alt,
                alt_provenance=receipt.alt_provenance,
                sha256=receipt.sha256,
            ),
        ),
        draft_revision=2,
    )


class AttachmentStageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.adapter = FakeControlAdapter(harness="codex")
        self.harness = make_harness(self, self.adapter, SESSION, harness="codex")
        await self.harness.start()
        self.service = self.harness.service
        self.epoch = self.harness.epoch
        self.caps = self._caps()

    def _caps(self):
        assert self.adapter.current is not None
        caps = control_capabilities_for("codex", self.adapter.current).attachments
        return {"image": caps.image, "file": caps.file, "resource": caps.resource}

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def _stage(self, request_id: str, uploads):
        return await attachments.stage(
            ControlRequest(
                service=self.service,
                authorization=OPERATOR,
                ar_session_id=SESSION,
                expected_bridge_epoch=self.epoch,
            ),
            request_id=request_id,
            kind_capabilities=self.caps,
            uploads=uploads,
        )

    async def test_mime_count_byte_and_kind_limits_are_typed(self) -> None:
        with self.assertRaises(OperationRejectedError):
            await self._stage(
                "at-3",
                [
                    attachments.StagedUpload(
                        kind="image", name="note.txt", mime_type="text/plain", alt=None, data=b"hi"
                    )
                ],
            )
        with self.assertRaises(OperationRejectedError):
            await self._stage("at-4", [_png() for _ in range(5)])
        oversized = attachments.StagedUpload(
            kind="image",
            name="big.png",
            mime_type="image/png",
            alt=None,
            data=b"\x00" * (5 * 1024 * 1024 + 1),
        )
        with self.assertRaises(OperationRejectedError):
            await self._stage("at-5", [oversized])
        file_kind = attachments.StagedUpload(
            kind="file", name="doc.pdf", mime_type="application/pdf", alt=None, data=b"%PDF"
        )
        with self.assertRaises(CapabilityRefusedError):
            await self._stage("at-6", [file_kind])


class AttachmentSubmitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.adapter = FakeControlAdapter(harness="codex")
        self.harness = make_harness(self, self.adapter, SESSION, harness="codex")
        await self.harness.start()
        self.service = self.harness.service
        self.epoch = self.harness.epoch
        assert self.adapter.current is not None
        caps = control_capabilities_for("codex", self.adapter.current).attachments
        self.caps = {"image": caps.image, "file": caps.file, "resource": caps.resource}

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def _stage_and_receipt(self, request_id: str):
        answer = await attachments.stage(
            ControlRequest(
                service=self.service,
                authorization=OPERATOR,
                ar_session_id=SESSION,
                expected_bridge_epoch=self.epoch,
            ),
            request_id=request_id,
            kind_capabilities=self.caps,
            uploads=[_png()],
        )
        return answer.receipts[0]

    async def test_submit_carries_refs_and_consumes_one_use(self) -> None:
        receipt = await self._stage_and_receipt("at-s1")
        answer = await attachments.submit(
            self.service, OPERATOR, SESSION, body=_submit_body("at-s1", self.epoch, receipt)
        )
        self.assertEqual(answer.acceptance, "immediate")
        assert answer.attachment is not None
        self.assertEqual(answer.attachment.phase, "dispatching")
        self.assertTrue(answer.operation_ref and answer.operation_ref.startswith("ar-oqr1."))
        (request,) = self.adapter.submit_requests
        self.assertEqual(len(request.assets), 1)
        self.assertEqual(request.assets[0].asset_id, receipt.asset_id)
        replay = await attachments.submit(
            self.service, OPERATOR, SESSION, body=_submit_body("at-s1", self.epoch, receipt)
        )
        self.assertEqual(replay.acceptance, "immediate")
        self.assertEqual(len(self.adapter.submit_requests), 1)
        with self.assertRaises(OperationConflictError):
            await attachments.submit(
                self.service,
                OPERATOR,
                SESSION,
                body=_submit_body("at-s1", self.epoch, receipt, text="changed content"),
            )

    async def test_tampered_asset_block_is_rejected_before_dispatch(self) -> None:
        receipt = await self._stage_and_receipt("at-s2")
        body = _submit_body("at-s2", self.epoch, receipt)
        asset_block = next(block for block in body.content if block.type == "asset-ref")
        tampered = asset_block.model_copy(update={"sha256": "0" * 64})
        body = body.model_copy(update={"content": (body.content[0], tampered)})
        with self.assertRaises(OperationRejectedError):
            await attachments.submit(self.service, OPERATOR, SESSION, body=body)
        self.assertEqual(len(self.adapter.submit_requests), 0)

    async def test_double_use_of_one_asset_is_typed(self) -> None:
        receipt = await self._stage_and_receipt("at-s3")
        await attachments.submit(
            self.service, OPERATOR, SESSION, body=_submit_body("at-s3", self.epoch, receipt)
        )
        with self.assertRaises(OperationConflictError):
            await attachments.submit(
                self.service,
                OPERATOR,
                SESSION,
                body=_submit_body("at-s3", self.epoch, receipt, text="again"),
            )
        self.assertEqual(len(self.adapter.submit_requests), 1)


class AttachmentRebindTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.adapter = FakeControlAdapter(harness="codex")
        self.harness = make_harness(self, self.adapter, SESSION, harness="codex")
        await self.harness.start()
        self.service = self.harness.service
        self.epoch = self.harness.epoch
        assert self.adapter.current is not None
        caps = control_capabilities_for("codex", self.adapter.current).attachments
        self.caps = {"image": caps.image, "file": caps.file, "resource": caps.resource}

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def _withdraw_with_asset(self):
        await drive_activity(self.harness, "running")
        staged = await attachments.stage(
            ControlRequest(
                service=self.service,
                authorization=OPERATOR,
                ar_session_id=SESSION,
                expected_bridge_epoch=self.epoch,
            ),
            request_id="at-r1",
            kind_capabilities=self.caps,
            uploads=[_png()],
        )
        await attachments.submit(
            self.service,
            OPERATOR,
            SESSION,
            body=_submit_body("at-r1", self.epoch, staged.receipts[0]),
        )
        await attachments.submit(
            self.service,
            OPERATOR,
            SESSION,
            body=ConversationSubmitRequest(
                expected_bridge_epoch=self.epoch,
                request_id="at-r2",
                disposition="next",
                content=(TextSubmitBlock(type="text", text="recover me with my asset"),),
                draft_revision=1,
            ),
        )
        projection = await queue_projection.operation_queue(
            self.service, OPERATOR, SESSION, expected_bridge_epoch=self.epoch
        )
        row = next(item for item in projection.items if item.phase == "queued")
        assert row.cockpit is not None
        response = await withdrawals.withdraw(
            ControlRequest(
                service=self.service,
                authorization=OPERATOR,
                ar_session_id=SESSION,
                expected_bridge_epoch=self.epoch,
            ),
            operation_ref=row.operation_ref,
            withdrawal_ref=row.cockpit.withdrawal_ref,
            withdraw_request_id="wd-r1",
        )
        return staged, response

    async def test_withdraw_marks_recoverable_and_rebind_exchanges_one_use(self) -> None:

        _staged, response = await self._withdraw_with_asset()
        assert isinstance(response, WithdrawnQueueResponse)
        self.assertEqual(len(response.recovery.attachments), 1)
        recovery_asset = response.recovery.attachments[0]
        self.assertEqual(recovery_asset.alt, "dot.png, image/png")
        answer = await attachments.rebind(
            ControlRequest(
                service=self.service,
                authorization=OPERATOR,
                ar_session_id=SESSION,
                expected_bridge_epoch=self.epoch,
            ),
            recovery_asset_ref=recovery_asset.recovery_asset_ref,
            request_id="at-r3",
        )
        (new_receipt,) = answer.receipts
        self.assertEqual(new_receipt.request_id, "at-r3")
        self.assertNotEqual(new_receipt.asset_id, _staged.receipts[0].asset_id)
        new_path = self.harness.endpoint.path.parent / "assets" / "at-r3" / new_receipt.asset_id
        self.assertTrue(new_path.exists())
        replay = await attachments.rebind(
            ControlRequest(
                service=self.service,
                authorization=OPERATOR,
                ar_session_id=SESSION,
                expected_bridge_epoch=self.epoch,
            ),
            recovery_asset_ref=recovery_asset.recovery_asset_ref,
            request_id="at-r3",
        )
        self.assertEqual(replay.receipts[0].asset_id, new_receipt.asset_id)
        with self.assertRaises(OperationConflictError):
            await attachments.rebind(
                ControlRequest(
                    service=self.service,
                    authorization=OPERATOR,
                    ar_session_id=SESSION,
                    expected_bridge_epoch=self.epoch,
                ),
                recovery_asset_ref=recovery_asset.recovery_asset_ref,
                request_id="at-r4",
            )
        # The rebound asset resubmits natively through the asset channel.
        answer2 = await attachments.submit(
            self.service,
            OPERATOR,
            SESSION,
            body=ConversationSubmitRequest(
                expected_bridge_epoch=self.epoch,
                request_id="at-r3",
                disposition="next",
                content=(
                    TextSubmitBlock(type="text", text="resubmitted"),
                    AssetSubmitBlock(
                        type="asset-ref",
                        asset_id=new_receipt.asset_id,
                        kind=new_receipt.kind,
                        name=new_receipt.name,
                        mime_type=new_receipt.mime_type,
                        alt=new_receipt.alt,
                        alt_provenance=new_receipt.alt_provenance,
                        sha256=new_receipt.sha256,
                    ),
                ),
                draft_revision=1,
            ),
        )
        self.assertIn(answer2.acceptance, {"immediate", "queued"})
        # The session is busy, so the resubmit queued; driving the lane idle
        # dispatches it through the asset channel with the rebound identity.
        self.adapter.auto_release = True
        self.adapter.set_activity("idle")
        deadline = asyncio.get_running_loop().time() + 5.0
        while len(self.adapter.submit_requests) < 2:
            if asyncio.get_running_loop().time() > deadline:
                self.fail("the resubmitted prompt never dispatched")
            await asyncio.sleep(0.05)
        self.assertEqual(self.adapter.submit_requests[-1].assets[0].asset_id, new_receipt.asset_id)


class AttachmentReconcileTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.adapter = FakeControlAdapter(harness="codex")
        self.harness = make_harness(self, self.adapter, SESSION, harness="codex")
        await self.harness.start()
        self.service = self.harness.service
        self.epoch = self.harness.epoch
        assert self.adapter.current is not None
        caps = control_capabilities_for("codex", self.adapter.current).attachments
        self.caps = {"image": caps.image, "file": caps.file, "resource": caps.resource}

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def _stage_submit_and_status(self, request_id: str, *, reconcile: bool):
        staged = await attachments.stage(
            ControlRequest(
                service=self.service,
                authorization=OPERATOR,
                ar_session_id=SESSION,
                expected_bridge_epoch=self.epoch,
            ),
            request_id=request_id,
            kind_capabilities=self.caps,
            uploads=[_png()],
        )
        await attachments.submit(
            self.service,
            OPERATOR,
            SESSION,
            body=_submit_body(request_id, self.epoch, staged.receipts[0]),
        )
        return await attachments.attachment_status(
            ControlRequest(
                service=self.service,
                authorization=OPERATOR,
                ar_session_id=SESSION,
                expected_bridge_epoch=self.epoch,
            ),
            request_id=request_id,
            reconcile=reconcile,
        )

    async def test_unknown_outcome_is_retained_and_never_cleaned(self) -> None:
        self.adapter.next_acceptance = "unknown"
        staged = await attachments.stage(
            ControlRequest(
                service=self.service,
                authorization=OPERATOR,
                ar_session_id=SESSION,
                expected_bridge_epoch=self.epoch,
            ),
            request_id="at-t2",
            kind_capabilities=self.caps,
            uploads=[_png()],
        )
        await attachments.submit(
            self.service,
            OPERATOR,
            SESSION,
            body=_submit_body("at-t2", self.epoch, staged.receipts[0]),
        )
        projection = await attachments.attachment_status(
            ControlRequest(
                service=self.service,
                authorization=OPERATOR,
                ar_session_id=SESSION,
                expected_bridge_epoch=self.epoch,
            ),
            request_id="at-t2",
            reconcile=True,
        )
        self.assertEqual(projection.phase, "unknown")
        self.assertEqual(projection.outcome, "unknown")
        spool_dir = self.harness.endpoint.path.parent / "assets" / "at-t2"
        self.assertTrue(spool_dir.exists())


if __name__ == "__main__":
    unittest.main()
