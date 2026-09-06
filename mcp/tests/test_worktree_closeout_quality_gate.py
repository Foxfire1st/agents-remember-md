from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from _quality_evidence_fixture import publish_passing_quality_gate
from agents_remember.application.lifecycle.lifecycle_operation_worker import (
    execute_operation,
)
from agents_remember.errors import CertificationContractError
from agents_remember.models.lifecycles.operation import LifecycleOperationRecoveryCommits
from agents_remember.models.worktree import SourceLineageProjection
from agents_remember.tasks import read_task_doc, write_task_doc
from agents_remember.worktrees.integration.closeout.certification.selection import (
    require_selected_certification,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.modules import closeout as closeout_module
from agents_remember.worktrees.modules import closeout_external
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.git import commit_if_dirty, commit_verified_staged
from agents_remember.worktrees.modules.quality import clean_executor
from agents_remember.worktrees.modules.quality import closeout_memory as closeout_memory_quality
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.modules.quality.execution import sandbox as execution_sandbox
from agents_remember.worktrees.queue import closeout_recovery, closeout_staged_quality
from agents_remember.worktrees.services import worktree_services
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    RepoBranchPlan,
    default_series_contract,
    load_contract,
)
from closeout_fixture_test_support import (
    _committed_state,
    _component_code_and_memory,
    _component_ledger,
    _PendingMemory,
    _public_apply,
    _selected_fixture,
    _start_selected,
    _with_memory_owner,
)
from closeout_input_test_support import MutationEvidenceRecorder, closeout_worktree_args
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    NODE_FIXTURE,
    install_agents_remember_profile,
    install_fixture_profile,
)
from test_closeout_certification_entrypoint import (
    _executor,
    _install_hook,
    _store,
)
from test_closeout_queue import MASTER_A
from test_worktree_support import (
    closeout_args,
    dirty_open_external_contract_fixture,
    git,
    init_repo,
    run_authorized_closeout_mechanics,
    write_passing_route_review,
)


def _checkout_with_profile(root: Path, *, repository_id: str = "agents-remember") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    repository = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if repository.returncode != 0:
        init_repo(root)
    if repository_id == "agents-remember":
        install_agents_remember_profile(root)
    else:
        install_fixture_profile(root, repository_id)
    return root


def _quality_target(
    worktree: Path,
    worktree_group: Path | None = None,
    *,
    repository_id: str = "agents-remember",
) -> code_quality_gate.QualityGateTarget:
    return code_quality_gate.QualityGateTarget(
        code_worktree=worktree,
        worktree_group=worktree_group or worktree / "enclosure",
        repository_id=repository_id,
        profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
    )


def _assert_closeout_commit_subjects(contract, commits: Mapping[str, str]) -> None:
    assert contract.memory_worktree is not None
    code = git(
        contract.code_worktree,
        "show",
        "-s",
        "--format=%s",
        commits["codeCommit"],
    )
    memory = git(
        contract.memory_worktree,
        "show",
        "-s",
        "--format=%s",
        commits["memoryContentCommit"],
    )
    ledger = git(
        contract.memory_worktree,
        "show",
        "-s",
        "--format=%s",
        commits["ledgerCommit"],
    )
    assert (code, memory, ledger) == ("Add feature", "Document feature", "Sync ledger")


class CloseoutCodeQualityGateTests(unittest.TestCase):
    def test_closeout_refuses_a_profile_bound_to_another_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _selected_fixture(Path(tmp), profile_repository_id="another-repository")
            contract = fixture.contracts[MASTER_A]
            before = _committed_state(contract)
            _install_hook(contract, "printf 'unexpected hook' > unexpected-hook.txt\n")
            with (
                mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch,
                mock.patch.object(code_quality_gate, "run_clean_quality") as execute,
            ):
                result = _public_apply(fixture)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "certification-admission-refused")
            self.assertEqual(result["gateStarts"], 0)
            self.assertIn("profile-repository-mismatch", str(result["findings"]))
            self.assertFalse((contract.code_worktree / "unexpected-hook.txt").exists())
            self.assertEqual(_committed_state(contract), before)
            self.assertIsNone(_store(contract).read())
            execute.assert_not_called()
            launch.assert_not_called()

    def test_contract_finalization_retry_reuses_exact_external_commits(self) -> None:
        """Writer-component recovery only; the fixture does not assert Gate-5 acceptance."""
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            assert contract.memory_worktree is not None and contract.ledger_path is not None
            captured: dict[str, str] = {}
            mutation_recorder = MutationEvidenceRecorder()

            def progress(phase: str, evidence: Mapping[str, object]) -> None:
                mutation_recorder(phase, evidence)
                if phase == "contract-finalization":
                    value = evidence.get("recovery_commits")
                    assert isinstance(value, dict)
                    captured.update({str(key): str(item) for key, item in value.items()})

            args = closeout_worktree_args(
                contract,
                code="Add feature",
                memory="Document feature",
                ledger="Sync ledger",
                approved=True,
                approval_note="developer approved exact closeout",
                operation_key="a" * 64,
                candidate_tree=closeout_module.code_candidate_tree(contract),
                operation_progress=progress,
            )
            code, memory = _component_code_and_memory(contract, args)
            ledger = _component_ledger(contract, args, code, memory)
            commits = LifecycleOperationRecoveryCommits(
                codeCommit=code, memoryContentCommit=memory, ledgerCommit=ledger
            )
            recovery_args = replace(args, recovery_commits=commits)
            with (
                mock.patch.object(
                    closeout_module,
                    "write_contract",
                    side_effect=RuntimeError("contract write interrupted"),
                ),
                self.assertRaisesRegex(RuntimeError, "contract write interrupted"),
            ):
                closeout_module._recover_closeout_finalization(contract, recovery_args)
            code_head = git(contract.code_worktree, "rev-parse", "HEAD")
            memory_head = git(contract.memory_worktree, "rev-parse", "HEAD")
            ledger_after_first = contract.ledger_path.read_bytes()
            self.assertEqual(captured["codeCommit"], code_head)
            self.assertEqual(captured["ledgerCommit"], memory_head)
            _assert_closeout_commit_subjects(contract, captured)
            mutation_recorder.assert_proven("code", "memory", "ledger")
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")
            recovered = closeout_module._recover_closeout_finalization(contract, recovery_args)
            assert recovered is not None
            self.assertEqual(recovered.payload["state"], "closed")
            self.assertTrue(recovered.payload["recovered"])
            pair_identity = recovered.payload["pairIdentity"]
            self.assertIsInstance(pair_identity, dict)
            assert isinstance(pair_identity, dict)
            self.assertEqual(
                pair_identity["contractPath"], contract.contract_path.resolve().as_posix()
            )
            self.assertEqual(pair_identity["codeRoot"], contract.code_worktree.resolve().as_posix())
            self.assertEqual(
                pair_identity["memoryRoot"], contract.memory_worktree.resolve().as_posix()
            )
            self.assertEqual(git(contract.code_worktree, "rev-parse", "HEAD"), code_head)
            self.assertEqual(git(contract.memory_worktree, "rev-parse", "HEAD"), memory_head)
            self.assertEqual(contract.ledger_path.read_bytes(), ledger_after_first)
            updated = load_contract(contract.contract_path)
            self.assertEqual(updated.code_commit, captured["codeCommit"])
            self.assertEqual(updated.memory_content_commit, captured["memoryContentCommit"])
            self.assertEqual(updated.ledger_commit, captured["ledgerCommit"])

    def test_memory_commit_interruption_stays_bound_to_the_published_ledger_intent(self) -> None:
        """Exercise actual memory/ledger writers below lifecycle admission, with no Gate-5 claim."""
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            assert contract.memory_worktree is not None and contract.ledger_path is not None
            previous_memory = contract.memory_content_commit
            mutation_recorder = MutationEvidenceRecorder()
            args = closeout_worktree_args(
                contract,
                code="Add feature",
                memory="Document feature",
                ledger="Sync ledger",
                approved=True,
                approval_note="developer approved exact closeout",
                operation_key="b" * 64,
                candidate_tree=closeout_module.code_candidate_tree(contract),
                operation_progress=mutation_recorder,
            )
            code_commit, memory_commit = _component_code_and_memory(contract, args)
            with (
                mock.patch.object(
                    closeout_external,
                    "write_ledger",
                    side_effect=RuntimeError("ledger write interrupted"),
                ),
                self.assertRaisesRegex(RuntimeError, "ledger write interrupted"),
            ):
                _component_ledger(contract, args, code_commit, memory_commit)
            self.assertEqual(mutation_recorder.evidence["code"].commit, code_commit)
            self.assertEqual(mutation_recorder.evidence["memory"].commit, memory_commit)
            self.assertNotEqual(memory_commit, previous_memory)
            self.assertEqual(git(contract.memory_worktree, "rev-parse", "HEAD"), memory_commit)
            self.assertEqual(mutation_recorder.evidence["code"].state, "commit-proven")
            self.assertEqual(mutation_recorder.evidence["memory"].state, "commit-proven")
            self.assertEqual(mutation_recorder.evidence["ledger"].state, "mutation-intent")
            self.assertIsNotNone(mutation_recorder.evidence["ledger"].expectedOutputTree)
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")
            self.assertIsNone(
                closeout_external.find_mapping(
                    closeout_external.load_ledger(contract.ledger_path), code_commit
                )
            )

    def test_closeout_refuses_stale_route_review_before_memory_or_code_quality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            (contract.code_worktree / "after-review.py").write_text("VALUE = 1\n", encoding="utf-8")
            with (
                mock.patch.object(closeout_module, "_memory_quality_before_refresh") as memory,
                mock.patch.object(
                    closeout_staged_quality, "run_strict_code_quality_gate"
                ) as quality,
                self.assertRaisesRegex(
                    ValueError, "candidate changed after independent route review"
                ),
            ):
                run_authorized_closeout_mechanics(closeout_args(contract, dry_run=True))

            memory.assert_not_called()
            quality.assert_not_called()

    def test_memory_quality_failure_without_a_findings_list_has_a_bounded_message(self) -> None:
        message = closeout_memory_quality._failure_message(
            {"findingCount": 1, "findings": {"unexpected": "shape"}}
        )

        self.assertIn("findingCount=1", message)
        self.assertNotIn("Findings:", message)

    def test_source_lineage_is_rechecked_after_quality_before_terminal_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _selected_fixture(Path(tmp))
            contract, store, runtime, running = _start_selected(fixture)
            before = _committed_state(contract)
            calls: list[clean_executor.CleanQualityRequest] = []
            execute = _executor(NODE_FIXTURE, calls)
            memory = _PendingMemory([])

            def source_moves(request):
                result = execute(request)
                git(fixture.code, "checkout", contract.code_source_branch)
                (fixture.code / "source-moved.txt").write_text("source moved during quality\n")
                git(fixture.code, "add", "-A")
                git(fixture.code, "commit", "-m", "Move original source after certification")
                return result

            with (
                mock.patch.object(code_quality_gate, "run_clean_quality", side_effect=source_moves),
                _with_memory_owner(memory),
                self.assertRaises(CertificationContractError) as refused,
            ):
                execute_operation(running, runtime)
            (finding,) = refused.exception.findings
            self.assertEqual(finding["code"], "candidate-source-authority-invalid")
            lineage = SourceLineageProjection.model_validate(finding["observed"])
            self.assertEqual(lineage.state, "blocked")
            self.assertEqual([edge.behind for edge in lineage.edges if edge.side == "code"], [1])
            self.assertEqual(lineage.recoveries[0].tool, "worktree_sync")
            self.assertEqual(len(calls), 1)
            self.assertEqual(memory.received, [])
            self.assertEqual(_committed_state(contract), before)
            current = store.read()
            assert current is not None and current.certification is not None
            self.assertEqual(current.certification.terminals, ())

    def test_route_review_is_rechecked_after_quality_before_terminal_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _selected_fixture(Path(tmp))
            contract, store, runtime, running = _start_selected(fixture)
            before = _committed_state(contract)
            calls: list[clean_executor.CleanQualityRequest] = []
            execute = _executor(NODE_FIXTURE, calls)
            memory = _PendingMemory([])

            def review_moves(request):
                result = execute(request)
                document = read_task_doc(fixture.tasks / fixture.leaf_refs[MASTER_A].path)
                write_task_doc(
                    contract.task_root, document.model_copy(update={"routeReview": None})
                )
                return result

            with (
                mock.patch.object(code_quality_gate, "run_clean_quality", side_effect=review_moves),
                _with_memory_owner(memory),
                self.assertRaises(CertificationContractError) as refused,
            ):
                execute_operation(running, runtime)
            self.assertIn("route-review-required", str(refused.exception.findings))
            self.assertEqual(len(calls), 1)
            self.assertEqual(memory.received, [])
            self.assertEqual(_committed_state(contract), before)
            current = store.read()
            assert current is not None and current.certification is not None
            self.assertEqual(current.certification.terminals, ())

    def test_series_candidate_is_rechecked_after_quality_before_approval_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            git(contract.code_worktree, "add", "-A")
            git(contract.code_worktree, "commit", "-m", "land leaf before master closeout")
            series = replace(contract, kind="series", leaf_id="")
            accepted_tree = closeout_module.code_candidate_tree(series)
            args = replace(
                closeout_worktree_args(
                    series,
                    code=None,
                    memory=None,
                    ledger=None,
                    approved=True,
                    approval_note="approved",
                    operation_progress=MutationEvidenceRecorder(),
                ),
                candidate_tree=accepted_tree,
            )

            def mutate_during_quality(*_args, **_kwargs):
                (series.code_worktree / "changed-during-quality.py").write_text(
                    "VALUE = 2\n", encoding="utf-8"
                )
                git(series.code_worktree, "add", "-A")
                git(series.code_worktree, "commit", "-m", "move exact atomic candidate")
                return {"required": True, "passed": True}, {}, False

            with (
                mock.patch.object(closeout_module, "load_contract", return_value=series),
                mock.patch.object(closeout_module, "require_series_contract_authority"),
                mock.patch.object(closeout_module, "refuse_series_workbench_commit"),
                mock.patch.object(closeout_module, "_validate_closeout_source_state"),
                mock.patch.object(closeout_module, "_refuse_unsatisfied_closeout_gate"),
                mock.patch.object(
                    closeout_module,
                    "_closeout_quality_preflight",
                    side_effect=mutate_during_quality,
                ),
                mock.patch.object(closeout_module, "_claim_closeout_gate") as claim,
                mock.patch.object(closeout_module, "accepted_code_commit") as commit,
                self.assertRaisesRegex(RuntimeError, "candidate changed after quality"),
            ):
                closeout_module.closeout_result(args, series)

            claim.assert_not_called()
            commit.assert_not_called()

    def test_series_workbench_is_rechecked_after_quality_before_approval_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            git(contract.code_worktree, "add", "-A")
            git(contract.code_worktree, "commit", "-m", "land leaf before master closeout")
            assert contract.memory_worktree is not None
            git(contract.memory_worktree, "add", "-A")
            git(contract.memory_worktree, "commit", "-m", "land leaf memory before master closeout")
            series = replace(contract, kind="series", leaf_id="")
            args = replace(
                closeout_worktree_args(
                    series,
                    code=None,
                    memory=None,
                    ledger=None,
                    approved=True,
                    approval_note="approved",
                    operation_progress=MutationEvidenceRecorder(),
                ),
                candidate_tree=closeout_module.code_candidate_tree(series),
            )

            def dirty_during_quality(*_args, **_kwargs):
                (series.code_worktree / "dirty-during-quality.py").write_text(
                    "VALUE = 2\n", encoding="utf-8"
                )
                return {"required": True, "passed": True}, {}, False

            with (
                mock.patch.object(closeout_module, "load_contract", return_value=series),
                mock.patch.object(closeout_module, "require_series_contract_authority"),
                mock.patch.object(closeout_module, "_validate_closeout_source_state"),
                mock.patch.object(closeout_module, "_refuse_unsatisfied_closeout_gate"),
                mock.patch.object(
                    closeout_module,
                    "_closeout_quality_preflight",
                    side_effect=dirty_during_quality,
                ),
                mock.patch.object(closeout_module, "_claim_closeout_gate") as claim,
                self.assertRaisesRegex(RuntimeError, "cannot create code, memory, or ledger"),
            ):
                closeout_module.closeout_result(args, series)

            claim.assert_not_called()

    def test_series_closeout_does_not_rerun_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = replace(
                dirty_open_external_contract_fixture(Path(tmp)), kind="series", leaf_id=""
            )
            with (
                mock.patch.object(
                    closeout_module, "_memory_quality_before_refresh", return_value={}
                ),
                mock.patch.object(closeout_module, "requires_strict_code_quality") as requires,
                mock.patch.object(closeout_module, "_gate_staged_code") as gate,
            ):
                result, memory, strict_required = closeout_module._closeout_quality_preflight(
                    contract,
                    WorktreeArgs(contract_path=contract.contract_path),
                    code_would_commit=True,
                )

            self.assertEqual(memory, {})
            self.assertFalse(strict_required)
            self.assertFalse(result["required"])
            self.assertEqual(result["status"], "not-required-master-altitude")
            requires.assert_not_called()
            gate.assert_not_called()

    def test_series_closeout_refuses_dirty_code_before_quality_or_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = replace(
                dirty_open_external_contract_fixture(Path(tmp)), kind="series", leaf_id=""
            )
            args = replace(
                closeout_worktree_args(
                    contract,
                    approved=True,
                    approval_note="approved",
                    operation_progress=MutationEvidenceRecorder(),
                ),
                candidate_tree=closeout_module.code_candidate_tree(contract),
            )

            with (
                mock.patch.object(closeout_module, "load_contract", return_value=contract),
                mock.patch.object(closeout_module, "require_series_contract_authority"),
                mock.patch.object(closeout_module, "_validate_closeout_source_state"),
                mock.patch.object(closeout_module, "_closeout_quality_preflight") as quality,
                mock.patch.object(closeout_module, "_claim_closeout_gate") as claim,
                self.assertRaisesRegex(RuntimeError, "cannot create code, memory, or ledger"),
            ):
                closeout_module.closeout_result(args, contract)

            quality.assert_not_called()
            claim.assert_not_called()

    def test_durable_closeout_refuses_a_missing_accepted_candidate_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _selected_fixture(Path(tmp))
            contract, store, runtime, running = _start_selected(fixture)
            before = _committed_state(contract)
            self.assertIsNotNone(running.candidateTree)
            payload = json.loads(store.path.read_bytes())
            payload["candidateTree"] = None
            store.path.write_text(json.dumps(payload))
            corrupted = store.path.read_bytes()
            memory = _PendingMemory([])
            with (
                mock.patch.object(code_quality_gate, "run_clean_quality") as execute,
                _with_memory_owner(memory),
                self.assertRaises(CertificationContractError) as refused,
            ):
                execute_operation(running, runtime)
            self.assertEqual(
                refused.exception.findings[0]["code"], "certification-selection-binding-mismatch"
            )
            current = store.read()
            assert current is not None
            self.assertIsNone(current.candidateTree)
            self.assertEqual(current.certification, running.certification)
            execute.assert_not_called()
            self.assertEqual(memory.received, [])
            self.assertEqual(store.path.read_bytes(), corrupted)
            self.assertEqual(_committed_state(contract), before)

    def test_memory_preflight_aborts_closeout_after_the_code_quality_gate(self) -> None:
        """A failed checker at the injected Gate-5 handoff cannot commit or finalize."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _selected_fixture(Path(tmp))
            contract, store, runtime, running = _start_selected(fixture)
            before = _committed_state(contract)
            failed_quality = {
                "ok": False,
                "findingCount": 1,
                "findings": [
                    {
                        "code": "entity_fingerprint_without_inventory",
                        "path": "entities.md",
                        "message": "orphaned entity fingerprint",
                    }
                ],
            }
            order: list[str] = []
            memory = _PendingMemory(order, run_checker=True)
            calls: list[clean_executor.CleanQualityRequest] = []
            execute = _executor(NODE_FIXTURE, calls)

            def code_gate(request):
                order.append("code-gate")
                return execute(request)

            with (
                mock.patch.object(code_quality_gate, "run_clean_quality", side_effect=code_gate),
                mock.patch.object(
                    worktree_services().memory_quality, "run_check", return_value=failed_quality
                ),
                _with_memory_owner(memory),
                self.assertRaisesRegex(RuntimeError, "entity_fingerprint_without_inventory"),
            ):
                execute_operation(running, runtime)
            self.assertEqual(order, ["code-gate", "memory"])
            self.assertEqual(_committed_state(contract), before)
            current = store.read()
            assert current is not None and current.certification is not None
            self.assertEqual([item.gate for item in current.certification.terminals], [1, 2, 3, 4])

    def test_a_red_code_quality_gate_blocks_the_memory_preflight_and_every_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _selected_fixture(Path(tmp))
            contract, store, runtime, running = _start_selected(fixture)
            before = _committed_state(contract)
            memory = _PendingMemory([])
            calls: list[clean_executor.CleanQualityRequest] = []
            with (
                mock.patch.object(
                    code_quality_gate,
                    "run_clean_quality",
                    side_effect=_executor(NODE_FIXTURE, calls, fail_gate=2),
                ),
                _with_memory_owner(memory),
                self.assertRaisesRegex(RuntimeError, "code-quality gate failed"),
            ):
                execute_operation(running, runtime)
            self.assertEqual(memory.received, [])
            self.assertEqual(_committed_state(contract), before)
            current = store.read()
            assert current is not None
            selected = require_selected_certification(
                load_contract(contract.contract_path), current
            )
            self.assertEqual(
                [(item.result.gate, item.result.disposition) for item in selected.terminals],
                [(1, "green"), (2, "red")],
            )
            self.assertEqual(len(calls), 1)

    def test_preview_advertises_memory_preflight_before_code_quality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            _checkout_with_profile(
                contract.code_worktree,
                repository_id=contract.repo_name,
            )
            write_passing_route_review(contract)
            output = io.StringIO()

            with redirect_stdout(output):
                self.assertEqual(
                    run_authorized_closeout_mechanics(closeout_args(contract, dry_run=True)),
                    0,
                )

            payload = json.loads(output.getvalue())
            order = payload["closeout_order"]
            self.assertLess(
                order.index("run-working-tree-memory-quality-preflight-before-code-quality"),
                order.index("run-configured-pre-commit-hook-once-and-restage-hook-edits"),
            )
            self.assertLess(
                order.index("run-configured-pre-commit-hook-once-and-restage-hook-edits"),
                order.index("run-strict-code-quality-over-that-staged-content"),
            )
            self.assertIn("before Pyright or pytest", payload["summary"])

    def test_closeout_hands_the_gate_the_exact_repository_profile_context(self) -> None:
        """Preview and selected apply preserve exact roots, repository and frozen profile."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _selected_fixture(Path(tmp))
            contract = fixture.contracts[MASTER_A]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    run_authorized_closeout_mechanics(closeout_args(contract, dry_run=True)), 0
                )
            preview = json.loads(output.getvalue())["code_quality_gate"]
            self.assertEqual(preview["status"], code_quality_gate.GATE_ENFORCED)
            self.assertTrue(preview["required"])
            contract, store, runtime, running = _start_selected(fixture)
            before = _committed_state(contract)
            calls: list[clean_executor.CleanQualityRequest] = []
            memory = _PendingMemory([])
            execute = _executor(NODE_FIXTURE, calls)

            def execute_candidate(request):
                prepared = clean_executor._prepare_sandbox(request)
                admitted = execution_sandbox._admit_prepared_profile(request, prepared)
                assert request.execution is not None
                self.assertEqual(
                    admitted.admitted.canonical, request.execution.run.repositoryProfile
                )
                self.assertEqual(admitted.plan, request.execution.run.repositoryPlan)
                self.assertEqual(
                    admitted.admitted.source_path.read_bytes(),
                    (contract.code_worktree / AGENTS_REMEMBER_PROFILE_REFERENCE).read_bytes(),
                )
                return execute(request)

            with (
                mock.patch.object(
                    code_quality_gate,
                    "run_clean_quality",
                    side_effect=execute_candidate,
                ),
                _with_memory_owner(memory),
            ):
                execute_operation(running, runtime)
            self.assertEqual(len(calls), 1)
            request = calls[0]
            self.assertEqual(request.code_worktree, contract.code_worktree)
            self.assertEqual(request.worktree_group, contract.worktree_group)
            self.assertEqual(request.repository_id, contract.repo_name)
            self.assertEqual(request.profile_reference, AGENTS_REMEMBER_PROFILE_REFERENCE)
            self.assertEqual(request.diff_base, contract.code_base_commit)
            self.assertEqual(request.mode, code_quality_gate.GATE_TARGETED)
            assert request.execution is not None
            self.assertEqual(
                request.execution.run.repositoryProfile.profile.repositoryId, contract.repo_name
            )
            self.assertEqual(request.execution.run.repositoryPlan.selectionId, "closeout-targeted")
            self.assertEqual(len(memory.received), 1)
            self.assertEqual(_committed_state(contract), before)
            current = store.read()
            assert current is not None
            self.assertNotEqual(current.status, "completed")

    def test_gate_failure_precedes_all_closeout_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _selected_fixture(Path(tmp))
            contract, store, runtime, running = _start_selected(fixture)
            before = _committed_state(contract)
            memory = _PendingMemory([])
            with (
                mock.patch.object(
                    code_quality_gate,
                    "run_clean_quality",
                    side_effect=RuntimeError("strict code-quality gate failed before code commit"),
                ) as execute,
                _with_memory_owner(memory),
                self.assertRaisesRegex(
                    RuntimeError, "strict code-quality gate failed before code commit"
                ),
            ):
                execute_operation(running, runtime)
            execute.assert_called_once()
            self.assertEqual(memory.received, [])
            self.assertEqual(_committed_state(contract), before)
            current = store.read()
            assert current is not None and current.certification is not None
            self.assertEqual(current.certification.terminals, ())

    def test_success_runs_hook_then_quality_then_waits_for_memory_acceptance(self) -> None:
        """Public code acceptance cannot enter the verified writer before Gate 5 accepts."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _selected_fixture(root)
            contract = fixture.contracts[MASTER_A]
            hook_marker = root / "hook-count.txt"
            _install_hook(contract, f"printf 'hook\\n' >> '{hook_marker}'\n")
            before = _committed_state(contract)
            contract, store, runtime, running = _start_selected(fixture)
            self.assertEqual(hook_marker.read_text().splitlines(), ["hook"])
            events: list[str] = []
            calls: list[clean_executor.CleanQualityRequest] = []
            execute = _executor(NODE_FIXTURE, calls)
            memory = _PendingMemory(events)

            def run_gate(request):
                self.assertEqual(hook_marker.read_text().splitlines(), ["hook"])
                events.append("quality")
                return execute(request)

            with (
                mock.patch.object(code_quality_gate, "run_clean_quality", side_effect=run_gate),
                mock.patch.object(
                    closeout_recovery, "commit_verified_staged", wraps=commit_verified_staged
                ) as commit,
                _with_memory_owner(memory),
            ):
                execute_operation(running, runtime)
            self.assertEqual(events, ["quality", "memory"])
            self.assertEqual(hook_marker.read_text().splitlines(), ["hook"])
            commit.assert_not_called()
            self.assertEqual(_committed_state(contract), before)
            current = store.read()
            assert current is not None
            self.assertNotEqual(current.status, "completed")


GATE_REFUSAL = "strict code-quality gate failed before code commit with exit code 1"


def _refusing_gate(message: str = GATE_REFUSAL):
    return mock.patch.object(
        closeout_staged_quality,
        "run_strict_code_quality_gate",
        side_effect=RuntimeError(message),
    )


def _task_worktree(root: Path) -> tuple[Path, Path]:
    """A real repository and linked worktree off it -- the shape closeout stages in.
    Both are real: the precondition is git's own distinction, so a fake would test itself.
    """
    repo = root / "repo"
    init_repo(repo, "main")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    install_agents_remember_profile(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "Add a tracked file and certification profile")
    worktree = root / "task-worktree"
    git(repo, "worktree", "add", "-b", "ar/task", str(worktree), "main")
    return repo, worktree


class CertifiedIndexCommitTests(unittest.TestCase):
    def test_async_candidate_refuses_later_worktree_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            (worktree / "tracked.txt").write_text("accepted\n", encoding="utf-8")
            candidate = closeout_staged_quality.worktree_candidate_tree(
                worktree, worktree.parent / "candidate.index"
            )
            (worktree / "tracked.txt").write_text("later\n", encoding="utf-8")

            with (
                mock.patch.object(closeout_staged_quality, "run_strict_code_quality_gate") as gate,
                self.assertRaisesRegex(RuntimeError, "candidate changed"),
            ):
                closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                    candidate_tree=candidate,
                )

            gate.assert_not_called()

    def test_async_candidate_is_the_tree_the_gate_receives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            (worktree / "accepted.py").write_text("VALUE = 1\n", encoding="utf-8")
            candidate = closeout_staged_quality.worktree_candidate_tree(
                worktree, worktree.parent / "candidate.index"
            )

            with mock.patch.object(
                closeout_staged_quality,
                "run_strict_code_quality_gate",
                side_effect=publish_passing_quality_gate,
            ):
                closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                    candidate_tree=candidate,
                )

            self.assertEqual(git(worktree, "write-tree"), candidate)

    def test_materialized_index_must_equal_the_accepted_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            (worktree / "accepted.py").write_text("VALUE = 1\n", encoding="utf-8")
            candidate = closeout_staged_quality.worktree_candidate_tree(
                worktree, worktree.parent / "candidate.index"
            )
            real_require_git = closeout_staged_quality.require_git

            def mismatched_write_tree(repo: Path, args: list[str]) -> str:
                if args == ["write-tree"]:
                    return "f" * 40
                return real_require_git(repo, args)

            with (
                mock.patch.object(
                    closeout_staged_quality, "worktree_candidate_tree", return_value=candidate
                ),
                mock.patch.object(
                    closeout_staged_quality, "require_git", side_effect=mismatched_write_tree
                ),
                self.assertRaisesRegex(RuntimeError, "while materializing the accepted tree"),
            ):
                closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                    candidate_tree=candidate,
                )

    def test_pre_commit_hook_runs_once_before_gate_and_not_during_verified_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            hooks = worktree / ".githooks"
            hooks.mkdir()
            marker = worktree / "hook-runs.txt"
            hook = hooks / "pre-commit"
            hook.write_text(
                f"#!/bin/sh\nprintf 'run\\n' >> '{marker.as_posix()}'\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            git(worktree, "config", "core.hooksPath", ".githooks")
            (worktree / "tracked.txt").write_text("two\n", encoding="utf-8")

            with mock.patch.object(
                closeout_staged_quality,
                "run_strict_code_quality_gate",
                side_effect=publish_passing_quality_gate,
            ):
                result = closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                )
            (worktree / "tracked.txt").write_text("three\n", encoding="utf-8")
            commit_verified_staged(worktree, "Commit the certified index")

            self.assertEqual(result["preCommitHook"], "passed")
            self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["run"])
            self.assertEqual(git(worktree, "show", "HEAD:tracked.txt"), "two")
            self.assertEqual((worktree / "tracked.txt").read_text(encoding="utf-8"), "three\n")
            self.assertEqual(
                commit_verified_staged(worktree, "No staged changes"),
                git(worktree, "rev-parse", "HEAD"),
            )

    def test_hook_mutation_invalidates_the_independently_reviewed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            hooks = worktree / ".githooks"
            hooks.mkdir()
            hook = hooks / "pre-commit"
            hook.write_text(
                "#!/bin/sh\nprintf 'hooked\\n' > tracked.txt\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            git(worktree, "config", "core.hooksPath", ".githooks")
            (worktree / "tracked.txt").write_text("reviewed\n", encoding="utf-8")
            candidate = closeout_staged_quality.worktree_candidate_tree(
                worktree, worktree.parent / "candidate.index"
            )

            with (
                mock.patch.object(closeout_staged_quality, "run_strict_code_quality_gate") as gate,
                self.assertRaisesRegex(
                    RuntimeError, "pre-commit hook changed the independently reviewed candidate"
                ),
            ):
                closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                    candidate_tree=candidate,
                )

            gate.assert_not_called()

    def test_clean_hook_preserves_the_independently_reviewed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            hooks = worktree / ".githooks"
            hooks.mkdir()
            hook = hooks / "pre-commit"
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hook.chmod(0o755)
            git(worktree, "config", "core.hooksPath", ".githooks")
            (worktree / "tracked.txt").write_text("reviewed\n", encoding="utf-8")
            candidate = closeout_staged_quality.worktree_candidate_tree(
                worktree, worktree.parent / "candidate.index"
            )

            with mock.patch.object(
                closeout_staged_quality,
                "run_strict_code_quality_gate",
                side_effect=publish_passing_quality_gate,
            ) as gate:
                result = closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                    candidate_tree=candidate,
                )

            self.assertEqual(result["preCommitHook"], "passed")
            gate.assert_called_once()


def _conflicted_task_worktree(root: Path) -> Path:
    repo, worktree = _task_worktree(root)
    git(repo, "checkout", "-b", "side")
    (repo / "tracked.txt").write_text("side\n", encoding="utf-8")
    git(repo, "commit", "-am", "Change tracked.txt on side")
    git(repo, "checkout", "main")
    (worktree / "tracked.txt").write_text("task\n", encoding="utf-8")
    git(worktree, "commit", "-am", "Change tracked.txt on the task branch")
    subprocess.run(
        ["git", "merge", "side"], cwd=worktree, capture_output=True, text=True, check=False
    )
    return worktree


class TaskWorktreePreconditionTests(unittest.TestCase):
    """Closeout stages, so it must first establish that staging here is free.
    A task worktree is disposable scratch space nobody works in, but a ``series``
    contract records the repository's own checkout. The guard proves through git
    itself (``--git-dir`` differing from ``--git-common-dir``) that the path to be
    written is a linked worktree, not a checkout a person works in.
    """

    def test_the_repositorys_own_checkout_is_refused_before_anything_is_staged(self) -> None:
        """Asserted as the damage that does not happen, not merely as a message.
        Without the guard, ``git add -A`` rewrites the ``add -p`` selection and stages
        the deliberately untracked ``secret.env`` -- both unrecoverable from git alone.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo, _worktree = _task_worktree(Path(tmp))
            # A partial `git add -p` selection: index and working tree deliberately differ.
            (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
            git(repo, "add", "tracked.txt")
            (repo / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
            (repo / "secret.env").write_text("TOKEN=REAL\n", encoding="utf-8")
            status_before = git(repo, "status", "--porcelain")

            with (
                mock.patch.object(closeout_staged_quality, "run_strict_code_quality_gate") as gate,
                self.assertRaises(RuntimeError) as caught,
            ):
                closeout_module._gate_staged_code(
                    _quality_target(repo, repo.parent),
                    diff_base="HEAD",
                )

            # The selection survives, and the untracked secret is still untracked with no
            self.assertEqual(git(repo, "show", ":tracked.txt"), "one\ntwo")
            self.assertEqual(git(repo, "ls-files", "--", "secret.env"), "")
            self.assertEqual(git(repo, "status", "--porcelain"), status_before)
            gate.assert_not_called()
            message = str(caught.exception)
            self.assertIn("is not a task worktree", message)
            self.assertIn("Nothing was staged and nothing was committed", message)

    def test_a_series_contracts_code_worktree_is_exactly_that_checkout(self) -> None:
        """The refusal above is aimed at a contract the system really can produce.
        Without this, the guard is a guess about a shape nobody builds. ``kind: "series"``
        records the repository path itself, so it is the concrete way a closeout could have
        reached a checkout a person works in.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _worktree = _task_worktree(root)
            series = default_series_contract(
                ContractTask(
                    name="Series Thing",
                    repo_name="repo",
                    coordination_root=root / "ar-coordination",
                    workflow_kind="chat-task",
                    memory_mode="internal",
                ),
                code=RepoBranchPlan(
                    repo_path=repo,
                    source_branch="main",
                    work_branch="ar/series-thing",
                    base_commit=git(repo, "rev-parse", "HEAD"),
                ),
            )

            self.assertEqual(series.kind, "series")
            self.assertEqual(series.code_worktree, repo)
            with self.assertRaises(RuntimeError) as caught:
                closeout_module._gate_staged_code(
                    _quality_target(series.code_worktree, series.worktree_group),
                    diff_base="HEAD",
                )
            self.assertIn("is not a task worktree", str(caught.exception))

    def test_a_task_worktree_passes_the_precondition_and_is_staged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            (worktree / "created.py").write_text("VALUE = 1\n", encoding="utf-8")
            with mock.patch.object(
                closeout_staged_quality,
                "run_strict_code_quality_gate",
                side_effect=publish_passing_quality_gate,
            ):
                result = closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                )

            self.assertTrue(result["required"])
            self.assertTrue(result["passed"])
            self.assertEqual(result["preCommitHook"], "not-configured")
            self.assertIn("created.py", git(worktree, "ls-files"))

    def test_a_refused_gate_leaves_the_task_worktree_staged(self) -> None:
        """No rollback, stated as a test rather than left to be discovered.
        There is no index snapshot to restore and nothing that must run at exit.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            (worktree / "created.py").write_text("VALUE = 1\n", encoding="utf-8")

            with _refusing_gate(), self.assertRaises(RuntimeError):
                closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                )

            self.assertIn("created.py", git(worktree, "ls-files"))
            git_dir = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-dir"))
            self.assertFalse((git_dir / "index.lock").exists())
            self.assertEqual(sorted(git_dir.glob("ar-closeout-index-*")), [])


class ConflictedIndexTests(unittest.TestCase):
    """A conflicted worktree fails cleanly instead of committing the markers.
    ``git add -A`` over an unmerged index resolves conflicts to the working tree,
    markers included; the refusal is deliberate, pre-staging, and names the state.
    """

    def test_a_conflicted_worktree_is_refused_before_anything_is_staged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _conflicted_task_worktree(Path(tmp))
            head_before = git(worktree, "rev-parse", "HEAD")
            status_before = git(worktree, "status", "--porcelain")
            self.assertIn("<<<<<<<", (worktree / "tracked.txt").read_text(encoding="utf-8"))

            with (
                mock.patch.object(closeout_staged_quality, "run_strict_code_quality_gate") as gate,
                self.assertRaises(RuntimeError) as caught,
            ):
                closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                )

            message = str(caught.exception)
            self.assertIn("closeout cannot stage the code worktree", message)
            self.assertIn("unmerged path", message)
            self.assertIn("tracked.txt", message)
            self.assertIn("conflict markers", message)
            gate.assert_not_called()
            self.assertEqual(git(worktree, "rev-parse", "HEAD"), head_before)
            self.assertEqual(git(worktree, "status", "--porcelain"), status_before)

    def test_the_reset_runs_after_the_conflict_check_not_before_it(self) -> None:
        """Order, asserted through what survives rather than through call bookkeeping.
        A mixed reset drops the unmerged entries and removes ``MERGE_HEAD``; run too
        early, it would silence the refusal, so the intact merge proves no reset ran.
        """
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _conflicted_task_worktree(Path(tmp))
            git_dir = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-dir"))
            self.assertTrue((git_dir / "MERGE_HEAD").exists())

            with (
                mock.patch.object(closeout_staged_quality, "run_strict_code_quality_gate") as gate,
                self.assertRaises(RuntimeError),
            ):
                closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                )

            gate.assert_not_called()
            self.assertTrue((git_dir / "MERGE_HEAD").exists())
            self.assertEqual(git(worktree, "diff", "--name-only", "--diff-filter=U"), "tracked.txt")


DROPPED_TOOL_ARTEFACT = ".dmypy.json"


class RetryStagesWhatAFirstRunWouldTests(unittest.TestCase):
    """A refused attempt must not decide what the next attempt commits.
    A file staged by a refused gate stays staged after the leaf ignores it, so the
    retry would commit it; the mixed reset removes that path dependence. The
    property is asserted as an equality of committed trees against a worktree that
    never saw the refusal.
    """

    @staticmethod
    def _end_state(worktree: Path) -> None:
        """The files as the leaf leaves them once it has ignored the tool's artefact."""
        (worktree / "a.py").write_text("VALUE = 2\n", encoding="utf-8")
        (worktree / DROPPED_TOOL_ARTEFACT).write_text('{"pid": 1}\n', encoding="utf-8")
        (worktree / ".gitignore").write_text(f"{DROPPED_TOOL_ARTEFACT}\n", encoding="utf-8")

    def _gate_then_commit(self, worktree: Path, message: str) -> str:
        with mock.patch.object(
            closeout_staged_quality,
            "run_strict_code_quality_gate",
            side_effect=publish_passing_quality_gate,
        ):
            closeout_module._gate_staged_code(
                _quality_target(worktree, worktree.parent),
                diff_base="HEAD",
            )
        commit_if_dirty(worktree, message)
        return git(worktree, "rev-parse", "HEAD^{tree}")

    def test_a_retry_commits_the_tree_a_first_run_would(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, retried = _task_worktree(root)
            fresh = root / "fresh-worktree"
            git(repo, "worktree", "add", "-b", "ar/fresh", str(fresh), "main")

            # Attempt one: the artefact is on disk and not yet ignored, and the gate refuses.
            (retried / "a.py").write_text("VALUE = 2\n", encoding="utf-8")
            (retried / DROPPED_TOOL_ARTEFACT).write_text('{"pid": 1}\n', encoding="utf-8")
            with _refusing_gate(), self.assertRaises(RuntimeError):
                closeout_module._gate_staged_code(
                    _quality_target(retried, retried.parent),
                    diff_base="HEAD",
                )
            self.assertIn(DROPPED_TOOL_ARTEFACT, git(retried, "ls-files"))
            # The leaf adds the ignore rule and retries, against a worktree still staged.
            self._end_state(retried)
            retried_tree = self._gate_then_commit(retried, "Closeout on the retry")
            self._end_state(fresh)
            fresh_tree = self._gate_then_commit(fresh, "Closeout on a first run")

            self.assertEqual(retried_tree, fresh_tree)
            self.assertNotIn(
                DROPPED_TOOL_ARTEFACT,
                git(retried, "ls-tree", "-r", "--name-only", "HEAD").splitlines(),
            )
