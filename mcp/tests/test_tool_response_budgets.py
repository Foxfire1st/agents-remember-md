"""Tool responses stay under budget; bulk detail lands in pruned report files.

S4 of the 2026-06-10 task: the flooders (`runtime_install` >50k chars,
`provider_diagnostics` 5.7k tokens, `provider_watchers` 1.5k) move their
passthrough bulk to `temp/tool-reports/<tool>/` and keep a compact outcome
plus `reportPath` inline.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.kernel.primitives.tool_reports import (
    prune_tool_reports,
    write_tool_report,
)

INLINE_BUDGET_CHARS = 4_000  # ~1k tokens; generous for outcomes, tiny vs the raw payloads


def fat_candidate(index: int, decision: str) -> dict:
    path = f"mcp/src/agents_remember/providers/some/long/module_{index:03d}.py"
    return {
        "source_path": path,
        "branch_onboarding": f"C:/ew/ar-coordination/worktrees/repo/task-ar/memory-task/onboarding/{path}.md",
        "target_onboarding": f"C:/ew/ar-coordination/memory-repos/ar-repo/onboarding/{path}.md",
        "evidence": "exact-landed-commit",
        "decision": decision,
        "reason": "all 1 source branch commit(s) touching this path are ancestors of official code ref",
        "target_exists": True,
    }


def fat_command(name: str) -> dict:
    return {
        "command": ["docker", "compose", "-f", "-", name, "-e", "PGPASSWORD=secret"],
        "stdout": "line\n" * 200,
        "stderr": "noise\n" * 200,
        "returncode": 0,
        "compose": {"project": "p", "baseFile": "x" * 200, "overrideSha256": "f" * 64},
    }


class ToolReportFileTests(unittest.TestCase):
    def test_write_creates_report_and_returns_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_tool_report(root, "demo", {"ok": True, "bulk": ["x"] * 50})
            self.assertTrue(path.exists())
            self.assertIn("tool-reports", path.as_posix())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["ok"], True)

    def test_secrets_are_redacted_in_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {"steps": [fat_command("up")]}
            path = write_tool_report(root, "demo", payload)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("secret", text)
            self.assertIn("PGPASSWORD=***", text)

    def test_prune_keeps_last_five(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for index in range(8):
                report = folder / f"report-{index}.json"
                report.write_text("{}", encoding="utf-8")
                age = (8 - index) * 60.0
                stamp = time.time() - age
                os.utime(report, (stamp, stamp))
            prune_tool_reports(folder)
            remaining = sorted(p.name for p in folder.glob("*.json"))
            self.assertEqual(len(remaining), 5)
            self.assertEqual(remaining, [f"report-{i}.json" for i in range(3, 8)])

    def test_prune_drops_reports_older_than_max_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            fresh = folder / "fresh.json"
            stale = folder / "stale.json"
            fresh.write_text("{}", encoding="utf-8")
            stale.write_text("{}", encoding="utf-8")
            old = time.time() - 8 * 86400
            os.utime(stale, (old, old))
            prune_tool_reports(folder)
            self.assertTrue(fresh.exists())
            self.assertFalse(stale.exists())


class CompactPayloadBudgetTests(unittest.TestCase):
    def test_carryover_report_retains_full_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full = {
                "state": "would-carryover",
                "candidates": [fat_candidate(i, "auto-carry") for i in range(40)],
            }
            path = write_tool_report(root, "memory_carryover_plan", full, label="plan")
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored["candidates"]), 40)
            self.assertEqual(
                stored["candidates"][0]["evidence"],
                "exact-landed-commit",
            )


if __name__ == "__main__":
    unittest.main()
