from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from _quality_admission import QUALITY_TEST_ADMISSION
from _ruff_repository_evidence import (
    ruff_lint_configuration,
    run_ruff_over_tracked_python,
    run_ruff_with_repository_configuration,
)
from agents_remember_test_support.code_quality import (
    check,
    crap_calculator,
    quality_plan,
)

COMPLEXITY_RULES = ("C901", "PLR0911", "PLR0912", "PLR0915")

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = Path(__file__).resolve().parent
ENVIRONMENT_NAME = re.compile(r"\b(?:AR|AGENTS_REMEMBER)_[A-Z0-9_]+\b")
SKIP_DECORATORS = ("skipUnless", "skipIf", "skipif")


class CodeQualityCheckTests(unittest.TestCase):
    def test_check_is_the_stable_facade_for_the_single_quality_plan(self) -> None:
        self.assertIs(check.CheckConfig, quality_plan.CheckConfig)
        self.assertIs(check.Step, quality_plan.Step)
        self.assertIs(check.quality_steps, quality_plan.quality_steps)

    def test_progress_and_coverage_state_overwrite_report_local_paths(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            source = write_sample_source(root)
            progress = root / "reports/quality-progress.json"
            coverage_data = root / "reports/coverage.data"
            pytest_events = root / "reports/pytest-events.jsonl"
            pytest_phases = root / "reports/pytest-phases.json"
            pytest_events.parent.mkdir(parents=True, exist_ok=True)
            pytest_events.write_text("stale\n", encoding="utf-8")
            pytest_phases.write_text("stale\n", encoding="utf-8")
            config = replace(
                sample_config(root, source),
                progress_report=progress,
                coverage_data=coverage_data,
                pytest_report_log=pytest_events,
                pytest_phase_report=pytest_phases,
            )

            exit_code = check.run_quality_check(
                config,
                runner=fake_runner([], root / "coverage.json"),
                printer=lambda _message: None,
            )

            self.assertEqual(exit_code, 0)
            payload = json.loads(progress.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["step"], "complete")
            self.assertIn("pytest", payload["completedSteps"])
            self.assertEqual(check.subprocess_env(config)["COVERAGE_FILE"], str(coverage_data))
            self.assertFalse(pytest_events.exists())
            self.assertFalse(pytest_phases.exists())

    def test_targeted_config_keeps_the_repository_file_size_arm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_source(root)
            full_scope = sample_config(root, source).scope
            derived = mock.Mock()
            derived.to_gate_scope.return_value = full_scope
            derived.coverage_root_modules = ()
            args = mock.Mock(
                project_root=root,
                targeted=True,
                diff_base="base",
                coverage_json=None,
                coverage_data=None,
                progress_report=None,
                pytest_report_log=None,
                threshold=30.0,
                top=20,
                diff_floor=100.0,
            )
            progress_report = root / "reports" / "quality-progress.json"
            explicit_progress_report = root / "reports" / "explicit-quality-progress.json"

            with (
                mock.patch.dict(
                    os.environ,
                    {check.QUALITY_PROGRESS_REPORT_ENV: progress_report.as_posix()},
                    clear=True,
                ),
                mock.patch.object(check.quality_scope, "validate_quality_config"),
                mock.patch.object(check.scope_reporting, "validate_invocation_environment"),
                mock.patch.object(
                    check.diff_coverage,
                    "resolve_base",
                    return_value=mock.Mock(revision="base"),
                ),
                mock.patch.object(check.targeted, "derive_targeted_scope", return_value=derived),
                mock.patch.object(check, "derive_scope", return_value=full_scope),
                mock.patch.object(check.quality_scope, "file_size_armed", return_value=True),
            ):
                config = check.config_from_args(args, admission=QUALITY_TEST_ADMISSION)
                args.progress_report = explicit_progress_report
                explicit_config = check.config_from_args(
                    args,
                    admission=QUALITY_TEST_ADMISSION,
                )

            self.assertTrue(config.targeted)
            self.assertTrue(config.file_size_armed)
            self.assertEqual(config.progress_report, progress_report)
            self.assertEqual(explicit_config.progress_report, explicit_progress_report)
            file_size = next(
                step
                for step in check.quality_steps(config, root / "coverage.json")
                if step.name == "file-size"
            )
            self.assertNotIn("--report", file_size.command)

    def test_every_development_entrypoint_pins_the_same_ruff_version(self) -> None:
        requirements = (REPOSITORY_ROOT / "requirements.txt").read_text(encoding="utf-8")
        package = tomllib.loads(
            (REPOSITORY_ROOT / "mcp" / "pyproject.toml").read_text(encoding="utf-8")
        )
        required = next(line for line in requirements.splitlines() if line.startswith("ruff=="))
        package_ruff = next(
            item
            for item in package["project"]["optional-dependencies"]["dev"]
            if item.startswith("ruff")
        )

        self.assertEqual(package_ruff, required)

    def test_quality_check_runs_fixed_suite_and_crap_calculator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_source(root)
            commands: list[list[str]] = []
            output: list[str] = []

            exit_code = check.run_quality_check(
                sample_config(root, source),
                runner=fake_runner(commands, root / "coverage.json"),
                printer=output.append,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                command_modules(commands),
                [
                    "ruff",
                    "ruff",
                    "agents_remember_test_support.code_quality.file_size",
                    "agents_remember_test_support.code_quality.layering",
                    "pyright",
                    "agents_remember_test_support.testing.evidence_lifecycle",
                    "radon",
                    "radon",
                    "pytest",
                ],
            )
            pyright_command = commands[4]
            self.assertIn("--pythonpath", pyright_command)
            self.assertIn(sys.executable, pyright_command)
            self.assertIn(source.as_posix(), pyright_command)
            self.assertIn((root / "tests").as_posix(), commands[8])
            self.assertTrue(any("CRAP-Calculator" in line for line in output))
            # Both post-pytest rails score the one coverage report the run produced.
            self.assertTrue(any("## diff-coverage" in line for line in output))

    def test_quality_check_fails_when_a_fixed_step_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_source(root)
            coverage_json = root / "coverage.json"
            commands: list[list[str]] = []

            exit_code = check.run_quality_check(
                sample_config(root, source),
                runner=fake_runner(commands, coverage_json, failing_step="ruff"),
                printer=lambda message: None,
            )

            self.assertEqual(exit_code, 1)
            self.assertNotIn("pytest", command_modules(commands))

    def test_quality_check_fails_when_coverage_json_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_source(root)

            exit_code = check.run_quality_check(
                sample_config(root, source, coverage_json=root / "missing-coverage.json"),
                runner=lambda name, command, cwd, env: check.StepResult(name, 0, command),
                printer=lambda message: None,
            )

            self.assertEqual(exit_code, 1)

    def test_crap_threshold_fails_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_source(root)
            coverage_json = root / "coverage.json"

            exit_code = check.run_quality_check(
                sample_config(root, source, threshold=1.0),
                runner=fake_runner([], coverage_json),
                printer=lambda message: None,
            )

            self.assertEqual(exit_code, 1)

    def test_cli_has_no_report_only_or_strict_opt_in_mode(self) -> None:
        help_text = unwrapped_help()

        self.assertNotIn("report-only", help_text)
        self.assertNotIn("fail-on-crap-threshold", help_text)
        self.assertIn("mandatory CRAP threshold enforcement", help_text)

    def test_repository_acceptance_has_no_ci_or_host_runner(self) -> None:
        hook = (REPOSITORY_ROOT / ".githooks" / "_gate.sh").read_text(encoding="utf-8")
        workflow = (REPOSITORY_ROOT / ".github/workflows/quality-checks.yml").read_text(
            encoding="utf-8"
        )
        dagger_main = (REPOSITORY_ROOT / ".dagger/src/agents_remember_quality/main.py").read_text(
            encoding="utf-8"
        )
        profile = (REPOSITORY_ROOT / "mcp/certification-profile-v1.json").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("agents_remember_test_support.code_quality.check", hook)
        self.assertIn("acceptance is Dagger-only", hook)
        self.assertNotIn("for step in lint typecheck test", hook)
        self.assertNotIn('npm --prefix dashboard run "test"', hook)
        self.assertNotIn("dagger/dagger-for-github", workflow)
        self.assertIn("./.githooks/_gate.sh targeted", workflow)
        self.assertNotIn("pytest", workflow)
        self.assertNotIn("npm run test", workflow)
        self.assertNotIn("agents_remember_test_support.code_quality.check", workflow)
        self.assertNotIn("quality_wrapper_command", dagger_main)
        self.assertIn('"railId": "python-suite"', profile)
        for content in (hook, workflow, dagger_main):
            self.assertNotIn("fail-on-crap-threshold", content)

    def test_workflows_preserve_the_pr_check_and_release_only_boundaries(self) -> None:
        workflows = REPOSITORY_ROOT / ".github" / "workflows"
        workflow_files = sorted(path.name for path in workflows.iterdir() if path.is_file())
        quality = (workflows / "quality-checks.yml").read_text(encoding="utf-8")
        publish = (workflows / "publish-mcp-to-pypi.yml").read_text(encoding="utf-8")

        def trigger_lines(document: str) -> tuple[str, ...]:
            match = re.search(r"(?ms)^on:\s*\n(?P<body>.*?)(?=^[^\s#])", document)
            self.assertIsNotNone(match)
            assert match is not None
            return tuple(
                line.strip()
                for line in match.group("body").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )

        self.assertEqual(workflow_files, ["publish-mcp-to-pypi.yml", "quality-checks.yml"])
        self.assertEqual(trigger_lines(quality), ("pull_request:",))
        self.assertIn("./.githooks/_gate.sh targeted", quality)
        self.assertEqual(trigger_lines(publish), ("push:", "tags:", '- "mcp-v*"'))
        self.assertIn("git merge-base --is-ancestor HEAD origin/main", publish)
        all_workflows = "\n".join(
            (workflows / name).read_text(encoding="utf-8") for name in workflow_files
        ).lower()
        for forbidden_class in (
            r"\bpytest\b",
            r"\bvitest\b",
            r"\bplaywright\b",
            r"agents[_-]remember\.code_quality\.check",
            r"dagger/dagger-for-github",
            r"\bdagger\s+call\s+quality\b",
            r"run-gated-integration",
            r"\bnpm(?:\s+--prefix\s+\S+)?\s+(?:run\s+)?(?:test|e2e)(?::\S+)?\b",
        ):
            with self.subTest(forbidden_class=forbidden_class):
                self.assertNotRegex(all_workflows, forbidden_class)

    def test_git_hooks_delegate_to_the_shared_tiered_gate(self) -> None:
        hook_tiers = {"pre-commit": "fast", "pre-push": "targeted"}

        for hook_name, tier in hook_tiers.items():
            hook = REPOSITORY_ROOT / ".githooks" / hook_name
            with self.subTest(hook=hook_name):
                content = hook.read_text(encoding="utf-8")
                self.assertIn(f'exec "$hook_dir/_gate.sh" {tier}', content)
                self.assertNotIn("fail-on-crap-threshold", content)

    def test_the_pre_push_tier_keeps_acceptance_inside_dagger(self) -> None:
        gate = (REPOSITORY_ROOT / ".githooks" / "_gate.sh").read_text(encoding="utf-8")

        self.assertIn("run_targeted_checks", gate)
        self.assertIn("leaf closeout owns targeted Dagger acceptance", gate)
        self.assertNotIn("code_quality.check --targeted", gate)
        self.assertIn("refusing host full-suite execution", gate)
        self.assertIn("usage: _gate.sh <fast|targeted|full>", gate)

    def test_run_fixed_checks_threads_checkout_source_onto_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_source(root)
            seen_env: list[Mapping[str, str]] = []

            def runner(
                name: str, command: list[str], cwd: Path, env: Mapping[str, str]
            ) -> check.StepResult:
                seen_env.append(env)
                return check.StepResult(name, 0, command)

            with mock.patch.dict(os.environ, {"PYTHONPATH": "/pre-existing"}):
                check.run_fixed_checks(
                    sample_config(root, source),
                    root / "coverage.json",
                    runner=runner,
                    printer=lambda message: None,
                )

            self.assertTrue(seen_env)
            entries = seen_env[0]["PYTHONPATH"].split(os.pathsep)
            # The checkout's own source import root comes first; a pre-existing
            # PYTHONPATH is preserved at the end.
            self.assertEqual(entries[0], str(source.resolve().parent))
            self.assertEqual(entries[-1], "/pre-existing")


class RadonIsAReportNotAGateTests(unittest.TestCase):
    """Radon exits 0 whatever it finds, so it never could fail this gate.

    The wrapper used to list it alongside the checks that can. These tests hold the
    correction in place: the two Radon steps are labelled reports, their findings are
    incapable of failing the run, and the help text no longer claims otherwise.
    """

    def test_radon_steps_are_declared_reports_and_the_rest_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_source(root)

            steps = check.quality_steps(sample_config(root, source), root / "coverage.json")

            reporting = {step.name for step in steps if not step.enforcing}
            enforcing = {step.name for step in steps if step.enforcing}
            self.assertEqual(reporting, {"radon-cc", "radon-mi"})
            self.assertEqual(
                enforcing,
                {
                    "ruff",
                    "ruff-format",
                    "pyright",
                    "pytest",
                    "file-size",
                    "layering",
                    "evidence-lifecycle",
                },
            )

    def test_report_section_header_says_it_cannot_fail(self) -> None:
        enforcing = check.step_header(check.Step("ruff", ["ruff"]))
        reporting = check.step_header(
            check.Step("radon-cc", ["radon"], report_note=check.RADON_REPORT_NOTE)
        )

        self.assertEqual(enforcing, "\n## ruff")
        self.assertIn("report only", reporting)
        self.assertIn("fail the gate", reporting)

    def test_help_text_does_not_present_radon_as_enforcement(self) -> None:
        help_text = unwrapped_help()

        self.assertIn("Radon", help_text)
        self.assertIn("report only", help_text)
        # The old description read "Ruff, Pyright, Radon, pytest coverage, and mandatory
        # CRAP threshold enforcement", which put Radon inside the enforcing list.
        self.assertNotIn("Pyright, Radon", help_text)

    def test_a_report_step_that_breaks_still_fails_the_gate(self) -> None:
        # A non-zero exit from a tool that exits 0 on every finding means the tool
        # itself is broken. Swallowing that would be a silent skip.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_source(root)
            output: list[str] = []

            failures = check.run_fixed_checks(
                sample_config(root, source),
                root / "coverage.json",
                runner=fake_runner([], root / "coverage.json", failing_step="radon-cc"),
                printer=output.append,
            )

            self.assertEqual(failures, 1)
            self.assertTrue(any("radon-cc could not run" in line for line in output))


class EveryEnforcingStepCanFailTests(unittest.TestCase):
    """The two steps this leaf added, and the complexity rules at full strength.

    `ruff format` and the complexity rules were both configured-but-unenforced before
    260731-EFA-L2: the formatter ran in no gate at all, and `max-complexity = 10` was set
    while `C901` was unselected. Arming them produced 67 complexity offenders, which were
    first parked behind a shrink-only baseline and then -- on the developer's correction --
    refactored outright. These assertions hold the wiring at full strength: the `ruff` step
    routes no rule away from itself, the four codes are selected and unignored, and a real
    Ruff run at this repository's configuration rejects an over-complex function.
    """

    def test_the_ruff_step_routes_no_rule_away_from_itself(self) -> None:
        # This step used to carry `--extend-ignore C901,PLR0911,PLR0912,PLR0915` so a
        # separate baseline step could own those four. Any narrowing flag here is a rule
        # the gate silently stops enforcing, so the command is asserted whole rather than
        # by the absence of one spelling.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_source(root)

            command = quality_steps_by_name(root)["ruff"].command

            self.assertEqual(command, [sys.executable, "-m", "ruff", "check", source.as_posix()])

    def test_the_complexity_rules_are_selected_and_nothing_ignores_them(self) -> None:
        lint = ruff_lint_configuration()
        selected = set(lint.get("select", []))
        ignored = set(lint.get("ignore", []))
        per_file: dict[str, list[str]] = lint.get("per-file-ignores", {})

        # `PL` selects the PLR09xx family; `C901` is named on its own line.
        self.assertIn("C901", selected)
        self.assertIn("PL", selected)
        for rule in COMPLEXITY_RULES:
            with self.subTest(rule=rule):
                self.assertNotIn(rule, ignored)
                for pattern, codes in per_file.items():
                    self.assertNotIn(rule, codes, f"{pattern} exempts {rule}")

    def test_ruff_rejects_an_over_complex_function_at_this_repository_configuration(
        self,
    ) -> None:
        # The rules biting is the whole point of removing the baseline, so it is proved
        # against the real configuration rather than inferred from the flag list above.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "over_complex.py"
            source.write_text(over_complex_function(), encoding="utf-8")

            completed = run_ruff_with_repository_configuration(source)

            self.assertEqual(completed.returncode, 1, completed.stdout)
            reported = {entry["code"] for entry in json.loads(completed.stdout)}
            self.assertEqual(reported & set(COMPLEXITY_RULES), set(COMPLEXITY_RULES))

    def test_no_suppression_directive_in_the_tree_holds_a_complexity_rule_down(self) -> None:
        # A line-level suppression naming one of these codes is a per-function baseline
        # with a shorter name, and it is the one exemption the gate itself cannot see:
        # Ruff honours the directive, so the wrapper goes green either way.
        # `--ignore-noqa` asks the question the gate cannot -- if the tree is still clean
        # with every directive disregarded, no directive is holding a rule down.
        #
        # Asserted against Ruff rather than by reading the source text. A comment that
        # merely discusses a suppression is indistinguishable from a real one to a grep,
        # and writing the directive's own spelling here to say so would make Ruff parse
        # this very comment as one.
        completed = run_ruff_over_tracked_python(
            "--ignore-noqa", "--select", ",".join(COMPLEXITY_RULES)
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)

    def test_ruff_format_is_checked_over_the_whole_derived_scope(self) -> None:
        # `ruff format --check` was in no gate before this leaf, and 206 files in the
        # wrapper's scope failed it. The debt was paid outright, so this step needs no
        # baseline -- but it does need the same tree-derived scope as the lint rail,
        # because 10 of the reformatted files were outside the old hand-written constant.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_sample_source(root)
            steps = quality_steps_by_name(root)
            step = steps["ruff-format"]

            self.assertTrue(step.enforcing)
            self.assertEqual(step.command[2:5], ["ruff", "format", "--check"])
            self.assertIn(source.as_posix(), step.command)
            self.assertEqual(
                [argument for argument in step.command if argument.endswith(".py")],
                [argument for argument in steps["ruff"].command if argument.endswith(".py")],
            )

    def test_the_complexity_baseline_and_its_gate_step_are_gone(self) -> None:
        # The ratchet is not merely emptied, it is deleted: an empty exemption list is a
        # place to put the next offender. The module, its file and the wrapper step that
        # ran it all go together, and the two local gates lose the routing that fed it.
        self.assertFalse((REPOSITORY_ROOT / "quality").exists())
        support = REPOSITORY_ROOT / "mcp/test_support/agents_remember_test_support/code_quality"
        self.assertFalse((support / "complexity_baseline.py").exists())
        for gate_file in (
            REPOSITORY_ROOT / ".githooks" / "_gate.sh",
            REPOSITORY_ROOT / ".github" / "workflows" / "quality-checks.yml",
        ):
            with self.subTest(gate_file=gate_file.name):
                self.assertNotIn("complexity_baseline", gate_file.read_text(encoding="utf-8"))
        # The fast tier's lint invocation, asserted as the command it runs rather than as
        # the absence of a spelling: the comment above it names `--extend-ignore` to say
        # why it is gone, and a substring search cannot tell that from the flag itself.
        self.assertEqual(
            shell_command_lines(REPOSITORY_ROOT / ".githooks" / "_gate.sh", "-m ruff check"),
            ['if over_tracked_python "$py" -m ruff check; then'],
        )


class CrapThresholdEnforcementTests(unittest.TestCase):
    """CRAP is enforced by the threshold alone -- there is no exemption list beside it."""

    def test_a_failing_gate_names_every_offender_not_only_the_reported_top(self) -> None:
        # The rendered table is capped at `--top`. The failure list is the work, so it is
        # not: a truncated finding list sends the reader off to re-run the tool by hand.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_many_branchy_functions(root, count=8)
            output: list[str] = []

            failures = check.run_crap_calculator(
                sample_config(root, source, threshold=5.0, top=2),
                root / "coverage.json",
                root,
                printer=output.append,
            )

            text = "\n".join(output)
            self.assertEqual(failures, 1)
            self.assertIn("8 function(s) meet or exceed the CRAP threshold", text)
            for index in range(8):
                self.assertIn(f"branchy_{index}", text)

    def test_an_offender_is_told_the_branch_coverage_that_would_clear_it(self) -> None:
        score = crap_calculator.FunctionScore(
            path=Path("pkg/sample.py"),
            function="f",
            kind="function",
            start_line=12,
            end_line=30,
            complexity=10,
            covered_lines=1,
            missing_lines=1,
            executable_lines=2,
            covered_branches=1,
            missing_branches=9,
            coverage_ratio=0.2,
            crap=crap_calculator.crap_score(10, 0.2),
        )

        line = check.crap_failure_line(score, Path("."), 25.0)

        self.assertIn("pkg/sample.py:12", line)
        # crap(10, c) < 25 needs (1 - c)**3 < 0.15, i.e. coverage above 46.9%.
        self.assertIn("above 46.9%", line)

    def test_an_offender_that_no_test_can_clear_is_told_to_split_instead(self) -> None:
        score = crap_calculator.FunctionScore(
            path=Path("pkg/sample.py"),
            function="f",
            kind="function",
            start_line=12,
            end_line=30,
            complexity=26,
            covered_lines=1,
            missing_lines=0,
            executable_lines=1,
            covered_branches=1,
            missing_branches=0,
            coverage_ratio=1.0,
            crap=26.0,
        )

        line = check.crap_failure_line(score, Path("."), 25.0)

        self.assertIn("cannot clear 25.0 at any coverage", line)
        self.assertIn("split it", line)

    def test_the_clearing_coverage_inverts_the_crap_formula(self) -> None:
        for complexity, threshold in ((6, 30.0), (10, 25.0), (15, 22.0)):
            with self.subTest(complexity=complexity, threshold=threshold):
                clearing = crap_calculator.coverage_clearing(complexity, threshold)
                assert clearing is not None
                self.assertAlmostEqual(crap_calculator.crap_score(complexity, clearing), threshold)
        self.assertIsNone(crap_calculator.coverage_clearing(30, 30.0))

    def test_no_repository_gate_carries_a_crap_exemption_file(self) -> None:
        # The threshold is the whole policy. A baseline, allowlist or ignore file beside it
        # would be an exemption the gate could not see past, which is the failure mode the
        # CRAP rail exists to avoid. `quality/` is where such a file would live -- it held
        # the complexity baseline until that was deleted -- so its absence is the assertion,
        # not a glob inside a directory that no longer exists.
        self.assertFalse((REPOSITORY_ROOT / "quality").exists())
        self.assertEqual(sorted(REPOSITORY_ROOT.glob("*crap*")), [])
        self.assertEqual(
            [
                path
                for path in check.git_ls_files(REPOSITORY_ROOT, "*")
                if "crap" in path.name.casefold() and path.suffix != ".py"
            ],
            [],
        )


def run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def write_sample_repository(root: Path) -> Path:
    """A throwaway repository shaped like this one: a package, a test tree, a script."""
    run_git(root, "init", "--quiet")
    (root / "pyproject.toml").write_text(
        "\n".join(
            (
                "[tool.ruff]",
                "line-length = 100",
                "[tool.pyright]",
                'include = ["."]',
                "[tool.radon]",
                'cc_min = "B"',
                "[tool.coverage.run]",
                "branch = true",
                "[tool.pytest.ini_options]",
                'testpaths = ["tests"]',
                "[tool.agents_remember]",
                'product_package_roots = ["pkg"]',
                "verification_package_roots = []",
                "",
            )
        ),
        encoding="utf-8",
    )
    for directory in ("pkg", "tests", "scripts"):
        (root / directory).mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_pkg.py").write_text(
        "def test_nothing() -> None: ...\n", encoding="utf-8"
    )
    (root / "scripts" / "sync.py").write_text("value = 1\n", encoding="utf-8")
    run_git(root, "add", "-A")
    return root


def unwrapped_help() -> str:
    """The parser's help with argparse's line wrapping undone.

    argparse rewraps the description to the terminal width, so asserting on a phrase
    means asserting on where the wrap happened to fall.
    """
    return re.sub(r"\s+", " ", check.build_parser().format_help())


def over_complex_function() -> str:
    """A function that trips all four complexity rules at once.

    Sixty guarded returns: 60 branches (PLR0912 allows 12), 61 returns (PLR0911 allows
    6), 121 statements (PLR0915 allows 50) and a cyclomatic complexity of 61 (C901 is
    configured at 10). Nothing subtle -- the point is that the configuration rejects it.
    """
    lines = ["def deliberately_over_complex(value: int) -> int:"]
    for index in range(60):
        lines.append(f"    if value == {index}:")
        lines.append(f"        return {index}")
    lines.append("    return -1")
    return "\n".join(lines) + "\n"


def shell_command_lines(script: Path, needle: str) -> list[str]:
    """Executable lines of ``script`` containing ``needle``, comments excluded."""
    return [
        stripped
        for line in script.read_text(encoding="utf-8").splitlines()
        if needle in (stripped := line.strip()) and not stripped.startswith("#")
    ]


def pytest_ini_options() -> dict[str, object]:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return data["tool"]["pytest"]["ini_options"]


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_code_quality_check.py:782).
def ini_strings(key: str) -> list[str]:  # pragma: no cover
    """One ini option as the list of strings pytest's schema says it is.

    A wrong shape raises here rather than reading as an empty list, which would let a
    mistyped option pass every assertion below it.
    """
    value = pytest_ini_options()[key]
    if not isinstance(value, list):
        raise TypeError(f"[tool.pytest.ini_options] {key} must be a list, got {type(value)!r}")
    return [str(entry) for entry in value]


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_code_quality_check.py:794).
def suite_environment_gates() -> set[str]:  # pragma: no cover
    """Environment variables that gate a whole test path in this suite.

    Read from the skip decorators themselves rather than from a list somebody keeps by
    hand. Module-level constants are substituted first, because the installed-runtime
    modules spell their gate as ``LIVE_OPT_IN`` and the real name only appears in the
    assignment. Only the *condition* is inspected: the reason strings also mention
    variables that merely parameterise a run (``AR_CLAUDE_STREAM_BINARY``), and those
    are not gates.
    """
    found: set[str] = set()
    for module in sorted(TESTS_ROOT.glob("test_*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        constants = module_level_constants(tree)
        for decorator in skip_decorators(tree):
            if not decorator.args:
                continue
            condition = substitute(ast.unparse(decorator.args[0]), constants)
            found.update(ENVIRONMENT_NAME.findall(condition))
    return found


def module_level_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            constants[target.id] = ast.unparse(node.value)
    return constants


def skip_decorators(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and is_skip_call(decorator.func):
                calls.append(decorator)
    return calls


def is_skip_call(func: ast.expr) -> bool:
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in SKIP_DECORATORS


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_code_quality_check.py:843).
def substitute(expression: str, constants: dict[str, str]) -> str:  # pragma: no cover
    """Inline module-level constants until the expression stops changing."""
    for _ in range(3):
        replaced = re.sub(
            r"\b[A-Za-z_][A-Za-z_0-9]*\b",
            lambda match: constants.get(match.group(0), match.group(0)),
            expression,
        )
        if replaced == expression:
            break
        expression = replaced
    return expression


def sample_scope(root: Path, source: Path) -> check.GateScope:
    return check.GateScope(
        lint_paths=[source],
        type_paths=[source],
        coverage_paths=[source],
        test_paths=[root / "tests"],
        size_paths=[source],
    )


def sample_config(
    root: Path,
    source: Path,
    *,
    coverage_json: Path | None = None,
    threshold: float = 30.0,
    top: int = 5,
) -> check.CheckConfig:
    return check.CheckConfig(
        project_root=root,
        scope=sample_scope(root, source),
        admission=QUALITY_TEST_ADMISSION,
        coverage_json=coverage_json if coverage_json is not None else root / "coverage.json",
        threshold=threshold,
        top=top,
    )


def write_many_branchy_functions(root: Path, *, count: int) -> Path:
    """`count` uncovered three-path functions, with the coverage report that scores them.

    Only each `def` line is executed -- the shape of a module that is imported and never
    called -- so every function carries two untaken branches and scores well above any
    threshold these tests use.
    """
    body = [
        "def branchy_{index}(value):",
        "    if value > 0:",
        "        return 1",
        "    if value < 0:",
        "        return 2",
        "    return 3",
        "",
    ]
    lines: list[str] = []
    executed: list[int] = []
    missing: list[int] = []
    missing_branches: list[list[int]] = []
    for index in range(count):
        start = len(lines) + 1
        lines.extend(line.format(index=index) for line in body)
        executed.append(start)
        missing.extend(range(start + 1, start + 6))
        missing_branches.extend(
            [[start + 1, start + 2], [start + 1, start + 3], [start + 3, start + 4]]
        )

    source = root / "sample.py"
    source.write_text("\n".join(lines), encoding="utf-8")
    (root / "coverage.json").write_text(
        json.dumps(
            {
                "meta": {"format": 3, "branch_coverage": True},
                "files": {
                    "sample.py": {
                        "executed_lines": executed,
                        "missing_lines": missing,
                        "executed_branches": [],
                        "missing_branches": missing_branches,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return source


def quality_steps_by_name(root: Path) -> dict[str, check.Step]:
    """The wrapper's steps for a throwaway root, keyed by step name."""
    source = write_sample_source(root)
    return {
        step.name: step
        for step in check.quality_steps(sample_config(root, source), root / "coverage.json")
    }


def write_sample_source(root: Path) -> Path:
    """One branchy module in a real repository.

    The repository is not decoration. Every rail of the wrapper reads the tree through
    git -- `derive_scope` from `git ls-files`, the changed-lines coverage floor from
    `git diff` against the merge base -- so a sample project that is not a repository
    exercises the wrapper in a state it can never actually be run from.
    """
    source = root / "sample.py"
    source.write_text(
        "\n".join(
            [
                "def simple(value):",
                "    if value:",
                "        return value + 1",
                "    return 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if not (root / ".git").exists():
        run_git(root, "init", "--quiet", "--initial-branch=main")
        run_git(root, "add", "-A")
        run_git(
            root,
            "-c",
            "user.email=gate@agents-remember.invalid",
            "-c",
            "user.name=Gate Tests",
            "commit",
            "--quiet",
            "-m",
            "sample",
        )
    return source


def fake_runner(
    commands: list[list[str]],
    coverage_json: Path,
    *,
    failing_step: str | None = None,
) -> check.CommandRunner:
    def run(name: str, command: list[str], cwd: Path, env: Mapping[str, str]) -> check.StepResult:
        commands.append(command)
        if name == "pytest":
            coverage_json.write_text(
                json.dumps(
                    {
                        # `meta.branch_coverage` is what the CRAP reader checks before it
                        # will score anything, so the stand-in report has to carry it.
                        "meta": {"format": 3, "branch_coverage": True},
                        "files": {
                            "sample.py": {
                                "executed_lines": [1, 2, 3, 4],
                                "missing_lines": [],
                                "executed_branches": [[2, 3], [2, 4]],
                                "missing_branches": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
        return check.StepResult(name, 1 if name == failing_step else 0, command)

    return run


def command_modules(commands: list[list[str]]) -> list[str]:
    return [command[2] for command in commands]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
