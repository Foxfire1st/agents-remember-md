from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import agents_remember.tasks.store as task_store
from agents_remember.tasks import TaskDocument, read_task_doc, write_task_doc
from agents_remember.worktrees.modules.finalize import FinalizeArgs, finalize_result
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    default_contract,
    write_contract,
)
from test_worktree_support import commit_file, git, init_repo


def _payload(result: WorktreeCommandResult) -> dict[str, Any]:
    return cast("dict[str, Any]", result.payload)


class LifecycleFinalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _contract(
        self,
        *,
        landed: bool = True,
        cleanup: str = "completed",
        fixture_name: str = "finalize-thing",
        **over: object,
    ):
        code_repo = self.tmp / f"code-{fixture_name}"
        code_base = init_repo(code_repo, "main")
        git(code_repo, "checkout", "-b", "ar/task")
        code_commit = commit_file(code_repo, "feature.txt", "feature\n", "Add feature")
        git(code_repo, "checkout", "main")
        if landed:
            git(code_repo, "merge", "--ff-only", "ar/task")
        contract = default_contract(
            ContractTask(
                name=fixture_name,
                repo_name="repo-a",
                coordination_root=self.tmp / "ar-coordination",
                workflow_kind="light-task",
                memory_mode="disabled",
            ),
            leaf=LeafIdentity(worktree_name=fixture_name, leaf_id="14"),
            code=RepoBranchPlan(
                repo_path=code_repo,
                source_branch="main",
                work_branch="ar/task",
                base_commit=code_base,
            ),
        )
        values = {
            "human_review_status": "approved",
            "approved_for_commit": True,
            "closeout_status": "completed",
            "code_commit": code_commit,
            "integration_status": "completed",
            "integrated_code_commit": code_commit,
            "cleanup": cleanup,
            **over,
        }
        closed = replace(contract, **values)
        write_contract(closed.contract_path, closed)
        return closed

    def _docs(self, contract) -> tuple[Path, Path]:
        master = TaskDocument.model_validate(
            {
                "id": "master",
                "slug": "task",
                "title": "Master",
                "kind": "master",
                "status": "inProgress",
                "repo": "repo-a",
                "type": "Master",
                "createdAt": "2026-06-23T21:00",
                "subTasks": [
                    {
                        "number": "14",
                        "name": "Finalize Thing",
                        "file": "14_finalize.md",
                        "status": "inProgress",
                    }
                ],
            }
        )
        master_json, _master_md = write_task_doc(contract.task_root, master)
        leaf = TaskDocument.model_validate(
            {
                "id": "14",
                "slug": "14_finalize",
                "title": "Finalize Thing",
                "kind": "subTask",
                "status": "inProgress",
                "repo": "repo-a",
                "type": "Code",
                "createdAt": "2026-06-23T22:00",
                "master": "task.md",
            }
        )
        leaf_json, _leaf_md = write_task_doc(contract.task_root, leaf)
        return leaf_json, master_json

    def _set_leaf_steps(self, leaf_json: Path, steps: list[dict[str, Any]]) -> None:
        leaf = read_task_doc(leaf_json)
        data = leaf.model_dump(by_alias=True)
        data["steps"] = steps
        write_task_doc(leaf_json.parent, TaskDocument.model_validate(data))

    def test_finalized_updates_leaf_and_immediate_parent_row(self) -> None:
        contract = self._contract()
        leaf_json, master_json = self._docs(contract)

        result = finalize_result(
            FinalizeArgs(
                contract_path=contract.contract_path,
                task_doc_path=leaf_json,
                master_doc_path=master_json,
                subtask_number="14",
            )
        )

        self.assertEqual(result.returncode, 0)
        payload = _payload(result)
        self.assertEqual(payload["state"], "finalized")
        self.assertEqual(payload["cleanup"]["state"], "already-completed")
        self.assertEqual(read_task_doc(leaf_json).status, "Completed")
        master = read_task_doc(master_json)
        self.assertEqual(master.subTasks[0].status, "Completed")
        self.assertEqual(master.status, "inProgress")
        self.assertEqual(master.decisions[0].decision, "Finalize task lifecycle.")

    def test_second_document_publish_failure_rolls_back_leaf_and_parent(self) -> None:
        contract = self._contract()
        leaf_json, master_json = self._docs(contract)
        paths = (
            leaf_json,
            leaf_json.with_suffix(".md"),
            master_json,
            master_json.with_suffix(".md"),
        )
        before = {path: path.read_bytes() for path in paths}
        real_atomic_write = task_store.atomic_write_text
        call_count = 0

        def fail_on_parent_json(path: Path, text: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise OSError("injected parent publication failure")
            real_atomic_write(path, text)

        with (
            patch.object(task_store, "atomic_write_text", side_effect=fail_on_parent_json),
            self.assertRaisesRegex(OSError, "injected parent publication failure"),
        ):
            finalize_result(FinalizeArgs(contract_path=contract.contract_path))

        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(read_task_doc(leaf_json).status, "inProgress")
        self.assertEqual(read_task_doc(master_json).subTasks[0].status, "inProgress")


if __name__ == "__main__":
    unittest.main()
