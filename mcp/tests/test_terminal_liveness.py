"""Tests for terminal catalog liveness hysteresis."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.models.conversations.control_wire import (
    AcceptanceState,
    ActivityState,
    AdapterSnapshot,
    ControlIdentity,
    ControlState,
)
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
    TerminalSessionStatus,
)
from agents_remember.serving.terminal_catalog import (
    TerminalCatalog,
)
from agents_remember.serving.terminal_liveness import (
    LivenessProbe,
    SnapshotReader,
    TerminalCatalogLivenessConfig,
    TerminalCatalogLivenessSweeper,
)
from agents_remember.serving.terminal_tmux import TmuxProbeResult


def _entry(session_id: str, *, status: TerminalSessionStatus = "running") -> TerminalCatalogEntry:
    return TerminalCatalogEntry(
        id=session_id,
        label=f"Terminal {session_id}",
        kind="harness",
        harness="codex",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("codex",),
        created_at="2026-07-07T00:00:00+00:00",
        last_attached_at="2026-07-07T00:00:00+00:00",
        status=status,
    )


def _snapshot(
    entry: TerminalCatalogEntry,
    *,
    control: ControlState,
    activity: ActivityState,
    acceptance: AcceptanceState,
) -> AdapterSnapshot:
    return AdapterSnapshot(
        identity=ControlIdentity(entry.id, entry.tmux_name, entry.created_at),
        control=control,
        activity=activity,
        acceptance=acceptance,
        vendor_session_id="vendor-1",
        raw={},
    )


def _ready_snapshot(entry: TerminalCatalogEntry) -> AdapterSnapshot:
    return _snapshot(entry, control="ready", activity="idle", acceptance="immediate")


@dataclass
class _Clock:
    moment: datetime

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


class _FakeHost:
    def __init__(self, result: TmuxProbeResult) -> None:
        self.result = result
        self.calls = 0
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def get(self, _sid: str) -> None:
        return None

    def has_session(self, tmux_name: str) -> bool:
        return self.probe_session(tmux_name).exists

    def probe_session(self, _tmux_name: str) -> TmuxProbeResult:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        return self.result


class TerminalCatalogLivenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.catalog = TerminalCatalog(self.tmp / "terminal-sessions.json")
        self.clock = _Clock(datetime(2026, 7, 7, tzinfo=UTC))

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _sweeper(
        self,
        host: _FakeHost,
        *,
        sweep_interval_seconds: float = 0.0,
    ) -> TerminalCatalogLivenessSweeper:
        return TerminalCatalogLivenessSweeper(
            self.catalog,
            host,
            now=self.clock,
            probe=LivenessProbe(
                hysteresis=TerminalCatalogLivenessConfig(
                    failure_threshold=3,
                    minimum_failure_window_seconds=5.0,
                    pane_gone_failure_threshold=1,
                    sweep_interval_seconds=sweep_interval_seconds,
                )
            ),
        )

    def _starting_sweeper(
        self,
        host: _FakeHost,
        *,
        snapshot_reader: SnapshotReader = _ready_snapshot,
        catalog: TerminalCatalog | None = None,
    ) -> TerminalCatalogLivenessSweeper:
        return TerminalCatalogLivenessSweeper(
            catalog or self.catalog,
            host,
            now=self.clock,
            probe=LivenessProbe(
                hysteresis=TerminalCatalogLivenessConfig(
                    failure_threshold=3,
                    minimum_failure_window_seconds=5.0,
                    pane_gone_failure_threshold=1,
                    sweep_interval_seconds=10.0,
                ),
                pane_capturer=lambda _tmux_name: "",
                snapshot_reader=snapshot_reader,
            ),
        )

    def _control_sweeper(
        self,
        host: _FakeHost,
        *,
        snapshot_reader: SnapshotReader,
    ) -> TerminalCatalogLivenessSweeper:
        return TerminalCatalogLivenessSweeper(
            self.catalog,
            host,
            now=self.clock,
            probe=LivenessProbe(
                hysteresis=TerminalCatalogLivenessConfig(
                    failure_threshold=3,
                    minimum_failure_window_seconds=5.0,
                    pane_gone_failure_threshold=1,
                    sweep_interval_seconds=0.0,
                ),
                pane_capturer=lambda _tmux_name: "",
                snapshot_reader=snapshot_reader,
            ),
        )

    def test_transient_failure_storm_leaves_sessions_running_until_window_elapsed(self) -> None:
        for index in range(14):
            self.catalog.upsert(_entry(f"s{index:02d}"))
        host = _FakeHost(TmuxProbeResult(exists=False, evidence="tmux-command-failed"))
        sweeper = self._sweeper(host)

        for _ in range(3):
            sweeper.refresh()
            self.clock.advance(1)

        entries = self.catalog.list()
        self.assertEqual({entry.status for entry in entries}, {"running"})
        self.assertEqual({entry.liveness_failures for entry in entries}, {3})


if __name__ == "__main__":
    unittest.main()
