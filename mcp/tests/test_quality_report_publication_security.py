from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agents_remember.worktrees.modules.quality import (
    clean_executor,
)
from repository_profile_test_support import (
    agents_remember_profile_execution,
    write_source_selection_artifacts,
)

CANDIDATE_TREE = "c" * 40


def _profile_execution():
    return agents_remember_profile_execution(candidate_tree=CANDIDATE_TREE)


def _publish_reports(source: Path, destination: Path):
    return clean_executor._publish_reports(
        source,
        destination,
        candidate_tree=CANDIDATE_TREE,
        profile_execution=_profile_execution(),
    )


def _source(root: Path, name: str, *, attempt: str) -> Path:
    source = root / name
    source.mkdir()
    (source / "clean-quality-results.json").write_text(
        json.dumps({"status": "passed", "exitCode": 0, "attempt": attempt}) + "\n",
        encoding="utf-8",
    )
    write_source_selection_artifacts(source, _profile_execution().plan)
    return source


class QualityReportPublicationSecurityTests(unittest.TestCase):
    def test_export_cannot_publish_an_artifact_outside_the_profile_inventory(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "clean-quality-results.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "exitCode": 0,
                        "repositoryOwnedDetail": "not interpreted by the framework",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (source / "undeclared.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "unexpected report files"):
                _publish_reports(source, root / "reports")
            self.assertFalse((root / "reports/quality-report-set.json").exists())

    def test_nested_legacy_directory_symlink_cannot_delete_external_reports(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            reports = root / "reports"
            _publish_reports(_source(root, "old", attempt="old"), reports)
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
                _publish_reports(candidate, reports)

            self.assertEqual(external_run.read_text(encoding="utf-8"), "must survive\n")
            self.assertEqual(
                (reports / clean_executor.REPORT_SET_MANIFEST).read_bytes(),
                pointer_before,
            )

    def test_generation_symlink_cannot_substitute_external_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            reports = root / "reports"
            _publish_reports(_source(root, "old", attempt="old"), reports)
            pointer_before = (reports / clean_executor.REPORT_SET_MANIFEST).read_bytes()
            candidate = _source(root, "candidate", attempt="candidate")
            names = clean_executor._validated_export_inventory(
                candidate,
                _profile_execution().admitted.canonical.profile.publishedArtifacts,
            )
            records = clean_executor._report_file_records(candidate, names)
            profile_execution = _profile_execution()
            profile_identity = clean_executor._profile_identity(profile_execution)
            dependencies = clean_executor.quality_report_dependencies(
                CANDIDATE_TREE, records, None, profile_identity
            ).model_dump(mode="json")
            generation = clean_executor._generation_digest(
                CANDIDATE_TREE,
                records,
                profile_identity,
                dependencies,
            )
            external = root / "external-generation"
            external.mkdir()
            sentinel = external / "sentinel"
            sentinel.write_text("must survive\n", encoding="utf-8")
            generation_root = reports / clean_executor.REPORT_GENERATIONS_DIRECTORY / generation
            generation_root.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "unsafe quality generation"):
                _publish_reports(candidate, reports)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "must survive\n")
            self.assertEqual(
                (reports / clean_executor.REPORT_SET_MANIFEST).read_bytes(),
                pointer_before,
            )


if __name__ == "__main__":
    unittest.main()
