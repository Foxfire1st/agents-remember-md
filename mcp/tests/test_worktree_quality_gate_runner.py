"""Strict quality-runner command, environment, cap, and report behavior."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from gate_certification_test_support import _checkout_with_profile, _git
from test_worktree_closeout_quality_gate import _quality_target


class CodeQualityGateTests(unittest.TestCase):
    def test_host_quality_execution_refuses_before_running_a_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            with self.assertRaisesRegex(RuntimeError, "host quality execution is forbidden"):
                code_quality_gate.run_local_quality_diagnostic(_quality_target(worktree))

    @pytest.mark.usefixtures("worktree_services")
    def test_interrupted_gate_keeps_the_previous_completed_test_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = _checkout_with_profile(root / "code")
            worktree_group = root / "enclosure"
            report = worktree_group / "reports" / "test-results.md"
            report.parent.mkdir(parents=True)
            report.write_text("previous completed run\n", encoding="utf-8")

            with (
                mock.patch.object(
                    code_quality_gate, "run_clean_quality", side_effect=KeyboardInterrupt
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree, worktree_group),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )

            self.assertEqual(report.read_text(encoding="utf-8"), "previous completed run\n")
