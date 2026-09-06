"""Forcing tests for complete durable-evidence ownership and lifecycle admission."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from agents_remember_test_support.testing.evidence_governance import LIFECYCLE_CATALOG_PATH
from agents_remember_test_support.testing.evidence_lifecycle import (
    CATALOG_SCHEMA,
    EvidenceLifecycleError,
    governed_artifact_paths,
    load_evidence_inventory,
)


@dataclass(frozen=True)
class _Artifact:
    kind: str = "fixture"
    authority: str = "internal-canonical"
    category: str = "unit-regression"
    fidelity: str = "in-process"
    cadence: str = "affected"
    lifetime: str = "permanent"
    replacement: str = "contract:owned-behavior"
    expires_after: str | None = None
    permanence_rationale: str | None = "Retained as the smallest owned proof."


class _Project:
    def __init__(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self._temporary = temporary
        self.root = Path(temporary.name)
        completed = subprocess.run(
            ["git", "init", "--quiet"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "synthetic Git init failed")
        self.write("pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = ['mcp/tests']\n")
        self.write("mcp/tests/test_owner.py", "def test_owner():\n    pass\n")

    def close(self) -> None:
        self._temporary.cleanup()

    def write(self, path: str, content: str = "{}\n") -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def catalog(
        self,
        path: str,
        *,
        spec: _Artifact | None = None,
        consumers: tuple[str, ...] = ("mcp/tests/test_owner.py",),
        large_fixture_bytes: int = 25_000,
    ) -> None:
        artifact = spec or _Artifact()
        self._write_observable_consumers(path, consumers)
        optional = ""
        if artifact.expires_after is not None:
            optional += f"expires_after = {artifact.expires_after}\n"
        if artifact.permanence_rationale is not None:
            optional += f'permanence_rationale = "{artifact.permanence_rationale}"\n'
        contract = ""
        if artifact.replacement.startswith("contract:"):
            identity = artifact.replacement.removeprefix("contract:")
            contract = f'''\n[[contract]]
id = "{identity}"
owner = "{path}"
evidence_node = "mcp/tests/test_owner.py::test_owner"
'''
        rendered_consumers = ", ".join(f'"{value}"' for value in consumers)
        content = f'''schema_version = "{CATALOG_SCHEMA}"
large_fixture_bytes = {large_fixture_bytes}
{contract}
[[artifact]]
path = "{path}"
kind = "{artifact.kind}"
authority = "{artifact.authority}"
owner = "test-owner"
category = "{artifact.category}"
fidelity = "{artifact.fidelity}"
cadence = "{artifact.cadence}"
source_version_or_generator = "test generator"
introduced_by = "test"
lifetime = "{artifact.lifetime}"
replacement_contract = "{artifact.replacement}"
consumer_scope = "exact"
consumers = [{rendered_consumers}]
{optional}'''
        self.write("mcp/tests/evidence-lifecycle.toml", content)

    def _write_observable_consumers(self, artifact: str, consumers: tuple[str, ...]) -> None:
        for consumer in consumers:
            self.write(
                consumer,
                "from pathlib import Path\n\n"
                f"ARTIFACT = {artifact!r}\n\n"
                "def test_owner():\n"
                "    Path(ARTIFACT).read_text(encoding='utf-8')\n",
            )


class EvidenceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = _Project()
        self.addCleanup(self.project.close)

    def test_new_fixture_or_ordinary_shared_support_without_metadata_is_refused(self) -> None:
        known = "mcp/tests/fixtures/known.json"
        self.project.write(known)
        self.project.write("mcp/tests/fixtures/new.json")
        ordinary_support = "mcp/tests/lifecycle_enclosure" + "_test_support.py"
        self.project.write(ordinary_support, "VALUE = 1\n")
        self.project.catalog(known)

        with self.assertRaises(EvidenceLifecycleError) as refusal:
            load_evidence_inventory(self.project.root)

        self.assertIn("mcp/tests/fixtures/new.json", str(refusal.exception))
        self.assertIn(ordinary_support, str(refusal.exception))

    def test_source_observed_missing_or_unsupported_consumers_are_refused(self) -> None:
        artifact = "mcp/tests/fixtures/known.json"
        self.project.write(artifact)
        consumers = ("mcp/tests/test_owner.py", "mcp/tests/test_second_owner.py")
        self.project.catalog(artifact, consumers=consumers)
        catalog = self.project.root / "mcp/tests/evidence-lifecycle.toml"
        catalog.write_text(
            catalog.read_text(encoding="utf-8").replace(
                ', "mcp/tests/test_second_owner.py"',
                "",
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(EvidenceLifecycleError, "missing=.*test_second_owner"):
            load_evidence_inventory(self.project.root)

        self.project.catalog(artifact)
        catalog.write_text(
            catalog.read_text(encoding="utf-8").replace(
                'consumers = ["mcp/tests/test_owner.py"]',
                'consumers = ["mcp/tests/test_owner.py", "mcp/tests/test_unrelated.py"]',
            ),
            encoding="utf-8",
        )
        self.project.write("mcp/tests/test_unrelated.py", "def test_unrelated():\n    pass\n")
        with self.assertRaisesRegex(EvidenceLifecycleError, "unsupported=.*test_unrelated"):
            load_evidence_inventory(self.project.root)

    def test_task_or_date_shaped_baseline_is_governed(self) -> None:
        known = "mcp/tests/fixtures/known.json"
        self.project.write(known)
        self.project.write("mcp/tests/test_260824_model_baseline.py", "VALUE = 1\n")
        self.project.catalog(known)

        with self.assertRaisesRegex(EvidenceLifecycleError, "test_260824_model_baseline.py"):
            load_evidence_inventory(self.project.root)

    def test_configured_size_threshold_governs_unknown_fixture_suffixes(self) -> None:
        known = "mcp/tests/fixtures/known.json"
        large_unknown = "mcp/tests/fixtures/provider-recording.capture"
        small_unknown = "mcp/tests/fixtures/tiny.capture"
        self.project.write(known)
        self.project.write(large_unknown, "x" * 32)
        self.project.write(small_unknown, "x" * 4)
        self.project.catalog(known, large_fixture_bytes=16)

        with self.assertRaisesRegex(EvidenceLifecycleError, large_unknown):
            load_evidence_inventory(self.project.root)

        self.project.catalog(known, large_fixture_bytes=64)
        inventory = load_evidence_inventory(self.project.root)
        self.assertEqual([item.path for item in inventory.artifacts], [known])
        self.assertNotIn(
            LIFECYCLE_CATALOG_PATH,
            governed_artifact_paths(self.project.root, large_fixture_bytes=1),
        )

    def test_stale_or_contradictory_metadata_is_refused(self) -> None:
        ghost = "mcp/tests/fixtures/ghost.json"
        self.project.catalog(
            ghost,
            spec=_Artifact(authority="internal-canonical", lifetime="versioned"),
        )

        with self.assertRaises(EvidenceLifecycleError) as refusal:
            load_evidence_inventory(self.project.root)

        message = str(refusal.exception)
        self.assertIn("cataloged artifact does not exist", message)
        self.assertIn("versioned evidence must have external authority", message)
        self.assertIn("outside governed evidence", message)

    def test_nonexistent_registered_contract_and_stale_contract_rows_are_refused(self) -> None:
        artifact = "mcp/tests/fixtures/known.json"
        self.project.write(artifact)
        self.project.catalog(artifact)
        catalog = self.project.root / "mcp/tests/evidence-lifecycle.toml"
        source = catalog.read_text(encoding="utf-8")
        catalog.write_text(
            source.replace('owner = "mcp/tests/fixtures/known.json"', 'owner = "missing.py"'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(EvidenceLifecycleError, "contract owner path does not exist"):
            load_evidence_inventory(self.project.root)

        catalog.write_text(
            source.replace(
                'replacement_contract = "contract:owned-behavior"',
                'replacement_contract = "node:mcp/tests/test_owner.py::test_owner"',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(EvidenceLifecycleError, "unreferenced identities"):
            load_evidence_inventory(self.project.root)

    def test_temporary_evidence_requires_a_real_executable_replacement(self) -> None:
        artifact = "mcp/tests/fixtures/migration.json"
        self.project.write(artifact)
        self.project.catalog(
            artifact,
            spec=_Artifact(
                category="migration",
                cadence="migration-window",
                lifetime="temporary",
                replacement="contract:future-proof",
                expires_after="2026-09-30",
                permanence_rationale=None,
            ),
        )

        with self.assertRaisesRegex(EvidenceLifecycleError, "requires an executable node"):
            load_evidence_inventory(self.project.root, today=date(2026, 8, 24))

        for replacement, message in (
            ("node:mcp/tests/missing.py::test_replacement", "replacement node path does not exist"),
            ("node:mcp/tests/test_owner.py::test_missing", "selector does not exist"),
        ):
            self.project.catalog(
                artifact,
                spec=_Artifact(
                    category="migration",
                    fidelity="transition-comparison",
                    cadence="migration-window",
                    lifetime="temporary",
                    replacement=replacement,
                    expires_after="2026-09-30",
                    permanence_rationale=None,
                ),
            )
            with (
                self.subTest(replacement=replacement),
                self.assertRaisesRegex(
                    EvidenceLifecycleError,
                    message,
                ),
            ):
                load_evidence_inventory(self.project.root, today=date(2026, 8, 24))

    def test_expired_migration_fails_even_when_its_replacement_exists(self) -> None:
        artifact = "mcp/tests/fixtures/migration.json"
        self.project.write(artifact)
        self.project.catalog(
            artifact,
            spec=_Artifact(
                category="migration",
                fidelity="transition-comparison",
                cadence="migration-window",
                lifetime="temporary",
                replacement="node:mcp/tests/test_owner.py::test_owner",
                expires_after="2026-08-23",
                permanence_rationale=None,
            ),
        )

        with self.assertRaisesRegex(EvidenceLifecycleError, "evidence expired on 2026-08-23"):
            load_evidence_inventory(self.project.root, today=date(2026, 8, 24))

    def test_future_migration_with_an_existing_replacement_is_valid(self) -> None:
        artifact = "mcp/tests/fixtures/migration.json"
        self.project.write(artifact)
        self.project.catalog(
            artifact,
            spec=_Artifact(
                category="migration",
                fidelity="transition-comparison",
                cadence="migration-window",
                lifetime="temporary",
                replacement="node:mcp/tests/test_owner.py::test_owner",
                expires_after="2026-09-30",
                permanence_rationale=None,
            ),
        )

        inventory = load_evidence_inventory(self.project.root, today=date(2026, 8, 24))
        self.assertEqual([item.path for item in inventory.artifacts], [artifact])


if __name__ == "__main__":
    unittest.main()
