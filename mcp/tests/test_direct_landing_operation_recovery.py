"""Production-shaped crash and drift cuts for durable direct landing."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any, cast
from unittest import mock

from agents_remember.application.lifecycle.direct_landing import (
    direct_landing_tool as _production_direct_landing_tool,
)
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.worktree_tools import (
    OperationControlRequest,
    worktree_operation_control_tool,
    worktree_status_tool,
)
from agents_remember.kernel.memory_ledger import (
    load_ledger,
    prepend_mapping,
    write_ledger,
)
from agents_remember.mcp.tools.direct_landing import direct_landing_payload
from agents_remember.models.direct_landing import DirectLandingResponse
from agents_remember.models.lifecycles.direct_landing import (
    DirectLandingLedgerIntent,
    DirectLandingOperationInput,
)
from agents_remember.models.lifecycles.mutation_evidence import CloseoutMutationLeg
from agents_remember.models.lifecycles.operation import require_lifecycle_operation_dependencies
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.direct_landing import (
    DirectLandingError,
    DirectLandingRequest,
)
from agents_remember.worktrees.direct_landing import (
    direct_landing as _production_direct_landing,
)
from agents_remember.worktrees.integration.direct_landing import (
    direct_landing_execution as execution,
)
from agents_remember.worktrees.integration.direct_landing.direct_landing_operation import (
    DirectLandingRuntime,
    direct_landing_record,
    direct_landing_store,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_generation_resume import (
    requeued_same_generation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_candidate import (
    LifecycleOperationCandidate,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_projection import (
    legal_operation_controls,
)
from agents_remember.worktrees.modules.git import head_commit, require_git
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.worktree_contract import load_contract
from test_direct_landing import _series_fixture, _without_projection_effects
from test_worktree_support import git

_RETRY_LEGS: tuple[CloseoutMutationLeg, CloseoutMutationLeg] = ("memory", "ledger")


def direct_landing(*args, **kwargs):
    """Exercise recovery below the independently covered scheduling fence."""

    with mock.patch("agents_remember.worktrees.direct_landing.require_first_ready_generation"):
        return _production_direct_landing(*args, **kwargs)


def direct_landing_tool(*args, **kwargs):
    """Exercise the public recovery surface below the same scheduling fence."""

    with mock.patch("agents_remember.worktrees.direct_landing.require_first_ready_generation"):
        return _production_direct_landing_tool(*args, **kwargs)


def _byte_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class DirectLandingOperationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _fixture(self) -> dict[str, Any]:
        fixture = _series_fixture(Path(self.temp.name) / "fx")
        memory = fixture["memory"]
        (memory / "onboarding").mkdir()
        (memory / "onboarding" / "feature.py.md").write_text("# feature\n", encoding="utf-8")
        return fixture

    @staticmethod
    def _request(fixture: dict[str, Any]) -> DirectLandingRequest:
        candidate_tree = require_git(
            fixture["code"],
            ["rev-parse", f"{fixture['code_head']}^{{tree}}"],
        )
        return DirectLandingRequest(
            contract_path=fixture["contract"].contract_path.as_posix(),
            code_commit=fixture["code_head"],
            memory_commit_message="direct memory",
            ledger_commit_message="direct ledger",
            candidate_tree=candidate_tree,
            intent_note="approved exact direct generation",
        )

    @staticmethod
    def _recover(fixture: dict[str, Any]) -> dict[str, Any]:
        record = direct_landing_store(fixture["contract"]).read()
        assert record is not None
        current = load_contract(fixture["contract"].contract_path)
        controls = legal_operation_controls(current, record)
        recover = next((row for row in controls if row["action"] == "recover"), None)
        classification = execution.classify_direct_landing_recovery(current, record)
        assert recover is not None, (
            record.status,
            record.phase,
            {leg: evidence.state for leg, evidence in sorted(record.mutationEvidence.items())},
            classification.state,
            classification.status,
            classification.detail,
            classification.expected,
            classification.observed,
            controls,
        )
        return worktree_operation_control_tool(
            fixture["config"],
            OperationControlRequest(**cast(dict[str, Any], recover["arguments"])),
        )

    @staticmethod
    def _public_control(fixture: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
        assert control["tool"] == "worktree_operation_control"
        return worktree_operation_control_tool(
            fixture["config"],
            OperationControlRequest(**cast(dict[str, Any], control["arguments"])),
        )

    def _admit_without_execution(self, fixture: dict[str, Any]) -> list[dict[str, Any]]:
        request = self._request(fixture)
        with mock.patch(
            "agents_remember.worktrees.direct_landing.execute_or_require_direct_landing_recovery",
            return_value={"ok": True, "state": "admitted"},
        ):
            admitted = direct_landing_tool(fixture["config"], request)
        self.assertTrue(admitted["ok"])
        return cast(list[dict[str, Any]], self._status(fixture)["legalControls"])

    @staticmethod
    def _status(fixture: dict[str, Any]) -> dict[str, Any]:
        contract = fixture["contract"]
        with mock.patch(
            "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
            return_value=WorktreeCommandResult(
                0,
                {
                    "contract_path": contract.contract_path.as_posix(),
                    "task_name": contract.task_name,
                },
            ),
        ):
            status = worktree_status_tool(
                fixture["config"],
                TaskRef(
                    repo_id=contract.repo_name,
                    contract_path=contract.contract_path.as_posix(),
                ),
            )
        return next(row for row in status["lifecycleOperations"] if row["kind"] == "direct-landing")

    def test_public_preoutput_recover_and_cancel_controls_execute(self) -> None:
        recover_fixture = self._fixture()
        recover_controls = self._admit_without_execution(recover_fixture)
        recover = next(row for row in recover_controls if row["action"] == "recover")
        recovered = self._public_control(recover_fixture, recover)
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["lifecycleOperation"]["status"], "completed")

        with tempfile.TemporaryDirectory() as path:
            cancel_fixture = _series_fixture(Path(path) / "cancel")
            cancel_controls = self._admit_without_execution(cancel_fixture)
            cancel = next(row for row in cancel_controls if row["action"] == "cancel")
            cancelled = self._public_control(cancel_fixture, cancel)
            self.assertTrue(cancelled["ok"])
            self.assertEqual(cancelled["lifecycleOperation"]["status"], "cancelled")
            refused = self._public_control(cancel_fixture, cancel)
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["status"], "lifecycle-control-not-legal")
            self.assertEqual(refused["nextTool"], "direct_landing")
            arguments = refused["nextArgs"]
            advanced = direct_landing_tool(
                cancel_fixture["config"], DirectLandingRequest(**arguments)
            )
            successor_record = direct_landing_store(cancel_fixture["contract"]).read()
            assert successor_record is not None
            self.assertTrue(advanced["ok"])
            self.assertEqual(
                (successor_record.generation, successor_record.status),
                (2, "completed"),
            )

    def test_exact_retry_resumes_the_existing_running_landing(self) -> None:
        fixture = self._fixture()
        self._admit_without_execution(fixture)
        record = direct_landing_store(fixture["contract"]).read()
        assert (
            record is not None
            and record.doorPublication is not None
            and isinstance(record.input, DirectLandingOperationInput)
            and isinstance(record.taskIntent, TaskIntentIdentity)
        )
        rebuilt = direct_landing_record(
            fixture["contract"],
            record.input,
            LifecycleOperationCandidate(
                state=record.candidateState,
                tree=record.candidateTree,
                fingerprint=record.fingerprint,
                task_intent=record.taskIntent,
            ),
            record.doorPublication,
        )
        require_lifecycle_operation_dependencies(rebuilt)

        observed = direct_landing_payload(fixture["config"], self._request(fixture))

        self.assertTrue(observed["ok"])
        self.assertEqual(observed["state"], "landed")
        self.assertEqual(observed["lifecycleOperation"]["status"], "completed")
        self.assertEqual(observed["lifecycleOperation"]["generation"], record.generation)
        validated = DirectLandingResponse.model_validate(observed)
        self.assertEqual(validated.state, "landed")
        assert validated.lifecycleOperation is not None
        self.assertEqual(validated.lifecycleOperation.status, "completed")

    def test_direct_retry_reset_preserves_memory_and_ledger_admission_identity(self) -> None:
        fixture = self._fixture()
        self._admit_without_execution(fixture)
        store = direct_landing_store(fixture["contract"])
        admitted = store.read()
        assert admitted is not None
        accepted = {leg: admitted.mutationEvidence[leg].acceptedBefore for leg in _RETRY_LEGS}
        self.assertTrue(all(item is not None for item in accepted.values()))

        def mutation_intents(record):
            mutations = dict(record.mutationEvidence)
            for leg in _RETRY_LEGS:
                snapshot = accepted[leg]
                assert snapshot is not None
                mutations[leg] = mutations[leg].model_copy(
                    update={
                        "state": "mutation-intent",
                        "before": snapshot,
                        "expectedOutputTree": snapshot.candidateTree,
                    }
                )
            return record.model_copy(update={"mutationEvidence": mutations})

        store.update(mutation_intents)

        def reconciled_attempts(record):
            mutations = dict(record.mutationEvidence)
            for leg in _RETRY_LEGS:
                snapshot = accepted[leg]
                assert snapshot is not None
                mutations[leg] = mutations[leg].model_copy(
                    update={"state": "reconciled-unchanged", "observed": snapshot}
                )
            return record.model_copy(update={"mutationEvidence": mutations})

        reconciled = store.update(reconciled_attempts)
        for leg in _RETRY_LEGS:
            with self.subTest(leg=leg):
                snapshot = accepted[leg]
                assert snapshot is not None

                def tamper(
                    record,
                    leg: CloseoutMutationLeg = leg,
                    snapshot=snapshot,
                ):
                    reset = requeued_same_generation(record)
                    mutations = dict(reset.mutationEvidence)
                    mutations[leg] = mutations[leg].model_copy(
                        update={"acceptedBefore": snapshot.model_copy(update={"head": "9" * 40})}
                    )
                    return reset.model_copy(update={"mutationEvidence": mutations})

                with self.assertRaisesRegex(RuntimeError, "accepted Git prestate is immutable"):
                    store.resume_generation(tamper, expected_generation=reconciled.generation)
        resumed, changed = store.resume_generation(
            requeued_same_generation,
            expected_generation=reconciled.generation,
        )
        self.assertTrue(changed)
        for leg in _RETRY_LEGS:
            reset = resumed.mutationEvidence[leg]
            self.assertEqual(reset.acceptedBefore, accepted[leg])
            self.assertIsNone(reset.before)
            self.assertIsNone(reset.observed)
            self.assertIsNone(reset.expectedOutputTree)
            self.assertEqual(
                resumed.mutationHistory[leg],
                [reconciled.mutationEvidence[leg]],
            )

    def test_direct_resume_cannot_clear_published_ledger_intent(self) -> None:
        fixture = self._fixture()
        self._admit_without_execution(fixture)
        store = direct_landing_store(fixture["contract"])
        record = store.read()
        assert record is not None
        before_text = "accepted ledger bytes\n"
        intended_text = "intended ledger bytes\n"
        intent = DirectLandingLedgerIntent(
            codeCommit=fixture["code_head"],
            memoryCommit="a" * 40,
            beforeText=before_text,
            beforeSha256=hashlib.sha256(before_text.encode("utf-8")).hexdigest(),
            intendedText=intended_text,
            intendedSha256=hashlib.sha256(intended_text.encode("utf-8")).hexdigest(),
        )
        runtime = DirectLandingRuntime(fixture["contract"], record)
        runtime.publish_ledger_intent(intent)
        current = store.read()
        assert current is not None and current.directLandingLedgerIntent == intent

        with self.assertRaisesRegex(RuntimeError, "ledger intent is immutable"):
            store.resume_generation(
                lambda retained: requeued_same_generation(retained).model_copy(
                    update={"directLandingLedgerIntent": None}
                ),
                expected_generation=current.generation,
            )
        self.assertEqual(store.read(), current)

    def test_post_admission_unreadable_ledger_persists_and_recovers_publicly(self) -> None:
        fixture = self._fixture()
        with mock.patch.object(
            execution,
            "_direct_ledger_commit",
            side_effect=RuntimeError("cut after proven memory commit"),
        ):
            interrupted = direct_landing_tool(fixture["config"], self._request(fixture))
        self.assertFalse(interrupted["ok"])
        self.assertEqual(interrupted["nextTool"], "worktree_operation_control")
        record = direct_landing_store(fixture["contract"]).read()
        assert record is not None and record.recoveryCommits is not None
        self.assertTrue(record.recoveryCommits.memoryContentCommit)
        fixture["contract"].ledger_path.unlink()
        record_before = direct_landing_store(fixture["contract"]).path.read_bytes()

        unreadable = worktree_operation_control_tool(
            fixture["config"],
            OperationControlRequest(**cast(dict[str, Any], interrupted["nextArgs"])),
        )
        self.assertFalse(unreadable["ok"])
        self.assertEqual(unreadable["status"], "direct-landing-evidence-unreadable")
        self.assertEqual(unreadable["nextAction"], "developer-decision")
        blocked = direct_landing_store(fixture["contract"]).read()
        assert blocked is not None
        self.assertEqual(blocked.status, "input-required")
        self.assertEqual(direct_landing_store(fixture["contract"]).path.read_bytes(), record_before)
        fixture["contract"].ledger_path.write_text(
            execution.direct_landing_input(blocked).ledgerBeforeText,
            encoding="utf-8",
        )
        observed = cast(
            dict[str, Any],
            direct_landing_tool(fixture["config"], self._request(fixture)),
        )
        recover = observed["lifecycleOperation"]["legalControls"]
        self.assertEqual([row["action"] for row in recover], ["recover"])
        completed = self._public_control(fixture, recover[0])
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["lifecycleOperation"]["status"], "completed")

    def test_identity_failure_is_typed_and_public_recover_retries_setup(self) -> None:
        fixture = self._fixture()
        with mock.patch.object(
            execution,
            "ensure_git_identity",
            side_effect=RuntimeError("identity configuration interrupted"),
        ):
            interrupted = direct_landing_tool(
                fixture["config"],
                self._request(fixture),
            )
        self.assertFalse(interrupted["ok"])
        self.assertEqual(interrupted["status"], "direct-landing-recovery-required")
        self.assertEqual(interrupted["nextTool"], "worktree_operation_control")
        with mock.patch.object(execution, "ensure_git_identity") as identity:
            completed = worktree_operation_control_tool(
                fixture["config"],
                OperationControlRequest(**cast(dict[str, Any], interrupted["nextArgs"])),
            )
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["lifecycleOperation"]["status"], "completed")
        identity.assert_called_once_with(fixture["memory"])

    def test_admission_to_memory_drift_refuses_same_generation(self) -> None:
        fixture = self._fixture()
        controls = self._admit_without_execution(fixture)
        stale_recover = next(row for row in controls if row["action"] == "recover")
        git(fixture["memory"], "add", "onboarding/feature.py.md")
        status = self._status(fixture)
        self.assertEqual(status["legalControls"], [])
        self.assertEqual(
            status["result"]["state"],
            "direct-landing-memory-evidence-conflict",
        )
        refused = self._public_control(fixture, stale_recover)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["status"], "direct-landing-memory-evidence-conflict")
        self.assertEqual(refused["nextAction"], "developer-decision")
        record = direct_landing_store(fixture["contract"]).read()
        assert record is not None
        self.assertEqual(record.generation, 1)
        self.assertEqual(record.mutationEvidence["memory"].state, "pre-mutation")

    def test_memory_intent_and_post_commit_cuts_recover_once(self) -> None:
        for cut in ("after-intent", "after-commit"):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as path:
                fixture = _series_fixture(Path(path) / "fx")
                memory = fixture["memory"]
                (memory / "onboarding").mkdir()
                (memory / "onboarding" / "feature.py.md").write_text(
                    "# feature\n", encoding="utf-8"
                )
                request = self._request(fixture)
                target = "commit_if_dirty" if cut == "after-intent" else "prove_git_commit"
                with (
                    mock.patch(
                        f"agents_remember.worktrees.integration.direct_landing.direct_landing_execution.{target}",
                        side_effect=RuntimeError(cut),
                    ),
                    self.assertRaises(DirectLandingError),
                ):
                    direct_landing(fixture["config"], request, fixture["contract"])
                before_recovery = head_commit(memory)
                result = self._recover(fixture)
                record = direct_landing_store(fixture["contract"]).read()
                assert record is not None and record.recoveryCommits is not None
                self.assertTrue(result["ok"])
                self.assertEqual(result["lifecycleOperation"]["generation"], 1)
                self.assertEqual(record.status, "completed")
                self.assertEqual(record.attempt, 2)
                if cut == "after-commit":
                    assert record.recoveryCommits is not None
                    self.assertEqual(
                        record.recoveryCommits.memoryContentCommit,
                        before_recovery,
                    )

    def test_inter_leg_drift_is_not_adopted_by_ledger_commit(self) -> None:
        fixture = self._fixture()
        original = execution._direct_ledger_commit

        def drift(runtime, args, facts):
            (fixture["memory"] / "unrelated.txt").write_text("drift\n", encoding="utf-8")
            return original(runtime, args, facts)

        with (
            mock.patch.object(execution, "_direct_ledger_commit", side_effect=drift),
            self.assertRaisesRegex(
                DirectLandingError,
                "direct-landing-ledger-evidence-conflict",
            ),
        ):
            direct_landing(fixture["config"], self._request(fixture), fixture["contract"])
        self.assertNotIn("unrelated.txt", git(fixture["memory"], "show", "HEAD^{tree}"))

    def test_ledger_prewrite_write_stage_and_commit_cuts_recover(self) -> None:
        cuts = (
            "after-intent",
            "before-write",
            "after-write",
            "after-stage",
            "before-commit",
            "after-commit",
        )
        for cut in cuts:
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as path:
                fixture = _series_fixture(Path(path) / "fx")
                memory = fixture["memory"]
                (memory / "onboarding").mkdir()
                (memory / "onboarding" / "feature.py.md").write_text(
                    "# feature\n", encoding="utf-8"
                )
                request = self._request(fixture)
                self._run_ledger_cut(fixture, request, cut)
                commit_after_cut = head_commit(memory)
                result = self._recover(fixture)
                record = direct_landing_store(fixture["contract"]).read()
                assert record is not None and record.recoveryCommits is not None
                self.assertTrue(result["ok"])
                self.assertEqual(
                    (result["lifecycleOperation"]["generation"], record.status),
                    (1, "completed"),
                )
                self.assertEqual(record.recoveryCommits.ledgerCommit, head_commit(memory))
                if cut == "after-commit":
                    self.assertEqual(commit_after_cut, head_commit(memory))

    def test_immutable_ledger_bytes_block_fresh_and_stale_public_recover(self) -> None:
        fixture = self._fixture()
        controls = self._admit_without_execution(fixture)
        stale_recover = next(row for row in controls if row["action"] == "recover")
        store = direct_landing_store(fixture["contract"])
        accepted = store.read()
        assert accepted is not None
        ledger_path = fixture["contract"].ledger_path
        assert ledger_path is not None
        ledger_path.write_text(
            execution.direct_landing_input(accepted).ledgerBeforeText + "# unrelated drift\n",
            encoding="utf-8",
        )
        record_before = store.path.read_bytes()
        head_before = head_commit(fixture["memory"])
        index_before = git(fixture["memory"], "write-tree")
        bytes_before = ledger_path.read_bytes()

        status = self._status(fixture)
        self.assertEqual(status["legalControls"], [])
        decision = status["result"]
        self.assertEqual(decision["nextAction"], "developer-decision")
        self.assertTrue(decision["developerDecisionRequired"])
        refused = self._public_control(fixture, stale_recover)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["nextAction"], "developer-decision")
        self.assertEqual(refused["expected"], decision["expected"])
        self.assertEqual(refused["observed"], decision["observed"])
        self.assertEqual(store.path.read_bytes(), record_before)
        self.assertEqual(head_commit(fixture["memory"]), head_before)
        self.assertEqual(git(fixture["memory"], "write-tree"), index_before)
        self.assertEqual(ledger_path.read_bytes(), bytes_before)

        ledger_path.write_text(
            execution.direct_landing_input(accepted).ledgerBeforeText,
            encoding="utf-8",
        )
        restored = self._status(fixture)
        recover = next(row for row in restored["legalControls"] if row["action"] == "recover")
        completed = self._public_control(fixture, recover)
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["lifecycleOperation"]["status"], "completed")

    def test_unexpected_memory_ref_blocks_fresh_and_stale_public_recover(self) -> None:
        fixture = self._fixture()
        controls = self._admit_without_execution(fixture)
        stale_recover = next(row for row in controls if row["action"] == "recover")
        store = direct_landing_store(fixture["contract"])
        accepted = store.read()
        assert accepted is not None
        git(fixture["memory"], "add", "-A")
        git(fixture["memory"], "commit", "-m", "unrelated external ref movement")
        record_before = store.path.read_bytes()
        head_before = head_commit(fixture["memory"])
        status = self._status(fixture)
        self.assertEqual(status["legalControls"], [])
        self.assertEqual(status["result"]["nextAction"], "developer-decision")
        refused = self._public_control(fixture, stale_recover)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["nextAction"], "developer-decision")
        self.assertEqual(refused["expected"], status["result"]["expected"])
        self.assertEqual(refused["observed"], status["result"]["observed"])
        self.assertEqual(store.path.read_bytes(), record_before)
        self.assertEqual(head_commit(fixture["memory"]), head_before)

    def test_terminal_publication_cut_recovers_without_duplicate_commit(self) -> None:
        fixture = self._fixture()
        request = self._request(fixture)
        with mock.patch.object(
            execution.DirectLandingRuntime,
            "finish",
            side_effect=RuntimeError("cut before terminal journal publication"),
        ):
            interrupted = direct_landing_tool(fixture["config"], request)
        self.assertFalse(interrupted["ok"])
        store = direct_landing_store(fixture["contract"])
        retained = store.read()
        assert retained is not None and retained.recoveryCommits is not None
        self.assertEqual(retained.mutationEvidence["memory"].state, "commit-proven")
        self.assertEqual(retained.mutationEvidence["ledger"].state, "commit-proven")
        head_before = head_commit(fixture["memory"])
        count_before = git(fixture["memory"], "rev-list", "--count", "HEAD")
        status = self._status(fixture)
        recover = next(row for row in status["legalControls"] if row["action"] == "recover")
        with (
            mock.patch.object(execution, "commit_if_dirty") as commit_again,
            mock.patch.object(execution, "write_ledger") as write_again,
            mock.patch.object(execution, "_prepare_or_resume_ledger_mutation") as prepare_again,
        ):
            completed = self._public_control(fixture, recover)
        self.assertTrue(completed["ok"])
        commit_again.assert_not_called()
        write_again.assert_not_called()
        prepare_again.assert_not_called()
        final = store.read()
        assert final is not None and final.recoveryCommits is not None
        self.assertEqual(final.status, "completed")
        self.assertEqual(final.recoveryCommits, retained.recoveryCommits)
        self.assertEqual(head_commit(fixture["memory"]), head_before)
        self.assertEqual(git(fixture["memory"], "rev-list", "--count", "HEAD"), count_before)

    def _run_ledger_cut(
        self,
        fixture: dict[str, Any],
        request: DirectLandingRequest,
        cut: str,
    ) -> None:
        patcher = self._ledger_cut_patcher(cut)
        with patcher, self.assertRaises(DirectLandingError):
            direct_landing(fixture["config"], request, fixture["contract"])

    def _ledger_cut_patcher(self, cut: str):
        factories = {
            "after-intent": self._after_ledger_intent,
            "before-write": self._ledger_write_cut,
            "after-write": self._ledger_write_cut,
            "after-stage": self._after_ledger_stage,
            "before-commit": self._before_ledger_commit,
            "after-commit": self._after_ledger_commit,
        }
        return factories[cut](cut)

    def _after_ledger_intent(self, cut: str):
        original = execution.begin_git_mutation

        def interrupted(args, **kwargs):
            if kwargs["leg"] == "ledger":
                raise RuntimeError(cut)
            return original(args, **kwargs)

        return mock.patch.object(execution, "begin_git_mutation", side_effect=interrupted)

    def _ledger_write_cut(self, cut: str):
        original = execution.write_ledger

        def interrupted(*args, **kwargs):
            if cut == "after-write":
                original(*args, **kwargs)
            raise RuntimeError(cut)

        return mock.patch.object(execution, "write_ledger", side_effect=interrupted)

    def _after_ledger_stage(self, cut: str):
        return mock.patch.object(
            execution,
            "bind_expected_output_tree",
            side_effect=RuntimeError(cut),
        )

    def _before_ledger_commit(self, cut: str):
        original = execution.commit_if_dirty

        def interrupted(repository, message):
            if message == "direct ledger":
                raise RuntimeError(cut)
            return original(repository, message)

        return mock.patch.object(execution, "commit_if_dirty", side_effect=interrupted)

    def _after_ledger_commit(self, cut: str):
        original = execution.prove_git_commit

        def interrupted(args, evidence, **kwargs):
            if evidence.leg == "ledger":
                raise RuntimeError(cut)
            return original(args, evidence, **kwargs)

        return mock.patch.object(execution, "prove_git_commit", side_effect=interrupted)

    def test_newest_committed_mapping_with_older_history_is_recovered(self) -> None:
        fixture = self._fixture()
        request = self._request(fixture)
        original_prove = execution.prove_git_commit

        def interrupt_memory_proof(args, evidence, **kwargs):
            if evidence.leg == "memory":
                raise RuntimeError("memory commit before proof")
            return original_prove(args, evidence, **kwargs)

        with (
            mock.patch.object(execution, "prove_git_commit", side_effect=interrupt_memory_proof),
            self.assertRaises(DirectLandingError),
        ):
            direct_landing(fixture["config"], request, fixture["contract"])
        memory_commit = head_commit(fixture["memory"])
        ledger = prepend_mapping(
            load_ledger(fixture["contract"].ledger_path),
            fixture["code_head"],
            "f" * 40,
        )
        write_ledger(
            fixture["contract"].ledger_path,
            prepend_mapping(
                ledger,
                fixture["code_head"],
                memory_commit,
            ),
        )
        git(fixture["memory"], "add", "memory.md")
        git(fixture["memory"], "commit", "-m", "external exact ledger output")
        existing_ledger_commit = head_commit(fixture["memory"])
        self._recover(fixture)
        record = direct_landing_store(fixture["contract"]).read()
        assert record is not None and record.recoveryCommits is not None
        self.assertEqual(record.recoveryCommits.memoryContentCommit, memory_commit)
        self.assertEqual(record.recoveryCommits.ledgerCommit, existing_ledger_commit)
        self.assertEqual(head_commit(fixture["memory"]), existing_ledger_commit)

    def test_historical_mapping_is_superseded_by_recovered_memory_only_change(self) -> None:
        fixture = _series_fixture(Path(self.temp.name) / "history")
        ledger_path = fixture["contract"].ledger_path
        historical_memory = "f" * 40
        write_ledger(
            ledger_path,
            prepend_mapping(load_ledger(ledger_path), fixture["code_head"], historical_memory),
        )
        git(fixture["memory"], "add", "memory.md")
        git(fixture["memory"], "commit", "-m", "historical ledger mapping")
        (fixture["memory"] / "memory-note.md").write_text("content\n", encoding="utf-8")
        controls = self._admit_without_execution(fixture)
        recover = next(row for row in controls if row["action"] == "recover")
        completed = self._public_control(fixture, recover)
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["lifecycleOperation"]["status"], "completed")
        record = direct_landing_store(fixture["contract"]).read()
        assert record is not None and record.recoveryCommits is not None
        memory_commit = record.recoveryCommits.memoryContentCommit
        ledger_commit = record.recoveryCommits.ledgerCommit
        self.assertNotEqual(memory_commit, historical_memory)
        ledger = load_ledger(ledger_path)
        self.assertEqual(ledger.rows[0].code_commit, fixture["code_head"])
        self.assertEqual(ledger.rows[0].memory_commit, memory_commit)
        self.assertEqual(ledger.rows[1].memory_commit, historical_memory)
        self.assertEqual(head_commit(fixture["memory"]), ledger_commit)

    def test_ledger_head_read_failure_keeps_same_generation_recoverable(self) -> None:
        fixture = self._fixture()
        private_sentinel = "PRIVATE-DIRECT-GIT-STDERR-/secret/path"
        real_run_git = execution.run_git

        def unreadable_head(repository, arguments):
            if arguments and arguments[0] == "show":
                return CompletedProcess(arguments, 2, stdout="", stderr=private_sentinel)
            return real_run_git(repository, arguments)

        with mock.patch.object(execution, "run_git", side_effect=unreadable_head):
            protected = direct_landing_tool(fixture["config"], self._request(fixture))
        record = direct_landing_store(fixture["contract"]).read()
        assert record is not None and record.recoveryCommits is not None
        self.assertFalse(protected["ok"])
        self.assertEqual(protected["status"], "direct-landing-ledger-head-invalid")
        observed = cast(dict[str, Any], protected["observed"])
        self.assertEqual(observed["errorType"], "GitCommandError")
        self.assertEqual(record.generation, 1)
        self.assertTrue(record.recoveryCommits.memoryContentCommit)
        self.assertFalse(record.recoveryCommits.ledgerCommit)
        status = self._status(fixture)
        stale = next(row for row in status["legalControls"] if row["action"] == "recover")
        with mock.patch.object(execution, "run_git", side_effect=unreadable_head):
            refused = self._public_control(fixture, stale)
        persisted = direct_landing_store(fixture["contract"]).read()
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["status"], "direct-landing-ledger-head-invalid")
        self.assertNotIn(private_sentinel, repr([protected, status, refused, persisted]))

    def test_ledger_head_parse_failure_never_exposes_invalid_bytes(self) -> None:
        fixture = self._fixture()
        private_sentinel = "PRIVATE-DIRECT-LEDGER-BYTES-/secret/path"
        real_run_git = execution.run_git

        def invalid_head(repository, arguments):
            if arguments and arguments[0] == "show":
                return CompletedProcess(
                    arguments,
                    0,
                    stdout=f"# {private_sentinel}\nnot-a-ledger-row\n",
                    stderr="",
                )
            return real_run_git(repository, arguments)

        with mock.patch.object(execution, "run_git", side_effect=invalid_head):
            protected = direct_landing_tool(fixture["config"], self._request(fixture))
        record = direct_landing_store(fixture["contract"]).read()
        assert record is not None
        self.assertFalse(protected["ok"])
        self.assertEqual(protected["status"], "direct-landing-ledger-head-invalid")
        observed = cast(dict[str, Any], protected["observed"])
        self.assertEqual(observed["errorType"], "LedgerError")
        status = self._status(fixture)
        self.assertEqual([row["action"] for row in status["legalControls"]], ["recover"])
        self.assertNotIn(private_sentinel, repr([protected, status, record]))

    def test_completed_same_request_observes_same_generation(self) -> None:
        fixture = self._fixture()
        request = self._request(fixture)
        first = direct_landing(fixture["config"], request, fixture["contract"])
        second = direct_landing(
            fixture["config"],
            request,
            load_contract(fixture["contract"].contract_path),
        )
        record = direct_landing_store(fixture["contract"]).read()
        assert record is not None
        self.assertEqual(
            _without_projection_effects(first),
            _without_projection_effects(second),
        )
        self.assertEqual((record.generation, record.attempt), (1, 1))

    def _assert_unreadable_journal(self, mode: str) -> None:
        fixture = self._fixture()
        controls = self._admit_without_execution(fixture)
        stale = next(row for row in controls if row["action"] == "recover")
        store = direct_landing_store(fixture["contract"])
        private = f"PRIVATE_DIRECT_{mode}_JOURNAL /tmp/direct-journal"
        path = store.path
        patcher = nullcontext()
        if mode == "malformed":
            path.write_text(
                f'{{"schemaVersion":"3.0","private":"{private}"',
                encoding="utf-8",
            )
        elif mode == "invalid-schema-3":
            path.write_text(
                json.dumps({"schemaVersion": "3.0", "private": private}),
                encoding="utf-8",
            )
        else:
            real_read_text = Path.read_text

            def unreadable(
                current: Path,
                encoding: str | None = None,
                errors: str | None = None,
            ) -> str:
                if current == path:
                    raise PermissionError(private)
                return real_read_text(current, encoding=encoding, errors=errors)

            patcher = mock.patch.object(
                Path,
                "read_text",
                new=unreadable,
            )
        before = _byte_tree(Path(self.temp.name))

        with patcher:
            status = self._status(fixture)
            refused = self._public_control(fixture, stale)
            repeated = direct_landing_tool(fixture["config"], self._request(fixture))

        self.assertEqual(status["legalControls"], [])
        status_result = cast(dict[str, Any], status["result"])
        self.assertEqual(status_result["nextAction"], "developer-decision")
        for response in (refused, repeated):
            self.assertFalse(response["ok"])
            self.assertEqual(
                response["status"],
                "direct-landing-lifecycle-journal-unreadable",
            )
            self.assertEqual(response["nextAction"], "developer-decision")
            self.assertEqual(response["expected"], status_result["expected"])
            self.assertEqual(response["observed"], status_result["observed"])
        self.assertNotIn(private, repr([status, refused, repeated]))
        self.assertEqual(_byte_tree(Path(self.temp.name)), before)

    def test_malformed_current_journal_has_public_totality(self) -> None:
        self._assert_unreadable_journal("malformed")

    def test_invalid_schema_three_journal_has_public_totality(self) -> None:
        self._assert_unreadable_journal("invalid-schema-3")

    def test_os_error_current_journal_has_public_totality(self) -> None:
        self._assert_unreadable_journal("os-error")


if __name__ == "__main__":
    unittest.main()
