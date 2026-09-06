"""Input/output checks for opt-in integration selection and result validation."""

from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = REPO_ROOT / "mcp" / "src"
sys.path.insert(0, str(MCP_SRC))

MARKER_NAME = re.compile(r"^([a-z_][a-z0-9_]*):")
ENVIRONMENT_NAME = re.compile(r"\b(?:AR|AGENTS_REMEMBER)_[A-Z0-9_]+\b")


def load_runner() -> ModuleType:
    path = REPO_ROOT / "scripts" / "run-gated-integration.py"
    spec = importlib.util.spec_from_file_location("run_gated_integration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_gated_integration"] = module
    spec.loader.exec_module(module)
    return module


def marker_entries() -> list[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return data["tool"]["pytest"]["ini_options"]["markers"]


def registered_gated_markers() -> list[str]:
    entries = [entry for entry in marker_entries() if ENVIRONMENT_NAME.search(entry)]
    names = [MARKER_NAME.match(entry) for entry in entries]
    return [match.group(1) for match in names if match is not None]


class GatedPathInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = load_runner()

    def test_the_runner_covers_every_registered_gated_marker_and_invents_none(self) -> None:
        self.assertEqual(
            {path.marker for path in self.runner.PATHS},
            set(registered_gated_markers()),
        )

    def test_every_path_states_what_it_requires(self) -> None:
        for path in self.runner.PATHS:
            with self.subTest(path=path.name):
                self.assertTrue(path.requires.strip())
                self.assertIn(
                    path.category,
                    {self.runner.CREDENTIAL_FREE, self.runner.VENDOR_CREDENTIALS},
                )

    def test_the_credential_free_paths_are_exactly_the_two_dagger_safe_runs(self) -> None:
        # Credential freedom is a claim about what each path needs, so it is asserted
        # here rather than inferred by external automation. Pi RPC installs its own
        # runtime and drives it offline against 127.0.0.1; the real-MCP path spawns this
        # repository's own server against a generated settings file. Everything else
        # needs an installed, signed-in vendor CLI, and four of those bill for real turns.
        credential_free = {
            path.name for path in self.runner.PATHS if path.category == self.runner.CREDENTIAL_FREE
        }

        self.assertEqual(
            credential_free,
            {"ar-run-pi-rpc-smoke", "agents-remember-real-mcp-config"},
        )

    def test_the_dry_run_selection_names_a_test_that_exists(self) -> None:
        # A stale node id would make an acceptance selection run nothing and still exit 0.
        node = self.runner.DRY_RUN_NODE
        file_part, class_name, test_name = node.split("::")
        source = (REPO_ROOT / file_part).read_text(encoding="utf-8")

        self.assertIn(f"class {class_name}(", source)
        self.assertIn(f"def {test_name}(", source)


class RunnerBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = load_runner()

    def test_the_generated_settings_file_carries_no_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.runner.write_settings(root)
            text = settings.read_text(encoding="utf-8")

            self.assertTrue(settings.is_file())
            for secret in ("apiKey", "api_key", "token", "password", "secret"):
                with self.subTest(secret=secret):
                    self.assertNotIn(secret, text)

    def test_the_generated_tree_exists_so_the_server_can_plan_against_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.runner.write_settings(root)

            self.assertTrue((root / "ar-coordination").is_dir())
            self.assertTrue((root / "workspace").is_dir())
            self.assertTrue(
                (root / "ar-coordination" / "memory-repos" / "ar-agents-remember").is_dir()
            )

    def test_a_short_run_fails_the_required_count(self) -> None:
        # The anti-skip guard. A skipped test exits pytest 0, so without this a runner
        # reports success for a job that ran nothing.
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.xml"
            report.write_text(
                '<testsuites><testsuite tests="2" skipped="1" errors="0" failures="0">'
                "</testsuite></testsuites>",
                encoding="utf-8",
            )

            self.assertEqual(self.runner.verify_passed(report, 2), 1)
            self.assertEqual(self.runner.verify_passed(report, 1), 0)

    def test_a_missing_report_fails_rather_than_passing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.runner.verify_passed(Path(tmp) / "absent.xml", 1), 1)

    def test_a_report_without_a_testsuite_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.xml"
            report.write_text("<testsuites></testsuites>", encoding="utf-8")

            self.assertEqual(self.runner.verify_passed(report, 1), 1)

    def test_listing_reports_readiness_without_running_anything(self) -> None:
        self.assertEqual(self.runner.main(["list"]), 0)

    def test_the_child_environment_sets_the_opt_in_variable(self) -> None:
        path = self.runner.BY_NAME["ar-run-pi-rpc-smoke"]

        environment = self.runner.child_environment(path, None)

        self.assertEqual(environment["AR_RUN_PI_RPC_SMOKE"], "1")
        self.assertIn(str(MCP_SRC), environment["PYTHONPATH"])

    def test_the_settings_path_is_handed_to_the_suite_by_variable(self) -> None:
        path = self.runner.BY_NAME["agents-remember-real-mcp-config"]

        environment = self.runner.child_environment(path, Path("/tmp/settings.json"))

        self.assertEqual(environment["AGENTS_REMEMBER_REAL_MCP_CONFIG"], "/tmp/settings.json")

    def test_credential_free_selects_both_no_vendor_account_paths(self) -> None:
        self.assertEqual(
            [path.name for path in self.runner.selected("credential-free")],
            ["ar-run-pi-rpc-smoke", "agents-remember-real-mcp-config"],
        )

    def test_readiness_reports_a_missing_binary_by_name(self) -> None:
        absent = self.runner.GatedPath(
            name="probe",
            marker="probe",
            environment={},
            category=self.runner.VENDOR_CREDENTIALS,
            requires="nothing",
            binaries=("a-binary-that-does-not-exist",),
        )

        self.assertIn("missing on PATH", self.runner.readiness(absent))
        self.assertEqual(
            self.runner.readiness(self.runner.BY_NAME["agents-remember-real-mcp-config"]),
            "no binary needed",
        )

    def test_the_pytest_command_selects_by_marker_unless_a_node_is_named(self) -> None:
        path = self.runner.BY_NAME["ar-run-pi-rpc-smoke"]
        request = self.runner.RunRequest()

        by_marker = self.runner.pytest_command(path, request, node=None, report=None)
        by_node = self.runner.pytest_command(path, request, node="a::b::c", report=Path("r.xml"))

        self.assertIn("-m", by_marker)
        self.assertIn("ar_run_pi_rpc_smoke", by_marker)
        self.assertIn("a::b::c", by_node)
        self.assertIn("--junit-xml", by_node)


if __name__ == "__main__":
    unittest.main()
