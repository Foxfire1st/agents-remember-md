from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.install import provider_watchers as install_provider_watchers
from agents_remember.install import runtime as install_runtime


def write_file(path: Path, content: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_runtime_source(root: Path) -> Path:
    source_root = root / "source"
    runtime_root = source_root / "runtime"
    write_file(source_root / "mcp" / "src" / "agents_remember" / "mcp" / "__init__.py")
    write_file(source_root / "mcp" / "requirements.txt", "mcp==1.12.4\n")
    for source_rel in install_runtime.AGENTS_MD_TARGETS:
        write_file(runtime_root / source_rel)
    write_file(runtime_root / "skills" / "U-01-core-skills" / "C-04" / "SKILL.md")
    write_file(runtime_root / "providers" / "requirements" / "codegraphcontext.txt")
    write_file(runtime_root / "providers" / "requirements" / "grepai.txt")
    write_file(runtime_root / "providers" / "patches" / "codegraphcontext" / "patch.diff")
    return source_root


def enabled_provider_settings(*provider_ids: str) -> dict:
    return {
        "contextProviders": {
            "enabled": True,
            "providers": {provider_id: {"enabled": True} for provider_id in provider_ids},
        }
    }


def watcher_result(action: str, *, ok: bool = True, partial: bool = False) -> dict:
    return {
        "provider": "watchers",
        "action": action,
        "ok": ok,
        "partial": partial,
        "recoveryActions": [],
    }


class InstallRuntimeTests(unittest.TestCase):
    def test_runtime_install_preserves_docker_provider_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_root = create_runtime_source(root)
            coordination_root = root / "ar-coordination"
            providers_root = coordination_root / "providers"
            cgc_exe = providers_root / "_venvs" / "codegraphcontext" / "Scripts" / "cgc.exe"
            cgc_state = (
                providers_root
                / "runners"
                / "codegraphcontext"
                / "repo"
                / ".codegraphcontext"
                / "state.json"
            )
            grepai_watch_log = (
                providers_root / "runners" / "grepai" / "memory-repos" / "logs" / "watch.log"
            )
            cgc_data = providers_root / "data" / "codegraphcontext" / "graph.db"
            cgc_log = providers_root / "logs" / "codegraphcontext" / "watch.log"
            central_cgc_log = (
                coordination_root / "logs" / "providers" / "codegraphcontext" / "watch.log"
            )
            old_script = coordination_root / "scripts" / "install-skills.sh"

            write_file(providers_root / "_bin" / "grepai.exe", "legacy grepai\n")
            write_file(cgc_exe, "live cgc\n")
            write_file(cgc_state)
            write_file(grepai_watch_log)
            write_file(cgc_data, "live graph\n")
            write_file(cgc_log, "live log\n")
            write_file(central_cgc_log, "live central log\n")
            write_file(providers_root / "old.txt")
            write_file(old_script)

            summary = install_runtime.install_runtime(
                source_root,
                coordination_root,
                dry_run=False,
                provider_deps=install_runtime.ProviderDependencyInstall(
                    settings={}, timeout=1800, enabled=False
                ),
            )

            self.assertEqual(summary.dependency_runs, 0)
            self.assertFalse((providers_root / "_bin" / "grepai.exe").exists())
            self.assertFalse(cgc_exe.exists())
            self.assertTrue(cgc_state.exists())
            self.assertTrue(grepai_watch_log.exists())
            self.assertTrue(cgc_data.exists())
            self.assertFalse(cgc_log.exists())
            self.assertTrue(central_cgc_log.exists())
            self.assertFalse((providers_root / "old.txt").exists())
            self.assertTrue((providers_root / "requirements" / "grepai.txt").exists())
            self.assertFalse((coordination_root / "scripts").exists())

    def test_runtime_install_provider_dependency_failure_attempts_watcher_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_root = create_runtime_source(root)
            coordination_root = root / "ar-coordination"
            events: list[str] = []

            def fake_watchers(args, action):
                events.append(f"watchers:{action}")
                return watcher_result(action)

            def fake_grepai_install(args):
                events.append("grepai:install")
                return {"ok": False, "provider": "grepai", "error": "boom"}

            with (
                patch.object(
                    install_provider_watchers.lifecycle,
                    "watchers_run",
                    side_effect=fake_watchers,
                ),
                patch.object(
                    install_runtime.lifecycle,
                    "grepai_install",
                    side_effect=fake_grepai_install,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "attempted non-destructive watcher recovery",
                ),
            ):
                install_runtime.install_runtime(
                    source_root,
                    coordination_root,
                    dry_run=False,
                    provider_deps=install_runtime.ProviderDependencyInstall(
                        settings=enabled_provider_settings("grepai-memory"), timeout=1800
                    ),
                )

            self.assertEqual(
                events,
                [
                    "watchers:stop",
                    "grepai:install",
                    "watchers:start",
                    "watchers:status",
                ],
            )


class AgenticSettingsSeedTests(unittest.TestCase):
    """runtime_install seeds the GLOBAL agentic settings file copy-if-missing (260703-L13)."""

    def test_existing_settings_file_is_never_clobbered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_root = create_runtime_source(root)
            coordination_root = root / "ar-coordination"
            settings_path = coordination_root / "system" / "settings.json"
            developer_content = '{"orchestration": {"spawn": {"harness": "codex"}}}\n'
            write_file(settings_path, developer_content)

            install_runtime.install_runtime(
                source_root,
                coordination_root,
                dry_run=False,
                provider_deps=install_runtime.ProviderDependencyInstall(
                    settings={}, timeout=1800, enabled=False
                ),
            )

            self.assertEqual(settings_path.read_text(encoding="utf-8"), developer_content)

    def test_dry_run_counts_the_seed_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_root = create_runtime_source(root)
            coordination_root = root / "ar-coordination"

            summary = install_runtime.install_runtime(
                source_root,
                coordination_root,
                dry_run=True,
                provider_deps=install_runtime.ProviderDependencyInstall(
                    settings={}, timeout=1800, enabled=False
                ),
            )

            self.assertFalse((coordination_root / "system" / "settings.json").exists())
            self.assertGreater(summary.copied_files, 0)


if __name__ == "__main__":
    unittest.main()
