from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents_remember.kernel.primitives.runtime_config import (
    McpRuntimeConfig,
    ProviderScope,
)
from agents_remember.observer.store import EventStore
from agents_remember.serving.projections.paths import observer_root
from agents_remember.serving.projections.projection_store import (
    project_and_write,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    WorktreeContract,
    default_contract,
    write_contract,
)
from test_observer_projection import FRESH, T0, _started


class SnapshotReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)

    def _config(self) -> McpRuntimeConfig:
        coord = (self.tmp / "coord").resolve()
        coord.mkdir(parents=True, exist_ok=True)
        return McpRuntimeConfig(
            config_path=coord / "mcp.settings.json",
            coordination_root=coord,
            workspace_root=(self.tmp / "ws").resolve(),
            transcript_root=coord / "logs",
            providers={
                "codegraphcontext-code": ProviderScope(
                    provider_id="codegraphcontext-code",
                    runtime_root=coord / "rt",
                    log_root=coord / "lg",
                    instance_id="projects",
                    scope="workspace",
                )
            },
        )

    @staticmethod
    def _write_addressable_contract(contract: WorktreeContract) -> None:
        write_contract(contract.contract_path, contract)
        publish_new_lifecycle_operation_location(
            contract,
            contract_text=contract.contract_path.read_text(encoding="utf-8"),
        )

    def _existence_contract(self, coord: Path):  # -> WorktreeContract
        contract = default_contract(
            ContractTask(
                name="Observe Lifecycle",
                repo_name="repo-a",
                coordination_root=coord,
                workflow_kind="light-task",
                memory_mode="external",
            ),
            leaf=LeafIdentity(worktree_name="observe", lifecycle_id="LC-1"),
            code=RepoBranchPlan(
                repo_path=coord / "repo-a",
                source_branch="main",
                work_branch="ar/observe",
                base_commit="0" * 40,
            ),
            memory=RepoBranchPlan(
                repo_path=coord / "repo-a-memory",
                source_branch="main",
                work_branch="ar/observe-memory",
                base_commit="1" * 40,
            ),
        )
        contract.contract_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_addressable_contract(contract)
        return contract

    def test_project_and_write_end_to_end(self) -> None:
        config = self._config()
        store = EventStore(observer_root(config))
        store.append(_started(lifecycle_id="LC1", ts=T0))
        proj = project_and_write(config, now=FRESH)
        self.assertEqual(proj.metrics.lifecycleCount, 1)
        self.assertTrue((observer_root(config) / "latest-state.json").exists())
