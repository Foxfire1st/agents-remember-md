from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import organizational_completion_test_support as fixture_mod
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.models.test_evidence import _certifying_evidence_from_verified_dagger
from agents_remember.worktrees.integration import integration_quality as quality
from agents_remember.worktrees.integration import integration_ref_transaction as ref_transaction
from agents_remember.worktrees.integration import (
    organizational_completion_integration as completion,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules.quality import clean_executor as clean_quality_executor
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.series_closeout import atomic_series_ledger_prefix
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    REPOSITORY_ROOT,
    agents_remember_profile_execution,
)


class L5QualityAndRecoveryEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = fixture_mod.OrganizationalCompletionFixture()
        self.owner.setUp()

    def tearDown(self) -> None:
        try:
            self.owner.doCleanups()
        finally:
            self.owner.tearDown()

    def test_published_quality_attestation_and_result_failure_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export = root / "export"
            reports = root / "reports"
            export.mkdir()
            result_path = export / "clean-quality-results.json"
            result_path.write_text("not json\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no valid authoritative result"):
                clean_quality_executor._publish_reports(
                    export,
                    reports,
                    candidate_tree="c" * 40,
                    profile_execution=agents_remember_profile_execution(candidate_tree="c" * 40),
                    attestation={"id": "one"},
                )
            target = code_quality_gate.QualityGateTarget(
                code_worktree=REPOSITORY_ROOT,
                worktree_group=root,
                repository_id="agents-remember",
                profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
            )
            plan = code_quality_gate.QualityGatePlan(mode="full")

            result_path.write_text(
                json.dumps({"status": "failed", "exitCode": 1}) + "\n",
                encoding="utf-8",
            )
            clean_quality_executor._publish_reports(
                export,
                reports,
                candidate_tree="c" * 40,
                profile_execution=agents_remember_profile_execution(candidate_tree="c" * 40),
                attestation={"id": "one"},
            )
            self.assertIsNone(
                code_quality_gate.recover_strict_code_quality_gate(
                    target,
                    diff_base="a" * 40,
                    plan=plan,
                    attestation={"id": "one"},
                )
            )

            manifest_path = reports / clean_quality_executor.REPORT_SET_MANIFEST
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("attestation")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no complete Dagger report"):
                clean_quality_executor.published_quality_attestation(reports)
            manifest["attestation"] = "invalid"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no complete Dagger report"):
                clean_quality_executor.published_quality_attestation(reports)

    def test_manifest_shape_is_object_root_and_shared_by_both_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            reports.mkdir()
            manifest_path = reports / clean_quality_executor.REPORT_SET_MANIFEST
            hostile_roots = (
                [],
                None,
                True,
                "generation",
                {"schemaVersion": "2.0", "generation": "a" * 64, "files": {}},
                {"schemaVersion": "1.0", "generation": "a" * 64, "files": []},
                {
                    "schemaVersion": "1.0",
                    "generation": "a" * 64,
                    "files": {"result.json": {"sha256": "a" * 64, "size": "1"}},
                },
            )
            for hostile in hostile_roots:
                with self.subTest(hostile=hostile):
                    manifest_path.write_text(json.dumps(hostile), encoding="utf-8")
                    errors = []
                    for consume in (
                        lambda: clean_quality_executor.published_report_path(
                            reports, "result.json"
                        ),
                        lambda: clean_quality_executor.published_quality_attestation(reports),
                    ):
                        with self.assertRaises(RuntimeError) as raised:
                            consume()
                        errors.append((type(raised.exception), str(raised.exception)))
                    self.assertEqual(errors[0], errors[1])
                    self.assertEqual(
                        errors[0][1],
                        "no complete Dagger report generation is published",
                    )

    def test_recovery_preserves_wrapper_path_and_exposes_published_result_separately(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export = root / "export"
            reports = root / "reports"
            export.mkdir()
            (export / "clean-quality-results.json").write_text(
                json.dumps({"status": "passed", "exitCode": 0}) + "\n",
                encoding="utf-8",
            )
            candidate_tree = "c" * 40
            clean_quality_executor._publish_reports(
                export,
                reports,
                candidate_tree=candidate_tree,
                profile_execution=agents_remember_profile_execution(candidate_tree=candidate_tree),
                attestation={"id": "one"},
            )
            target = code_quality_gate.QualityGateTarget(
                code_worktree=REPOSITORY_ROOT,
                worktree_group=root,
                repository_id="agents-remember",
                profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
            )
            plan = code_quality_gate.QualityGatePlan(mode="full")
            manifest = clean_quality_executor.load_published_quality_manifest(reports)
            fresh = code_quality_gate._strict_quality_success_payload(
                target,
                diff_base="a" * 40,
                plan=plan,
                evidence=_certifying_evidence_from_verified_dagger(
                    candidate_tree=candidate_tree,
                    result_sha256="d" * 64,
                ),
                manifest=manifest,
            )
            with mock.patch.object(
                code_quality_gate,
                "require_git",
                return_value=candidate_tree,
            ):
                recovered = code_quality_gate.recover_strict_code_quality_gate(
                    target,
                    diff_base="a" * 40,
                    plan=plan,
                    attestation={"id": "one"},
                )
            assert recovered is not None
            self.assertEqual(recovered["reportPath"], fresh["reportPath"])
            self.assertEqual(Path(str(fresh["reportPath"])).name, "test-results.md")
            self.assertEqual(recovered["publishedResultPath"], fresh["publishedResultPath"])
            published = Path(str(recovered["publishedResultPath"]))
            self.assertEqual(published.name, "clean-quality-results.json")
            self.assertEqual(json.loads(published.read_text(encoding="utf-8"))["exitCode"], 0)

            (export / "clean-quality-results.json").write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "must be a JSON object"):
                clean_quality_executor._publish_reports(
                    export,
                    reports,
                    candidate_tree=candidate_tree,
                    profile_execution=agents_remember_profile_execution(
                        candidate_tree=candidate_tree
                    ),
                    attestation={"id": "two"},
                )

    def test_organizational_gate_returns_a_certificate_without_a_sink(self) -> None:
        contract = self.owner._certified_contract(final=True)
        plan = completion.preview_organizational_completion(contract)
        assert plan is not None
        with (
            mock.patch.object(quality, "requires_strict_code_quality", return_value=True),
            mock.patch.object(
                quality,
                "run_strict_code_quality_gate",
                side_effect=fixture_mod._full_gate(contract),
            ) as gate,
        ):
            outcome = quality.run_integration_quality_gate(
                contract,
                completion=plan,
                profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
            )
        gate.assert_called_once()
        self.assertIsNotNone(outcome.certification)

    def test_public_ledger_and_series_prefix_preconditions_refuse_wrong_contracts(self) -> None:
        contract = self.owner._certified_contract(final=True)
        with self.assertRaisesRegex(RuntimeError, "leaf or series"):
            ref_transaction.require_integrated_ledger_mapping(
                replace(contract, kind="invalid"),  # type: ignore[arg-type]
                ref_transaction.IntegratedCommits("a" * 40, "b" * 40, "c" * 40),
                memory_source_commit="d" * 40,
            )
        with self.assertRaisesRegex(RuntimeError, "external-memory series"):
            atomic_series_ledger_prefix(replace(contract, kind="leaf"))

    def test_closeout_wal_cannot_claim_an_integration_quality_failure(self) -> None:
        contract = self.owner._certified_contract(final=True)
        closeout = LifecycleOperationStore(
            operation_record_path(contract.worktree_group, "closeout")
        ).read()
        assert closeout is not None
        with self.assertRaisesRegex(ValueError, "belongs to integration only"):
            LifecycleOperationRecord.model_validate(
                {
                    **closeout.model_dump(mode="json"),
                    "result": {"state": "organizational-completion-gate-failed"},
                }
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
