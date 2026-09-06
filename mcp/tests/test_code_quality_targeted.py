"""Tests for the leaf change-set-scoped quality derivation (260731-EFA-L17-R1/R5)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from _evidence_catalog_fixture import write_synthetic_evidence_catalog
from agents_remember_test_support.code_quality import (
    targeted,
)
from agents_remember_test_support.code_quality.dependency_ownership import SelectionReasonKind


def run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )


def write_quality_config(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        "\n".join(
            (
                "[tool.ruff]",
                "line-length = 100",
                "[tool.pyright]",
                'include = ["."]',
                "[tool.radon]",
                'cc_min = "B"',
                "[tool.coverage.run]",
                "branch = true",
                "[tool.pytest.ini_options]",
                'testpaths = ["tests"]',
                "[tool.agents_remember]",
                'product_package_roots = ["src/pkg"]',
                'verification_package_roots = [".dagger/src/quality"]',
                "",
            )
        ),
        encoding="utf-8",
    )


def targeted_repository(root: Path) -> str:
    """A leaf-shaped repository: package, importer chain, and tests, at a baseline."""
    run_git(root, "init", "--quiet", "--initial-branch=main")
    write_quality_config(root)
    (root / "src/pkg").mkdir(parents=True)
    (root / ".dagger/src/quality").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "mcp/tests").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "src/pkg/__init__.py").write_text("", encoding="utf-8")
    (root / ".dagger/src/quality/__init__.py").write_text("", encoding="utf-8")
    (root / ".dagger/src/quality/support.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src/pkg/module.py").write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    (root / "src/pkg/relative.py").write_text(
        "from .module import value\nRELATIVE = value()\n", encoding="utf-8"
    )
    (root / "src/pkg/deep.py").write_text("from ... import nope\nDEEP = nope\n", encoding="utf-8")
    (root / "src/pkg/common.py").write_text(
        "from pkg.module import value\nfrom pkg.extra import other\nCOMMON = value() + other\n",
        encoding="utf-8",
    )
    (root / "src/pkg/importer.py").write_text(
        "from pkg.module import value\nVALUE = value()\n", encoding="utf-8"
    )
    (root / "src/pkg/top.py").write_text(
        "from pkg.importer import VALUE\nTOP = VALUE\n", encoding="utf-8"
    )
    (root / "tests/test_module.py").write_text(
        "from _support import SUPPORT\n"
        "from pkg.module import value\n\n"
        "def test_value() -> None:\n    assert value() == SUPPORT\n",
        encoding="utf-8",
    )
    (root / "tests/test_extra.py").write_text(
        "import os\n"
        "from . import sibling\n"
        "from pkg.module import *\n"
        "def test_nothing() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    (root / "scripts/sync.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "scripts/test_module.py").write_text("NAME_MATCH_DECOY = True\n", encoding="utf-8")
    (root / "tests/_support.py").write_text("SUPPORT = 1\n", encoding="utf-8")
    (root / "tests/conftest.py").write_text("\n", encoding="utf-8")
    (root / "mcp/tests/_catalog_anchor.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "mcp/tests/fixtures").mkdir()
    (root / "mcp/tests/fixtures/owned.json").write_text("{}\n", encoding="utf-8")
    write_synthetic_evidence_catalog(
        root,
        {
            "mcp/tests/_catalog_anchor.py": ("tests/test_module.py",),
            "mcp/tests/fixtures/owned.json": ("tests/test_extra.py",),
        },
    )
    run_git(root, "add", "-A")
    run_git(
        root,
        "-c",
        "user.email=targeted@agents-remember.invalid",
        "-c",
        "user.name=Targeted Tests",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    return run_git(root, "rev-parse", "HEAD").stdout.strip()


class TargetedScopeDerivationTests(unittest.TestCase):
    def test_changed_files_closure_and_test_subset_are_derived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = targeted_repository(root)
            (root / "src/pkg/module.py").write_text(
                "def value() -> int:\n    return 2\n", encoding="utf-8"
            )
            (root / "src/pkg/extra.py").write_text(
                "def other() -> int:\n    return 3\n", encoding="utf-8"
            )
            run_git(root, "add", "-A")

            derived = targeted.derive_targeted_scope(root, base)

            self.assertEqual(
                derived.changed_paths,
                (Path("src/pkg/extra.py"), Path("src/pkg/module.py")),
            )
            self.assertEqual(derived.lint_paths, derived.changed_paths)
            # Reverse-import closure: importer.py imports module.py, top.py imports
            # importer.py, common.py imports both changed modules, and the extra
            # module itself is in the closure.
            for path in (
                "src/pkg/importer.py",
                "src/pkg/top.py",
                "src/pkg/common.py",
                "src/pkg/extra.py",
            ):
                self.assertIn(Path(path), derived.type_paths)
            self.assertNotIn(Path("src/pkg/module.py"), derived.reverse_import_closure)
            self.assertEqual(
                derived.coverage_paths,
                (Path("src/pkg/extra.py"), Path("src/pkg/module.py")),
            )
            # test_module.py reaches module.py through imports; test_extra.py matches
            # the extra module by name even though it does not import it.
            self.assertEqual(
                derived.test_paths,
                (Path("tests/test_extra.py"), Path("tests/test_module.py")),
            )
            self.assertIn(
                SelectionReasonKind.NAME_HEURISTIC,
                {
                    reason.kind
                    for reason in derived.test_impact.reasons_for(Path("tests/test_extra.py"))
                },
            )
            self.assertIn(
                SelectionReasonKind.IMPORT_CONSUMER,
                {
                    reason.kind
                    for reason in derived.test_impact.reasons_for(Path("tests/test_module.py"))
                },
            )

    def test_changed_production_module_without_owner_refuses_without_broadening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = targeted_repository(root)
            (root / "src/pkg/naked.py").write_text(
                "def uncovered() -> int:\n    return 0\n", encoding="utf-8"
            )
            run_git(root, "add", "-A")

            derived = targeted.derive_targeted_scope(root, base)

            self.assertFalse(derived.test_impact.complete)
            self.assertEqual(derived.test_paths, ())
            self.assertEqual(
                tuple(reason.source for reason in derived.test_impact.unresolved_inputs),
                (Path("src/pkg/naked.py"),),
            )

    def test_shared_support_change_selects_static_import_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = targeted_repository(root)
            (root / "tests/_support.py").write_text("SUPPORT = 2\n", encoding="utf-8")

            derived = targeted.derive_targeted_scope(root, base)

            self.assertEqual(derived.test_paths, (Path("tests/test_module.py"),))
            self.assertTrue(derived.test_impact.complete)
            self.assertEqual(
                {reason.kind for reason in derived.test_impact.reasons_for(derived.test_paths[0])},
                {SelectionReasonKind.IMPORT_CONSUMER},
            )

    def test_verification_package_change_never_becomes_product_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = targeted_repository(root)
            verification = root / ".dagger/src/quality/support.py"
            verification.write_text("VALUE = 2\n", encoding="utf-8")

            derived = targeted.derive_targeted_scope(root, base)

            self.assertEqual(derived.changed_paths, (Path(".dagger/src/quality/support.py"),))
            self.assertEqual(derived.lint_paths, derived.changed_paths)
            self.assertIn(Path(".dagger/src/quality/support.py"), derived.type_paths)
            self.assertEqual(derived.coverage_paths, ())
            self.assertEqual(derived.coverage_root_modules, ("pkg",))
