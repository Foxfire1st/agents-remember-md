from __future__ import annotations

from typing import Any
from unittest import mock

from agents_remember.models.conversations.control_wire import (
    SubmissionAuthorityDescriptor,
    SubmissionReceipt,
    WithdrawalResult,
)
from agents_remember.serving.harness_capabilities import CapabilitySnapshot, SetResult
from agents_remember.serving.harness_control_models import (
    ReconciliationResult,
    SubmissionLookup,
    SubmissionStatus,
    SubmissionStatusBatch,
)
from test_serving_response_conformance import _PNG, ServingResponseConformanceTests


class ServingResponseConformance2(ServingResponseConformanceTests):
    def test_notes_routes_conform(self) -> None:
        with self._client() as client:
            self._check(
                client, "GET", "/api/notes/list", status=200, params={"repo": "R", "master": "t"}
            )
            self._check(
                client,
                "GET",
                "/api/notes/read",
                status=200,
                params={"repo": "R", "master": "t", "path": "design.md"},
            )
            self._check(
                client,
                "GET",
                "/api/notes/read",
                status=404,
                params={"repo": "R", "master": "t", "path": "ghost.md"},
            )

    def test_requirement_routes_conform(self) -> None:
        context = {"repo": "R", "master": "t", "document": "t/task.json"}
        with self._client() as client:
            self._check(client, "GET", "/api/requirements/list", status=200, params=context)
            self._check(
                client,
                "GET",
                "/api/requirements/list",
                status=400,
                params={**context, "document": "other/task.json"},
            )
            self._check(
                client,
                "GET",
                "/api/requirements/list",
                status=404,
                params={**context, "repo": "ghost"},
            )
            self._check(
                client,
                "GET",
                "/api/requirements/read",
                status=200,
                params={**context, "path": "R1.md"},
            )
            self._check(
                client,
                "GET",
                "/api/requirements/read",
                status=400,
                params={**context, "path": "../task.json"},
            )
            self._check(
                client,
                "GET",
                "/api/requirements/read",
                status=404,
                params={**context, "path": "ghost.md"},
            )

    def test_changeset_routes_conform(self) -> None:
        leaf = {"repo": "R", "master": "t", "leaf": "leaf-1", "mode": "working"}
        with self._client() as client:
            self._check(client, "GET", "/api/changeset/task", status=200, params=leaf)
            self._check(
                client,
                "GET",
                "/api/changeset/file-diff",
                status=200,
                params={**leaf, "kind": "code", "path": "f.py"},
            )
            self._check(
                client,
                "GET",
                "/api/changeset/master",
                status=200,
                params={"repo": "R", "master": "t"},
            )
            self._check(
                client,
                "GET",
                "/api/changeset/task",
                status=400,
                params={"repo": "R", "leaf": "leaf-1"},
            )

    def test_action_and_inbox_routes_conform(self) -> None:
        with self._client() as client:
            self._check(
                client,
                "POST",
                "/api/actions/dismiss",
                status=202,
                route="/api/actions/{action}",
                json={"actor": "developer", "itemId": "item-1", "kind": "actionable-drift"},
            )
            # A gate verb against a lifecycle with no open gate: the 409 the router mints
            # after the evaluator has already accepted the request.
            self._check(
                client,
                "POST",
                "/api/actions/approve",
                status=409,
                route="/api/actions/{action}",
                json={"actor": "developer", "target": "ghost"},
            )
            self._check(
                client,
                "POST",
                "/api/actions/approve",
                status=400,
                route="/api/actions/{action}",
                json={"actor": "developer"},
            )
            # A non-gate verb whose target is not in the projection at all.
            self._check(
                client,
                "POST",
                "/api/actions/pause",
                status=404,
                route="/api/actions/{action}",
                json={"actor": "developer", "target": "ghost"},
            )
            posted = self._check(
                client,
                "POST",
                "/api/operator-inbox",
                status=200,
                json={"lifecycleId": "L1", "ask": "Continue?", "response": "Yes"},
            )
            self._check(
                client,
                "POST",
                f"/api/operator-inbox/{posted['entryId']}/dismiss",
                status=200,
                route="/api/operator-inbox/{entry_id}/dismiss",
            )
            self._check(
                client,
                "POST",
                "/api/operator-inbox/ghost/dismiss",
                status=404,
                route="/api/operator-inbox/{entry_id}/dismiss",
            )

    def test_terminal_catalog_routes_conform(self) -> None:
        # The two routes FastAPI itself validates. ``sessions`` must carry a row with the
        # conditional key set actually populated, or the one live-enforced model on the whole
        # surface would be exercised against an empty list.
        with self._client() as client:
            sessions = self._check(client, "GET", "/api/terminal/sessions", status=200)
            self._check(client, "GET", "/api/harnesses", status=200)
        rows = {row["id"]: row for row in sessions["sessions"]}
        self.assertIn("live", rows)
        self.assertEqual(rows["live"]["harness"], "claude")
        # The conditional half really is conditional: an unset field is an ABSENT key, never a
        # null. This is what ``response_model_exclude_unset`` preserves.
        self.assertEqual(rows["live"]["controlProtocol"], "ar-harness-control/v1")
        self.assertNotIn("retiredAt", rows["live"])
        self.assertNotIn("launchArgs", rows["live"])
        self.assertNotIn("spawnRole", rows["live"])

    def test_terminal_control_routes_conform(self) -> None:
        with self._client() as client:
            self._check(
                client,
                "POST",
                "/api/terminal/landed-cleanup",
                status=200,
                json={"sessionIds": ["landed", "ghost"]},
            )
            self._check(
                client,
                "POST",
                "/api/terminal/live/attach-task",
                status=200,
                route="/api/terminal/{session}/attach-task",
                json={
                    "taskDocumentRef": {"repository": "R", "path": "t/leaf-1.json"},
                    "role": "worker",
                },
            )
            self._check(
                client,
                "POST",
                "/api/terminal/ghost/attach-task",
                status=404,
                route="/api/terminal/{session}/attach-task",
                json={
                    "taskDocumentRef": {"repository": "R", "path": "t/leaf-1.json"},
                    "role": "worker",
                },
            )
            # A plain pane: the paster's tmux call fails in this fixture, which is still a real
            # ``TerminalPaneDelivery`` body -- ``delivered: false`` with the capture attached.
            self._check(
                client,
                "POST",
                "/api/terminal/plain/paste",
                status=200,
                route="/api/terminal/{session}/paste",
                json={"text": "hello", "submit": False},
            )
            self._check(
                client,
                "POST",
                "/api/terminal/ghost/paste",
                status=404,
                route="/api/terminal/{session}/paste",
                json={"text": "hello"},
            )
            self._check(
                client,
                "POST",
                "/api/terminal/live/rename",
                status=200,
                route="/api/terminal/{session}/rename",
                json={"label": "Renamed"},
            )
            self._check(
                client,
                "POST",
                "/api/terminal/ghost/rename",
                status=404,
                route="/api/terminal/{session}/rename",
                json={"label": "x"},
            )
            self._check(
                client,
                "POST",
                "/api/terminal/live/retire",
                status=403,
                route="/api/terminal/{session}/retire",
                json={"actorSession": "live", "reason": "self"},
            )
            self._check(
                client,
                "POST",
                "/api/terminal/live/retire",
                status=404,
                route="/api/terminal/{session}/retire",
                json={"actorSession": "ghost", "reason": "x"},
            )
            self._check(
                client,
                "POST",
                "/api/terminal/plain/terminate",
                status=200,
                route="/api/terminal/{session}/terminate",
            )
            self._check(
                client,
                "POST",
                "/api/terminal/ghost/terminate",
                status=404,
                route="/api/terminal/{session}/terminate",
            )
            self._check(
                client,
                "POST",
                "/api/terminal/live/image",
                status=200,
                route="/api/terminal/{session}/image",
                files={"file": ("dot.png", _PNG, "image/png")},
            )
            self._check(
                client,
                "POST",
                "/api/terminal/live/image",
                status=400,
                route="/api/terminal/{session}/image",
                files={"file": ("dot.txt", b"nope", "text/plain")},
            )
            self._check(
                client,
                "POST",
                "/api/terminal/ghost/image",
                status=404,
                route="/api/terminal/{session}/image",
                files={"file": ("dot.png", _PNG, "image/png")},
            )
            # The opener refuses an unknown kind before it ever reaches the host.
            self._check(
                client,
                "POST",
                "/api/terminal/fresh",
                status=400,
                route="/api/terminal/{session}",
                json={"kind": "nonsense", "label": "x"},
            )

    def test_harness_control_routes_conform(self) -> None:
        # The bridge is the one thing doubled: every route below still resolves its seat
        # through the real catalog + liveness path and answers through the real serializers.
        authority = SubmissionAuthorityDescriptor(bridge_epoch="epoch-1")
        patches = {
            "read_control_capabilities": CapabilitySnapshot((), None, None),
            "set_control_model": SetResult(
                ok=True, acceptance="echo-verified", requested_value="opus"
            ),
            "set_control_effort": SetResult(
                ok=True, acceptance="immediate", requested_value="high"
            ),
            "read_submission_authority": authority,
            "read_submission_status": SubmissionStatusBatch(
                bridge_epoch="epoch-1",
                submissions=(
                    SubmissionLookup(
                        request_id="r1",
                        outcome="found",
                        submission=SubmissionStatus(
                            request_id="r1",
                            state="delivered",
                            submitted_at="2026-06-14T10:00:00+00:00",
                            updated_at="2026-06-14T10:00:01+00:00",
                            accepted_at=None,
                            withdrawable=False,
                        ),
                    ),
                ),
            ),
            "withdraw_control_submission": WithdrawalResult(
                request_id="r1", outcome="not-withdrawable", state="delivered"
            ),
            "submit_control_prompt": SubmissionReceipt(
                request_id="r1",
                acceptance="queued",
                submitted_at="2026-06-14T10:00:00+00:00",
                bridge_epoch="epoch-1",
            ),
            "reconcile_control_prompt": ReconciliationResult(
                request_id="r1",
                state="accepted",
                reconciled_at="2026-06-14T10:00:02+00:00",
                bridge_epoch="epoch-1",
            ),
        }
        stack = [
            mock.patch(f"agents_remember.serving.harness_control_api.{name}", return_value=value)
            for name, value in patches.items()
        ]
        for patch in stack:
            patch.start()
            self.addCleanup(patch.stop)
        with self._client() as client:
            base = "/api/terminal/live"
            self._check(
                client,
                "GET",
                f"{base}/capabilities",
                status=200,
                route="/api/terminal/{session}/capabilities",
            )
            self._check(
                client,
                "POST",
                f"{base}/set-model",
                status=200,
                route="/api/terminal/{session}/set-model",
                json={"model": "opus"},
            )
            self._check(
                client,
                "POST",
                f"{base}/set-effort",
                status=200,
                route="/api/terminal/{session}/set-effort",
                json={"effort": "high"},
            )
            self._check(
                client,
                "GET",
                f"{base}/submission-authority",
                status=200,
                route="/api/terminal/{session}/submission-authority",
            )
            self._check(
                client,
                "POST",
                f"{base}/submission-status",
                status=200,
                route="/api/terminal/{session}/submission-status",
                json={"expectedBridgeEpoch": "epoch-1", "requestIds": ["r1"]},
            )
            self._check(
                client,
                "POST",
                f"{base}/withdraw",
                status=200,
                route="/api/terminal/{session}/withdraw",
                json={"expectedBridgeEpoch": "epoch-1", "requestId": "r1"},
            )
            self._check(
                client,
                "POST",
                f"{base}/submit",
                status=200,
                route="/api/terminal/{session}/submit",
                json={"requestId": "r1", "text": "go", "expectedBridgeEpoch": "epoch-1"},
            )
            self._check(
                client,
                "POST",
                f"{base}/reconcile",
                status=200,
                route="/api/terminal/{session}/reconcile",
                json={"requestId": "r1", "expectedBridgeEpoch": "epoch-1"},
            )
            self._check(
                client,
                "POST",
                f"{base}/interaction-response",
                status=409,
                route="/api/terminal/{session}/interaction-response",
                json={
                    "interactionId": "q1",
                    "expectedBridgeEpoch": "stale",
                    "response": "allow",
                },
            )
            self._check(
                client,
                "GET",
                "/api/terminal/ghost/capabilities",
                status=404,
                route="/api/terminal/{session}/capabilities",
            )
            self._check(
                client,
                "GET",
                "/api/harnesses/nope/capabilities",
                status=404,
                route="/api/harnesses/{harness}/capabilities",
            )

    def test_conversation_routes_conform(self) -> None:
        epoch = {"expectedBridgeEpoch": "epoch-1"}
        turn = {"turnId": "t1", "requestId": "r1"}
        withdraw = {"operationRef": "o1", "withdrawRequestId": "w1"}
        ghost = "/api/terminal/ghost"
        calls: list[tuple[str, str, str, int, dict[str, Any]]] = [
            (
                "GET",
                f"{ghost}/conversation",
                "/api/terminal/{ar_session_id}/conversation",
                404,
                {"params": epoch},
            ),
            (
                "POST",
                f"{ghost}/conversation/agents/a1/history",
                "/api/terminal/{ar_session_id}/conversation/agents/{agent_id}/history",
                404,
                {"params": epoch},
            ),
            (
                "GET",
                f"{ghost}/conversation/events",
                "/api/terminal/{ar_session_id}/conversation/events",
                400,
                {"params": {**epoch, "after": "not-a-cursor"}},
            ),
            (
                "POST",
                f"{ghost}/conversation/interrupt",
                "/api/terminal/{ar_session_id}/conversation/interrupt",
                404,
                {"params": epoch, "json": turn},
            ),
            (
                "POST",
                f"{ghost}/conversation/interrupt-status",
                "/api/terminal/{ar_session_id}/conversation/interrupt-status",
                404,
                {"params": epoch, "json": turn},
            ),
            (
                "POST",
                f"{ghost}/conversation/interrupt-reconcile",
                "/api/terminal/{ar_session_id}/conversation/interrupt-reconcile",
                404,
                {"params": epoch, "json": turn},
            ),
            (
                "GET",
                f"{ghost}/operation-queue",
                "/api/terminal/{ar_session_id}/operation-queue",
                404,
                {"params": epoch},
            ),
            (
                "POST",
                f"{ghost}/operation-queue/withdraw",
                "/api/terminal/{ar_session_id}/operation-queue/withdraw",
                404,
                {"params": epoch, "json": {**withdraw, "withdrawalRef": "wr1"}},
            ),
            (
                "POST",
                f"{ghost}/operation-queue/withdraw-status",
                "/api/terminal/{ar_session_id}/operation-queue/withdraw-status",
                404,
                {"params": epoch, "json": withdraw},
            ),
            (
                "POST",
                f"{ghost}/operation-queue/withdraw-reconcile",
                "/api/terminal/{ar_session_id}/operation-queue/withdraw-reconcile",
                404,
                {"params": epoch, "json": withdraw},
            ),
            (
                "GET",
                f"{ghost}/operation-queue/pending-withdrawal-recoveries",
                "/api/terminal/{ar_session_id}/operation-queue/pending-withdrawal-recoveries",
                404,
                {"params": epoch},
            ),
            (
                "POST",
                f"{ghost}/operation-queue/withdraw-recovery",
                "/api/terminal/{ar_session_id}/operation-queue/withdraw-recovery",
                404,
                {"params": epoch, "json": {"recoveryRef": "rc1"}},
            ),
            (
                "POST",
                f"{ghost}/operation-queue/withdraw-recovery-ack",
                "/api/terminal/{ar_session_id}/operation-queue/withdraw-recovery-ack",
                404,
                {
                    "params": epoch,
                    "json": {"recoveryRef": "rc1", "disposition": "keep-current-draft"},
                },
            ),
            (
                "POST",
                f"{ghost}/conversation/attachments",
                "/api/terminal/{ar_session_id}/conversation/attachments",
                404,
                {
                    "params": epoch,
                    "data": {"requestId": "a1"},
                    "files": [("assets", ("dot.png", _PNG, "image/png"))],
                },
            ),
            (
                "POST",
                f"{ghost}/conversation/attachments/rebind",
                "/api/terminal/{ar_session_id}/conversation/attachments/rebind",
                404,
                {"params": epoch, "json": {"recoveryAssetRef": "ar1", "requestId": "r1"}},
            ),
            (
                "GET",
                f"{ghost}/conversation/attachments/r1/status",
                "/api/terminal/{ar_session_id}/conversation/attachments/{request_id}/status",
                404,
                {"params": epoch},
            ),
            (
                "POST",
                f"{ghost}/conversation/attachments/r1/reconcile",
                "/api/terminal/{ar_session_id}/conversation/attachments/{request_id}/reconcile",
                404,
                {"params": epoch},
            ),
            (
                "POST",
                f"{ghost}/conversation/submit",
                "/api/terminal/{ar_session_id}/conversation/submit",
                404,
                {
                    "json": {
                        "expectedBridgeEpoch": "epoch-1",
                        "requestId": "s1",
                        "disposition": "next",
                        "draftRevision": 1,
                        "content": [{"type": "text", "text": "hi"}],
                    }
                },
            ),
            (
                "GET",
                f"{ghost}/conversation/policy",
                "/api/terminal/{ar_session_id}/conversation/policy",
                404,
                {"params": epoch},
            ),
            (
                "GET",
                f"{ghost}/conversation/telemetry",
                "/api/terminal/{ar_session_id}/conversation/telemetry",
                404,
                {"params": epoch},
            ),
            (
                "GET",
                "/api/harnesses/nope/conversations",
                "/api/harnesses/{harness_id}/conversations",
                404,
                {},
            ),
            (
                "GET",
                "/api/harnesses/nope/conversations/k",
                "/api/harnesses/{harness_id}/conversations/{conversation_key}",
                404,
                {},
            ),
            (
                "POST",
                "/api/harnesses/nope/conversations/k/open",
                "/api/harnesses/{harness_id}/conversations/{conversation_key}/open",
                404,
                {"json": {"requestId": "o1", "expectedIdentityDigest": "d"}},
            ),
            (
                "POST",
                "/api/harnesses/nope/conversations/k/open-status",
                "/api/harnesses/{harness_id}/conversations/{conversation_key}/open-status",
                404,
                {"json": {"requestId": "o1"}},
            ),
            (
                "POST",
                "/api/harnesses/nope/conversations/k/open-reconcile",
                "/api/harnesses/{harness_id}/conversations/{conversation_key}/open-reconcile",
                404,
                {"json": {"requestId": "o1"}},
            ),
        ]
        with self._client() as client:
            for method, path, route, status, kwargs in calls:
                self._check(client, method, path, status=status, route=route, **kwargs)
