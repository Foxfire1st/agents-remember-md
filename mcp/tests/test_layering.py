"""Unit tests for the layers.toml layering fitness function (260731-EFA-L9 R12)."""

from __future__ import annotations

from pathlib import Path

from agents_remember_test_support.code_quality import layering


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _contract(root: Path) -> layering.LayersContract:
    return layering.load_contract(root / "layers.toml")


def _make_contract_toml() -> str:
    return """
[contract]
order = ["errors", "kernel", "models", "serving"]

[package.errors]
path = "."
root_modules = ["errors.py", "__init__.py"]
present = true

[package.kernel]
path = "kernel/"
present = true

[package.models]
path = "models/"
present = true

[package.serving]
path = "serving/"
present = true
"""


def test_rank_violation_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_tree(
        root,
        {
            "layers.toml": _make_contract_toml(),
            "mcp/src/agents_remember/kernel/__init__.py": "",
            "mcp/src/agents_remember/kernel/core.py": (
                "from agents_remember.models.gadget import Gadget\n"
            ),
            "mcp/src/agents_remember/models/__init__.py": "",
            "mcp/src/agents_remember/models/gadget.py": "",
            "mcp/src/agents_remember/serving/__init__.py": "",
        },
    )
    report = layering.check_layering(root)
    assert not report.ok
    assert len(report.violations) == 1
    assert report.violations[0].importer == "kernel"
    assert report.violations[0].imported == "models"


def test_clean_tree_passes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_tree(
        root,
        {
            "layers.toml": _make_contract_toml(),
            "mcp/src/agents_remember/__init__.py": "",
            "mcp/src/agents_remember/errors.py": "",
            "mcp/src/agents_remember/kernel/__init__.py": "",
            "mcp/src/agents_remember/kernel/core.py": (
                "from agents_remember.errors import AgentsRememberError\n"
            ),
            "mcp/src/agents_remember/models/__init__.py": "",
            "mcp/src/agents_remember/models/gadget.py": (
                "from agents_remember.kernel.core import thing\n"
            ),
            "mcp/src/agents_remember/serving/__init__.py": "",
            "mcp/src/agents_remember/serving/app.py": (
                "from agents_remember.models.gadget import Gadget\n"
            ),
        },
    )
    report = layering.check_layering(root)
    assert report.ok, layering.render(report)


def test_undeclared_package_import_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_tree(
        root,
        {
            "layers.toml": _make_contract_toml(),
            "mcp/src/agents_remember/__init__.py": "",
            "mcp/src/agents_remember/errors.py": "",
            "mcp/src/agents_remember/kernel/__init__.py": "",
            "mcp/src/agents_remember/kernel/a.py": ("from agents_remember import rogue\n"),
            "mcp/src/agents_remember/models/__init__.py": "",
            "mcp/src/agents_remember/serving/__init__.py": "",
        },
    )
    report = layering.check_layering(root)
    assert not report.ok
    assert len(report.undeclared_imports) == 1
    statement = report.undeclared_imports[0]
    assert statement.importer == "kernel"
    assert statement.imported == "rogue"
    assert statement.module == "agents_remember.rogue"
    assert "undeclared package import" in layering.render(report)
