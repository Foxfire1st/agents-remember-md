"""Deterministic races for the bridge-owned submission/setter authority."""

from __future__ import annotations

import asyncio
import sys
import unittest
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.errors import (
    HarnessAdapterBusyError,
    HarnessBridgeEpochMismatchError,
    HarnessControlError,
    HarnessRequestConflictError,
)
from agents_remember.models.conversations.control_wire import (
    AdapterSnapshot,
    ControlIdentity,
    ControlOperationRef,
    LaunchSpec,
    SubmissionReceipt,
)
from agents_remember.serving.harness_capabilities import CapabilitySnapshot, SetResult
from agents_remember.serving.harness_control_models import (
    CONTROL_PROTOCOL_VERSION,
    REQUIRED_ADAPTER_CAPABILITIES,
    AdapterEvent,
    AdapterHandshake,
    InteractionResponse,
    PromptRequest,
    ReconciliationResult,
    ShutdownMode,
)
from agents_remember.serving.harness_submission_authority import (
    DISPATCH_ACCEPTANCE_GRACE_SECONDS,
    BridgeSnapshotPort,
    HarnessSubmissionAuthority,
    SubmissionLimits,
)
from agents_remember.serving.harness_submission_ledger import (
    OperationRecord,
    SubmissionLedger,
)

NOW = "2026-07-17T12:00:00+00:00"


def _identity() -> ControlIdentity:
    return ControlIdentity("session-1", "ar-session-1", NOW)


class _AuthorityAdapter:
    def __init__(self) -> None:
        self.current = AdapterSnapshot(
            identity=_identity(),
            control="ready",
            activity="idle",
            acceptance="immediate",
        )
        self.submit_results: deque[SubmissionReceipt | Exception] = deque()
        self.set_results: deque[SetResult | Exception] = deque()
        self.submissions: list[PromptRequest] = []
        self.set_operations: list[tuple[str, str, ControlOperationRef | None]] = []
        self.responses: list[InteractionResponse] = []
        self.submit_started = asyncio.Event()
        self.release_submit = asyncio.Event()
        self.block_submit = False
        self.set_started = asyncio.Event()
        self.release_set = asyncio.Event()
        self.block_set = False
        self.stop_modes: list[ShutdownMode] = []
        self.preflight_results: deque[Exception] = deque()
        self.reconcile_results: deque[ReconciliationResult] = deque()
        self.preflight_started = asyncio.Event()
        self.release_preflight = asyncio.Event()
        self.block_preflight = False

    async def start(self, launch: LaunchSpec) -> AdapterHandshake:
        return AdapterHandshake(
            protocol_version=CONTROL_PROTOCOL_VERSION,
            adapter_id="authority-fake",
            identity=launch.identity,
            capabilities=REQUIRED_ADAPTER_CAPABILITIES,
            snapshot=self.current,
        )

    def advertise(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(models=(), selected_model_key=None, selected_effort=None)

    async def set_model(
        self, model_key: str, *, operation: ControlOperationRef | None = None
    ) -> SetResult:
        self.set_operations.append(("set-model", model_key, operation))
        self.set_started.set()
        if self.block_set:
            await self.release_set.wait()
        result = (
            self.set_results.popleft()
            if self.set_results
            else SetResult(
                ok=True,
                acceptance="immediate",
                requested_value=model_key,
            )
        )
        if isinstance(result, Exception):
            raise result
        return result

    async def set_effort(
        self, effort: str, *, operation: ControlOperationRef | None = None
    ) -> SetResult:
        self.set_operations.append(("set-effort", effort, operation))
        result = (
            self.set_results.popleft()
            if self.set_results
            else SetResult(
                ok=True,
                acceptance="immediate",
                requested_value=effort,
            )
        )
        if isinstance(result, Exception):
            raise result
        return result

    async def snapshot(self) -> AdapterSnapshot:
        return self.current

    async def preflight_operation(self, operation: ControlOperationRef) -> None:
        self.assert_operation = operation
        self.preflight_started.set()
        if self.block_preflight:
            await self.release_preflight.wait()
        if self.preflight_results:
            raise self.preflight_results.popleft()

    async def _events(self) -> AsyncIterator[AdapterEvent]:
        if False:
            yield AdapterEvent(0, "unused", _identity(), NOW)

    def subscribe(self) -> AsyncIterator[AdapterEvent]:
        return self._events()

    async def submit(self, request: PromptRequest) -> SubmissionReceipt:
        self.submissions.append(request)
        self.submit_started.set()
        if self.block_submit:
            await self.release_submit.wait()
        result = (
            self.submit_results.popleft()
            if self.submit_results
            else SubmissionReceipt(
                request_id=request.request_id,
                acceptance="immediate",
                submitted_at=request.submitted_at,
                accepted_at=NOW,
            )
        )
        if isinstance(result, Exception):
            raise result
        return replace(result, request_id=request.request_id, submitted_at=request.submitted_at)

    async def respond(self, response: InteractionResponse) -> None:
        self.responses.append(response)
        self.current = replace(self.current, pending_interaction=None)

    async def reconcile(self, request_id: str) -> ReconciliationResult:
        if not self.reconcile_results:
            return ReconciliationResult(request_id, "unresolved", NOW)
        return replace(self.reconcile_results.popleft(), request_id=request_id)

    async def stop(self, mode: ShutdownMode) -> None:
        self.stop_modes.append(mode)


def _authority(
    adapter: _AuthorityAdapter,
    *,
    timeline_limit: int = 64,
    ledger_limit: int = 256,
    bridge_epoch: str = "epoch-1",
    dispatch_grace_seconds: float = DISPATCH_ACCEPTANCE_GRACE_SECONDS,
) -> HarnessSubmissionAuthority:
    authority = HarnessSubmissionAuthority(
        adapter,
        BridgeSnapshotPort(
            clock=lambda: NOW,
            snapshot=lambda: adapter.current,
            set_snapshot=lambda value: setattr(adapter, "current", value),
            publish=lambda: None,
        ),
        SubmissionLimits(
            timeline=timeline_limit,
            ledger=ledger_limit,
            dispatch_grace_seconds=dispatch_grace_seconds,
        ),
        bridge_epoch=bridge_epoch,
    )
    authority.start()
    return authority


def _prompt(request_id: str, text: str = "hello", source: str = "cockpit") -> PromptRequest:
    return PromptRequest(request_id, source, text, NOW)  # type: ignore[arg-type]


async def _complete(
    authority: HarnessSubmissionAuthority,
    operation: ControlOperationRef,
    sequence: int,
) -> None:
    await authority.observe_event(
        AdapterEvent(
            sequence=sequence,
            kind="completed",
            identity=_identity(),
            created_at=NOW,
            operation=operation,
        )
    )


class HarnessSubmissionAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_active_operation_does_not_block_status_or_queued_withdrawal(self) -> None:
        adapter = _AuthorityAdapter()
        adapter.block_submit = True
        authority = _authority(adapter)
        try:
            first_task = asyncio.create_task(authority.submit(_prompt("first")))
            await asyncio.wait_for(adapter.submit_started.wait(), 1)

            second = await authority.submit(_prompt("second", "withdraw me"))
            self.assertEqual(second.acceptance, "queued")
            status = await asyncio.wait_for(
                authority.ledger.status("epoch-1", ("second",), cockpit_only=True),
                0.1,
            )
            second_status = status.submissions[0].submission
            self.assertIsNotNone(second_status)
            assert second_status is not None
            self.assertEqual(second_status.state, "queued")
            withdrawn = await asyncio.wait_for(
                authority.withdraw("epoch-1", "second", cockpit_only=True),
                0.1,
            )
            self.assertEqual((withdrawn.outcome, withdrawn.state), ("withdrawn", "withdrawn"))
            self.assertEqual([item.request_id for item in adapter.submissions], ["first"])

            adapter.release_submit.set()
            first = await asyncio.wait_for(first_task, 1)
            self.assertEqual(first.acceptance, "immediate")
            assert authority.active_operation is not None
            await _complete(authority, authority.active_operation, 1)
        finally:
            await authority.stop(forced=True)

    async def test_dispatch_claim_wins_atomic_withdrawal_race(self) -> None:
        adapter = _AuthorityAdapter()
        adapter.block_submit = True
        authority = _authority(adapter)
        try:
            submit_task = asyncio.create_task(authority.submit(_prompt("dispatch-wins")))
            await asyncio.wait_for(adapter.submit_started.wait(), 1)
            result = await authority.withdraw("epoch-1", "dispatch-wins", cockpit_only=True)
            self.assertEqual((result.outcome, result.state), ("not-withdrawable", "dispatching"))
            adapter.release_submit.set()
            await submit_task
            assert authority.active_operation is not None
            await _complete(authority, authority.active_operation, 1)
        finally:
            await authority.stop(forced=True)

    async def test_withdrawal_during_preflight_wins_before_dispatch_claim(self) -> None:
        adapter = _AuthorityAdapter()
        adapter.block_preflight = True
        authority = _authority(adapter)
        try:
            submit_task = asyncio.create_task(authority.submit(_prompt("withdraw-preflight")))
            await asyncio.wait_for(adapter.preflight_started.wait(), 1)

            withdrawn = await authority.withdraw("epoch-1", "withdraw-preflight", cockpit_only=True)
            self.assertEqual((withdrawn.outcome, withdrawn.state), ("withdrawn", "withdrawn"))
            receipt = await asyncio.wait_for(submit_task, 1)
            self.assertEqual(receipt.acceptance, "rejected")

            adapter.release_preflight.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(adapter.submissions, [])
            status = await authority.ledger.status(
                "epoch-1", ("withdraw-preflight",), cockpit_only=True
            )
            submission = status.submissions[0].submission
            self.assertIsNotNone(submission)
            assert submission is not None
            self.assertEqual(submission.state, "withdrawn")
        finally:
            adapter.release_preflight.set()
            await authority.stop(forced=True)

    async def test_completion_before_receipt_is_buffered_and_releases_exact_head(self) -> None:
        adapter = _AuthorityAdapter()
        adapter.block_submit = True
        authority = _authority(adapter)
        try:
            first_task = asyncio.create_task(authority.submit(_prompt("early")))
            await asyncio.wait_for(adapter.submit_started.wait(), 1)
            first_ref = authority.active_operation
            assert first_ref is not None
            await _complete(authority, first_ref, 1)
            second = await authority.submit(_prompt("next"))
            self.assertEqual(second.acceptance, "queued")

            adapter.block_submit = False
            adapter.release_submit.set()
            await first_task
            for _ in range(5):
                if len(adapter.submissions) == 2:
                    break
                await asyncio.sleep(0)
            self.assertEqual([item.request_id for item in adapter.submissions], ["early", "next"])
            second_ref = authority.active_operation
            assert second_ref is not None and second_ref.operation_id == "next"
            with self.assertRaisesRegex(HarnessControlError, "duplicate"):
                await _complete(authority, first_ref, 2)
            self.assertEqual(authority.active_operation, second_ref)
            await _complete(authority, second_ref, 3)
        finally:
            await authority.stop(forced=True)

    async def test_same_id_is_idempotent_but_source_or_payload_change_conflicts(self) -> None:
        adapter = _AuthorityAdapter()
        authority = _authority(adapter)
        try:
            first = await authority.submit(_prompt("same", "first"))
            duplicate = await authority.submit(_prompt("same", "first"))
            self.assertEqual((first.acceptance, duplicate.acceptance), ("immediate", "immediate"))
            self.assertEqual(len(adapter.submissions), 1)
            with self.assertRaises(HarnessRequestConflictError):
                await authority.submit(_prompt("same", "different"))
            with self.assertRaises(HarnessRequestConflictError):
                await authority.submit(_prompt("same", "first", source="durable"))
            assert authority.active_operation is not None
            await _complete(authority, authority.active_operation, 1)
        finally:
            await authority.stop(forced=True)

    async def test_certified_pre_send_busy_requeues_without_vendor_queue_or_resend(self) -> None:
        adapter = _AuthorityAdapter()
        adapter.submit_results.append(HarnessAdapterBusyError("became busy before write"))
        authority = _authority(adapter)
        try:
            receipt = await authority.submit(_prompt("busy"))
            self.assertEqual(receipt.acceptance, "queued")
            status = await authority.ledger.status("epoch-1", ("busy",), cockpit_only=True)
            busy_status = status.submissions[0].submission
            self.assertIsNotNone(busy_status)
            assert busy_status is not None
            self.assertEqual(busy_status.state, "queued")
            self.assertEqual(len(adapter.submissions), 1)
        finally:
            await authority.stop(forced=True)

    async def test_epoch_and_public_source_scope_fail_closed(self) -> None:
        adapter = _AuthorityAdapter()
        adapter.block_submit = True
        authority = _authority(adapter)
        try:
            terminal_task = asyncio.create_task(
                authority.submit(_prompt("terminal", source="terminal"))
            )
            await adapter.submit_started.wait()
            await authority.submit(_prompt("cockpit"))
            status = await authority.ledger.status(
                "epoch-1", ("terminal", "cockpit", "missing"), cockpit_only=True
            )
            self.assertEqual(
                [item.outcome for item in status.submissions],
                [
                    "not-found",
                    "found",
                    "not-found",
                ],
            )
            with self.assertRaises(HarnessBridgeEpochMismatchError):
                await authority.withdraw("old-epoch", "cockpit", cockpit_only=True)
            adapter.release_submit.set()
            await terminal_task
        finally:
            await authority.stop(forced=True)


class SubmissionLedgerTests(unittest.IsolatedAsyncioTestCase):
    """The record store on its own: what it forgets, what it refuses, and what it answers.

    Reached directly rather than through the authority because these are the ledger's own
    boundaries -- a store with no room, a page with no size, a lookup batch with no bound --
    and driving them through a live authority would mean asserting on a fake adapter's
    scheduling rather than on the store's contract.
    """

    def _ledger(self, *, limit: int = 4) -> SubmissionLedger:
        return SubmissionLedger(bridge_epoch="epoch-1", limit=limit, lock=asyncio.Lock())

    def _enrol(
        self,
        ledger: SubmissionLedger,
        kind: str,
        state: str,
        *,
        operation_id: str | None = None,
        requested_value: str | None = None,
    ) -> OperationRecord:
        record = OperationRecord(
            ref=ledger.next_ref(kind, operation_id),  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            submitted_at=NOW,
            updated_at=NOW,
            source="cockpit",
            requested_value=requested_value,
        )
        ledger.enrol(record)
        return record

    def test_a_ledger_with_nothing_droppable_refuses_room_rather_than_forgetting_a_row(
        self,
    ) -> None:
        # A live row is the only evidence that a send may have landed, and a pinned row is
        # one the caller is still dispatching. Making room by dropping either would answer
        # "not-found" for an operation that is still happening.
        for label, state, pinned in (
            ("live rows", "queued", lambda key: False),
            ("pinned terminal rows", "delivered", lambda key: True),
        ):
            with self.subTest(label):
                ledger = self._ledger(limit=2)
                self._enrol(ledger, "prompt", state, operation_id="first")
                self._enrol(ledger, "prompt", state, operation_id="second")

                self.assertFalse(ledger.make_room(pinned))
                self.assertEqual(ledger.retained_record_count, 2)
                self.assertIsNotNone(ledger.by_request_id("first"))


if __name__ == "__main__":
    unittest.main()
