"""Tests for the full quality gate's memory cap (260731-EFA-L17-R3)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from _quality_admission import QUALITY_TEST_ADMISSION
from agents_remember.kernel.primitives import memory_cap
from agents_remember_test_support.code_quality import check


def run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )


def minimal_repository(root: Path) -> Path:
    run_git(root, "init", "--quiet", "--initial-branch=main")
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
    (root / "pkg").mkdir()
    (root / "tests").mkdir()
    (root / "pkg/__init__.py").write_text("", encoding="utf-8")
    (root / "tests/test_pkg.py").write_text(
        "def test_nothing() -> None:\n    assert True\n", encoding="utf-8"
    )
    run_git(root, "add", "-A")
    run_git(
        root,
        "-c",
        "user.email=cap@agents-remember.invalid",
        "-c",
        "user.name=Cap Tests",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    return root


class MemoryCapPlanningTests(unittest.TestCase):
    def test_systemd_scope_availability_branches(self) -> None:
        with mock.patch.object(memory_cap.shutil, "which", return_value=None):
            self.assertFalse(memory_cap.systemd_scope_available())
        with mock.patch.object(memory_cap.Path, "is_dir", return_value=False):
            self.assertFalse(memory_cap.systemd_scope_available())
        with (
            mock.patch.object(memory_cap.shutil, "which", return_value="/usr/bin/systemd-run"),
            mock.patch.object(memory_cap.Path, "is_dir", return_value=True),
            mock.patch.object(memory_cap.os, "geteuid", return_value=0),
        ):
            self.assertTrue(memory_cap.systemd_scope_available())
        with (
            mock.patch.object(memory_cap.shutil, "which", return_value="/usr/bin/systemd-run"),
            mock.patch.object(memory_cap.Path, "is_dir", return_value=True),
            mock.patch.object(memory_cap.os, "geteuid", return_value=1000),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(memory_cap.systemd_scope_available())
        with (
            mock.patch.object(memory_cap.shutil, "which", return_value="/usr/bin/systemd-run"),
            mock.patch.object(memory_cap.Path, "is_dir", return_value=True),
            mock.patch.object(memory_cap.os, "geteuid", return_value=1000),
            mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=True),
            mock.patch.object(memory_cap.Path, "is_socket", return_value=False),
        ):
            self.assertFalse(memory_cap.systemd_scope_available())
        with (
            mock.patch.object(memory_cap.shutil, "which", return_value="/usr/bin/systemd-run"),
            mock.patch.object(memory_cap.Path, "is_dir", return_value=True),
            mock.patch.object(memory_cap.os, "geteuid", return_value=1000),
            mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=True),
            mock.patch.object(memory_cap.Path, "is_socket", return_value=True),
        ):
            self.assertTrue(memory_cap.systemd_scope_available())

    def test_systemd_plan_wraps_the_command_in_a_scope(self) -> None:
        with mock.patch.object(memory_cap.os, "geteuid", return_value=1000):
            plan = memory_cap.plan_capped_command(
                "python",
                ["-m", "agents_remember_test_support.code_quality.check", "--diff-base", "abc"],
                2147483648,
                systemd_run_available=True,
            )

        self.assertEqual(plan.mechanism, memory_cap.SYSTEMD_MECHANISM)
        self.assertEqual(plan.cap_bytes, 2147483648)
        self.assertEqual(plan.policy, memory_cap.QUALITY_MEMORY_CAP_POLICY)
        self.assertEqual(plan.command[0], "systemd-run")
        self.assertIn("--user", plan.command)
        self.assertIn("--scope", plan.command)
        self.assertIn("MemoryMax=2147483648", plan.command)
        self.assertFalse(any(part.startswith("MemorySwapMax=") for part in plan.command))
        self.assertIn("python", plan.command)
        self.assertIn("--diff-base", plan.command)
        with mock.patch.object(memory_cap.os, "geteuid", return_value=0):
            root_plan = memory_cap.plan_capped_command(
                "python",
                ["-m", "agents_remember_test_support.code_quality.check"],
                2147483648,
                systemd_run_available=True,
            )
        self.assertNotIn("--user", root_plan.command)

    def test_rlimit_fallback_inserts_the_self_cap_flag(self) -> None:
        plan = memory_cap.plan_capped_command(
            "/venv/bin/python",
            ["-m", "agents_remember_test_support.code_quality.check", "--diff-base", "abc"],
            1073741824,
            systemd_run_available=False,
        )

        self.assertEqual(plan.mechanism, memory_cap.RLIMIT_MECHANISM)
        self.assertEqual(
            plan.command,
            [
                "/venv/bin/python",
                "-m",
                "agents_remember_test_support.code_quality.check",
                "--memory-cap-bytes",
                "1073741824",
                "--diff-base",
                "abc",
            ],
        )

    def test_with_self_cap_rejects_malformed_module_args(self) -> None:
        with self.assertRaisesRegex(ValueError, "starting with"):
            memory_cap.with_self_cap(["python"], 1024)


class WrapperMemoryCapTests(unittest.TestCase):
    @mock.patch.object(check, "require_dagger_admission", new=lambda **_: QUALITY_TEST_ADMISSION)
    def test_main_refuses_a_non_positive_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = minimal_repository(Path(tmp))
            output: list[str] = []

            with mock.patch.object(check, "print_line", output.append):
                exit_code = check.main(
                    [
                        "--project-root",
                        str(root),
                        "--memory-cap-bytes",
                        "0",
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertTrue(any("must be a positive integer" in line for line in output))
            self.assertTrue(any("result: quality-wrapper FAIL" in line for line in output))

    @mock.patch.object(check, "require_dagger_admission", new=lambda **_: QUALITY_TEST_ADMISSION)
    def test_main_applies_the_cap_and_names_the_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = minimal_repository(Path(tmp))
            output: list[str] = []
            cap = 2 * 1024**3

            def fake_gate(config: check.CheckConfig) -> int:
                del config
                return 0

            with (
                mock.patch.object(check, "run_quality_check", fake_gate),
                mock.patch.object(check.resource, "setrlimit") as setrlimit,
                mock.patch.object(check, "print_line", output.append),
                mock.patch.dict(os.environ),
            ):
                exit_code = check.main(
                    [
                        "--project-root",
                        str(root),
                        "--memory-cap-bytes",
                        str(cap),
                    ]
                )
                self.assertIn(memory_cap.MEMORY_CAP_ENV, os.environ)

            self.assertEqual(exit_code, 0)
            setrlimit.assert_called_once_with(check.resource.RLIMIT_AS, (cap, cap))
            self.assertTrue(
                any(
                    line.startswith(f"memory-cap: policy={memory_cap.QUALITY_MEMORY_CAP_POLICY}")
                    for line in output
                )
            )

    @mock.patch.object(check, "require_dagger_admission", new=lambda **_: QUALITY_TEST_ADMISSION)
    def test_main_refuses_loudly_when_the_cap_cannot_be_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = minimal_repository(Path(tmp))
            output: list[str] = []

            with (
                mock.patch.object(
                    check.resource,
                    "setrlimit",
                    side_effect=OSError("no permission"),
                ),
                mock.patch.object(check, "print_line", output.append),
            ):
                exit_code = check.main(
                    [
                        "--project-root",
                        str(root),
                        "--memory-cap-bytes",
                        str(1024),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertTrue(any("memory-cap could not be applied" in line for line in output))
            self.assertTrue(any("result: quality-wrapper FAIL" in line for line in output))

    @mock.patch.object(check, "require_dagger_admission", new=lambda **_: QUALITY_TEST_ADMISSION)
    def test_main_reports_cap_exceeded_with_the_policy_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = minimal_repository(Path(tmp))
            output: list[str] = []

            def exploding_gate(config: check.CheckConfig) -> int:
                del config
                raise MemoryError("alloc failed")

            with (
                mock.patch.object(check, "run_quality_check", exploding_gate),
                mock.patch.object(check.resource, "setrlimit"),
                mock.patch.object(check, "print_line", output.append),
            ):
                exit_code = check.main(
                    [
                        "--project-root",
                        str(root),
                        "--memory-cap-bytes",
                        str(1024),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertTrue(any("memory cap exceeded" in line for line in output))
            self.assertTrue(any(memory_cap.QUALITY_MEMORY_CAP_POLICY in line for line in output))

    @mock.patch.object(check, "require_dagger_admission", new=lambda **_: QUALITY_TEST_ADMISSION)
    def test_main_reports_a_memory_error_without_a_cap_as_out_of_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = minimal_repository(Path(tmp))
            output: list[str] = []

            def exploding_gate(config: check.CheckConfig) -> int:
                del config
                raise MemoryError("alloc failed")

            with (
                mock.patch.object(check, "run_quality_check", exploding_gate),
                mock.patch.object(check, "print_line", output.append),
            ):
                exit_code = check.main(["--project-root", str(root)])

            self.assertEqual(exit_code, 1)
            self.assertTrue(any("out of memory" in line for line in output))
