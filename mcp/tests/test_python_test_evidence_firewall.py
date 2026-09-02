"""Pure rejection/acceptance matrix for Python test evidence consumers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from agents_remember.models.test_evidence import (
    EvidenceConsumer,
    EvidenceConsumerRefusal,
    require_certifying_evidence,
)
from agents_remember.worktrees.modules.quality import clean_executor as clean_quality_executor
from agents_remember_test_support.code_quality import check, retry_proof
from agents_remember_test_support.testing.consumer_inventory import ACCEPTING_CONSUMER_INVENTORY
from agents_remember_test_support.testing.dagger_admission import (
    DaggerAdmission,
    DaggerAdmissionError,
)
from repository_profile_test_support import agents_remember_profile_execution


class PythonTestEvidenceFirewallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.non_certifying = object()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_diagnostic_evidence_is_rejected_by_every_accepting_consumer(self) -> None:
        for contract in ACCEPTING_CONSUMER_INVENTORY:
            with (
                self.subTest(consumer=contract.consumer),
                self.assertRaises(EvidenceConsumerRefusal),
            ):
                require_certifying_evidence(
                    self.non_certifying,
                    consumer=contract.consumer,
                )

    def test_coverage_and_retry_require_the_opaque_admission_capability(self) -> None:
        forged = cast(DaggerAdmission, self.non_certifying)
        config = check.CheckConfig(
            project_root=self.root,
            scope=check.GateScope(
                lint_paths=[],
                type_paths=[],
                coverage_paths=[],
                test_paths=[Path("mcp/tests/test_plain.py")],
            ),
            admission=forged,
            coverage_json=None,
            threshold=30.0,
            top=5,
        )
        with self.assertRaises(DaggerAdmissionError):
            check.quality_steps(config, self.root / "coverage.json")

        inputs = retry_proof.RetryInputs(
            project_root=self.root,
            targeted=False,
            base_revision="base",
            threshold=30.0,
            top=5,
            diff_floor=100.0,
            coverage_paths=(),
            test_arguments=(Path("mcp/tests/test_plain.py"),),
            untracked_paths=(),
            cache_root=self.root / "retry-cache",
            lane_digest="lane-digest",
            lane_trigger="release",
            lane_population=("accept=release",),
        )
        with self.assertRaises(DaggerAdmissionError):
            retry_proof.prepare(inputs, admission=forged, printer=lambda _line: None)

    def test_copying_or_renaming_diagnostic_output_cannot_create_publication_authority(
        self,
    ) -> None:
        exported = self.root / "exported"
        exported.mkdir()
        reports = self.root / "reports"
        (exported / "clean-quality-results.json").write_text(
            json.dumps(
                {
                    "schemaVersion": "non-accepting-investigation/v1",
                    "status": "passed",
                    "acceptanceEligible": False,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "invalid pipeline exit code"):
            clean_quality_executor._publish_reports(  # pyright: ignore[reportPrivateUsage]
                exported,
                reports,
                candidate_tree="a" * 40,
                profile_execution=agents_remember_profile_execution(candidate_tree="a" * 40),
            )
        self.assertFalse(reports.exists())

    def test_verified_dagger_generation_mints_evidence_for_lifecycle_consumers(self) -> None:
        exported = self.root / "exported"
        exported.mkdir()
        result = exported / "clean-quality-results.json"
        result.write_text('{"status":"passed","exitCode":0}\n', encoding="utf-8")
        reports = self.root / "reports"
        clean_quality_executor._publish_reports(  # pyright: ignore[reportPrivateUsage]
            exported,
            reports,
            candidate_tree="b" * 40,
            profile_execution=agents_remember_profile_execution(candidate_tree="b" * 40),
        )

        for consumer in (
            EvidenceConsumer.QUALITY,
            EvidenceConsumer.LIFECYCLE,
            EvidenceConsumer.CLOSEOUT,
            EvidenceConsumer.INTEGRATION,
        ):
            with self.subTest(consumer=consumer):
                evidence = clean_quality_executor.require_published_quality_evidence(
                    reports,
                    candidate_tree="b" * 40,
                    consumer=consumer,
                )
                self.assertEqual(evidence.candidate_tree, "b" * 40)
                self.assertEqual(len(evidence.result_sha256), 64)

        with self.assertRaisesRegex(RuntimeError, "another candidate tree"):
            clean_quality_executor.require_published_quality_evidence(
                reports,
                candidate_tree="c" * 40,
                consumer=EvidenceConsumer.CLOSEOUT,
            )

        published = clean_quality_executor.published_report_path(
            reports,
            "clean-quality-results.json",
        )
        published.write_text("tampered", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            clean_quality_executor.require_published_quality_evidence(
                reports,
                candidate_tree="b" * 40,
                consumer=EvidenceConsumer.INTEGRATION,
            )

    def test_failed_dagger_generation_cannot_mint_acceptance_authority(self) -> None:
        exported = self.root / "failed"
        exported.mkdir()
        (exported / "clean-quality-results.json").write_text(
            '{"status":"failed","exitCode":7}\n',
            encoding="utf-8",
        )
        reports = self.root / "reports"
        clean_quality_executor._publish_reports(  # pyright: ignore[reportPrivateUsage]
            exported,
            reports,
            candidate_tree="d" * 40,
            profile_execution=agents_remember_profile_execution(candidate_tree="d" * 40),
        )

        with self.assertRaisesRegex(RuntimeError, "did not pass acceptance"):
            clean_quality_executor.require_published_quality_evidence(
                reports,
                candidate_tree="d" * 40,
                consumer=EvidenceConsumer.QUALITY,
            )


if __name__ == "__main__":
    unittest.main()
