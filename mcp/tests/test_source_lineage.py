"""Transitive source-line enforcement from canonical task identity."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

import pytest

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import TaskDocument, read_task_doc, write_task_doc
from agents_remember.worktrees.activation.atomic_series_activation import observe_atomic_series
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.modules import start as start_module
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.start import _preflighted_contract, attach_result
from agents_remember.worktrees.source_lineage import (
    lineage_refusal,
    require_current_source_lineage,
    source_lineage_for_contract,
    source_lineage_for_task,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    contract_publication_text,
    default_contract,
    default_series_contract,
    write_contract,
)
from repository_profile_test_support import install_fixture_profile


class SourceLineageTests(unittest.TestCase):
    def test_leaf_identity_proves_code_and_memory_transitively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp), external_memory=True)

            projection = source_lineage_for_task(fixture.coordination, fixture.leaf_ref)

            assert projection is not None
            self.assertEqual(projection.state, "current")
            self.assertEqual(
                [(edge.relation, edge.side) for edge in projection.edges],
                [
                    ("super-to-master", "code"),
                    ("super-to-master", "memory"),
                    ("master-to-leaf", "code"),
                    ("master-to-leaf", "memory"),
                ],
            )
            self.assertIsNone(lineage_refusal(projection))

    def test_organizational_super_move_blocks_the_leaf_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _organizational_fixture(Path(tmp))
            _commit_on(fixture.code_repo, "super", "super.txt")

            projection = source_lineage_for_contract(fixture.leaf_contract)

            assert projection is not None
            self.assertEqual(projection.state, "blocked")
            self.assertEqual(projection.edges[0].relation, "super-to-leaf")
            with self.assertRaisesRegex(
                RuntimeError,
                "closeout requires current transitive source lineage",
            ):
                require_current_source_lineage(fixture.leaf_contract, operation="closeout")

    def test_start_rechecks_exact_source_tips_before_start_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _organizational_fixture(Path(tmp), external_memory=True)
            contract = fixture.leaf_contract
            args = WorktreeArgs(dry_run=False, stale_base_choice="proceed-stale")
            context = SimpleNamespace(
                code_repository_name="repo",
                coordination_root=fixture.coordination,
            )
            before_contract = contract.contract_path.read_bytes()
            self.assertFalse(contract.code_worktree.exists())
            assert contract.memory_worktree is not None
            self.assertFalse(contract.memory_worktree.exists())
            self.assertIs(
                _preflighted_contract(context, contract, args),
                contract,
            )

            _commit_on(fixture.code_repo, "super", "raced-super.txt")

            with (
                mock.patch.object(start_module, "_record_start_progress"),
                mock.patch.object(start_module, "_record_start_block"),
                mock.patch.object(start_module, "ensure_worktree") as ensure,
            ):
                result = start_module._create_start_enclosure(context, contract, args)

            self.assertEqual((result.returncode, result.payload["state"]), (2, "blocked"))
            ensure.assert_not_called()
            self.assertFalse(contract.code_worktree.exists())
            self.assertFalse(contract.memory_worktree.exists())
            self.assertEqual(contract.contract_path.read_bytes(), before_contract)

    @pytest.mark.integration
    def test_attach_refuses_before_stale_task_context_is_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            _commit_on(fixture.code_repo, "super", "super.txt")

            result = attach_result(WorktreeArgs(contract_path=fixture.leaf_contract.contract_path))

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.payload["state"], "blocked")
            self.assertIn(
                "Attach refused before stale task context",
                cast(str, result.payload["summary"]),
            )
            self.assertEqual(observe_atomic_series(fixture.master_contract).state, "active")

    def test_parent_and_leaf_paths_may_be_sibling_worktrees_of_one_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _fixture(root)
            parent_checkout = root / "parent-checkout"
            _git(
                fixture.code_repo,
                "worktree",
                "add",
                "--detach",
                str(parent_checkout),
                "super",
            )
            parent = replace(fixture.master_contract, code_repo_path=parent_checkout)
            write_contract(parent.contract_path, parent)

            projection = source_lineage_for_contract(fixture.leaf_contract)

            assert projection is not None
            self.assertEqual(projection.state, "current")
            self.assertTrue(all(edge.state == "current" for edge in projection.edges))

    def test_lifecycle_boundary_requires_the_full_transitive_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            self.assertIsNotNone(
                require_current_source_lineage(fixture.leaf_contract, operation="closeout")
            )
            _commit_on(fixture.code_repo, "super", "super.txt")

            with self.assertRaisesRegex(
                RuntimeError,
                "closeout requires current transitive source lineage.*worktree_sync",
            ):
                require_current_source_lineage(fixture.leaf_contract, operation="closeout")


class _Fixture:
    def __init__(
        self,
        coordination: Path,
        code_repo: Path,
        master_contract,
        leaf_contract,
        leaf_ref: TaskDocumentRef,
    ) -> None:
        self.coordination = coordination
        self.code_repo = code_repo
        self.master_contract = master_contract
        self.leaf_contract = leaf_contract
        self.leaf_ref = leaf_ref


def _fixture(
    root: Path,
    *,
    external_memory: bool = False,
    publish_locations: bool = True,
    selected_profile: bool = False,
) -> _Fixture:
    coordination = root / "coordination"
    code_repo = _repo(root / "code", selected_profile=selected_profile)
    memory_repo = _repo(root / "memory") if external_memory else None
    task_root = coordination / "tasks" / "repo" / "master"
    _write_task_tree(coordination)
    memory_mode = "external" if external_memory else "disabled"
    memory_plan = (
        RepoBranchPlan(
            memory_repo,
            "super",
            "ar/master",
            _git(memory_repo, "rev-parse", "super"),
        )
        if memory_repo is not None
        else None
    )
    master = default_series_contract(
        ContractTask("master", "repo", coordination, "light-task", memory_mode),
        code=RepoBranchPlan(
            code_repo,
            "super",
            "ar/master",
            _git(code_repo, "rev-parse", "super"),
        ),
        memory=memory_plan,
        task_root=task_root,
    )
    write_contract(master.contract_path, master)
    if publish_locations:
        publish_new_lifecycle_operation_location(
            master,
            contract_text=contract_publication_text(master.contract_path, master),
        )
    leaf_memory = (
        RepoBranchPlan(
            memory_repo,
            "ar/master",
            "leaf",
            _git(memory_repo, "rev-parse", "ar/master"),
        )
        if memory_repo is not None
        else None
    )
    leaf = default_contract(
        ContractTask(
            "master",
            "repo",
            coordination,
            "light-task",
            memory_mode,
            parent_task_name=master.task_name,
            parent_contract_path=master.contract_path,
        ),
        leaf=LeafIdentity("leaf", leaf_id="leaf-1"),
        code=RepoBranchPlan(
            code_repo,
            "ar/master",
            "leaf",
            _git(code_repo, "rev-parse", "ar/master"),
        ),
        memory=leaf_memory,
    )
    write_contract(leaf.contract_path, leaf)
    if publish_locations:
        publish_new_lifecycle_operation_location(
            leaf,
            contract_text=contract_publication_text(leaf.contract_path, leaf),
        )
    return _Fixture(
        coordination,
        code_repo,
        master,
        leaf,
        TaskDocumentRef(repository="repo", path="master/leaf-1.json"),
    )


def _organizational_fixture(root: Path, *, external_memory: bool = False) -> _Fixture:
    fixture = _fixture(
        root,
        external_memory=external_memory,
        publish_locations=False,
    )
    master_path = fixture.coordination / "tasks" / "repo" / "master"
    master_doc = read_task_doc(master_path / "task.json")
    write_task_doc(
        master_path,
        master_doc.model_copy(update={"executionNature": "organizational"}),
    )
    fixture.master_contract.contract_path.unlink()
    leaf = replace(
        fixture.leaf_contract,
        parent_contract_path=None,
        code_source_branch="super",
        code_base_commit=_git(fixture.code_repo, "rev-parse", "super"),
        memory_source_branch="super" if external_memory else "",
        memory_base_commit=(
            _git(fixture.leaf_contract.memory_repo_path, "rev-parse", "super")
            if fixture.leaf_contract.memory_repo_path is not None
            else ""
        ),
    )
    write_contract(leaf.contract_path, leaf)
    publish_new_lifecycle_operation_location(
        leaf,
        contract_text=contract_publication_text(leaf.contract_path, leaf),
    )
    fixture.leaf_contract = leaf
    return fixture


def _write_task_tree(coordination: Path) -> None:
    task_root = coordination / "tasks" / "repo"
    write_task_doc(
        task_root / "sprint",
        _doc(
            id="SPRINT",
            slug="sprint",
            title="Sprint",
            kind="master",
            orchestrates=["master"],
            integrationBranch="super",
            executionGraph={
                "nodes": [{"repository": "repo", "path": "master/task.json"}],
                "edges": [],
            },
        ),
    )
    write_task_doc(
        task_root / "master",
        _doc(
            id="MASTER",
            slug="master",
            title="Master",
            kind="master",
            executionNature="atomic",
            subTasks=[
                {"number": "leaf-1", "name": "Leaf", "file": "leaf-1.md", "status": "inProgress"}
            ],
        ),
    )
    write_task_doc(
        task_root / "master",
        _doc(id="leaf-1", slug="leaf-1", title="Leaf", kind="subTask", master="task.md"),
    )


def _doc(**values: object) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "repo": "repo",
            "createdAt": "2026-08-12T00:00",
            **values,
        }
    )


def _repo(path: Path, *, selected_profile: bool = False) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "super")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "base.txt")
    if selected_profile:
        profile = install_fixture_profile(path, "repo")
        _git(path, "add", profile.relative_to(path).as_posix())
    _git(path, "commit", "-m", "base")
    _git(path, "branch", "main", "super")
    _git(path, "branch", "ar/master")
    _git(path, "branch", "leaf", "ar/master")
    _git(path, "update-ref", "refs/remotes/origin/main", _git(path, "rev-parse", "main"))
    _git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return path


def _commit_on(repo: Path, branch: str, name: str) -> None:
    _git(repo, "switch", branch)
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
