from __future__ import annotations

from pathlib import Path
from unittest import mock

from agents_remember.memory_quality.style.citations import (
    fixer,
    range_resolution,
    source_index,
)
from test_memory_citation_fix import TreeCase


class DocumentScopeTests(TreeCase):
    """`--document` exists because a curator wave shares one memory worktree.

    A tree-wide ``--fix`` rewrites documents anywhere in it, so one curator's run can rewrite
    another's document mid-edit -- measured on the pilot, where two of four curators avoided
    it only by dry-running first or by copying their document to a throwaway tree.
    """

    def _two_failing_cards(self) -> None:
        self.tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
        self.tree.source("kernel/store.py", "def persist():\n    return 2\n")
        self.tree.card(
            "kernel/a.py", "| The census. | `build_route_indexes` | kernel/gone.py:1-2 |"
        )
        self.tree.card("kernel/b.py", "| The write path. | `persist` | kernel/gone.py:1-2 |")

    def test_a_scoped_fix_leaves_every_other_document_byte_identical(self) -> None:
        self._two_failing_cards()
        before = self.tree.card_text("kernel/b.py.md")
        snapshot = self.tree.source_snapshot_id()

        result = fixer.fix_onboarding_root(
            self.tree.onboarding,
            self.tree.code,
            only="kernel/a.py.md",
            expected_snapshot=snapshot,
        )

        self.assertEqual(result["claimsRepaired"], 1)
        self.assertEqual(self.tree.card_text("kernel/b.py.md"), before)
        self.assertIn("kernel/indexes.py", self.tree.row("kernel/a.py.md"))

    def test_invalid_exact_paths_refuse_without_memory_discovery_or_source_acquisition(
        self,
    ) -> None:
        self._two_failing_cards()
        outside = self.tree.memory / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        (self.tree.onboarding / "escape.md").symlink_to(outside)

        for selected in (
            "/absolute.md",
            "../outside.md",
            "kernel/a.py",
            "./kernel/a.py.md",
            "kernel//a.py.md",
            "kernel/missing.py.md",
            "escape.md",
        ):
            with (
                self.subTest(selected=selected),
                mock.patch.object(
                    Path,
                    "rglob",
                    side_effect=AssertionError("exact selection used memory rglob"),
                ),
                mock.patch.object(
                    source_index,
                    "open_repository_index",
                    side_effect=AssertionError("invalid document acquired source index"),
                ),
                self.assertRaises(ValueError),
            ):
                range_resolution.check_onboarding_root(
                    self.tree.onboarding,
                    self.tree.code,
                    only=selected,
                    expected_snapshot="a" * 64,
                )


class ScopedNormalisationTests(TreeCase):
    """A curator's provisional passing range is generated away inside its one document."""

    def test_expanded_sources_are_deduplicated_and_the_second_run_is_byte_identical(self) -> None:
        self.tree.source(
            "kernel/store.py",
            "def persist():\n    return 1\n\n\ndef reload():\n    return 2\n",
        )
        self.tree.card(
            "kernel/store.py",
            "| The two operations. | `persist`; `reload` | "
            "kernel/store.py:1-6; kernel/store.py:1-6 |",
        )
        snapshot = self.tree.source_snapshot_id()

        first = fixer.fix_onboarding_root(
            self.tree.onboarding,
            self.tree.code,
            only="kernel/store.py.md",
            expected_snapshot=snapshot,
        )

        self.assertEqual(self.sources(first), "kernel/store.py:1-2; kernel/store.py:5-6")
        self.assertEqual(first["claimsRepaired"], 1)
        self.assertEqual(first["claimsNormalised"], 0)
        self.assertTrue(first["ok"], first)
        after_first = self.tree.card_text("kernel/store.py.md")

        second = fixer.fix_onboarding_root(
            self.tree.onboarding,
            self.tree.code,
            only="kernel/store.py.md",
            expected_snapshot=snapshot,
        )

        self.assertEqual(second["claimsNormalised"], 0)
        self.assertEqual(second["documentsWritten"], 0)
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.tree.card_text("kernel/store.py.md"), after_first)

    def test_a_malformed_source_segment_blocks_normalisation_without_deleting_evidence(
        self,
    ) -> None:
        """A formatter may not make a malformed dependency pointer disappear to become green."""
        self.tree.source("kernel/store.py", "a=1\nb=2\nc=3\nd=4\ndef persist():\n    return 2\n")
        self.tree.card(
            "kernel/store.py",
            "| Local behavior plus external evidence. | `persist` | "
            "kernel/store.py:5-5; [dependency](https://example.test/source.py#L10) |",
        )
        before = self.tree.card_text("kernel/store.py.md")
        snapshot = self.tree.source_snapshot_id()

        result = fixer.fix_onboarding_root(
            self.tree.onboarding,
            self.tree.code,
            only="kernel/store.py.md",
            expected_snapshot=snapshot,
        )

        self.assertEqual(result["claimsNormalised"], 0)
        self.assertEqual(result["documentsWritten"], 0)
        self.assertEqual(result["findingsRemaining"], 1)
        self.assertFalse(result["ok"])
        self.assertEqual(self.tree.card_text("kernel/store.py.md"), before)
        self.assertEqual(
            [one["code"] for one in self.tree.check()["findings"]],
            ["citation_source_malformed"],
        )
