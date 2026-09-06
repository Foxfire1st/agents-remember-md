"""One human approval must be consumable exactly once (260731-EFA-L5 R11).

The mechanism, as it actually exists in the tree:

1. ``worktrees/modules/closeout.py::_claim_closeout_gate`` calls
   ``controlplane/store.py::GateStore.claim_approval``, which folds the lifecycle's gate log, asks
   ``controlplane/enforcement.py::evaluate_gate`` whether the mutation may proceed, and -- if it
   may -- appends ``controlplane/records.py::apply_gate``, a fresh snapshot with the same gate id
   and ``state="applied"``. All three inside ONE held ``exclusive_access``.
2. ``evaluate_gate`` has an explicit ``applied`` branch that refuses: *"gate <id> was already
   applied; open a fresh gate for a new mutation"*.
3. The claim is taken IMMEDIATELY BEFORE the first irreversible act (the code commit), so an
   approval authorises one attempt rather than one success.

So the entire defence against replaying a human approval is **one appended record**. There is no
second signal: no in-memory flag, no separate marker file, no timestamp comparison. Three
independent things can therefore take that record away, and all three were real:

* it is LOST, which is what the unlocked ``GateStore`` did whenever a compaction rewrote the log
  underneath it (:class:`GateReplayWindowTests`);
* it is RECLAIMED, because ``applied`` used to be pruned at any age, so any later decision on the
  lifecycle erased it (:class:`AppliedMarkerRetentionTests`, R1);
* it is never the deciding record at all, because the check and the write were two steps with the
  whole closeout between them (:class:`ApprovalClaimAtomicityTests`, R2) and the write came after
  the mutations rather than before (:class:`ClaimPrecedesTheIrreversibleWorkTests`, R3).

Each class below reproduces one of those and pins it shut.
"""

from __future__ import annotations

import io
import multiprocessing
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from _store_durability import parked_rewrite
from agents_remember.controlplane.enforcement import CLOSEOUT_GATE_KIND
from agents_remember.controlplane.gate_decisions import _reclaim_gate_log
from agents_remember.controlplane.interaction_retention import (
    CONSUMED_APPROVAL_GATE_KINDS,
    INTERACTION_RECORD_TTL_SECONDS,
    gate_keep_ids,
)
from agents_remember.controlplane.records import (
    GateAnchor,
    GateRecord,
    GateVerdict,
    create_gate,
    decide_gate,
)
from agents_remember.controlplane.store import GateStore
from agents_remember.serving.projections.paths import observer_logs_root
from agents_remember.worktrees.modules import closeout as closeout_mod
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.integrate import HANDOVER_GATE_KIND
from agents_remember.worktrees.queue import closeout_recovery, closeout_staged_quality
from agents_remember.worktrees.worktree_contract import write_contract
from pydantic import ValidationError
from test_worktree_support import (
    closeout_args,
    dirty_open_external_contract_fixture,
    git,
    run_authorized_closeout_mechanics,
    write_passing_route_review,
)

LIFECYCLE = "L-replay"
GATE_ID = "01HREPLAY"
APPLIED_MARKER = '"state":"applied"'

# How one racing closeout reports itself back across the process boundary. ``CRASHED`` is
# distinct from ``REFUSED`` on purpose: a child that died for an unrelated reason would otherwise
# be counted as "correctly refused" and the race assertion would pass without racing anything.
REFUSED, PERMITTED, CRASHED = 0, 1, -1


def _seed_approved_gate(coordination_root: Path) -> str:
    """An open ``closeout-approval`` gate approved by the developer -- a real human approval.

    ``decidedBy="developer"`` is the anti-self-approval boundary: the agent's own ``gate_decide``
    records ``decidedBy="model"``, which ``approval_failure_reason`` refuses. Only a decision the
    human actually made reaches the branch this test is about.

    The timestamps are minted fresh rather than fixed. ``gate_keep_ids`` drops any gate older than
    ``INTERACTION_RECORD_TTL_SECONDS`` (24h), so a hard-coded stamp would let *retention* delete
    the gate and the concurrency test would "fail" for a reason that has nothing to do with
    durability -- and, worse, could "pass" for one.

    That leaves exactly two ways a gate seeded here can leave the log, and both are things a test
    in this file is about rather than background noise: it is LOST to a racing rewrite, or it is
    physically deleted. It cannot age out (the stamps are seconds old) and it cannot be reclaimed
    once consumed (an ``applied`` snapshot of a ``CONSUMED_APPROVAL_GATE_KINDS`` gate is retained
    with no TTL -- :class:`AppliedMarkerRetentionTests` is what holds that shut).
    """
    now = datetime.now(UTC)
    store = GateStore(observer_logs_root(coordination_root))
    gate = create_gate(
        "closeout-approval",
        gate_id=GATE_ID,
        now=(now - timedelta(seconds=5)).isoformat(),
        anchor=GateAnchor(lifecycle_id=LIFECYCLE),
    )
    store.append(gate)
    store.append(
        decide_gate(
            gate,
            GateVerdict(decision="approve", by="developer", via="dashboard", note=None),
            now=now.isoformat(),
        )
    )
    return gate.id


def _prunable_gate(index: int) -> GateRecord:
    """A gate the compactor will drop, so a compaction pass actually rewrites the log.

    ``expired`` rather than ``applied``: an ``applied`` snapshot of a consumed-approval kind is now
    retained forever (R1), so using one here would make the compaction a no-op and the concurrency
    scenario below would prove nothing. ``alarm-ack`` keeps the kind honest -- nothing decides on
    it -- while ``expired`` is the state a superseded gate genuinely reaches.
    """
    return GateRecord(
        id=f"spent-{index}",
        ts=datetime.now(UTC).isoformat(),
        kind="alarm-ack",
        state="expired",
        lifecycleId=LIFECYCLE,
    )


def _claim(root: Path) -> Any:
    contract = SimpleNamespace(lifecycle_id=LIFECYCLE, coordination_root=root)
    return closeout_mod._claim_closeout_gate(contract, WorktreeArgs())


def _consumer_main(root: str, ready: Any, done: Any) -> None:
    """Run the real closeout claim once the compactor is parked mid-rewrite."""
    ready.wait(10.0)
    try:
        _claim(Path(root))
    finally:
        done.set()


def _compactor_main(root: str, ready: Any, done: Any) -> None:
    """Compact the lifecycle's gate log, parked between its read and its commit."""
    store = GateStore(observer_logs_root(Path(root)))
    store.append(_prunable_gate(0))
    with parked_rewrite(ready, done, 3.0):
        store.compact(LIFECYCLE, now=datetime.now(UTC))


def _racing_closeout_main(root: str, start: Any, results: Any, index: int) -> None:
    """One of two real closeouts racing the claim.

    Nothing is slept between fold and append because there is nowhere left to sleep: that is the
    fix. The shape this replaces put the code commit, the memory quality gate, the memory commit,
    the ledger commit and ``write_contract`` between the two halves, so the window was not narrow,
    it was the whole closeout -- and a single ``claim_approval`` is what removes the between.
    """
    start.wait(10.0)
    try:
        _claim(Path(root))
    except RuntimeError:
        results[index] = REFUSED
    except BaseException:
        # A crash must not read as a refusal: `CRASHED` is what stops a child that died for an
        # unrelated reason from being counted as this test's "correctly refused" arm.
        results[index] = CRASHED
    else:
        results[index] = PERMITTED


class GateReplayWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.coordination_root = Path(tmp.name)
        self.store = GateStore(observer_logs_root(self.coordination_root))
        self.contract = SimpleNamespace(
            lifecycle_id=LIFECYCLE, coordination_root=self.coordination_root
        )

    def _log(self) -> Path:
        return self.store.log_path(LIFECYCLE)

    def _guard(self) -> Any:
        return closeout_mod._closeout_gate_guard(self.contract, WorktreeArgs())

    def _claim(self) -> Any:
        return closeout_mod._claim_closeout_gate(self.contract, WorktreeArgs())

    def _tear_the_applied_record(self) -> None:
        """Cut the log's last line short of its ``applied`` marker: a half-written append.

        Deliberately NOT an extra torn line appended beside an intact ``applied`` snapshot. That
        arrangement leaves the gate genuinely applied, so the claim would raise its already-applied
        ``RuntimeError`` even with the torn line deleted, and any assertion about "it raised" would
        be satisfied by an exception that has nothing to do with tearing. Tearing the ``applied``
        marker itself is the case the strict read exists for, and the only one where refusing and
        permitting the replay are the two outcomes on offer.

        The precondition is asserted rather than assumed: if the closeout's write order ever
        changes so the applied snapshot is no longer the last line, this fails loudly instead of
        quietly tearing something else.
        """
        lines = self._log().read_text(encoding="utf-8").splitlines()
        self.assertIn(
            APPLIED_MARKER, lines[-1], "expected the applied snapshot to be the log's last write"
        )
        torn = lines[-1].split(APPLIED_MARKER)[0]
        self.assertNotIn(APPLIED_MARKER, torn, "the tear must remove the marker it is about")
        # No trailing newline: the write died mid-record, which is what leaves a torn line.
        self._log().write_text("\n".join([*lines[:-1], torn]), encoding="utf-8")

    def test_a_consumed_approval_cannot_be_replayed_by_a_second_closeout(self) -> None:
        """The contract itself: approve once, claim once, and the second closeout is refused."""
        gate_id = _seed_approved_gate(self.coordination_root)

        first = self._claim()
        self.assertIsNotNone(first)
        assert first is not None
        self.assertTrue(first.permitted, first.reason)
        self.assertEqual(first.gate_id, gate_id)
        self.assertEqual(self.store.current(LIFECYCLE)[gate_id].state, "applied")

        with self.assertRaises(RuntimeError) as refused:
            self._claim()
        self.assertIn("already applied", str(refused.exception))

    def test_the_applied_record_is_the_only_thing_closing_the_window(self) -> None:
        """Delete just that one appended record and the same approval is spendable again.

        This is the counterfactual that makes durability load-bearing rather than tidy: it is not
        that a lost record degrades the audit trail, it is that losing this specific record hands
        a second closeout a human approval it was never given.
        """
        _seed_approved_gate(self.coordination_root)
        self._claim()

        surviving = [
            line
            for line in self._log().read_text(encoding="utf-8").splitlines()
            if line.strip() and APPLIED_MARKER not in line
        ]
        self.assertEqual(len(surviving), 2, "expected the open and approved snapshots to remain")
        self._log().write_text("\n".join(surviving) + "\n", encoding="utf-8")

        replayed = self._guard()
        self.assertIsNotNone(replayed)
        assert replayed is not None
        self.assertTrue(
            replayed.permitted,
            "losing the applied snapshot must be what re-opens the replay window -- if this "
            "assertion fails, the enforcement mechanism changed and the durability requirement "
            "it justifies has to be re-derived",
        )

    @pytest.mark.evidence_integration
    def test_the_applied_record_survives_a_concurrent_gate_log_compaction(self) -> None:
        """The regression: the consume races a compaction of the same log, in real processes.

        The compactor is parked between its read and its commit while the closeout claim runs to
        completion, which is the ordinary interleaving of an MCP tool applying a closeout while
        another writer reclaims the same gate log. At the leaf's base commit the ``applied``
        snapshot lands in the compactor's stale snapshot's blind spot and is gone, so the second
        closeout is permitted and the approval is spent twice.
        """
        _seed_approved_gate(self.coordination_root)
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        done = context.Event()
        root = str(self.coordination_root)
        compactor = context.Process(target=_compactor_main, args=(root, ready, done))
        consumer = context.Process(target=_consumer_main, args=(root, ready, done))
        compactor.start()
        consumer.start()
        for process in (compactor, consumer):
            process.join(30.0)
            self.assertFalse(process.is_alive(), "durability scenario failed to terminate")

        self.assertEqual(
            self.store.current(LIFECYCLE)[GATE_ID].state,
            "applied",
            "the consume's applied snapshot was lost to the concurrent compaction",
        )
        with self.assertRaises(RuntimeError) as refused:
            self._claim()
        self.assertIn("already applied", str(refused.exception))

    def test_the_gate_fold_refuses_rather_than_skipping_a_torn_approval_record(self) -> None:
        """A torn line in the gate log must not be able to hide the ``applied`` marker.

        The reason ``GateStore``'s enforcement read is strict: a tolerant reader that skipped a
        malformed line would, on a log whose last write was torn, silently fold back to the
        ``approved`` snapshot and permit the replay. Refusing the whole read keeps the failure
        visible instead of turning it into an approval.

        ``ValidationError`` and not ``Exception``: the applied marker is the line this test tears,
        so the gate is not applied on disk and the already-applied ``RuntimeError`` is unreachable
        here. Only two outcomes remain -- the strict read refuses, or the fold reverts to
        ``approved`` and permits -- and naming the exception is what tells them apart. Asserting
        merely "something raised" would have been satisfied by that ``RuntimeError`` and would
        have passed with the tear removed entirely.
        """
        _seed_approved_gate(self.coordination_root)
        self._claim()

        self._tear_the_applied_record()

        with self.assertRaises(ValidationError):
            self._claim()

        # Control arm: the tolerant fold over this SAME log -- the policy the enforcement read
        # deliberately does not use -- skips the torn marker and reports the gate as still
        # approved. That is not a degraded reading of the log, it is a second spend of a human
        # approval, and it is what the refusal above is buying.
        self.assertEqual(
            self.store.projected_current(LIFECYCLE, now=None)[GATE_ID].state,
            "approved",
            "a tolerant fold must be shown to hand back an approved gate here; if it does not, "
            "this test no longer demonstrates what the strict read is preventing",
        )


class AppliedMarkerRetentionTests(unittest.TestCase):
    """R1: the reclaim pass must not be able to delete the record that closes the window.

    ``PRUNE_IMMEDIATE_GATE_STATES`` used to contain ``applied``, so ``_keep_gate`` dropped a
    consumed approval at ANY AGE -- and the shared decision owner runs
    ``controlplane/gate_decisions.py::_reclaim_gate_log`` after EVERY gate decision. The attack
    needs no race and no privilege: decide any second gate on the same lifecycle and the marker
    is gone.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.coordination_root = Path(tmp.name)
        self.store = GateStore(observer_logs_root(self.coordination_root))
        self.contract = SimpleNamespace(
            lifecycle_id=LIFECYCLE, coordination_root=self.coordination_root
        )

    def test_an_unrelated_gate_decision_cannot_reclaim_the_consumed_approval(self) -> None:
        """The reproduction, on the production reclaim path.

        An ``agent-question`` gate is the mildest possible second gate -- a question the operator
        answered -- and answering it calls the same ``_reclaim_gate_log`` every decision calls. If
        the marker can be reclaimed, this is enough to hand the next closeout the human's approval.
        """
        _seed_approved_gate(self.coordination_root)
        closeout_mod._claim_closeout_gate(self.contract, WorktreeArgs())
        self.store.append(
            create_gate(
                "agent-question",
                gate_id="01HQUESTION",
                now=datetime.now(UTC).isoformat(),
                anchor=GateAnchor(lifecycle_id=LIFECYCLE),
            )
        )

        _reclaim_gate_log(self.store, LIFECYCLE, now=datetime.now(UTC))

        self.assertIn(
            GATE_ID,
            self.store.current(LIFECYCLE),
            "the reclaim pass deleted the consumed-approval marker; the next closeout now folds "
            "to permitted-gateless and spends the same human approval a second time",
        )
        with self.assertRaises(RuntimeError) as refused:
            closeout_mod._claim_closeout_gate(self.contract, WorktreeArgs())
        self.assertIn("already applied", str(refused.exception))

    def test_the_marker_outlives_the_ordinary_interaction_ttl(self) -> None:
        """No TTL either, not just no immediate prune.

        A retention window would be a guess about how long a human might take between a failed
        closeout and a retry; overnight beats a 24h window, and the two guesses this code has
        already made (30 seconds, then zero) were both wrong. So the assertion is about a clock
        far past any window: the marker is kept because of what it is, not how new it is.
        """
        applied = GateRecord(
            id=GATE_ID,
            ts=datetime.now(UTC).isoformat(),
            kind="closeout-approval",
            state="applied",
            lifecycleId=LIFECYCLE,
        )
        much_later = datetime.now(UTC) + timedelta(seconds=INTERACTION_RECORD_TTL_SECONDS * 365)

        self.assertEqual(gate_keep_ids([applied], now=much_later), {GATE_ID})

    def test_withdrawn_and_superseded_gates_are_still_reclaimed_immediately(self) -> None:
        """The asymmetry is the decision, so it is asserted rather than left implied.

        Dropping a gate's last snapshot returns the fold to permitted-gateless. For ``cancelled``
        (withdrawn) and ``expired`` (superseded by a newer gate on the same lifecycle) that is the
        intended meaning -- neither ever carried an approval. For ``applied`` it is the replay.
        Keeping all three would grow every gate log forever for no safety; keeping none is the
        defect. If this test starts failing because someone widened the retained set, the growth
        argument in ``interaction_retention`` has to be re-made.
        """
        stamp = datetime.now(UTC).isoformat()
        records = [
            GateRecord(
                id=f"G-{state}",
                ts=stamp,
                kind="closeout-approval",
                state=state,
                lifecycleId=LIFECYCLE,
            )
            for state in ("cancelled", "expired")
        ]

        self.assertEqual(gate_keep_ids(records, now=datetime.now(UTC)), set())

    def test_a_non_authority_kinds_applied_snapshot_is_still_reclaimable(self) -> None:
        """``agent-question`` is applied on every answered vendor interaction and decides nothing.

        Retaining those would make the retained set unbounded in a long adapter session, which is
        the growth the reclaim pass exists to bound. What makes it safe to drop is not the volume
        but the fact that no enforcement path ever asks ``evaluate_gate`` about this kind, so the
        record refuses nothing.
        """
        answered = GateRecord(
            id="Q1",
            ts=datetime.now(UTC).isoformat(),
            kind="agent-question",
            state="applied",
            lifecycleId=LIFECYCLE,
        )

        self.assertEqual(gate_keep_ids([answered], now=datetime.now(UTC)), set())

    def test_every_kind_an_enforcement_path_evaluates_is_retained_when_applied(self) -> None:
        """The kind list cannot silently fall behind the enforcement rungs.

        ``CONSUMED_APPROVAL_GATE_KINDS`` is an enumeration, and an enumeration guarding an
        invariant drifts. The two kinds any code path currently folds before a mutation are
        module constants, so this reads them from the modules that enforce them rather than
        restating them: add a third enforcement rung on a kind nobody retained, and this fails.
        """
        for kind in (CLOSEOUT_GATE_KIND, HANDOVER_GATE_KIND):
            with self.subTest(kind=kind):
                self.assertIn(
                    kind,
                    CONSUMED_APPROVAL_GATE_KINDS,
                    f"{kind} is folded before a server-side mutation, so its applied snapshot is "
                    "an authority record and reclaiming it re-opens the replay window",
                )


class ApprovalClaimAtomicityTests(unittest.TestCase):
    """R2: the check and the consume must be one step, not two with a closeout in between."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.coordination_root = Path(tmp.name)
        self.store = GateStore(observer_logs_root(self.coordination_root))

    @pytest.mark.evidence_integration
    def test_two_real_closeouts_racing_the_claim_spend_the_approval_once(self) -> None:
        """Two processes, both told the gate is approved, both trying to consume it.

        Real processes rather than threads because the two writers this log actually has are
        processes, and an in-process mutex would make a thread-level version pass while the
        cross-process case still lost. At the pre-fix shape both were permitted and two ``applied``
        snapshots reached disk; the assertion is on both facts, because either alone can be
        satisfied by a fix that only half works -- one ``applied`` line with two permits means two
        closeouts ran their mutations.
        """
        _seed_approved_gate(self.coordination_root)
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Array("i", [0, 0])
        root = str(self.coordination_root)
        procs = [
            context.Process(target=_racing_closeout_main, args=(root, start, results, index))
            for index in range(2)
        ]
        for proc in procs:
            proc.start()
        start.set()
        for proc in procs:
            proc.join(30.0)
            self.assertFalse(proc.is_alive(), "racing closeout failed to terminate")

        self.assertEqual(
            sorted(results[index] for index in range(2)),
            [REFUSED, PERMITTED],
            "exactly one of two racing closeouts may be permitted and the other must be REFUSED "
            "rather than crashed; two permits means one human approval authorised two sets of "
            "commits",
        )
        applied = [
            line
            for line in self.store.log_path(LIFECYCLE).read_text(encoding="utf-8").splitlines()
            if APPLIED_MARKER in line
        ]
        self.assertEqual(len(applied), 1, "the approval was marked consumed more than once")

    def test_the_claim_writes_nothing_when_it_refuses(self) -> None:
        """A refusal must not consume, or a rejected gate would block its own re-decision."""
        now = datetime.now(UTC).isoformat()
        store = GateStore(observer_logs_root(self.coordination_root))
        store.append(
            create_gate(
                "closeout-approval",
                gate_id=GATE_ID,
                now=now,
                anchor=GateAnchor(lifecycle_id=LIFECYCLE),
            )
        )
        before = store.log_path(LIFECYCLE).read_bytes()

        guard = store.claim_approval(LIFECYCLE, kind=CLOSEOUT_GATE_KIND, now=now)

        self.assertFalse(guard.permitted)
        self.assertEqual(store.log_path(LIFECYCLE).read_bytes(), before)

    def test_a_gateless_lifecycle_claims_nothing_and_stays_permitted(self) -> None:
        """The additive fallback survives the change: no gate of this kind, no consume, no write.

        A lifecycle that never opened a closeout gate is governed by the chat commit approval, and
        the claim must leave that path exactly as it was -- in particular it must not invent an
        ``applied`` record for a gate that does not exist.
        """
        store = GateStore(observer_logs_root(self.coordination_root))

        guard = store.claim_approval(
            LIFECYCLE, kind=CLOSEOUT_GATE_KIND, now=datetime.now(UTC).isoformat()
        )

        self.assertTrue(guard.permitted)
        self.assertIsNone(guard.gate_id)
        self.assertFalse(store.log_path(LIFECYCLE).exists())


def _approved_closeout_gate_for(contract) -> str:
    """Seed a developer-approved closeout gate on a real worktree contract's lifecycle."""
    now = datetime.now(UTC)
    store = GateStore(observer_logs_root(contract.coordination_root))
    gate = create_gate(
        "closeout-approval",
        gate_id=GATE_ID,
        now=(now - timedelta(seconds=5)).isoformat(),
        anchor=GateAnchor(lifecycle_id=contract.lifecycle_id),
    )
    store.append(gate)
    store.append(
        decide_gate(
            gate,
            GateVerdict(decision="approve", by="developer", via="dashboard", note=None),
            now=now.isoformat(),
        )
    )
    return gate.id


class ClaimPrecedesTheIrreversibleWorkTests(unittest.TestCase):
    """R3: the approval must be spent BEFORE the first thing that cannot be taken back.

    These exercise the real publication, quality-preflight and approval components with
    explicit fixture inputs. They do not establish public closeout or gate acceptance.
    """

    def _lifecycle_contract(self, root: Path):
        """The fixture contract, re-written with a lifecycle id so a gate can govern it."""
        contract = dataclass_replace(
            dirty_open_external_contract_fixture(root), lifecycle_id=LIFECYCLE
        )
        write_contract(contract.contract_path, contract)
        write_passing_route_review(contract)
        return contract

    def _closeout(self, contract) -> int:
        with redirect_stdout(io.StringIO()):
            return run_authorized_closeout_mechanics(
                closeout_args(contract),
            )

    def test_the_approval_is_already_consumed_when_the_first_commit_runs(self) -> None:
        """The ordering property, observed from inside the first irreversible act.

        The accepted code commit is the first thing a closeout does that a person would have to
        undo.
        Reading the gate's state at the moment it is entered is the strongest available statement
        of R3's fix: whatever happens after this instant -- a crash, a failed memory quality gate,
        an ENOSPC on the marker's own append -- the approval is already spent, so it cannot be
        found live sitting on top of finished work.

        Asserting the gate state INSIDE the call rather than after the closeout matters: after a
        successful run the gate is applied either way, so an end-state assertion would pass
        unchanged against the defect this test exists to catch.
        """
        with tempfile.TemporaryDirectory() as tmp:
            contract = self._lifecycle_contract(Path(tmp))
            gate_id = _approved_closeout_gate_for(contract)
            store = GateStore(observer_logs_root(contract.coordination_root))
            seen: list[str] = []
            real_commit = closeout_recovery.commit_verified_staged

            def spy(repo: Path, message: str) -> str:
                seen.append(store.current(LIFECYCLE)[gate_id].state)
                return real_commit(repo, message)

            with mock.patch.object(closeout_recovery, "commit_verified_staged", side_effect=spy):
                self.assertEqual(self._closeout(contract), 0)

            self.assertTrue(seen, "closeout committed nothing; the ordering was never exercised")
            self.assertEqual(
                seen[0],
                "applied",
                "the approval was still spendable when the first commit ran -- a closeout that "
                "dies from here on leaves a live approval on top of completed, irreversible work",
            )

    def test_a_refusal_before_any_commit_leaves_the_approval_unspent(self) -> None:
        """The other half of the trade, and the reason the claim is not simply at the top.

        Everything upstream of the claim only reads or restages the task's own disposable
        worktree, so a refusal there has changed nothing a person can see -- and must therefore
        cost nobody their approval. The strict code-quality gate refusing is the common case of
        this, not an exotic one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            contract = self._lifecycle_contract(Path(tmp))
            gate_id = _approved_closeout_gate_for(contract)
            store = GateStore(observer_logs_root(contract.coordination_root))
            assert contract.memory_worktree is not None
            code_head = git(contract.code_worktree, "rev-parse", "HEAD")
            memory_head = git(contract.memory_worktree, "rev-parse", "HEAD")

            with (
                mock.patch.object(closeout_mod, "requires_strict_code_quality", return_value=True),
                mock.patch.object(
                    closeout_staged_quality,
                    "run_strict_code_quality_gate",
                    side_effect=RuntimeError("strict code-quality gate failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "strict code-quality gate failed"),
            ):
                closeout_mod._closeout_quality_preflight(
                    contract,
                    WorktreeArgs.from_namespace(closeout_args(contract)),
                    code_would_commit=True,
                )

            self.assertEqual(
                store.current(LIFECYCLE)[gate_id].state,
                "approved",
                "a refusal that committed nothing must not spend the developer's approval",
            )

            self.assertEqual(git(contract.code_worktree, "rev-parse", "HEAD"), code_head)
            self.assertEqual(git(contract.memory_worktree, "rev-parse", "HEAD"), memory_head)

    def test_an_unapproved_gate_refuses_before_the_worktree_is_staged(self) -> None:
        """The early read still earns its place: it denies without doing any of the work.

        It decides nothing -- the claim does -- but removing it would make an open gate refuse
        only after a full strict code-quality run over a freshly staged worktree.
        """
        with tempfile.TemporaryDirectory() as tmp:
            contract = self._lifecycle_contract(Path(tmp))
            store = GateStore(observer_logs_root(contract.coordination_root))
            store.append(
                create_gate(
                    "closeout-approval",
                    gate_id=GATE_ID,
                    now=datetime.now(UTC).isoformat(),
                    anchor=GateAnchor(lifecycle_id=LIFECYCLE),
                )
            )

            index_before = git(contract.code_worktree, "write-tree")
            status_before = git(contract.code_worktree, "status", "--porcelain=v1")
            with (
                mock.patch.object(closeout_mod, "_gate_staged_code") as staged,
                self.assertRaisesRegex(RuntimeError, "is open; await a policy-valid decision"),
            ):
                closeout_mod._refuse_unsatisfied_closeout_gate(
                    contract, WorktreeArgs.from_namespace(closeout_args(contract))
                )

            staged.assert_not_called()
            self.assertEqual(git(contract.code_worktree, "write-tree"), index_before)
            self.assertEqual(git(contract.code_worktree, "status", "--porcelain=v1"), status_before)
