from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(MCP_SRC))
sys.path.insert(0, str(MCP_TESTS))

from agents_remember.memory_quality.check import (
    BEFORE_METADATA_REFRESH_CHECKS,
    run_memory_quality_check,
)
from agents_remember.memory_quality.style.document_shape import entity_catalog_alignment


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_file_level_onboarding(
    path: Path,
    *,
    source_path: str,
    commit_hash: str,
    commit_date: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"# {source_path}",
                "",
                "| Field                  | Value                                      |",
                "| ---------------------- | ------------------------------------------ |",
                "| repository             | agents-remember                         |",
                f"| path                   | `{source_path}` |",
                "| doc_type               | `file-level-onboarding`                    |",
                f"| lastVerifiedCommitHash | `{commit_hash}` |",
                f"| lastVerifiedCommitDate | {commit_date} |",
                "",
                "## Purpose",
                "",
                "Fixture onboarding.",
                "",
                "## Update History",
                "",
                "- 2026-05-24T00:37+02:00: Created fixture onboarding.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_entity_catalog(path: Path, *, inventory: list[str], fingerprints: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    inventory_lines = [line for name in inventory for line in (f"### `{name}`", "", "Fixture.", "")]
    fingerprint_lines = [
        f"| `{name}` | `git-blob-set-sha256-v1` | `abc` | `README.md` |" for name in fingerprints
    ]
    path.write_text(
        "\n".join(
            [
                "# Repository Entity Catalog",
                "",
                "## Entity Inventory",
                "",
                *inventory_lines,
                "## Entity Fingerprints",
                "",
                "| Entity | Algorithm | Fingerprint | Evidence Paths |",
                "| --- | --- | --- | --- |",
                *fingerprint_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )


class MemoryQualityTests(unittest.TestCase):
    def test_entity_catalog_alignment_rejects_orphaned_fingerprint_before_code_rails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            onboarding = Path(tmp_dir) / "onboarding"
            write_entity_catalog(
                onboarding / "entities.md",
                inventory=["Present"],
                fingerprints=["Present", "Orphan"],
            )

            result = run_memory_quality_check(
                onboarding, checks=[entity_catalog_alignment.CHECK_NAME]
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["findingCount"], 1)
            self.assertEqual(result["findings"][0]["code"], "entity_fingerprint_without_inventory")
            self.assertEqual(BEFORE_METADATA_REFRESH_CHECKS[0], entity_catalog_alignment.CHECK_NAME)

    def test_entity_catalog_alignment_accepts_one_fingerprint_per_inventory_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            onboarding = Path(tmp_dir) / "onboarding"
            write_entity_catalog(
                onboarding / "entities.md", inventory=["Aligned"], fingerprints=["Aligned"]
            )

            result = run_memory_quality_check(onboarding)

            self.assertTrue(result["ok"])
            self.assertIn(entity_catalog_alignment.CHECK_NAME, result["checks"])


def initialize_clean_memory_fixture(root: Path) -> None:
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
    write_file_level_onboarding(
        memory / "onboarding" / "README.md.md",
        source_path="README.md",
        commit_hash=git_output(repo, ["rev-parse", "HEAD"]),
        commit_date=git_output(repo, ["show", "-s", "--format=%cI", "HEAD"]),
    )


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
