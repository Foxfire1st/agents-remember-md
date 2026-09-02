from __future__ import annotations

import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.kernel.primitives.identity import (
    provider_instance_id,
)
from agents_remember.kernel.primitives.runtime_config import (
    ConfigError,
    McpRuntimeConfig,
    load_config,
    require_config_path,
)
from agents_remember.providers.cgc.context.core import cgc_runner_image
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


class LifecycleSettingsDerivationTests(unittest.TestCase):
    def test_cgc_runner_image_matches_single_derivation(self) -> None:
        """Regression for GitHub #50: settings must not derive the runner image
        independently — that dropped the image layer revision, so upgrading
        hosts kept a cached guard-less image under the guard entrypoint."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "workspace" / "agents-remember").mkdir(parents=True)
            path = root / ".harness" / "mcp-settings.json"
            write_json(path, settings_payload(root))
            config = load_config(path)

            settings = lifecycle_settings_from_config(config)

        cgc = settings["contextProviders"]["providers"]["codegraphcontext-code"]
        generated = cgc["runtime"]["runner"]["image"]
        self.assertEqual(generated, cgc_runner_image())
        self.assertRegex(generated, r":[\d.]+-\w+$")  # version-layerrevision
        # L12: per-repo managed exclusions ride the generated roots so the materialized
        # .cgcignore excludes the committed dashboard bundle from watch/index work.
        ar_root = next(r for r in cgc["roots"] if r["repoId"] == "agents-remember")
        self.assertEqual(ar_root["cgcignorePatterns"], ["mcp/src/agents_remember/package_data/"])


class McpConfigTests(unittest.TestCase):
    def test_direct_execution_enabled_parses_and_rejects_non_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            path = root / "mcp-settings.json"
            payload = settings_payload(root)
            payload["directExecutionEnabled"] = True
            write_json(path, payload)
            self.assertTrue(load_config(path).direct_execution_enabled)
            payload["directExecutionEnabled"] = "yes"
            write_json(path, payload)
            with self.assertRaisesRegex(ConfigError, "directExecutionEnabled must be a boolean"):
                load_config(path)

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

    def test_config_path_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(ConfigError, "absolute"):
            require_config_path(Path("relative-settings.json"))

    def test_config_path_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing = Path(tmp_dir) / "missing-settings.json"

            with self.assertRaisesRegex(ConfigError, "does not exist"):
                require_config_path(missing)

    def test_coordinator_system_settings_is_not_mcp_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            path = root / "ar-coordination" / "system" / "settings.json"
            write_json(path, settings_payload(root))

            with self.assertRaisesRegex(ConfigError, "coordinator system/settings.json"):
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

    def test_provider_instance_id_can_be_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["providers"]["grepai-memory"]["instanceId"] = "bench-case-a"
            payload["providers"]["grepai-memory"]["scope"] = "benchmark"
            path = root / ".codex" / "mcp" / "settings.json"
            write_json(path, payload)

            config = load_config(path)

            provider = config.providers["grepai-memory"]
            self.assertEqual(provider.instance_id, "bench-case-a")
            self.assertEqual(provider.scope, "benchmark")
            self.assertEqual(
                provider.runtime_root,
                root / "ar-coordination" / "providers" / "runners" / "grepai" / "bench-case-a",
            )

    def test_default_provider_instance_ids_are_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace_root = root / "Projects"
            coordination_root = workspace_root / "ar-coordination"
            worktree_runtime = (
                coordination_root
                / "worktrees"
                / "agents-remember"
                / "provider-task"
                / "provider-runtime"
            )

            self.assertEqual(provider_instance_id("workspace", workspace_root), "projects")
            self.assertEqual(
                provider_instance_id("worktree", worktree_runtime),
                "projects-provider-task",
            )
            self.assertEqual(
                provider_instance_id("benchmark", workspace_root),
                "projects-benchmark",
            )

    def test_repository_memory_root_prefers_internal_ar_memory_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo_root = root / "workspace" / "agents-remember"
            internal_memory = repo_root / "ar-memory"
            internal_memory.mkdir(parents=True)
            path = root / ".codex" / "mcp" / "settings.json"
            write_json(path, settings_payload(root))

            config = load_config(path)

            self.assertEqual(config.repositories["agents-remember"].memory_root, internal_memory)
            grepai_roots = lifecycle_settings_from_config(config)["contextProviders"]["providers"][
                "grepai-memory"
            ]["roots"]
            self.assertEqual(
                grepai_roots,
                [{"projectId": "agents-remember", "path": internal_memory.as_posix()}],
            )

    def test_harness_skill_root_is_none_without_registration_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            path = root / "mcp-settings.json"
            write_json(path, settings_payload(root))

            config = load_config(path)

            self.assertIsNone(config.harness_skill_root)

    def test_harness_skill_root_override_wins_over_inference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["harnessSkillRoot"] = str(root / "custom" / "skills")
            path = root / ".codex" / "mcp" / "settings.json"
            write_json(path, payload)

            config = load_config(path)

            self.assertEqual(config.harness_skill_root, root / "custom" / "skills")

    def test_loads_repository_contract_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["repositories"]["agents-remember"]["contractPath"] = (
                "tasks/agents-remember/task/enclosures/leaf/series-contract.md"
            )
            path = root / "mcp-settings.json"
            write_json(path, payload)

            config = load_config(path)

            contract_path = config.repositories["agents-remember"].contract_path
            assert contract_path is not None
            self.assertEqual(contract_path.name, "series-contract.md")

    def test_loads_explicit_repository_certification_profile_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["repositories"]["agents-remember"]["certificationProfile"] = (
                "mcp/certification-profile-v1.json"
            )
            path = root / "mcp-settings.json"
            write_json(path, payload)

            config = load_config(path)

            self.assertEqual(
                config.repositories["agents-remember"].certification_profile,
                Path("mcp/certification-profile-v1.json"),
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

    def test_legacy_memory_settings_includes_key_is_tolerated_and_ignored(self) -> None:
        """The dead memorySettingsIncludes plumbing was removed with 260703-L13.

        Existing settings files may still carry the key; its absence from the
        parser must be harmless -- the entry is ignored like any other unknown
        repository field and the parsed scope no longer exposes it.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["repositories"]["agents-remember"]["memorySettingsIncludes"] = [
                str(root / "anywhere" / "settings.json")
            ]
            path = root / "mcp-settings.json"
            write_json(path, payload)

            config = load_config(path)

            scope = config.repositories["agents-remember"]
            self.assertFalse(hasattr(scope, "memory_settings_includes"))

    def test_harness_skill_root_must_be_absolute_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["harnessSkillRoot"] = ".codex/skills"
            path = root / "mcp-settings.json"
            write_json(path, payload)

            with self.assertRaisesRegex(ConfigError, "harnessSkillRoot.*absolute"):
                load_config(path)

    def test_provider_settings_reject_derived_path_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["providers"]["grepai-memory"]["runnerRoot"] = str(
                root / "provider-runners" / "grepai"
            )
            payload["providers"]["grepai-memory"]["logRoot"] = str(
                root / "ar-coordination" / "providers" / "logs" / "grepai"
            )
            path = root / "mcp-settings.json"
            write_json(path, payload)

            with self.assertRaisesRegex(ConfigError, "derived by the server"):
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

    def test_timeout_caps_reject_boolean_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["timeoutCaps"]["toolSeconds"] = True
            path = root / "mcp-settings.json"
            write_json(path, payload)

            with self.assertRaisesRegex(ConfigError, "non-negative integer"):
                load_config(path)

    def test_timeout_caps_reject_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["timeoutCaps"]["bogusSeconds"] = 120
            path = root / "mcp-settings.json"
            write_json(path, payload)

            with self.assertRaisesRegex(ConfigError, "unsupported timeout cap"):
                load_config(path)

    def test_provider_setup_seconds_zero_means_unlimited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            payload["timeoutCaps"]["providerSetupSeconds"] = 0
            path = root / "mcp-settings.json"
            write_json(path, payload)

            config = load_config(path)

            self.assertEqual(config.timeout_caps["providerSetupSeconds"], 0)

    def test_legacy_provider_seconds_is_rejected_with_rename_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            del payload["timeoutCaps"]["providerSetupSeconds"]
            payload["timeoutCaps"]["providerSeconds"] = 120
            path = root / "mcp-settings.json"
            write_json(path, payload)

            with self.assertRaisesRegex(ConfigError, "renamed to providerSetupSeconds"):
                load_config(path)


class DashboardSettingsTests(unittest.TestCase):
    def _load(self, dashboard: object | None) -> McpRuntimeConfig:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            if dashboard is not None:
                payload["dashboard"] = dashboard
            path = root / "mcp-settings.json"
            write_json(path, payload)
            return load_config(path)

    def test_defaults_stay_off_when_the_key_is_absent(self) -> None:
        config = self._load(None)
        self.assertFalse(config.dashboard.auto_start)
        self.assertEqual(config.dashboard.port, 8765)

    def test_parses_auto_start_and_port(self) -> None:
        config = self._load({"autoStart": True, "port": 9321})
        self.assertTrue(config.dashboard.auto_start)
        self.assertEqual(config.dashboard.port, 9321)

    def test_unknown_dashboard_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unsupported dashboard setting"):
            self._load({"autostart": True})

    def test_auto_start_must_be_a_boolean(self) -> None:
        with self.assertRaisesRegex(ConfigError, "autoStart must be a boolean"):
            self._load({"autoStart": 1})

    def test_port_must_be_a_valid_port_number(self) -> None:
        for bad in (True, 0, 65536, "8080"):
            with self.assertRaisesRegex(ConfigError, "dashboard.port"):
                self._load({"port": bad})

    def test_non_object_dashboard_settings_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "dashboard settings must be an object"):
            self._load(["autoStart"])


class ProviderDegradationSettingsTests(unittest.TestCase):
    def _load(self, degradation: object | None) -> McpRuntimeConfig:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            if degradation is not None:
                payload["providerDegradation"] = degradation
            path = root / "mcp-settings.json"
            write_json(path, payload)
            return load_config(path)

    def test_defaults_to_enabled_failsafe_thresholds(self) -> None:
        config = self._load(None)

        self.assertTrue(config.provider_degradation.enabled)
        self.assertTrue(config.provider_degradation.fail_safe_enabled)
        self.assertEqual(config.provider_degradation.memory_degraded_ratio, 0.80)
        self.assertEqual(config.provider_degradation.memory_critical_ratio, 0.92)

    def test_parses_explicit_thresholds(self) -> None:
        config = self._load(
            {
                "enabled": False,
                "failSafeEnabled": False,
                "memoryDegradedRatio": 0.70,
                "memoryCriticalRatio": 0.95,
                "degradedSamples": 4,
                "criticalSamples": 3,
                "healthySamples": 5,
                "watcherLagDegradedCommits": 7,
                "watcherLagCriticalCommits": 30,
                "watcherLagDegradedMinutes": 11,
                "watcherLagCriticalMinutes": 45,
                "probeDegradedMs": 3000,
                "probeCriticalMs": 12000,
                "setupFailureDegradedStreak": 3,
                "setupFailureCriticalStreak": 4,
                "recentSampleLimit": 240,
            }
        )

        settings = config.provider_degradation
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.fail_safe_enabled)
        self.assertEqual(settings.memory_degraded_ratio, 0.70)
        self.assertEqual(settings.memory_critical_ratio, 0.95)
        self.assertEqual(settings.degraded_samples, 4)
        self.assertEqual(settings.critical_samples, 3)
        self.assertEqual(settings.healthy_samples, 5)
        self.assertEqual(settings.watcher_lag_degraded_commits, 7)
        self.assertEqual(settings.watcher_lag_critical_commits, 30)
        self.assertEqual(settings.watcher_lag_degraded_minutes, 11)
        self.assertEqual(settings.watcher_lag_critical_minutes, 45)
        self.assertEqual(settings.probe_degraded_ms, 3000)
        self.assertEqual(settings.probe_critical_ms, 12000)
        self.assertEqual(settings.setup_failure_degraded_streak, 3)
        self.assertEqual(settings.setup_failure_critical_streak, 4)
        self.assertEqual(settings.recent_sample_limit, 240)

    def test_unknown_provider_degradation_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "memoryDegradedRato"):
            self._load({"memoryDegradedRato": 0.75})

    def test_provider_degradation_shape_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "providerDegradation settings"):
            self._load(["enabled"])

    def test_provider_degradation_values_are_typed(self) -> None:
        bad_cases = (
            {"enabled": 1},
            {"memoryCriticalRatio": 1.2},
            {"criticalSamples": 0},
            {"recentSampleLimit": True},
        )
        for case in bad_cases:
            with self.assertRaises(ConfigError):
                self._load(case)


class RetirementSettingsTests(unittest.TestCase):
    """Completion cleanup edge gates plus the default-on ARG-L1 close behavior."""

    def _load(self, retirement: object | None) -> McpRuntimeConfig:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            if retirement is not None:
                payload["retirement"] = retirement
            path = root / "mcp-settings.json"
            write_json(path, payload)
            return load_config(path)

    def test_defaults_are_both_on_when_the_key_is_absent(self) -> None:
        config = self._load(None)
        self.assertTrue(config.retirement.auto_land_on_integration)
        self.assertTrue(config.retirement.auto_land_on_finalize)
        self.assertTrue(config.retirement.auto_close_completed_seats)

    def test_parses_both_flags_explicitly_off(self) -> None:
        config = self._load({"autoLandOnIntegration": False, "autoLandOnFinalize": False})
        self.assertFalse(config.retirement.auto_land_on_integration)
        self.assertFalse(config.retirement.auto_land_on_finalize)
        self.assertTrue(config.retirement.auto_close_completed_seats)

    def test_auto_close_can_restore_the_previous_landed_behavior(self) -> None:
        config = self._load({"autoCloseCompletedSeats": False})
        self.assertFalse(config.retirement.auto_close_completed_seats)
        self.assertTrue(config.retirement.auto_land_on_integration)
        self.assertTrue(config.retirement.auto_land_on_finalize)

    def test_parses_legacy_auto_retire_flags_as_aliases(self) -> None:
        config = self._load({"autoRetireOnIntegration": False, "autoRetireOnFinalize": False})
        self.assertFalse(config.retirement.auto_land_on_integration)
        self.assertFalse(config.retirement.auto_land_on_finalize)

    def test_unknown_retirement_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unsupported retirement setting"):
            self._load({"autoRetireOnLaunch": True})

    def test_auto_land_on_integration_must_be_a_boolean(self) -> None:
        with self.assertRaisesRegex(ConfigError, "autoLandOnIntegration must be a boolean"):
            self._load({"autoLandOnIntegration": "yes"})

    def test_auto_land_on_finalize_must_be_a_boolean(self) -> None:
        with self.assertRaisesRegex(ConfigError, "autoLandOnFinalize must be a boolean"):
            self._load({"autoLandOnFinalize": 1})

    def test_auto_close_completed_seats_must_be_a_boolean(self) -> None:
        with self.assertRaisesRegex(ConfigError, "autoCloseCompletedSeats must be a boolean"):
            self._load({"autoCloseCompletedSeats": "yes"})

    def test_legacy_auto_retire_alias_must_be_a_boolean(self) -> None:
        with self.assertRaisesRegex(ConfigError, "autoRetireOnIntegration must be a boolean"):
            self._load({"autoRetireOnIntegration": "yes"})

    def test_non_object_retirement_settings_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "retirement settings must be an object"):
            self._load(["autoLandOnIntegration"])


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

    def test_defaults_to_all_human_gate_policy(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            config = self._load()

        closeout = config.orchestration.gate_policy.rule_for("closeout-approval")
        self.assertIsNone(closeout.delegated_role)
        self.assertEqual(caught, [])

    def test_global_agentic_file_sources_gate_delegation(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            config = self._load(
                global_orchestration={"gateDelegation": {"policy": "manager-decides-leaf-gates"}}
            )

        policy = config.orchestration.gate_policy
        self.assertEqual(policy.rule_for("plan-approval").delegated_role, "manager")
        self.assertEqual(policy.rule_for("closeout-approval").delegated_role, "manager")
        self.assertIsNone(policy.rule_for("push-approval").delegated_role)
        self.assertEqual(caught, [])

    def test_authority_gate_delegation_is_legacy_fallback_with_warning(self) -> None:
        with self.assertWarnsRegex(UserWarning, "moved to the global agentic settings file"):
            config = self._load(
                authority={"gateDelegation": {"policy": "manager-decides-leaf-gates"}}
            )

        policy = config.orchestration.gate_policy
        self.assertEqual(policy.rule_for("plan-approval").delegated_role, "manager")

    def test_global_file_shadows_authority_value_with_warning(self) -> None:
        with self.assertWarnsRegex(UserWarning, "is IGNORED"):
            config = self._load(
                authority={"gateDelegation": {"policy": "manager-decides-leaf-gates"}},
                global_orchestration={"gateDelegation": {"policy": "all-human"}},
            )

        # The global (all-human) value wins over the shadowed authority value.
        self.assertIsNone(config.orchestration.gate_policy.rule_for("plan-approval").delegated_role)

    def test_loops_in_authority_file_is_rejected_naming_the_new_home(self) -> None:
        with self.assertRaisesRegex(
            ConfigError, r"unsupported orchestration setting\(s\): loops.*system.settings\.json"
        ):
            self._load(authority={"loops": {"defaults": {"maxRounds": 3}}})

    def test_roles_and_concurrency_in_authority_file_are_rejected(self) -> None:
        """Previously reserved-and-silently-dropped; the L13 move closes the trap."""
        with self.assertRaisesRegex(
            ConfigError, r"unsupported orchestration setting\(s\): concurrency, roles"
        ):
            self._load(
                authority={
                    "roles": {"worker": {"harness": "codex"}},
                    "concurrency": {"maxParallelLeaves": 3},
                }
            )

    def test_custom_delegated_kind_can_require_reviewer_verdict(self) -> None:
        config = self._load(
            global_orchestration={
                "gateDelegation": {
                    "kinds": {
                        "closeout-approval": {
                            "role": "manager",
                            "requireReviewerVerdict": True,
                        }
                    }
                }
            }
        )

        closeout = config.orchestration.gate_policy.rule_for("closeout-approval")
        self.assertEqual(closeout.delegated_role, "manager")
        self.assertTrue(closeout.require_reviewer_verdict)

    def test_at_seams_flag_binds_delegated_seam_rules_through_parse(self) -> None:
        config = self._load(
            global_orchestration={
                "gateDelegation": {
                    "policy": "manager-decides-leaf-gates",
                    "requireReviewerVerdictAtSeams": True,
                }
            }
        )

        policy = config.orchestration.gate_policy
        handover = policy.rule_for("master-handover-approval")
        self.assertEqual(handover.delegated_role, "orchestrator")
        self.assertTrue(handover.require_reviewer_verdict)
        self.assertFalse(policy.rule_for("plan-approval").require_reviewer_verdict)
        self.assertTrue(config.orchestration.require_reviewer_verdict_at_seams)

    def test_human_pinned_kind_in_global_file_fails_boot(self) -> None:
        with self.assertRaisesRegex(ConfigError, "human-pinned"):
            self._load(
                global_orchestration={
                    "gateDelegation": {"kinds": {"push-approval": {"role": "manager"}}}
                }
            )

    def test_human_pinned_kind_in_legacy_authority_fallback_still_fails(self) -> None:
        with self.assertRaisesRegex(ConfigError, "human-pinned"):
            self._load(
                authority={"gateDelegation": {"kinds": {"push-approval": {"role": "manager"}}}}
            )

    def test_malformed_global_agentic_file_fails_boot_naming_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = settings_payload(root)
            global_path = root / "ar-coordination" / "system" / "settings.json"
            global_path.parent.mkdir(parents=True)
            global_path.write_text("{not json", encoding="utf-8")
            path = root / "mcp-settings.json"
            write_json(path, payload)

            with self.assertRaisesRegex(
                ConfigError, r"cannot parse agentic settings JSON.*settings\.json"
            ):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
