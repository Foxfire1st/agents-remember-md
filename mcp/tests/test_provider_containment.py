"""Provider containment (260707-HFX-L1): unconfigured on disk ⇒ no launch, ever.

The 2026-07-07 WSL OOM proved two bypasses of the settings gate: the boot
snapshot (running servers never re-read the authority file) and benchmark
self-arming (the case manifest synthesized+persisted its own providers map).
These tests pin the containment layer that closes both, plus the aggregate
setup lock and the metrics feed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents_remember.application import worktree_tools
from agents_remember.kernel.primitives.runtime_config import (
    McpRuntimeConfig,
    ProviderScope,
    RepositoryScope,
)


def _armed_boot_config(tmp: Path, *, disk_providers: dict) -> McpRuntimeConfig:
    """A config whose BOOT SNAPSHOT is armed while the disk says ``disk_providers``."""
    authority = tmp / "authority.json"
    authority.write_text(json.dumps({"version": 1, "providers": disk_providers}), encoding="utf-8")
    coordination_root = tmp / "coord"
    workspace_root = tmp / "ws"
    return McpRuntimeConfig(
        config_path=authority,
        coordination_root=coordination_root,
        workspace_root=workspace_root,
        transcript_root=coordination_root / "logs" / "mcp",
        repositories={
            "repo": RepositoryScope(repo_id="repo", path=workspace_root / "repo"),
        },
        providers={
            "grepai-memory": ProviderScope(
                provider_id="grepai-memory",
                runtime_root=coordination_root / "providers" / "runners" / "grepai" / "i1",
                log_root=coordination_root / "logs" / "providers" / "grepai" / "i1",
                instance_id="i1",
            ),
        },
    )


class WorktreeStartVetoTests(unittest.TestCase):
    def test_stale_armed_snapshot_is_vetoed_by_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _armed_boot_config(Path(tmp), disk_providers={})
            captured: dict[str, object] = {}

            def fake_start(
                args: worktree_tools.git_worktree_manager.WorktreeArgs,
            ) -> worktree_tools.git_worktree_manager.WorktreeCommandResult:
                captured["provider_setup_config"] = args.provider_setup_config
                return worktree_tools.git_worktree_manager.WorktreeCommandResult(
                    returncode=0, payload={"state": "blocked"}
                )

            with mock.patch.object(worktree_tools.git_worktree_manager, "start_result", fake_start):
                result = worktree_tools.worktree_start_tool(
                    config,
                    worktree_tools.TaskIdentity(repo_id="repo", task_name="t", worktree_name="w"),
                )
        # The launch side-channel never materializes: no settings file, no setup config.
        self.assertIsNone(captured["provider_setup_config"])
        veto = result["providersAuthority"]
        self.assertEqual(veto["bootSnapshotProviders"], ["grepai-memory"])


if __name__ == "__main__":
    unittest.main()
