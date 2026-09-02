from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents_remember.worktrees.modules.quality import (
    clean_executor,
    published_manifest,
    report_publication_paths,
    result_artifacts,
)

CANDIDATE_TREE = "c" * 40


def _source(root: Path, name: str, *, attempt: str) -> Path:
    source = root / name
    source.mkdir()
    (source / "clean-quality-results.json").write_text(
        json.dumps({"status": "passed", "exitCode": 0, "attempt": attempt}) + "\n",
        encoding="utf-8",
    )
    return source


class QualityReportPublicationSecurityTests(unittest.TestCase):
    def test_result_cannot_reference_an_artifact_outside_the_export_inventory(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "clean-quality-results.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "exitCode": 0,
                        "completedSteps": ["quality-wrapper"],
                        "causalFailureReport": "causal-failures.json",
                        "causalFailureSummary": "causal-failures.md",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "references missing report artifacts"):
                clean_executor._publish_reports(
                    source,
                    root / "reports",
                    candidate_tree=CANDIDATE_TREE,
                )
            self.assertFalse((root / "reports/quality-report-set.json").exists())

    def test_nested_legacy_directory_symlink_cannot_delete_external_reports(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            reports = root / "reports"
            clean_executor._publish_reports(
                _source(root, "old", attempt="old"),
                reports,
                candidate_tree=CANDIDATE_TREE,
            )
            pointer_before = (reports / clean_executor.REPORT_SET_MANIFEST).read_bytes()
            external = root / "external"
            external.mkdir()
            external_run = external / "run-1.json"
            external_run.write_text("must survive\n", encoding="utf-8")
            (reports / "ambient-role-chat-e2e").symlink_to(external, target_is_directory=True)

            candidate = _source(root, "candidate", attempt="candidate")
            nested = candidate / "ambient-role-chat-e2e"
            nested.mkdir()
            (nested / "run-1.json").write_text("candidate\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unsafe legacy report directory"):
                clean_executor._publish_reports(
                    candidate,
                    reports,
                    candidate_tree=CANDIDATE_TREE,
                )

            self.assertEqual(external_run.read_text(encoding="utf-8"), "must survive\n")
            self.assertEqual(
                (reports / clean_executor.REPORT_SET_MANIFEST).read_bytes(),
                pointer_before,
            )

    def test_generation_symlink_cannot_substitute_external_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            reports = root / "reports"
            clean_executor._publish_reports(
                _source(root, "old", attempt="old"),
                reports,
                candidate_tree=CANDIDATE_TREE,
            )
            pointer_before = (reports / clean_executor.REPORT_SET_MANIFEST).read_bytes()
            candidate = _source(root, "candidate", attempt="candidate")
            names = clean_executor._validated_export_inventory(candidate)
            records = clean_executor._report_file_records(candidate, names)
            dependencies = clean_executor.quality_report_dependencies(
                CANDIDATE_TREE, records, None
            ).model_dump(mode="json")
            generation = clean_executor._generation_digest(CANDIDATE_TREE, records, dependencies)
            external = root / "external-generation"
            external.mkdir()
            sentinel = external / "sentinel"
            sentinel.write_text("must survive\n", encoding="utf-8")
            generation_root = reports / clean_executor.REPORT_GENERATIONS_DIRECTORY / generation
            generation_root.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "unsafe quality generation"):
                clean_executor._publish_reports(
                    candidate,
                    reports,
                    candidate_tree=CANDIDATE_TREE,
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "must survive\n")
            self.assertEqual(
                (reports / clean_executor.REPORT_SET_MANIFEST).read_bytes(),
                pointer_before,
            )

    def test_historical_generation_symlink_refuses_before_pointer_moves(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            reports = root / "reports"
            clean_executor._publish_reports(
                _source(root, "old", attempt="old"),
                reports,
                candidate_tree=CANDIDATE_TREE,
            )
            pointer_before = (reports / clean_executor.REPORT_SET_MANIFEST).read_bytes()
            external = root / "external-history"
            external.mkdir()
            sentinel = external / "sentinel"
            sentinel.write_text("must survive\n", encoding="utf-8")
            historical = reports / clean_executor.REPORT_GENERATIONS_DIRECTORY / ("f" * 64)
            historical.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "unsafe published quality generation"):
                clean_executor._publish_reports(
                    _source(root, "candidate", attempt="candidate"),
                    reports,
                    candidate_tree=CANDIDATE_TREE,
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "must survive\n")
            self.assertEqual(
                (reports / clean_executor.REPORT_SET_MANIFEST).read_bytes(),
                pointer_before,
            )
            selected = clean_executor.published_report_path(
                reports,
                "clean-quality-results.json",
            )
            self.assertIn('"attempt": "old"', selected.read_text(encoding="utf-8"))

    def test_authoritative_result_references_are_typed_and_step_owned(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            result = source / "clean-quality-results.json"

            with self.assertRaisesRegex(RuntimeError, "no valid authoritative result"):
                result_artifacts.validate_result_artifact_references(root / "missing", set())

            result.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no valid authoritative result"):
                result_artifacts.validate_result_artifact_references(source, set())

            result.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "must be an object"):
                result_artifacts.validate_result_artifact_references(source, set())

            invalid_payloads = (
                (
                    {"completedSteps": "quality-wrapper"},
                    "completedSteps must be a string list",
                ),
                (
                    {"completedSteps": [], "ambientRoleChatEvidence": "invalid"},
                    "ambientRoleChatEvidence must be an object",
                ),
                (
                    {"completedSteps": [], "ambientRoleChatEvidence": {"runs": "invalid"}},
                    "ambientRoleChatEvidence.runs must be a list",
                ),
                (
                    {"completedSteps": [], "ambientRoleChatEvidence": {"runs": ["../escape"]}},
                    "contains an invalid artifact reference",
                ),
            )
            for payload, message in invalid_payloads:
                with self.subTest(message=message):
                    result.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, message):
                        result_artifacts.validate_result_artifact_references(source, set())

            result.write_text(
                json.dumps(
                    {
                        "completedSteps": ["quality-wrapper"],
                        "causalFailureReport": "causal-failures.json",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "omitted its causal artifact references"):
                result_artifacts.validate_result_artifact_references(
                    source,
                    {"causal-failures.json"},
                )

            causal = {"causal-failures.json", "causal-failures.md"}
            result.write_text(
                json.dumps(
                    {
                        "completedSteps": [],
                        "causalFailureReport": "causal-failures.json",
                        "causalFailureSummary": "causal-failures.md",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "incomplete quality-wrapper claimed causal artifact references",
            ):
                result_artifacts.validate_result_artifact_references(source, causal)

            complete = {
                "completedSteps": ["quality-wrapper"],
                "causalFailureReport": "causal-failures.json",
                "causalFailureSummary": "causal-failures.md",
                "ambientRoleChatEvidence": {
                    "summary": "ambient/summary.json",
                    "runs": ["ambient/run-1.json"],
                },
            }
            result.write_text(json.dumps(complete), encoding="utf-8")
            result_artifacts.validate_result_artifact_references(
                source,
                causal | {"ambient/summary.json", "ambient/run-1.json"},
            )

            result.write_text("{}", encoding="utf-8")
            result_artifacts.validate_result_artifact_references(source, set())

    def test_publication_path_boundaries_cover_irregular_and_cleanup_states(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            not_directory = root / "not-directory"
            not_directory.write_text("file", encoding="utf-8")
            with self.assertRaisesRegex(
                published_manifest.PublishedQualityManifestError,
                "must be a real directory",
            ):
                published_manifest.require_real_directory_or_missing(
                    not_directory,
                    purpose="test directory",
                )

            not_file = root / "not-file"
            not_file.mkdir()
            with self.assertRaisesRegex(
                published_manifest.PublishedQualityManifestError,
                "must be a regular file",
            ):
                published_manifest.require_real_file_or_missing(
                    not_file,
                    purpose="test file",
                )

            with (
                mock.patch.object(Path, "lstat", autospec=True, side_effect=OSError("inspect")),
                self.assertRaisesRegex(
                    published_manifest.PublishedQualityManifestError,
                    "cannot inspect test directory",
                ),
            ):
                published_manifest.require_real_directory_or_missing(
                    root / "unreadable-directory",
                    purpose="test directory",
                )

            with (
                mock.patch.object(Path, "lstat", autospec=True, side_effect=OSError("inspect")),
                self.assertRaisesRegex(
                    published_manifest.PublishedQualityManifestError,
                    "cannot inspect test file",
                ),
            ):
                published_manifest.require_real_file_or_missing(
                    root / "unreadable-file",
                    purpose="test file",
                )

            self.assertFalse(published_manifest.is_safe_relative_report_path(""))
            self.assertFalse(published_manifest.is_safe_relative_report_path("bad\\name"))
            with self.assertRaisesRegex(ValueError, "file record is invalid"):
                published_manifest._parse_file("result.json", [])

            inventory = root / "inventory"
            inventory.mkdir()
            (inventory / "directory").mkdir()
            (inventory / "file").write_text("file", encoding="utf-8")
            (inventory / "link").symlink_to(inventory / "file")
            os.mkfifo(inventory / "pipe")
            files, directories, irregular = report_publication_paths.report_tree_inventory(
                inventory
            )
            self.assertEqual(files, {"file"})
            self.assertEqual(directories, {"directory"})
            self.assertEqual(irregular, {"link", "pipe"})

            legacy = root / "legacy"
            legacy.mkdir()
            nonempty = legacy / "nonempty"
            nonempty.mkdir()
            (nonempty / "sentinel").write_text("keep", encoding="utf-8")
            report_publication_paths.remove_legacy_report_projection(
                legacy,
                exported_files={"missing.json"},
                exported_directories={"missing", "nonempty"},
            )
            self.assertTrue((nonempty / "sentinel").exists())

            generation = root / "generation"
            generation.mkdir()
            (generation / "unexpected.json").write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "undeclared path"):
                clean_executor._validate_generation(generation, {})


if __name__ == "__main__":
    unittest.main()
