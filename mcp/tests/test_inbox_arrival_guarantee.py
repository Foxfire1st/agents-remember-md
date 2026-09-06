"""Scoped terminal-row fixture for inbox delivery tests."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

from agents_remember.serving.terminal_catalog import TerminalCatalogEntry

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


def _seat(session_id: str, **overrides: object) -> TerminalCatalogEntry:
    base: dict[str, object] = dict(
        id=session_id,
        label=session_id,
        kind="harness",
        harness="codex",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("codex",),
        created_at=NOW.isoformat(),
        last_attached_at=NOW.isoformat(),
        status="running",
    )
    base.update(overrides)
    return TerminalCatalogEntry(**base)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
