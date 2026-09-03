"""CCR-R10 forcing fixtures: the deterministic anchor-to-range projection.

Only exact unique moves generate: the range is projected from the frozen source-index
snapshot through the shared oracle (symbol_index.locate / Sightings.unique), the claim
is rewritten inside the same byte-level edit transaction as an explicit generated
no-content-impact Update History bullet, and every projection binds snapshot id, prior
claim digest, anchor, resolved extent, new document digest, and repair-tool version.
Multiple definitions, parsed mention-only anchors, renames, deletions, stale snapshots,
conflicting writes, and malformed claims refuse deterministically and never accept the
old range as a fallback.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from agents_remember.memory_quality.style.citations import (
    deterministic_projection,
    extents,
    fixer,
    model,
    range_resolution,
    repair,
    source_index,
    symbol_index,
)
from agents_remember.memory_quality.style.citations.editing import Site, rewritten
from agents_remember.memory_quality.style.citations.resolution import Trees
from agents_remember.memory_quality.style.update_history.history_order import (
    datetime_value,
    parse_timestamp,
)

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)

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


def document(*rows: str, path: str, history: tuple[str, ...] = ()) -> str:
    """A canonical card, optionally carrying an Update History section."""
    header = "\n".join(line.format(path=path) for line in CARD_HEADER)
    body = header + "\n" + "\n".join(rows) + "\n"
    if not history:
        return body
    section = ["", "## Update History", ""]
    section.extend("- " + one for one in history)
    return body + "\n".join(section) + "\n"


def filler(count: int) -> str:
    return "\n".join(f"line{index} = {index}" for index in range(1, count + 1)) + "\n"


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

    def card(self, source_path: str, *rows: str, history: tuple[str, ...] = ()) -> Path:
        return self.write(
            self.onboarding,
            f"{source_path}.md",
            document(*rows, path=source_path, history=history),
        )

    def trees(self) -> Trees:
        return Trees(code_root=self.code, memory_root=self.memory)

    def fix(self, *, dry_run: bool = False, now: datetime = NOW) -> dict[str, Any]:
        with mock.patch.object(deterministic_projection, "now_utc", return_value=now):
            return fixer.fix_onboarding_root(self.onboarding, self.code, dry_run=dry_run)

    def check(self) -> dict[str, Any]:
        return range_resolution.check_onboarding_root(self.onboarding, self.code)

    def card_text(self, relative: str) -> str:
        return (self.onboarding / relative).read_text(encoding="utf-8")

    def card_bytes(self, relative: str) -> bytes:
        return (self.onboarding / relative).read_bytes()

    def row(self, relative: str, offset: int = 0) -> str:
        return self.card_text(relative).splitlines()[FIRST_ROW - 1 + offset]

    def source_cell(self, relative: str) -> str:
        return self.row(relative).split("|")[3].strip()


class TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = Tree(Path(self._tmp.name))

    def sources(self, result: dict[str, Any], index: int = 0) -> str:
        self.assertTrue(result["repairs"], result["declined"])
        return str(result["repairs"][index]["now"])

    def projection(self, result: dict[str, Any], index: int = 0) -> dict[str, Any]:
        self.assertTrue(result["projections"], result["declined"])
        return dict(result["projections"][index])

    def declined(self, result: dict[str, Any], index: int = 0) -> dict[str, Any]:
        self.assertTrue(result["declined"], result["repairs"])
        return dict(result["declined"][index])

    def assert_check_clean(self) -> None:
        result = self.tree.check()
        self.assertTrue(result["ok"], result["findings"])

    def sha(self, payload: str) -> str:
        return sha256(payload.encode("utf-8")).hexdigest()


class UniqueMoveProjectionTests(TreeCase):
    """The one generated class: an exact unique move, fully bound and atomic."""

    def plant_move(self) -> None:
        self.tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
        self.tree.card(
            "kernel/caller.py",
            "| The census freezes membership. | `build_route_indexes` "
            "| kernel/route_index.py:1-2 |",
            history=("2026-08-01T10:00:00+02:00: Previous entry.",),
        )

    def test_a_unique_move_projects_the_range_and_binds_every_packet_field(self) -> None:
        self.plant_move()

        result = self.tree.fix()

        self.assertEqual(result["claimsRepaired"], 1)
        self.assertEqual(result["declinedCount"], 0)
        self.assertEqual(result["projectionCount"], 1)
        p = self.projection(result)
        self.assertEqual(p["document"], "kernel/caller.py.md")
        self.assertEqual(p["line"], FIRST_ROW)
        self.assertEqual(p["anchors"], ["`build_route_indexes`"])
        self.assertEqual(p["was"], "kernel/route_index.py:1-2")
        self.assertEqual(p["now"], "kernel/indexes.py:1-2")
        self.assertEqual(
            p["resolvedExtents"],
            [
                {
                    "anchor": "`build_route_indexes`",
                    "path": "kernel/indexes.py",
                    "start": 1,
                    "end": 2,
                    "kind": "definition",
                }
            ],
        )
        self.assertEqual(p["priorClaimDigest"], self.sha("kernel/route_index.py:1-2"))
        self.assertEqual(p["repairToolVersion"], deterministic_projection.REPAIR_TOOL_VERSION)
        self.assertTrue(p["snapshotId"])
        self.assertEqual(
            p["newDocumentDigest"],
            sha256(self.tree.card_bytes("kernel/caller.py.md")).hexdigest(),
        )
        self.assertEqual(self.tree.source_cell("kernel/caller.py.md"), "kernel/indexes.py:1-2")
        self.assertIn("build_route_indexes", p["historyBullet"] or "")
        self.assert_check_clean()

    def test_the_history_bullet_is_newest_first_and_parseable_by_the_checker(self) -> None:
        self.plant_move()

        result = self.tree.fix()

        text = self.tree.card_text("kernel/caller.py.md")
        bullet = self.projection(result)["historyBullet"]
        self.assertIsNotNone(bullet)
        assert bullet is not None
        self.assertIn(bullet, text)
        self.assertLess(text.index(bullet), text.index("2026-08-01T10:00:00+02:00: Previous"))
        self.assertIn("No content impact:", bullet)
        self.assertIn("kernel/indexes.py:1-2", bullet)
        self.assertTrue(bullet.startswith("- 2026-09-03T12:00:00+00:00:"))
        stamp = parse_timestamp(bullet.lstrip("- "))
        self.assertIsNotNone(stamp)
        assert stamp is not None
        self.assertEqual(datetime_value(stamp), datetime(2026, 9, 3, 12, 0, 0))
        self.assert_check_clean()

    def test_a_dry_run_stages_the_projection_but_writes_nothing(self) -> None:
        self.plant_move()
        before = self.tree.card_text("kernel/caller.py.md")

        result = self.tree.fix(dry_run=True)

        p = self.projection(result)
        self.assertTrue(result["dryRun"])
        self.assertTrue(p["historyBullet"])
        self.assertEqual(self.tree.card_text("kernel/caller.py.md"), before)
        self.assertEqual(self.tree.source_cell("kernel/caller.py.md"), "kernel/route_index.py:1-2")

    def test_a_second_run_is_a_byte_for_byte_no_op(self) -> None:
        self.plant_move()
        self.tree.fix()
        after = self.tree.card_text("kernel/caller.py.md")

        again = self.tree.fix()

        self.assertEqual(again["claimsRepaired"], 0)
        self.assertEqual(again["projectionCount"], 0)
        self.assertEqual(again["documentsWritten"], 0)
        self.assertEqual(again["projections"], [])
        self.assertEqual(self.tree.card_text("kernel/caller.py.md"), after)

    def test_same_inputs_produce_the_same_projection_record(self) -> None:
        first = Tree(Path(self._tmp.name) / "one")
        second = Tree(Path(self._tmp.name) / "two")
        for tree in (first, second):
            tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
            tree.card(
                "kernel/caller.py",
                "| The census. | `build_route_indexes` | kernel/route_index.py:1-2 |",
            )

        left = first.fix()["projections"][0]
        right = second.fix()["projections"][0]

        self.assertEqual(left["now"], right["now"])
        self.assertEqual(left["snapshotId"], right["snapshotId"])
        self.assertEqual(left["priorClaimDigest"], right["priorClaimDigest"])
        self.assertEqual(left["newDocumentDigest"], right["newDocumentDigest"])
        self.assertEqual(left["at"], right["at"])
        self.assertEqual(left["repairToolVersion"], right["repairToolVersion"])

    def test_a_document_without_a_history_section_gets_a_bound_projection_but_no_bullet(
        self,
    ) -> None:
        self.tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
        self.tree.card(
            "kernel/caller.py",
            "| The census. | `build_route_indexes` | kernel/route_index.py:1-2 |",
        )

        result = self.tree.fix()

        p = self.projection(result)
        self.assertIsNone(p["historyBullet"])
        self.assertNotIn("## Update History", self.tree.card_text("kernel/caller.py.md"))
        self.assert_check_clean()

    def test_a_multi_anchor_claim_binds_every_resolved_extent(self) -> None:
        self.tree.source("kernel/left.py", "def persist():\n    return 1\n")
        self.tree.source("kernel/right.py", "def reload():\n    return 2\n")
        self.tree.card(
            "kernel/caller.py",
            "| Both halves. | `persist`; `reload` | kernel/left.py:2-2; kernel/right.py:2-2 |",
        )

        result = self.tree.fix()

        self.assertEqual(self.sources(result), "kernel/left.py:1-2; kernel/right.py:1-2")
        p = self.projection(result)
        self.assertEqual(len(p["resolvedExtents"]), 2)
        self.assertEqual(
            [(one["anchor"], one["path"]) for one in p["resolvedExtents"]],
            [
                ("`persist`", "kernel/left.py"),
                ("`reload`", "kernel/right.py"),
            ],
        )

    def test_an_in_file_tiebreaker_move_is_still_deterministically_projectable(self) -> None:
        """The cited range picks one of two same-name constructs; the chosen extent binds."""
        self.tree.source(
            "serving/live.py",
            "def emit():\n    return 1\n" + filler(20) + "def emit():\n    return 2\n",
        )
        self.tree.card("serving/caller.py", "| The emitter. | `emit` | serving/live.py:24-24 |")

        result = self.tree.fix()

        self.assertEqual(self.sources(result), "serving/live.py:23-24")
        p = self.projection(result)
        self.assertEqual(p["resolvedExtents"][0]["path"], "serving/live.py")
        self.assertEqual(p["resolvedExtents"][0]["start"], 23)
        self.assertEqual(p["resolvedExtents"][0]["end"], 24)
        self.assert_check_clean()

    def test_an_unparsed_language_with_one_occurrence_uses_the_existing_uniqueness_rule(
        self,
    ) -> None:
        """The packet boundary case: unparsed languages may resolve off a lone occurrence."""
        self.tree.source("dashboard/rail.css", ".persist {\n  color: red;\n}\n")
        self.tree.card(
            "dashboard/panel.tsx",
            "| The style rule. | `persist` | dashboard/rail.css:9-9 |",
        )

        result = self.tree.fix()

        self.assertEqual(self.sources(result), "dashboard/rail.css:1-1")
        p = self.projection(result)
        self.assertEqual(p["resolvedExtents"][0]["kind"], "occurrence")
        self.assertEqual(p["resolvedExtents"][0]["start"], 1)
        self.assertEqual(p["resolvedExtents"][0]["end"], 1)
        self.assert_check_clean()


class ProjectionRefusalTests(TreeCase):
    """Every packet refusal class: nothing is generated, nothing is guessed."""

    def assert_refused_without_projection(self, result: dict[str, Any]) -> None:
        self.assertEqual(result["claimsRepaired"], 0)
        self.assertEqual(result["projectionCount"], 0)
        self.assertEqual(result["projections"], [])

    def test_a_rename_is_refused_and_never_guesses_a_similar_name(self) -> None:
        self.tree.source(
            "controlplane/lifecycle.py",
            "def _require_command_lifecycle(command):\n    raise ValueError(command)\n",
        )
        self.tree.card(
            "controlplane/caller.py",
            "| The command palette maps its lifecycle. | `_map_command_lifecycle` "
            "| controlplane/lifecycle.py:1-2 |",
        )

        result = self.tree.fix()

        self.assert_refused_without_projection(result)
        declined = self.declined(result)
        self.assertEqual(declined["code"], repair.ANCHOR_ABSENT)
        self.assertNotIn("_require_command_lifecycle", declined["message"])
        self.assertEqual(
            self.tree.source_cell("controlplane/caller.py.md"), "controlplane/lifecycle.py:1-2"
        )

    def test_a_deletion_is_refused_without_accepting_the_old_range(self) -> None:
        self.tree.source("serving/live.py", "def kept():\n    return 1\n")
        self.tree.card(
            "serving/caller.py",
            "| The palette runs turn.stop. | `stop_turn` | serving/live.py:1-2 |",
        )

        result = self.tree.fix()

        self.assert_refused_without_projection(result)
        self.assertEqual(self.declined(result)["code"], repair.ANCHOR_ABSENT)
        self.assertEqual(self.tree.source_cell("serving/caller.py.md"), "serving/live.py:1-2")

    def test_multiple_definitions_are_refused_and_name_every_candidate(self) -> None:
        self.tree.source("serving/left.py", "def emit():\n    return 1\n")
        self.tree.source("serving/right.py", "def emit():\n    return 2\n")
        self.tree.card("serving/caller.py", "| The emitter. | `emit` | serving/gone.py:1-2 |")

        result = self.tree.fix()

        self.assert_refused_without_projection(result)
        declined = self.declined(result)
        self.assertEqual(declined["code"], repair.ANCHOR_AMBIGUOUS)
        self.assertIn("serving/left.py:1-2", declined["message"])
        self.assertIn("serving/right.py:1-2", declined["message"])

    def test_a_parsed_mention_only_anchor_is_refused_even_when_it_is_the_only_one(self) -> None:
        """The packet boundary: a parsed language with only a mention remains unresolved."""
        self.tree.source("kernel/notes.py", '"""The persist path was removed."""\n')
        self.tree.card("kernel/caller.py", "| The write path. | `persist` | kernel/gone.py:1-1 |")

        result = self.tree.fix()

        self.assert_refused_without_projection(result)
        declined = self.declined(result)
        self.assertIn(declined["code"], {repair.ANCHOR_ABSENT, repair.ANCHOR_AMBIGUOUS})
        self.assertIn("kernel/notes.py", declined["message"])
        self.assertEqual(self.tree.source_cell("kernel/caller.py.md"), "kernel/gone.py:1-1")

    def test_a_malformed_claim_is_not_candidate_for_projection(self) -> None:
        self.tree.source("kernel/store.py", "def persist():\n    return 1\n")
        self.tree.card(
            "kernel/caller.py",
            "| The write path. | `persist` | not a citation at all |",
        )

        result = self.tree.fix()

        self.assertEqual(result["projectionCount"], 0)
        self.assertEqual(result["claimsRepaired"], 0)
        self.assertEqual(result["declinedCount"], 0)
        self.assertGreater(result["findingsRemaining"], 0)

    def test_a_stale_snapshot_refuses_the_whole_run_before_any_write(self) -> None:
        self.tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
        self.tree.card(
            "kernel/caller.py",
            "| The census. | `build_route_indexes` | kernel/route_index.py:1-2 |",
        )

        with self.assertRaises(source_index.SourceIndexError):
            fixer.fix_onboarding_root(
                self.tree.onboarding,
                self.tree.code,
                expected_snapshot="f" * 64,
            )

        self.assertEqual(self.tree.source_cell("kernel/caller.py.md"), "kernel/route_index.py:1-2")


class TransactionSeamTests(TreeCase):
    """The plan-then-stage seam the packet's open truth gap left to choose."""

    def test_a_conflicting_write_between_plan_and_stage_refuses_the_rewrite(self) -> None:
        self.tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
        card = self.tree.card(
            "kernel/caller.py",
            "| The census. | `build_route_indexes` | kernel/route_index.py:1-2 |",
        )

        with source_index.open_repository_index(self.tree.trees()) as index:
            lines = card.read_text(encoding="utf-8").split("\n")
            located, _wrapped = fixer.sites(lines)
            claim, site = located[0]
            seen = symbol_index.locate(claim.anchors, self.tree.trees(), index=index)
            outcome = repair.plan(claim, self.tree.trees(), fixer.Sources(), seen)
            assert isinstance(outcome, repair.Repair)
            projection = deterministic_projection.plan_projection(
                deterministic_projection.ProjectionRequest(
                    lines=lines,
                    site=site,
                    relative="kernel/caller.py.md",
                    claim=claim,
                    outcome=outcome,
                    index=index,
                    now=NOW,
                    history_line=deterministic_projection.history_section_line(lines),
                )
            )
            assert not isinstance(projection, deterministic_projection.ProjectionDecline)
            self.assertIsNone(projection.new_document_digest)

            changed = list(lines)
            changed[claim.line - 1] = changed[claim.line - 1].replace(
                "kernel/route_index.py:1-2", "kernel/route_index.py:400-480"
            )
            self.assertFalse(
                deterministic_projection.verify_unchanged(changed, site, projection.was)
            )
            declined = deterministic_projection.conflicting_write_decline(
                "kernel/caller.py.md", claim.line, claim.anchors[0].written
            )
            self.assertEqual(declined.code, deterministic_projection.PROJECTION_CONFLICT)

    def test_the_staged_range_edit_and_the_history_edit_land_in_one_batch(self) -> None:
        """Both edits share one document batch: no range rewrite without its bullet."""
        self.tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
        card = self.tree.card(
            "kernel/caller.py",
            "| The census. | `build_route_indexes` | kernel/route_index.py:1-2 |",
            history=("2026-08-01T10:00:00+02:00: Previous entry.",),
        )

        with source_index.open_repository_index(self.tree.trees()) as index:
            lines = card.read_text(encoding="utf-8").split("\n")
            located, _wrapped = fixer.sites(lines)
            claim, site = located[0]
            seen = symbol_index.locate(claim.anchors, self.tree.trees(), index=index)
            outcome = repair.plan(claim, self.tree.trees(), fixer.Sources(), seen)
            assert isinstance(outcome, repair.Repair)
            heading = deterministic_projection.history_section_line(lines)
            projection = deterministic_projection.plan_projection(
                deterministic_projection.ProjectionRequest(
                    lines=lines,
                    site=site,
                    relative="kernel/caller.py.md",
                    claim=claim,
                    outcome=outcome,
                    index=index,
                    now=NOW,
                    history_line=heading,
                )
            )
            assert not isinstance(projection, deterministic_projection.ProjectionDecline)
            assert heading is not None and projection.history_bullet is not None
            batch: list[tuple[Any, str]] = [(site, projection.now)]
            batch.append(
                deterministic_projection.history_edit(lines, heading, [projection.history_bullet])
            )
            digest = deterministic_projection.document_digest(lines, batch)
            row_only = deterministic_projection.document_digest(lines, [(site, projection.now)])
            self.assertNotEqual(digest, row_only)
            self.assertIn(projection.history_bullet, "\n".join(batch_rendered(lines, batch)))


def batch_rendered(lines: list[str], batch: list[tuple[Any, str]]) -> list[str]:
    return rewritten(lines, batch)


class ProjectionBoundaryTests(TreeCase):
    """plan_projection's one refusal inside a live repair: the anchor must resolve
    to exactly ONE extent in the repair's own oracle.

    The single guard covers both degenerate counts: a repair that placed the anchor in
    two cited files at once, and a repair whose location list carries no match for the
    anchor at all. Neither can be bound to a deterministic range, so each declines with
    the empty-projection code instead of guessing.
    """

    def request(
        self,
        anchor: model.Anchor,
        locations: tuple[repair.ResolvedLocation, ...],
        was: str = "kernel/left.py:1-2; kernel/right.py:99-99",
    ) -> deterministic_projection.ProjectionRequest:
        row = f"| The write path. | `{anchor.text}` | {was} |"
        start = row.index(was)
        lines = [row]
        claim = model.Claim(
            line=1,
            anchors=(anchor,),
            citations=(),
            malformed=(),
            unchecked_spans=0,
        )
        outcome = repair.Repair(
            sources=tuple(f"{one.path}:{one.extent.start}-{one.extent.end}" for one in locations)
            or ("kernel/left.py:1-2", "kernel/right.py:1-2"),
            locations=locations,
        )
        index = mock.Mock(snapshot_id="c" * 64)
        return deterministic_projection.ProjectionRequest(
            lines=lines,
            site=Site(line=1, start=start, end=start + len(was)),
            relative="kernel/caller.py.md",
            claim=claim,
            outcome=outcome,
            index=index,
            now=NOW,
            history_line=None,
        )

    def test_an_anchor_placed_in_two_cited_files_declines_the_empty_projection(self) -> None:
        anchor = model.Anchor(kind=model.SYMBOL, text="persist")
        declined = deterministic_projection.plan_projection(
            self.request(
                anchor,
                locations=(
                    repair.ResolvedLocation(
                        anchor=anchor,
                        path="kernel/left.py",
                        extent=extents.Extent(start=1, end=2, kind="definition"),
                    ),
                    repair.ResolvedLocation(
                        anchor=anchor,
                        path="kernel/right.py",
                        extent=extents.Extent(start=1, end=2, kind="definition"),
                    ),
                ),
            )
        )
        self.assertIsInstance(declined, deterministic_projection.ProjectionDecline)
        assert isinstance(declined, deterministic_projection.ProjectionDecline)
        self.assertEqual(declined.code, deterministic_projection.PROJECTION_EMPTY)
        self.assertEqual(declined.anchor, "`persist`")
        self.assertIn("2 extent(s)", declined.message)

    def test_a_repair_with_no_matching_extent_for_the_anchor_declines_too(self) -> None:
        anchor = model.Anchor(kind=model.SYMBOL, text="persist")
        declined = deterministic_projection.plan_projection(self.request(anchor, locations=()))
        self.assertIsInstance(declined, deterministic_projection.ProjectionDecline)
        assert isinstance(declined, deterministic_projection.ProjectionDecline)
        self.assertEqual(declined.code, deterministic_projection.PROJECTION_EMPTY)
        self.assertEqual(declined.anchor, "`persist`")
        self.assertIn("0 extent(s)", declined.message)


class ProjectionDeclineThroughFixerTests(TreeCase):
    """The decline surface seen by a full fix_onboarding_root run.

    A claim whose one anchor is repaired out of TWO cited files at once has no unique
    extent to bind: the source cell still gets the mechanically generated ranges, but the
    projection transaction refuses and reports the refusal the way every other refused
    claim is reported.
    """

    def test_a_two_file_anchor_resolution_stages_the_edit_and_refuses_the_projection(
        self,
    ) -> None:
        self.tree.source("kernel/left.py", "def persist():\n    return 1\n")
        self.tree.source("kernel/right.py", "def persist():\n    return 2\n")
        self.tree.card(
            "kernel/caller.py",
            "| The write path. | `persist` | kernel/left.py:1-2; kernel/right.py:99-99 |",
        )

        result = self.tree.fix()

        self.assertEqual(result["claimsRepaired"], 1)
        self.assertEqual(result["projectionCount"], 0)
        self.assertEqual(result["projections"], [])
        self.assertEqual(result["declinedCount"], 1)
        declined = self.declined(result)
        self.assertEqual(declined["code"], deterministic_projection.PROJECTION_EMPTY)
        self.assertEqual(declined["anchor"], "`persist`")
        self.assertEqual(declined["line"], FIRST_ROW)
        self.assertIn("2 extent(s)", declined["message"])
        self.assertEqual(
            self.tree.source_cell("kernel/caller.py.md"),
            "kernel/left.py:1-2; kernel/right.py:1-2",
        )
        self.assertFalse(result["ok"])


class StagingGuardTests(TreeCase):
    """The run-level staging seams: one shared stamp, one digest per document batch."""

    def plant_move(self) -> None:
        self.tree.source("kernel/indexes.py", "def build_route_indexes():\n    return 1\n")
        self.tree.card(
            "kernel/caller.py",
            "| The census. | `build_route_indexes` | kernel/route_index.py:1-2 |",
            history=("2026-08-01T10:00:00+02:00: Previous entry.",),
        )

    def test_a_preexisting_stamp_is_kept_without_reconsulting_the_clock(self) -> None:
        """The stamp guard's second arm: when the run already carries a stamp, the clock
        is never consulted again and every projection binds that one shared instant."""
        self.plant_move()
        stamped = fixer.Staging(stamp=NOW)

        with (
            mock.patch.object(deterministic_projection, "now_utc") as clock,
            mock.patch.object(fixer, "Staging", return_value=stamped),
        ):
            result = fixer.fix_onboarding_root(self.tree.onboarding, self.tree.code)

        clock.assert_not_called()
        p = self.projection(result)
        self.assertEqual(p["at"], NOW.astimezone(UTC).isoformat(timespec="seconds"))
        bullet = p["historyBullet"]
        self.assertIsNotNone(bullet)
        assert bullet is not None
        self.assertTrue(bullet.startswith("- 2026-09-03T12:00:00+00:00:"))
        self.assert_check_clean()

    def test_each_document_batch_digest_binds_only_its_own_projection(self) -> None:
        """Two documents, two projections: the digest pass must walk past the OTHER
        document's projection and bind each newDocumentDigest to its own batch bytes."""
        self.tree.source("kernel/alpha.py", "def alpha():\n    return 1\n")
        self.tree.source("kernel/beta.py", "def beta():\n    return 2\n")
        self.tree.card(
            "kernel/first.py",
            "| The alpha census. | `alpha` | kernel/old_alpha.py:1-2 |",
            history=("2026-08-01T10:00:00+02:00: Previous entry.",),
        )
        self.tree.card(
            "kernel/second.py",
            "| The beta census. | `beta` | kernel/old_beta.py:1-2 |",
            history=("2026-08-01T10:00:00+02:00: Previous entry.",),
        )

        result = self.tree.fix()

        self.assertEqual(result["claimsRepaired"], 2)
        self.assertEqual(result["projectionCount"], 2)
        self.assertEqual(result["documentsWritten"], 2)
        bound = {
            p["document"]: (
                p["newDocumentDigest"],
                self.sha(self.tree.card_text(p["document"])),
            )
            for p in result["projections"]
        }
        self.assertEqual(len(bound), 2)
        for document, (digest, actual) in bound.items():
            self.assertIsNotNone(digest)
            self.assertEqual(digest, actual, document)
        self.assert_check_clean()


class ClockContractTests(unittest.TestCase):
    """The injectable timestamp source really returns a UTC-aware instant.

    fix_onboarding_root routes its whole run through deterministic_projection.now_utc
    unless a stamp is already carried, so the real clock body is production behavior and
    is exercised here without a patch.
    """

    def test_the_injectable_clock_returns_a_utc_aware_instant(self) -> None:
        stamp = deterministic_projection.now_utc()
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.astimezone(UTC).isoformat(timespec="seconds")[-6:], "+00:00")


class ScopedNormalisationEditTests(unittest.TestCase):
    """The non-repairing arm of _decide: a passing claim that normalisation rewrites
    stages its edit and returns before any projection transaction is opened."""

    def test_a_normalised_passing_claim_stages_the_edit_and_skips_the_projection(
        self,
    ) -> None:
        was = "kernel/route_index.py:1-2"
        now = "kernel/indexes.py:1-2"
        lines = [f"| The census. | `build_route_indexes` | {was} |"]
        start = lines[0].index(was)
        site = Site(line=1, start=start, end=start + len(was))
        claim = model.Claim(
            line=1,
            anchors=(),
            citations=(),
            malformed=(),
            unchecked_spans=0,
        )
        one = fixer.Candidate(
            document=Path("kernel/caller.py.md"),
            relative="kernel/caller.py.md",
            claim=claim,
            site=site,
            repairing=False,
            gating=False,
        )
        walk = SimpleNamespace(
            documents=SimpleNamespace(lines=lambda _document: lines),
            result=SimpleNamespace(refused=[], applied=[]),
        )
        staging = fixer.Staging()
        with mock.patch.object(fixer, "_scoped_source", return_value=(now, None)):
            fixer._decide(one, None, cast(fixer.Walk, walk), staging, Path("/onboarding"))

        self.assertEqual(
            [(one.path, one.line, one.was, one.now) for one in walk.result.applied],
            [("kernel/caller.py.md", 1, was, now)],
        )
        self.assertEqual(walk.result.refused, [])
        self.assertEqual(staging.edits, {Path("kernel/caller.py.md"): [(site, now)]})
        self.assertEqual(staging.bullets, {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
