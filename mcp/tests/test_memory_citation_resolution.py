"""Citation resolution rejects missing, escaped and out-of-range sources while preserving valid pooled claims."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.memory_quality.check import (
    run_memory_quality_check,
)
from agents_remember.memory_quality.style.citations import (
    range_resolution,
)

CARD_HEADER = (
    "# {path}",
    "",
    "| Field | Value |",
    "| --- | --- |",
    "| repository | agents-remember |",
    "| path | `{path}` |",
    "",
    "## Repo-Internal References",
    "",
    "| Finding | Anchor | Source |",
    "| --- | --- | --- |",
)
SUPERSEDED_HEADER = (
    "| Finding | Citations | Source Path |",
    "| --- | --- | --- |",
)
EVERY_CODE = frozenset(
    {
        "citation_table_columns_wrong",
        "citation_source_malformed",
        "citation_source_duplicate",
        "citation_anchor_missing",
        "citation_source_missing",
        "citation_range_out_of_bounds",
        "citation_anchor_absent_from_range",
        "citation_prose_malformed",
        "citation_prose_not_in_cit_form",
        "citation_prose_form_in_table_cell",
        "citation_source_vanished",
    }
)


def document(*rows: str, path: str) -> str:
    header = "\n".join(line.format(path=path) for line in CARD_HEADER)
    return header + "\n" + "\n".join(rows) + "\n"


def numbered(count: int, *, marker: str = "line") -> str:
    return "\n".join(f"const {marker}{index} = {index};" for index in range(1, count + 1)) + "\n"


class Tree:
    """A memory repository and the code repository it documents, both on disk."""

    def __init__(self, root: Path) -> None:
        self.code = root / "code"
        self.memory = root / "memory"
        self.onboarding = self.memory / "onboarding"
        self.code.mkdir(parents=True)
        self.onboarding.mkdir(parents=True)

    def write(self, base: Path, relative: str, body: str) -> Path:
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def source(self, relative: str, body: str) -> Path:
        return self.write(self.code, relative, body)

    def memory_file(self, relative: str, body: str) -> Path:
        return self.write(self.memory, relative, body)

    def card(self, source_path: str, *rows: str, at: str | None = None) -> Path:
        return self.write(
            self.onboarding, at or f"{source_path}.md", document(*rows, path=source_path)
        )

    def run(self) -> dict:
        return range_resolution.check_onboarding_root(self.onboarding, self.code)


class TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = Tree(Path(self._tmp.name))

    def assert_clean(self, result: dict) -> None:
        self.assertTrue(result["ok"], result["findings"])
        self.assertEqual(result["findingCount"], 0, result["findings"])
        self.assertEqual(result["reportOnlyFindingCount"], 0, result["reportOnlyFindings"])

    def codes(self, result: dict) -> list[str]:
        return [one["code"] for one in result["findings"]]


class FalsePositiveFixtures(TreeCase):
    """Every mode the module docstring enumerates, on a construct that exists in the tree."""

    def test_1_a_word_boundary_is_not_satisfied_by_a_longer_identifier(self) -> None:
        """`SERVED` must not pass on `SERVED_LIFECYCLE`, nor `taskName` on `enclosureTaskName`."""
        self.tree.source("serving/lifecycle.py", "SERVED_LIFECYCLE = 1\nenclosureTaskName = 2\n")
        self.tree.card(
            "serving/caller.py",
            "| The two names. | `SERVED`; `taskName` | serving/lifecycle.py:1-2 |",
        )
        result = self.tree.run()
        self.assertEqual(self.codes(result), ["citation_anchor_absent_from_range"] * 2)
        messages = " ".join(one["message"] for one in result["findings"])
        self.assertIn("the range holds ['SERVED_LIFECYCLE']", messages)
        self.assertIn("`taskName`", messages)

    def test_1b_the_same_names_pass_when_the_range_really_holds_them(self) -> None:
        self.tree.source("serving/lifecycle.py", "SERVED = 1\ntaskName = 2\n")
        self.tree.card(
            "serving/caller.py",
            "| The two names. | `SERVED`; `taskName` | serving/lifecycle.py:1-2 |",
        )
        self.assert_clean(self.tree.run())

    def test_4_two_ranges_one_anchor_are_pooled_not_paired(self) -> None:
        """``PUBLIC_TOOLS`` cited with a sub-range pointing into its own body."""
        body = numbered(20) + "export const PUBLIC_TOOLS = [\n" + numbered(60) + "];\n"
        self.tree.source("mcp/tools/base.py", body)
        self.tree.card(
            "mcp/tools/base.py",
            "| The two terminal-catalog public tools. | `PUBLIC_TOOLS` "
            "| mcp/tools/base.py:21-77; mcp/tools/base.py:23-24 |",
        )
        self.assert_clean(self.tree.run())


class ProseGrammarTests(TreeCase):
    """`cit:([anchors], path:start-end)` in running text, sharing every table rule."""

    def prose(self, *body: str) -> dict:
        self.tree.source("kernel/route_index.py", "def build_route_indexes():\n    pass\n" * 30)
        self.tree.card("kernel/caller.py", "| Nothing here. | — | — |", "", *body)
        return self.tree.run()

    def test_a_well_formed_citation_resolves_and_passes(self) -> None:
        result = self.prose(
            "The census freezes membership cit:([`build_route_indexes`], "
            "kernel/route_index.py:1-2) and nothing else does."
        )
        self.assert_clean(result)
        self.assertEqual(result["proseCitations"], 1)
        self.assertEqual(result["resolvedCitations"], 1)

    def test_an_out_of_bounds_prose_range_fails_with_the_shared_code(self) -> None:
        result = self.prose(
            "The census cit:([`build_route_indexes`], kernel/route_index.py:1-900)."
        )
        self.assertEqual(self.codes(result), ["citation_range_out_of_bounds"])

    def test_an_absent_prose_anchor_fails_with_the_shared_code(self) -> None:
        result = self.prose("The census cit:([`absentName`], kernel/route_index.py:1-2).")
        self.assertEqual(self.codes(result), ["citation_anchor_absent_from_range"])

    def test_a_citation_inside_a_fence_is_not_scanned(self) -> None:
        result = self.prose("```", "cit:([`gone`], kernel/route_index.py:900-999)", "```")
        self.assert_clean(result)
        self.assertEqual(result["proseCitations"], 0)


class MisplacedSerialisationTests(TreeCase):
    """A `cit:` in a table cell is the wrong serialisation, and silence there is the defect."""

    def test_a_cit_written_into_a_finding_cell_is_reported(self) -> None:
        self.tree.source("kernel/route_index.py", "def build_route_indexes():\n    pass\n")
        self.tree.card(
            "kernel/caller.py",
            "| The census cit:([`build_route_indexes`], kernel/route_index.py:1-2). | `x` | — |",
        )
        result = self.tree.run()
        codes = self.codes(result)
        self.assertIn("citation_prose_form_in_table_cell", codes)
        message = next(one["message"] for one in result["findings"] if one["code"] == codes[0])
        self.assertIn("Anchor and Source columns", message)
        self.assertIn("delete the `cit:`", message)


class DeletedClassTests(TreeCase):
    """L6-R13: the two classes R27 made unrepresentable are gone, not dormant."""

    def test_a_parent_step_can_no_longer_reach_a_file_at_a_shallower_depth(self) -> None:
        """The link-depth class needed a relative link that climbs. The grammar refuses one."""
        self.tree.source("dashboard/webtui-scope.config.cjs", "x\n")
        self.tree.card(
            "dashboard/src/styles/webtui.css",
            "| The scoping options. | `x` | ../../../webtui-scope.config.cjs:1-1 |",
        )
        self.assertEqual(self.codes(self.tree.run()), ["citation_source_malformed"])


class StyleSurfaceTests(unittest.TestCase):
    """How the check reaches the gate, and what it says when it cannot resolve."""

    def test_without_a_code_root_the_result_says_so_instead_of_passing_quietly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "onboarding"
            root.mkdir(parents=True)
            result = run_memory_quality_check(root)
            block = result["checks"][range_resolution.CHECK_NAME]
            self.assertEqual(block["status"], "no-code-repository-root")
            self.assertEqual(block["filesChecked"], 0)


if __name__ == "__main__":
    unittest.main()
