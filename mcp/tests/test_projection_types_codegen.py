"""Projection schema/TypeScript generation and its fail-on-diff gate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Literal

import pytest
from agents_remember.observer.projection import (
    AgentPickupNode,
    Analytics,
    EngineProcessNode,
    GateNode,
    LifecycleProjection,
    SeriesNode,
    TaskDocNode,
    WorkspaceProjection,
)
from agents_remember_test_support.code_quality import projection_types
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sync-projection-types.py"
PROJECTION_PROVENANCE_FILES = (
    Path("dashboard/e2e-production/cockpit.production.spec.ts"),
    Path("dashboard/src/dev/fixtures.ts"),
    Path("dashboard/src/test/fixtures/wire.ts"),
    Path("dashboard/src/test/wireFixtureGuard.test.ts"),
)
DATACLASS_MIRROR_FILES = (
    Path("dashboard/src/types/terminalCatalog.ts"),
    Path("dashboard/src/types/harnessCapabilities.ts"),
)


class ProjectionSchemaGenerationTests(unittest.TestCase):
    def test_schema_emission_is_the_workspace_projection_schema(self) -> None:
        emitted = json.loads(projection_types.schema_json())
        self.assertEqual(emitted, WorkspaceProjection.model_json_schema())
        self.assertEqual(emitted["title"], "WorkspaceProjection")

    def test_generation_is_deterministic_and_idempotent(self) -> None:
        first_schema = projection_types.schema_json()
        first_typescript = projection_types.typescript_text()
        self.assertEqual(first_schema, projection_types.schema_json())
        self.assertEqual(first_typescript, projection_types.typescript_text())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projection_types.sync_generated_files(root)
            first = {
                path: (root / path).read_bytes() for path in projection_types.generated_files()
            }
            projection_types.sync_generated_files(root)
            second = {
                path: (root / path).read_bytes() for path in projection_types.generated_files()
            }
        self.assertEqual(first, second)

    def test_schema_vocabulary_changes_flow_into_typescript(self) -> None:
        schema = json.loads(projection_types.schema_json())
        state = schema["$defs"]["LifecycleProjection"]["properties"]["state"]["enum"]
        state.append("reviewing")
        schema["$defs"]["Metrics"]["properties"]["reviewingCount"] = {
            "default": 0,
            "type": "integer",
        }
        schema["$defs"]["GateNode"]["properties"]["freshness"] = {"type": "boolean"}

        rendered = projection_types.render_typescript(
            schema,
            json.loads(projection_types.schema_json(projection_types.served_projection_schema())),
        )

        self.assertIn('"reviewing"', rendered)
        self.assertIn("freshness: boolean;", rendered)

    def test_non_nullable_output_fields_are_required_without_an_escape(self) -> None:
        schema = json.loads(projection_types.schema_json())
        rendered = projection_types.typescript_text()

        def serialized_defaults(model: type[BaseModel]) -> dict[str, object]:
            return model.model_construct().model_dump(exclude_none=True, by_alias=True)

        defaulted_fields = (
            (serialized_defaults(GateNode), ("evidenceRefs",)),
            (serialized_defaults(LifecycleProjection), ("stateEnteredAt",)),
            (serialized_defaults(AgentPickupNode), ("attemptCount",)),
            (
                serialized_defaults(TaskDocNode),
                ("bodyRevision", "createdAt", "orchestrates"),
            ),
            (serialized_defaults(SeriesNode), ("createdAt",)),
            (serialized_defaults(EngineProcessNode), ("landing",)),
            (serialized_defaults(Analytics), ("agentPickups", "expectationRows")),
        )
        for payload, field_names in defaulted_fields:
            for field_name in field_names:
                self.assertIn(field_name, payload)

        models = dict(schema["$defs"])
        models["WorkspaceProjection"] = {"properties": schema["properties"]}
        for model_name, model in models.items():
            if "properties" not in model:
                continue
            match = re.search(
                rf"export interface {re.escape(model_name)}(?: extends [^{{]+)? {{\n(.*?)\n}}",
                rendered,
                re.DOTALL,
            )
            self.assertIsNotNone(match, model_name)
            body = match.group(1) if match else ""
            for property_name, node in model["properties"].items():
                nullable = any(variant.get("type") == "null" for variant in node.get("anyOf", []))
                metric_bucket = (
                    model_name == "Metrics"
                    and property_name.endswith("Count")
                    and property_name != "lifecycleCount"
                )
                if nullable or metric_bucket:
                    continue
                self.assertRegex(body, rf"(?m)^  {re.escape(property_name)}: ")
                self.assertNotRegex(body, rf"(?m)^  {re.escape(property_name)}\?: ")

    def test_pydantic_single_literal_const_is_rendered_exactly(self) -> None:
        class ConstProbe(BaseModel):
            mode: Literal["strict"]

        schema = json.loads(projection_types.schema_json())
        schema["$defs"]["GateNode"]["properties"]["mode"] = ConstProbe.model_json_schema()[
            "properties"
        ]["mode"]

        rendered = projection_types.render_typescript(
            schema,
            json.loads(projection_types.schema_json(projection_types.served_projection_schema())),
        )

        self.assertIn('mode: "strict";', rendered)
        self.assertNotIn("mode: string;", rendered)

    def test_runtime_refinements_are_preserved_beside_the_typescript_shape(self) -> None:
        schema = json.loads(projection_types.schema_json())
        gate = schema["$defs"]["GateNode"]["properties"]
        gate["state"]["pattern"] = "^strict$"
        gate["decisions"]["items"]["minLength"] = 1

        rendered = projection_types.render_typescript(
            schema,
            json.loads(projection_types.schema_json(projection_types.served_projection_schema())),
        )

        self.assertIn(
            '/** JSON Schema refinements: {"pattern":"^strict$"} */\n  state:',
            rendered,
        )
        self.assertIn(
            '/** JSON Schema refinements: {"items":{"minLength":1}} */\n  decisions:',
            rendered,
        )

    def test_unknown_constraints_still_report_the_exact_path_and_remediation(self) -> None:
        schema = json.loads(projection_types.schema_json())
        schema["$defs"]["GateNode"]["properties"]["state"]["format"] = "custom"

        with self.assertRaises(projection_types.ProjectionTypeGenerationError) as raised:
            projection_types.render_typescript(
                schema,
                json.loads(
                    projection_types.schema_json(projection_types.served_projection_schema())
                ),
            )
        message = str(raised.exception)
        self.assertIn("GateNode.state: unsupported keyword 'format'", message)
        self.assertIn("preserve and render every listed keyword exactly", message)
        self.assertIn("never silently drop schema truth", message)


class ProjectionSchemaDriftTests(unittest.TestCase):
    def run_script(self, root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo-root", str(root), *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_check_reports_both_stale_outputs_without_rewrite_and_regeneration_restores_hashes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.run_script(root).returncode, 0)
            paths = tuple(root / path for path in projection_types.generated_files())
            original_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
            }

            schema = root / projection_types.SCHEMA_OUTPUT
            typescript = root / projection_types.TYPESCRIPT_OUTPUT
            schema.write_text(
                schema.read_text(encoding="utf-8").replace(
                    '"title": "WorkspaceProjection"',
                    '"title": "TamperedProjection"',
                    1,
                ),
                encoding="utf-8",
            )
            typescript.write_text(
                typescript.read_text(encoding="utf-8").replace(
                    "generatedAt: string;", "generatedAt: number;", 1
                ),
                encoding="utf-8",
            )
            tampered_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
            }
            self.assertNotEqual(tampered_hashes, original_hashes)

            rejected = self.run_script(root, "--check")

            self.assertEqual(rejected.returncode, 1)
            for relative_path in projection_types.generated_files():
                self.assertIn(relative_path.as_posix(), rejected.stdout)
            self.assertIn(projection_types.REGENERATE_COMMAND, rejected.stdout)
            self.assertEqual(
                {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
                tampered_hashes,
            )

            self.assertEqual(self.run_script(root).returncode, 0)
            self.assertEqual(
                {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
                original_hashes,
            )
            self.assertEqual(self.run_script(root, "--check").returncode, 0)

    @pytest.mark.integration
    def test_documented_check_command_runs_with_its_exact_checkout_environment(self) -> None:
        header_line = next(
            line
            for line in projection_types.typescript_text().splitlines()
            if line.startswith("// Drift check: ")
        )
        self.assertEqual(
            header_line.removeprefix("// Drift check: "), projection_types.CHECK_COMMAND
        )

        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONPATH"] = "mcp/src"

        completed = subprocess.run(
            [sys.executable, "scripts/sync-projection-types.py", "--check"],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("projection schema and TypeScript are current", completed.stdout)

    def test_project_git_gate_checks_generated_projection_files(self) -> None:
        gate = (REPO_ROOT / ".githooks" / "_gate.sh").read_text(encoding="utf-8")
        self.assertIn('"$py" scripts/sync-projection-types.py --check', gate)

    def test_full_pyright_resolves_the_standalone_generator_against_package_source(self) -> None:
        configuration = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        environments = configuration["tool"]["pyright"]["executionEnvironments"]

        self.assertIn(
            {"root": "scripts", "extraPaths": ["mcp/src", "mcp/test_support"]},
            environments,
        )

    def test_projection_provenance_claims_stay_true(self) -> None:
        forbidden = (
            r"\bno generator (?:exists|anywhere)\b",
            r"(?:types/)?projection\.ts.{0,50}\b(?:is|was|remains) "
            r"(?:a )?hand-maintained\b",
            r"\b(?:typescript )?mirror.{0,50}\b(?:is|was|remains) "
            r"(?:a )?hand-maintained\b",
            r"\bhand-maintained (?:typescript )?mirror\b",
            r"snapshot\.json.{0,80}\b(?:is|was) (?!not\b)(?:auto-)?generated\b",
            r"snapshot\.json.{0,120}\b(?:generated|derived) from (?:the )?pydantic\b",
        )
        for relative_path in PROJECTION_PROVENANCE_FILES:
            normalized = " ".join(
                (REPO_ROOT / relative_path).read_text(encoding="utf-8").lower().split()
            )
            with self.subTest(path=relative_path.as_posix()):
                for pattern in forbidden:
                    self.assertIsNone(re.search(pattern, normalized), pattern)

        for relative_path in DATACLASS_MIRROR_FILES:
            self.assertNotIn(relative_path, PROJECTION_PROVENANCE_FILES)
            self.assertIn(
                "dataclass-backed surface in lockstep by hand",
                (REPO_ROOT / relative_path).read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
