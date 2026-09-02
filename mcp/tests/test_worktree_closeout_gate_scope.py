from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from _quality_evidence_fixture import publish_passing_quality_gate
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.queue import closeout_staged_quality
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    default_contract,
    default_series_contract,
    load_contract,
    write_contract,
)
from agents_remember_test_support.code_quality import check as quality_check
from repository_profile_test_support import install_fixture_profile
from test_worktree_support import (
    closeout_args,
    git,
    init_repo,
    run_authorized_closeout_mechanics,
    write_passing_route_review,
)

CREATED_FILE = "pkg/leaf_addition.py"


def gate_scope_contract_fixture(root: Path):
    """A leaf whose closeout isolates exactly what the quality gate receives."""
    code_repo = root / "repo-a"
    init_repo(code_repo, "main")
    install_fixture_profile(code_repo, "repo-a")
    (code_repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
        "[tool.agents_remember]\n"
        'product_package_roots = ["pkg"]\n'
        "verification_package_roots = []\n",
        encoding="utf-8",
    )
    (code_repo / "pkg").mkdir()
    (code_repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (code_repo / "pkg" / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(code_repo, "add", "-A")
    git(code_repo, "commit", "-m", "Add package, certification profile and pytest config")
    contract = default_contract(
        ContractTask(
            name="Gate Scope Thing",
            repo_name="repo-a",
            coordination_root=root / "ar-coordination",
            workflow_kind="chat-task",
            memory_mode="internal",
        ),
        leaf=LeafIdentity(worktree_name="gate-scope-thing"),
        code=RepoBranchPlan(
            repo_path=code_repo,
            source_branch="main",
            work_branch="ar/gate-scope-thing",
            base_commit=git(code_repo, "rev-parse", "HEAD"),
        ),
    )
    parent = default_series_contract(
        ContractTask(
            name="Gate Scope Master",
            repo_name="repo-a",
            coordination_root=root / "ar-coordination",
            workflow_kind="chat-task",
            memory_mode="internal",
        ),
        code=RepoBranchPlan(
            repo_path=code_repo,
            source_branch="main",
            work_branch="main",
            base_commit=git(code_repo, "rev-parse", "HEAD"),
        ),
    )
    write_contract(parent.contract_path, parent)
    contract = replace(contract, parent_contract_path=parent.contract_path)
    git(
        code_repo,
        "worktree",
        "add",
        "-b",
        contract.code_work_branch,
        str(contract.code_worktree),
        "main",
    )
    write_contract(contract.contract_path, contract)
    return contract


class ScopeRecordingGate:
    """Run the repository profile's real scope derivation and first enforcing rail."""

    def __init__(self) -> None:
        self.lint_paths: list[str] = []

    def __call__(
        self,
        target: code_quality_gate.QualityGateTarget,
        *,
        diff_base: str = "",
        plan: code_quality_gate.QualityGatePlan | None = None,
    ) -> dict[str, object]:
        del plan
        worktree = target.code_worktree
        self.lint_paths = quality_check.posix_args(quality_check.derive_scope(worktree).lint_paths)
        completed = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-cache", *self.lint_paths],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "strict code-quality gate failed before code commit with exit code "
                f"{completed.returncode}.\nQuality output tail:\n{completed.stdout}"
            )
        result = publish_passing_quality_gate(target, diff_base=diff_base)
        result["command"] = "ruff"
        return result


class CloseoutGateSeesCreatedFilesTests(unittest.TestCase):
    def test_a_created_file_carrying_a_lint_error_fails_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = gate_scope_contract_fixture(Path(tmp))
            (contract.code_worktree / CREATED_FILE).write_text("import os\n", encoding="utf-8")
            write_passing_route_review(contract)
            gate = ScopeRecordingGate()

            with (
                mock.patch.object(
                    closeout_staged_quality, "run_strict_code_quality_gate", side_effect=gate
                ),
                self.assertRaises(RuntimeError) as caught,
            ):
                run_authorized_closeout_mechanics(closeout_args(contract))

            message = str(caught.exception)
            self.assertIn("strict code-quality gate failed before code commit", message)
            self.assertIn(CREATED_FILE, gate.lint_paths)
            self.assertIn(f"{CREATED_FILE}:1:", message)
            self.assertIn("F401", message)
            self.assertEqual(
                git(contract.code_worktree, "rev-parse", "HEAD"), contract.code_base_commit
            )
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")

    def test_a_refused_gate_commits_nothing_and_leaves_the_worktree_staged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = gate_scope_contract_fixture(Path(tmp))
            (contract.code_worktree / CREATED_FILE).write_text("import os\n", encoding="utf-8")
            (contract.code_worktree / "pkg" / "existing.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            write_passing_route_review(contract)

            with (
                mock.patch.object(
                    closeout_staged_quality,
                    "run_strict_code_quality_gate",
                    side_effect=ScopeRecordingGate(),
                ),
                self.assertRaises(RuntimeError),
            ):
                run_authorized_closeout_mechanics(closeout_args(contract))

            self.assertEqual(
                git(contract.code_worktree, "rev-parse", "HEAD"), contract.code_base_commit
            )
            self.assertEqual(load_contract(contract.contract_path).closeout_status, "not-started")
            staged = git(contract.code_worktree, "write-tree")
            self.assertIn(CREATED_FILE, git(contract.code_worktree, "ls-files"))
            git(contract.code_worktree, "add", "-A")
            self.assertEqual(git(contract.code_worktree, "write-tree"), staged)

    def test_the_gates_scope_is_the_commits_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = gate_scope_contract_fixture(Path(tmp))
            (contract.code_worktree / CREATED_FILE).write_text("VALUE = 2\n", encoding="utf-8")
            (contract.code_worktree / "pkg" / "existing.py").unlink()
            write_passing_route_review(contract)
            gate = ScopeRecordingGate()

            with (
                mock.patch.object(
                    closeout_staged_quality, "run_strict_code_quality_gate", side_effect=gate
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(run_authorized_closeout_mechanics(closeout_args(contract)), 0)

            committed = git(
                contract.code_worktree, "ls-tree", "-r", "--name-only", "HEAD"
            ).splitlines()
            self.assertIn(CREATED_FILE, committed)
            self.assertNotIn("pkg/existing.py", committed)
            self.assertEqual(
                sorted(gate.lint_paths),
                sorted(path for path in committed if path.endswith(".py")),
            )
