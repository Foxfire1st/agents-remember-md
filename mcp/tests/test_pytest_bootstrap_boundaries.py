"""Pure forcing proof for admission/bootstrap separation."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _evidence_catalog_fixture import write_synthetic_evidence_catalog
from agents_remember.kernel.primitives.checkout_coordination import declared_execution_mode
from agents_remember.models.test_evidence import (
    EvidenceConsumer,
    EvidenceConsumerRefusal,
    require_certifying_evidence,
)
from agents_remember_test_support.testing.certifying_bootstrap import (
    prepare_certifying_pytest_bootstrap,
)
from agents_remember_test_support.testing.dagger_admission import (
    DAGGER_TEST_ATTESTATION_ENV,
    DaggerAdmission,
    DaggerAdmissionError,
    dagger_admission_refusal,
    require_dagger_admission_capability,
)
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

    def test_certification_requires_its_own_admission(self) -> None:
        attestation = self.root / "attestation"
        attestation.write_text(VALID_NONCE, encoding="utf-8")
        certifying = prepare_certifying_pytest_bootstrap(
            self.root,
            environ={DAGGER_TEST_ATTESTATION_ENV: VALID_NONCE},
            attestation_path=attestation,
        )
        self.assertIs(
            require_dagger_admission_capability(certifying.admission),
            certifying.admission,
        )

        with self.assertRaisesRegex(DaggerAdmissionError, "absent or invalid"):
            prepare_certifying_pytest_bootstrap(
                self.root / "not-a-candidate",
                environ={},
                attestation_path=attestation,
            )

        with self.assertRaises(EvidenceConsumerRefusal):
            require_certifying_evidence(object(), consumer=EvidenceConsumer.QUALITY)

    def test_admission_matrix_is_total_and_does_not_expose_the_nonce(self) -> None:
        attestation = self.root / "attestation"
        self.assertIn("absent or invalid", dagger_admission_refusal({}, attestation) or "")
        self.assertIn(
            "unavailable",
            dagger_admission_refusal({DAGGER_TEST_ATTESTATION_ENV: VALID_NONCE}, attestation) or "",
        )
        attestation.write_text("f" * 32, encoding="utf-8")
        self.assertIn(
            "do not match",
            dagger_admission_refusal({DAGGER_TEST_ATTESTATION_ENV: VALID_NONCE}, attestation) or "",
        )
        with self.assertRaises(TypeError):
            DaggerAdmission(attestation_path=attestation, nonce_sha256=VALID_NONCE)
        forged = object.__new__(DaggerAdmission)
        with self.assertRaises(DaggerAdmissionError):
            require_dagger_admission_capability(forged)

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

    def test_shared_pytest_plugin_has_no_admission_or_external_service_dependency(self) -> None:
        package = (
            Path(__file__).resolve().parents[1] / "test_support" / "agents_remember_test_support"
        )
        shared = (package / "testing" / "pytest_bootstrap.py").read_text(encoding="utf-8")
        certifying = (package / "pytest_certifying_bootstrap.py").read_text(encoding="utf-8")
        self.assertNotIn("dagger", shared.lower())
        self.assertNotIn("worktree_services", shared)
        self.assertNotIn("providers", shared)
        self.assertIn("pytest_bootstrap", certifying)
        self.assertIn("worktree_services", certifying)

    def test_certifying_plugin_defers_the_service_graph_until_fixture_execution(self) -> None:
        mcp_root = Path(__file__).resolve().parents[1]
        source_root = mcp_root / "src"
        test_support_root = mcp_root / "test_support"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            (test_support_root.as_posix(), source_root.as_posix())
        )
        command = "\n".join(
            (
                "import sys",
                "import agents_remember_test_support.pytest_certifying_bootstrap",
                "forbidden = {",
                "    'agents_remember_test_support.testing',",
                "    'agents_remember.application.worktree_services',",
                "    'agents_remember.models.lifecycles.operation',",
                "}",
                "loaded = sorted(forbidden.intersection(sys.modules))",
                "raise SystemExit('eager imports: ' + ', '.join(loaded) if loaded else 0)",
            )
        )

        completed = subprocess.run(
            [sys.executable, "-c", command],
            cwd=mcp_root.parent,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

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
