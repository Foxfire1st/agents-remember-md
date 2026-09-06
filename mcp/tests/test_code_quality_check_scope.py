from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from _quality_admission import QUALITY_TEST_ADMISSION
from agents_remember_test_support.code_quality import check
from test_code_quality_check import (
    ENVIRONMENT_NAME,
    REPOSITORY_ROOT,
    ini_strings,
    pytest_ini_options,
    run_git,
    suite_environment_gates,
    write_sample_repository,
)


class GateScopeDerivationTests(unittest.TestCase):
    def test_module_declares_no_hand_written_scope_constant(self) -> None:
        source = (
            REPOSITORY_ROOT / "mcp/test_support/agents_remember_test_support/code_quality/check.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("DEFAULT_SOURCE_PATHS", source)
        self.assertNotIn("DEFAULT_TEST_PATHS", source)

    def test_scope_derived_from_this_checkout_applies_each_rail_to_its_owner(self) -> None:
        scope = check.derive_scope(REPOSITORY_ROOT)
        expected_coverage = [Path("mcp/src/agents_remember")]

        self.assertGreater(len(scope.lint_paths), 500)
        self.assertEqual(scope.lint_paths, scope.type_paths)
        self.assertEqual(scope.coverage_paths, expected_coverage)
        self.assertEqual(scope.test_paths, [Path("mcp/tests")])

    def test_a_script_outside_every_package_reaches_ruff_and_pyright(self) -> None:
        # The case the old hand-written constants missed for 1,882 lines: a script that
        # is neither inside the source package nor inside the test tree. Nobody has to
        # remember to add it -- the derivation finds it because git tracks it, and the
        # assertion is made on the argument vectors the tools actually receive.
        with tempfile.TemporaryDirectory() as tmp:
            root = write_sample_repository(Path(tmp))

            scope = check.derive_scope(root)
            steps = {
                step.name: step
                for step in check.quality_steps(
                    check.CheckConfig(
                        project_root=root,
                        scope=scope,
                        admission=QUALITY_TEST_ADMISSION,
                        coverage_json=None,
                        threshold=30.0,
                        top=5,
                    ),
                    root / "coverage.json",
                )
            }

            self.assertIn(Path("scripts/sync.py"), scope.lint_paths)
            self.assertIn("scripts/sync.py", steps["ruff"].command)
            self.assertIn("scripts/sync.py", steps["pyright"].command)
            self.assertEqual(scope.coverage_paths, [Path("pkg")])
            self.assertIn("--cov=pkg", steps["pytest"].command)
            self.assertNotIn("--cov=tests", steps["pytest"].command)

    def test_scope_is_the_index_so_an_unadded_file_is_not_yet_part_of_the_tree(self) -> None:
        # `git ls-files` reads the index, so `git add`-ing a file puts it in scope
        # immediately -- which is what the pre-commit tier certifies. A file that has
        # never been added is not part of the tree yet, and this records that boundary
        # rather than leaving it to be discovered.
        with tempfile.TemporaryDirectory() as tmp:
            root = write_sample_repository(Path(tmp))
            (root / "scripts" / "scratch.py").write_text("value = 2\n", encoding="utf-8")

            self.assertNotIn(Path("scripts/scratch.py"), check.derive_scope(root).lint_paths)

            run_git(root, "add", "scripts/scratch.py")

            self.assertIn(Path("scripts/scratch.py"), check.derive_scope(root).lint_paths)

    def test_top_level_packages_ignores_nested_packages(self) -> None:
        tracked = [
            Path("mcp/src/agents_remember/__init__.py"),
            Path("mcp/src/agents_remember/kernel/__init__.py"),
            Path("scripts/sync-skills.py"),
        ]

        self.assertEqual(check.top_level_packages(tracked), [Path("mcp/src/agents_remember")])

    def test_new_importable_package_requires_explicit_product_or_verification_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_sample_repository(Path(tmp))
            support = root / "support"
            support.mkdir()
            (support / "__init__.py").write_text("", encoding="utf-8")
            run_git(root, "add", "support/__init__.py")

            with self.assertRaises(check.ScopeError) as raised:
                check.derive_scope(root)

            self.assertIn("missing=['support']", str(raised.exception))

            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace(
                    "verification_package_roots = []",
                    'verification_package_roots = ["support"]',
                ),
                encoding="utf-8",
            )
            scope = check.derive_scope(root)

            self.assertEqual(scope.coverage_paths, [Path("pkg")])
            self.assertIn(Path("support/__init__.py"), scope.lint_paths)
            self.assertIn(Path("support/__init__.py"), scope.type_paths)

    def test_package_authority_rejects_overlap_and_stale_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_sample_repository(Path(tmp))
            pyproject = root / "pyproject.toml"
            original = pyproject.read_text(encoding="utf-8")
            pyproject.write_text(
                original.replace(
                    "verification_package_roots = []",
                    'verification_package_roots = ["pkg"]',
                ),
                encoding="utf-8",
            )

            with self.assertRaises(check.ScopeError) as overlap:
                check.derive_scope(root)

            self.assertIn("both product and verification", str(overlap.exception))

            pyproject.write_text(
                original.replace(
                    "verification_package_roots = []",
                    'verification_package_roots = ["missing_support"]',
                ),
                encoding="utf-8",
            )

            with self.assertRaises(check.ScopeError) as stale:
                check.derive_scope(root)

            self.assertIn("stale=['missing_support']", str(stale.exception))

    def test_missing_testpaths_is_an_error_rather_than_a_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")

            with self.assertRaises(check.ScopeError) as raised:
                check.pytest_testpaths(root)

            self.assertIn("testpaths", str(raised.exception))

    def test_a_project_with_no_pyproject_at_all_cannot_derive_where_the_suite_lives(self) -> None:
        # The neighbouring case to a missing `testpaths` key: there is no file to read it
        # from. Both must refuse, because the fallback a reader might expect -- "just run
        # everything" or "run nothing" -- is how a gate reports success for an empty run.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaises(check.ScopeError) as raised:
                check.pytest_testpaths(root)

            self.assertIn("no pyproject.toml at", str(raised.exception))
            self.assertIn((root / "pyproject.toml").as_posix(), str(raised.exception))

    def test_a_pytest_table_that_is_not_a_table_reads_as_absent_rather_than_crashing(self) -> None:
        # `[tool.pytest] = "..."` is a typo a person makes, not a shape TOML forbids. Walking
        # into a scalar must yield "no such section" so the missing-testpaths refusal fires,
        # rather than an AttributeError from the wrapper's own scope derivation.
        data: Mapping[str, object] = {"tool": {"pytest": "tests"}}

        self.assertEqual(check.toml_section(data, ("tool", "pytest", "ini_options")), {})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[tool]\npytest = "tests"\n', encoding="utf-8")

            with self.assertRaises(check.ScopeError) as raised:
                check.pytest_testpaths(root)

            self.assertIn("testpaths is missing or empty", str(raised.exception))

    def test_a_repository_tracking_no_python_is_refused_instead_of_scoped_to_nothing(self) -> None:
        # An empty scope would make ruff, pyright and pytest each run over zero paths and
        # exit 0, so the gate would pass by measuring nothing at all.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "--quiet")
            (root / "README.md").write_text("no code here\n", encoding="utf-8")
            run_git(root, "add", "-A")

            with self.assertRaises(check.ScopeError) as raised:
                check.derive_scope(root)

            self.assertIn("git tracks no Python files", str(raised.exception))

    def test_python_that_belongs_to_no_package_refuses_explicit_product_authority(self) -> None:
        # Tracked Python, but no importable package. The explicit authority contract cannot
        # name an operational product root, so the gate refuses before deriving empty coverage.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "--quiet")
            (root / "pyproject.toml").write_text(
                '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
                "[tool.agents_remember]\nproduct_package_roots = []\n"
                "verification_package_roots = []\n",
                encoding="utf-8",
            )
            (root / "sync.py").write_text("value = 1\n", encoding="utf-8")
            run_git(root, "add", "-A")

            self.assertEqual(check.top_level_packages([Path("sync.py")]), [])

            with self.assertRaises(check.ScopeError) as raised:
                check.derive_scope(root)

            self.assertIn(
                "product_package_roots must contain at least one operational package",
                str(raised.exception),
            )

    @mock.patch.object(check, "require_dagger_admission", new=lambda **_: QUALITY_TEST_ADMISSION)
    def test_scope_failure_exits_non_zero_with_an_explanation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output: list[str] = []
            with mock.patch.object(check, "print_line", output.append):
                exit_code = check.main(["--project-root", tmp])

            self.assertEqual(exit_code, 1)
            self.assertTrue(any("gate scope could not be derived" in line for line in output))

    @mock.patch.object(check, "require_dagger_admission", new=lambda **_: QUALITY_TEST_ADMISSION)
    def test_a_derivable_scope_runs_the_gate_and_main_reports_its_verdict(self) -> None:
        """``main`` owns no verdict of its own: it derives the scope, then hands back
        whatever the gate decided. The scope it hands over is the one derived from the
        project root on the command line, and the threshold and diff base are the parsed
        arguments -- not defaults re-invented here."""
        handed: list[check.CheckConfig] = []

        def gate(config: check.CheckConfig) -> int:
            handed.append(config)
            return 7

        with tempfile.TemporaryDirectory() as tmp:
            root = write_sample_repository(Path(tmp))
            with mock.patch.object(check, "run_quality_check", gate):
                exit_code = check.main(
                    [
                        "--project-root",
                        str(root),
                        "--threshold",
                        "12.5",
                        "--diff-base",
                        "HEAD~1",
                    ]
                )

        self.assertEqual(exit_code, 7)
        [config] = handed
        self.assertEqual(config.project_root, root.resolve())
        self.assertEqual(config.threshold, 12.5)
        self.assertEqual(config.diff_base, "HEAD~1")
        self.assertEqual(config.scope.coverage_paths, [Path("pkg")])
        self.assertEqual(config.scope.test_paths, [Path("tests")])
        self.assertIn(Path("scripts/sync.py"), config.scope.lint_paths)


class PytestConfigurationTests(unittest.TestCase):
    """The pytest configuration this repository had none of until 260731-EFA-L2."""

    def test_strictness_switches_are_on(self) -> None:
        self.assertIn("-n=4", ini_strings("addopts"))
        self.assertIn("not integration", ini_strings("addopts"))
        self.assertIn("--strict-markers", ini_strings("addopts"))
        self.assertIn("--strict-config", ini_strings("addopts"))
        self.assertIs(pytest_ini_options()["xfail_strict"], True)
        self.assertEqual(ini_strings("testpaths"), ["mcp/tests"])

    def test_python_classes_covers_the_house_naming_convention(self) -> None:
        # `<Subject>Tests` is what this suite actually writes; pytest's default only
        # matches `Test<Subject>`. Every such class reaches unittest.TestCase today, so
        # this is prospective -- a plain `PlainTests` class would be silently skipped.
        self.assertEqual(ini_strings("python_classes"), ["Test*", "*Tests"])

    def test_filterwarnings_errors_by_default(self) -> None:
        entries = ini_strings("filterwarnings")

        self.assertEqual(entries[0], "error")
        for entry in entries[1:]:
            with self.subTest(entry=entry):
                self.assertTrue(entry.startswith("ignore"))

    def test_the_warning_ignore_list_is_capped(self) -> None:
        # `error` first means any warning not named below fails the suite, so the list
        # cannot grow silently. This cap is the other half: it makes *shrinking* the
        # list a required edit rather than an optional one. An exact count, not a
        # ceiling -- paying one entry off forces the number down in the same commit.
        # Ignore entries at 2026-07-31: 3, all third party. The two that were ours were
        # ResourceWarnings for a subprocess pipe and a TemporaryDirectory finalised by GC
        # instead of closed; both leaks are fixed, so both entries are gone. What is left
        # is a starlette testclient notice and two websockets deprecations reached through
        # uvicorn: no action exists here, and they go when uvicorn moves off the deprecated
        # websockets API.
        ignores = [entry for entry in ini_strings("filterwarnings") if entry.startswith("ignore")]

        self.assertEqual(len(ignores), 3)

    def test_registered_markers_and_the_suite_environment_gates_agree(self) -> None:
        # Registering a marker for a gate that no longer exists leaves a stale line;
        # adding a gate without a marker leaves a path nothing can select. Both are
        # failures, so the registry cannot drift in either direction.
        registered = {
            name for marker in ini_strings("markers") for name in ENVIRONMENT_NAME.findall(marker)
        }

        self.assertEqual(registered, suite_environment_gates())
