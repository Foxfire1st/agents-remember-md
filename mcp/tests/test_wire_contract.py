"""Small input/output checks for mutations escaping the modeled wire boundary."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

import pytest
from agents_remember_test_support.code_quality import wire_contract

pytestmark = pytest.mark.fitness

PACKAGE_ROOT = MCP_SRC / "agents_remember"


def _wire(source: str) -> list[str]:
    """What the rule reports for a single-module ``source``, as ``line [form]`` strings."""
    tree = ast.parse(source)
    trees = {"fixture.py": tree}
    return [
        f"{offender.line} [{offender.form}]"
        for offender in wire_contract.module_mutation_offenders(
            tree,
            "fixture.py",
            producers=wire_contract.dump_returning_names(trees),
            validators=wire_contract.validating_names(trees),
        )
    ]


class FunctionBoundaryTests(unittest.TestCase):
    """Detect dump-derived values returned across a function boundary."""

    def test_a_function_returning_a_dump_makes_its_callers_taint_sources(self) -> None:
        source = (
            "def build(model):\n"
            "    return model.model_dump()\n"
            "\n"
            "def serve(model):\n"
            "    body = build(model)\n"
            "    body['extra'] = 1\n"
            "    return body\n"
        )
        self.assertEqual(_wire(source), ["6 [post-dump mutation]"])


class WireSweepReachTests(unittest.TestCase):
    """Every mutation and laundering form the rule claims to catch."""

    def test_a_plain_key_assignment_is_caught(self) -> None:
        source = "def f(m):\n    p = m.model_dump()\n    p['x'] = 1\n    return p\n"
        self.assertEqual(_wire(source), ["3 [post-dump mutation]"])

    def test_a_copy_does_not_launder_the_dump(self) -> None:
        # Mutating a copy of a dumped body ships exactly the same undeclared key.
        for form in ("dict(m.model_dump())", "m.model_dump().copy()", "{**m.model_dump()}"):
            source = f"def f(m):\n    p = {form}\n    p['x'] = 1\n    return p\n"
            self.assertEqual(_wire(source), ["3 [post-dump mutation]"], form)


class WireSweepFalsePositiveTests(unittest.TestCase):
    """Known-good constructs the package really contains. None of these may be reported."""

    def test_setting_fields_on_the_model_before_the_dump_is_the_sanctioned_pattern(self) -> None:
        # mcp/tools/base.py: the choke point writes nextStep/agentNotifierBanner onto the
        # MODEL, then dumps once. This is what the remediation asks for.
        source = (
            "def f(model):\n"
            "    model.nextStep = step\n"
            "    model.agentNotifierBanner = banner\n"
            "    return model.model_dump(mode='json', exclude_none=True)\n"
        )
        self.assertEqual(_wire(source), [])

    def test_reading_a_dumped_dict_is_not_mutating_it(self) -> None:
        source = (
            "def f(m):\n"
            "    p = m.model_dump()\n"
            "    value = p['x']\n"
            "    keys = sorted(p)\n"
            "    other = {**p, 'extra': 1}\n"
            "    return value, keys, other\n"
        )
        self.assertEqual(_wire(source), [])


if __name__ == "__main__":
    unittest.main()
