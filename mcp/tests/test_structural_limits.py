"""Tests for the function, class-surface, and directory structural caps.

Repository tests arm all three caps and validate the bounded ``layers.toml`` sequencing
declaration. Synthetic bites require complete offender lists. Known-good fixtures pin
decorators, overloads, properties, protocols, nested scopes, relocated-method
classification, external receivers, and exact limit boundaries.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

import pytest
from agents_remember_test_support.code_quality import structural_limits

pytestmark = pytest.mark.fitness

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = MCP_SRC / "agents_remember"
LAYERS_PATH = REPOSITORY_ROOT / structural_limits.LAYERS_FILE

FUNCTION_REMEDY = (
    "extract the cohesive steps into named helpers, or hoist a nested def to module "
    "level. A flat declaration block splits by grouping its declarations."
)
CLASS_REMEDY = (
    "the class has taken a second job; move the methods that belong to it into a "
    "collaborator, or make internal steps private. Moving them into a base class it "
    "inherits is not a split -- the surface is unchanged and only the measurement moves -- "
    "and moving them into a sibling module as free functions over the instance is not a "
    "split either; those are counted here as the methods they are. "
    "A sequencing deviation, if one is genuinely warranted, is declared in "
    f"{structural_limits.LAYERS_FILE} for the whole DIRECTORY under "
    f"[{structural_limits.SEQUENCING_TABLE}.<name>] with an owner, a date, the caps it "
    "departs from and the leaf that deletes it -- never for a named class, and never as "
    "an entry in this test."
)
DIRECTORY_REMEDY = (
    "split the directory into sub-packages. A sequencing deviation, if one is genuinely "
    f"warranted, is declared in {structural_limits.LAYERS_FILE} under "
    f"[{structural_limits.SEQUENCING_TABLE}.<name>] with an owner, a date, the caps it "
    "departs from and the leaf that deletes it -- never as an entry in this test."
)


# One well-formed `[sequencing.*]` entry, as TOML source. The declaration tests each remove
# or corrupt exactly one field of it, so "what a complete deviation looks like" is written
# once and every negative case is visibly a single deviation from it.
WELL_FORMED_DEVIATION = {
    "directory": '"drawer/"',
    "limits": '["directory_modules"]',
    "declared_on": '"2026-08-01"',
    "owner": '"someone"',
    "deleted_by": '"260731-EFA-L12"',
}


def deviation(directory: str, *limits: str) -> structural_limits.DirectoryDeviation:
    """A complete deviation for a fixture package, so no test builds a half-formed one."""
    return structural_limits.DirectoryDeviation(
        name=f"{directory}_size",
        directory=directory,
        declared_on="2026-08-01",
        owner="someone",
        deleted_by="260731-EFA-L12",
        limits=limits or (structural_limits.DIRECTORY_MODULES,),
    )


def write_package(root: Path, modules: dict[str, str]) -> Path:
    """A throwaway package shaped like this one, from ``{relative path: source}``."""
    package = root / "sample_package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for relative, source in modules.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return package


def function_of_length(name: str, body_lines: int) -> str:
    """A function whose measured length is exactly ``body_lines`` + 1."""
    body = "\n".join(f"    value_{index} = {index}" for index in range(body_lines))
    return f"def {name}() -> None:\n{body}\n"


def class_with_public_methods(name: str, count: int) -> str:
    methods = "\n".join(
        f"    def method_{index}(self) -> int:\n        return {index}\n" for index in range(count)
    )
    return f"class {name}:\n{methods}\n"


def relocated_parser(*, methods: int, steps: int, private_steps: bool = False) -> dict[str, str]:
    """A state machine and a sibling module of free functions that drive its cursor.

    The shape 260731-EFA-L6 built to get ``_MarkdownSettingsParser`` from 31 public methods
    under the cap, and then deleted: each step takes the parser as an UNANNOTATED first
    parameter and writes the same two cursor fields the methods wrote. ``private_steps``
    spells the sibling functions with a leading underscore, which is the same honest
    remedy as a ``_``-prefixed method.
    """
    prefix = "_" if private_steps else ""
    body = "\n".join(
        f"    def method_{index}(self) -> None:\n        self.current_rule = {index}\n"
        for index in range(methods)
    )
    sibling = "\n".join(
        f"def {prefix}step_{index}(parser, line):\n"
        f"    parser.current_rule = line\n"
        f"    parser.current_list = None\n"
        for index in range(steps)
    )
    return {
        "parser.py": f"class Parser:\n    current_rule = None\n    current_list = None\n\n{body}",
        "parser_steps.py": sibling,
    }


class ClassSurfaceTests(unittest.TestCase):
    """A class's public surface is its declared, non-underscore method names."""

    def test_a_wide_class_is_reported_with_its_measured_surface(self) -> None:
        source = class_with_public_methods("Wide", 16)

        offenders = [
            offender
            for offender in structural_limits.measure_classes(source, display_path="wide.py")
            if offender.measured > structural_limits.CLASS_PUBLIC_METHOD_LIMIT
        ]

        self.assertEqual([offender.name for offender in offenders], ["Wide"])
        self.assertEqual(offenders[0].measured, 16)
        self.assertEqual(offenders[0].excess, 1)


class RelocationTests(unittest.TestCase):
    """Moving a method to the next file does not remove it from the class's surface.

    A cap satisfiable by relocation teaches relocation, and this leaf demonstrated it: the
    first attempt at ``_MarkdownSettingsParser`` moved fifteen methods into two sibling
    modules as ``def step(parser, ...)``, which lowered the measured number to 13 and
    changed nothing about the class -- the same names, the same cursor fields, the same
    single caller, and pyright checking ``Any`` where it had checked the parser. Measured
    by the rule below, that tree reports the parser at 22.
    """

    def test_moving_methods_into_a_sibling_module_does_not_lower_the_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = write_package(Path(tmp), relocated_parser(methods=8, steps=10))

            offenders = structural_limits.wide_classes(package)
            message = structural_limits.render_offenders("class(es)", offenders, CLASS_REMEDY)

            self.assertEqual([offender.name for offender in offenders], ["Parser"])
            self.assertEqual(offenders[0].measured, 18)
            self.assertIn("parser.py:1 Parser", message)


class KnownGoodConstructTests(unittest.TestCase):
    """Constructs this repository contains that the checks must never flag.

    Every entry here is a shape that a naive implementation gets wrong, and each is named
    in ``structural_limits``'s own docstring so the false-positive reasoning travels with
    the checker rather than living only in its tests.
    """

    def public_names(self, source: str) -> set[str]:
        """The public surface of the fixture's one TOP-LEVEL class.

        Top-level rather than "the only class in the file": a nested-class fixture has two,
        and charging the inner one's methods to the outer is the very thing being ruled out.
        """
        tree = ast.parse(textwrap.dedent(source))
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        self.assertEqual(len(classes), 1, "fixture must declare exactly one top-level class")
        return structural_limits.public_method_names(classes[0])

    def measure(self, source: str) -> dict[str, int]:
        measured = structural_limits.measure_functions(
            textwrap.dedent(source), display_path="fixture.py"
        )
        return {offender.name: offender.measured for offender in measured}

    def test_a_property_and_its_setter_count_once(self) -> None:
        names = self.public_names(
            """
            class Subject:
                @property
                def value(self) -> int:
                    return self._value

                @value.setter
                def value(self, incoming: int) -> None:
                    self._value = incoming
            """
        )

        self.assertEqual(names, {"value"})

    def test_typing_overloads_count_once(self) -> None:
        names = self.public_names(
            """
            class Subject:
                @overload
                def read(self, key: str) -> str: ...

                @overload
                def read(self, key: int) -> int: ...

                def read(self, key: str | int) -> str | int:
                    return key
            """
        )

        self.assertEqual(names, {"read"})


class ProbeTests(unittest.TestCase):
    """Each check, shown rejecting a deliberate violation (R16).

    A check that has never rejected anything is indistinguishable from one that cannot, and
    a check that reports one offender at a time turns a batch fix into an iteration loop.
    Both properties are asserted here against a throwaway package.
    """

    def test_the_function_length_check_reports_every_offender_not_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            over = structural_limits.FUNCTION_LINE_LIMIT + 20
            package = write_package(
                Path(tmp),
                {
                    "first.py": function_of_length("first_offender", over),
                    "second.py": function_of_length("second_offender", over + 10),
                    "innocent.py": function_of_length("innocent", 3),
                },
            )

            offenders = structural_limits.long_functions(package)
            message = structural_limits.render_offenders("function(s)", offenders, FUNCTION_REMEDY)

            self.assertEqual(
                [offender.name for offender in offenders],
                ["second_offender", "first_offender"],
            )
            self.assertIn("2 function(s) over the limit", message)
            self.assertIn("first.py:1 first_offender", message)
            self.assertIn("second.py:1 second_offender", message)
            self.assertNotIn("innocent", message)
            self.assertIn(FUNCTION_REMEDY, message)

    def test_the_directory_check_rejects_a_crowded_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            over = structural_limits.DIRECTORY_MODULE_LIMIT + 5
            package = write_package(
                Path(tmp), {f"drawer/module_{index}.py": "" for index in range(over)}
            )

            offenders = structural_limits.crowded_directories(package)
            message = structural_limits.render_offenders(
                "director(ies)", offenders, DIRECTORY_REMEDY
            )

            self.assertEqual([offender.name for offender in offenders], ["drawer"])
            self.assertEqual(offenders[0].measured, over)
            self.assertIn("1 director(ies) over the limit", message)
            # A directory has no line to point at, so the location is the directory itself.
            self.assertIn("  30 (limit 25, +5)  drawer drawer", message)
            self.assertIn(DIRECTORY_REMEDY, message)

    def test_a_declared_deviation_silences_exactly_the_directory_it_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            over = structural_limits.DIRECTORY_MODULE_LIMIT + 5
            package = write_package(
                Path(tmp),
                {f"declared/module_{index}.py": "" for index in range(over)}
                | {f"undeclared/module_{index}.py": "" for index in range(over)},
            )

            offenders = structural_limits.crowded_directories(
                package, deviations=[deviation("declared", structural_limits.DIRECTORY_MODULES)]
            )

            self.assertEqual([offender.name for offender in offenders], ["undeclared"])


if __name__ == "__main__":
    unittest.main()
