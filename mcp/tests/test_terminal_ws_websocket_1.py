from __future__ import annotations

import json
import os
from typing import cast

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.serving.app import _TERMINAL_EXIT_FRAME
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_terminal_ws import TerminalWebSocketTests, _catalog_entry


class TerminalWebSocketTests1(TerminalWebSocketTests):
    def test_unknown_session_is_refused(self) -> None:
        with (
            TestClient(self.app) as client,
            client.websocket_connect("/api/terminal/ghost") as ws,
            self.assertRaises(WebSocketDisconnect) as ctx,
        ):
            ws.receive_text()
        self.assertEqual(ctx.exception.code, 4404)

    def test_pty_output_forwarded_as_binary(self) -> None:
        self._register_live()
        with TestClient(self.app) as client, client.websocket_connect("/api/terminal/live") as ws:
            self.host.feed(b"\x1b[32mok\x1b[0m")
            self.assertEqual(ws.receive_bytes(), b"\x1b[32mok\x1b[0m")

    def test_client_stdin_written_to_pty(self) -> None:
        self._register_live()
        with TestClient(self.app) as client, client.websocket_connect("/api/terminal/live") as ws:
            ws.send_text(json.dumps({"type": "stdin", "data": "echo hi\n"}))
            self.assertEqual(self.host.read_child_input(), b"echo hi\n")

    def test_client_resize_forwarded_in_order(self) -> None:
        self._register_live()
        with TestClient(self.app) as client, client.websocket_connect("/api/terminal/live") as ws:
            ws.send_text(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
            # A following stdin we can read back proves the resize frame was processed first.
            ws.send_text(json.dumps({"type": "stdin", "data": "x"}))
            self.assertEqual(self.host.read_child_input(), b"x")
        self.assertEqual(self.host.resizes, [(100, 30)])

    def test_client_disconnect_detaches_local_pty_without_exiting_catalog_row(self) -> None:
        self.catalog.upsert(_catalog_entry("live", cwd=self.tmp))
        self.host.probe_names.add("ar-live")

        with TestClient(self.app) as client, client.websocket_connect("/api/terminal/live"):
            pass

        self.assertEqual(self.host.closed, ["live"])
        entry = self.catalog.get("live")
        assert entry is not None
        self.assertEqual(entry.status, "running")

    def test_parallel_websockets_use_independent_terminal_clients(self) -> None:
        self._register_live()

        with TestClient(self.app) as client:  # noqa: SIM117 - ws2 must close while ws1 remains open.
            with client.websocket_connect("/api/terminal/live") as ws1:
                with client.websocket_connect("/api/terminal/live") as ws2:
                    self.assertEqual(len(self.host.attachments), 2)
                    self.host.feed_all(b"shared-output")
                    self.assertEqual(ws1.receive_bytes(), b"shared-output")
                    self.assertEqual(ws2.receive_bytes(), b"shared-output")
                self.host.feed_to(self.host.attachments[0], b"still-open")
                self.assertEqual(ws1.receive_bytes(), b"still-open")

        self.assertEqual(self.host.closed, ["live", "live"])

    def test_child_exit_sends_exit_frame_then_closes(self) -> None:
        self._register_live()
        with TestClient(self.app) as client, client.websocket_connect("/api/terminal/live") as ws:
            self.host.end()
            self.assertEqual(ws.receive_text(), _TERMINAL_EXIT_FRAME)
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_bytes()

    def test_host_shutdown_on_app_teardown(self) -> None:
        with TestClient(self.app):
            pass
        self.assertTrue(self.host.shutdown_called)

    def test_post_open_spawns_shell_at_workspace_root(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                "/api/terminal/term-1",
                json={"kind": "terminal", "label": "Terminal 1", "lifecycleId": "LC1"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session"], "term-1")
        self.assertEqual(body["kind"], "terminal")
        self.assertEqual(body["label"], "Terminal 1")
        self.assertEqual(body["lifecycleId"], "LC1")
        self.assertEqual(body["tmuxName"], "ar-term-1")
        self.assertEqual(body["status"], "running")
        self.assertEqual(len(self.host.ensured), 1)
        ensured = self.host.ensured[0]
        self.assertEqual(ensured["sid"], "term-1")
        self.assertEqual(ensured["cwd"], self.tmp)  # workspace_root (== _config's tmp)
        self.assertEqual(len(cast(list, ensured["command"])), 1)  # the shell argv
        self.assertEqual(ensured["lifecycle_id"], "LC1")
        self.assertFalse(ensured["suspend_unsafe"])  # a shell keeps Ctrl-Z (job control)
        self.assertEqual(self.host.opened, [])
        self.assertEqual(self.host.closed, [])
        entry = self.catalog.get("term-1")
        assert entry is not None
        self.assertEqual(entry.label, "Terminal 1")
        self.assertEqual(entry.lifecycle_id, "LC1")
        self.assertEqual(entry.status, "running")

    def test_get_terminal_sessions_lists_catalog_entries(self) -> None:
        with TestClient(self.app) as client:
            client.post("/api/terminal/term-1", json={"kind": "terminal", "label": "Terminal 1"})
            response = client.get("/api/terminal/sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["sessions"][0],
            {
                "id": "term-1",
                "label": "Terminal 1",
                "kind": "terminal",
                "cwd": str(self.tmp),
                "tmuxName": "ar-term-1",
                "command": [os.environ.get("SHELL") or "/bin/bash"],
                "createdAt": response.json()["sessions"][0]["createdAt"],
                "lastAttachedAt": response.json()["sessions"][0]["lastAttachedAt"],
                "status": "running",
                "seatRole": "terminal",
            },
        )

    def test_post_open_keeps_detached_session_available_for_first_websocket(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                "/api/terminal/term-1", json={"kind": "terminal", "label": "Terminal 1"}
            )
            self.assertEqual(response.status_code, 200)
            with client.websocket_connect("/api/terminal/term-1") as ws:
                self.host.feed(b"first-attach")
                self.assertEqual(ws.receive_bytes(), b"first-attach")

        self.assertEqual(len(self.host.ensured), 1)
        self.assertEqual(len(self.host.attached), 1)
        self.assertEqual(self.host.closed, ["term-1"])
        entry = self.catalog.get("term-1")
        assert entry is not None
        self.assertEqual(entry.status, "running")

    def test_websocket_attaches_missing_host_from_catalog_when_tmux_exists(self) -> None:
        self.catalog.upsert(
            _catalog_entry("restored", cwd=self.tmp, tmux_name="ar-restored", command=("bash",))
        )
        self.host.probe_names.add("ar-restored")
        with (
            TestClient(self.app) as client,
            client.websocket_connect("/api/terminal/restored") as ws,
        ):
            self.host.feed(b"restored-output")
            self.assertEqual(ws.receive_bytes(), b"restored-output")
        self.assertEqual(self.host.attached[0]["sid"], "restored")
        self.assertEqual(self.host.attached[0]["name"], "ar-restored")
        entry = self.catalog.get("restored")
        assert entry is not None
        self.assertEqual(entry.status, "running")

    def test_websocket_attaches_landed_catalog_session_for_inspection(self) -> None:
        self.catalog.upsert(
            _catalog_entry(
                "landed", cwd=self.tmp, status="landed", tmux_name="ar-landed", command=("bash",)
            )
        )
        self.host.probe_names.add("ar-landed")
        with TestClient(self.app) as client, client.websocket_connect("/api/terminal/landed") as ws:
            self.host.feed(b"landed-output")
            self.assertEqual(ws.receive_bytes(), b"landed-output")
        self.assertEqual(self.host.attached[0]["sid"], "landed")
        entry = self.catalog.get("landed")
        assert entry is not None
        self.assertEqual(entry.status, "landed")

    def test_websocket_marks_stale_catalog_session_exited(self) -> None:
        self.catalog.upsert(_catalog_entry("stale", cwd=self.tmp, tmux_name="ar-stale"))
        with (
            TestClient(self.app) as client,
            client.websocket_connect("/api/terminal/stale") as ws,
            self.assertRaises(WebSocketDisconnect) as ctx,
        ):
            ws.receive_text()
        self.assertEqual(ctx.exception.code, 4404)
        entry = self.catalog.get("stale")
        assert entry is not None
        self.assertEqual(entry.status, "exited")

    def test_get_terminal_sessions_marks_stale_tmux_rows_exited(self) -> None:
        self.catalog.upsert(_catalog_entry("stale", cwd=self.tmp, tmux_name="ar-stale"))
        (self.tmp / "settings.json").write_text(
            json.dumps({"orchestration": {"agentNotifier": {"enabled": False}}}),
            encoding="utf-8",
        )
        with TestClient(self.app) as client:
            response = client.get("/api/terminal/sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sessions"][0]["status"], "exited")

    def test_terminate_marks_catalog_and_kills_tmux(self) -> None:
        with TestClient(self.app) as client:
            client.post("/api/terminal/term-1", json={"kind": "terminal", "label": "Terminal 1"})
            response = client.post("/api/terminal/term-1/terminate")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "terminated")
        self.assertEqual(self.host.terminated, ["ar-term-1"])
        entry = self.catalog.get("term-1")
        assert entry is not None
        self.assertEqual(entry.status, "terminated")

    def test_landed_cleanup_closes_only_landed_rows_and_reports_skips(self) -> None:
        self.catalog.upsert(
            _catalog_entry("landed", cwd=self.tmp, status="landed", tmux_name="ar-landed")
        )
        self.catalog.upsert(_catalog_entry("running", cwd=self.tmp, tmux_name="ar-running"))
        self.catalog.upsert(
            _catalog_entry("exited", cwd=self.tmp, status="exited", tmux_name="ar-exited")
        )
        self.host.probe_names.add("ar-running")

        with TestClient(self.app) as client:
            response = client.post(
                "/api/terminal/landed-cleanup",
                json={"sessionIds": ["landed", "running", "exited", "ghost"]},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "cleaned")
        self.assertEqual(body["closed"], 1)
        self.assertEqual(body["skipped"], 3)
        self.assertEqual(body["closedSessions"], ["landed"])
        self.assertEqual(
            body["skippedSessions"],
            [
                {"session": "running", "reason": "status:running"},
                {"session": "exited", "reason": "status:exited"},
                {"session": "ghost", "reason": "unknown-session"},
            ],
        )
        self.assertEqual(self.host.terminated, ["ar-landed"])
        landed = self.catalog.get("landed")
        running = self.catalog.get("running")
        exited = self.catalog.get("exited")
        assert landed is not None
        assert running is not None
        assert exited is not None
        self.assertEqual(landed.status, "terminated")
        self.assertEqual(landed.retired_reason, "landed group cleanup")
        self.assertEqual(running.status, "running")
        self.assertEqual(exited.status, "exited")

    def test_post_open_rejects_unknown_kind(self) -> None:
        with TestClient(self.app) as client:
            response = client.post("/api/terminal/x", json={"kind": "bogus"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.host.ensured, [])

    def test_post_open_claims_task_document_and_persists_it(self) -> None:
        task_document_ref = TaskDocumentRef(
            repository="agents-remember",
            path="260628_operations-integration/260628-L5.json",
        )
        with TestClient(self.app) as client:
            response = client.post(
                "/api/terminal/term-1",
                json={
                    "kind": "terminal",
                    "taskDocumentRef": task_document_ref.model_dump(),
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["taskDocumentRef"], task_document_ref.model_dump())
        entry = self.catalog.get("term-1")
        assert entry is not None
        self.assertEqual(entry.task_document_ref, task_document_ref)

    def test_post_open_rejects_missing_task_document_ref(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                "/api/terminal/term-1",
                json={
                    "kind": "terminal",
                    "taskDocumentRef": {
                        "repository": "repo",
                        "path": "master/missing-leaf.json",
                    },
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "task-binding-invalid")
        self.assertIn("role scope", response.json()["detail"])
        self.assertEqual(self.host.ensured, [])
        self.assertIsNone(self.catalog.get("term-1"))

    def test_post_open_without_task_document_still_works(self) -> None:
        with TestClient(self.app) as client:
            response = client.post("/api/terminal/term-1", json={"kind": "terminal"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["taskDocumentRef"])
        entry = self.catalog.get("term-1")
        assert entry is not None
        self.assertIsNone(entry.task_document_ref)
