"""Focused proof for pytest-plugin and support dependency discovery."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
from _evidence_catalog_fixture import write_synthetic_evidence_catalog
from agents_remember_test_support.code_quality.dependency_ownership import (
    AMBIENT_ROLE_RUNNER_PATH,
    CODEX_CONFIG_PATH,
    LAYERS_CONTRACT_PATH,
    DependencyOwnershipGraph,
)
from agents_remember_test_support.code_quality.scope import ScopeError
from agents_remember_test_support.testing import dependency_facts as facts


def test_file_imports_includes_python_and_declared_pytest_plugins(tmp_path: Path) -> None:
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        "\n".join(
            (
                "import package.child",
                "from sibling import helper",
                "pytest_plugins: tuple[str, ...] = ('plugins.alpha', 'plugins.beta')",
            )
        ),
        encoding="utf-8",
    )
    imports = facts.file_imports(conftest, None)
    assert {
        "package",
        "package.child",
        "sibling",
        "sibling.helper",
        "plugins",
        "plugins.alpha",
        "plugins.beta",
    }.issubset(imports)

    ordinary = tmp_path / "module.py"
    ordinary.write_text("pytest_plugins = ('recursive.plugin',)\n", encoding="utf-8")
    assert {"recursive", "recursive.plugin"}.issubset(facts.file_imports(ordinary, None))
    invalid = tmp_path / "invalid.py"
    invalid.write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(ScopeError, match="could not parse"):
        facts.file_imports(invalid, None)


def test_pytest_plugin_ast_helpers_accept_only_assignment_string_values() -> None:
    tree = ast.parse(
        "pytest_plugins = ['one.plugin', dynamic]\n"
        "pytest_plugins: tuple[str, ...] = ('two.plugin',)\n"
        "other = 'ignored.plugin'\n"
    )
    assignments = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))]
    first, second, other = assignments
    assert isinstance(first, ast.Assign)
    assert isinstance(second, ast.AnnAssign)
    assert isinstance(other, ast.Assign)
    assert facts._pytest_plugins_value(first) is not None
    assert facts._pytest_plugins_value(second) is not None
    assert facts._pytest_plugins_value(other) is None
    assert facts._pytest_plugins_value(ast.Pass()) is None
    assert facts._pytest_plugins_target(first.targets[0])
    assert not facts._pytest_plugins_target(other.targets[0])

    assert second.value is not None
    with pytest.raises(ScopeError, match="literal dotted module names"):
        facts._literal_plugin_names(first.value)
    assert facts._literal_plugin_names(second.value) == ("two.plugin",)
    with pytest.raises(ScopeError, match="literal string or sequence"):
        facts._literal_plugin_names(ast.Name(id="dynamic"))
    with pytest.raises(ScopeError, match="literal dotted module names"):
        facts._pytest_plugin_imports(tree)


def test_nested_pytest_plugin_edges_reach_the_complete_test_population(tmp_path: Path) -> None:
    _initialize_repository(tmp_path)
    _write(tmp_path, "tests/conftest.py", "pytest_plugins = ('plugins.alpha',)\n")
    _write(tmp_path, "tests/plugins/alpha.py", "pytest_plugins = ('plugins.beta',)\n")
    _write(tmp_path, "tests/plugins/beta.py", "VALUE = 1\n")
    _write(tmp_path, "tests/test_one.py", "def test_one():\n    assert True\n")
    _write(tmp_path, "tests/test_two.py", "def test_two():\n    assert True\n")
    _write(tmp_path, "mcp/tests/fixtures/anchor.json", "{}\n")
    write_synthetic_evidence_catalog(
        tmp_path,
        {"mcp/tests/fixtures/anchor.json": ("tests/test_one.py",)},
    )

    impact = DependencyOwnershipGraph(tmp_path).resolve([Path("tests/plugins/beta.py")])

    assert impact.complete
    assert impact.unresolved_inputs == ()
    assert impact.tests == (Path("tests/test_one.py"), Path("tests/test_two.py"))
    assert all(
        any(reason.kind.value == "import-consumer" for reason in impact.reasons_for(test))
        for test in impact.tests
    )


def test_dynamic_nested_plugin_declaration_refuses_complete_ownership(tmp_path: Path) -> None:
    _initialize_repository(tmp_path)
    _write(tmp_path, "tests/conftest.py", "pytest_plugins = ('plugins.alpha',)\n")
    _write(tmp_path, "tests/plugins/alpha.py", "pytest_plugins = dynamic_plugins\n")
    _write(tmp_path, "tests/plugins/beta.py", "VALUE = 1\n")
    _write(tmp_path, "tests/test_one.py", "def test_one():\n    assert True\n")
    _write(tmp_path, "mcp/tests/fixtures/anchor.json", "{}\n")
    write_synthetic_evidence_catalog(
        tmp_path,
        {"mcp/tests/fixtures/anchor.json": ("tests/test_one.py",)},
    )

    impact = DependencyOwnershipGraph(tmp_path).resolve([Path("tests/plugins/beta.py")])

    assert not impact.complete
    assert "import-graph-invalid" in impact.unresolved_inputs[0].detail
    assert impact.tests == ()


def test_imported_support_reaches_a_test_that_loads_its_owner_by_literal_path(
    tmp_path: Path,
) -> None:
    _initialize_repository(tmp_path)
    _write(tmp_path, ".dagger/src/quality/__init__.py", "")
    _write(tmp_path, ".dagger/src/quality/support.py", "VALUE = 1\n")
    _write(tmp_path, ".dagger/src/quality/main.py", "from quality.support import VALUE\n")
    _write(
        tmp_path,
        "tests/test_quality.py",
        "from pathlib import Path\n"
        "MODULE = Path('.dagger/src/quality/main.py')\n"
        "def test_module():\n"
        "    assert 'VALUE' in MODULE.read_text(encoding='utf-8')\n",
    )
    _write(tmp_path, "mcp/tests/fixtures/anchor.json", "{}\n")
    write_synthetic_evidence_catalog(
        tmp_path,
        {"mcp/tests/fixtures/anchor.json": ("tests/test_quality.py",)},
    )

    impact = DependencyOwnershipGraph(tmp_path).resolve([Path(".dagger/src/quality/support.py")])

    assert impact.complete
    assert impact.tests == (Path("tests/test_quality.py"),)
    assert any(
        reason.kind.value == "import-consumer"
        for reason in impact.reasons_for(Path("tests/test_quality.py"))
    )


def test_exact_dotted_module_literal_is_an_observable_test_consumer(tmp_path: Path) -> None:
    _initialize_repository(tmp_path)
    _write(tmp_path, "src/pkg/__init__.py", "")
    _write(tmp_path, "src/pkg/wiring.py", "VALUE = 1\n")
    _write(
        tmp_path,
        "tests/test_wiring.py",
        'WIRING_MODULE = "pkg.wiring"\n'
        "def test_wiring_identity():\n"
        "    assert WIRING_MODULE.endswith('.wiring')\n",
    )
    _write(tmp_path, "mcp/tests/fixtures/anchor.json", "{}\n")
    write_synthetic_evidence_catalog(
        tmp_path,
        {"mcp/tests/fixtures/anchor.json": ("tests/test_wiring.py",)},
    )

    impact = DependencyOwnershipGraph(tmp_path).resolve([Path("src/pkg/wiring.py")])

    assert impact.complete
    assert impact.tests == (Path("tests/test_wiring.py"),)
    assert any(
        reason.kind.value == "literal-consumer"
        for reason in impact.reasons_for(Path("tests/test_wiring.py"))
    )


def test_codex_starter_config_has_exact_observed_consumers() -> None:
    repository_root = Path(__file__).parents[2]

    impact = DependencyOwnershipGraph(repository_root).resolve([CODEX_CONFIG_PATH])

    assert impact.complete
    assert impact.tests == (
        Path("mcp/tests/test_public_surface_conformance.py"),
        Path("mcp/tests/test_starter_renderers.py"),
    )
    assert all(
        any(
            reason.kind.value == "declared-consumer"
            and reason.detail == "verified-repository-input"
            for reason in impact.reasons_for(test)
        )
        for test in impact.tests
    )


def test_ambient_role_runner_has_exact_pytest_consumers() -> None:
    repository_root = Path(__file__).parents[2]

    impact = DependencyOwnershipGraph(repository_root).resolve([AMBIENT_ROLE_RUNNER_PATH])

    assert impact.complete
    assert impact.unresolved_inputs == ()
    assert not impact.global_invalidation
    assert impact.global_invalidators == ()
    assert impact.tests == (
        Path("mcp/tests/test_agents_remember_quality.py"),
        Path("mcp/tests/test_clean_quality_executor.py"),
        Path("mcp/tests/test_gate_certificate_authority.py"),
        Path("mcp/tests/test_l5_quality_and_recovery_edges.py"),
        Path("mcp/tests/test_python_test_evidence_firewall.py"),
        Path("mcp/tests/test_quality_gate_public_contract.py"),
        Path("mcp/tests/test_quality_report_publication_security.py"),
        Path("mcp/tests/test_rail_evidence_publication.py"),
        Path("mcp/tests/test_repository_certification_profiles.py"),
        Path("mcp/tests/test_repository_profile_authority.py"),
        Path("mcp/tests/test_repository_profile_branch_coverage.py"),
        Path("mcp/tests/test_repository_quality_branch_coverage.py"),
        Path("mcp/tests/test_worktree_closeout_gate_scope.py"),
        Path("mcp/tests/test_worktree_closeout_quality_gate.py"),
        Path("mcp/tests/test_worktree_integrate_quality_gate.py"),
        Path("mcp/tests/test_worktree_quality_gate_runner.py"),
    )
    assert all(
        any(
            reason.kind.value == "declared-consumer"
            and reason.detail == "verified-repository-input"
            for reason in impact.reasons_for(test)
        )
        for test in impact.tests
    )


def test_layers_contract_has_exact_observed_consumers() -> None:
    repository_root = Path(__file__).parents[2]

    impact = DependencyOwnershipGraph(repository_root).resolve([LAYERS_CONTRACT_PATH])

    assert impact.complete
    assert impact.tests == (
        Path("mcp/tests/test_application_boundary.py"),
        Path("mcp/tests/test_l6_diff_coverage_code_quality.py"),
        Path("mcp/tests/test_layering.py"),
        Path("mcp/tests/test_leaf_structural_coverage.py"),
        Path("mcp/tests/test_structural_limits.py"),
    )
    assert all(
        any(
            reason.kind.value == "declared-consumer"
            and reason.detail == "verified-repository-input"
            for reason in impact.reasons_for(test)
        )
        for test in impact.tests
    )


def _initialize_repository(root: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    _write(root, "pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = ['tests']\n")


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
