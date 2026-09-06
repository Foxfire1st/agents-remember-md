from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents_remember_test_support.code_quality import check
from test_code_quality_check import (
    run_git,
    write_sample_repository,
)


class GateScopeDerivationTests(unittest.TestCase):
    def test_new_importable_package_requires_explicit_product_or_verification_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_sample_repository(Path(tmp))
            support = root / "support"
            support.mkdir()
            (support / "__init__.py").write_text("", encoding="utf-8")
            run_git(root, "add", "support/__init__.py")

            with self.assertRaises(check.ScopeError) as raised:
                check.derive_scope(root)

            self.assertIn("missing=['support']", str(raised.exception))

            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace(
                    "verification_package_roots = []",
                    'verification_package_roots = ["support"]',
                ),
                encoding="utf-8",
            )
            scope = check.derive_scope(root)

            self.assertEqual(scope.coverage_paths, [Path("pkg")])
            self.assertIn(Path("support/__init__.py"), scope.lint_paths)
            self.assertIn(Path("support/__init__.py"), scope.type_paths)

    def test_package_authority_rejects_overlap_and_stale_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_sample_repository(Path(tmp))
            pyproject = root / "pyproject.toml"
            original = pyproject.read_text(encoding="utf-8")
            pyproject.write_text(
                original.replace(
                    "verification_package_roots = []",
                    'verification_package_roots = ["pkg"]',
                ),
                encoding="utf-8",
            )

            with self.assertRaises(check.ScopeError) as overlap:
                check.derive_scope(root)

            self.assertIn("both product and verification", str(overlap.exception))

            pyproject.write_text(
                original.replace(
                    "verification_package_roots = []",
                    'verification_package_roots = ["missing_support"]',
                ),
                encoding="utf-8",
            )

            with self.assertRaises(check.ScopeError) as stale:
                check.derive_scope(root)

            self.assertIn("stale=['missing_support']", str(stale.exception))
