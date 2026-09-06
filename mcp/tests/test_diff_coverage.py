"""Changed-lines coverage bites using real throwaway git repositories."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from _quality_admission import QUALITY_TEST_ADMISSION
from agents_remember_test_support.code_quality import check, diff_coverage


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=gate@agents-remember.invalid",
            "-c",
            "user.name=Gate Tests",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return completed.stdout


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def coverage_report(
    root: Path,
    files: dict[str, dict[str, object]],
    *,
    branch: bool = True,
) -> Path:
    """A Coverage.py JSON report shaped exactly like the wrapper's."""
    payload = {
        "meta": {"format": 3, "version": "7.14.3", "branch_coverage": branch},
        "files": {
            path: {
                "executed_lines": data.get("executed_lines", []),
                "missing_lines": data.get("missing_lines", []),
                "executed_branches": data.get("executed_branches", []),
                "missing_branches": data.get("missing_branches", []),
            }
            for path, data in files.items()
        },
    }
    destination = root / "coverage.json"
    destination.write_text(json.dumps(payload), encoding="utf-8")
    return destination


def seeded_repository(root: Path) -> str:
    """A repository with one committed module, and the commit it was committed in."""
    git(root, "init", "--quiet", "--initial-branch=main")
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/module.py", "def first() -> int:\n    return 1\n")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "seed")
    return git(root, "rev-parse", "HEAD").strip()


class BaseResolutionTests(unittest.TestCase):
    def test_explicit_base_wins_over_every_environment_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)

            resolution = diff_coverage.resolve_base(
                root,
                explicit_base=seed,
                environment={"AR_GATE_DIFF_BASE": "main", "GITHUB_BASE_REF": "main"},
            )

            self.assertEqual(resolution.revision, seed)
            self.assertEqual(resolution.origin, "--diff-base")

    def test_an_unknown_explicit_base_is_an_error_rather_than_a_silent_fallback(self) -> None:
        # Falling back would compare against something the caller did not ask for and
        # report a verdict about the wrong range of history.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeded_repository(root)

            with self.assertRaises(diff_coverage.DiffScopeError) as raised:
                diff_coverage.resolve_base(root, explicit_base="no-such-revision")

            self.assertIn("no-such-revision", str(raised.exception))

    def test_the_environment_variable_names_the_source_branch_git_cannot_infer(self) -> None:
        # A leaf branch cut from a series branch has no upstream and `main` is not its
        # source, so this is the only signal that reaches the right fork point.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            git(root, "checkout", "--quiet", "-b", "series")
            write(root, "pkg/module.py", "def first() -> int:\n    return 1\n\n\nX = 2\n")
            git(root, "commit", "--quiet", "-am", "series work")
            series_head = git(root, "rev-parse", "HEAD").strip()
            git(root, "checkout", "--quiet", "-b", "leaf")

            resolution = diff_coverage.resolve_base(
                root, environment={"AR_GATE_DIFF_BASE": "series"}
            )

            self.assertEqual(resolution.revision, series_head)
            self.assertIn("AR_GATE_DIFF_BASE", resolution.origin)
            self.assertNotEqual(resolution.revision, seed)

    def test_a_pull_request_base_is_read_from_the_actions_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            git(root, "checkout", "--quiet", "-b", "topic")
            write(root, "pkg/module.py", "def first() -> int:\n    return 2\n")
            git(root, "commit", "--quiet", "-am", "topic work")

            resolution = diff_coverage.resolve_base(root, environment={"GITHUB_BASE_REF": "main"})

            self.assertEqual(resolution.revision, seed)
            self.assertIn("pull request base", resolution.origin)

    def test_the_configured_upstream_is_used_when_nothing_more_explicit_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            git(root, "checkout", "--quiet", "-b", "topic")
            git(root, "branch", "--set-upstream-to=main", "topic")
            write(root, "pkg/module.py", "def first() -> int:\n    return 3\n")
            git(root, "commit", "--quiet", "-am", "topic work")

            resolution = diff_coverage.resolve_base(root, environment={})

            self.assertEqual(resolution.revision, seed)
            self.assertIn("upstream", resolution.origin)

    def test_the_default_branch_is_the_last_resort_before_the_empty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            git(root, "checkout", "--quiet", "-b", "topic")
            write(root, "pkg/module.py", "def first() -> int:\n    return 4\n")
            git(root, "commit", "--quiet", "-am", "topic work")

            resolution = diff_coverage.resolve_base(root, environment={})

            self.assertEqual(resolution.revision, seed)
            self.assertIn("default branch", resolution.origin)

    def test_a_first_commit_with_no_merge_base_compares_against_the_empty_tree(self) -> None:
        # A first commit is measured in full and never becomes an empty-success state.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "--quiet", "--initial-branch=orphan")
            write(root, "pkg/module.py", "value = 1\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "first")

            resolution = diff_coverage.resolve_base(root, environment={})

            self.assertEqual(resolution.revision, diff_coverage.EMPTY_TREE)
            self.assertIn("empty tree", resolution.origin)

            changed = diff_coverage.changed_python_lines(root, resolution.revision)

            self.assertEqual(changed, {"pkg/module.py": {1}})

    def test_a_candidate_with_no_shared_history_is_skipped_not_used(self) -> None:
        # `main` exists here, so the candidate resolves -- but it shares no commit with
        # this branch, so there is no merge base. Using its tip anyway would produce a
        # diff of two unrelated trees; the resolver moves on and ends at the empty tree.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeded_repository(root)
            git(root, "checkout", "--quiet", "--orphan", "unrelated")
            git(root, "rm", "--quiet", "-rf", ".")
            write(root, "pkg/other.py", "value = 1\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "unrelated")

            resolution = diff_coverage.resolve_base(root, environment={})

            self.assertEqual(resolution.revision, diff_coverage.EMPTY_TREE)

    def test_a_broken_git_invocation_raises_rather_than_reporting_no_changes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(diff_coverage.DiffScopeError),
        ):
            diff_coverage.run_git(Path(tmp), ["rev-parse", "HEAD"])

    def test_merge_base_returns_none_for_unrelated_histories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeded_repository(root)
            git(root, "checkout", "--quiet", "--orphan", "unrelated")
            write(root, "other.py", "value = 1\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "unrelated")

            self.assertIsNone(diff_coverage.merge_base(root, "main"))

    def test_the_three_git_wrappers_agree_on_which_failures_are_this_gate_s_error(self) -> None:
        # Every git-facing helper must convert an invalid project root to the same gate error.
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-root"

            for call in (
                lambda: diff_coverage.run_git(missing, ["ls-files"]),
                lambda: diff_coverage.revision_exists(missing, "HEAD"),
                lambda: diff_coverage.merge_base(missing, "main"),
            ):
                with self.assertRaises(diff_coverage.DiffScopeError):
                    call()

            wedged = subprocess.TimeoutExpired(cmd=["git"], timeout=300)
            with patch.object(diff_coverage.git_command, "run_git", side_effect=wedged):
                for call in (
                    lambda: diff_coverage.run_git(missing, ["ls-files"]),
                    lambda: diff_coverage.revision_exists(missing, "HEAD"),
                    lambda: diff_coverage.merge_base(missing, "main"),
                ):
                    with self.assertRaises(diff_coverage.DiffScopeError):
                        call()

    def test_a_git_that_ran_and_said_no_is_still_an_answer_not_an_error(self) -> None:
        # The conversion is for failures to *run* git. A revision that does not exist and a
        # pair of refs with no merge base are answers, and converting those would turn every
        # ordinary negative into a gate crash.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)

            self.assertTrue(diff_coverage.revision_exists(root, seed))
            self.assertFalse(diff_coverage.revision_exists(root, "no-such-revision"))
            self.assertEqual(diff_coverage.merge_base(root, "main"), seed)


class ChangedLineTests(unittest.TestCase):
    def test_added_modified_and_renamed_files_all_report_their_new_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/module.py", "def first() -> int:\n    return 99\n")
            write(root, "pkg/added.py", "A = 1\nB = 2\n")
            git(root, "add", "-A")
            git(root, "mv", "pkg/module.py", "pkg/renamed.py")

            changed = diff_coverage.changed_python_lines(root, seed)

            self.assertEqual(changed["pkg/added.py"], {1, 2})
            self.assertEqual(changed["pkg/renamed.py"], {2})
            self.assertNotIn("pkg/module.py", changed)

    def test_a_pure_deletion_contributes_no_changed_lines(self) -> None:
        # git writes `+start,0` for a hunk that only removes lines. Treating the count
        # as the default 1 would invent a changed line that does not exist and then
        # demand coverage for it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "pkg/__init__.py", "")
            write(root, "pkg/module.py", "A = 1\nB = 2\nC = 3\n")
            git(root, "init", "--quiet", "--initial-branch=main")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "seed")
            seed = git(root, "rev-parse", "HEAD").strip()
            write(root, "pkg/module.py", "A = 1\nC = 3\n")

            self.assertEqual(diff_coverage.changed_python_lines(root, seed), {})

    def test_a_deleted_file_is_not_a_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            (root / "pkg" / "module.py").unlink()

            self.assertEqual(diff_coverage.changed_python_lines(root, seed), {})

    def test_working_tree_edits_count_because_they_are_what_coverage_measured(self) -> None:
        # The suite imports the working tree, so the diff has to end at the working
        # tree. Comparing base..HEAD instead would score line numbers from a file the
        # coverage report never saw.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/module.py", "def first() -> int:\n    return 1\n\n\nUNSTAGED = 1\n")

            self.assertEqual(
                diff_coverage.changed_python_lines(root, seed), {"pkg/module.py": {3, 4, 5}}
            )

    def test_a_dev_null_post_image_drops_the_file_rather_than_keeping_the_previous_one(
        self,
    ) -> None:
        # `--diff-filter=ACMR` never produces this, because a deletion is filtered out
        # before it can write `+++ /dev/null`. The guard exists so that a caller who
        # widens the filter gets nothing attributed to the wrong file instead of the
        # deleted file's hunks landing on whatever was parsed before it.
        diff = "\n".join(
            [
                "diff --git a/pkg/gone.py b/pkg/gone.py",
                "--- a/pkg/gone.py",
                "+++ /dev/null",
                "@@ -1,3 +0,0 @@",
                "diff --git a/pkg/kept.py b/pkg/kept.py",
                "--- a/pkg/kept.py",
                "+++ b/pkg/kept.py",
                "@@ -0,0 +1,2 @@",
            ]
        )

        self.assertEqual(diff_coverage.parse_unified_diff(diff), {"pkg/kept.py": {1, 2}})

    def test_an_unparsable_hunk_header_is_dropped_rather_than_guessed_at(self) -> None:
        # Inventing a line number here would demand coverage for a line that may not
        # exist, so the header is skipped and the rest of the file still parses.
        diff = "\n".join(
            [
                "+++ b/pkg/module.py",
                "@@ this is not a hunk header @@",
                "@@ -0,0 +1 @@",
            ]
        )

        self.assertEqual(diff_coverage.parse_unified_diff(diff), {"pkg/module.py": {1}})

    def test_non_python_changes_are_not_collected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "README.md", "prose\n")

            self.assertEqual(diff_coverage.changed_python_lines(root, seed), {})


class MeasurementTests(unittest.TestCase):
    def measure(
        self,
        root: Path,
        base: str,
        files: dict[str, dict[str, object]],
    ) -> diff_coverage.DiffCoverage:
        report = coverage_report(root, files)
        resolution = diff_coverage.BaseResolution(revision=base, origin="test")
        return diff_coverage.measure(root, report, resolution)

    def test_a_covered_change_scores_statements_and_branches_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/module.py", "def first() -> int:\n    return 1\n\n\nX = 2\nY = 3\n")

            result = self.measure(
                root,
                seed,
                {
                    "pkg/module.py": {
                        "executed_lines": [5, 6],
                        "executed_branches": [[5, 6]],
                    }
                },
            )

            self.assertEqual(result.state, "measured")
            self.assertEqual((result.covered_units, result.total_units), (3, 3))
            self.assertEqual(result.percent, 100.0)

    def test_changed_path_case_matches_casefolded_coverage_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/module.py", "VALUE = 1\n")

            result = self.measure(
                root,
                seed,
                {"pkg/Module.py": {"executed_lines": [1]}},
            )

            self.assertEqual(result.state, "measured")
            self.assertEqual(result.unmeasured_files, ())
            self.assertEqual((result.covered_units, result.total_units), (1, 1))

    def test_an_uncovered_changed_line_is_named_not_only_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/module.py", "def first() -> int:\n    return 1\n\n\nX = 2\nY = 3\n")

            result = self.measure(
                root,
                seed,
                {"pkg/module.py": {"executed_lines": [5], "missing_lines": [6]}},
            )
            rendered = "\n".join(diff_coverage.render(result, 100.0))

            self.assertEqual(result.uncovered_lines, ("pkg/module.py:6",))
            self.assertIn("pkg/module.py:6", rendered)
            self.assertIn("uncovered line", rendered)

    def test_an_untaken_branch_on_a_changed_line_is_named_with_its_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(
                root,
                "pkg/module.py",
                "def first() -> int:\n    return 1\n\n\ndef second(flag: bool) -> int:\n"
                "    if flag:\n        return 2\n    return 3\n",
            )

            result = self.measure(
                root,
                seed,
                {
                    "pkg/module.py": {
                        "executed_lines": [5, 6, 7, 8],
                        "executed_branches": [[6, 7]],
                        "missing_branches": [[6, 8]],
                    }
                },
            )
            rendered = "\n".join(diff_coverage.render(result, 100.0))

            self.assertEqual(result.untaken_branches, ("pkg/module.py:6 -> 8",))
            self.assertIn("untaken branch", rendered)
            self.assertLess(result.percent, 100.0)

    def test_a_branch_that_leaves_the_function_is_reported_as_an_exit(self) -> None:
        # Coverage.py writes a function exit as a negative destination line; printing
        # "-6" as a line number would send the reader to a line that does not exist.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/module.py", "def first() -> int:\n    return 1\n\n\nX = 2\n")

            result = self.measure(
                root,
                seed,
                {"pkg/module.py": {"executed_lines": [5], "missing_branches": [[5, -5]]}},
            )

            self.assertEqual(result.untaken_branches, ("pkg/module.py:5 -> exit",))

    def test_a_changed_line_carrying_no_statement_is_not_counted_as_covered(self) -> None:
        # A comment or a blank line is not a unit. Counting it as covered would let a
        # change pad its own ratio with prose.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/module.py", "def first() -> int:\n    return 1\n\n\n# a comment\n")

            result = self.measure(root, seed, {"pkg/module.py": {"executed_lines": [1, 2]}})

            self.assertEqual(result.state, "no-measurable-changes")
            self.assertEqual(result.total_units, 0)

    def test_a_new_module_the_suite_never_imports_scores_zero(self) -> None:
        # Coverage.py lists an unimported file under `--cov` with every line missing,
        # which is the case a floor exists to catch.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/fresh.py", "def never_called() -> int:\n    return 7\n")
            git(root, "add", "-A")

            result = self.measure(root, seed, {"pkg/fresh.py": {"missing_lines": [1, 2]}})

            self.assertEqual(result.state, "measured")
            self.assertEqual(result.percent, 0.0)
            self.assertEqual(result.uncovered_lines, ("pkg/fresh.py:1", "pkg/fresh.py:2"))

    def test_an_empty_diff_says_so_rather_than_reporting_a_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)

            result = self.measure(root, seed, {})
            rendered = "\n".join(diff_coverage.render(result, 100.0))

            self.assertEqual(result.state, "no-changed-lines")
            self.assertIn("nothing for a coverage floor to certify", rendered)
            self.assertNotIn("changed coverage", rendered)

    def test_a_diff_touching_only_non_python_files_is_its_own_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "README.md", "prose\n")
            git(root, "add", "-A")

            result = self.measure(root, seed, {})
            rendered = "\n".join(diff_coverage.render(result, 100.0))

            self.assertEqual(result.state, "no-python-changes")
            self.assertIn("none of them Python", rendered)

    def test_changed_python_outside_the_measured_packages_is_named_on_every_run(self) -> None:
        # `--cov` measures the shipped packages, so the suite itself and `scripts/` are
        # outside it. An unscored file nobody prints is indistinguishable from a covered
        # one, so the report lists them whether or not the floor passes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "scripts/sync.py", "A = 1\nB = 2\n")
            git(root, "add", "-A")

            result = self.measure(root, seed, {})
            rendered = "\n".join(diff_coverage.render(result, 100.0))

            self.assertEqual(result.state, "no-measurable-changes")
            self.assertEqual(result.unmeasured_files, (("scripts/sync.py", 2),))
            self.assertIn("scripts/sync.py", rendered)
            self.assertIn("outside the packages coverage measures", rendered)

    def test_the_report_states_the_base_and_the_floor_it_was_judged_against(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)

            result = self.measure(root, seed, {})
            rendered = "\n".join(diff_coverage.render(result, 100.0))

            self.assertIn(seed, rendered)
            self.assertIn("100.0%", rendered)

    def test_a_report_without_branch_measurement_is_refused(self) -> None:
        # The floor counts branch arcs. A statement-only report has none, so every
        # partially-taken branch on a changed line would silently read as covered.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/module.py", "def first() -> int:\n    return 1\n\n\nX = 2\n")
            report = coverage_report(root, {"pkg/module.py": {"executed_lines": [5]}}, branch=False)

            with self.assertRaises(RuntimeError) as raised:
                diff_coverage.measure(
                    root, report, diff_coverage.BaseResolution(revision=seed, origin="test")
                )

            self.assertIn("branch_coverage", str(raised.exception))

    def test_the_ratio_of_a_measurement_with_no_units_is_not_a_division_error(self) -> None:
        empty = diff_coverage.empty_result(
            diff_coverage.BaseResolution(revision="x", origin="test"), "no-changed-lines"
        )

        self.assertEqual(empty.ratio, 1.0)
        self.assertEqual(empty.percent, 100.0)


class WrapperIntegrationTests(unittest.TestCase):
    """The floor as the wrapper runs it: same coverage JSON, same exit code."""

    def build(self, root: Path, floor: float, base: str) -> check.CheckConfig:
        return check.CheckConfig(
            project_root=root,
            scope=check.GateScope(
                lint_paths=[], type_paths=[], coverage_paths=[Path("pkg")], test_paths=[]
            ),
            admission=QUALITY_TEST_ADMISSION,
            coverage_json=None,
            threshold=20.0,
            top=5,
            diff_base=base,
            diff_floor=floor,
        )

    def test_a_diff_below_the_floor_fails_the_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/module.py", "def first() -> int:\n    return 1\n\n\nX = 2\nY = 3\n")
            report = coverage_report(
                root, {"pkg/module.py": {"executed_lines": [5], "missing_lines": [6]}}
            )
            output: list[str] = []

            failures = check.run_diff_coverage(
                self.build(root, 100.0, seed), report, root, printer=output.append
            )

            self.assertEqual(failures, 1)
            self.assertTrue(any("pkg/module.py:6" in line for line in output))

    def test_a_diff_at_the_floor_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(root, "pkg/module.py", "def first() -> int:\n    return 1\n\n\nX = 2\n")
            report = coverage_report(root, {"pkg/module.py": {"executed_lines": [5]}})
            output: list[str] = []

            failures = check.run_diff_coverage(
                self.build(root, 100.0, seed), report, root, printer=output.append
            )

            self.assertEqual(failures, 0)
            self.assertTrue(any("100.00%" in line for line in output))

    def test_a_missing_coverage_json_fails_instead_of_passing_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            output: list[str] = []

            failures = check.run_diff_coverage(
                self.build(root, 100.0, seed), root / "absent.json", root, printer=output.append
            )

            self.assertEqual(failures, 1)
            self.assertTrue(any("coverage JSON was not created" in line for line in output))

    def test_an_unusable_base_fails_the_step_rather_than_the_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seeded_repository(root)
            report = coverage_report(root, {})
            output: list[str] = []

            failures = check.run_diff_coverage(
                self.build(root, 100.0, "no-such-revision"), report, root, printer=output.append
            )

            self.assertEqual(failures, 1)
            self.assertTrue(any("no-such-revision" in line for line in output))

    def test_the_floor_runs_inside_the_wrapper_rather_than_beside_it(self) -> None:
        # A separate invocation is a gate somebody has to remember to run, which is the
        # same as not having one. This asserts the wrapper's own exit code carries it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = seeded_repository(root)
            write(
                root,
                "pyproject.toml",
                "[tool.pytest.ini_options]\n"
                'testpaths = ["tests"]\n'
                "[tool.agents_remember]\n"
                'product_package_roots = ["pkg"]\n'
                "verification_package_roots = []\n",
            )
            write(root, "pkg/module.py", "def first() -> int:\n    return 1\n\n\nX = 2\nY = 3\n")
            git(root, "add", "-A")
            report = coverage_report(
                root, {"pkg/module.py": {"executed_lines": [5], "missing_lines": [6]}}
            )
            report_payload = report.read_text(encoding="utf-8")
            output: list[str] = []

            def runner(name: str, command: list[str], cwd: Path, env: object) -> check.StepResult:
                if name == "pytest":
                    report.write_text(report_payload, encoding="utf-8")
                return check.StepResult(name=name, return_code=0, command=command)

            config = check.CheckConfig(
                project_root=root,
                scope=check.derive_scope(root),
                admission=QUALITY_TEST_ADMISSION,
                coverage_json=report,
                threshold=20.0,
                top=5,
                diff_base=seed,
                diff_floor=100.0,
            )
            exit_code = check.run_quality_check(config, runner=runner, printer=output.append)

            self.assertEqual(exit_code, 1)
            self.assertTrue(any("## diff-coverage" in line for line in output))
            self.assertTrue(any("pkg/module.py:6" in line for line in output))

    def test_the_wrapper_exposes_the_base_and_floor_as_flags(self) -> None:
        parsed = check.build_parser().parse_args(["--diff-base", "abc123", "--diff-floor", "95"])

        self.assertEqual(parsed.diff_base, "abc123")
        self.assertEqual(parsed.diff_floor, 95.0)

    def test_delivery_floor_defaults_to_ninety_percent(self) -> None:
        self.assertEqual(diff_coverage.DEFAULT_DIFF_COVERAGE_FLOOR, 90.0)
        self.assertEqual(check.build_parser().parse_args([]).diff_floor, 90.0)


if __name__ == "__main__":
    unittest.main()
