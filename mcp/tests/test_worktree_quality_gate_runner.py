"""Strict quality-runner command, environment, cap, and report behavior."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.execution import (
    admit_repository_profile_execution,
)
from agents_remember.errors import CertificationProfileError
from agents_remember.models.test_evidence import _certifying_evidence_from_verified_dagger
from agents_remember.worktrees.modules.quality import clean_executor
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.modules.quality.clean_executor import CleanQualityOutcome
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
)
from gate_certification_test_support import _checkout_with_profile, _gate_catalog, _git, _lane_for
from test_worktree_closeout_quality_gate import (
    _checkout_with_profile as generic_profile_checkout,
)
from test_worktree_closeout_quality_gate import _quality_target


def _successful_quality_outcome(request, *, stdout: str = "passed\n") -> CleanQualityOutcome:
    candidate_tree = subprocess.run(
        ["git", "write-tree"],
        cwd=request.code_worktree,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    admitted, lane, _candidate = _lane_for(
        request.code_worktree,
        request.mode,
        diff_base=request.diff_base,
    )
    with tempfile.TemporaryDirectory() as temporary:
        exported = Path(temporary)
        (exported / "clean-quality-results.json").write_text(
            json.dumps({"status": "passed", "exitCode": 0, "gates": _gate_catalog(lane, exported)})
            + "\n",
            encoding="utf-8",
        )
        clean_executor._publish_reports(  # pyright: ignore[reportPrivateUsage]
            exported,
            request.worktree_group / "reports",
            candidate_tree=candidate_tree,
            profile_execution=admit_repository_profile_execution(
                admitted,
                purpose="closeout",
                mode=request.mode,
                candidate_identity=CandidateIdentity(kind="git-tree", value=candidate_tree),
                source_selection=lane.repositoryPlan.sourceSelection,
            ),
        )
    manifest = load_published_quality_manifest(request.worktree_group / "reports")
    evidence = _certifying_evidence_from_verified_dagger(
        candidate_tree=candidate_tree,
        result_sha256=manifest.require_file(manifest.result_decoder.artifactPath).sha256,
    )
    return CleanQualityOutcome(
        subprocess.CompletedProcess(["dagger"], 0, stdout=stdout),
        evidence,
        manifest,
    )


def _failed_quality_outcome(returncode: int, stdout: str = "") -> CleanQualityOutcome:
    return CleanQualityOutcome(
        subprocess.CompletedProcess(["dagger"], returncode, stdout=stdout),
        None,
        None,
    )


class CodeQualityGateTests(unittest.TestCase):
    def test_preview_requires_the_explicit_repository_profile_for_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            target = _quality_target(worktree)

            preview = code_quality_gate.code_quality_gate_preview(
                target,
                code_would_commit=True,
            )

            self.assertTrue(preview["required"])
            self.assertEqual(preview["status"], code_quality_gate.GATE_ENFORCED)
            self.assertEqual(
                preview["command"],
                "dagger --progress=plain call quality "
                "'--source=<exact-staged-candidate>' "
                "'--repository-bundle=<exact-git-ancestry-bundle>' "
                "'--execution-manifest=<exact-admission-manifest>' "
                "reports export '--path=<profile-report-export>'",
            )
            self.assertEqual(preview["mode"], code_quality_gate.GATE_TARGETED)
            self.assertEqual(preview["executorAdapterId"], "certifying-dagger")
            self.assertIn("profile", str(preview["reason"]))
            self.assertTrue(
                code_quality_gate.requires_strict_code_quality(
                    target,
                    code_would_commit=True,
                )
            )

    def test_preview_reports_no_code_commit_when_nothing_would_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            target = _quality_target(worktree)

            preview = code_quality_gate.code_quality_gate_preview(
                target,
                code_would_commit=False,
            )

            self.assertFalse(preview["required"])
            self.assertEqual(preview["status"], code_quality_gate.GATE_NO_CODE_COMMIT)
            self.assertEqual(preview["command"], "")
            self.assertFalse(
                code_quality_gate.requires_strict_code_quality(
                    target,
                    code_would_commit=False,
                )
            )

    def test_preview_refuses_a_missing_profile_instead_of_skipping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            consuming_repo = Path(tmp)
            target = code_quality_gate.QualityGateTarget(
                code_worktree=consuming_repo,
                worktree_group=consuming_repo / "enclosure",
                repository_id="consumer-repo",
                profile_reference=None,
            )

            with self.assertRaises(CertificationProfileError) as caught:
                code_quality_gate.code_quality_gate_preview(
                    target,
                    code_would_commit=True,
                )
            self.assertEqual(caught.exception.status, "certification-profile-invalid")
            self.assertIn("authority is missing", str(caught.exception))
            with self.assertRaises(CertificationProfileError):
                code_quality_gate.requires_strict_code_quality(
                    target,
                    code_would_commit=True,
                )

    def test_profile_authority_is_repository_generic_without_a_name_special_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = generic_profile_checkout(
                Path(tmp),
                repository_id="consumer-repo",
            )
            target = _quality_target(candidate, repository_id="consumer-repo")

            self.assertTrue(
                code_quality_gate.requires_strict_code_quality(
                    target,
                    code_would_commit=True,
                )
            )
            preview = code_quality_gate.code_quality_gate_preview(
                target,
                code_would_commit=True,
            )
            self.assertEqual(preview["status"], code_quality_gate.GATE_ENFORCED)

    def test_gate_refuses_to_run_when_the_profile_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            target = code_quality_gate.QualityGateTarget(
                code_worktree=worktree,
                worktree_group=worktree / "enclosure",
                repository_id="agents-remember",
                profile_reference=None,
            )

            with (
                mock.patch.object(code_quality_gate, "run_clean_quality") as execute,
                self.assertRaises(CertificationProfileError),
            ):
                code_quality_gate.run_strict_code_quality_gate(
                    target,
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )
            execute.assert_not_called()

    def test_host_quality_execution_refuses_before_running_a_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            with self.assertRaisesRegex(RuntimeError, "host quality execution is forbidden"):
                code_quality_gate.run_local_quality_diagnostic(_quality_target(worktree))

    def test_dagger_executor_uses_the_same_staged_candidate_and_never_runs_host_rails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = _checkout_with_profile(root / "code")
            target = _quality_target(worktree, root / "enclosure")
            with mock.patch.object(
                code_quality_gate,
                "run_clean_quality",
                side_effect=lambda request: _successful_quality_outcome(
                    request,
                    stdout="dagger passed\n",
                ),
            ) as clean:
                result = code_quality_gate.run_strict_code_quality_gate(
                    target,
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                    plan=code_quality_gate.QualityGatePlan(
                        mode=code_quality_gate.GATE_FULL,
                        memory_cap_bytes=2_147_483_648,
                    ),
                )

            request = clean.call_args.args[0]
            self.assertEqual(request.code_worktree, worktree)
            self.assertEqual(request.worktree_group, root / "enclosure")
            self.assertEqual(request.mode, code_quality_gate.GATE_FULL)
            self.assertEqual(request.diff_base, _git(worktree, "rev-parse", "HEAD"))
            self.assertEqual(request.memory_cap_bytes, 2_147_483_648)
            self.assertEqual(request.repository_id, "agents-remember")
            self.assertEqual(result["executor"], "dagger")
            self.assertIn("dagger --progress=plain call quality", str(result["command"]))
            self.assertEqual(result["executorAdapterId"], "certifying-dagger")
            memory_cap = result["memoryCap"]
            assert isinstance(memory_cap, dict)
            self.assertEqual(memory_cap["mechanism"], "container-wrapper")
            report = Path(str(result["reportPath"])).read_text(encoding="utf-8")
            self.assertIn("Memory-cap policy: `dagger-inner-wrapper`", report)

    def test_gate_replaces_one_test_report_instead_of_accumulating_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = _checkout_with_profile(root / "code")
            worktree_group = root / "enclosure"
            report = worktree_group / "reports" / "test-results.md"
            report.parent.mkdir(parents=True)
            report.write_text("obsolete run\n", encoding="utf-8")

            outputs = iter(("first completed run\n", "second completed run\n"))
            with mock.patch.object(
                code_quality_gate,
                "run_clean_quality",
                side_effect=lambda request: _successful_quality_outcome(
                    request,
                    stdout=next(outputs),
                ),
            ):
                code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree, worktree_group),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )
                code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree, worktree_group),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )

            report_text = report.read_text(encoding="utf-8")
            self.assertNotIn("obsolete run", report_text)
            self.assertNotIn("first completed run", report_text)
            self.assertIn("second completed run", report_text)
            self.assertEqual(
                sorted(path.name for path in report.parent.glob("*.md")),
                ["test-results.md"],
            )

    def test_interrupted_gate_keeps_the_previous_completed_test_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = _checkout_with_profile(root / "code")
            worktree_group = root / "enclosure"
            report = worktree_group / "reports" / "test-results.md"
            report.parent.mkdir(parents=True)
            report.write_text("previous completed run\n", encoding="utf-8")

            with (
                mock.patch.object(
                    code_quality_gate, "run_clean_quality", side_effect=KeyboardInterrupt
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree, worktree_group),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )

            self.assertEqual(report.read_text(encoding="utf-8"), "previous completed run\n")

    def test_gate_measures_the_leaf_diff_not_the_whole_branch(self) -> None:
        """The leaf's base commit reaches the immutable execution manifest.

        Without it the repository rail resolves its base to origin/HEAD or main, and the
        100% per-diff coverage floor then measures every change on the integration
        branch instead of this leaf's own diff. That is unpassable for any leaf, so
        the gate would block every closeout rather than enforce anything.
        """
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            with mock.patch.object(
                code_quality_gate,
                "run_clean_quality",
                side_effect=_successful_quality_outcome,
            ) as clean:
                result = code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )

            self.assertEqual(clean.call_args.args[0].diff_base, _git(worktree, "rev-parse", "HEAD"))
            self.assertEqual(result["diffBase"], _git(worktree, "rev-parse", "HEAD"))
            self.assertIn("--execution-manifest=", str(result["command"]))
            self.assertNotIn("--diff-base", str(result["command"]))
            report = code_quality_gate.test_results_report_path(
                _quality_target(worktree).worktree_group
            )
            self.assertIn(f"Diff base: `{_git(worktree, 'rev-parse', 'HEAD')}`", report.read_text())

    def test_gate_preview_reports_the_diff_base_it_will_use(self) -> None:
        """The preview names the exact command, so a reader can rerun what will run."""
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            preview = code_quality_gate.code_quality_gate_preview(
                _quality_target(worktree),
                code_would_commit=True,
                diff_base=_git(worktree, "rev-parse", "HEAD"),
            )
            self.assertEqual(preview["diffBase"], _git(worktree, "rev-parse", "HEAD"))
            self.assertIn("--execution-manifest=", str(preview["command"]))
            self.assertNotIn("--diff-base", str(preview["command"]))

    def test_gate_command_refuses_unknown_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            with self.assertRaisesRegex(ValueError, "unknown quality gate mode"):
                code_quality_gate._gate_command(
                    _quality_target(worktree),
                    "",
                    mode=cast(Any, "bogus"),
                )

    def test_command_planning_uses_only_the_declared_profile_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            target = _quality_target(worktree)
            command, invocation = code_quality_gate._gate_command_parts(
                target,
                code_quality_gate.QualityGatePlan(),
                "",
                "closeout-staged",
            )
            self.assertEqual(
                command[0:4],
                ["dagger", "--progress=plain", "call", "quality"],
            )
            self.assertEqual(
                command[-3:],
                ["reports", "export", "--path=<profile-report-export>"],
            )
            self.assertEqual(invocation, "closeout-staged")
            policy = code_quality_gate._memory_policy_payload()
            memory_policy = policy["memoryPolicy"]
            assert isinstance(memory_policy, dict)
            self.assertEqual(memory_policy["mode"], "container-host-managed")

    def test_dagger_preview_is_symbolic_and_does_not_require_host_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            preview = code_quality_gate.code_quality_gate_preview(
                _quality_target(worktree),
                code_would_commit=True,
                diff_base=_git(worktree, "rev-parse", "HEAD"),
                plan=code_quality_gate.QualityGatePlan(
                    mode=code_quality_gate.GATE_FULL,
                    memory_cap_bytes=4096,
                ),
            )
        self.assertIn("dagger --progress=plain call quality", str(preview["command"]))
        self.assertIn("--repository-bundle", str(preview["command"]))
        memory_cap = preview["memoryCap"]
        assert isinstance(memory_cap, dict)
        self.assertEqual(memory_cap["capBytes"], 4096)

    def test_full_gate_command_is_pinned_dagger_without_an_explicit_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            command = code_quality_gate._gate_command(
                _quality_target(worktree),
                "",
                mode=code_quality_gate.GATE_FULL,
            )
            self.assertIn("dagger --progress=plain call quality", command)
            self.assertNotIn("--mode=", command)

    def test_full_gate_preview_names_the_memory_cap_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            preview = code_quality_gate.code_quality_gate_preview(
                _quality_target(worktree),
                code_would_commit=True,
                diff_base=_git(worktree, "rev-parse", "HEAD"),
                plan=code_quality_gate.QualityGatePlan(
                    mode=code_quality_gate.GATE_FULL,
                    memory_cap_bytes=2147483648,
                ),
            )
            self.assertEqual(preview["mode"], code_quality_gate.GATE_FULL)
            self.assertIn("--memory-cap-bytes=2147483648", str(preview["command"]))
            memory_cap = preview["memoryCap"]
            assert isinstance(memory_cap, dict)
            self.assertEqual(memory_cap["capBytes"], 2147483648)
            self.assertEqual(memory_cap["policy"], "dagger-inner-wrapper")
            policy = preview["memoryPolicy"]
            assert isinstance(policy, dict)
            self.assertEqual(policy["mode"], "explicit-cap")
            self.assertEqual(policy["processPolicy"], "profile-adapter-owned")
            self.assertEqual(policy["swap"], "container-host-managed")

    def test_full_gate_preview_without_a_cap_is_container_host_managed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))

            preview = code_quality_gate.code_quality_gate_preview(
                _quality_target(worktree),
                code_would_commit=True,
                plan=code_quality_gate.QualityGatePlan(mode=code_quality_gate.GATE_FULL),
            )

            self.assertNotIn("memoryCap", preview)
            policy = preview["memoryPolicy"]
            assert isinstance(policy, dict)
            self.assertEqual(policy["mode"], "container-host-managed")
            self.assertEqual(policy["processPolicy"], "profile-adapter-owned")
            self.assertEqual(policy["swap"], "container-host-managed")

    def test_full_gate_without_a_cap_uses_container_host_memory_and_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            with mock.patch.object(
                code_quality_gate,
                "run_clean_quality",
                side_effect=_successful_quality_outcome,
            ) as clean:
                result = code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree),
                    plan=code_quality_gate.QualityGatePlan(mode=code_quality_gate.GATE_FULL),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )

            request = clean.call_args.args[0]
            self.assertEqual(request.mode, code_quality_gate.GATE_FULL)
            self.assertIsNone(request.memory_cap_bytes)
            self.assertNotIn("memoryCap", result)
            policy = result["memoryPolicy"]
            assert isinstance(policy, dict)
            self.assertEqual(policy["mode"], "container-host-managed")
            report = worktree / "enclosure" / "reports" / "test-results.md"
            report_text = report.read_text(encoding="utf-8")
            self.assertIn("- Memory policy: `container-host-managed`", report_text)
            self.assertIn("- Swap policy: `container-host-managed`", report_text)

    def test_gate_run_refuses_unknown_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))

            with (
                self.assertRaisesRegex(ValueError, "unknown quality gate mode"),
            ):
                code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree),
                    plan=code_quality_gate.QualityGatePlan(mode=cast(Any, "bogus")),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )

    def test_full_gate_run_uses_the_planned_cap_mechanism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))
            with mock.patch.object(
                code_quality_gate,
                "run_clean_quality",
                side_effect=_successful_quality_outcome,
            ) as clean:
                result = code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                    plan=code_quality_gate.QualityGatePlan(
                        mode=code_quality_gate.GATE_FULL,
                        memory_cap_bytes=1024,
                    ),
                )

            self.assertTrue(result["passed"])
            request = clean.call_args.args[0]
            self.assertEqual(request.memory_cap_bytes, 1024)
            self.assertEqual(result["mode"], code_quality_gate.GATE_FULL)
            memory_cap = result["memoryCap"]
            assert isinstance(memory_cap, dict)
            self.assertEqual(memory_cap["mechanism"], "container-wrapper")

    def test_full_gate_kill_names_the_policy_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))

            with (
                mock.patch.object(
                    code_quality_gate,
                    "run_clean_quality",
                    return_value=_failed_quality_outcome(137),
                ),
                self.assertRaisesRegex(RuntimeError, "killed by the memory cap") as caught,
            ):
                code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree),
                    plan=code_quality_gate.QualityGatePlan(
                        mode=code_quality_gate.GATE_FULL,
                        memory_cap_bytes=1024,
                    ),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )

            self.assertIn("dagger-inner-wrapper", str(caught.exception))

    def test_gate_failure_names_the_cap_when_the_scope_is_sigkilled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))

            with (
                mock.patch.object(
                    code_quality_gate,
                    "run_clean_quality",
                    return_value=_failed_quality_outcome(-9),
                ),
                self.assertRaisesRegex(RuntimeError, "killed by the memory cap") as caught,
            ):
                code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree),
                    plan=code_quality_gate.QualityGatePlan(
                        mode=code_quality_gate.GATE_FULL,
                        memory_cap_bytes=1024,
                    ),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )

            self.assertIn("dagger-inner-wrapper", str(caught.exception))

    def test_gate_failure_includes_bounded_profile_adapter_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = _checkout_with_profile(Path(tmp))

            with (
                mock.patch.object(
                    code_quality_gate,
                    "run_clean_quality",
                    return_value=_failed_quality_outcome(
                        1,
                        "\n".join(f"line-{index}" for index in range(50)),
                    ),
                ),
                self.assertRaisesRegex(
                    RuntimeError, "strict code-quality gate failed before code commit"
                ) as caught,
            ):
                code_quality_gate.run_strict_code_quality_gate(
                    _quality_target(worktree),
                    diff_base=_git(worktree, "rev-parse", "HEAD"),
                )

            self.assertNotIn("line-0", str(caught.exception))
            self.assertIn("line-49", str(caught.exception))
            report = worktree / "enclosure" / "reports" / "test-results.md"
            self.assertIn(report.as_posix(), str(caught.exception))
            report_text = report.read_text(encoding="utf-8")
            self.assertIn("- Status: **failed**", report_text)
            self.assertIn("    line-0", report_text)
            self.assertIn("    line-49", report_text)
