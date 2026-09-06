from __future__ import annotations

import tempfile
from pathlib import Path

from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.worktrees import git_worktree_manager as worktree_manager
from agents_remember.worktrees.task_resolver import (
    leaf_enclosure_path,
    series_contract_path,
)
from agents_remember.worktrees.worktree_contract import (
    load_contract,
)
from test_worktree_support import (
    WorktreeSupportTests,
    git,
    init_repo,
)


class WorktreeSupport1(WorktreeSupportTests):
    def test_master_start_and_abandon_preserve_parent_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code_repo = workspace / "repo-a"
            init_repo(code_repo, "main")
            git(code_repo, "branch", "super", "main")
            coordination_root = workspace / "ar-coordination"
            (coordination_root / "memory-repos" / "ar-repo-a" / "system").mkdir(parents=True)
            (coordination_root / "memory-repos" / "ar-repo-a" / "onboarding").mkdir()
            task_root = coordination_root / "tasks" / "repo-a" / "260624_master"
            write_task_doc(
                coordination_root / "tasks" / "repo-a" / "260624_sprint",
                TaskDocument.model_validate(
                    {
                        "id": "sprint",
                        "slug": "task",
                        "title": "Sprint",
                        "kind": "master",
                        "status": "inProgress",
                        "repo": "repo-a",
                        "createdAt": "2026-06-24T01:00",
                        "orchestrates": ["260624_master"],
                        "integrationBranch": "super",
                        "executionGraph": {
                            "nodes": [
                                {
                                    "repository": "repo-a",
                                    "path": "260624_master/task.json",
                                }
                            ],
                            "edges": [],
                        },
                    }
                ),
            )
            write_task_doc(
                task_root,
                TaskDocument.model_validate(
                    {
                        "id": "master",
                        "slug": "task",
                        "title": "Master Series",
                        "kind": "master",
                        "status": "inProgress",
                        "repo": "repo-a",
                        "createdAt": "2026-06-24T02:00",
                        "executionNature": "atomic",
                        "subTasks": [
                            {
                                "number": "15",
                                "name": "Leaf task",
                                "file": "15_leaf.md",
                                "status": "inProgress",
                            }
                        ],
                    }
                ),
            )
            write_task_doc(
                task_root,
                TaskDocument.model_validate(
                    {
                        "id": "15",
                        "slug": "15_leaf",
                        "title": "Leaf task",
                        "kind": "subTask",
                        "status": "inProgress",
                        "repo": "repo-a",
                        "createdAt": "2026-06-24T02:01",
                        "master": "task.md",
                    }
                ),
            )

            result = worktree_manager.start_result(
                worktree_manager.WorktreeArgs(
                    code_repository_name="repo-a",
                    workspace_root=workspace,
                    coordination_root=coordination_root,
                    code_repository_root=code_repo,
                    topology="external",
                    task_name="260624_master",
                    worktree_name="15_leaf",
                    leaf_id="15_leaf",
                    workflow_kind="light-task",
                    memory_mode="disabled",
                    skip_provider_setup=True,
                    lifecycle_id="LC-LEAF",
                )
            )

            self.assertEqual(result.returncode, 0)
            root_contract = load_contract(series_contract_path(task_root))
            leaf_contract = load_contract(leaf_enclosure_path(task_root, "15"))
            self.assertEqual(
                (root_contract.kind, root_contract.code_source_branch), ("series", "super")
            )
            self.assertEqual(root_contract.code_work_branch, "ar/260624_master")
            self.assertEqual(root_contract.code_worktree, code_repo)
            self.assertEqual((leaf_contract.kind, leaf_contract.leaf_id), ("leaf", "15"))
            self.assertEqual(leaf_contract.code_source_branch, "ar/260624_master")
            self.assertEqual(leaf_contract.code_work_branch, "ar/15_leaf")
            self.assertEqual(leaf_contract.parent_contract_path, root_contract.contract_path)
            self.assertEqual(
                result.payload["enclosure_path"], leaf_contract.contract_path.as_posix()
            )
            self.assertIn(
                "ar/260624_master", git(code_repo, "branch", "--list", "ar/260624_master")
            )
            self.assertIn("ar/15_leaf", git(code_repo, "branch", "--list", "ar/15_leaf"))

            abandoned = worktree_manager.abandon_result(
                worktree_manager.WorktreeArgs(
                    contract_path=leaf_contract.contract_path,
                    approved=True,
                    teardown_providers=False,
                )
            )
            self.assertEqual(abandoned.returncode, 0, abandoned.payload)
            self.assertEqual(abandoned.payload["state"], "abandoned")
            self.assertFalse(leaf_contract.code_worktree.exists())
            self.assertEqual(git(code_repo, "branch", "--list", "ar/15_leaf"), "")
            self.assertTrue(root_contract.contract_path.exists())
            self.assertIn(
                "ar/260624_master", git(code_repo, "branch", "--list", "ar/260624_master")
            )
