from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
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
MCP_TEST_SUPPORT = Path(__file__).resolve().parents[1] / "test_support"
sys.path.insert(0, str(MCP_SRC))

from _quality_admission import QUALITY_TEST_ADMISSION
from agents_remember_test_support.code_quality import (
    check,
    crap_calculator,
    diff_coverage,
    scope,
    scope_reporting,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ESLINT_EXECUTABLE = REPOSITORY_ROOT / "dashboard/node_modules/.bin/eslint"
ESLINT_AVAILABLE = ESLINT_EXECUTABLE.is_file() and os.access(ESLINT_EXECUTABLE, os.X_OK)


def run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )


def write_quality_config(root: Path, *, pyright_venv: bool = False) -> None:
    venv = 'venvPath = "."\nvenv = ".venv"\n' if pyright_venv else ""
    (root / "pyproject.toml").write_text(
        "\n".join(
            (
                "[tool.ruff]",
                "line-length = 100",
                "[tool.pyright]",
                'include = ["."]',
                venv.rstrip(),
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


def sample_repository(root: Path) -> Path:
    run_git(root, "init", "--quiet", "--initial-branch=main")
    write_quality_config(root)
    for directory in ("pkg", "tests", "scripts", "dashboard/src"):
        (root / directory).mkdir(parents=True)
    (root / "pkg/__init__.py").write_text("", encoding="utf-8")
    (root / "pkg/module.py").write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    (root / "tests/test_module.py").write_text(
        "def test_value() -> None:\n    assert True\n", encoding="utf-8"
    )
    (root / "scripts/sync.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "dashboard/package.json").write_text(
        json.dumps({"scripts": {"lint": "eslint ."}}), encoding="utf-8"
    )
    (root / ".gitignore").write_text("pkg/ignored.py\n", encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(
        root,
        "-c",
        "user.email=scope@agents-remember.invalid",
        "-c",
        "user.name=Scope Tests",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    return root


def config_for(root: Path, derived: scope.GateScope) -> check.CheckConfig:
    return check.CheckConfig(
        project_root=root,
        scope=derived,
        admission=QUALITY_TEST_ADMISSION,
        coverage_json=root / "coverage.json",
        threshold=30.0,
        top=5,
    )


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def workflow_run_blocks(workflow: str) -> list[str]:
    lines = workflow.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        match = re.match(r"^(?P<indent>\s*)run:\s*(?P<value>.*)$", lines[index])
        if match is None:
            index += 1
            continue
        value = match.group("value")
        if value != "|":
            blocks.append(value)
            index += 1
            continue
        indent = len(match.group("indent"))
        body: list[str] = []
        index += 1
        while index < len(lines):
            line = lines[index]
            if line and len(line) - len(line.lstrip()) <= indent:
                break
            body.append(line.strip())
            index += 1
        blocks.append("\n".join(body))
    return blocks


class WrapperScopeOutputTests(unittest.TestCase):
    def test_wrapper_tier_reports_scope_untracked_state_and_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            derived = scope.derive_scope(root)
            output: list[str] = []

            def runner(
                name: str,
                command: list[str],
                cwd: Path,
                env: Mapping[str, str],
            ) -> check.StepResult:
                del cwd, env
                return check.StepResult(name, 0, command)

            with (
                mock.patch.object(check, "run_crap_calculator", return_value=0),
                mock.patch.object(check, "run_diff_coverage", return_value=0),
            ):
                exit_code = check.run_quality_check(
                    config_for(root, derived), runner=runner, printer=output.append
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(output[0].startswith("scope: quality-wrapper | input="))
            self.assertTrue(any(line.startswith("scope: untracked-exposure |") for line in output))
            self.assertEqual(output[-1], "result: quality-wrapper PASS")

    def test_every_fixed_step_states_input_config_and_nonzero_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            derived = scope.derive_scope(root)
            config = replace(
                config_for(root, derived),
                causal_failure_report=root / "causal-failures.json",
            )
            names = [step.name for step in check.quality_steps(config, root / "coverage.json")]

            for name in names:
                with self.subTest(name=name):
                    line = scope_reporting.fixed_step_scope_line(name, root, derived)
                    self.assertTrue(line.startswith(f"scope: {name} | input="), line)
                    self.assertIn(" | config=", line)
                    self.assertIn(" | units=", line)
                    self.assertNotIn("units=0 ", line)

    def test_fixed_step_scope_precedes_an_explicit_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            derived = scope.derive_scope(root)
            output: list[str] = []

            def runner(
                name: str,
                command: list[str],
                cwd: Path,
                env: Mapping[str, str],
            ) -> check.StepResult:
                del cwd, env
                return check.StepResult(name, 0, command)

            failures = check.run_fixed_checks(
                config_for(root, derived),
                root / "coverage.json",
                runner=runner,
                printer=output.append,
            )

            self.assertEqual(failures, 0)
            for name in (
                "ruff",
                "ruff-format",
                "pyright",
                "radon-cc",
                "radon-mi",
                "pytest",
                "file-size",
                "evidence-lifecycle",
            ):
                header = next(index for index, line in enumerate(output) if f"## {name}" in line)
                provenance = next(
                    index
                    for index, line in enumerate(output)
                    if line.startswith(f"scope: {name} |")
                )
                result = next(
                    index
                    for index, line in enumerate(output)
                    if line.startswith(f"result: {name} ")
                )
                self.assertLess(header, provenance)
                self.assertLess(provenance, result)

    def test_zero_function_crap_scope_refuses_instead_of_passing_vacuously(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            coverage = root / "coverage.json"
            coverage.write_text("{}", encoding="utf-8")
            output: list[str] = []
            with mock.patch.object(crap_calculator, "calculate_scores", return_value=[]):
                result = check.run_crap_calculator(
                    config_for(root, scope.derive_scope(root)),
                    coverage,
                    root,
                    printer=output.append,
                )

            self.assertEqual(result, 1)
            self.assertTrue(any("units=0 functions" in line for line in output))
            self.assertIn("result: CRAP-Calculator FAIL", output)

    def test_diff_scope_names_dirty_tree_exclusion_and_denominator(self) -> None:
        result = diff_coverage.DiffCoverage(
            base=diff_coverage.BaseResolution("abc123", "test base"),
            state="measured",
            covered_units=7,
            total_units=9,
            uncovered_lines=(),
            untaken_branches=(),
            unmeasured_files=(),
            changed_files=2,
        )

        line = scope_reporting.diff_scope_line(
            result,
            Path("coverage.json"),
            100.0,
            environment={},
        )

        self.assertIn("manual working tree", line)
        self.assertIn("untracked files excluded", line)
        self.assertIn("units=2 changed Python files; 9 measurable statements+branches", line)

    def test_radon_and_coverage_report_distinct_on_disk_and_result_populations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            (root / "pkg/untracked module.py").write_text(
                "def untracked() -> int:\n    return 2\n", encoding="utf-8"
            )
            derived = scope.derive_scope(root)
            radon = scope_reporting.fixed_step_scope_line("radon-cc", root, derived)
            pytest_line = scope_reporting.fixed_step_scope_line("pytest", root, derived)
            coverage = root / "coverage.json"
            coverage.write_text(
                json.dumps({"files": {"pkg/module.py": {}, "pkg/untracked module.py": {}}}),
                encoding="utf-8",
            )
            result = scope_reporting.coverage_result_scope_line(coverage)

        self.assertIn("units=3 on-disk product Python files", radon)
        self.assertIn("3 on-disk product Python files offered to Coverage.py", pytest_line)
        self.assertIn("units=2 Coverage.py file records", result)

    def test_coverage_result_scope_is_printed_before_pytest_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            coverage = root / "coverage.json"
            output: list[str] = []

            def runner(
                name: str,
                command: list[str],
                cwd: Path,
                env: Mapping[str, str],
            ) -> check.StepResult:
                del cwd, env
                if name == "pytest":
                    coverage.write_text(
                        json.dumps({"files": {"pkg/module.py": {}}}), encoding="utf-8"
                    )
                return check.StepResult(name, 0, command)

            failures = check.run_fixed_checks(
                config_for(root, scope.derive_scope(root)),
                coverage,
                runner=runner,
                printer=output.append,
            )

        self.assertEqual(failures, 0)
        provenance = next(
            index for index, line in enumerate(output) if line.startswith("scope: coverage-result")
        )
        result = next(
            index for index, line in enumerate(output) if line.startswith("result: pytest ")
        )
        self.assertLess(provenance, result)


class ConfigTruthTests(unittest.TestCase):
    def test_inert_pyright_venv_declaration_is_a_scope_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            write_quality_config(root, pyright_venv=True)

            with self.assertRaisesRegex(scope.ScopeError, "resolves to missing directory"):
                scope.validate_quality_config(root)

    def test_repository_pyright_config_relies_on_the_wrapper_interpreter(self) -> None:
        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as handle:
            pyright = tomllib.load(handle)["tool"]["pyright"]

        self.assertNotIn("venvPath", pyright)
        self.assertNotIn("venv", pyright)
        line = scope_reporting.fixed_step_scope_line(
            "pyright",
            REPOSITORY_ROOT,
            scope.derive_scope(REPOSITORY_ROOT),
        )
        self.assertIn("--pythonpath", line)
        self.assertIn("no venv declaration", line)

    def test_missing_required_tool_config_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            (root / "pyproject.toml").write_text(
                '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(scope.ScopeError, r"\[tool.ruff\].*missing"):
                scope.validate_quality_config(root)


class UntrackedExposureTests(unittest.TestCase):
    def test_source_test_and_dashboard_siblings_are_reported_without_git_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            before_index = run_git(root, "ls-files", "--stage").stdout
            before_stash = run_git(root, "stash", "list").stdout
            additions = {
                "pkg/new module.py": "VALUE = 2\n",
                "tests/test new.py": "def test_new() -> None:\n    assert True\n",
                "dashboard/src/new panel.tsx": "export const value = 1\n",
                "docs/outside.md": "outside\n",
                "pkg/ignored.py": "IGNORED = True\n",
            }
            for relative, content in additions.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            derived = scope.derive_scope(root)
            rendered = scope_reporting.untracked_scope_lines(derived, sample_limit=2)

            self.assertEqual(
                derived.untracked_paths,
                [
                    Path("dashboard/src/new panel.tsx"),
                    Path("pkg/new module.py"),
                    Path("tests/test new.py"),
                ],
            )
            self.assertNotIn(Path("docs/outside.md"), derived.untracked_paths)
            self.assertNotIn(Path("pkg/ignored.py"), derived.untracked_paths)
            self.assertTrue(any("NOT in this measurement" in line for line in rendered))
            self.assertEqual(rendered[-1], "  ... 1 more untracked file(s) not shown")
            self.assertEqual(run_git(root, "ls-files", "--stage").stdout, before_index)
            self.assertEqual(run_git(root, "stash", "list").stdout, before_stash)

    def test_untracked_enumeration_failure_refuses(self) -> None:
        failed = subprocess.CompletedProcess(
            ["git", "ls-files"],
            2,
            stdout="",
            stderr="enumeration broke",
        )
        with (
            mock.patch.object(scope.git_command, "run_git", return_value=failed),
            self.assertRaisesRegex(scope.ScopeError, "enumerate.*untracked"),
        ):
            scope.git_untracked_files(Path("."), [Path("pkg")])

    def test_untracked_central_module_is_exposed_beside_a_measured_100_percent_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            central = root / "pkg/durable_store.py"
            central.write_text("def durable() -> int:\n    return 1\n", encoding="utf-8")
            (root / "pkg/module.py").write_text(
                "def value() -> int:\n    return 1\n\n\nEXTRA = 2\n", encoding="utf-8"
            )
            coverage = root / "coverage.json"
            coverage.write_text(
                json.dumps(
                    {
                        "meta": {"format": 3, "branch_coverage": True},
                        "files": {
                            "pkg/module.py": {
                                "executed_lines": [1, 2, 5],
                                "missing_lines": [],
                                "executed_branches": [],
                                "missing_branches": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            derived = scope.derive_scope(root)
            result = diff_coverage.measure(
                root,
                coverage,
                diff_coverage.BaseResolution("HEAD", "test HEAD"),
            )
            rendered = "\n".join(scope_reporting.untracked_scope_lines(derived))

            self.assertEqual(result.state, "measured")
            self.assertEqual(result.percent, 100.0)
            self.assertGreater(result.total_units, 0)
            self.assertIn("pkg/durable_store.py", rendered)
            self.assertIn("NOT in this measurement", rendered)

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_quality_scope_reporting.py:451).
    def test_real_sequencer_hook_reports_spaced_untracked_path_without_mutation(
        self,
    ) -> None:  # pragma: no cover
        with tempfile.TemporaryDirectory() as tmp:
            root = sample_repository(Path(tmp))
            hook = root / ".githooks/_gate.sh"
            hook.parent.mkdir()
            shutil.copy2(REPOSITORY_ROOT / ".githooks/_gate.sh", hook)
            shutil.copy2(
                REPOSITORY_ROOT / "scripts/python-runtime-contract.env",
                root / "scripts/python-runtime-contract.env",
            )
            for name in (
                "sync-projection-types.py",
                "sync-skills.py",
                "sync-runtime.py",
                "sync-harness.py",
            ):
                script = root / "scripts" / name
                script.write_text(
                    'import sys\nif "--list-targets" in sys.argv:\n    print("generated-target")\n',
                    encoding="utf-8",
                )
            run_git(root, "add", ".githooks/_gate.sh", "scripts")
            run_git(
                root,
                "-c",
                "user.email=scope@agents-remember.invalid",
                "-c",
                "user.name=Scope Tests",
                "commit",
                "--quiet",
                "-m",
                "hook fixture",
            )
            write_executable(
                root / ".venv/bin/python",
                "#!/usr/bin/env sh\n"
                'echo "the repository-root .venv must not run MCP hooks" >&2\n'
                "exit 97\n",
            )
            write_executable(
                root / "mcp/.venv/bin/python",
                "#!/usr/bin/env sh\n"
                'if [ "$1" = "-m" ] && '
                '[ "$2" = "agents_remember_test_support.code_quality.scope_reporting" ]; then\n'
                f'  exec {shlex.quote(sys.executable)} "$@"\n'
                "fi\n"
                "exit 0\n",
            )
            # The dashboard rail is fail-closed on a missing install: give the temp repo a real
            # node_modules and a stub npm so the sequencer contract test exercises the hook's
            # untracked-scope behavior without invoking a frontend toolchain that is not installed.
            (root / "dashboard/node_modules").mkdir(parents=True, exist_ok=True)
            shim_dir = Path(tmp).parent / "l8-sequencer-bin"
            shim_dir.mkdir(parents=True, exist_ok=True)
            write_executable(shim_dir / "npm", "#!/usr/bin/env sh\nexit 0\n")
            untracked = root / "pkg/untracked module.py"
            untracked.write_text("VALUE = 2\n", encoding="utf-8")
            git_dir = Path(run_git(root, "rev-parse", "--git-dir").stdout.strip())
            if not git_dir.is_absolute():
                git_dir = root / git_dir
            (git_dir / "MERGE_HEAD").write_text(
                run_git(root, "rev-parse", "HEAD").stdout, encoding="utf-8"
            )
            index_path = Path(run_git(root, "rev-parse", "--git-path", "index").stdout.strip())
            if not index_path.is_absolute():
                index_path = root / index_path
            before_index_file = digest_bytes(index_path.read_bytes())
            before_index = digest_text(run_git(root, "ls-files", "--stage", "-z").stdout)
            before_cached = digest_text(run_git(root, "diff", "--cached", "--binary").stdout)
            before_stash = digest_text(run_git(root, "stash", "list").stdout)
            environment = dict(os.environ)
            environment["PYTHONPATH"] = f"{MCP_TEST_SUPPORT}:{MCP_SRC}"
            environment["PATH"] = f"{shim_dir}:{environment.get('PATH', '')}"

            completed = subprocess.run(
                [hook.as_posix(), "fast"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            after_index = digest_text(run_git(root, "ls-files", "--stage", "-z").stdout)
            after_index_file = digest_bytes(index_path.read_bytes())
            after_cached = digest_text(run_git(root, "diff", "--cached", "--binary").stdout)
            after_stash = digest_text(run_git(root, "stash", "list").stdout)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("scope: untracked-exposure", completed.stdout)
        self.assertIn("units=1 non-ignored untracked files", completed.stdout)
        self.assertIn("pkg/untracked module.py", completed.stdout)
        self.assertIn("NOT in this measurement", completed.stdout)
        self.assertEqual(after_index_file, before_index_file)
        self.assertEqual(after_index, before_index)
        self.assertEqual(after_cached, before_cached)
        self.assertEqual(after_stash, before_stash)


class CallerProvenanceTests(unittest.TestCase):
    def test_hook_selects_only_the_mcp_development_environment(self) -> None:
        gate = (REPOSITORY_ROOT / ".githooks/_gate.sh").read_text(encoding="utf-8")

        self.assertIn('local_py="$root/mcp/.venv/bin/python"', gate)
        self.assertIn('shared_py="$main_root/mcp/.venv/bin/python"', gate)
        self.assertIn("import agents_remember_test_support.code_quality.scope_reporting", gate)
        self.assertNotIn('[ -x ".venv/bin/python" ]', gate)
        self.assertNotIn("command -v python3", gate)
        self.assertIn("complete MCP dev environment not found at mcp/.venv", gate)

    def test_pre_push_ref_range_is_stated_without_a_worktree_certification_claim(self) -> None:
        raw = (
            "refs/heads/feature 1234567890abcdef refs/heads/feature "
            "0000000000000000000000000000000000000000\n"
        )
        environment = {
            scope_reporting.INVOCATION_ENV: "pre-push",
            scope_reporting.PUSH_UPDATES_ENV: raw,
        }

        scope_reporting.validate_invocation_environment(environment)
        description = scope_reporting.invocation_description(environment)

        self.assertIn("pre-push ref updates (1)", description)
        self.assertIn("refs/heads/feature@1234567890ab", description)
        self.assertIn("do not mutate the index", description)
        gate = (REPOSITORY_ROOT / ".githooks/_gate.sh").read_text(encoding="utf-8")
        pre_push = (REPOSITORY_ROOT / ".githooks/pre-push").read_text(encoding="utf-8")
        self.assertNotIn("certify the working tree", gate)
        self.assertIn('push_updates="$(cat)"', pre_push)
        self.assertIn('AR_QUALITY_INVOCATION="pre-push"', pre_push)

    def test_pre_push_shell_forwards_git_stdin_without_touching_the_index(self) -> None:
        update = (
            "refs/heads/feature 1234567890abcdef refs/heads/feature "
            "0000000000000000000000000000000000000000"
        )
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp)
            hook = hooks / "pre-push"
            shutil.copy2(REPOSITORY_ROOT / ".githooks/pre-push", hook)
            gate = hooks / "_gate.sh"
            gate.write_text(
                '#!/usr/bin/env sh\nprintf "%s\\n" "$1" "$AR_QUALITY_INVOCATION" '
                '"$AR_QUALITY_PUSH_UPDATES"\n',
                encoding="utf-8",
            )
            gate.chmod(0o755)

            completed = subprocess.run(
                [hook.as_posix()],
                input=f"{update}\n",
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(), ["targeted", "pre-push", update])

    def test_pre_push_with_no_updates_refuses_vacuous_range(self) -> None:
        environment = {
            scope_reporting.INVOCATION_ENV: "pre-push",
            scope_reporting.PUSH_UPDATES_ENV: "",
        }

        with self.assertRaisesRegex(scope_reporting.ScopeReportingError, "zero ref updates"):
            scope_reporting.validate_invocation_environment(environment)

    def test_closeout_labels_the_already_staged_candidate(self) -> None:
        environment = {scope_reporting.INVOCATION_ENV: "closeout-staged"}

        self.assertEqual(environment[scope_reporting.INVOCATION_ENV], "closeout-staged")
        self.assertIn("staged candidate", scope_reporting.invocation_description(environment))

    def test_integration_invocations_name_the_clean_checkout(self) -> None:
        base = diff_coverage.BaseResolution("c0", "test")

        master = scope_reporting.diff_input_description(
            base, {scope_reporting.INVOCATION_ENV: "master-integration"}
        )
        leaf = scope_reporting.diff_input_description(
            base, {scope_reporting.INVOCATION_ENV: "leaf-integration"}
        )

        self.assertIn("master integration tree", master)
        self.assertIn("leaf integration tree", leaf)

    def test_every_generated_hook_gate_has_a_counted_target_map(self) -> None:
        scripts = {
            "projection": Path("scripts/sync-projection-types.py"),
            "skills": Path("scripts/sync-skills.py"),
            "runtime": Path("scripts/sync-runtime.py"),
            "harness": Path("scripts/sync-harness.py"),
        }

        for name, script in scripts.items():
            with self.subTest(name=name):
                line = scope_reporting.generated_scope_line(REPOSITORY_ROOT, name, script)
                self.assertIn(f"scope: generated-{name}", line)
                self.assertRegex(line, r"units=[1-9][0-9]* generated targets")

    def test_dashboard_typecheck_names_nonvacuous_projects_and_inputs(self) -> None:
        line = scope_reporting.dashboard_scope_line(REPOSITORY_ROOT, "typecheck")

        self.assertIn("config=dashboard/tsconfig.json", line)
        self.assertRegex(line, r"units=3 projects; [1-9][0-9]* TypeScript inputs")

    @unittest.skipUnless(ESLINT_AVAILABLE, "dashboard-local ESLint executable is unavailable")
    def test_dashboard_lint_matches_live_eslint_machine_result_set(self) -> None:
        dashboard = REPOSITORY_ROOT / "dashboard"
        completed = subprocess.run(
            [
                (dashboard / "node_modules/.bin/eslint").as_posix(),
                ".",
                "--format",
                "json",
            ],
            cwd=dashboard,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertIn(completed.returncode, {0, 1}, completed.stderr)
        rows = json.loads(completed.stdout)
        line = scope_reporting.dashboard_scope_line(REPOSITORY_ROOT, "lint")

        self.assertIn(f"units={len(rows)} ESLint-resolved files", line)
        self.assertNotIn("styled-system/", "\n".join(row["filePath"] for row in rows))

    def test_dashboard_build_reports_only_config_owned_stable_inputs(self) -> None:
        line = scope_reporting.dashboard_scope_line(REPOSITORY_ROOT, "build")

        self.assertIn("Panda include [./src/**/*.{ts,tsx}]", line)
        self.assertIn("bundled module graph intentionally uncounted", line)
        self.assertIn(
            "units=1 Panda source glob; 3 TypeScript projects; 441 TypeScript inputs", line
        )
        self.assertIn("9 explicit Vite inputs", line)
        for name in (
            "index.html",
            "vite.config.ts",
            "tsconfig.json",
            "tsconfig.app.json",
            "tsconfig.node.json",
            "panda.config.ts",
            "postcss.config.cjs",
            "package.json",
            "package-lock.json",
        ):
            self.assertIn(name, line)

    def test_randomized_pytest_and_dagger_share_the_contract_while_ci_stays_non_test(self) -> None:
        line = scope_reporting.randomized_pytest_scope_line(REPOSITORY_ROOT, "260731")
        self.assertIn("scope: randomized-pytest", line)
        self.assertIn("seed=260731", line)
        self.assertRegex(line, r"units=[1-9][0-9]* Python test files")

        workflow = (REPOSITORY_ROOT / ".github/workflows/quality-checks.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("dagger/dagger-for-github", workflow)
        self.assertIn("./.githooks/_gate.sh targeted", workflow)
        self.assertFalse(
            any(
                "agents_remember_test_support.code_quality.check" in block
                or re.search(r"\bpython\s+-m\s+pytest\b", block)
                or re.search(r"\bnpm\s+run\s+", block)
                for block in workflow_run_blocks(workflow)
            )
        )
        dagger_main = (REPOSITORY_ROOT / ".dagger/src/agents_remember_quality/main.py").read_text(
            encoding="utf-8"
        )
        dagger_command = (
            REPOSITORY_ROOT / ".dagger/src/agents_remember_quality/quality_command.py"
        ).read_text(encoding="utf-8")
        profile = (REPOSITORY_ROOT / "mcp/certification-profile-v1.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("quality_wrapper_command", dagger_main)
        self.assertIn("agents_remember_test_support.code_quality.check", dagger_command)
        self.assertIn('"railId": "dashboard-lint"', profile)
        self.assertIn('"test:coverage"', profile)
        self.assertIn('"--fail-on-flaky-tests"', profile)

    def test_every_dashboard_ci_rail_uses_the_shared_provenance_path(self) -> None:
        profile = (REPOSITORY_ROOT / "mcp/certification-profile-v1.json").read_text(
            encoding="utf-8"
        )

        for step in ("typecheck", "test", "build", "coverage", "diff-coverage"):
            with self.subTest(step=step):
                line = scope_reporting.dashboard_scope_line(REPOSITORY_ROOT, step)
                self.assertIn(f"scope: dashboard-{step}", line)
                self.assertIn(" | input=", line)
                self.assertIn(" | config=", line)
                self.assertRegex(line, r"units=.*[1-9][0-9]*")
                command = {
                    "test": "test:coverage",
                    "coverage": "test:coverage",
                    "diff-coverage": "coverage:diff",
                }.get(step, step)
                self.assertIn(f'"{command}"', profile)

        e2e_line = scope_reporting.dashboard_scope_line(REPOSITORY_ROOT, "e2e")
        self.assertIn("scope: dashboard-e2e", e2e_line)
        self.assertIn(" | input=", e2e_line)
        self.assertIn(" | config=", e2e_line)
        self.assertRegex(e2e_line, r"units=.*[1-9][0-9]*")
        self.assertIn('"railId": "dashboard-browser-e2e"', profile)
        self.assertIn('"--fail-on-flaky-tests"', profile)

        gate = (REPOSITORY_ROOT / ".githooks/_gate.sh").read_text(encoding="utf-8")
        self.assertIn("agents_remember_test_support.code_quality.scope_reporting", gate)
        self.assertNotIn("agents_remember_test_support.code_quality.check", gate)
        self.assertIn("acceptance is Dagger-only", gate)

    def test_vacuous_dashboard_project_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = Path(tmp)
            (dashboard / "tsconfig.json").write_text(
                json.dumps({"files": [], "references": [{"path": "./tsconfig.app.json"}]}),
                encoding="utf-8",
            )
            (dashboard / "tsconfig.app.json").write_text(
                json.dumps({"files": [], "include": []}), encoding="utf-8"
            )

            with self.assertRaisesRegex(scope_reporting.ScopeReportingError, "zero input files"):
                scope_reporting.tsconfig_inputs(dashboard)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
