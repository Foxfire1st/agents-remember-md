from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from typing import TypeGuard

import pytest
from _quality_admission import QUALITY_TEST_ADMISSION
from _ruff_repository_evidence import (
    REPOSITORY_ROOT,
    ruff_lint_configuration,
    run_ruff_over_tracked_python,
    run_ruff_with_repository_configuration,
)
from agents_remember_test_support.testing.dagger_admission import (
    require_dagger_admission_capability,
)

ARGUMENT_COUNT_RULE = "PLR0913"
# The one path exempt from PLR0913, spelled twice on purpose. The pattern is what
# `pyproject.toml` must say verbatim; the directory is what that pattern must resolve to.
# Widening the exemption has to defeat both, and the tests still walk whatever the
# pyproject pattern actually matches rather than what these constants claim.
TOOL_DECLARATION_DIRECTORY = "mcp/src/agents_remember/mcp/registration"
TOOL_DECLARATION_PATTERN = "mcp/src/agents_remember/mcp/registration/*.py"


class ToolSignatureExemptionTests(unittest.TestCase):
    """PLR0913's one exemption covers published MCP tool declarations and nothing else.

    ``mcp/src/agents_remember/mcp/registration/`` is exempt because FastMCP derives each
    tool's published JSON input schema from the Python signature, so collapsing a parameter
    list into an object is a breaking wire change rather than a refactor. That reason holds
    only for `@server.tool()` declarations. These tests stop the exemption from becoming a
    place to park ordinary code: the moment a plain function appears under that path, or a
    second path is exempted, or the pattern is widened, one of them fails.
    """

    def setUp(self) -> None:
        require_dagger_admission_capability(QUALITY_TEST_ADMISSION)

    def test_plr0913_is_armed_and_nothing_globally_ignores_it(self) -> None:
        lint = ruff_lint_configuration()

        self.assertIn("PL", set(lint.get("select", [])))
        self.assertNotIn(ARGUMENT_COUNT_RULE, set(lint.get("ignore", [])))
        self.assertNotIn("max-args", lint.get("pylint", {}))

    def test_the_registration_modules_are_the_only_path_exempt_from_plr0913(self) -> None:
        exempted = {
            pattern
            for pattern, codes in ruff_lint_configuration().get("per-file-ignores", {}).items()
            if ARGUMENT_COUNT_RULE in codes
        }

        self.assertEqual(exempted, {TOOL_DECLARATION_PATTERN})

    @pytest.mark.integration
    def test_every_function_in_the_exempted_path_is_a_published_tool_declaration(self) -> None:
        modules = exempted_tool_modules()
        self.assertEqual(
            modules,
            sorted((REPOSITORY_ROOT / TOOL_DECLARATION_DIRECTORY).glob("*.py")),
            "the exemption pattern no longer matches exactly the tool declaration modules",
        )

        for module in modules:
            with self.subTest(module=module.name):
                self.assertEqual(ordinary_code_in_tool_module(module), [])

    @pytest.mark.integration
    def test_no_suppression_directive_in_the_tree_holds_an_argument_count_finding_down(
        self,
    ) -> None:
        completed = run_ruff_over_tracked_python("--ignore-noqa", "--select", ARGUMENT_COUNT_RULE)

        self.assertEqual(completed.returncode, 0, completed.stdout)

    def test_ruff_rejects_a_seven_parameter_function_at_this_repository_configuration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "seven_parameters.py"
            source.write_text(seven_parameter_function(), encoding="utf-8")

            completed = run_ruff_with_repository_configuration(source)

            self.assertEqual(completed.returncode, 1, completed.stdout)
            reported = {entry["code"] for entry in json.loads(completed.stdout)}
            self.assertIn(ARGUMENT_COUNT_RULE, reported)


def exempted_tool_modules() -> list[Path]:
    """Every file the PLR0913 per-file-ignore actually reaches."""
    patterns = [
        pattern
        for pattern, codes in ruff_lint_configuration().get("per-file-ignores", {}).items()
        if ARGUMENT_COUNT_RULE in codes
    ]
    return sorted({match for pattern in patterns for match in REPOSITORY_ROOT.glob(pattern)})


# 260731-EFA-L7 R10: unchanged structural branches are exercised by the enclosing walk.
def ordinary_code_in_tool_module(module: Path) -> list[str]:  # pragma: no cover
    """Return exempted module nodes that are not published tool declarations or registrars."""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    registrars = {node for node in tree.body if is_tool_registrar(node)}
    registrar_names = {registrar.name for registrar in registrars}
    findings: list[str] = []
    for node in ast.walk(tree):
        where = f"{module.name}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.ClassDef):
            findings.append(f"{where} class {node.name}")
        elif isinstance(node, ast.Lambda):
            findings.append(f"{where} lambda")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node in registrars:
                findings.extend(registrar_body_findings(node, registrar_names, module.name))
            elif not any(is_server_tool_decorator(decorator) for decorator in node.decorator_list):
                findings.append(f"{where} function {node.name} is not a @server.tool()")
    return findings


def is_tool_registrar(node: ast.stmt) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    """A module-level, undecorated ``[_]register_<something>_tools`` host."""
    return (
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.lstrip("_").startswith("register_")
        and node.name.endswith("_tools")
        and not node.decorator_list
    )


# 260731-EFA-L7 R10: unchanged structural branches are exercised by the enclosing walk.
def registrar_body_findings(  # pragma: no cover
    registrar: ast.FunctionDef | ast.AsyncFunctionDef,
    registrar_names: set[str],
    module_name: str,
) -> list[str]:
    """Registrar statements that are neither a tool declaration nor a delegation."""
    findings: list[str] = []
    for index, statement in enumerate(registrar.body):
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if index == 0 and is_docstring(statement):
            continue
        if is_registrar_delegation(statement, registrar_names):
            continue
        findings.append(
            f"{module_name}:{statement.lineno} registrar {registrar.name} contains "
            f"{type(statement).__name__}"
        )
    return findings


def is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def is_registrar_delegation(statement: ast.stmt, registrar_names: set[str]) -> bool:
    """A bare call to a registrar this module defines, and to nothing else."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id in registrar_names
    )


def is_server_tool_decorator(decorator: ast.expr) -> bool:
    """Return true for exactly ``@server.tool()``."""
    return (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "tool"
        and isinstance(decorator.func.value, ast.Name)
        and decorator.func.value.id == "server"
    )


def seven_parameter_function() -> str:
    """An ordinary function two parameters over Ruff's default ``max-args`` of five."""
    names = [f"value_{index}" for index in range(7)]
    signature = ", ".join(f"{name}: int" for name in names)
    return f"def ordinary({signature}) -> int:\n    return {' + '.join(names)}\n"
