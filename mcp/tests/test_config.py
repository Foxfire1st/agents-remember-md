from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.kernel.primitives.runtime_config import (
    ConfigError,
    McpRuntimeConfig,
    load_config,
)
from agents_remember.providers.settings import lifecycle_settings_from_config
from test_worktree_support import init_repo


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def settings_payload(root: Path) -> dict:
    coordination_root = root / "ar-coordination"
    workspace_root = root / "workspace"
    return {
        "version": 1,
        "coordinationRoot": str(coordination_root),
        "workspaceRoot": str(workspace_root),
        "repositories": {"agents-remember": {}},
        "providers": {
            "codegraphcontext-code": {},
            "grepai-memory": {},
        },
        "timeoutCaps": {
            "toolSeconds": 30,
            "providerSetupSeconds": 1800,
        },
        "benchmarksEnabled": True,
    }


class McpConfigTests(unittest.TestCase):
    def test_two_repository_ids_cannot_share_one_git_common_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "workspace"
            physical = workspace / "repo-a"
            init_repo(physical, "main")
            (workspace / "repo-b").symlink_to(physical, target_is_directory=True)
            payload = settings_payload(root)
            payload["repositories"] = {"repo-a": {}, "repo-b": {}}
            path = root / "mcp-settings.json"
            write_json(path, payload)

            with self.assertRaisesRegex(ConfigError, "share Git common-dir"):
                load_config(path)

    def test_external_memory_cannot_alias_another_configured_code_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "workspace"
            init_repo(workspace / "repo-a", "main")
            other = workspace / "repo-b"
            init_repo(other, "main")
            memory = root / "ar-coordination" / "memory-repos" / "ar-repo-a"
            memory.parent.mkdir(parents=True)
            memory.symlink_to(other, target_is_directory=True)
            payload = settings_payload(root)
            payload["repositories"] = {"repo-a": {}, "repo-b": {}}
            path = root / "mcp-settings.json"
            write_json(path, payload)

            with self.assertRaisesRegex(ConfigError, "share Git common-dir"):
                load_config(path)

    def test_config_must_not_live_inside_coordination_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            path = root / "ar-coordination" / "mcp-settings.json"
            write_json(path, settings_payload(root))

            with self.assertRaisesRegex(ConfigError, "inside the coordinator root"):
                load_config(path)

    def test_loads_authority_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            path = root / ".codex" / "mcp" / "settings.json"
            write_json(path, settings_payload(root))

            config = load_config(path)

            self.assertEqual(config.allowed_repo_ids, ("agents-remember",))
            self.assertEqual(
                config.allowed_provider_ids,
                ("codegraphcontext-code", "grepai-memory"),
            )
            self.assertEqual(config.timeout_caps["toolSeconds"], 30)
            self.assertEqual(
                config.transcript_root,
                root / "ar-coordination" / "logs" / "mcp",
            )
            self.assertEqual(config.harness_skill_root, root / ".codex" / "skills")
            self.assertEqual(
                config.repositories["agents-remember"].path,
                root / "workspace" / "agents-remember",
            )
            memory_root = config.repositories["agents-remember"].memory_root
            assert memory_root is not None
            self.assertEqual(memory_root.name, "ar-agents-remember")
            self.assertEqual(config.providers["grepai-memory"].scope, "workspace")
            self.assertEqual(config.providers["grepai-memory"].instance_id, "workspace")
            self.assertEqual(
                config.providers["grepai-memory"].log_root,
                root
                / "ar-coordination"
                / "logs"
                / "providers"
                / "grepai"
                / config.providers["grepai-memory"].instance_id,
            )
            self.assertEqual(
                config.providers["codegraphcontext-code"].runtime_root,
                root
                / "ar-coordination"
                / "providers"
                / "runners"
                / "codegraphcontext"
                / config.providers["codegraphcontext-code"].instance_id,
            )

            lifecycle_settings = lifecycle_settings_from_config(config)
            providers = lifecycle_settings["contextProviders"]["providers"]
            grepai = providers["grepai-memory"]
            grepai_instance = config.providers["grepai-memory"].instance_id
            cgc_instance = config.providers["codegraphcontext-code"].instance_id
            self.assertEqual(
                grepai["runtimeRoot"],
                (
                    root / "ar-coordination" / "providers" / "runners" / "grepai" / grepai_instance
                ).as_posix(),
            )
            self.assertEqual(grepai["instance"]["id"], grepai_instance)
            self.assertEqual(grepai["instance"]["scope"], "workspace")
            self.assertEqual(
                grepai["instance"]["labels"]["agents-remember.instance-id"],
                grepai_instance,
            )
            self.assertEqual(
                grepai["instance"]["labels"]["agents-remember.provider"],
                "grepai-memory",
            )
            self.assertEqual(grepai["runtime"]["mode"], "docker")
            self.assertEqual(
                grepai["runtime"]["composeProject"],
                f"agents-remember-grepai-{grepai_instance}",
            )
            self.assertEqual(
                grepai["runtime"]["network"]["name"],
                f"ar-grepai-memory-{grepai_instance}",
            )
            self.assertEqual(grepai["runtime"]["runner"]["image"], "agents-remember/grepai:0.35.0")
            self.assertEqual(
                grepai["runtime"]["runner"]["containerName"],
                f"ar-grepai-watcher-{grepai_instance}",
            )
            self.assertEqual(
                grepai["backend"]["runtimeRoot"],
                (
                    root
                    / "ar-coordination"
                    / "providers"
                    / "data"
                    / "grepai"
                    / grepai_instance
                    / "postgres"
                ).as_posix(),
            )
            self.assertEqual(grepai["embedder"]["provider"], "ollama")
            self.assertEqual(grepai["embedder"]["backend"]["image"], "ollama/ollama:latest")
            self.assertEqual(
                grepai["embedder"]["backend"]["containerName"],
                f"ar-grepai-ollama-{grepai_instance}",
            )
            self.assertEqual(providers["codegraphcontext-code"]["instance"]["id"], cgc_instance)
            self.assertEqual(
                providers["codegraphcontext-code"]["instance"]["scope"],
                "workspace",
            )
            self.assertEqual(
                providers["codegraphcontext-code"]["runtime"]["composeProject"],
                f"agents-remember-cgc-{cgc_instance}",
            )
            self.assertEqual(
                providers["codegraphcontext-code"]["runtime"]["runner"]["containerNameTemplate"],
                f"ar-cgc-watcher-{cgc_instance}-<repoId>",
            )
            self.assertEqual(
                providers["codegraphcontext-code"]["backend"]["runtimeRoot"],
                (
                    root
                    / "ar-coordination"
                    / "providers"
                    / "data"
                    / "codegraphcontext"
                    / cgc_instance
                    / "falkordb"
                ).as_posix(),
            )
            self.assertEqual(
                providers["codegraphcontext-code"]["backend"]["network"]["name"],
                f"ar-cgc-code-{cgc_instance}",
            )

    def test_repository_certification_profile_reference_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            path = root / "mcp-settings.json"
            for invalid in (
                ["one.json", "two.json"],
                "../outside.json",
                "/absolute/profile.json",
                "C:/absolute/profile.json",
                "mcp\\profile.json",
                ".",
                "mcp/",
            ):
                payload["repositories"]["agents-remember"]["certificationProfile"] = invalid
                write_json(path, payload)
                with (
                    self.subTest(invalid=invalid),
                    self.assertRaisesRegex(ConfigError, "certificationProfile"),
                ):
                    load_config(path)

    def test_repository_contract_path_cannot_escape_coordination_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["repositories"]["agents-remember"]["contractPath"] = str(
                root / "outside" / "series-contract.md"
            )
            path = root / "mcp-settings.json"
            write_json(path, payload)

            with self.assertRaisesRegex(ConfigError, "contractPath must be inside"):
                load_config(path)


class OrchestrationSettingsTests(unittest.TestCase):
    """gateDelegation boot sourcing (260703-L13, GQ1).

    The key's home is the GLOBAL agentic settings file
    (``<coordinationRoot>/system/settings.json``), read once at boot through the
    kernel loader (boot-snapshot). An authority-file value is a one-cycle
    legacy fallback with a boot warning; every other ``orchestration.*`` key in
    the authority file fails loud naming the new home.
    """

    def _load(
        self,
        *,
        authority: object | None = None,
        global_orchestration: object | None = None,
    ) -> McpRuntimeConfig:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            if authority is not None:
                payload["orchestration"] = authority
            if global_orchestration is not None:
                global_path = root / "ar-coordination" / "system" / "settings.json"
                write_json(global_path, {"orchestration": global_orchestration})
            path = root / "mcp-settings.json"
            write_json(path, payload)
            return load_config(path)

    def test_human_pinned_kind_in_global_file_fails_boot(self) -> None:
        with self.assertRaisesRegex(ConfigError, "human-pinned"):
            self._load(
                global_orchestration={
                    "gateDelegation": {"kinds": {"push-approval": {"role": "manager"}}}
                }
            )


if __name__ == "__main__":
    unittest.main()
