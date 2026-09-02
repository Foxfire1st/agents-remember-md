"""Focused public-contract proofs for immutable quality-result recovery."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents_remember.models.tools.tool_response import finalize_tool_response
from agents_remember.models.worktree import WorktreeIntegrateResponse
from agents_remember.worktrees.modules.quality import clean_executor as clean_quality_executor
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from pydantic import ValidationError
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    REPOSITORY_ROOT,
    agents_remember_profile_execution,
)


class QualityGatePublicContractTests(unittest.TestCase):
    def test_recovery_refuses_same_id_decoder_byte_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_tree = "c" * 40
            export = root / "export"
            reports = root / "reports"
            export.mkdir()
            (export / "clean-quality-results.json").write_text(
                json.dumps({"status": "passed", "exitCode": 0}) + "\n",
                encoding="utf-8",
            )
            clean_quality_executor._publish_reports(
                export,
                reports,
                candidate_tree=candidate_tree,
                profile_execution=agents_remember_profile_execution(candidate_tree=candidate_tree),
                attestation={"id": "decoder-drift"},
            )
            pointer = reports / clean_quality_executor.REPORT_SET_MANIFEST
            manifest = json.loads(pointer.read_text(encoding="utf-8"))
            manifest["resultDecoder"]["passedValue"] = "accepted"
            pointer.write_text(json.dumps(manifest), encoding="utf-8")
            target = code_quality_gate.QualityGateTarget(
                REPOSITORY_ROOT,
                root,
                "agents-remember",
                AGENTS_REMEMBER_PROFILE_REFERENCE,
            )

            with mock.patch.object(
                code_quality_gate,
                "require_git",
                return_value=candidate_tree,
            ):
                recovered = code_quality_gate.recover_strict_code_quality_gate(
                    target,
                    diff_base="a" * 40,
                    plan=code_quality_gate.QualityGatePlan(mode="full"),
                    attestation={"id": "decoder-drift"},
                )

            self.assertIsNone(recovered)

    def test_recovery_uses_one_manifest_generation_when_the_pointer_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_tree = "c" * 40
            export = root / "export"
            reports = root / "reports"
            export.mkdir()
            result_path = export / "clean-quality-results.json"
            result_path.write_text(
                json.dumps({"status": "passed", "exitCode": 0, "generation": "a"}) + "\n",
                encoding="utf-8",
            )
            generation_a = clean_quality_executor._publish_reports(
                export,
                reports,
                candidate_tree=candidate_tree,
                profile_execution=agents_remember_profile_execution(candidate_tree=candidate_tree),
                attestation={"id": "a"},
            )["generation"]
            real_loader = code_quality_gate.load_published_quality_manifest

            def rotate_after_snapshot(destination: Path):
                snapshot = real_loader(destination)
                result_path.write_text(
                    json.dumps({"status": "failed", "exitCode": 1, "generation": "b"}) + "\n",
                    encoding="utf-8",
                )
                clean_quality_executor._publish_reports(
                    export,
                    reports,
                    candidate_tree=candidate_tree,
                    profile_execution=agents_remember_profile_execution(
                        candidate_tree=candidate_tree
                    ),
                    attestation={"id": "b"},
                )
                return snapshot

            target = code_quality_gate.QualityGateTarget(
                REPOSITORY_ROOT,
                root,
                "agents-remember",
                AGENTS_REMEMBER_PROFILE_REFERENCE,
            )
            plan = code_quality_gate.QualityGatePlan(mode="full")
            with (
                mock.patch.object(
                    code_quality_gate,
                    "load_published_quality_manifest",
                    side_effect=rotate_after_snapshot,
                ) as loader,
                mock.patch.object(
                    code_quality_gate,
                    "require_git",
                    return_value=candidate_tree,
                ),
            ):
                recovered = code_quality_gate.recover_strict_code_quality_gate(
                    target,
                    diff_base="a" * 40,
                    plan=plan,
                    attestation={"id": "a"},
                )

            assert recovered is not None
            self.assertEqual(loader.call_count, 1)
            published = Path(str(recovered["publishedResultPath"]))
            self.assertEqual(published.parent.name, generation_a)
            self.assertEqual(
                json.loads(published.read_text(encoding="utf-8"))["generation"],
                "a",
            )
            current = real_loader(reports)
            self.assertNotEqual(current.generation, generation_a)
            self.assertEqual(dict(current.attestation or {}), {"id": "b"})

    def test_public_worktree_response_models_and_retains_both_quality_paths(self) -> None:
        quality_result = {
            "required": True,
            "status": "enforced",
            "passed": True,
            "command": "dagger call quality",
            "diffBase": "a" * 40,
            "mode": "full",
            "executor": "dagger",
            "reportPath": "/enclosure/reports/test-results.md",
            "publishedResultPath": (
                "/enclosure/reports/.quality-report-generations/"
                f"{'b' * 64}/clean-quality-results.json"
            ),
        }

        payload = finalize_tool_response(
            "worktree_integrate",
            {
                "ok": True,
                "operation": "worktree_integrate",
                "quality_gate": quality_result,
            },
        )

        self.assertEqual(payload["quality_gate"]["reportPath"], quality_result["reportPath"])
        self.assertEqual(
            payload["quality_gate"]["publishedResultPath"],
            quality_result["publishedResultPath"],
        )
        self.assertGreater(payload["tokens"], 0)
        schema = WorktreeIntegrateResponse.model_json_schema()
        quality_schema = json.dumps(schema["$defs"]["QualityGateResult"], sort_keys=True)
        self.assertIn('"reportPath"', quality_schema)
        self.assertIn('"publishedResultPath"', quality_schema)

        with self.assertRaises(ValidationError):
            finalize_tool_response(
                "worktree_integrate",
                {
                    "ok": True,
                    "operation": "worktree_integrate",
                    "quality_gate": {**quality_result, "unmodeledPath": "/private"},
                },
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
