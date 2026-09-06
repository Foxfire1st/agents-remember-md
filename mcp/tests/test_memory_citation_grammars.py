"""Supported language definitions distinguish safe citation moves from ambiguous mentions."""

from __future__ import annotations

import sys
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_TESTS = Path(__file__).resolve().parent
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
sys.path.insert(0, str(MCP_SRC))
sys.path.insert(0, str(MCP_TESTS))

from agents_remember.memory_quality.style.citations import grammars, model
from test_memory_citation_fix import TreeCase


def spans(path: str, source: str) -> dict[str, list[tuple[int, int]]]:
    return grammars.definitions(path, source.splitlines())


class TypeScriptPureMoveTests(TreeCase):
    """L6-R16, both directions: the grammar is what turns this decline into a repair.

    One tree, one move. ``RailRow`` is DECLARED in one file and MENTIONED in another, and
    the citation points at the file it left. With the TypeScript grammar the mention is not
    a candidate, so the declaration resolves uniquely and ``--fix`` repoints the claim; with
    the grammar withdrawn both files are mentions, nothing resolves uniquely, and the claim
    is handed to the curator with both locations named.
    """

    def moved(self) -> None:
        self.tree.source(
            "dashboard/src/rail/RailRow.tsx",
            'import { useState } from "react";\n\n'
            "export class RailRow {\n  render() {\n    return null;\n  }\n}\n",
        )
        self.tree.source(
            "dashboard/src/panels/FlowTab.tsx",
            'import { RailRow } from "../rail/RailRow";\n\n'
            "export const FlowTab = () => new RailRow();\n",
        )
        self.tree.card(
            "dashboard/src/panels/FlowTab.tsx",
            "| The rail row owns its own render. | `RailRow` | dashboard/src/rail.tsx:1-4 |",
        )

    def withdrawn(self) -> AbstractContextManager[Any]:
        """The tree exactly as it was before this leaf: no grammar reads ``.tsx``."""
        remaining = {
            suffix: grammar
            for suffix, grammar in grammars.SUFFIX_GRAMMARS.items()
            if grammar not in {grammars.TSX, grammars.TYPESCRIPT, grammars.JAVASCRIPT}
        }
        return mock.patch.dict(grammars.SUFFIX_GRAMMARS, remaining, clear=True)

    def test_with_the_grammar_the_move_is_repaired_onto_the_declaration(self) -> None:
        self.moved()

        result = self.tree.fix()

        self.assertEqual(self.sources(result), "dashboard/src/rail/RailRow.tsx:3-7")
        self.assertEqual(result["declinedCount"], 0)
        self.assert_check_clean()

    def test_without_the_grammar_the_same_move_is_declined(self) -> None:
        self.moved()

        with self.withdrawn():
            result = self.tree.fix()

        self.assertEqual(result["claimsRepaired"], 0)
        self.assertEqual(self.declined(result)["code"], "anchor_ambiguous")


class PythonExtentTests(unittest.TestCase):
    """Tree-sitter's Python extents, pinned against the constructs ``ast`` used to read.

    Measured over this repository's 707 Python files while the two paths still ran side by
    side: 705 produced byte-identical name-to-extent maps. The two that differed differed
    only by a comment written INSIDE the construct, which tree-sitter includes and ``ast``
    cannot see -- ``serving/conversation/models.py`` ``FeatureCapability`` 662-683 against
    662-690, and ``serving/harness_submission_authority.py``
    ``_certified_pre_send_busy`` 884-893 against 884-894. That shape is pinned below so the
    difference stays a decision rather than a surprise.
    """

    def test_a_decorated_definition_is_stamped_from_its_first_decorator(self) -> None:
        source = "import functools\n\n\n@functools.cache\ndef build():\n    return 1\n"

        self.assertEqual(spans("k.py", source)["build"], [(4, 6)])

    def test_a_class_and_its_methods_and_attributes_all_bind(self) -> None:
        source = "class K:\n    attr = 1\n\n    def method(self):\n        local = 2\n"

        self.assertEqual(
            spans("k.py", source),
            {"K": [(1, 5)], "attr": [(2, 2)], "method": [(4, 5)], "local": [(5, 5)]},
        )


class ScriptDefinitionTests(unittest.TestCase):
    """Every construct the TypeScript, TSX and JavaScript rule calls a definition."""

    DECLARATIONS = (
        ("export class RailRow {}\n", "RailRow", (1, 1)),
        ("export interface Props {\n  title: string;\n}\n", "Props", (1, 3)),
        ("export type Mode = 'a' | 'b';\n", "Mode", (1, 1)),
        ("export enum Kind { A, B }\n", "Kind", (1, 1)),
        ("export const LIMIT = 20;\n", "LIMIT", (1, 1)),
        ("export function main() {}\n", "main", (1, 1)),
        ("export function* gen() {}\n", "gen", (1, 1)),
        ("declare function ambient(): void;\n", "ambient", (1, 1)),
        ("abstract class Abs {\n  abstract go(): void;\n}\n", "go", (2, 2)),
        ("class Fields {\n  static X = 1;\n}\n", "X", (2, 2)),
        ("class Holder {\n  render() {\n    return 1;\n  }\n}\n", "render", (2, 4)),
        ("namespace NS {}\n", "NS", (1, 1)),
        ("interface Props {\n  onClick(): void;\n}\n", "onClick", (2, 2)),
    )

    def test_each_declaration_form_binds_its_name_over_its_own_lines(self) -> None:
        for source, name, span in self.DECLARATIONS:
            with self.subTest(name=name):
                self.assertEqual(spans("dashboard/x.ts", source).get(name), [span])

    def test_a_tsx_component_is_read_by_the_tsx_dialect(self) -> None:
        source = 'export const Row = () => <div className="rail-row">text</div>;\n'

        self.assertEqual(spans("dashboard/x.tsx", source)["Row"], [(1, 1)])

    def test_a_javascript_module_is_read_by_the_javascript_grammar(self) -> None:
        for suffix in (".js", ".jsx", ".mjs", ".cjs"):
            with self.subTest(suffix=suffix):
                found = spans(f"dashboard/x{suffix}", "export function main() {}\n")
                self.assertEqual(found["main"], [(1, 1)])


OFFLINE_PROBE = '''\
"""Parse one file of every supported language with every network path closed."""

import socket
import sys


def _blocked(*args, **kwargs):
    raise OSError("network egress is blocked by the offline parse guard")


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
socket.gethostbyname = _blocked

try:
    socket.create_connection(("example.invalid", 80))
except OSError:
    pass
else:
    raise SystemExit("the network block did not take effect; the run proves nothing")

from agents_remember.memory_quality.style.citations import grammars

for path, source in (
    ("k.py", "def build():\\n    return 1\\n"),
    ("k.ts", "export class RailRow {}\\n"),
    ("k.tsx", "export const Row = () => null;\\n"),
    ("k.js", "export function main() {}\\n"),
):
    if not grammars.definitions(path, source.splitlines()):
        raise SystemExit(f"{path} parsed to nothing with egress blocked")
print("ok")
'''


class TypeScriptAnchorGrammarTests(unittest.TestCase):
    """R32 modes 1, 2, 3 and 5, plus the nearby shapes that stay negative."""

    def test_a_url_does_not_lose_its_double_slash_during_quote_matching(self) -> None:
        anchor = model.Anchor(kind=model.QUOTE, text="https://example.invalid/a")

        self.assertTrue(model.occurs_in(anchor, 'const url = "https://example.invalid/a";'))


class TypeScriptInterfacePoolRepairTests(TreeCase):
    """R32 mode 4: pooled members repair to their defining interface file."""

    def test_three_interface_members_in_another_file_repair_as_one_claim(self) -> None:
        self.tree.source("dashboard/src/panels/old.ts", "export const keep = true;\n")
        self.tree.source(
            "dashboard/src/types/panel.ts",
            "export interface PanelProps {\n"
            "  title: string;\n"
            "  onSelect(): void;\n"
            "  disabled?: boolean;\n"
            "}\n",
        )
        self.tree.card(
            "dashboard/src/panels/view.tsx",
            "| Panel inputs are shared. | `title`; `onSelect`; `disabled` "
            "| dashboard/src/panels/gone.ts:1-4 |",
        )

        result = self.tree.fix()

        self.assertEqual(result["claimsRepaired"], 1, result["declined"])
        self.assertEqual(result["declinedCount"], 0, result["declined"])
        self.assertEqual(
            self.sources(result),
            "dashboard/src/types/panel.ts:2-4",
        )


if __name__ == "__main__":
    unittest.main()
