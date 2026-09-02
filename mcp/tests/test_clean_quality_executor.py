from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from unittest import mock

from agents_remember.certification.repository_profiles.execution import (
    AdmittedRepositoryProfileExecution,
)
from agents_remember.certification.repository_profiles.models import ProfileMode
from agents_remember.errors import CertificationExecutorPrerequisiteError
from agents_remember.worktrees.modules.quality import clean_executor as clean_quality_executor
from agents_remember.worktrees.modules.quality.clean_executor import (
    CleanQualityRequest,
    run_clean_quality,
)
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    agents_remember_profile_execution,
    install_agents_remember_profile,
)

CANDIDATE_TREE = "c" * 40


def quality_request(
    repository_root: Path,
    worktree_group: Path,
    mode: str,
    diff_base: str = "",
    *,
    memory_cap_bytes: int | None = None,
) -> CleanQualityRequest:
    return CleanQualityRequest(
        code_worktree=repository_root,
        worktree_group=worktree_group,
        repository_id="agents-remember",
        profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
        mode=cast(ProfileMode, mode),
        diff_base=diff_base,
        memory_cap_bytes=memory_cap_bytes,
    )


def publish_reports(
    source: Path,
    destination: Path,
    *,
    candidate_tree: str = CANDIDATE_TREE,
    profile_execution: AdmittedRepositoryProfileExecution | None = None,
):
    return clean_quality_executor._publish_reports(
        source,
        destination,
        candidate_tree=candidate_tree,
        profile_execution=(
            profile_execution or agents_remember_profile_execution(candidate_tree=candidate_tree)
        ),
    )


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def repository(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "dagger.json").write_text('{"engineVersion":"v0.21.8"}\n', encoding="utf-8")
    install_agents_remember_profile(repo)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    return repo


class CleanQualityExecutorTests(unittest.TestCase):
    def _assert_exact_pipeline_publication(
        self,
        *,
        group: Path,
        calls: list[list[str]],
        result,
    ) -> None:
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.evidence)
        sandbox = group / "reports/test-sandbox"
        source = sandbox / "source"
        self.assertEqual((source / "tracked.txt").read_text(encoding="utf-8"), "candidate\n")
        self.assertEqual((source / "created.txt").read_text(encoding="utf-8"), "created\n")
        self.assertEqual(git(source, "diff", "--cached", "--name-only"), "created.txt\ntracked.txt")
        manifest = json.loads((sandbox / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["profile"]["configuredReference"], "mcp/certification-profile-v1.json"
        )
        self.assertEqual(manifest["executorAdapter"]["functionName"], "quality")
        self.assertEqual(
            (len(manifest["profile"]["profileDigest"]), len(manifest["bundleSha256"])),
            (64, 64),
        )
        bundle = sandbox / "candidate.bundle"
        self.assertTrue(bundle.is_file())
        self.assertEqual(len(calls), 1)
        self.assertIn(f"--source={source.as_posix()}", calls[0])
        self.assertIn(f"--repository-bundle={bundle.as_posix()}", calls[0])
        self.assertIn(
            f"--execution-manifest={(sandbox / 'manifest.json').as_posix()}",
            calls[0],
        )
        self.assertEqual(len(manifest["publishedArtifacts"]), 14)
        self.assertNotIn("--attempt-nonce", " ".join(calls[0]))
        self.assertNotIn("--candidate-head", " ".join(calls[0]))
        self.assertNotIn("docker", " ".join(item for call in calls for item in call).lower())
        self.assertEqual(
            clean_quality_executor.published_report_path(
                group / "reports", "coverage.data"
            ).read_bytes(),
            b"\x00coverage",
        )
        for proof_name in ("python-runtime.json", "python-venv-runtime.json"):
            proof = clean_quality_executor.published_report_path(group / "reports", proof_name)
            self.assertEqual(
                json.loads(proof.read_text(encoding="utf-8"))["schema"],
                "ar-python-runtime-proof/v1",
            )
        e2e_summary = clean_quality_executor.published_report_path(
            group / "reports", "ambient-role-chat-e2e/summary.json"
        )
        self.assertEqual(
            json.loads(e2e_summary.read_text(encoding="utf-8"))["status"],
            "passed",
        )

    def test_exact_staged_candidate_is_passed_to_one_pinned_dagger_pipeline(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repo = repository(root)
            (repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
            (repo / "created.txt").write_text("created\n", encoding="utf-8")
            git(repo, "add", "-A")
            group = root / "enclosure"
            calls: list[list[str]] = []

            def runner(
                command: list[str], cwd: Path, env: Mapping[str, str]
            ) -> subprocess.CompletedProcess[str]:
                del env
                calls.append(command)
                self.assertEqual(cwd, group / "reports/test-sandbox/source")
                export = Path(
                    next(item.split("=", 1)[1] for item in command if item.startswith("--path="))
                )
                export.mkdir(parents=True)
                (export / "clean-quality-results.json").write_text(
                    '{"status":"passed","exitCode":0}\n', encoding="utf-8"
                )
                (export / "coverage.data").write_bytes(b"\x00coverage")
                (export / "python-runtime.json").write_text(
                    '{"schema":"ar-python-runtime-proof/v1"}\n', encoding="utf-8"
                )
                (export / "python-venv-runtime.json").write_text(
                    '{"schema":"ar-python-runtime-proof/v1"}\n', encoding="utf-8"
                )
                e2e = export / "ambient-role-chat-e2e"
                e2e.mkdir()
                (e2e / "summary.json").write_text(
                    '{"schema":"ar-ambient-role-chat-e2e-summary/v1","status":"passed"}\n',
                    encoding="utf-8",
                )
                for run in (1, 2):
                    (e2e / f"run-{run}.json").write_text(
                        json.dumps(
                            {
                                "schema": "ar-ambient-role-chat-e2e-run/v1",
                                "status": "passed",
                                "run": run,
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(command, 0, stdout="exported\n", stderr="")

            result = run_clean_quality(
                quality_request(
                    repo,
                    group,
                    "targeted",
                    git(repo, "rev-parse", "HEAD"),
                    memory_cap_bytes=1024,
                ),
                runner=runner,
                executor_resolver=lambda _env: "/usr/local/bin/dagger",
            )

            sandbox = group / "reports/test-sandbox"
            self._assert_exact_pipeline_publication(group=group, calls=calls, result=result)

            (sandbox / "obsolete").write_text("old", encoding="utf-8")
            run_clean_quality(
                quality_request(repo, group, "full"),
                runner=runner,
                executor_resolver=lambda _env: "/usr/local/bin/dagger",
            )
            self.assertFalse((sandbox / "obsolete").exists())

    def test_public_runner_refuses_invalid_mode_and_windows_interop_root(self) -> None:
        request = quality_request(Path("/repo"), Path("/enclosure"), "quick")
        with self.assertRaisesRegex(ValueError, "unknown clean quality mode"):
            run_clean_quality(request)

        request = quality_request(Path("/mnt/c/repo"), Path("/enclosure"), "full")
        with self.assertRaisesRegex(RuntimeError, "Windows-mounted WSL path"):
            run_clean_quality(request)

    def test_export_failure_is_returned_without_running_status_or_publishing(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repo = repository(root)
            calls: list[list[str]] = []

            def runner(
                command: list[str], cwd: Path, env: Mapping[str, str]
            ) -> subprocess.CompletedProcess[str]:
                del cwd, env
                calls.append(command)
                return subprocess.CompletedProcess(command, 8, stdout="", stderr="engine red")

            result = run_clean_quality(
                quality_request(repo, root / "group", "full"),
                runner=runner,
                executor_resolver=lambda _env: "dagger",
            )

            self.assertEqual(result.returncode, 8)
            self.assertIsNone(result.evidence)
            self.assertEqual(len(calls), 1)
            progress = json.loads(
                (root / "group/reports/quality-progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(progress["status"], "failed")

    def test_unavailable_admitted_executor_is_a_typed_gate_owned_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repo = repository(root)
            group = root / "group"

            def unavailable(_env: Mapping[str, str]) -> str:
                raise RuntimeError("native executable is unavailable")

            with self.assertRaises(CertificationExecutorPrerequisiteError) as caught:
                run_clean_quality(
                    quality_request(repo, group, "full"),
                    executor_resolver=unavailable,
                )

            self.assertEqual(
                caught.exception.status,
                "certification-executor-prerequisite-failed",
            )
            self.assertEqual(len(caught.exception.findings), 1)
            finding = caught.exception.findings[0]
            self.assertEqual(finding["code"], "executor-prerequisite-unavailable")
            self.assertEqual(finding["gate"], 1)
            self.assertEqual(finding["affectedGates"], (1, 2, 3, 4))
            self.assertEqual(finding["correctiveOwners"], ("repository-quality",))
            self.assertFalse((group / "reports/dagger-progress.log").exists())

    def test_admitted_executor_start_failure_is_typed_and_gate_owned(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repo = repository(root)
            group = root / "group"

            def cannot_start(
                command: list[str], cwd: Path, env: Mapping[str, str]
            ) -> subprocess.CompletedProcess[str]:
                del command, cwd, env
                raise OSError("executor permission denied")

            with self.assertRaises(CertificationExecutorPrerequisiteError) as caught:
                run_clean_quality(
                    quality_request(repo, group, "targeted"),
                    runner=cannot_start,
                    executor_resolver=lambda _env: "dagger",
                )

            finding = caught.exception.findings[0]
            self.assertEqual(finding["code"], "executor-prerequisite-unavailable")
            self.assertEqual(finding["gate"], 1)
            progress = json.loads(
                (group / "reports/quality-progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(progress["status"], "failed")
            self.assertEqual(progress["detail"], "admitted repository executor could not start")

    def test_invalid_exported_exit_code_refuses_instead_of_guessing(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repo = repository(root)

            def runner(
                command: list[str], cwd: Path, env: Mapping[str, str]
            ) -> subprocess.CompletedProcess[str]:
                del cwd, env
                export = Path(
                    next(item.split("=", 1)[1] for item in command if item.startswith("--path="))
                )
                export.mkdir(parents=True)
                (export / "clean-quality-results.json").write_text(
                    '{"status":"passed","exitCode":"zero"}\n', encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, stdout="exported\n", stderr="")

            with self.assertRaisesRegex(RuntimeError, "invalid pipeline exit code"):
                run_clean_quality(
                    quality_request(repo, root / "group", "full"),
                    runner=runner,
                    executor_resolver=lambda _env: "dagger",
                )

    def test_exported_failure_is_the_authoritative_pipeline_exit(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repo = repository(root)

            def runner(
                command: list[str], cwd: Path, env: Mapping[str, str]
            ) -> subprocess.CompletedProcess[str]:
                del cwd, env
                export = Path(
                    next(item.split("=", 1)[1] for item in command if item.startswith("--path="))
                )
                export.mkdir(parents=True)
                (export / "clean-quality-results.json").write_text(
                    '{"status":"failed","exitCode":8}\n', encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, stdout="exported\n", stderr="")

            result = run_clean_quality(
                quality_request(repo, root / "group", "full", "base"),
                runner=runner,
                executor_resolver=lambda _env: "dagger",
            )

            self.assertEqual(result.returncode, 8)
            progress = json.loads(
                (root / "group/reports/quality-progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(progress["status"], "failed")
            self.assertEqual(progress["detail"], "repository certification adapter failed")

    def test_exported_result_validation_rejects_missing_and_contradictory_authority(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            export = Path(tmp)
            decoder = agents_remember_profile_execution(candidate_tree=CANDIDATE_TREE).decoder
            with self.assertRaisesRegex(RuntimeError, "no safe authoritative result"):
                clean_quality_executor._exported_pipeline_exit(
                    export, decoder, {"clean-quality-results.json"}
                )
            report = export / "clean-quality-results.json"
            report.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no valid authoritative result"):
                clean_quality_executor._exported_pipeline_exit(
                    export, decoder, {"clean-quality-results.json"}
                )
            report.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "must be a JSON object"):
                clean_quality_executor._exported_pipeline_exit(
                    export, decoder, {"clean-quality-results.json"}
                )
            report.write_text('{"status":"passed","exitCode":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid pipeline exit code"):
                clean_quality_executor._exported_pipeline_exit(
                    export, decoder, {"clean-quality-results.json"}
                )
            report.write_text('{"status":"passed","exitCode":2}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "contradictory result"):
                clean_quality_executor._exported_pipeline_exit(
                    export, decoder, {"clean-quality-results.json"}
                )
            report.write_text('{"status":"passed","exitCode":0}\n', encoding="utf-8")
            self.assertEqual(
                clean_quality_executor._exported_pipeline_exit(
                    export,
                    decoder,
                    {"clean-quality-results.json"},
                ),
                0,
            )

    def test_invalid_or_unowned_exports_never_replace_durable_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repo = repository(root)
            reports = root / "group/reports"
            reports.mkdir(parents=True)
            durable = reports / "clean-quality-results.json"
            durable.write_text('{"status":"passed","exitCode":0}\n', encoding="utf-8")
            coverage = reports / "coverage.json"
            coverage.write_text("prior", encoding="utf-8")

            def contradictory(
                command: list[str], cwd: Path, env: Mapping[str, str]
            ) -> subprocess.CompletedProcess[str]:
                del cwd, env
                export = Path(
                    next(item.split("=", 1)[1] for item in command if item.startswith("--path="))
                )
                export.mkdir(parents=True)
                (export / "clean-quality-results.json").write_text(
                    '{"status":"passed","exitCode":2}\n', encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with self.assertRaisesRegex(RuntimeError, "contradictory result"):
                run_clean_quality(
                    quality_request(repo, root / "group", "full", "base"),
                    runner=contradictory,
                    executor_resolver=lambda _env: "dagger",
                )
            self.assertEqual(
                durable.read_text(encoding="utf-8"), '{"status":"passed","exitCode":0}\n'
            )

            self.assertEqual(coverage.read_text(encoding="utf-8"), "prior")

            export = root / "malicious"
            export.mkdir()
            (export / "clean-quality-results.json").write_text(
                '{"status":"passed","exitCode":0}\n', encoding="utf-8"
            )
            (export / "closeout-operation.json").write_text("forged", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unexpected report files"):
                publish_reports(export, reports)
            self.assertEqual(
                durable.read_text(encoding="utf-8"), '{"status":"passed","exitCode":0}\n'
            )

            (export / "closeout-operation.json").unlink()
            nested = export / "ambient-role-chat-e2e"
            nested.mkdir()
            (nested / "run-3.json").write_text("forged", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unexpected report files"):
                publish_reports(export, reports)
            self.assertEqual(
                durable.read_text(encoding="utf-8"), '{"status":"passed","exitCode":0}\n'
            )

    def test_nested_profile_artifact_directories_include_the_complete_parent_chain(self) -> None:
        self.assertEqual(
            clean_quality_executor._report_directories({"one/two/three/result.json"}),
            frozenset({"one", "one/two", "one/two/three"}),
        )

    def test_not_applicable_gate_cleans_stale_managed_projection_without_requiring_it(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "clean-quality-results.json").write_text(
                '{"status":"passed","exitCode":0}\n',
                encoding="utf-8",
            )
            reports = root / "reports"
            stale = reports / "ambient-role-chat-e2e/summary.json"
            stale.parent.mkdir(parents=True)
            stale.write_text('{"status":"stale"}\n', encoding="utf-8")
            execution = agents_remember_profile_execution(
                candidate_tree=CANDIDATE_TREE,
                purpose="local-precommit",
                mode="targeted",
            )

            manifest = publish_reports(
                source,
                reports,
                profile_execution=execution,
            )

            self.assertFalse(stale.exists())
            self.assertEqual(
                set(cast(dict[str, object], manifest["files"])),
                {"clean-quality-results.json"},
            )

    def test_report_generation_pointer_never_exposes_a_partial_copy(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            reports = root / "reports"
            old = root / "old"
            old.mkdir()
            (old / "clean-quality-results.json").write_text(
                '{"status":"passed","exitCode":0,"attempt":"old"}\n', encoding="utf-8"
            )
            publish_reports(old, reports)
            manifest_before = (reports / clean_quality_executor.REPORT_SET_MANIFEST).read_bytes()

            new = root / "new"
            new.mkdir()
            (new / "clean-quality-results.json").write_text(
                '{"status":"passed","exitCode":0,"attempt":"new"}\n', encoding="utf-8"
            )
            (new / "coverage.json").write_text('{"new":true}\n', encoding="utf-8")
            (new / "causal-failures.json").write_text(
                '{"schemaVersion":"python-causal-failures/v1"}\n',
                encoding="utf-8",
            )
            (new / "causal-failures.md").write_text(
                "# Causal failures\n",
                encoding="utf-8",
            )
            real_copy = clean_quality_executor.shutil.copyfile
            for fail_after in (1, 2):
                copies = 0

                def interrupted_copy(
                    source: Path,
                    target: Path,
                    fail_at: int = fail_after,
                ) -> Path:
                    nonlocal copies
                    copies += 1
                    if copies == fail_at:
                        raise OSError("injected publication failure")
                    return real_copy(source, target)

                with (
                    mock.patch.object(
                        clean_quality_executor.shutil,
                        "copyfile",
                        side_effect=interrupted_copy,
                    ),
                    self.assertRaisesRegex(OSError, "injected publication failure"),
                ):
                    publish_reports(new, reports)
                self.assertEqual(
                    (reports / clean_quality_executor.REPORT_SET_MANIFEST).read_bytes(),
                    manifest_before,
                )
                selected = clean_quality_executor.published_report_path(
                    reports, "clean-quality-results.json"
                )
                self.assertIn('"attempt":"old"', selected.read_text(encoding="utf-8"))

            with (
                mock.patch.object(
                    clean_quality_executor,
                    "_prune_report_generations",
                    side_effect=OSError("crash before pointer"),
                ),
                self.assertRaisesRegex(OSError, "crash before pointer"),
            ):
                publish_reports(new, reports)
            selected = clean_quality_executor.published_report_path(
                reports, "clean-quality-results.json"
            )
            self.assertIn('"attempt":"old"', selected.read_text(encoding="utf-8"))

    def test_competing_report_publisher_reuses_the_complete_generation(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "clean-quality-results.json").write_text(
                '{"status":"passed","exitCode":0}\n', encoding="utf-8"
            )

            def competing_rename(staged: Path, target: Path) -> Path:
                clean_quality_executor.shutil.copytree(staged, target)
                raise FileExistsError(target)

            with mock.patch.object(Path, "rename", autospec=True, side_effect=competing_rename):
                manifest = publish_reports(source, root / "reports")

            selected = clean_quality_executor.published_report_path(
                root / "reports", "clean-quality-results.json"
            )
            self.assertEqual(json.loads(selected.read_text(encoding="utf-8"))["exitCode"], 0)
            self.assertEqual(selected.parent.name, manifest["generation"])

    def test_report_manifest_verification_and_generation_pruning_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            reports = root / "reports"
            with self.assertRaisesRegex(RuntimeError, "no complete Dagger report"):
                clean_quality_executor.published_report_path(reports, "result.json")

            reports.mkdir()
            manifest_path = reports / clean_quality_executor.REPORT_SET_MANIFEST
            manifest_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": "2.0",
                        "generation": "invalid",
                        "candidateTree": CANDIDATE_TREE,
                        "files": {"result.json": {"sha256": "", "size": 0}},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "no complete Dagger report"):
                clean_quality_executor.published_report_path(reports, "result.json")

            files: dict[str, dict[str, object]] = {"result.json": {"sha256": "0" * 64, "size": 1}}
            profile_identity: dict[str, object] = {
                "profileDigest": "b" * 64,
                "profilePlanDigest": "c" * 64,
                "profileSelectionId": "fixture-full",
                "executorAdapterId": "fixture-dagger",
                "resultDecoder": {
                    "decoderId": "fixture-result",
                    "decoderKind": "json-exit-status",
                    "artifactPath": "result.json",
                    "statusField": "status",
                    "exitCodeField": "exitCode",
                    "passedValue": "passed",
                    "failedValue": "failed",
                    "artifactReferences": [],
                    "referenceActivations": [],
                    "consumingGates": [1, 2, 3, 4],
                },
            }
            dependencies = clean_quality_executor.quality_report_dependencies(
                CANDIDATE_TREE, files, None, profile_identity
            ).model_dump(mode="json")
            manifest_fields: dict[str, object] = {
                "candidateTree": CANDIDATE_TREE,
                **profile_identity,
                "files": files,
                "dependencies": dependencies,
            }
            generation = clean_quality_executor.quality_generation_digest(manifest_fields)
            manifest = {
                "schemaVersion": "3.0",
                "generation": generation,
                **manifest_fields,
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError,
                r"published Dagger report is incomplete: result\.json",
            ):
                clean_quality_executor.published_report_path(reports, "result.json")

            traversal_manifest = {
                **manifest,
                "files": {"../result.json": {"sha256": "0" * 64, "size": 1}},
            }
            manifest_path.write_text(json.dumps(traversal_manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no complete Dagger report"):
                clean_quality_executor.published_report_path(reports, "../result.json")

            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            generation_root = (
                reports / clean_quality_executor.REPORT_GENERATIONS_DIRECTORY / generation
            )
            generation_root.mkdir(parents=True)
            (generation_root / "result.json").write_bytes(b"x")
            with self.assertRaisesRegex(RuntimeError, "failed generation verification"):
                clean_quality_executor.published_report_path(reports, "result.json")
            with self.assertRaisesRegex(RuntimeError, "copy failed verification"):
                clean_quality_executor._validate_generation(
                    generation_root,
                    {"result.json": {"sha256": "0" * 64, "size": 1}},
                )

            generations = root / "generations"
            generations.mkdir()
            names = [f"{number:064x}" for number in range(1, 5)]
            for number, name in enumerate(names):
                path = generations / name
                path.mkdir()
                os.utime(path, ns=(number, number))
            clean_quality_executor._prune_report_generations(generations, names[-1])
            self.assertFalse((generations / names[0]).exists())
            self.assertEqual(
                {path.name for path in generations.iterdir()},
                {names[-1], names[-2]},
            )

    def test_report_publish_git_guard_and_streaming_progress_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "did not export"):
                publish_reports(root / "missing", root / "reports")
            empty = root / "empty"
            empty.mkdir()
            reports = root / "reports"
            reports.mkdir()
            (reports / "coverage.json").write_text("stale", encoding="utf-8")
            (reports / "pytest-events.jsonl").write_text("stale", encoding="utf-8")
            (reports / "dagger-progress.log").write_text("owned elsewhere", encoding="utf-8")
            (empty / "nested").mkdir()
            with self.assertRaisesRegex(RuntimeError, "unexpected report files"):
                publish_reports(empty, reports)
            self.assertTrue((reports / "coverage.json").exists())
            self.assertTrue((reports / "pytest-events.jsonl").exists())
            self.assertTrue((reports / "dagger-progress.log").exists())
            failed = subprocess.CompletedProcess(["git"], 2, stdout="", stderr="bad ref\n")
            with self.assertRaisesRegex(RuntimeError, "could not resolve base: bad ref"):
                clean_quality_executor._git_ok(failed, "resolve base")
            passed = subprocess.CompletedProcess(["git"], 0, stdout=" patch\n", stderr="")
            self.assertEqual(
                clean_quality_executor._git_ok(passed, "read patch", preserve_output=True),
                " patch\n",
            )

            class Process:
                stdout = io.BytesIO(b"first\n\x1b[31mred\x1b[0m\n\n")

                @staticmethod
                def wait() -> int:
                    return 3

            progress = root / "reports/dagger-progress.log"
            progress.parent.mkdir(parents=True, exist_ok=True)
            progress.write_text("prior\n", encoding="utf-8")
            with mock.patch.object(
                clean_quality_executor.subprocess, "Popen", return_value=Process()
            ):
                completed = clean_quality_executor._stream_dagger(
                    ["dagger"], root, {"PATH": "/usr/bin"}, progress_path=progress
                )
            self.assertEqual(completed.returncode, 3)
            self.assertEqual(completed.stdout, "first\n\x1b[31mred\x1b[0m\n\n")
            self.assertEqual(
                progress.read_text(encoding="utf-8"),
                "prior\nfirst\n\x1b[31mred\x1b[0m\n\n",
            )
            current = json.loads(
                (progress.parent / "quality-progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(current["detail"], "red")

            class LongProcess:
                stdout = io.BytesIO(b"0123456789\n" * 20)

                @staticmethod
                def wait() -> int:
                    return 0

            bounded = root / "reports/bounded.log"
            with (
                mock.patch.object(
                    clean_quality_executor.subprocess, "Popen", return_value=LongProcess()
                ),
                mock.patch.object(clean_quality_executor, "DAGGER_PROGRESS_MAX_BYTES", 80),
                mock.patch.object(clean_quality_executor, "DAGGER_RESULT_MAX_BYTES", 50),
            ):
                completed = clean_quality_executor._stream_dagger(
                    ["dagger"], root, {"PATH": "/usr/bin"}, progress_path=bounded
                )
            self.assertLessEqual(len(completed.stdout.encode("utf-8")), 50)
            self.assertLessEqual(bounded.stat().st_size, 80)
            self.assertIn("older Dagger output truncated", bounded.read_text(encoding="utf-8"))
            self.assertEqual(
                clean_quality_executor._bounded_text_tail("abcdef", max_bytes=3), "def"
            )
            self.assertEqual(
                clean_quality_executor._utf8_tail_bytes(b"short", max_bytes=8), b"short"
            )

            class HugeLineProcess:
                stdout = io.BytesIO(("é" * 1000).encode("utf-8"))

                @staticmethod
                def wait() -> int:
                    return 0

            huge = root / "reports/huge-line.log"
            with (
                mock.patch.object(
                    clean_quality_executor.subprocess,
                    "Popen",
                    return_value=HugeLineProcess(),
                ),
                mock.patch.object(clean_quality_executor, "DAGGER_PROGRESS_MAX_BYTES", 80),
                mock.patch.object(clean_quality_executor, "DAGGER_RESULT_MAX_BYTES", 50),
            ):
                clean_quality_executor._stream_dagger(
                    ["dagger"], root, {"PATH": "/usr/bin"}, progress_path=huge
                )
            self.assertLessEqual(huge.stat().st_size, 80)
            huge.read_text(encoding="utf-8")

    def test_stream_reads_fixed_chunks_and_writes_at_most_two_progress_windows(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)

            class CountingStream:
                def __init__(self) -> None:
                    self.payload = bytearray(b"x" * 4096)
                    self.read_sizes: list[int] = []

                def read(self, size: int) -> bytes:
                    self.read_sizes.append(size)
                    chunk = bytes(self.payload[:size])
                    del self.payload[:size]
                    return chunk

            stream = CountingStream()

            class Process:
                stdout = stream

                @staticmethod
                def wait() -> int:
                    return 0

            progress = root / "reports/dagger-progress.log"
            real_live_write = clean_quality_executor._write_live_progress
            real_atomic_write = clean_quality_executor.atomic_write_bytes
            with (
                mock.patch.object(
                    clean_quality_executor.subprocess,
                    "Popen",
                    return_value=Process(),
                ),
                mock.patch.object(clean_quality_executor, "DAGGER_STREAM_CHUNK_BYTES", 32),
                mock.patch.object(clean_quality_executor, "DAGGER_PROGRESS_MAX_BYTES", 96),
                mock.patch.object(clean_quality_executor, "DAGGER_RESULT_MAX_BYTES", 64),
                mock.patch.object(
                    clean_quality_executor,
                    "_write_live_progress",
                    wraps=real_live_write,
                ) as live_write,
                mock.patch.object(
                    clean_quality_executor,
                    "atomic_write_bytes",
                    wraps=real_atomic_write,
                ) as atomic_write,
            ):
                clean_quality_executor._stream_dagger(
                    ["dagger"], root, {"PATH": "/usr/bin"}, progress_path=progress
                )

            self.assertTrue(stream.read_sizes)
            self.assertLessEqual(max(stream.read_sizes), 32)
            live_bytes = sum(len(call.args[1]) for call in live_write.call_args_list)
            final_bytes = sum(len(call.args[1]) for call in atomic_write.call_args_list)
            self.assertLessEqual(live_bytes, 96)
            self.assertLessEqual(final_bytes, 96)
            self.assertLessEqual(live_bytes + final_bytes, 192)
            self.assertLessEqual(progress.stat().st_size, 96)

    def test_stream_normalizes_text_test_doubles_and_blank_progress_chunks(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)

            class TextStream:
                chunks = iter((" \n", "text detail\n", ""))

                @classmethod
                def read(cls, _size: int) -> str:
                    return next(cls.chunks)

            class Process:
                stdout = TextStream()

                @staticmethod
                def wait() -> int:
                    return 0

            progress = root / "reports/dagger-progress.log"
            with mock.patch.object(
                clean_quality_executor.subprocess,
                "Popen",
                return_value=Process(),
            ):
                completed = clean_quality_executor._stream_dagger(
                    ["dagger"], root, {"PATH": "/usr/bin"}, progress_path=progress
                )

            self.assertEqual(completed.stdout, " \ntext detail\n")
            self.assertEqual(clean_quality_executor._chunk_detail(b"\n \n"), "")
            current = json.loads(
                (progress.parent / "quality-progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(current["detail"], "text detail")

    def test_profile_executor_resolution_uses_the_native_command_boundary(self) -> None:
        with mock.patch.object(
            clean_quality_executor, "native_command", return_value=["/usr/bin/dagger"]
        ) as native:
            self.assertEqual(
                clean_quality_executor._resolve_executor("dagger", {"PATH": "/usr/bin"}),
                "/usr/bin/dagger",
            )
        native.assert_called_once_with(["dagger"], {"PATH": "/usr/bin"})


if __name__ == "__main__":
    unittest.main()
