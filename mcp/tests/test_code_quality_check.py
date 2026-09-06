from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))


COMPLEXITY_RULES = ("C901", "PLR0911", "PLR0912", "PLR0915")

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = Path(__file__).resolve().parent
ENVIRONMENT_NAME = re.compile(r"\b(?:AR|AGENTS_REMEMBER)_[A-Z0-9_]+\b")
SKIP_DECORATORS = ("skipUnless", "skipIf", "skipif")


def run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def write_sample_repository(root: Path) -> Path:
    """A throwaway repository shaped like this one: a package, a test tree, a script."""
    run_git(root, "init", "--quiet")
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
                'product_package_roots = ["pkg"]',
                "verification_package_roots = []",
                "",
            )
        ),
        encoding="utf-8",
    )
    for directory in ("pkg", "tests", "scripts"):
        (root / directory).mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_pkg.py").write_text(
        "def test_nothing() -> None: ...\n", encoding="utf-8"
    )
    (root / "scripts" / "sync.py").write_text("value = 1\n", encoding="utf-8")
    run_git(root, "add", "-A")
    return root


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
