"""Forcing tests for source-pair-scoped atomic-series activation authority."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.worktrees.activation.atomic_series_activation import (
    activation_waiting_reason,
    observe_atomic_series,
    publish_atomic_series_selection,
)
from agents_remember.worktrees.activation.atomic_series_activation_release import (
    release_atomic_series_selection,
)
from agents_remember.worktrees.modules.startup.start_contract import (
    MasterSeriesContractSpec,
    ensure_master_series_contract,
)
from agents_remember.worktrees.worktree_contract import (
    WorktreeContract,
    write_contract,
)
from test_worktree_support import git, init_repo

REPO = "repo-a"
NOW = "2026-08-26T00:00:00+00:00"
MASTER_A = TaskDocumentRef(repository=REPO, path="master-a/task.json")
MASTER_B = TaskDocumentRef(repository=REPO, path="master-b/task.json")


def _master(ref: TaskDocumentRef) -> TaskDocument:
    name = Path(ref.path).parent.name
    return TaskDocument.model_validate(
        {
            "id": name.upper(),
            "slug": name,
            "title": name,
            "kind": "master",
            "status": "inProgress",
            "repo": REPO,
            "createdAt": NOW,
            "subTasks": [],
        }
    )


class ActivationFixture:
    def __init__(self, root: Path) -> None:
        self.coord = root / "coordination"
        self.tasks = self.coord / "tasks" / REPO
        self.code = root / "code"
        init_repo(self.code, "main")
        git(self.code, "branch", "super", "main")
        for ref in (MASTER_A, MASTER_B):
            write_task_doc(self.tasks / Path(ref.path).parent, _master(ref))
        write_task_doc(
            self.tasks / "sprint",
            TaskDocument.model_validate(
                {
                    "id": "SPRINT",
                    "slug": "sprint",
                    "title": "Sprint",
                    "kind": "master",
                    "status": "inProgress",
                    "repo": REPO,
                    "createdAt": NOW,
                    "orchestrates": ["master-a", "master-b"],
                    "integrationBranch": "super",
                }
            ),
        )

    def contract(self, name: str) -> WorktreeContract:
        result = ensure_master_series_contract(
            MasterSeriesContractSpec(
                coordination_root=self.coord,
                repo_name=REPO,
                code_repo=self.code,
                memory_root=None,
                task_root=self.tasks / name,
                task_name=name,
                parent_task_name="sprint",
                protected_branch="super",
            )
        )
        assert isinstance(result, WorktreeContract)
        release_atomic_series_selection(result, timestamp=NOW)
        return result


class AtomicSeriesActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ActivationFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_selection_switches_logical_active_owner_without_retiring_work(self) -> None:
        contract_a = self.fixture.contract("master-a")
        contract_b = self.fixture.contract("master-b")

        reconciling_a = publish_atomic_series_selection(contract_a, "reconciling", timestamp=NOW)
        active_a = publish_atomic_series_selection(contract_a, "active", timestamp=NOW)
        selected_b = publish_atomic_series_selection(contract_b, "reconciling", timestamp=NOW)

        self.assertEqual(reconciling_a.state, "reconciling")
        self.assertEqual(active_a.state, "active")
        self.assertEqual(selected_b.selected_master, MASTER_B)
        self.assertEqual(
            activation_waiting_reason(selected_b, MASTER_A),
            f"atomic-series-paused-by: {MASTER_B.key}",
        )
        self.assertEqual(
            activation_waiting_reason(selected_b, MASTER_B), "atomic-series-reconciling"
        )
        self.assertTrue(contract_a.contract_path.is_file())
        self.assertTrue(contract_b.contract_path.is_file())

    def test_source_pairs_are_isolated(self) -> None:
        contract_a = self.fixture.contract("master-a")
        contract_b = self.fixture.contract("master-b")
        git(self.fixture.code, "branch", "other-super", "main")
        isolated_b = replace(contract_b, code_source_branch="other-super")
        write_contract(isolated_b.contract_path, isolated_b)

        selected_a = publish_atomic_series_selection(contract_a, "active", timestamp=NOW)
        observed_b = observe_atomic_series(isolated_b)

        self.assertEqual(selected_a.state, "active")
        self.assertEqual(observed_b.state, "vacant")
        self.assertNotEqual(selected_a.activation_path, observed_b.activation_path)
