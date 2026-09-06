"""Disposable notifier settings and terminal-row builders."""

from __future__ import annotations

import sys
import unittest
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path
from typing import cast

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
    TerminalSessionKind,
    TerminalSessionStatus,
)
from agents_remember.serving.terminal_paste import (
    PasteResult,
    TerminalPaster,
)

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


def _entry(
    session_id: str,
    *,
    kind: TerminalSessionKind = "harness",
    status: TerminalSessionStatus = "running",
    task_document_ref: TaskDocumentRef | None = None,
) -> TerminalCatalogEntry:
    """A seat row. Turn state comes from the row's own ``with_turn_state``; anything else
    from ``replace(...)`` -- ``TerminalCatalogEntry`` already carries every field, so the
    builder supplies only what identifies the seat rather than mirroring the row's shape.
    """
    return TerminalCatalogEntry(
        id=session_id,
        label=f"Chat {session_id}",
        kind=kind,
        harness="codex",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("codex",),
        created_at="2026-07-08T00:00:00+00:00",
        last_attached_at="2026-07-08T00:00:00+00:00",
        status=status,
        task_document_ref=task_document_ref,
    )


class _FakeHost:
    """The minimal ``deliver_inbox_entry`` seam: every catalog session is reachable."""

    def has_session(self, _tmux_name: str) -> bool:
        return True

    def terminate(self, _sid: str, *, tmux_name: str | None = None) -> None:
        pass


def _fake_paster() -> TerminalPaster:
    """An already-log-confirmed delivery seam for agent-notifier orchestration tests."""

    class _AcceptedPaster:
        # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_agent_notifier.py:101).
        def paste(  # pragma: no cover
            self,
            _tmux_name: str,
            _text: str,
            *,
            submit: bool = False,
            **_kwargs: object,
        ) -> PasteResult:
            return PasteResult(delivered=True, submitted=submit)

    return cast(TerminalPaster, _AcceptedPaster())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
