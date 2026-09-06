"""The File Size Budget rail: bands, exit codes, wrapper wiring, and scope."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_TEST_SUPPORT = Path(__file__).resolve().parents[1] / "test_support"
sys.path.insert(0, str(MCP_SRC))

from _quality_admission import QUALITY_TEST_ADMISSION
from agents_remember_test_support.code_quality import check, file_size


class FileSizeBandsTests(unittest.TestCase):
    def test_measure_counts_newlines_like_wc_and_flags_only_hard_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            over = root / "over.py"
            under = root / "under.py"
            over.write_text("x = 1\n" * 1200, encoding="utf-8")
            under.write_text("x = 1\n" * 1199, encoding="utf-8")

            findings = file_size.measure([over, under])

            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].path, over)
            self.assertEqual(findings[0].line_count, 1200)
            self.assertEqual(findings[0].band, "hard-limit-exceeded")
            self.assertEqual(file_size.line_count(under), 1199)


class FileSizeWrapperWiringTests(unittest.TestCase):
    def test_unarmed_step_reports_and_armed_step_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_repository_and_source(root)
            base_config = check.CheckConfig(
                project_root=root,
                scope=check.GateScope([source], [source], [source], [root / "tests"]),
                admission=QUALITY_TEST_ADMISSION,
                coverage_json=root / "coverage.json",
                threshold=20.0,
                top=5,
            )

            unarmed = check.quality_steps(base_config, root / "coverage.json")
            armed = check.quality_steps(
                check.CheckConfig(
                    project_root=root,
                    scope=base_config.scope,
                    admission=QUALITY_TEST_ADMISSION,
                    coverage_json=base_config.coverage_json,
                    threshold=base_config.threshold,
                    top=base_config.top,
                    file_size_armed=True,
                ),
                root / "coverage.json",
            )

            unarmed_command = next(step.command for step in unarmed if step.name == "file-size")
            armed_command = next(step.command for step in armed if step.name == "file-size")
            self.assertIn("--report", unarmed_command)
            self.assertNotIn("--report", armed_command)


def run_detector(extra: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agents_remember_test_support.code_quality.file_size",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env={
            "PYTHONPATH": f"{MCP_TEST_SUPPORT}:{MCP_SRC}",
            "PATH": "/usr/bin:/bin",
        },
    )


def write_sample_repository_and_source(root: Path) -> Path:
    """A real git repository with a package, tests, a script, and a sample module."""
    run_git(root, "init", "--quiet", "--initial-branch=main")
    (root / "pyproject.toml").write_text(
        (
            "[tool.pytest.ini_options]\n"
            'testpaths = ["tests"]\n'
            "[tool.agents_remember]\n"
            "file_size_armed = false\n"
            'product_package_roots = ["pkg"]\n'
            "verification_package_roots = []\n"
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
    source = root / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    run_git(root, "add", "-A")
    return source


def run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
