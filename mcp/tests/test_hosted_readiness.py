from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from agents_remember.models.conversations.control_wire import (
    AdapterSnapshot,
    ControlIdentity,
)
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
)
from agents_remember.serving.hosted_readiness import (
    ReadinessWait,
    hosted_session_readiness,
)
from agents_remember.serving.terminal_catalog import (
    TerminalCatalog,
)


def _entry(
    session_id: str,
    *,
    status: str = "running",
    created_at: str = "2026-07-12T10:00:00+00:00",
    controlled: bool = True,
) -> TerminalCatalogEntry:
    return TerminalCatalogEntry(
        id=session_id,
        label=session_id,
        kind="harness",
        harness="codex",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("codex",),
        created_at=created_at,
        last_attached_at=created_at,
        status=status,  # type: ignore[arg-type]
        control_state="starting" if controlled else None,
        control_endpoint=Path(f"/tmp/{session_id}.sock") if controlled else None,
        control_protocol="ar-harness-control/v1" if controlled else None,
    )


def _snapshot(
    entry: TerminalCatalogEntry,
    *,
    control: str = "ready",
    acceptance: str = "immediate",
    activity: str = "idle",
) -> AdapterSnapshot:
    return AdapterSnapshot(
        identity=ControlIdentity(entry.id, entry.tmux_name, entry.created_at),
        control=control,  # type: ignore[arg-type]
        activity=activity,  # type: ignore[arg-type]
        acceptance=acceptance,  # type: ignore[arg-type]
        vendor_session_id="vendor-1",
        raw={"thread": "vendor-1"},
    )


class _Host:
    def __init__(self, answers: list[bool] | None = None) -> None:
        self.answers = list(answers or [])

    def has_session(self, tmux_name: str) -> bool:
        del tmux_name
        return self.answers.pop(0) if self.answers else True


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


@pytest.fixture
def catalog(tmp_path: Path) -> TerminalCatalog:
    return TerminalCatalog(tmp_path / "terminal-sessions.json")


def test_exact_adapter_handshake_is_ready_and_pane_probes_are_diagnostic_only(
    catalog: TerminalCatalog,
) -> None:
    entry = _entry("target")
    catalog.upsert(entry)
    pane_calls: list[str] = []
    result = hosted_session_readiness(
        catalog,
        _Host(),
        session_id="target",
        snapshot_reader=_snapshot,
        pane_capturer=lambda name: pane_calls.append(name) or "booting forever",
        pane_mode_probe=lambda _name: True,
    )
    assert result.status == "ready"
    assert result.entry is not None
    assert result.entry.control_vendor_session_id == "vendor-1"
    assert pane_calls == []


@pytest.mark.parametrize("acceptance", ["rejected", "unknown", "unsupported"])
def test_ready_control_without_acceptance_is_not_ready(
    catalog: TerminalCatalog, acceptance: str
) -> None:
    entry = _entry("target")
    catalog.upsert(entry)
    result = hosted_session_readiness(
        catalog,
        _Host(),
        session_id="target",
        snapshot_reader=lambda current: _snapshot(current, acceptance=acceptance),
    )
    assert result.status == "not-ready"
    assert f"acceptance={acceptance}" in (result.detail or "")


def test_not_ready_wait_is_bounded(catalog: TerminalCatalog) -> None:
    entry = _entry("target")
    catalog.upsert(entry)
    clock = _Clock()
    result = hosted_session_readiness(
        catalog,
        _Host(),
        session_id="target",
        snapshot_reader=lambda current: _snapshot(
            current, control="starting", acceptance="unknown", activity="unknown"
        ),
        wait=ReadinessWait(
            seconds=0.25, monotonic=clock.monotonic, sleep=clock.sleep, poll_interval=0.1
        ),
    )
    assert result.status == "not-ready"
    assert clock.value == pytest.approx(0.25)


def test_exact_identity_change_during_adapter_read_is_unknown(catalog: TerminalCatalog) -> None:
    entry = _entry("target")
    catalog.upsert(entry)

    def read(current: TerminalCatalogEntry) -> AdapterSnapshot:
        catalog.upsert(replace(current, created_at="2026-07-12T11:00:00+00:00"))
        return _snapshot(current)

    result = hosted_session_readiness(catalog, _Host(), session_id="target", snapshot_reader=read)
    assert result.status == "unknown-session"
    assert "identity changed" in (result.detail or "")
