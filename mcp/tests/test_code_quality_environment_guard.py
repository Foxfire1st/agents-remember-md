from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from _quality_admission import QUALITY_TEST_ADMISSION
from agents_remember_test_support.code_quality import check
from agents_remember_test_support.testing import dagger_admission


class CodeQualityEnvironmentGuardTests(unittest.TestCase):
    def test_direct_wrapper_refuses_before_targeted_or_retry_planning_without_attestation(
        self,
    ) -> None:
        with (
            mock.patch.dict(check.os.environ, {}, clear=True),
            mock.patch.object(check, "build_parser") as parser,
            mock.patch.object(check, "print_line") as printer,
        ):
            exit_code = check.main(["--targeted", "--diff-base", "base"])

        self.assertEqual(exit_code, 1)
        parser.assert_not_called()
        self.assertIn("refusing host execution", str(printer.call_args_list))

    def test_direct_wrapper_refuses_a_mismatched_dagger_attestation_before_planning(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            attestation = Path(tmp) / "dagger-test-attestation"
            attestation.write_text("f" * 32, encoding="utf-8")
            with (
                mock.patch.object(dagger_admission, "DAGGER_TEST_ATTESTATION_PATH", attestation),
                mock.patch.dict(
                    check.os.environ,
                    {dagger_admission.DAGGER_TEST_ATTESTATION_ENV: "0" * 32},
                    clear=True,
                ),
                mock.patch.object(check, "build_parser") as parser,
                mock.patch.object(check, "print_line") as printer,
            ):
                exit_code = check.main([])

        self.assertEqual(exit_code, 1)
        parser.assert_not_called()
        self.assertIn("do not match", str(printer.call_args_list))

    @mock.patch.object(check, "require_dagger_admission", new=lambda **_: QUALITY_TEST_ADMISSION)
    def test_main_uses_the_report_environment_to_select_its_native_temp_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            report = Path(tmp) / "reports" / "quality-progress.json"
            environment = dict(os.environ)
            environment[check.QUALITY_PROGRESS_REPORT_ENV] = report.as_posix()
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(
                    check,
                    "native_subprocess_environment",
                    side_effect=lambda env, *, temp_root: {**env, "TMPDIR": str(temp_root)},
                ) as native,
                mock.patch.object(check, "config_from_args", return_value=mock.Mock()),
                mock.patch.object(check, "run_quality_check", return_value=0),
            ):
                self.assertEqual(check.main([]), 0)

            self.assertEqual(native.call_args.kwargs["temp_root"], check.QUALITY_TEMP_ROOT)

    @mock.patch.object(check, "require_dagger_admission", new=lambda **_: QUALITY_TEST_ADMISSION)
    def test_main_without_a_report_uses_the_native_default_temp_root(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                check,
                "native_subprocess_environment",
                side_effect=lambda env, *, temp_root: {**env, "TMPDIR": str(temp_root)},
            ) as native,
            mock.patch.object(check, "config_from_args", return_value=mock.Mock()),
            mock.patch.object(check, "run_quality_check", return_value=0),
        ):
            self.assertEqual(check.main([]), 0)

        self.assertEqual(native.call_args.kwargs["temp_root"], check.QUALITY_TEMP_ROOT)
