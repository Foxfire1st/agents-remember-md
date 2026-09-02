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
from agents_remember.models.lifecycles.operation import LifecycleOperationRecoveryCommits
from agents_remember.worktrees.modules import closeout as closeout_module
from agents_remember.worktrees.modules import closeout_external
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.git import commit_if_dirty, commit_verified_staged
from agents_remember.worktrees.modules.quality import closeout_memory as closeout_memory_quality
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.queue import closeout_recovery, closeout_staged_quality
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    RepoBranchPlan,
    default_series_contract,
    load_contract,
    write_contract,
)
from closeout_input_test_support import MutationEvidenceRecorder, closeout_worktree_args
from test_worktree_support import (
    closeout_args,
    dirty_open_external_contract_fixture,
    git,
    init_repo,
    run_authorized_closeout_mechanics,
    write_file_onboarding,
    write_passing_route_review,
)


def _checkout_with_wrapper(root: Path) -> Path:
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
    wrapper = root / code_quality_gate.QUALITY_WRAPPER
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("# wrapper marker\n", encoding="utf-8")
    return root


def _quality_target(
    worktree: Path, worktree_group: Path | None = None
) -> code_quality_gate.QualityGateTarget:
    return code_quality_gate.QualityGateTarget(
        code_worktree=worktree,
        worktree_group=worktree_group or worktree / "enclosure",
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
    def test_agents_remember_closeout_refuses_a_missing_self_owned_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            initial = dirty_open_external_contract_fixture(Path(tmp))
            task_root = (
                initial.coordination_root / "tasks" / "agents-remember" / initial.task_root.name
            )
            contract = replace(
                initial,
                repo_name="agents-remember",
                task_root=task_root,
                task_artifact=task_root / "task.md",
            )
            write_contract(contract.contract_path, contract)
            write_passing_route_review(contract)

            with (
                mock.patch.object(
                    closeout_module,
                    "_closeout_attestations",
                    return_value=closeout_module._CloseoutAttestations(),
                ),
                mock.patch.object(
                    closeout_module,
                    "accepted_closeout_memory_pair",
                    return_value=mock.Mock(
                        no_impact=closeout_module.CuratorCoherenceNoImpact(),
                        pair_identity=None,
                    ),
                ),
                mock.patch.object(closeout_module, "_memory_quality_before_refresh") as memory,
                mock.patch.object(closeout_module, "_claim_closeout_gate") as claim,
                mock.patch.object(closeout_module, "accepted_code_commit") as commit,
                mock.patch.object(closeout_staged_quality, "run_strict_code_quality_gate") as gate,
                self.assertRaisesRegex(RuntimeError, "self-owned wrapper"),
            ):
                run_authorized_closeout_mechanics(closeout_args(contract))

            memory.assert_not_called()
            gate.assert_not_called()
            claim.assert_not_called()
            commit.assert_not_called()

    def test_contract_finalization_retry_reuses_exact_external_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            assert contract.memory_worktree is not None
            assert contract.ledger_path is not None
            captured: dict[str, str] = {}
            mutation_recorder = MutationEvidenceRecorder()

            def progress(phase: str, evidence: Mapping[str, object]) -> None:
                mutation_recorder(phase, evidence)
                if phase == "contract-finalization":
                    value = evidence.get("recovery_commits")
                    assert isinstance(value, dict)
                    captured.update({str(key): str(item) for key, item in value.items()})

            first = closeout_worktree_args(
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
            with (
                mock.patch.object(
                    closeout_module,
                    "write_contract",
                    side_effect=RuntimeError("contract write interrupted"),
                ),
                self.assertRaisesRegex(RuntimeError, "contract write interrupted"),
            ):
                closeout_module.closeout_result(first, contract)

            code_head = git(contract.code_worktree, "rev-parse", "HEAD")
            memory_head = git(contract.memory_worktree, "rev-parse", "HEAD")
            ledger_after_first = contract.ledger_path.read_bytes()
            self.assertEqual(captured["codeCommit"], code_head)
            self.assertEqual(captured["ledgerCommit"], memory_head)
            _assert_closeout_commit_subjects(contract, captured)
            mutation_recorder.assert_proven("code", "memory", "ledger")
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")

            recovered = closeout_module.closeout_result(
                replace(
                    first,
                    recovery_commits=LifecycleOperationRecoveryCommits.model_validate(captured),
                    operation_progress=progress,
                ),
                contract,
            )

            self.assertEqual(recovered.payload["state"], "closed")
            self.assertTrue(recovered.payload["recovered"])
            pair_identity = recovered.payload["pairIdentity"]
            self.assertIsInstance(pair_identity, dict)
            assert isinstance(pair_identity, dict)
            self.assertEqual(
                pair_identity["contractPath"],
                contract.contract_path.resolve().as_posix(),
            )
            self.assertEqual(
                pair_identity["codeRoot"],
                contract.code_worktree.resolve().as_posix(),
            )
            self.assertEqual(
                pair_identity["memoryRoot"],
                contract.memory_worktree.resolve().as_posix(),
            )
            self.assertEqual(git(contract.code_worktree, "rev-parse", "HEAD"), code_head)
            self.assertEqual(git(contract.memory_worktree, "rev-parse", "HEAD"), memory_head)
            self.assertEqual(contract.ledger_path.read_bytes(), ledger_after_first)
            updated = load_contract(contract.contract_path)
            self.assertEqual(updated.code_commit, captured["codeCommit"])
            self.assertEqual(
                updated.memory_content_commit,
                captured["memoryContentCommit"],
            )
            self.assertEqual(updated.ledger_commit, captured["ledgerCommit"])

    def test_memory_commit_interruption_stays_bound_to_the_published_ledger_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            assert contract.memory_worktree is not None
            assert contract.ledger_path is not None
            previous_memory = contract.memory_content_commit
            mutation_recorder = MutationEvidenceRecorder()

            first = closeout_worktree_args(
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
            with (
                mock.patch.object(
                    closeout_external,
                    "write_ledger",
                    side_effect=RuntimeError("ledger write interrupted"),
                ),
                self.assertRaisesRegex(RuntimeError, "ledger write interrupted"),
            ):
                closeout_module.closeout_result(first, contract)

            code_commit = mutation_recorder.evidence["code"].commit
            memory_commit = mutation_recorder.evidence["memory"].commit
            assert code_commit is not None
            assert memory_commit is not None
            self.assertTrue(memory_commit)
            self.assertNotEqual(memory_commit, previous_memory)
            self.assertEqual(git(contract.memory_worktree, "rev-parse", "HEAD"), memory_commit)
            self.assertEqual(mutation_recorder.evidence["code"].state, "commit-proven")
            self.assertEqual(mutation_recorder.evidence["memory"].state, "commit-proven")
            self.assertEqual(mutation_recorder.evidence["ledger"].state, "mutation-intent")
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")
            mapping = closeout_external.find_mapping(
                closeout_external.load_ledger(contract.ledger_path), code_commit
            )
            self.assertIsNone(mapping)

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

    def test_source_lineage_is_rechecked_after_quality_before_approval_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            with (
                mock.patch.object(
                    closeout_module,
                    "_validate_closeout_source_state",
                    side_effect=[None, RuntimeError("super moved during quality")],
                ) as source_check,
                mock.patch.object(
                    closeout_module,
                    "_closeout_quality_preflight",
                    return_value=({"required": True, "passed": True}, {}, False),
                ),
                mock.patch.object(closeout_module, "_claim_closeout_gate") as claim,
                self.assertRaisesRegex(RuntimeError, "super moved during quality"),
            ):
                run_authorized_closeout_mechanics(closeout_args(contract))

            self.assertEqual(source_check.call_count, 2)
            claim.assert_not_called()

    def test_route_review_is_rechecked_after_quality_before_approval_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            with (
                mock.patch.object(
                    closeout_module,
                    "require_current_route_review",
                    side_effect=[
                        {"required": True, "candidateTree": "reviewed"},
                        {"required": True, "candidateTree": "changed-during-quality"},
                    ],
                ),
                mock.patch.object(
                    closeout_module,
                    "_closeout_quality_preflight",
                    return_value=({"required": True, "passed": True}, {}, False),
                ),
                mock.patch.object(closeout_module, "_claim_closeout_gate") as claim,
                self.assertRaisesRegex(RuntimeError, "changed after route review and quality"),
            ):
                run_authorized_closeout_mechanics(closeout_args(contract))

            claim.assert_not_called()

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
            contract = dirty_open_external_contract_fixture(Path(tmp))
            args = replace(
                closeout_worktree_args(
                    contract,
                    approved=True,
                    approval_note="approved",
                    operation_progress=MutationEvidenceRecorder(),
                ),
                operation_key="closeout:test",
                candidate_tree=None,
            )

            with (
                mock.patch.object(closeout_module, "_closeout_quality_preflight") as quality,
                self.assertRaisesRegex(RuntimeError, "missing its accepted candidate tree"),
            ):
                closeout_module.closeout_result(args, contract)

            quality.assert_not_called()

    def test_memory_preflight_failure_never_starts_the_code_quality_gate(self) -> None:
        """A broken entity catalog must abort before hooks, Ruff, Pyright, or pytest."""
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
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

            with (
                mock.patch.object(
                    closeout_module, "requires_strict_code_quality", return_value=True
                ),
                mock.patch.object(
                    closeout_module.worktree_services().memory_quality,
                    "run_check",
                    return_value=failed_quality,
                ),
                mock.patch.object(
                    closeout_staged_quality, "run_pre_commit_hook_if_configured"
                ) as hook,
                mock.patch.object(closeout_staged_quality, "run_strict_code_quality_gate") as gate,
                self.assertRaisesRegex(RuntimeError, "entity_fingerprint_without_inventory"),
            ):
                run_authorized_closeout_mechanics(closeout_args(contract))

            hook.assert_not_called()
            gate.assert_not_called()
            self.assertEqual(
                git(contract.code_worktree, "rev-parse", "HEAD"), contract.code_base_commit
            )

    def test_preview_advertises_memory_preflight_before_code_quality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            _checkout_with_wrapper(contract.code_worktree)
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

    def test_closeout_hands_the_gate_the_code_worktree_not_the_repository_name(self) -> None:
        """Both closeout entry points must pass the checkout, and nothing else catches it.

        The deciders take a checkout path. Handing them ``contract.repo_name`` -- the
        signature they had before the repository-name hard-code was removed -- makes
        ``quality_wrapper_path`` build a relative path off the process CWD, which is not a
        file, so ``requires_strict_code_quality`` returns ``False`` and the gate the product
        documents as mandatory silently never runs. ``contract`` is unannotated in
        ``closeout.py``, so Pyright type-checks that mistake in silence; every other test in
        this file patches ``requires_strict_code_quality`` out and cannot see the argument.
        """
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            _checkout_with_wrapper(contract.code_worktree)
            assert contract.memory_worktree is not None
            write_file_onboarding(  # the planted wrapper is a changed source file too
                contract.memory_worktree / "onboarding",
                contract.repo_name,
                code_quality_gate.QUALITY_WRAPPER.as_posix(),
                contract.code_base_commit,
            )
            write_passing_route_review(contract)
            deciders: list[object] = []
            real_requires = code_quality_gate.requires_strict_code_quality

            def spy(
                target: Path,
                *,
                code_would_commit: bool,
                required_when_missing: bool = False,
            ) -> bool:
                deciders.append(target)
                return real_requires(
                    target,
                    code_would_commit=code_would_commit,
                    required_when_missing=required_when_missing,
                )

            # Preview path (closeout.py:282): reports the enforced state for a dirty
            # checkout that carries the wrapper, rather than "wrapper-unavailable".
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    run_authorized_closeout_mechanics(closeout_args(contract, dry_run=True)),
                    0,
                )
            gate = json.loads(output.getvalue())["code_quality_gate"]
            self.assertEqual(gate["status"], code_quality_gate.GATE_ENFORCED)
            self.assertTrue(gate["required"])

            # Apply path (closeout.py:589-593): the real decider runs and fires the gate.
            with (
                mock.patch.object(closeout_module, "requires_strict_code_quality", side_effect=spy),
                mock.patch.object(
                    closeout_staged_quality,
                    "run_strict_code_quality_gate",
                    side_effect=publish_passing_quality_gate,
                ) as gate_run,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(run_authorized_closeout_mechanics(closeout_args(contract)), 0)

            self.assertEqual(deciders, [contract.code_worktree])
            gate_run.assert_called_once_with(
                code_quality_gate.QualityGateTarget(
                    code_worktree=contract.code_worktree,
                    worktree_group=contract.worktree_group,
                ),
                diff_base=contract.code_base_commit,
                plan=code_quality_gate.QualityGatePlan(
                    mode=code_quality_gate.GATE_TARGETED,
                    executor="dagger",
                ),
            )

    def test_gate_failure_precedes_all_closeout_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            assert contract.memory_worktree is not None
            code_head = git(contract.code_worktree, "rev-parse", "HEAD")
            memory_head = git(contract.memory_worktree, "rev-parse", "HEAD")
            ledger_before = (contract.memory_worktree / "memory.md").read_bytes()

            with (
                mock.patch.object(
                    closeout_module, "requires_strict_code_quality", return_value=True
                ),
                mock.patch.object(
                    closeout_staged_quality,
                    "run_strict_code_quality_gate",
                    side_effect=RuntimeError("strict code-quality gate failed before code commit"),
                ),
                self.assertRaisesRegex(
                    RuntimeError, "strict code-quality gate failed before code commit"
                ),
            ):
                run_authorized_closeout_mechanics(closeout_args(contract))

            self.assertEqual(git(contract.code_worktree, "rev-parse", "HEAD"), code_head)
            self.assertEqual(git(contract.memory_worktree, "rev-parse", "HEAD"), memory_head)
            self.assertEqual((contract.memory_worktree / "memory.md").read_bytes(), ledger_before)
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")

    def test_success_runs_hook_then_quality_then_verified_code_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = dirty_open_external_contract_fixture(Path(tmp))
            events: list[str] = []

            def run_gate(
                target: code_quality_gate.QualityGateTarget,
                *,
                diff_base: str = "",
                plan: code_quality_gate.QualityGatePlan | None = None,
            ) -> dict[str, object]:
                events.append("quality")
                return publish_passing_quality_gate(target, diff_base=diff_base, plan=plan)

            def run_hook(_repo: Path) -> bool:
                events.append("pre-commit-hook")
                return False

            def record_verified_commit(repo: Path, message: str) -> str:
                events.append("verified-code-commit")
                return commit_verified_staged(repo, message)

            with (
                mock.patch.object(
                    closeout_module, "requires_strict_code_quality", return_value=True
                ),
                mock.patch.object(
                    closeout_staged_quality,
                    "run_strict_code_quality_gate",
                    side_effect=run_gate,
                ),
                mock.patch.object(
                    closeout_staged_quality,
                    "run_pre_commit_hook_if_configured",
                    side_effect=run_hook,
                ),
                mock.patch.object(
                    closeout_recovery,
                    "commit_verified_staged",
                    side_effect=record_verified_commit,
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(run_authorized_closeout_mechanics(closeout_args(contract)), 0)

            self.assertEqual(events[:3], ["pre-commit-hook", "quality", "verified-code-commit"])


GATE_REFUSAL = "strict code-quality gate failed before code commit with exit code 1"


def _refusing_gate(message: str = GATE_REFUSAL):
    return mock.patch.object(
        closeout_staged_quality,
        "run_strict_code_quality_gate",
        side_effect=RuntimeError(message),
    )


def _task_worktree(root: Path) -> tuple[Path, Path]:
    """A repository and a linked worktree off it, which is the shape closeout runs in.

    ``(repository checkout, task worktree)``. Both are real: the precondition under test
    is git's own distinction between the two, so a fixture that faked it would be testing
    the fixture.
    """
    repo = root / "repo"
    init_repo(repo, "main")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "Add a tracked file")
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
                    worktree,
                    worktree_group=worktree.parent,
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
                    worktree,
                    worktree_group=worktree.parent,
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
                    worktree,
                    worktree_group=worktree.parent,
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
                    worktree, worktree_group=worktree.parent, diff_base="HEAD"
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
                    worktree,
                    worktree_group=worktree.parent,
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
                    worktree,
                    worktree_group=worktree.parent,
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

    Staging is safe in a task worktree because that checkout is disposable scratch space
    with nobody in it -- ``worktree_start`` makes it and ``lifecycle_finalize_task``
    destroys it. It is not safe in a repository's own checkout, and closeout can be handed
    one: ``default_series_contract`` records ``code_worktree=code.repo_path`` for a
    ``kind: "series"`` contract, and nothing else on the apply path would stop it.

    The guard tests git's own definition of a linked worktree -- ``--git-dir`` differing
    from ``--git-common-dir`` -- rather than the contract's ``kind``, because that is the
    property the safety argument actually rests on. ``kind`` is a label beside the path;
    the git-dir comparison constrains the path that is about to be written.
    """

    def test_the_repositorys_own_checkout_is_refused_before_anything_is_staged(self) -> None:
        """Asserted as the damage that does not happen, not merely as a message.

        Measured on git 2.43 with this guard removed: ``git add -A`` here rewrites the
        staged ``t.txt`` from the ``add -p`` selection (``one\\ntwo``) to the working-tree
        version, and stages ``secret.env`` -- writing a durable blob for a file the person
        deliberately left untracked. Both are unrecoverable from git alone.
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
                    repo, worktree_group=repo.parent, diff_base="HEAD"
                )

            # The selection survives, and the untracked secret is still untracked with no
            # object written for it.
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
                    series.code_worktree,
                    worktree_group=series.worktree_group,
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
                    worktree, worktree_group=worktree.parent, diff_base="HEAD"
                )

            self.assertTrue(result["required"])
            self.assertTrue(result["passed"])
            self.assertEqual(result["preCommitHook"], "not-configured")
            self.assertIn("created.py", git(worktree, "ls-files"))

    def test_a_refused_gate_leaves_the_task_worktree_staged(self) -> None:
        """No rollback, stated as a test rather than left to be discovered.

        An earlier attempt saved the index file aside and copied it back. That machinery is
        gone: there is no snapshot to orphan, no ``index.lock`` to leave stale, and nothing
        that has to run at exit for the checkout to be in a sane state.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            (worktree / "created.py").write_text("VALUE = 1\n", encoding="utf-8")

            with _refusing_gate(), self.assertRaises(RuntimeError):
                closeout_module._gate_staged_code(
                    worktree, worktree_group=worktree.parent, diff_base="HEAD"
                )

            self.assertIn("created.py", git(worktree, "ls-files"))
            git_dir = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-dir"))
            self.assertFalse((git_dir / "index.lock").exists())
            self.assertEqual(sorted(git_dir.glob("ar-closeout-index-*")), [])


class ConflictedIndexTests(unittest.TestCase):
    """A conflicted worktree fails cleanly instead of committing the markers.

    ``git add -A`` over an unmerged index does not refuse -- it resolves every conflict to
    whatever the working tree holds, markers included, and closeout then commits that. The
    refusal is deliberate, is checked before anything is staged, and says what state the
    checkout is in rather than reporting plumbing from a command nobody ran.
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
                    worktree, worktree_group=worktree.parent, diff_base="HEAD"
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

        A mixed reset drops the unmerged index entries and removes ``MERGE_HEAD``. Run
        before the check, it would leave ``diff --diff-filter=U`` with nothing to report,
        the refusal would never fire again, and ``add -A`` would go on to stage the
        ``<<<<<<<`` markers. So the merge being intact after the refusal is the property
        that says the reset has not run yet -- and it is the property that keeps the
        refusal above from quietly becoming unreachable.
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
                    worktree, worktree_group=worktree.parent, diff_base="HEAD"
                )

            gate.assert_not_called()
            self.assertTrue((git_dir / "MERGE_HEAD").exists())
            self.assertEqual(git(worktree, "diff", "--name-only", "--diff-filter=U"), "tracked.txt")


DROPPED_TOOL_ARTEFACT = ".dmypy.json"


class RetryStagesWhatAFirstRunWouldTests(unittest.TestCase):
    """A refused attempt must not decide what the next attempt commits.

    ``git add -A`` applies ignore rules only to paths git does not already track or hold
    staged. A file staged by a refused gate therefore stays staged after the leaf adds it to
    ``.gitignore``, and the retry commits it -- which is exactly how a ``.dmypy.json`` a type
    checker had dropped in the worktree got into this leaf's own first commit. The mixed
    reset is what removes that path dependence.

    The property is asserted as an equality of committed trees rather than as the presence
    of a ``reset`` call: what has to hold is that a retry commits what a worktree that never
    saw the refusal commits. Both sides run the same closeout steps against the same end
    state on disk, so the only thing that can make the trees differ is history the index
    carried across attempts.
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
                worktree, worktree_group=worktree.parent, diff_base="HEAD"
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
                    retried, worktree_group=retried.parent, diff_base="HEAD"
                )
            self.assertIn(DROPPED_TOOL_ARTEFACT, git(retried, "ls-files"))

            # The leaf adds the ignore rule and retries, against a worktree still staged.
            self._end_state(retried)
            retried_tree = self._gate_then_commit(retried, "Closeout on the retry")

            # The same end state, closed out once, in a worktree that never saw the refusal.
            self._end_state(fresh)
            fresh_tree = self._gate_then_commit(fresh, "Closeout on a first run")

            self.assertEqual(retried_tree, fresh_tree)
            self.assertNotIn(
                DROPPED_TOOL_ARTEFACT,
                git(retried, "ls-tree", "-r", "--name-only", "HEAD").splitlines(),
            )
