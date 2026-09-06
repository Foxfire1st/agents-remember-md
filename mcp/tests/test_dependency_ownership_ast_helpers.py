"""Focused proof for pytest-plugin and support dependency discovery."""

from __future__ import annotations

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


@pytest.mark.integration
def test_repository_inputs_reach_their_supported_consumers() -> None:
    # The graph validates the complete artifact catalog as part of this one real census.
    graph = DependencyOwnershipGraph(Path(__file__).parents[2])
    for source, consumer in (
        (CODEX_CONFIG_PATH, Path("mcp/tests/test_starter_renderers.py")),
        (AMBIENT_ROLE_RUNNER_PATH, Path("mcp/tests/test_agents_remember_quality.py")),
        (LAYERS_CONTRACT_PATH, Path("mcp/tests/test_layering.py")),
    ):
        impact = graph.resolve([source])
        assert impact.complete
        assert not impact.global_invalidation
        assert consumer in impact.tests
        assert any(
            reason.kind.value == "declared-consumer" for reason in impact.reasons_for(consumer)
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
