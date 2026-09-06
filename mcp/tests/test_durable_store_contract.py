"""``ar-durable-store/1.0`` in ONE process: threads, re-entrancy, and the rewrite entry points.

``test_controlplane_store_durability.py`` is the loss proof and it uses ``multiprocessing``
deliberately -- the GIL would serialise the very window it forces. This file is the other axis of
the same contract, the one that harness cannot see: two THREADS of one process, which is what the
dashboard actually is. ``AttentionDismissalStore.dismiss`` is reached from the HTTP dismiss route
(``serving/app.py``) and ``prune_lifecycles`` from the projection sweep; both are whole-file
read-modify-writes over the same log, and attention-dismissals is both the store that measured the
worst loss at the base commit (31.45%) and the one wired to a button a person presses.

WHAT IS AND IS NOT BEING CLAIMED HERE. ``flock`` does exclude two threads of one process: the lock
lives on the open file description and ``exclusive_access`` opens a fresh one per acquisition, so
thread B blocks on thread A exactly as another process would. That was measured before this file
was written, not assumed, and it means the thread-level lost update was already closed. What was
NOT closed is that the exclusion rested on where the handle came from rather than on anything the
contract said: cache that handle on the store -- the obvious fix for an append path that opens two
files per record -- and every thread would share one description, ``flock`` would silently stop
excluding them, and no test in the tree would have noticed. ``thread_mutex_for`` makes the
in-process half of the exclusion a stated property, and the first test below asserts it directly
rather than inferring it from an ordering ``flock`` alone would also produce.

The re-entrancy case is here because that mutex is a second lock a thread can now hang itself on,
and the thread-local depth counter that already tolerates the first one has to compose with it.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from agents_remember.application.runtime import startup as server_startup
from agents_remember.controlplane import attention_dismissals as attention_module
from agents_remember.controlplane.attention_dismissals import (
    AttentionDismissalRecord,
    AttentionDismissalStore,
)
from agents_remember.controlplane.durable_store import (
    AGENT_NOTIFIER_SIGNAL_OWNERSHIP,
    DURABLE_STORE_CONTRACT,
    GATE_OWNERSHIP,
    ORCHESTRATION_NUDGE_OWNERSHIP,
    CompactionOwnerError,
    DurableStoreError,
    ProcessRole,
    UnsafeLockFilesystemError,
    append_line,
    declare_process_role,
    declared_process_role,
    exclusive_access,
    require_lock_held,
    rewrite_lines,
)
from agents_remember.controlplane.expectation_rows import (
    Expectation,
    ExpectationRowStore,
    create_expectation_row,
)
from agents_remember.controlplane.gate_decisions import _reclaim_gate_log
from agents_remember.controlplane.orchestration_nudges import (
    OrchestrationNudgeRecord,
    OrchestrationNudgeStore,
    replace_records,
)
from agents_remember.controlplane.records import GateAnchor, GateRecord, create_gate
from agents_remember.controlplane.store import GateStore
from agents_remember.kernel import atomic_write, file_lock
from agents_remember.kernel.file_lock import lock_path_for, thread_mutex_for
from agents_remember.mcp import server as server_module
from agents_remember_test_support.testing.global_state import preserve_owned_mutable_state
from pydantic import ValidationError
from test_config import settings_payload

# Long enough that a wedged lock fails the test instead of hanging the suite, short enough that a
# real deadlock is reported in seconds rather than at the pytest timeout.
WATCHDOG_SECONDS = 15.0

NOW = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)


class _TempRoot(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


def _contain_process_role(test: unittest.TestCase) -> None:
    state = preserve_owned_mutable_state()
    state.__enter__()
    test.addCleanup(state.__exit__, None, None, None)


def _drift(item_id: str) -> AttentionDismissalRecord:
    """A dismissal the prune keeps: no lifecycle, and ``actionable-drift`` survives on its own."""
    return AttentionDismissalRecord(
        itemId=item_id,
        kind="actionable-drift",
        dismissedAt="2026-06-14T10:00:00Z",
    )


def _stale(item_id: str, lifecycle_id: str) -> AttentionDismissalRecord:
    """A dismissal the prune drops once its lifecycle is no longer live."""
    return AttentionDismissalRecord(
        itemId=item_id,
        kind="stale-session",
        lifecycleId=lifecycle_id,
        dismissedAt="2026-06-14T10:00:00Z",
    )


class _IgnoredFlock:
    """``fcntl`` as WSL DrvFs presents it: every ``flock`` accepted, and none of them excluding.

    THE FILESYSTEM IS FAKED, NOT THE CODE. ``_verify_lock_capability``'s refusal only fires where
    a second ``flock`` on a second file description SUCCEEDS, which no correctly locking
    filesystem does and no test can mount -- so the substitution is at the one boundary the probe
    talks to, and it reproduces what DrvFs does literally: accept the call, take no lock. Every
    other name (``LOCK_EX``, ``LOCK_NB``, ``LOCK_UN``) forwards untouched, and only
    ``file_lock``'s own module reference is swapped, so nothing else in the interpreter
    silently loses its locks while a test runs.
    """

    @staticmethod
    def flock(_fileno: int, _operation: int) -> None:
        """Accepted and ignored -- which is precisely what makes a lock decorative."""

    def __getattr__(self, name: str) -> Any:
        return getattr(fcntl, name)


class _FailingOs:
    """``os`` as the atomic publish sees it, with exactly one call swapped for a failure.

    Scoped to ``kernel.atomic_write``'s module reference alone -- which is where
    ``rewrite_lines``' temp-write, rename and fsyncs have lived since L6 consolidated the
    package's thirteen copies of them. ``getpid``, ``open``, ``fsync``, ``close`` and
    ``O_RDONLY`` all forward to the real ``os``, so the rewrite under test is the shipped one
    everywhere except at the single instruction the scenario needs to fail.
    """

    def __init__(self, failing: str, error: BaseException) -> None:
        self._failing = failing
        self._error = error

    def _fail(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self._error

    def __getattr__(self, name: str) -> Any:
        if name == self._failing:
            return self._fail
        return getattr(os, name)


@pytest.mark.fitness
class InProcessExclusivityTests(_TempRoot):
    """The exclusion holds between THREADS, and is stated rather than inherited from ``flock``."""

    def _log(self) -> Path:
        return self.root / "workspace" / "gates.jsonl"

    @pytest.mark.integration
    @pytest.mark.usefixtures("worktree_services")
    def test_a_second_thread_is_kept_out_of_a_log_that_is_already_held(self) -> None:
        """Both halves: the in-process mutex is really held, and no second entry happens.

        The ordering assertion alone would pass on ``flock`` by itself, so it is not the
        interesting one -- the ``acquire(blocking=False)`` probe is. It asks the log's own mutex,
        from a thread that is not the holder, whether the log is claimed, and a False there is the
        in-process exclusion existing as a fact rather than as a consequence of how the lockfile
        happened to be opened.
        """
        log = self._log()
        holding = threading.Event()
        let_go = threading.Event()
        order: list[str] = []
        failures: list[BaseException] = []

        def holder() -> None:
            try:
                with exclusive_access(log, GATE_OWNERSHIP):
                    order.append("A-holds")
                    holding.set()
                    let_go.wait(WATCHDOG_SECONDS)
                    order.append("A-leaves")
            except BaseException as error:  # reported to the main thread, never swallowed
                failures.append(error)

        def follower() -> None:
            try:
                holding.wait(WATCHDOG_SECONDS)
                mutex = thread_mutex_for(log)
                claimed = not mutex.acquire(blocking=False)
                if not claimed:
                    mutex.release()
                order.append("B-refused" if claimed else "B-let-in-while-A-held")
                let_go.set()
                with exclusive_access(log, GATE_OWNERSHIP):
                    order.append("B-enters")
            except BaseException as error:  # reported to the main thread, never swallowed
                failures.append(error)

        threads = [
            threading.Thread(target=holder, daemon=True),
            threading.Thread(target=follower, daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WATCHDOG_SECONDS)

        self.assertEqual(failures, [])
        self.assertEqual([thread.is_alive() for thread in threads], [False, False])
        self.assertEqual(order, ["A-holds", "B-refused", "A-leaves", "B-enters"])

    def test_one_mutex_per_log_and_a_different_log_is_a_different_mutex(self) -> None:
        """A per-log mutex that handed out a new object per call would guard nothing.

        Two callers of the same log must be told about the SAME lock -- otherwise each would hold
        one the other does not -- and two different logs must not be told about one lock, or the
        gate log would serialise against every unrelated store on the box.
        """
        gates = self._log()
        nudges = self.root / "workspace" / "orchestration-nudges.jsonl"

        self.assertIs(thread_mutex_for(gates), thread_mutex_for(gates))
        self.assertIsNot(thread_mutex_for(gates), thread_mutex_for(nudges))

    def test_taking_a_logs_exclusivity_twice_on_one_thread_does_not_deadlock(self) -> None:
        """Re-entrancy, which the added mutex turns into a second way to hang.

        No store nests exclusivity today -- each splits its reclaim into a locking method and a
        ``_locked`` half so that it cannot -- but that is a convention held by six modules, and
        ``exclusive_access`` already spends a thread-local depth counter tolerating a breach of
        it, because ``flock`` on a second file description would deadlock a thread against
        itself. A non-re-entrant mutex would reintroduce that deadlock one layer below where the
        counter can see it, so the two locks have to tolerate re-entry together. This is the test
        that says so.

        Four things are asserted and the absence of a hang is only the first: the inner frame can
        drive a rewrite -- so ``require_lock_held`` sees the hold through the nesting -- the OUTER
        frame still holds after the inner one exits (a counter that popped instead of restoring
        would fail here, having silently released a lock its caller still believes it has), and
        the hold is genuinely gone once the outermost frame does.
        """
        log = self._log()
        done = threading.Event()
        outcome: dict[str, object] = {}

        def nested() -> None:
            try:
                with exclusive_access(log, GATE_OWNERSHIP):
                    append_line(log, json.dumps({"depth": 0}))
                    with exclusive_access(log, GATE_OWNERSHIP):
                        # Nested: the rewrite invariant must recognise the outer hold.
                        rewrite_lines(log, [json.dumps({"depth": 1})], GATE_OWNERSHIP)
                    # Back out one level, and the log is still ours to rewrite.
                    rewrite_lines(log, [json.dumps({"depth": 2})], GATE_OWNERSHIP)
                outcome["text"] = log.read_text(encoding="utf-8")
                try:
                    require_lock_held(log, "gate")
                except DurableStoreError:
                    outcome["released"] = True
            except BaseException as error:  # reported to the main thread, never swallowed
                outcome["error"] = error
            finally:
                done.set()

        thread = threading.Thread(target=nested, daemon=True)
        thread.start()

        self.assertTrue(done.wait(WATCHDOG_SECONDS), "nested exclusive_access deadlocked")
        thread.join(WATCHDOG_SECONDS)
        self.assertNotIn("error", outcome)
        # The innermost rewrite ran, and so did the one after the inner frame closed.
        self.assertEqual(outcome["text"], '{"depth": 2}\n')
        # ...and the outermost exit really let go: a rewrite now refuses.
        self.assertIs(outcome.get("released"), True)

    @pytest.mark.integration
    @pytest.mark.usefixtures("worktree_services")
    def test_a_dismissal_is_not_lost_to_a_prune_sweeping_on_another_thread(self) -> None:
        """The lost-update pair the dashboard actually runs, forced rather than raced.

        The projection sweep is parked between its read and its physical rewrite -- holding a
        filtered set that predates the dismissal about to arrive -- and the HTTP dismiss route
        runs on a second thread while it sits there. Without exclusion the sweep's stale set wins
        and the operator's click is discarded with no error: exactly the shape this leaf exists to
        remove, at thread scale instead of process scale.

        Only the moment of the physical write is interposed on; the store's own read, filter and
        commit are the real ones.
        """
        store = AttentionDismissalStore(self.root)
        store.dismiss(_drift("actionable-drift:keep"))
        store.dismiss(_stale("stale-session:L-dead", "L-dead"))

        parked = threading.Event()
        release = threading.Event()
        dismiss_started = threading.Event()
        dismiss_done = threading.Event()
        failures: list[BaseException] = []
        park_guard = threading.Lock()
        parked_already: list[bool] = []
        real_rewrite = attention_module.rewrite_lines

        def parking_rewrite(path: Path, lines: list[str], ownership: object) -> None:
            park = False
            with park_guard:
                if not parked_already:
                    parked_already.append(True)
                    park = True
            if park:
                parked.set()
                release.wait(WATCHDOG_SECONDS)
            real_rewrite(path, lines, ownership)  # type: ignore[arg-type]

        def sweep() -> None:
            try:
                store.prune_lifecycles(set())
            except BaseException as error:  # reported to the main thread, never swallowed
                failures.append(error)

        def dismiss() -> None:
            dismiss_started.set()
            try:
                store.dismiss(_drift("actionable-drift:new"))
            except BaseException as error:  # reported to the main thread, never swallowed
                failures.append(error)
            finally:
                dismiss_done.set()

        with patch.object(attention_module, "rewrite_lines", parking_rewrite):
            sweeper = threading.Thread(target=sweep, daemon=True)
            sweeper.start()
            self.assertTrue(parked.wait(WATCHDOG_SECONDS), "the sweep never reached its rewrite")
            dismisser = threading.Thread(target=dismiss, daemon=True)
            dismisser.start()
            self.assertTrue(dismiss_started.wait(WATCHDOG_SECONDS))
            # The window is open right now: the sweep is holding a set it read before this
            # dismissal existed. The dismisser must be shut out of it, not merely lucky.
            self.assertFalse(
                dismiss_done.wait(0.25),
                "the dismissal proceeded while a prune held the log -- the two overlap",
            )
            release.set()
            sweeper.join(WATCHDOG_SECONDS)
            dismisser.join(WATCHDOG_SECONDS)

        self.assertEqual(failures, [])
        self.assertEqual([sweeper.is_alive(), dismisser.is_alive()], [False, False])
        # The sweep's work AND the dismissal both survive: the dead lifecycle's row is gone, the
        # row it was told to keep is still there, and the click that raced it was not discarded.
        self.assertEqual(
            sorted(store.current()),
            ["actionable-drift:keep", "actionable-drift:new"],
        )


@pytest.mark.fitness
class UnsafeLockFilesystemTests(_TempRoot):
    """A lock that does not exclude is refused loudly, never downgraded to a silent no-op.

    The MCP process and the dashboard serialize through this lock on the assumption they share a
    host and a local POSIX filesystem. NFS and SMB emulate ``flock`` with per-process byte-range
    locks and WSL's DrvFs ignores it outright: on any of the three the second acquisition of the
    capability probe succeeds, the lock is decorative, and the two daemons would go back to
    losing records with nothing raised anywhere. That is the branch these tests hold shut, with
    the filesystem faked at the ``fcntl`` boundary (see :class:`_IgnoredFlock`) because it is not
    something a test can mount.
    """

    def _log(self) -> Path:
        return self.root / "workspace" / "gates.jsonl"

    def test_a_lock_that_does_not_exclude_is_refused_and_names_what_to_fix(self) -> None:
        """The refusal has to be actionable: which log, which lock file, and which filesystems.

        An operator reading this in a daemon's log needs to know their coordination root is the
        problem. The last two assertions are the ones that matter to durability: the caller never
        reached the body it wanted the lock for, and no log was created -- refusing to run is the
        contract, not writing on unserialized.
        """
        log = self._log()
        lock = lock_path_for(log)
        entered: list[str] = []

        with (
            patch.object(file_lock, "fcntl", _IgnoredFlock()),
            self.assertRaises(UnsafeLockFilesystemError) as raised,
            exclusive_access(log, GATE_OWNERSHIP),
        ):
            entered.append("body")  # only reached if the probe stopped running before the body

        message = str(raised.exception)
        self.assertIn(str(lock), message)
        self.assertIn(GATE_OWNERSHIP.store, message)
        self.assertIn("NFS, SMB or WSL DrvFs", message)
        # A durable-store contract violation, so a caller catching the family catches this too.
        self.assertIsInstance(raised.exception, DurableStoreError)
        self.assertEqual(entered, [])
        self.assertFalse(log.exists())

    def test_a_store_refuses_the_append_itself_and_recovers_once_the_lock_works(self) -> None:
        """The refusal reaches the shipped write path, and is about the filesystem, not the path.

        First half: ``GateStore.append`` -- an ordinary caller that never mentions locking -- gets
        the refusal and writes nothing, which is the whole point of probing before the write
        rather than after it. Second half: the same append on the same log succeeds the moment
        ``flock`` excludes again, so a failed probe is not a latch that poisons a path for the
        rest of the process.
        """
        store = GateStore(self.root)
        gate = create_gate("agent-question", gate_id="G1", now=NOW.isoformat())

        with patch.object(file_lock, "fcntl", _IgnoredFlock()):
            with self.assertRaises(UnsafeLockFilesystemError):
                store.append(gate)
            self.assertFalse(self._log().exists())

        store.append(gate)

        self.assertEqual([record.id for record in store.read(None)], ["G1"])


class SchemaVersionMajorTests(_TempRoot):
    """One rule -- an unknown MAJOR is not readable -- and the two read policies it decides.

    ``DurableRecord`` validates ``schemaVersion`` on the way in, and that single validator is
    what lets the strict and tolerant readers behave differently on a record from a future build
    without either of them carrying a version branch: validation raises, so the authority read
    fails loudly and the projection read skips the row. These tests assert the rule and that
    consequence, in that order.

    Distinct from the worktree contract's front-matter ``schemaVersion``
    (``test_worktree_contract_lifecycle.py``), which applies the same major/minor rule through
    the same :func:`schema_version_supported` helper to a different artifact.
    """

    def _gate(self, gate_id: str) -> GateRecord:
        return create_gate(
            "agent-question",
            gate_id=gate_id,
            now=NOW.isoformat(),
            anchor=GateAnchor(lifecycle_id="L1"),
        )

    def _append_future_major(self, log: Path, gate_id: str) -> None:
        """Append the row a build implementing a NEWER major would have written.

        Built by re-stamping a real record rather than hand-rolling JSON, so the row differs from
        a valid one in exactly the field under test and the refusal cannot be coming from
        anything else.
        """
        payload = json.loads(self._gate(gate_id).model_dump_json(by_alias=True, exclude_none=True))
        payload["schemaVersion"] = "2.0"
        with exclusive_access(log, GATE_OWNERSHIP):
            append_line(log, json.dumps(payload))

    def test_a_record_stamped_with_an_unknown_major_is_refused_at_the_boundary(self) -> None:
        """Refused where it enters, and the refusal says what this build can actually read."""
        with self.assertRaises(ValidationError) as raised:
            GateRecord(
                schemaVersion="2.0",
                id="G-future",
                ts=NOW.isoformat(),
                kind="agent-question",
                state="open",
            )

        message = str(raised.exception)
        self.assertIn("unsupported schemaVersion '2.0'", message)
        self.assertIn(DURABLE_STORE_CONTRACT, message)

    def test_an_unknown_minor_of_the_supported_major_is_accepted_and_kept(self) -> None:
        """The other half of the rule, and the reason it is stated as major/minor at all.

        A minor bump is additive, so a record carrying one is readable and must survive the round
        trip with the version it was written under -- not silently rewritten to this build's.
        """
        record = GateRecord(
            schemaVersion="1.9",
            id="G-minor",
            ts=NOW.isoformat(),
            kind="agent-question",
            state="open",
        )

        self.assertEqual(record.schemaVersion, "1.9")
        self.assertIn('"schemaVersion":"1.9"', record.model_dump_json(by_alias=True))

    def test_a_future_row_stops_the_authority_read_and_only_costs_projection_a_tick(self) -> None:
        """The read-policy split, driven by the version rule instead of by a torn line.

        The enforcement fold must not quietly drop a row it could not interpret -- an unreadable
        ``applied`` marker is a consumed approval the fold would report as never consumed -- so
        the strict read raises. The dashboard's tick must not 500 on the same log, so the
        tolerant read returns what it could read. The last assertion is the one that keeps the
        tolerance cheap: a skipped row is skipped for that read only, never written back, so the
        log still holds the future row for a build that can read it.
        """
        store = GateStore(self.root)
        store.append(self._gate("G1"))
        log = store.log_path("L1")
        self._append_future_major(log, "G-future")
        on_disk = log.read_bytes()

        self.assertEqual([record.id for record in store.read_for_projection("L1")], ["G1"])
        with self.assertRaises(ValidationError):
            store.read("L1")

        self.assertEqual(log.read_bytes(), on_disk)


class BlankLineToleranceTests(_TempRoot):
    """A blank line is not a record and not a torn line -- both readers step over it.

    Worth stating because the two policies are otherwise opposite (260731-EFA-L5 R8): a torn line
    stops the strict reader and is skipped by the tolerant one. A blank line is neither party's
    problem -- an interrupted flush or a hand-edited log leaves them -- so BOTH readers skip it,
    and the strict reader doing so is not the tolerance leaking across.
    """

    def _with_blank_line(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("\n", "\n\n", 1), encoding="utf-8")

    def test_a_blank_line_in_a_gate_log_costs_neither_reader_a_record(self) -> None:
        store = GateStore(self.root)
        for gate_id in ("G1", "G2"):
            store.append(
                create_gate(
                    "agent-question",
                    gate_id=gate_id,
                    now=NOW.isoformat(),
                    anchor=GateAnchor(lifecycle_id="L1"),
                )
            )
        self._with_blank_line(store.log_path("L1"))

        self.assertEqual([record.id for record in store.read_for_projection("L1")], ["G1", "G2"])
        self.assertEqual([record.id for record in store.read("L1")], ["G1", "G2"])

    def test_a_blank_line_in_an_expectation_log_costs_neither_reader_a_row(self) -> None:
        store = ExpectationRowStore(self.root)
        for row_id in ("E1", "E2"):
            store.append(
                create_expectation_row(
                    Expectation(kind="ack-by", source_id=f"src-{row_id}"),
                    row_id=row_id,
                    now=NOW.isoformat(),
                    due_at=(NOW + timedelta(minutes=5)).isoformat(),
                )
            )
        self._with_blank_line(store.log_path())

        self.assertEqual([row.id for row in store.read_for_projection()], ["E1", "E2"])
        self.assertEqual([row.id for row in store.read()], ["E1", "E2"])


@pytest.mark.fitness
class OrchestrationNudgeRewriteTests(_TempRoot):
    """The nudge log's two rewrite entry points: the safe one and the primitive it wraps."""

    def setUp(self) -> None:
        super().setUp()
        self.store = OrchestrationNudgeStore(self.root)

    def _nudge(self, nudge_id: str, *, reason: str = "inactive") -> OrchestrationNudgeRecord:
        return OrchestrationNudgeRecord(
            id=nudge_id,
            ts=NOW.isoformat(),
            state="sent",
            reason=reason,  # type: ignore[arg-type]
            targetAgentId="agent-1",
            message=f"nudge {nudge_id}",
        )

    def test_compact_drops_what_keep_rejects_and_reports_the_count(self) -> None:
        for nudge_id in ("N1", "N2", "N3"):
            self.store.append(self._nudge(nudge_id))

        dropped = self.store.compact(keep=lambda record: record.id == "N2")

        self.assertEqual(dropped, 2)
        self.assertEqual([record.id for record in self.store.read()], ["N2"])
        # The survivor is a whole record, not a truncated line: the rewrite re-serialises it.
        self.assertEqual(self.store.read()[0].message, "nudge N2")

    def test_compact_that_keeps_everything_leaves_the_file_byte_identical(self) -> None:
        """The no-op branch must not rewrite: a rewrite that changes nothing still replaces the
        inode, which is a window an appender can be caught in for no gain at all."""
        for nudge_id in ("N1", "N2"):
            self.store.append(self._nudge(nudge_id))
        before = self.store.log_path().read_bytes()

        dropped = self.store.compact(keep=lambda _record: True)

        self.assertEqual(dropped, 0)
        self.assertEqual(self.store.log_path().read_bytes(), before)

    def test_compact_empties_the_log_without_unlinking_it(self) -> None:
        self.store.append(self._nudge("N1"))

        dropped = self.store.compact(keep=lambda _record: False)

        self.assertEqual(dropped, 1)
        self.assertEqual(self.store.read(), [])
        self.assertTrue(self.store.log_path().is_file())
        self.assertEqual(self.store.log_path().read_bytes(), b"")

    def test_replace_records_rewrites_the_log_when_the_caller_holds_the_lock(self) -> None:
        path = self.store.log_path()
        self.store.append(self._nudge("N1"))
        self.store.append(self._nudge("N2"))

        with exclusive_access(path, ORCHESTRATION_NUDGE_OWNERSHIP):
            kept = [record for record in self.store.read() if record.id == "N1"]
            replace_records(path, kept)

        self.assertEqual([record.id for record in self.store.read()], ["N1"])

    def test_replace_records_refuses_an_unlocked_caller_and_changes_nothing(self) -> None:
        """The refusal IS the durability property, not a tidiness check.

        ``records`` was chosen by a read this call did not make. Unlocked, everything appended
        since that read is about to be discarded -- so the refusal has to leave the log exactly as
        it found it, which is the second assertion here.
        """
        path = self.store.log_path()
        self.store.append(self._nudge("N1"))
        before = path.read_bytes()

        with self.assertRaises(DurableStoreError) as raised:
            replace_records(path, [])

        self.assertIn("without holding", str(raised.exception))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([record.id for record in self.store.read()], ["N1"])


@pytest.mark.fitness
class FailedRewriteTests(_TempRoot):
    """A rewrite that dies mid-flight leaves no temp file behind and no damage to the log.

    ``rewrite_lines`` is the only destructive rewrite in the control plane, and its temp file is
    the one artifact of it that outlives a failure. A stranded ``.gates.jsonl.<pid>.<uuid>.tmp``
    is not cosmetic: it is a partially written copy of an authority log sitting next to it, and
    colliding temp names are the defect that turned the dashboard's dismiss route into a 500
    before this contract existed. (The temp carries a uuid as well as the pid since L6: the pid
    alone still let two threads in one process share one temp path.)

    The failure is injected at the ``os`` boundary (see :class:`_FailingOs`) because these two
    instructions cannot be made to fail from the outside: a full disk or a crashing rename is not
    reproducible on demand. Everything asserted is on the filesystem afterwards, never on the
    substitution.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store = GateStore(self.root)
        self.store.append(
            create_gate(
                "agent-question",
                gate_id="G1",
                now=NOW.isoformat(),
                anchor=GateAnchor(lifecycle_id="L1"),
            )
        )
        self.log = self.store.log_path("L1")

    def _temp_names(self) -> list[str]:
        return sorted(path.name for path in self.log.parent.iterdir() if path.name.endswith(".tmp"))

    def _rewrite_under(self, failure: _FailingOs) -> BaseException:
        before = self.log.read_bytes()
        with (
            # The lock is taken OUTSIDE the substitution and released after it: the failure under
            # test is the rewrite's, not the lock's, and ``exclusive_access`` never touches ``os``.
            exclusive_access(self.log, GATE_OWNERSHIP),
            patch.object(atomic_write, "os", failure),
            self.assertRaises(BaseException) as raised,
        ):
            rewrite_lines(self.log, [], GATE_OWNERSHIP)
        # The log the rewrite was about to replace is byte-identical, and still readable as the
        # record it held: a rewrite that failed must not be able to damage what it was replacing.
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual([record.id for record in self.store.read("L1")], ["G1"])
        # Nothing pid-scoped survives the failure, whatever the pid.
        self.assertEqual(self._temp_names(), [])
        return raised.exception

    def test_a_failed_rename_removes_the_temp_file_it_had_already_written(self) -> None:
        """The worst moment to fail: the temp file exists, is fsynced, and the rename did not run.

        The pid-scoped prefix is asserted as well as the directory sweep, so this test says which
        file it means -- the name the contract promises -- rather than only that no ``.tmp`` is
        left. The trailing uuid is not spelled out because it is per call by design.
        """
        prefix = f".{self.log.name}.{os.getpid()}."

        error = self._rewrite_under(_FailingOs("replace", OSError(errno.EIO, "rename failed")))

        self.assertIsInstance(error, OSError)
        self.assertEqual(getattr(error, "errno", None), errno.EIO)
        self.assertEqual(list(self.log.parent.glob(f"{prefix}*.tmp")), [])

    def test_an_interrupted_rewrite_cleans_up_too(self) -> None:
        """Why the handler catches ``BaseException`` and not ``Exception``.

        ``KeyboardInterrupt`` is not an ``Exception``: an operator pressing Ctrl-C during a
        compaction -- or a pytest timeout, or ``SystemExit`` from a shutdown path -- would slip
        past a narrower handler and strand the temp file. It is raised here from the ``fsync``
        inside the temp write, so the interrupt lands at a different instruction from the test
        above and both positions inside the guarded block are covered.
        """
        error = self._rewrite_under(_FailingOs("fsync", KeyboardInterrupt()))

        self.assertIsInstance(error, KeyboardInterrupt)
        self.assertNotIsInstance(error, Exception)


class GateDeleteTests(_TempRoot):
    """``GateStore.delete`` answers whether it removed anything, and touches nothing when not."""

    def setUp(self) -> None:
        super().setUp()
        self.store = GateStore(self.root)
        self.store.append(
            create_gate(
                "agent-question",
                gate_id="G1",
                now=NOW.isoformat(),
                anchor=GateAnchor(lifecycle_id="L1"),
            )
        )

    def test_deleting_an_id_that_is_not_there_is_false_and_rewrites_nothing(self) -> None:
        """False, and no rewrite -- a delete that missed must not replace the inode anyway.

        ``delete`` is called with an id a caller believes is present (a cancel reclaiming its
        gate). When that belief is wrong the answer must be an honest False rather than a silent
        success, and the log must be left alone: rewriting it would open the same
        appender-versus-rewriter window for a call that had nothing to do.
        """
        before = self.store.log_path("L1").read_bytes()

        self.assertFalse(self.store.delete("G-absent", "L1"))

        self.assertEqual(self.store.log_path("L1").read_bytes(), before)
        self.assertEqual([record.id for record in self.store.read("L1")], ["G1"])

    def test_deleting_an_id_that_is_there_is_true_and_removes_it(self) -> None:
        self.assertTrue(self.store.delete("G1", "L1"))

        self.assertEqual(self.store.read("L1"), [])
        self.assertTrue(self.store.log_path("L1").is_file())


class ProcessRoleDeclarationTests(_TempRoot):
    """The MCP entry point declares which durable-store writer this process is.

    The declaration this makes is process-global and has no reset, so it MUST be undone before
    the next test runs -- left standing it would make every later test in this interpreter claim
    to be the MCP server, and ``check_declared_writer`` would start refusing the dashboard-only
    stores. That undoing is no longer done here: the autouse ``restore_declared_process_role``
    fixture in ``mcp/tests/conftest.py`` does it for every test in the tree, because this file was
    never the only one that declares (``cli/dashboard.py::run`` declares ``"dashboard"`` and
    ``test_serving.py::CliRunTests`` calls it seven times). ``ProcessRoleIsolationTests`` below is
    the proof that the fixture actually restores.
    """

    def setUp(self) -> None:
        super().setUp()
        _contain_process_role(self)

    def test_main_declares_the_mcp_role_before_the_server_starts_serving(self) -> None:
        """Declared at the entry point, before anything can write, and load-bearing once made.

        The ordering is the point: a role declared after the server is running would leave the
        first writes of the process unattributed. ``run_server`` therefore records what the role
        was at the moment it was called, and the writer check is then exercised in both
        directions -- the gate log names ``mcp`` among its writers and must accept this process;
        supervisor-signals names only the dashboard and must refuse it.
        """
        settings = self.root / "mcp-settings.json"
        settings.write_text(json.dumps(settings_payload(self.root)), encoding="utf-8")
        role_when_serving: list[str | None] = []

        with (
            patch.object(server_startup, "maybe_autostart_dashboard"),
            patch.object(
                server_module,
                "run_server",
                lambda _config: role_when_serving.append(declared_process_role()),
            ),
        ):
            code = server_module.main(["--config", str(settings)])

        self.assertEqual(code, 0)
        self.assertEqual(role_when_serving, ["mcp"])
        self.assertEqual(declared_process_role(), "mcp")
        GATE_OWNERSHIP.check_declared_writer()
        with self.assertRaises(CompactionOwnerError):
            AGENT_NOTIFIER_SIGNAL_OWNERSHIP.check_declared_writer()


class ProcessRoleIsolationTests(unittest.TestCase):
    """``restore_declared_process_role`` (``mcp/tests/conftest.py``) really does restore.

    A MUTUALLY-CHECKING PAIR, and that is the whole design. Each test asserts the process is
    undeclared BEFORE declaring a role of its own, so whichever of the two runs second is the one
    that fails when the fixture leaks -- in either order, and the two are run in both orders
    explicitly rather than being trusted to pytest's collection order. A one-directional
    "declare here, assert clean there" pair would prove the fixture only for the order it was
    written in, which is the same class of defect as a test that cannot fail.

    The two roles differ so a leak surfaces as the wrong role rather than as a bare ``None``
    mismatch, and ``dashboard`` is the one this suite actually leaks in practice:
    ``test_serving.py::CliRunTests`` reaches ``cli/dashboard.py::run``, whose first statement is
    ``declare_process_role("dashboard")``.

    ``_declared`` is not read directly here on purpose -- ``declared_process_role()`` is the
    accessor the two ownership predicates use, so this asserts what they would see.
    """

    def setUp(self) -> None:
        _contain_process_role(self)

    def _declare_after_asserting_a_clean_process(self, role: ProcessRole) -> None:
        self.assertIsNone(
            declared_process_role(),
            "another test's declare_process_role escaped into this one; the autouse "
            "restore_declared_process_role fixture in mcp/tests/conftest.py is what undoes it, "
            "and a leaked role silently changes is_compaction_owner and check_declared_writer",
        )
        declare_process_role(role)
        self.assertEqual(
            declared_process_role(),
            role,
            "the fixture must restore between tests, never clear during one",
        )

    def test_a_declared_mcp_role_does_not_escape_into_its_neighbour(self) -> None:
        self._declare_after_asserting_a_clean_process("mcp")

    def test_a_declared_dashboard_role_does_not_escape_into_its_neighbour(self) -> None:
        self._declare_after_asserting_a_clean_process("dashboard")


class GateReclaimOwnershipTests(_TempRoot):
    """The one call site that ASKS ``is_compaction_owner`` must act on a No.

    ``controlplane/gate_decisions.py::_reclaim_gate_log`` owns gate compaction after this leaf took
    it off the dashboard's projection tick. Both the MCP application composition and dashboard
    serving composition reach that same lower service, so its ownership guard is the whole of
    what keeps the moved reclaim from simply running in the old process again.

    It had no test. The guard's skip branch was reached only as a side effect of a role LEAKING
    out of ``test_serving.py::CliRunTests`` into ``test_tool_response_conformance.py``, which
    happens to sort after it -- so the evidence for this leaf's R1 was an artefact of filename
    order, and closing the leak (``restore_declared_process_role``) is what exposed that. Both
    roles are exercised here on one log so the contrast is the assertion: the same call, the same
    reclaimable record, and the outcome decided solely by who this process says it is.
    """

    def setUp(self) -> None:
        super().setUp()
        _contain_process_role(self)
        self.store = GateStore(self.root)
        # Fresh stamps, not the module's fixed NOW: ``_reclaim_gate_log`` compacts against the
        # real clock, and a gate older than INTERACTION_RECORD_TTL_SECONDS would be dropped by
        # retention rather than by the branch under test.
        stamp = datetime.now(UTC).isoformat()
        self.store.append(
            GateRecord(
                id="G-open", ts=stamp, kind="closeout-approval", state="open", lifecycleId="L1"
            )
        )
        # ``expired`` is in PRUNE_IMMEDIATE_GATE_STATES, so a reclaim that runs must drop it --
        # and one record surviving beside it keeps the rewrite off the unlink branch. Deliberately
        # not ``applied``: since 260731-EFA-L5 R1 a consumed approval is retained at any age, so an
        # applied record would be dropped by nobody and both halves of this test would pass for the
        # wrong reason.
        self.store.append(
            GateRecord(
                id="G-expired",
                ts=stamp,
                kind="closeout-approval",
                state="expired",
                lifecycleId="L1",
            )
        )

    def _ids(self) -> list[str]:
        return sorted(record.id for record in self.store.read("L1"))

    def test_the_dashboard_skips_the_gate_reclaim_and_the_mcp_process_performs_it(self) -> None:
        declare_process_role("dashboard")
        _reclaim_gate_log(self.store, "L1", now=datetime.now(UTC))
        self.assertEqual(
            self._ids(),
            ["G-expired", "G-open"],
            "the dashboard is not the gate log's compaction owner; reclaiming here is exactly "
            "the misplaced reclaim pass this leaf removed from the projection tick",
        )

        declare_process_role("mcp")
        _reclaim_gate_log(self.store, "L1", now=datetime.now(UTC))
        self.assertEqual(
            self._ids(),
            ["G-open"],
            "the owner must actually reclaim -- a guard that skips for everyone would pass the "
            "assertion above while leaving the log to grow forever",
        )


if __name__ == "__main__":
    unittest.main()
