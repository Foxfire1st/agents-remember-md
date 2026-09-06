"""Document parsing and timestamp repair preserve code examples, table cells and historical ordering."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.memory_quality.check import run_memory_quality_check
from agents_remember.memory_quality.style.document_shape import inline_scan, tables
from agents_remember.memory_quality.style.update_history import history_order, history_order_fix

# Verbatim from onboarding/dashboard/src/panels/session-cockpit/SessionsView.test.tsx.md
# and onboarding/mcp/src/agents_remember/observer/reducer.py.md. Both are correct prose
# whose first character is '+'. Flagging either is what gets this check switched off.
LEGITIMATE_PLUS_LINES = (
    "+3 integration cases from 260715-FEUI-L2; +7 from 260715-FEUI-L6 incl. the fix round; +2",
    "+enclosure/repoId from the envelope), `_ended_updates` (L405-L407 - the ONE way",
)
# Verbatim from onboarding/mcp/tests/test_packaged_assets_and_context_values.py.md line 30.
# The code span ends in a backslash, so its closing backtick is preceded by one.
WINDOWS_PREFIX_ROW = (
    "| `LongPathTests` | `long_path` prefixes Windows paths with the "
    "`\\\\?\\` extended-length marker. |"
)
# Verbatim from onboarding/dashboard/src/panels/EngineRoom.tsx.md line 91: escaped pipes
# in prose, BETWEEN code spans.
ESCAPED_PIPE_OUTSIDE_SPAN_ROW = (
    "| §4.2 3-zone room (`Panel fill` -> `roomShell` -> header + `roomGrid` "
    "[stack \\| `roomStage` \\| `roomZone`]). | - | [panels/EngineRoom.tsx](EngineRoom.tsx) |"
)
# Verbatim from onboarding/dashboard/src/dev/benchProbes.ts.md line 40: escaped pipes
# INSIDE a code span, which is a different rule reaching the same answer.
ESCAPED_PIPE_INSIDE_SPAN_ROW = (
    '| `CockpitBenchTransition` | `"launch-failures" \\| "set-turn-ended" \\| '
    '"defer-next-open" \\| "release-open"` - the steps a driver can drive a scenario '
    "through mid-test |"
)
# Verbatim from onboarding/dashboard/src/panels/RailChat.tsx.md line 89, wrapped into a
# table cell: a double-backtick span quoting text that itself contains backticks.
MULTI_BACKTICK_ROW = (
    "| `Pane` | pass ``ariaLabel={`terminal: ${session.label}`}`` so each pane's "
    '`role="group"` landmark is named |'
)
# Verbatim from onboarding/dashboard/src/panels/overview.md lines 708-709 -- the pair the
# old naive-as-UTC comparison read as correctly ordered.
LIVE_MIXED_FRAME_PAIR = (
    "- 2026-06-19T05:48 - Task 6 slice 6e-3: the Chats view gained a `SessionComposer`.",
    "- 2026-06-19T06:39+02:00 - No route impact: an engine-room crash fix guards a read.",
)


def write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    return path


def document(*lines: str) -> str:
    return "\n".join(("# Example", "", *lines, ""))


def history_document(*bullets: str) -> str:
    return "\n".join(("# Example", "", "## Update History", "", *bullets, ""))


def table_document(*rows: str) -> str:
    return document("| Class | Unit |", "| --- | --- |", *rows)


class PrecisionFixtures(unittest.TestCase):
    """Known-good constructs, copied from live memory documents, that must not be flagged."""

    def assert_clean(self, body: str) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "onboarding"
            write(root, "example.md", body)
            result = run_memory_quality_check(root)
            self.assertTrue(result["ok"], result["findings"])
            self.assertEqual(result["findingCount"], 0, result["findings"])

    def test_the_same_windows_cell_in_a_three_column_table_keeps_its_columns(self) -> None:
        """The tree carries both table shapes; in the wider one the merge is a ragged row.

        ``| Finding | Citations | Source Path |`` is the three-column shape used across
        ``dashboard/`` onboarding. Put the live two-column row's Windows cell in the middle
        of it and the wrong scan order eats the rest of the line, turning a correct row
        into a reported defect -- which is the false positive that decided this check.
        """
        row = WINDOWS_PREFIX_ROW + " L12-L20 |"
        self.assertEqual(len(inline_scan.split_row(row)), 3)
        self.assert_clean(document("| Class | Unit | Citations |", "| --- | --- | --- |", row))

    def test_a_diff_quoted_in_a_fenced_block_is_the_document_working(self) -> None:
        self.assert_clean(document("```diff", "+## Contract", "+ wrapped continuation", "```"))


class TableTests(unittest.TestCase):
    def check(self, body: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "onboarding"
            write(root, "example.md", body)
            return tables.check_onboarding_root(root)

    def test_a_missing_cell_is_reported(self) -> None:
        result = self.check(table_document("| only one |"))
        self.assertEqual(result["findings"][0]["code"], "table_row_cell_count_mismatch")
        self.assertIn("1 cells", result["findings"][0]["message"])
        self.assertIn("Nothing is lost", result["findings"][0]["message"])


class UpdateHistoryTimezoneTests(unittest.TestCase):
    """The offset rule, and the closeout diff that scopes it."""

    def check(self, *bullets: str) -> dict:
        """An unversioned root: no HEAD, so nothing is historical and every line is in scope."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "onboarding"
            write(root, "example.md", history_document(*bullets))
            return history_order.check_onboarding_root(root)

    def test_the_live_mixed_frame_pair_is_caught_instead_of_passing_silently(self) -> None:
        result = self.check(*LIVE_MIXED_FRAME_PAIR)
        self.assertFalse(result["ok"])
        codes = [finding["code"] for finding in result["findings"]]
        self.assertEqual(codes, ["update_history_timestamp_naive"])
        self.assertEqual(result["findings"][0]["timestamp"], "2026-06-19T05:48")

    def test_the_fixer_refuses_to_sort_a_section_that_mixes_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "onboarding"
            path = write(root, "example.md", history_document(*LIVE_MIXED_FRAME_PAIR))
            before = path.read_text(encoding="utf-8")
            result = history_order_fix.fix_onboarding_root(root)
            self.assertFalse(result["ok"])
            self.assertEqual(result["skippedFiles"], ["example.md"])
            self.assertEqual(result["changedFiles"], [])
            self.assertEqual(path.read_text(encoding="utf-8"), before)


class ClosingDiffScopeTests(unittest.TestCase):
    """The rule applies to what this closeout wrote, and to nothing else.

    Each case builds the real thing the gate stands in: a memory repository whose
    Update History is already committed, then edited the way a closeout edits it.
    """

    def git(self, repo: Path, *args: str) -> None:
        result = subprocess.run(
            ["git", *args], cwd=repo, text=True, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)

    def memory_repo(self, tmp_dir: str, *bullets: str) -> Path:
        repo = Path(tmp_dir) / "memory"
        root = repo / "onboarding"
        write(root, "example.md", history_document(*bullets))
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.email", "l6@example.invalid")
        self.git(repo, "config", "user.name", "L6")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "history as it already stands")
        return root

    def test_a_naive_bullet_this_closeout_adds_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = self.memory_repo(tmp_dir, "- 2026-06-19T05:48: Historical.")
            write(
                root,
                "example.md",
                history_document(
                    "- 2026-06-20T11:00: Added by this closeout, no offset.",
                    "- 2026-06-19T05:48: Historical.",
                ),
            )
            result = history_order.check_onboarding_root(root)
            self.assertFalse(result["ok"])
            self.assertEqual(
                [(f["code"], f["line"]) for f in result["findings"]],
                [("update_history_timestamp_naive", 5)],
            )

    def test_a_renamed_document_is_not_treated_as_newly_written(self) -> None:
        """A pure rename contributes no newly written lines."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = self.memory_repo(tmp_dir, "- 2026-06-19T05:48: Historical.")
            self.git(root.parent, "mv", "onboarding/example.md", "onboarding/moved.md")
            result = history_order.check_onboarding_root(root)
            self.assertTrue(result["ok"], result["findings"])


if __name__ == "__main__":
    unittest.main()
