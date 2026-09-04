from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles import (
    AdmittedRepositoryProfile,
    admit_repository_profile_execution,
    canonicalize_repository_profile,
)
from agents_remember.worktrees.modules.quality import (
    clean_executor,
    published_manifest,
    report_publication_paths,
)
from repository_profile_test_support import (
    NODE_FIXTURE,
    agents_remember_profile_execution,
    fixture_profile,
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
    return source


class QualityReportPublicationSecurityTests(unittest.TestCase):
    def test_generation_digest_refuses_manifest_decoder_field_drift(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            reports = root / "reports"
            _publish_reports(_source(root, "candidate", attempt="candidate"), reports)
            pointer = reports / clean_executor.REPORT_SET_MANIFEST
            manifest = json.loads(pointer.read_text(encoding="utf-8"))
            manifest["resultDecoder"]["passedValue"] = "accepted"
            pointer.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                published_manifest.PublishedQualityManifestError,
                "no complete Dagger report generation",
            ):
                published_manifest.load_published_quality_manifest(reports)

    def test_manifest_rejects_an_invalid_runtime_authority_digest(self) -> None:
        """A forged or malformed host-authority digest must not parse as a pointer."""
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            reports = root / "reports"
            _publish_reports(_source(root, "candidate", attempt="candidate"), reports)
            pointer = reports / clean_executor.REPORT_SET_MANIFEST
            manifest = json.loads(pointer.read_text(encoding="utf-8"))
            manifest["runtimeAuthorityDigest"] = "not-a-64-hex-digest"
            pointer.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                published_manifest.PublishedQualityManifestError,
                "no complete Dagger report generation",
            ):
                published_manifest.load_published_quality_manifest(reports)

    def test_failed_terminal_result_publishes_without_pass_only_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            terminal = {
                "status": "failed",
                "exit-code": 9,
                "attemptedSteps": ["static-quality"],
                "failedStep": "static-quality",
                "stepExitCodes": {"static-quality": 9},
            }
            (source / "result.json").write_text(
                json.dumps(terminal) + "\n",
                encoding="utf-8",
            )
            canonical = canonicalize_repository_profile(fixture_profile(NODE_FIXTURE))
            admitted = AdmittedRepositoryProfile(
                repository_id=NODE_FIXTURE.repository_id,
                repository_root=root,
                source_path=root / "profile.json",
                source_sha256="a" * 64,
                canonical=canonical,
            )
            execution = admit_repository_profile_execution(
                admitted,
                purpose="closeout",
                mode="full",
                candidate_identity=CandidateIdentity(kind="git-tree", value=CANDIDATE_TREE),
            )

            manifest = clean_executor._publish_reports(
                source,
                root / "reports",
                candidate_tree=CANDIDATE_TREE,
                profile_execution=execution,
            )

            self.assertEqual(set(cast(dict[str, object], manifest["files"])), {"result.json"})
            published = clean_executor.published_report_path(root / "reports", "result.json")
            self.assertEqual(json.loads(published.read_text(encoding="utf-8")), terminal)

    def test_nested_decoder_artifact_resolves_from_the_generation_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            reports = Path(temporary) / "reports"
            execution = _profile_execution()
            decoder = execution.decoder.model_copy(update={"artifactPath": "nested/result.json"})
            generation = "d" * 64
            result = (
                reports
                / clean_executor.REPORT_GENERATIONS_DIRECTORY
                / generation
                / decoder.artifactPath
            )
            result.parent.mkdir(parents=True)
            payload = json.dumps({"status": "passed", "exitCode": 0}).encode("utf-8")
            result.write_bytes(payload)
            file_values: dict[str, dict[str, object]] = {
                decoder.artifactPath: {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
            }
            profile_identity = {
                "profileDigest": execution.admitted.canonical.profileDigest,
                "profilePlanDigest": execution.plan.planDigest,
                "profileSelectionId": execution.selection.selectionId,
                "executorAdapterId": execution.executor.adapterId,
                "resultDecoder": decoder.model_dump(mode="json"),
            }
            manifest = published_manifest.PublishedQualityManifest(
                schema_version=published_manifest.QUALITY_MANIFEST_SCHEMA_VERSION,
                generation=generation,
                candidate_tree=CANDIDATE_TREE,
                profile_digest=execution.admitted.canonical.profileDigest,
                profile_plan_digest=execution.plan.planDigest,
                profile_selection_id=execution.selection.selectionId,
                executor_adapter_id=execution.executor.adapterId,
                result_decoder=decoder,
                files={
                    decoder.artifactPath: published_manifest.PublishedQualityFile(
                        sha256=cast(str, file_values[decoder.artifactPath]["sha256"]),
                        size=cast(int, file_values[decoder.artifactPath]["size"]),
                    )
                },
                attestation=None,
                runtime_authority_digest=None,
                dependencies=published_manifest.quality_report_dependencies(
                    CANDIDATE_TREE,
                    file_values,
                    None,
                    profile_identity,
                ),
            )

            evidence = clean_executor.certifying_evidence_from_published_manifest(
                reports,
                manifest,
                candidate_tree=CANDIDATE_TREE,
            )

            self.assertEqual(evidence.candidate_tree, CANDIDATE_TREE)

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

    def test_profile_decoder_preserves_result_artifact_reference_failures(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            cases = (
                (
                    {"completedSteps": "causal-preflight"},
                    (),
                    "completedSteps must be a string list",
                ),
                (
                    {"ambientRoleChatEvidence": "invalid"},
                    (),
                    "ambientRoleChatEvidence must be a JSON object",
                ),
                (
                    {"ambientRoleChatEvidence": {"runs": "invalid"}},
                    (),
                    "ambientRoleChatEvidence.runs must be a string list",
                ),
                (
                    {"ambientRoleChatEvidence": {"runs": ["../escape.json"]}},
                    (),
                    "contains an invalid artifact reference",
                ),
                (
                    {"causalFailureReport": "causal-failures.json"},
                    (),
                    "references missing published artifacts",
                ),
                (
                    {
                        "completedSteps": ["causal-preflight"],
                        "causalFailureReport": "causal-failures.json",
                    },
                    ("causal-failures.json",),
                    "omitted required artifact references",
                ),
                (
                    {
                        "completedSteps": [],
                        "causalFailureReport": "causal-failures.json",
                        "causalFailureSummary": "causal-failures.md",
                    },
                    ("causal-failures.json", "causal-failures.md"),
                    "claimed artifact references",
                ),
            )
            for index, (extra, artifact_names, message) in enumerate(cases):
                with self.subTest(message=message):
                    source = root / f"invalid-{index}"
                    source.mkdir()
                    payload = {"status": "passed", "exitCode": 0, **extra}
                    (source / "clean-quality-results.json").write_text(
                        json.dumps(payload) + "\n",
                        encoding="utf-8",
                    )
                    for name in artifact_names:
                        (source / name).write_text("evidence\n", encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, message):
                        _publish_reports(source, root / f"reports-{index}")

            complete = root / "complete"
            complete.mkdir()
            artifacts = (
                "causal-failures.json",
                "causal-failures.md",
                "ambient-role-chat-e2e/summary.json",
                "ambient-role-chat-e2e/run-1.json",
                "ambient-role-chat-e2e/run-2.json",
            )
            payload = {
                "status": "passed",
                "exitCode": 0,
                "completedSteps": ["causal-preflight"],
                "causalFailureReport": artifacts[0],
                "causalFailureSummary": artifacts[1],
                "ambientRoleChatEvidence": {
                    "summary": artifacts[2],
                    "runs": [artifacts[3], artifacts[4]],
                },
            }
            (complete / "clean-quality-results.json").write_text(
                json.dumps(payload) + "\n",
                encoding="utf-8",
            )
            for name in artifacts:
                destination = complete / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("evidence\n", encoding="utf-8")

            manifest = _publish_reports(complete, root / "reports-complete")

            self.assertEqual(manifest["profileDigest"], _profile_execution().plan.profileDigest)

            selector_absent = root / "selector-absent"
            selector_absent.mkdir()
            absent_payload = {
                "status": "passed",
                "exitCode": 0,
                "causalFailureReport": artifacts[0],
                "causalFailureSummary": artifacts[1],
            }
            (selector_absent / "clean-quality-results.json").write_text(
                json.dumps(absent_payload) + "\n",
                encoding="utf-8",
            )
            for name in artifacts[:2]:
                (selector_absent / name).write_text("evidence\n", encoding="utf-8")

            _publish_reports(selector_absent, root / "reports-selector-absent")

            nullable_optionals = root / "nullable-optionals"
            nullable_optionals.mkdir()
            nullable_payload = {
                "status": "passed",
                "exitCode": 0,
                "completedSteps": None,
                "causalFailureReport": artifacts[0],
                "causalFailureSummary": artifacts[1],
                "ambientRoleChatEvidence": None,
            }
            (nullable_optionals / "clean-quality-results.json").write_text(
                json.dumps(nullable_payload) + "\n",
                encoding="utf-8",
            )
            for name in artifacts[:2]:
                (nullable_optionals / name).write_text("evidence\n", encoding="utf-8")

            _publish_reports(nullable_optionals, root / "reports-nullable-optionals")

            active_null = root / "active-null"
            active_null.mkdir()
            null_payload = {
                "status": "passed",
                "exitCode": 0,
                "completedSteps": ["causal-preflight"],
                "causalFailureReport": None,
                "causalFailureSummary": artifacts[1],
            }
            (active_null / "clean-quality-results.json").write_text(
                json.dumps(null_payload) + "\n",
                encoding="utf-8",
            )
            (active_null / artifacts[1]).write_text("evidence\n", encoding="utf-8")

            _publish_reports(active_null, root / "reports-active-null")

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

    def test_historical_generation_symlink_refuses_before_pointer_moves(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            reports = root / "reports"
            _publish_reports(_source(root, "old", attempt="old"), reports)
            pointer_before = (reports / clean_executor.REPORT_SET_MANIFEST).read_bytes()
            external = root / "external-history"
            external.mkdir()
            sentinel = external / "sentinel"
            sentinel.write_text("must survive\n", encoding="utf-8")
            historical = reports / clean_executor.REPORT_GENERATIONS_DIRECTORY / ("f" * 64)
            historical.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "unsafe published quality generation"):
                _publish_reports(_source(root, "candidate", attempt="candidate"), reports)

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
