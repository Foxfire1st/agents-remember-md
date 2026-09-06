"""Pure forcing proof for admission/bootstrap separation."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from _evidence_catalog_fixture import write_synthetic_evidence_catalog
from agents_remember.kernel.primitives.checkout_coordination import declared_execution_mode
from agents_remember_test_support.testing.global_state import (
    begin_pytest_process,
    end_pytest_process,
    restore_owned_mutable_state,
    snapshot_owned_mutable_state,
)
from agents_remember_test_support.testing.hermetic_bootstrap import (
    DISPOSABLE_GIT_IDENTITY,
    BootstrapConfigurationError,
    activate_current_pytest_environment,
    candidate_test_process,
    hermetic_pytest_environment,
)

VALID_NONCE = "0123456789abcdef0123456789abcdef"


class PytestBootstrapBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        package = self.root / "mcp" / "src" / "agents_remember"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        test_support = self.root / "mcp" / "test_support" / "agents_remember_test_support"
        test_support.mkdir(parents=True)
        (test_support / "__init__.py").write_text("", encoding="utf-8")
        tests = self.root / "mcp" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_plain.py").write_text(
            "def test_plain():\n    assert 2 + 2 == 4\n",
            encoding="utf-8",
        )
        (tests / "_catalog_anchor.py").write_text("VALUE = 1\n", encoding="utf-8")
        write_synthetic_evidence_catalog(
            self.root,
            {"mcp/tests/_catalog_anchor.py": ("mcp/tests/test_plain.py",)},
        )
        (self.root / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["mcp/tests"]\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_environment_is_candidate_bound_scrubbed_disposable_and_reversible(self) -> None:
        process = candidate_test_process(self.root)
        hostile = {
            "GIT_DIR": "/real/repository/.git",
            "GIT_WORK_TREE": "/real/repository",
            "GIT_AUTHOR_NAME": "Real Developer",
            "PYTHONPATH": "/wrong/checkout",
            "PATH": "/usr/bin",
            "KEEP": "yes",
        }
        child = hermetic_pytest_environment(
            process,
            hostile,
            cache_root=self.root.parent / "isolated-cache",
        )
        self.assertNotIn("GIT_DIR", child)
        self.assertNotIn("GIT_WORK_TREE", child)
        self.assertEqual(child["GIT_AUTHOR_NAME"], DISPOSABLE_GIT_IDENTITY["GIT_AUTHOR_NAME"])
        self.assertEqual(
            child["PYTHONPATH"],
            os.pathsep.join((process.test_support_root.as_posix(), process.source_root.as_posix())),
        )
        self.assertEqual(child["KEEP"], "yes")
        expected_temp = (self.root.parent / "isolated-cache" / "tmp").as_posix()
        self.assertEqual(
            {child[name] for name in ("TMPDIR", "TMP", "TEMP")},
            {expected_temp},
        )

        current = dict(hostile)
        before = dict(current)
        lease = activate_current_pytest_environment(process, current)
        self.assertNotIn("GIT_DIR", current)
        self.assertEqual(current["GIT_AUTHOR_NAME"], DISPOSABLE_GIT_IDENTITY["GIT_AUTHOR_NAME"])
        lease.close()
        lease.close()
        self.assertEqual(current, before)

        with self.assertRaises(BootstrapConfigurationError):
            hermetic_pytest_environment(
                process,
                os.environ,
                cache_root=self.root / ".cache",
            )

    def test_test_process_declaration_and_global_state_are_restored(self) -> None:
        before = snapshot_owned_mutable_state()
        try:
            end_pytest_process()
            self.assertIsNone(declared_execution_mode())
            begin_pytest_process()
            self.assertEqual(declared_execution_mode(), "test")
        finally:
            restore_owned_mutable_state(before)
        self.assertEqual(snapshot_owned_mutable_state(), before)


if __name__ == "__main__":
    unittest.main()
