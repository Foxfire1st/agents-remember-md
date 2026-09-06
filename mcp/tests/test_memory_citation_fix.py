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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
