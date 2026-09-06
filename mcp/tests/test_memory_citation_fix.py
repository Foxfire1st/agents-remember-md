"""L6-R27/R28: what ``--fix`` repairs, what it refuses, and where it is allowed to write.

Four repair classes, one probe each (L6-R16), and the three refusals matter more than the
repair: a confident wrong answer here silently repoints a claim at code that does not do
what the claim says.

    PURE MOVE      the anchor kept its name and changed file       -> applied
    RENAME         a differently-named function does the job now   -> refused
    DELETION       the anchor exists nowhere                       -> refused
    AMBIGUOUS      the anchor resolves in more than one place      -> refused

:class:`NoSimilarityMatchingTests` is the one that would fail loudest if somebody added
"did you mean": it plants the recorded pair, ``_map_command_lifecycle`` cited against a tree
holding only ``_require_command_lifecycle``, and requires the refusal not to name it.

:class:`WriteGuardTests` proves the other half of R27 by filesystem: the official memory
repo is untouched, and a contract that names it is refused rather than followed.
"""

import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(MCP_SRC))
sys.path.insert(0, str(MCP_TESTS))

from agents_remember.memory_quality.style.citations import (
    fixer,
    range_resolution,
    repair,
    source_index,
    source_index_database,
)
from agents_remember.memory_quality.style.citations.resolution import Trees

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
FIRST_ROW = len(CARD_HEADER) + 1


@contextmanager
def _frozen_no_discovery() -> Iterator[None]:
    with (
        mock.patch.object(
            Path,
            "rglob",
            side_effect=AssertionError("frozen refusal scanned memory"),
        ),
        mock.patch.object(
            source_index.os,
            "walk",
            side_effect=AssertionError("frozen refusal walked source"),
        ),
        mock.patch.object(
            source_index,
            "_tree_state",
            side_effect=AssertionError("frozen refusal inspected source"),
        ),
        mock.patch.object(
            source_index,
            "_reclaim_legacy_cache_roots",
            side_effect=AssertionError("frozen refusal reclaimed cache"),
        ),
        mock.patch.object(
            source_index,
            "_build_and_publish",
            side_effect=AssertionError("frozen refusal rebuilt/fell back"),
        ),
        mock.patch.object(
            source_index_database.Database,
            "validate_application_integrity",
            side_effect=AssertionError("frozen refusal traversed integrity"),
        ),
    ):
        yield


def document(*rows: str, path: str) -> str:
    header = "\n".join(line.format(path=path) for line in CARD_HEADER)
    return header + "\n" + "\n".join(rows) + "\n"


def filler(count: int, *, marker: str = "line") -> str:
    return "\n".join(f"{marker}{index} = {index}" for index in range(1, count + 1)) + "\n"


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

    def card(self, source_path: str, *rows: str, at: str | None = None) -> Path:
        return self.write(
            self.onboarding, at or f"{source_path}.md", document(*rows, path=source_path)
        )

    def trees(self) -> Trees:
        return Trees(code_root=self.code, memory_root=self.memory)

    def source_snapshot_id(self) -> str:
        with source_index.open_repository_index(self.trees()) as index:
            return index.snapshot_id

    def fix(self, *, dry_run: bool = False) -> dict[str, Any]:
        return fixer.fix_onboarding_root(self.onboarding, self.code, dry_run=dry_run)

    def check(self) -> dict[str, Any]:
        return range_resolution.check_onboarding_root(self.onboarding, self.code)

    def card_text(self, relative: str) -> str:
        return (self.onboarding / relative).read_text(encoding="utf-8")

    def row(self, relative: str, offset: int = 0) -> str:
        return self.card_text(relative).splitlines()[FIRST_ROW - 1 + offset]


class TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = Tree(Path(self._tmp.name))

    def sources(self, result: dict[str, Any], index: int = 0) -> str:
        self.assertTrue(result["repairs"], result["declined"])
        return str(result["repairs"][index]["now"])

    def declined(self, result: dict[str, Any], index: int = 0) -> dict[str, Any]:
        self.assertTrue(result["declined"], result["repairs"])
        return dict(result["declined"][index])

    def assert_check_clean(self) -> None:
        result = self.tree.check()
        self.assertTrue(result["ok"], result["findings"])


class PureMoveTests(TreeCase):
    """A symbol that kept its name and changed file. The only class that auto-repairs."""

    def test_a_vanished_file_is_repointed_to_where_the_symbol_now_lives(self) -> None:
        self.tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
        self.tree.card(
            "kernel/caller.py",
            "| The census freezes membership. | `build_route_indexes` "
            "| kernel/route_index.py:1-2 |",
        )

        result = self.tree.fix()

        self.assertEqual(self.sources(result), "kernel/indexes.py:1-2")
        self.assertEqual(result["claimsRepaired"], 1)
        self.assertEqual(result["declinedCount"], 0)
        self.assertEqual(result["findingsRemaining"], 0)
        self.assertTrue(result["ok"])
        self.assert_check_clean()

    def test_a_symbol_that_left_a_file_that_still_exists_replaces_that_source(self) -> None:
        """The old module survived the extraction; the citation must not survive with it."""
        self.tree.source("kernel/route_index.py", filler(20))
        self.tree.source("kernel/indexes.py", filler(4) + "def build_route_indexes():\n    x = 1\n")
        self.tree.card(
            "kernel/caller.py",
            "| The census freezes membership. | `build_route_indexes` "
            "| kernel/route_index.py:1-2 |",
        )

        result = self.tree.fix()

        self.assertEqual(self.sources(result), "kernel/indexes.py:5-6")
        self.assertNotIn("route_index.py", self.tree.row("kernel/caller.py.md"))
        self.assert_check_clean()

    def test_a_move_is_found_even_when_forty_other_files_mention_the_name(self) -> None:
        """Uniqueness is judged on DEFINITIONS: callers move nothing."""
        self.tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
        for index in range(40):
            self.tree.source(f"callers/use{index}.py", "build_route_indexes()\n")
        self.tree.card(
            "kernel/caller.py",
            "| The census. | `build_route_indexes` | kernel/route_index.py:1-2 |",
        )

        self.assertEqual(self.sources(self.tree.fix()), "kernel/indexes.py:1-2")

    def test_the_whole_construct_is_stamped_decorators_included(self) -> None:
        self.tree.source(
            "kernel/indexes.py",
            "import functools\n\n\n@functools.cache\ndef build_route_indexes():\n    return 1\n",
        )
        self.tree.card(
            "kernel/caller.py",
            "| The census. | `build_route_indexes` | kernel/indexes.py:1-1 |",
        )

        self.assertEqual(self.sources(self.tree.fix()), "kernel/indexes.py:4-6")


class ReflowTests(TreeCase):
    """The bulk of the churn: the file changed shape and the number went stale."""

    def test_a_range_that_stops_short_of_its_own_construct_is_regenerated(self) -> None:
        self.tree.source("kernel/store.py", filler(10) + "def persist():\n    return 2\n")
        self.tree.card("kernel/store.py", "| The write path. | `persist` | kernel/store.py:12-12 |")

        result = self.tree.fix()

        self.assertEqual(self.sources(result), "kernel/store.py:11-12")

    def test_a_range_past_the_end_of_the_file_is_regenerated(self) -> None:
        self.tree.source("kernel/store.py", "def persist():\n    return 2\n")
        self.tree.card(
            "kernel/store.py", "| The write path. | `persist` | kernel/store.py:400-480 |"
        )

        self.assertEqual(self.sources(self.tree.fix()), "kernel/store.py:1-2")

    def test_a_citation_that_is_already_right_is_left_byte_identical(self) -> None:
        self.tree.source("kernel/store.py", "def persist():\n    return 2\n")
        self.tree.card("kernel/store.py", "| The write path. | `persist` | kernel/store.py:1-2 |")
        before = self.tree.card_text("kernel/store.py.md")

        result = self.tree.fix()

        self.assertEqual(result["failingClaims"], 0)
        self.assertEqual(result["documentsWritten"], 0)
        self.assertEqual(self.tree.card_text("kernel/store.py.md"), before)

    def test_a_second_run_is_a_no_op(self) -> None:
        self.tree.source("kernel/store.py", filler(10) + "def persist():\n    return 2\n")
        self.tree.card("kernel/store.py", "| The write path. | `persist` | kernel/store.py:12-12 |")
        self.tree.fix()
        after = self.tree.card_text("kernel/store.py.md")

        again = self.tree.fix()

        self.assertEqual(again["claimsRepaired"], 0)
        self.assertEqual(self.tree.card_text("kernel/store.py.md"), after)

    def test_a_dry_run_reports_the_rewrite_and_writes_nothing(self) -> None:
        self.tree.source("kernel/store.py", filler(10) + "def persist():\n    return 2\n")
        self.tree.card("kernel/store.py", "| The write path. | `persist` | kernel/store.py:12-12 |")
        before = self.tree.card_text("kernel/store.py.md")

        result = self.tree.fix(dry_run=True)

        self.assertEqual(self.sources(result), "kernel/store.py:11-12")
        self.assertEqual(result["documentsWritten"], 0)
        self.assertEqual(self.tree.card_text("kernel/store.py.md"), before)

    def test_the_row_around_the_source_cell_survives_the_rewrite(self) -> None:
        self.tree.source("kernel/store.py", filler(10) + "def persist():\n    return 2\n")
        self.tree.card(
            "kernel/store.py",
            "| The write path, and `persist` is named here too. | `persist` "
            "|   kernel/store.py:12-12   |",
        )

        self.tree.fix()

        self.assertEqual(
            self.tree.row("kernel/store.py.md"),
            "| The write path, and `persist` is named here too. | `persist` "
            "|   kernel/store.py:11-12   |",
        )


class NoSimilarityMatchingTests(TreeCase):
    """Refuse similarity guesses when an anchor is absent."""

    def rename(self) -> dict[str, Any]:
        self.tree.source(
            "controlplane/lifecycle.py",
            "def _require_command_lifecycle(command):\n    raise ValueError(command)\n",
        )
        self.tree.card(
            "controlplane/caller.py",
            "| The command palette maps its lifecycle. | `_map_command_lifecycle` "
            "| controlplane/lifecycle.py:1-2 |",
        )
        return self.tree.fix()

    def test_a_rename_is_refused_rather_than_guessed_at(self) -> None:
        result = self.rename()

        self.assertEqual(result["claimsRepaired"], 0)
        self.assertEqual(self.declined(result)["code"], repair.ANCHOR_ABSENT)

    def test_the_refusal_never_names_the_similar_symbol(self) -> None:
        """One maps and the other validates; a string distance cannot tell them apart."""
        message = self.declined(self.rename())["message"]

        self.assertNotIn("_require_command_lifecycle", message)
        self.assertIn("exists NOWHERE", message)
        self.assertIn("Read the CLAIM", message)

    def test_the_row_is_left_exactly_as_written(self) -> None:
        self.rename()

        self.assertIn("controlplane/lifecycle.py:1-2", self.tree.row("controlplane/caller.py.md"))


class DeletionAndAmbiguityTests(TreeCase):
    """The other two refusals, each with the work order R28 requires."""

    def test_a_deleted_anchor_is_refused_and_says_the_claim_is_what_changed(self) -> None:
        self.tree.source("serving/live.py", "def kept():\n    return 1\n")
        self.tree.card(
            "serving/caller.py",
            "| The palette runs turn.stop. | `stop_turn` | serving/live.py:1-2 |",
        )

        declined = self.declined(self.tree.fix())

        self.assertEqual(declined["code"], repair.ANCHOR_ABSENT)
        self.assertEqual(declined["anchor"], "`stop_turn`")
        self.assertIn("rename or a deletion", declined["message"])

    def test_an_ambiguous_anchor_is_refused_and_names_every_candidate(self) -> None:
        self.tree.source("serving/left.py", "def emit():\n    return 1\n")
        self.tree.source("serving/right.py", "def emit():\n    return 2\n")
        self.tree.card("serving/caller.py", "| The emitter. | `emit` | serving/gone.py:1-2 |")

        declined = self.declined(self.tree.fix())

        self.assertEqual(declined["code"], repair.ANCHOR_AMBIGUOUS)
        self.assertIn("2 file(s) hold it", declined["message"])
        self.assertIn("serving/left.py:1-2", declined["message"])
        self.assertIn("serving/right.py:1-2", declined["message"])
        self.assertIn("never picks between candidates by similarity", declined["message"])

    def test_two_constructs_of_the_same_name_in_the_cited_file_are_refused(self) -> None:
        self.tree.source(
            "serving/live.py",
            "def emit():\n    return 1\n" + filler(20) + "def emit():\n    return 2\n",
        )
        self.tree.card("serving/caller.py", "| The emitter. | `emit` | serving/live.py:40-45 |")

        declined = self.declined(self.tree.fix())

        self.assertEqual(declined["code"], repair.ANCHOR_AMBIGUOUS)
        self.assertIn("occurs 2 times", declined["message"])
        self.assertIn("picks out none of them", declined["message"])

    def test_the_cited_range_picks_between_two_constructs_when_it_overlaps_one(self) -> None:
        """The author's own range is the tiebreaker inside a file, never a similarity score."""
        self.tree.source(
            "serving/live.py",
            "def emit():\n    return 1\n" + filler(20) + "def emit():\n    return 2\n",
        )
        self.tree.card("serving/caller.py", "| The emitter. | `emit` | serving/live.py:24-24 |")

        self.assertEqual(self.sources(self.tree.fix()), "serving/live.py:23-24")

    def test_a_tree_left_with_findings_it_has_no_rule_for_does_not_report_ok(self) -> None:
        """The halfway state must not be indistinguishable from a finished one."""
        self.tree.source("kernel/store.py", filler(10) + "def persist():\n    return 2\n")
        self.tree.card(
            "kernel/store.py",
            "| The write path. | `persist` | kernel/store.py:12-12 |",
            "| Unanchored. | — | kernel/store.py:1-2 |",
        )

        result = self.tree.fix()

        self.assertEqual(result["claimsRepaired"], 1)
        self.assertEqual(result["declinedCount"], 0)
        self.assertEqual(result["findingsRemaining"], 1)
        self.assertFalse(result["ok"])

    def test_a_declined_claim_leaves_a_complete_work_order(self) -> None:
        self.tree.source("serving/live.py", "def kept():\n    return 1\n")
        self.tree.card(
            "serving/caller.py",
            "| The palette runs turn.stop. | `stop_turn` | serving/live.py:1-2 |",
        )

        declined = self.declined(self.tree.fix())

        self.assertEqual(declined["path"], "serving/caller.py.md")
        self.assertEqual(declined["line"], FIRST_ROW)
        self.assertTrue(declined["message"])


class MultiAnchorRangeTests(TreeCase):
    """Several anchors pool, so the generated source list is a UNION, never a span."""

    def two_anchors(self) -> dict[str, Any]:
        self.tree.source(
            "kernel/store.py",
            "def persist():\n    return 1\n" + filler(30) + "def reload():\n    return 2\n",
        )
        self.tree.card(
            "kernel/store.py",
            "| Both halves of the store. | `persist`; `reload` | kernel/store.py:1-1 |",
        )
        return self.tree.fix()

    def test_two_distant_anchors_produce_two_ranges_not_one_span(self) -> None:
        self.assertEqual(
            self.sources(self.two_anchors()), "kernel/store.py:1-2; kernel/store.py:33-34"
        )

    def test_the_union_is_not_the_enclosing_span(self) -> None:
        self.assertNotIn("kernel/store.py:1-34", self.sources(self.two_anchors()))

    def test_discontiguous_anchors_are_repaired_rather_than_refused(self) -> None:
        self.assertEqual(self.two_anchors()["declinedCount"], 0)
        self.assert_check_clean()

    def test_abutting_extents_are_merged_into_one_range(self) -> None:
        self.tree.source(
            "kernel/store.py", "def persist():\n    return 1\ndef reload():\n    return 2\n"
        )
        self.tree.card(
            "kernel/store.py",
            "| Both halves. | `persist`; `reload` | kernel/store.py:99-99 |",
        )

        self.assertEqual(self.sources(self.tree.fix()), "kernel/store.py:1-4")

    def test_anchors_in_two_files_produce_one_source_each(self) -> None:
        self.tree.source("kernel/left.py", "def persist():\n    return 1\n")
        self.tree.source("kernel/right.py", "def reload():\n    return 2\n")
        self.tree.card(
            "kernel/caller.py",
            "| Both halves. | `persist`; `reload` | kernel/left.py:2-2; kernel/right.py:2-2 |",
        )

        self.assertEqual(self.sources(self.tree.fix()), "kernel/left.py:1-2; kernel/right.py:1-2")


class AnchorKindTests(TreeCase):
    """One extent rule per anchor kind: AST construct, markdown section, quoted lines."""

    def test_a_heading_anchor_spans_to_the_next_heading_of_equal_or_higher_level(self) -> None:
        self.tree.source(
            "docs/path-rules.md",
            "# Rules\n\n## Scoping\n\nPrefixes win.\n\n### Detail\n\nMore.\n\n## Other\n\nNo.\n",
        )
        self.tree.card(
            "kernel/paths.py",
            "| Path rules scope by prefix. | `## Scoping` | docs/path-rules.md:1-1 |",
        )

        self.assertEqual(self.sources(self.tree.fix()), "docs/path-rules.md:3-10")

    def test_a_quoted_literal_spans_the_lines_it_occupies(self) -> None:
        self.tree.source(
            "serving/refusal.py",
            '"""Refusal banner.\n\nYou must pass the application\nas an import string.\n"""\n',
        )
        self.tree.card(
            "cli/dashboard.py",
            '| The refusal is loud. | "You must pass the application as an import string" '
            "| serving/refusal.py:1-1 |",
        )

        self.assertEqual(self.sources(self.tree.fix()), "serving/refusal.py:3-4")

    def test_a_typescript_construct_is_stamped_from_its_declaration(self) -> None:
        self.tree.source("dashboard/rail.ts", filler(5) + "export class RailRow {}\n" + filler(5))
        self.tree.card("dashboard/panel.tsx", "| The row. | `RailRow` | dashboard/rail.ts:1-1 |")

        self.assertEqual(self.sources(self.tree.fix()), "dashboard/rail.ts:6-6")

    def test_a_language_that_is_not_parsed_falls_back_to_occurrence_runs(self) -> None:
        self.tree.source("dashboard/rail.css", ".x {}\n.rail-row {\n  color: red;\n}\n")
        self.tree.card(
            "dashboard/panel.tsx",
            '| The row colour. | "color: red" | dashboard/rail.css:1-1 |',
        )

        self.assertEqual(self.sources(self.tree.fix()), "dashboard/rail.css:3-3")

    def test_a_python_file_that_does_not_parse_falls_back_the_same_way(self) -> None:
        self.tree.source("kernel/broken.py", filler(3) + "def persist(:\n")
        self.tree.card("kernel/caller.py", "| The write path. | `persist` | kernel/broken.py:1-1 |")

        self.assertEqual(self.sources(self.tree.fix()), "kernel/broken.py:4-4")


class SourcePreservationTests(TreeCase):
    """What `--fix` must not delete, and the one thing it must."""

    def test_a_citation_into_a_dependency_is_carried_through_untouched(self) -> None:
        self.tree.source("kernel/store.py", filler(10) + "def persist():\n    return 2\n")
        self.tree.card(
            "kernel/store.py",
            "| The write path and its warning. | `persist` "
            "| kernel/store.py:12-12; uvicorn/main.py:604-607 |",
        )

        self.assertEqual(
            self.sources(self.tree.fix()), "kernel/store.py:11-12; uvicorn/main.py:604-607"
        )

    def test_an_unanchored_live_source_survives_a_pure_reflow(self) -> None:
        self.tree.source("kernel/store.py", filler(10) + "def persist():\n    return 2\n")
        self.tree.source("kernel/context.py", filler(9))
        self.tree.card(
            "kernel/store.py",
            "| The write path. | `persist` | kernel/store.py:12-12; kernel/context.py:1-9 |",
        )

        self.assertEqual(
            self.sources(self.tree.fix()), "kernel/store.py:11-12; kernel/context.py:1-9"
        )

    def test_an_unanchored_live_source_is_dropped_once_an_anchor_has_moved(self) -> None:
        """Keeping it would leave the vacated pointer sitting beside its own replacement."""
        self.tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
        self.tree.source("kernel/route_index.py", filler(9))
        self.tree.card(
            "kernel/caller.py",
            "| The census. | `build_route_indexes` | kernel/route_index.py:1-9 |",
        )

        self.assertEqual(self.sources(self.tree.fix()), "kernel/indexes.py:1-2")


class UnresolvableOnlyClaimTests(TreeCase):
    """A claim whose every source is a dependency is SATISFIED, not permanently failing.

    Found by a curator pilot, and it made ``--fix`` useless on the third-party form this leaf
    mandates: ``unsatisfied`` folded over an empty ``bodies``, so ``any(...)`` was False for
    every anchor and the claim reported as failing on every run. Since ``ok`` is ``not refused
    and not remaining``, ``--fix`` could never report ok on a tree carrying one of these.
    """

    def _dependency_only_card(self) -> None:
        self.tree.source("kernel/store.py", filler(10))
        self.tree.card(
            "kernel/store.py",
            '| The refusal is loud, not silent. | "You must pass the application as an '
            'import string" | uvicorn/main.py:604-607 |',
        )

    def test_a_claim_citing_only_a_dependency_is_not_a_failing_claim(self) -> None:
        self._dependency_only_card()

        result = self.tree.fix()

        self.assertEqual(result["failingClaims"], 0, result["declined"])
        self.assertEqual(result["declinedCount"], 0, result["declined"])
        self.assertTrue(result["ok"], result)

    def test_the_checker_and_the_fixer_agree_that_nothing_resolved_means_nothing_unheld(
        self,
    ) -> None:
        """The guard lives in ``unsatisfied`` so a third caller cannot reintroduce the bug."""
        self._dependency_only_card()

        self.assert_check_clean()
        self.assertEqual(self.tree.fix()["failingClaims"], 0)

    def test_scoped_normalisation_does_not_make_a_dependency_permanently_unfixable(self) -> None:
        self._dependency_only_card()
        snapshot = self.tree.source_snapshot_id()

        result = fixer.fix_onboarding_root(
            self.tree.onboarding,
            self.tree.code,
            only="kernel/store.py.md",
            expected_snapshot=snapshot,
        )

        self.assertEqual(result["declinedCount"], 0)
        self.assertEqual(result["documentsWritten"], 0)
        self.assertTrue(result["ok"], result)

    def test_an_unheld_anchor_is_still_reported_when_something_did_resolve(self) -> None:
        """The guard must not swallow the real finding it sits next to."""
        self.tree.source("kernel/store.py", filler(10))
        self.tree.card(
            "kernel/store.py",
            "| The write path. | `persist` | kernel/store.py:1-9; uvicorn/main.py:604-607 |",
        )

        codes = {finding["code"] for finding in self.tree.check()["findings"]}

        self.assertIn("citation_anchor_absent_from_range", codes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
