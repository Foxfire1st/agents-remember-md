from __future__ import annotations

from agents_remember.serving.terminal_paste import (
    AcceptanceWindow,
    DispatchPastePolicy,
    PasteRecoveryLadder,
    TerminalPaster,
    TerminalPasterSeams,
)


class _Clock:
    def __init__(self, step: float = 0.5) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class _Tmux:
    """A tmux double that can also refuse: ``paste_results`` and ``failing_keys`` fail commands.

    Every tmux command the paster drives can fail in production (the pane went away mid-ladder,
    the server is wedged), and the paster's contract is different for each failure -- an unwritten
    buffer is not delivered, a refused Enter is delivered but unsubmitted. The double therefore has
    to be able to say no per command, not merely record the calls.
    """

    def __init__(
        self,
        *,
        capture_values: list[str] | None = None,
        paste_results: list[bool] | None = None,
        failing_keys: frozenset[str] = frozenset(),
    ) -> None:
        self.loads: list[str] = []
        self.pastes = 0
        self.keys: list[str] = []
        self.captures = 0
        self.capture_values = list(capture_values or [])
        self.paste_results = list(paste_results or [])
        self.failing_keys = failing_keys
        self.sleeps: list[float] = []

    def load(self, _name: str, text: str) -> bool:
        self.loads.append(text)
        return True

    def paste(self, _tmux: str, _name: str) -> bool:
        self.pastes += 1
        return self.paste_results.pop(0) if self.paste_results else True

    def key(self, _tmux: str, key: str) -> bool:
        self.keys.append(key)
        return key not in self.failing_keys

    def capture(self, _tmux: str) -> str:
        self.captures += 1
        if self.capture_values:
            return self.capture_values.pop(0)
        return "failure pane"

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _paster(tmux: _Tmux) -> TerminalPaster:
    return TerminalPaster(
        TerminalPasterSeams(
            load_buffer=tmux.load,
            paste_buffer=tmux.paste,
            send_key=tmux.key,
            capture_pane=tmux.capture,
            sleep=tmux.sleep,
            monotonic=_Clock(),
        )
    )


def test_success_uses_log_probe_and_never_captures_pane() -> None:
    tmux = _Tmux()
    result = _paster(tmux).paste("ar-1", "brief", submit=True, accepted=lambda: True)
    assert result.submitted
    assert tmux.loads == []
    assert tmux.keys == []
    assert tmux.captures == 0


def test_an_unwritable_buffer_reports_undelivered_and_never_presses_enter() -> None:
    # tmux refused the paste, so nothing reached the composer. Pressing Enter now would submit
    # whatever the composer already held, so the paster must stop with delivered=False instead.
    tmux = _Tmux(paste_results=[False])
    result = _paster(tmux).paste(
        "ar-1",
        "brief",
        submit=True,
        accepted=lambda: False,
        ladder=PasteRecoveryLadder(window=AcceptanceWindow(flush_window=1.0)),
    )
    assert not result.delivered
    assert not result.submitted
    assert result.capture == "failure pane"
    assert tmux.pastes == 1
    assert tmux.keys == []


def test_unobservable_pane_blocks_repaste() -> None:
    tmux = _Tmux(capture_values=["codex >", ""])
    result = _paster(tmux).paste(
        "ar-1",
        "brief",
        submit=True,
        accepted=lambda: False,
        ladder=PasteRecoveryLadder(window=AcceptanceWindow(flush_window=1.0)),
    )
    assert result.delivered
    assert not result.submitted
    assert result.capture == ""
    assert tmux.pastes == 1
    assert tmux.keys == ["Enter", "Enter"]


def test_dispatch_retry_submits_visible_same_draft_without_repaste() -> None:
    tmux = _Tmux(
        capture_values=[
            "[Pasted Content 5 chars]\n"
            "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} Explain this codebase"
        ]
    )
    result = _paster(tmux).paste_dispatch(
        "ar-1",
        "brief",
        accepted=lambda: tmux.keys == ["Enter"],
        policy=DispatchPastePolicy(attempt="recovery", visible_marker="entry: E1", harness="codex"),
    )
    assert result.submitted
    assert tmux.loads == []
    assert tmux.pastes == 0
    assert tmux.keys == ["Enter"]


def test_dispatch_retry_leaves_unrelated_codex_draft_pending() -> None:
    capture = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} do not overwrite me"
    tmux = _Tmux(capture_values=[capture])
    result = _paster(tmux).paste_dispatch(
        "ar-1",
        "brief",
        accepted=lambda: False,
        policy=DispatchPastePolicy(attempt="recovery", visible_marker="entry: E1", harness="codex"),
    )
    assert result.delivered
    assert not result.submitted
    assert result.capture == capture
    assert tmux.loads == []
    assert tmux.pastes == 0
    assert tmux.keys == []


def test_dispatch_retry_does_not_submit_historical_matching_marker() -> None:
    capture = (
        "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} entry: E1\n■ You've hit your usage limit."
    )
    tmux = _Tmux(capture_values=[capture])
    result = _paster(tmux).paste_dispatch(
        "ar-1",
        "brief",
        accepted=lambda: False,
        policy=DispatchPastePolicy(attempt="recovery", visible_marker="entry: E1", harness="codex"),
    )
    assert result.delivered
    assert not result.submitted
    assert tmux.loads == []
    assert tmux.pastes == 0
    assert tmux.keys == []
