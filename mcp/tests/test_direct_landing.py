"""Wave 2 (L16-R6/R7/R8/R9): branch-addressed direct execution and error dialect.

Covers the policy-gated series-contract binding for ``record_route_review``
(R6), the lock-serialized ``direct_landing`` operation with its pre-commit staged-
candidate gate (R7/R8), and the contract-bound refusal dialect (R9). Uses real
scratch git repos and a synthetic coordination root -- never the live tree.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents_remember.kernel.memory_ledger import create_initial_ledger, write_ledger
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig, load_config
from agents_remember.models.direct_landing import DirectLandingResponse
from agents_remember.worktrees.direct_landing import (
    DirectLandingError,
    DirectLandingRequest,
)
from agents_remember.worktrees.direct_landing import (
    direct_landing as _production_direct_landing,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.modules.git import (
    head_commit,
    require_git,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    RepoBranchPlan,
    default_series_contract,
    write_contract,
)
from closeout_input_test_support import ensure_fixture_waiting_door
from test_worktree_support import git, init_repo


def direct_landing(*args, **kwargs):
    """Exercise direct landing below the independently covered scheduling fence."""

    with mock.patch("agents_remember.worktrees.direct_landing.require_first_ready_generation"):
        return _production_direct_landing(*args, **kwargs)


def _scratch_config(
    root: Path,
    code: Path,
    memory: Path | None,
    *,
    direct_execution_enabled: bool = True,
) -> McpRuntimeConfig:
    configured_code = root / "repo-a"
    if not configured_code.exists():
        configured_code.symlink_to(code, target_is_directory=True)
    if memory is not None:
        configured_memory = root / "coord" / "memory-repos" / "ar-repo-a"
        configured_memory.parent.mkdir(parents=True, exist_ok=True)
        if not configured_memory.exists():
            configured_memory.symlink_to(memory, target_is_directory=True)
    config_path = root / (
        "settings.json" if direct_execution_enabled else "settings-direct-disabled.json"
    )
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "coordinationRoot": (root / "coord").as_posix(),
                "workspaceRoot": root.as_posix(),
                "repositories": {"repo-a": {}},
                "directExecutionEnabled": direct_execution_enabled,
            }
        ),
        encoding="utf-8",
    )
    return load_config(config_path)


def _series_fixture(root: Path, *, code_commit_message: str = "code commit") -> dict:
    """A task-root series contract over a real code + memory repo pair."""
    coord = root / "coord"
    tasks = coord / "tasks" / "repo-a" / "direct-task"
    tasks.mkdir(parents=True)
    code = root / "code"
    memory = coord / "memory-repos" / "ar-repo-a"
    code_base = init_repo(code, "main")
    git(code, "checkout", "-b", "ar/direct-task", "main")
    (code / "feature.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git(code, "add", "-A")
    git(code, "commit", "-m", code_commit_message)
    code_head = git(code, "rev-parse", "HEAD")
    git(code, "checkout", "main")
    git(code, "branch", "super", "main")

    init_repo(memory, "main")
    git(memory, "checkout", "-b", "ar/direct-task", "main")
    write_ledger(
        memory / "memory.md",
        create_initial_ledger("repo-a", code_base, head_commit(memory)),
    )
    git(memory, "add", "memory.md")
    git(memory, "commit", "-m", "seed ledger")
    memory_base = head_commit(memory)

    task = ContractTask(
        name="direct-task",
        repo_name="repo-a",
        coordination_root=coord,
        workflow_kind="light-task",
        memory_mode="external",
    )
    contract = default_series_contract(
        task,
        code=RepoBranchPlan(
            repo_path=code,
            source_branch="super",
            work_branch="ar/direct-task",
            base_commit=code_base,
        ),
        memory=RepoBranchPlan(
            repo_path=memory,
            source_branch="main",
            work_branch="ar/direct-task",
            base_commit=memory_base,
        ),
    )
    write_contract(contract.contract_path, contract)
    contract, _fixture_bypass = ensure_fixture_waiting_door(contract)
    publish_new_lifecycle_operation_location(
        contract,
        contract_text=contract.contract_path.read_text(encoding="utf-8"),
    )
    return {
        "config": _scratch_config(root, code, memory),
        "contract": contract,
        "code": code,
        "memory": memory,
        "code_head": code_head,
        "candidate_tree": require_git(code, ["rev-parse", f"{code_head}^{{tree}}"]),
        "tasks": tasks,
    }


def _byte_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class DirectLandingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_direct_landing_verifies_code_commit_then_ledger(self) -> None:
        root = Path(self.temp.name)
        fixture = _series_fixture(root / "fx")
        config = fixture["config"]
        contract = fixture["contract"]
        memory = fixture["memory"]

        # A mismatch between the requested commit and the series branch HEAD refuses.
        with (
            mock.patch(
                "agents_remember.worktrees.direct_landing.require_git",
                side_effect=AssertionError("foreign commit must not be dereferenced"),
            ) as tree_read,
            self.assertRaisesRegex(DirectLandingError, "not the current series branch HEAD"),
        ):
            direct_landing(
                config,
                DirectLandingRequest(
                    contract_path=contract.contract_path.as_posix(),
                    code_commit="0" * 40,
                    candidate_tree=fixture["candidate_tree"],
                    memory_commit_message="direct memory content",
                    ledger_commit_message="direct ledger mapping",
                    intent_note="approve",
                ),
                contract,
            )
        tree_read.assert_not_called()

        # Preview reports the would-land facts without mutating.
        before_preview = _byte_tree(root)
        preview = direct_landing(
            config,
            DirectLandingRequest(
                contract_path=contract.contract_path.as_posix(),
                code_commit=fixture["code_head"],
                candidate_tree=fixture["candidate_tree"],
                memory_commit_message="direct memory content",
                ledger_commit_message="direct ledger mapping",
                intent_note="approve",
                dry_run=True,
            ),
            contract,
        )
        self.assertEqual(preview["state"], "would-land")
        self.assertEqual(DirectLandingResponse.model_validate(preview).state, "would-land")
        self.assertEqual(preview["codeCommit"], fixture["code_head"])
        self.assertEqual(_byte_tree(root), before_preview)
        before = git(memory, "rev-parse", "HEAD")

        # Add dirty memory content, then land: code verified + memory + ledger row.
        (memory / "onboarding").mkdir(exist_ok=True)
        (memory / "onboarding" / "feature.py.md").write_text("# feature\n", encoding="utf-8")
        landed = direct_landing(
            config,
            DirectLandingRequest(
                contract_path=contract.contract_path.as_posix(),
                code_commit=fixture["code_head"],
                candidate_tree=fixture["candidate_tree"],
                memory_commit_message="direct memory",
                ledger_commit_message="direct ledger",
                intent_note="approved by owner",
            ),
            contract,
        )
        self.assertEqual(landed["state"], "landed")
        self.assertEqual(DirectLandingResponse.model_validate(landed).state, "landed")
        self.assertEqual(landed["codeCommit"], fixture["code_head"])
        self.assertTrue(landed["memoryContentCommit"])
        self.assertTrue(landed["ledgerCommit"])
        after = git(memory, "rev-parse", "HEAD")
        self.assertNotEqual(before, after)
        self.assertEqual(git(memory, "show", "-s", "--format=%s", after), "direct ledger")
        self.assertEqual(
            git(memory, "show", "-s", "--format=%s", str(landed["memoryContentCommit"])),
            "direct memory",
        )
        ledger_text = git(memory, "show", f"{after}:memory.md")
        self.assertIn(fixture["code_head"], ledger_text)
        self.assertIn(landed["memoryContentCommit"], ledger_text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
