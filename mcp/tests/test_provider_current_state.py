from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(MCP_SRC))
sys.path.insert(0, str(MCP_TESTS))

from agents_remember.kernel.primitives.runtime_config import (
    load_config,
)
from agents_remember.providers import current_state
from agents_remember.providers import status as provider_status
from test_config import settings_payload, write_json


class ProviderCurrentStateTests(unittest.TestCase):
    def test_current_state_is_current_truth_not_setup_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "mcp-settings.json"
            write_json(config_path, settings_payload(root))
            config = load_config(config_path)
            status = ready_status_payload(root)

            written = current_state.write_current_provider_state(config, status)

            payload = written["state"]
            self.assertEqual(payload["state"], "ready")
            self.assertTrue(payload["ok"])
            self.assertNotIn("lastSetup", payload)
            self.assertEqual(
                written["path"],
                (
                    root
                    / "ar-coordination"
                    / "logs"
                    / "providers"
                    / "status"
                    / "workspace"
                    / "workspace"
                    / "current.json"
                ).as_posix(),
            )
            saved = json.loads(Path(written["path"]).read_text(encoding="utf-8"))
            memory_root = config.repositories["agents-remember"].memory_root
            assert memory_root is not None
            self.assertEqual(saved["providers"]["grepai-memory"]["watcherUp"], True)
            self.assertEqual(
                saved["providers"]["grepai-memory"]["targetRepos"],
                [
                    {
                        "repoId": "agents-remember",
                        "path": memory_root.as_posix(),
                    }
                ],
            )
            self.assertEqual(
                saved["providers"]["grepai-memory"]["resources"]["postgres"]["uptimeSeconds"],
                7200,
            )

    def test_current_state_reports_per_repo_cgc_degradation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "mcp-settings.json"
            write_json(config_path, settings_payload(root))
            config = load_config(config_path)
            status = ready_status_payload(root)
            cgc = next(
                result for result in status["results"] if result["provider"] == "codegraphcontext"
            )
            cgc["ok"] = False
            cgc["results"][0]["ok"] = False
            cgc["results"][0]["process"]["alive"] = False
            cgc["results"][0]["process"]["containerState"] = {
                "containerState": "exited",
                "running": False,
                "startedAt": None,
                "uptimeSeconds": None,
                "health": None,
            }

            payload = current_state.build_current_provider_state(config, status)

            self.assertEqual(payload["state"], "degraded")
            cgc_state = payload["providers"]["codegraphcontext-code"]
            self.assertEqual(cgc_state["state"], "degraded")
            repo_state = cgc_state["resources"]["watchers"]["agents-remember"]
            self.assertFalse(repo_state["watcherUp"])
            self.assertEqual(repo_state["containerState"], "exited")
            self.assertEqual(repo_state["indexingState"], "unknown")

    def test_provider_status_reports_restart_recovery_for_grepai_no_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "mcp-settings.json"
            write_json(config_path, settings_payload(root))
            config = load_config(config_path)
            status = ready_status_payload(root)
            grepai = next(result for result in status["results"] if result["provider"] == "grepai")
            grepai["watcher"]["workspaceStatus"] = {
                "returncode": 0,
                "stdout": "No workspaces configured.\n",
            }

            with mock.patch.object(
                provider_status,
                "_watchers_status",
                return_value=status,
            ):
                packet = provider_status.provider_status_packet(config)
                diagnostics = provider_status.provider_diagnostics_packet(config)

            recovery = packet["providers"]["recoveryActions"][0]
            self.assertEqual(recovery["provider"], "grepai-memory")
            self.assertIn("provider_watchers(action='restart')", recovery["recoveryAction"])
            self.assertEqual(diagnostics["recoveryActions"], packet["providers"]["recoveryActions"])

    def test_current_state_ignores_disabled_providers_for_aggregate_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "mcp-settings.json"
            write_json(config_path, settings_payload(root))
            config = load_config(config_path)
            status = ready_status_payload(root)
            status["enabled"]["grepai-memory"] = False
            status["results"] = [
                result for result in status["results"] if result["provider"] != "grepai"
            ]

            payload = current_state.build_current_provider_state(config, status)

            self.assertEqual(payload["state"], "ready")
            self.assertEqual(payload["providers"]["grepai-memory"]["state"], "disabled")
            self.assertEqual(
                payload["providers"]["grepai-memory"]["indexingState"],
                "disabled",
            )

    def test_restarting_watcher_is_not_ready(self) -> None:
        """A crash-looping container reports Running=true between restarts but
        cannot serve; readiness must not count it as a live watcher."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "mcp-settings.json"
            write_json(config_path, settings_payload(root))
            config = load_config(config_path)
            status = ready_status_payload(root)
            cgc = next(
                result for result in status["results"] if result["provider"] == "codegraphcontext"
            )
            cgc["results"][0]["process"]["containerState"]["containerState"] = "restarting"

            payload = current_state.build_current_provider_state(config, status)

            repo_state = payload["providers"]["codegraphcontext-code"]["resources"]["watchers"][
                "agents-remember"
            ]
            self.assertFalse(repo_state["watcherUp"])
            self.assertNotEqual(repo_state["state"], "ready")
            self.assertEqual(payload["providers"]["codegraphcontext-code"]["state"], "degraded")
            self.assertFalse(payload["ok"])


def ready_status_payload(root: Path) -> dict:
    return {
        "provider": "watchers",
        "action": "status",
        "ok": True,
        "partial": False,
        "settingsFile": (root / "provider-settings.json").as_posix(),
        "processNamespace": {"durableForDaemons": True, "warning": None},
        "enabled": {
            "grepai-memory": True,
            "codegraphcontext-code": True,
        },
        "results": [
            {
                "provider": "grepai",
                "action": "status",
                "ok": True,
                "runtimeRoot": (
                    root / "ar-coordination" / "providers" / "runners" / "grepai"
                ).as_posix(),
                "watcherRunning": True,
                "backend": container_payload("ar-grepai-postgres-workspace"),
                "embedder": container_payload("ar-grepai-ollama-workspace"),
                "watcher": {
                    **container_payload("ar-grepai-watcher-workspace"),
                    "workspaceStatus": {
                        "returncode": 0,
                        "stdout": "Workspaces (1):\n\n  agents-remember-memory-projects\n",
                    },
                },
            },
            {
                "provider": "codegraphcontext",
                "action": "status-all",
                "ok": True,
                "backend": container_payload("ar-cgc-falkordb-workspace"),
                "results": [
                    {
                        "provider": "codegraphcontext",
                        "action": "status",
                        "ok": True,
                        "repoId": "agents-remember",
                        "indexingState": "unknown",
                        "process": {
                            "alive": True,
                            "containerName": "ar-cgc-watcher-workspace-agents-remember",
                            "containerState": running_container_state(),
                        },
                    }
                ],
            },
        ],
    }


def container_payload(name: str) -> dict:
    return {
        "ok": True,
        "containerName": name,
        "image": "example:latest",
        "running": True,
        "containerState": running_container_state(),
    }


def running_container_state() -> dict:
    return {
        "containerState": "running",
        "running": True,
        "startedAt": "2026-05-28T09:30:00+00:00",
        "uptimeSeconds": 7200,
        "health": None,
    }


if __name__ == "__main__":
    unittest.main()
