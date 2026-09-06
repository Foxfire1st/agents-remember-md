from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(MCP_SRC))
sys.path.insert(0, str(MCP_TESTS))

from agents_remember.application.context_packet import (
    ContextPacketError,
    ContextPacketRequest,
    build_context_packet,
)
from agents_remember.kernel.memory_ledger import create_initial_ledger, write_ledger
from agents_remember.kernel.primitives.runtime_config import (
    load_config,
)
from test_config import settings_payload, write_json


class ContextPacketTests(unittest.TestCase):
    def test_builds_packet_from_allowed_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            initialize_context_fixture(root)
            config = write_and_load_config(root)

            packet = build_context_packet(
                config,
                ContextPacketRequest(repo_id="agents-remember"),
            )

            self.assertTrue(packet["ok"])
            self.assertEqual(packet["operation"], "context_packet")
            self.assertEqual(packet["contextPacketVersion"], 2)
            self.assertEqual(packet["repo"]["state"], "available")
            self.assertEqual(packet["repo"]["id"], "agents-remember")
            self.assertTrue(packet["repo"]["head"])
            self.assertFalse(packet["repo"]["dirty"])
            self.assertNotIn("repoId", packet)
            self.assertNotIn("storage", packet)
            self.assertNotIn("pathRules", packet)
            self.assertNotIn("crossRepo", packet)
            self.assertEqual(
                packet["paths"]["taskRoot"],
                (root / "ar-coordination" / "tasks" / "agents-remember").as_posix(),
            )
            self.assertEqual(packet["memory"]["mode"], "external")
            self.assertIn("pathRules", packet["memory"]["storage"])
            self.assertIn("crossRepo", packet["memory"])
            self.assertEqual(packet["worktree"], {"state": "inactive"})
            self.assertEqual(packet["providers"]["state"], "failed")
            self.assertTrue(Path(packet["providers"]["currentStateFile"]).exists())
            self.assertEqual(packet["providers"]["diagnosticsTool"], "provider_diagnostics")
            self.assertNotIn("currentState", packet["providers"])
            self.assertNotIn("rawStatus", packet["providers"])
            self.assertNotIn("rawStatus", packet["providers"]["items"][0])
            self.assertEqual(packet["drift"], {"status": "notChecked"})

    def test_rejects_unknown_repo_before_filesystem_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = write_and_load_config(root)

            with self.assertRaisesRegex(ContextPacketError, "not allowed"):
                build_context_packet(config, ContextPacketRequest(repo_id="other-repo"))

    def test_freshness_reports_behind_code_branch_and_ledger_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            initialize_context_fixture(root)
            repo = root / "workspace" / "agents-remember"
            bare = root / "origin.git"
            run_git(repo, ["clone", "--bare", str(repo), str(bare)])
            run_git(repo, ["remote", "add", "origin", str(bare)])
            branch = current_branch(repo)
            run_git(repo, ["fetch", "origin"])
            run_git(repo, ["branch", f"--set-upstream-to=origin/{branch}", branch])
            other = root / "other"
            run_git(repo, ["clone", str(bare), str(other)])
            run_git(other, ["config", "user.email", "agents-remember@example.invalid"])
            run_git(other, ["config", "user.name", "Agents Remember"])
            (other / "new.txt").write_text("new\n", encoding="utf-8")
            run_git(other, ["add", "new.txt"])
            run_git(other, ["commit", "-m", "remote change"])
            run_git(other, ["push", "origin", "HEAD"])
            memory = root / "ar-coordination" / "memory-repos" / "ar-agents-remember"
            write_ledger(
                memory / "memory.md",
                create_initial_ledger("agents-remember", current_head(repo), "0" * 40),
            )
            config = write_and_load_config(root)

            packet = build_context_packet(
                config,
                ContextPacketRequest(repo_id="agents-remember", include_freshness=True),
            )

            freshness = packet["freshness"]
            self.assertEqual(freshness["code"]["state"], "behind")
            self.assertEqual(freshness["code"]["behind"], 1)
            self.assertTrue(freshness["ledgerMapsCodeHead"])


def write_and_load_config(root: Path):
    config_path = root / "mcp-settings.json"
    write_json(config_path, settings_payload(root))
    return load_config(config_path)


def initialize_context_fixture(root: Path) -> None:
    repo = root / "workspace" / "agents-remember"
    memory = root / "ar-coordination" / "memory-repos" / "ar-agents-remember"
    (memory / "system").mkdir(parents=True, exist_ok=True)
    (memory / "onboarding").mkdir(parents=True, exist_ok=True)
    (memory / "system" / "settings.md").write_text("# Settings\n", encoding="utf-8")
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, ["init"])
    run_git(repo, ["config", "user.email", "agents-remember@example.invalid"])
    run_git(repo, ["config", "user.name", "Agents Remember"])
    (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
    run_git(repo, ["add", "README.md"])
    run_git(repo, ["commit", "-m", "init"])


def current_branch(repo: Path) -> str:
    return git_output(repo, ["branch", "--show-current"]) or "master"


def current_head(repo: Path) -> str:
    return git_output(repo, ["rev-parse", "HEAD"])


def git_output(repo: Path, args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def run_git(repo: Path, args: list[str]) -> None:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
