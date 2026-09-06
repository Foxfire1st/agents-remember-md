"""Transitive source-line enforcement from canonical task identity."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.worktree import SourceLineageProjection
from agents_remember.tasks import TaskDocument, read_task_doc, write_task_doc
from agents_remember.worktrees.activation.atomic_series_activation import observe_atomic_series
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.modules import start as start_module
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.git import repository_identity
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.modules.start import _preflighted_contract, attach_result
from agents_remember.worktrees.source_lineage import (
    lineage_block_payload,
    lineage_refusal,
    parent_source_lineage,
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
    def test_repository_identity_rejects_absent_and_non_git_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(repository_identity(None))
            self.assertIsNone(repository_identity(root / "absent"))
            plain = root / "plain"
            plain.mkdir()
            self.assertIsNone(repository_identity(plain))

    def test_sprint_roles_have_no_single_master_lineage_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))

            projection = source_lineage_for_task(
                fixture.coordination,
                TaskDocumentRef(repository="repo", path="sprint/task.json"),
            )

            self.assertIsNone(projection)

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

    def test_organizational_master_and_leaf_use_the_direct_super_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _organizational_fixture(Path(tmp), external_memory=True)

            master_projection = source_lineage_for_task(
                fixture.coordination,
                TaskDocumentRef(repository="repo", path="master/task.json"),
            )
            leaf_projection = source_lineage_for_task(fixture.coordination, fixture.leaf_ref)

            assert master_projection is not None
            assert leaf_projection is not None
            self.assertEqual(master_projection.state, "current")
            self.assertEqual(master_projection.edges, [])
            self.assertEqual(leaf_projection.state, "current")
            self.assertEqual(
                [(edge.relation, edge.side) for edge in leaf_projection.edges],
                [("super-to-leaf", "code"), ("super-to-leaf", "memory")],
            )
            self.assertEqual(parent_source_lineage(fixture.leaf_contract).state, "current")  # type: ignore[union-attr]

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

    def test_start_requires_exact_code_and_memory_source_tip_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _organizational_fixture(root, external_memory=True)

            _commit_on(fixture.code_repo, "super", "code-forward.txt")
            forward = parent_source_lineage(fixture.leaf_contract)
            assert forward is not None
            self.assertEqual(forward.state, "blocked")
            self.assertEqual(
                [(edge.side, edge.state) for edge in forward.edges],
                [("code", "behind"), ("memory", "current")],
            )

            rewound = _organizational_fixture(root / "rewound", external_memory=True)
            memory_repo = rewound.leaf_contract.memory_repo_path
            assert memory_repo is not None
            old_memory = _git(memory_repo, "rev-parse", "super")
            _commit_on(memory_repo, "super", "memory-new.txt")
            new_memory = _git(memory_repo, "rev-parse", "super")
            rewound_contract = replace(rewound.leaf_contract, memory_base_commit=new_memory)
            write_contract(rewound_contract.contract_path, rewound_contract)
            _git(memory_repo, "reset", "--hard", old_memory)

            projection = parent_source_lineage(rewound_contract)
            assert projection is not None
            self.assertEqual(projection.state, "blocked")
            self.assertEqual(
                [(edge.side, edge.state) for edge in projection.edges],
                [("code", "current"), ("memory", "diverged")],
            )

    def test_atomic_start_requires_exact_code_and_memory_source_tip_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp), external_memory=True)
            memory_repo = fixture.leaf_contract.memory_repo_path
            assert memory_repo is not None
            old_memory = _git(memory_repo, "rev-parse", "ar/master")
            _commit_on(fixture.code_repo, "ar/master", "atomic-code-forward.txt")
            _commit_on(memory_repo, "ar/master", "atomic-memory-new.txt")
            new_memory = _git(memory_repo, "rev-parse", "ar/master")
            contract = replace(fixture.leaf_contract, memory_base_commit=new_memory)
            write_contract(contract.contract_path, contract)
            _git(memory_repo, "reset", "--hard", old_memory)
            contract_before = contract.contract_path.read_bytes()

            projection = parent_source_lineage(contract)
            assert projection is not None
            self.assertEqual(projection.state, "blocked")
            self.assertEqual(
                [
                    (edge.relation, edge.side, edge.state)
                    for edge in projection.edges
                    if edge.state != "current"
                ],
                [
                    ("master-to-leaf", "code", "behind"),
                    ("master-to-leaf", "memory", "diverged"),
                ],
            )

            result = _preflighted_contract(
                SimpleNamespace(code_repository_name="repo"),
                contract,
                WorktreeArgs(dry_run=True, stale_base_choice="proceed-stale"),
            )

            self.assertIsInstance(result, WorktreeCommandResult)
            self.assertFalse(contract.code_worktree.exists())
            assert contract.memory_worktree is not None
            self.assertFalse(contract.memory_worktree.exists())
            self.assertEqual(contract.contract_path.read_bytes(), contract_before)

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

    def test_super_move_blocks_before_leaf_start_even_with_stale_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            _commit_on(fixture.code_repo, "super", "super.txt")

            result = _preflighted_contract(
                SimpleNamespace(code_repository_name="repo"),
                fixture.leaf_contract,
                WorktreeArgs(dry_run=True, stale_base_choice="proceed-stale"),
            )

            self.assertIsInstance(result, WorktreeCommandResult)
            result = cast("WorktreeCommandResult", result)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.payload["nextOperation"], "sync_source_lineage")
            lineage = SourceLineageProjection.model_validate(result.payload["source_lineage"])
            self.assertEqual(lineage.state, "blocked")
            self.assertEqual(lineage.edges[0].relation, "super-to-master")
            next_args = cast("dict[str, object]", result.payload["nextArgs"])
            self.assertEqual(
                next_args["contract_path"],
                fixture.master_contract.contract_path.as_posix(),
            )

    def test_master_move_blocks_leaf_dispatch_with_leaf_sync_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            _commit_on(fixture.code_repo, "ar/master", "master.txt")

            projection = source_lineage_for_task(fixture.coordination, fixture.leaf_ref)

            assert projection is not None
            self.assertEqual(projection.state, "blocked")
            stale = [edge for edge in projection.edges if edge.state != "current"]
            self.assertEqual(
                [(edge.relation, edge.side) for edge in stale], [("master-to-leaf", "code")]
            )
            self.assertEqual(
                projection.recoveries[0].contractPath,
                fixture.leaf_contract.contract_path.as_posix(),
            )

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

    def test_missing_leaf_contract_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            fixture.leaf_contract.contract_path.unlink()

            projection = source_lineage_for_task(fixture.coordination, fixture.leaf_ref)

            assert projection is not None
            self.assertEqual(projection.state, "unavailable")
            self.assertEqual(projection.edges[0].relation, "master-to-leaf")
            refusal = lineage_refusal(projection)
            assert refusal is not None
            self.assertEqual(refusal[0], "source-lineage-unavailable")

    def test_missing_master_contract_names_the_super_to_master_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            fixture.master_contract.contract_path.unlink()

            projection = source_lineage_for_task(
                fixture.coordination,
                TaskDocumentRef(repository="repo", path="master/task.json"),
            )

            assert projection is not None
            self.assertEqual(projection.state, "unavailable")
            self.assertEqual(projection.edges[0].relation, "super-to-master")

    def test_malformed_master_contract_fails_closed_for_task_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            fixture.master_contract.contract_path.write_text("not a contract\n", encoding="utf-8")

            projection = source_lineage_for_task(
                fixture.coordination,
                TaskDocumentRef(repository="repo", path="master/task.json"),
            )

            assert projection is not None
            self.assertEqual(projection.state, "unavailable")

    def test_parent_only_preflight_fails_closed_for_every_missing_parent_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            self.assertIsNone(parent_source_lineage(fixture.master_contract))

            no_parent = replace(fixture.leaf_contract, parent_contract_path=None)
            self.assertEqual(parent_source_lineage(no_parent).state, "unavailable")  # type: ignore[union-attr]

            absent = replace(
                fixture.leaf_contract,
                parent_contract_path=fixture.coordination / "absent-series-contract.md",
            )
            self.assertEqual(parent_source_lineage(absent).state, "unavailable")  # type: ignore[union-attr]

            fixture.master_contract.contract_path.write_text("invalid\n", encoding="utf-8")
            self.assertEqual(
                parent_source_lineage(fixture.leaf_contract).state,  # type: ignore[union-attr]
                "unavailable",
            )

    def test_contract_resolution_fails_closed_for_non_task_and_missing_parent_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            other = replace(fixture.leaf_contract, kind="other")
            self.assertIsNone(source_lineage_for_contract(other))

            no_parent = replace(fixture.leaf_contract, parent_contract_path=None)
            self.assertEqual(source_lineage_for_contract(no_parent).state, "unavailable")  # type: ignore[union-attr]

            fixture.master_contract.contract_path.write_text("invalid\n", encoding="utf-8")
            self.assertEqual(
                source_lineage_for_contract(fixture.leaf_contract).state,  # type: ignore[union-attr]
                "unavailable",
            )

    def test_unavailable_lineage_has_no_sync_recovery_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            projection = source_lineage_for_contract(
                replace(fixture.master_contract, code_repo_path=Path(tmp) / "absent")
            )

            assert projection is not None
            payload = lineage_block_payload(projection)
            self.assertNotIn("nextTool", payload)

    def test_contract_branch_mismatch_and_git_failures_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            cases: tuple[tuple[str, Any], ...] = (
                (
                    "leaf-parent-branch-mismatch",
                    replace(fixture.leaf_contract, code_source_branch="not-master"),
                ),
                (
                    "repository-absent",
                    replace(fixture.master_contract, code_repo_path=Path(tmp) / "absent"),
                ),
                ("branch-name-absent", replace(fixture.master_contract, code_source_branch="")),
                (
                    "branch-ref-absent",
                    replace(fixture.master_contract, code_work_branch="not-a-ref"),
                ),
            )
            for name, contract in cases:
                with self.subTest(name=name):
                    projection = source_lineage_for_contract(contract)
                    assert projection is not None
                    self.assertEqual(projection.state, "unavailable")

            with mock.patch(
                "agents_remember.worktrees.source_lineage.ahead_behind", return_value=None
            ):
                projection = source_lineage_for_contract(fixture.master_contract)
            assert projection is not None
            self.assertEqual(projection.state, "unavailable")

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

    def test_diverged_master_reports_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture(Path(tmp))
            _commit_on(fixture.code_repo, "ar/master", "master.txt")
            _commit_on(fixture.code_repo, "super", "super.txt")

            projection = source_lineage_for_contract(fixture.master_contract)

            assert projection is not None
            self.assertEqual(projection.state, "blocked")
            self.assertEqual(projection.edges[0].state, "diverged")
            self.assertEqual((projection.edges[0].ahead, projection.edges[0].behind), (1, 1))

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
