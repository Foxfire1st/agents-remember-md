"""Tests for the dashboard terminal-session catalog."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
    TerminalSessionKind,
)
from agents_remember.serving.terminal_catalog import (
    DispatchBriefReceiptStore,
    TerminalCatalog,
)


def _entry(
    session_id: str,
    *,
    created_at: str = "2026-06-26T00:00:00Z",
    task_document_ref: TaskDocumentRef | None = None,
    kind: TerminalSessionKind = "terminal",
) -> TerminalCatalogEntry:
    return TerminalCatalogEntry(
        id=session_id,
        label=f"Terminal {session_id}",
        kind=kind,
        harness="claude" if kind == "harness" else None,
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("bash",),
        created_at=created_at,
        last_attached_at=created_at,
        status="running",
        task_document_ref=task_document_ref,
    )


class TerminalCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.catalog = TerminalCatalog(self.tmp / "terminal-sessions.json")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_landed_state_round_trips_and_is_not_reanimated(self) -> None:
        self.catalog.upsert(_entry("a"))
        self.catalog.mark_landed(
            "a",
            at="2026-07-09T00:00:00+00:00",
            reason="done",
            edge="leaf-integration",
        )

        raw = json.loads(self.catalog.path.read_text(encoding="utf-8"))
        row = raw["sessions"][0]
        self.assertEqual(row["status"], "landed")
        self.assertEqual(row["landedAt"], "2026-07-09T00:00:00+00:00")
        self.assertEqual(row["landedReason"], "done")
        self.assertEqual(row["landedEdge"], "leaf-integration")

        attached = self.catalog.mark_attached("a", "2026-07-09T00:01:00+00:00")
        assert attached is not None
        self.assertEqual(attached.status, "landed")
        liveness = self.catalog.record_liveness_probe(
            "a", alive=True, checked_at=datetime.fromisoformat("2026-07-09T00:02:00+00:00")
        )
        assert liveness is not None
        self.assertEqual(liveness.status, "landed")
        exited = self.catalog.mark_exited("a")
        assert exited is not None
        self.assertEqual(exited.status, "landed")

    def test_dispatch_brief_receipts_are_idempotent_and_refuse_a_second_receipt(self) -> None:
        self.catalog.upsert(_entry("a", kind="harness"))
        receipts = DispatchBriefReceiptStore(self.catalog)
        self.assertIsNone(receipts.bind("missing", entry_id="dispatch-brief-1"))

        first = receipts.bind("a", entry_id="dispatch-brief-1")
        repeated = receipts.bind("a", entry_id="dispatch-brief-1")

        assert first is not None and repeated is not None
        self.assertEqual(first.dispatch_brief_entry_id, "dispatch-brief-1")
        self.assertEqual(repeated.dispatch_brief_entry_id, "dispatch-brief-1")
        with self.assertRaisesRegex(ValueError, "different dispatch brief"):
            receipts.bind("a", entry_id="dispatch-brief-2")
        self.assertEqual(
            self.catalog.get("a").dispatch_brief_entry_id,  # type: ignore[union-attr]
            "dispatch-brief-1",
        )

    def test_read_refuses_torn_extra_data_without_erasing_evidence(self) -> None:
        self.catalog.upsert(_entry("a"))
        good = self.catalog.path.read_text(encoding="utf-8")
        self.catalog.path.write_text(good + '\nxName": "ar-torn"\n}\n', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            self.catalog.list()
        self.assertIn("ar-torn", self.catalog.path.read_text(encoding="utf-8"))

    def test_concurrent_upserts_do_not_lose_or_corrupt_rows(self) -> None:
        # Reproduces the bug class directly: many threads upserting distinct rows at once. With the
        # read-modify-write serialized under the lock + a unique temp per write, every row survives and the
        # file stays valid JSON. (Pre-fix: lost updates and a torn, unreadable file.)
        ids = [f"s{i:03d}" for i in range(40)]
        blocker = threading.Barrier(len(ids))

        def _add(session_id: str) -> None:
            blocker.wait()  # maximize overlap on the read-modify-write
            self.catalog.upsert(
                _entry(session_id, created_at=f"2026-06-26T00:00:{session_id[-2:]}Z")
            )

        threads = [threading.Thread(target=_add, args=(session_id,)) for session_id in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        stored = {entry.id for entry in self.catalog.list()}
        self.assertEqual(stored, set(ids))  # no lost updates
        json.loads(
            self.catalog.path.read_text(encoding="utf-8")
        )  # still valid JSON (no torn write)

    def test_cross_instance_termination_is_sticky_and_never_resurrected(self) -> None:
        other = TerminalCatalog(self.catalog.path)
        self.catalog.upsert(_entry("worker"))
        finished = threading.Event()

        def terminate() -> None:
            other.mark_terminated("worker", "2026-07-12T10:01:00+00:00")
            finished.set()

        with self.catalog.batch():
            self.catalog.record_turn_state("worker", "working", changed_at="2026-07-12T10:00Z")
            writer = threading.Thread(target=terminate)
            writer.start()
            self.assertFalse(finished.wait(timeout=0.05))
        writer.join(timeout=1)
        self.assertEqual(other.get("worker").status, "terminated")  # type: ignore[union-attr]

        with self.catalog.batch():
            self.catalog.record_liveness_probe(
                "worker",
                alive=True,
                checked_at=datetime.fromisoformat("2026-07-12T10:02:00+00:00"),
            )
        self.assertEqual(other.get("worker").status, "terminated")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
