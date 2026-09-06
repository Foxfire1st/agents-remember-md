"""Prevent lost writes during store compaction and distinguish strict from display reads.

The compaction regression uses separate processes to exercise the actual file-lock
boundary. Torn gate records must block authority while leaving display reads usable.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _store_durability import (
    ADAPTERS,
    CASES,
    run_forced_lost_update,
)
from agents_remember.controlplane.records import GateRecord
from agents_remember.controlplane.store import GateStore
from agents_remember.serving.projections.paths import observer_logs_root
from agents_remember.serving.projections.snapshots import read_gates
from pydantic import ValidationError

# A line cut off mid-write: exactly what a crash or an interleaved append leaves behind.
TORN_LINE = '{"schema":"ar-gate-record/v1","id":"torn","ts":"2026-0'

# Which reads are correctness-bearing (a dropped row changes a decision) and which only paint a
# screen. Derived from the call sites, not from the docstrings:
#   gate            -> worktrees/modules/closeout.py, worktrees/modules/integrate.py and
#                      serving/hosted_interactions.py fold it to permit or refuse a mutation, none
#                      of them wrapping the read in a suppression.
#   expectation     -> the L2 overdue sweep reads it directly (observer/snapshots.py calls itself
#                      "surfacing only" and says so).
#   operator_inbox  -> mcp/tools/operator_inbox.py consumes rows; a consume is the ack of record.
#   attention       -> observer/snapshots.py + observer/projection_inputs.py only: dashboard state.
#   nudge           -> rate-limit bookkeeping for orchestration nudges.
#   agent-notifier -> the sweep's cooldown memory, documented as non-authoritative.


def _describe(result: dict[str, object]) -> str:
    attempted = int(result["attempted"])  # type: ignore[arg-type]
    completed = int(result.get("completed", attempted))  # type: ignore[arg-type]
    lost = int(result["lost"])  # type: ignore[arg-type]
    percent = (100.0 * lost / completed) if completed else 0.0
    return (
        f"{result['case']} / {result['scenario']}: attempted={attempted} completed={completed}; "
        f"{lost} completed records "
        f"written were missing afterwards ({percent:.2f}% loss); "
        f"sample={result['lost_sample']} torn_lines={result['torn_lines']} "
        f"reclaim_attempts={result.get('reclaim_attempts')} "
        f"successful_reclaims={result.get('successful_reclaims')} "
        f"stragglers={result['stragglers']}"
    )


class _TempRootTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def _tear(self, case: str) -> Path:
        """Seed one intact survivor for ``case`` and append a torn line after it."""
        return self._tear_mid_log(case, ("survivor-intact",), ())

    def _tear_mid_log(self, case: str, before: tuple[str, ...], after: tuple[str, ...]) -> Path:
        """Seed the ``before`` records, write a torn line, then seed the ``after`` records.

        A non-empty ``after`` puts the torn line INSIDE the log rather than at its end, which is
        what separates a reader that drops one row from a reader that stops at the first bad one:
        the trailing records are only reachable if the skip resumes.
        """
        adapter = ADAPTERS[case]()
        root = observer_logs_root(self.tmp)
        store = adapter.open(root)
        for record_id in before:
            adapter.write(store, record_id)
        with adapter.log_path(root).open("a", encoding="utf-8") as handle:
            handle.write(TORN_LINE + "\n")
        for record_id in after:
            adapter.write(store, record_id)
        return root


class MultiProcessDurabilityTests(_TempRootTest):
    """R10: N processes appending while one compacts, over all six record types."""

    @pytest.mark.evidence_integration
    def test_no_record_is_lost_when_an_append_races_a_compaction(self) -> None:
        """The lost-update window: an append lands between the reclaim's read and its commit.

        Forced rather than raced, so the result is the same on every run and on every machine --
        "no flakes" cuts both ways, and a stochastic reproduction of a narrow window is exactly
        the kind of test that passes on a loaded CI box and proves nothing.
        """
        for case in CASES:
            with self.subTest(store=case):
                result = run_forced_lost_update(case, self.tmp / f"lost-{case}")
                self.assertEqual(result["attempted"], 1, _describe(result))
                self.assertEqual(result["lost"], 0, _describe(result))
                self.assertEqual(result["stragglers"], [], _describe(result))


class TornLinePolicyTests(_TempRootTest):
    """R8: the fold that decides and the fold that renders need opposite torn-line policies.

    Which is why gates and expectation-rows each ship TWO readers instead of one compromise:
    ``read`` raises for the enforcement fold, ``read_for_projection`` skips the bad row and keeps
    going for the dashboard. Both halves are pinned below, on both logs -- a strict reader that
    started skipping and a tolerant reader that started raising are each a defect, and each has a
    test here that fails on it.
    """

    def _seed_gate(self) -> None:
        # ``open`` and not ``applied``/``cancelled``/``expired``: the projection keep-filter prunes
        # those, and a gate the filter removed would prove nothing about torn-line handling.
        GateStore(observer_logs_root(self.tmp)).append(
            GateRecord(
                id="gate-intact",
                ts=datetime.now(UTC).isoformat(),
                kind="closeout-approval",
                state="open",
            )
        )

    def _append_torn_line(self) -> None:
        path = observer_logs_root(self.tmp) / "workspace" / "gates.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(TORN_LINE + "\n")

    def test_gate_enforcement_fold_refuses_a_torn_line(self) -> None:
        """The enforcement fold must fail loudly, never skip.

        ``current`` / ``all_current`` are what ``worktrees/modules/closeout.py``,
        ``worktrees/modules/integrate.py`` and ``serving/hosted_interactions.py`` read to decide
        whether a mutation may proceed, and none of them wraps the read in a suppression. Skipping
        a malformed line there could drop exactly the ``applied`` marker that closes the replay
        window, and the second mutation would be permitted with no error and no log line.
        """
        self._seed_gate()
        self._append_torn_line()
        store = GateStore(observer_logs_root(self.tmp))

        for path_name, read in (
            ("read", lambda: store.read(None)),
            ("current", lambda: store.current(None)),
            ("all_current", store.all_current),
        ):
            with self.subTest(enforcement_path=path_name), self.assertRaises(ValidationError):
                read()

    def test_gate_projection_fold_degrades_instead_of_crashing(self) -> None:
        """The dashboard fold over the same log must survive a torn line, per row.

        ``observer/snapshots.read_gates`` is the projection side; it runs on the 1s tick and
        behind the dashboard's ASGI handlers. A single malformed row must neither raise nor cost
        the reader every other gate in that log -- degrading to "one row missing" is a dashboard
        degrading; degrading to "no gates at all" is the projection losing its content.
        """
        self._seed_gate()
        self._append_torn_line()

        gates = read_gates(self.tmp, now=datetime.now(UTC))

        self.assertEqual(
            [gate.id for gate in gates],
            ["gate-intact"],
            "the projection must still surface the intact gates beside a torn line",
        )
