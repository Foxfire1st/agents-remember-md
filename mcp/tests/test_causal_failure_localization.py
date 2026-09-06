"""Pure contracts for owner preflights, exact causal nodes, and retry evidence."""

from __future__ import annotations

import ast
import asyncio
import multiprocessing
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from agents_remember.application.lifecycle.lifecycle_operation_worker import (
    terminal_operation_record,
)
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember_test_support.code_quality.causal_preflight import (
    PREFLIGHTS,
    PreflightSpec,
    _organizational_repair_record,
    evaluate_preflights,
)
from agents_remember_test_support.testing import causal_dependency, causal_failures
from agents_remember_test_support.testing.causal_dependency import (
    CausalNodeDependency,
    derive_causal_nodes,
)
from agents_remember_test_support.testing.causal_failures import (
    CAUSAL_REPORT_SCHEMA,
    FailureClass,
    execution_profile,
    load_causal_report,
    runtime_failure_record,
    write_causal_report,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE: dict[str, object] = {
    "tree": "a" * 40,
    "environmentId": "b" * 64,
    "attemptNonceSha256": "c" * 64,
}
DEPENDENT_NODES = (
    "mcp/tests/test_causal_failure_localization.py::"
    "CausalFailureLocalizationTests::test_terminal_operation_preserves_candidate_identity",
    "mcp/tests/test_causal_failure_localization.py::"
    "CausalFailureLocalizationTests::test_terminal_operation_preserves_canonical_repair",
    "mcp/tests/test_causal_failure_localization.py::"
    "CausalFailureLocalizationTests::test_terminal_operation_preserves_integration_authority",
)
DEPENDENT_NODE = DEPENDENT_NODES[1]
SAME_FILE_INDEPENDENT_NODE = (
    "mcp/tests/test_causal_failure_localization.py::"
    "CausalFailureLocalizationTests::test_observed_runtime_failures_retain_exact_retry_inputs"
)
UNRELATED_REVERSE_IMPORTER_NODE = (
    "mcp/tests/test_git_command.py::"
    "RunnerContractTests::test_stdin_is_devnull_unless_input_text_is_given"
)


class _RuntimeReport:
    nodeid = "mcp/tests/test_socket_case.py::test_disconnect"
    outcome = "failed"
    when = "call"
    duration = 1.25
    start = 10.0
    stop = 11.25
    worker_id = "gw3"

    def __init__(self) -> None:
        self.user_properties: list[tuple[str, object]] = [
            ("arFailureClass", FailureClass.PROCESS_ENVIRONMENT.value),
            ("arFailureFamilies", "async-runtime,socket-runtime"),
            (
                "arRetrySemantics",
                "repeat-exact-node-with-seed-worker-timing-and-process-topology",
            ),
            ("arRandomOrderSeed", "260824"),
            ("arProcessTopology", "xdist:gw3/4"),
        ]


class _Config:
    def __init__(self, report: Path) -> None:
        self.report = report

    def getoption(self, name: str, default: object = None) -> object:
        if name == causal_failures.CAUSAL_REPORT_OPTION:
            return self.report
        if name == "random_order_seed":
            return None
        return default


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.path = REPOSITORY_ROOT / nodeid.split("::", maxsplit=1)[0]
        self.user_properties: list[tuple[str, object]] = []
        self.markers: list[object] = []

    def add_marker(self, marker: object) -> None:
        self.markers.append(marker)


class CausalFailureLocalizationTests(unittest.TestCase):
    def test_real_lifecycle_owner_preflight_is_compatible(self) -> None:
        PREFLIGHTS[0].validator()

    def test_directly_imported_owner_class_attribute_call_is_observed(self) -> None:
        tree = ast.parse(
            "from owner.module import Service\n\ndef test_contract():\n    Service.verify()\n"
        )
        bindings = causal_dependency._owner_bindings(  # pyright: ignore[reportPrivateUsage]
            tree,
            "owner.module",
            "Service",
        )
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))

        self.assertTrue(
            causal_dependency._calls_owner(  # pyright: ignore[reportPrivateUsage]
                function,
                bindings,
            )
        )

    def test_terminal_operation_preserves_canonical_repair(self) -> None:
        record, transitioned = self._forced_terminal_transition()

        self.assertEqual(transitioned.result, record.result)

    def test_terminal_operation_preserves_candidate_identity(self) -> None:
        record, transitioned = self._forced_terminal_transition()

        self.assertEqual(transitioned.candidateState, record.candidateState)
        self.assertEqual(transitioned.candidateTree, record.candidateTree)
        self.assertEqual(transitioned.generation, record.generation)

    def test_terminal_operation_preserves_integration_authority(self) -> None:
        record, transitioned = self._forced_terminal_transition()

        self.assertEqual(transitioned.integrationAuthority, record.integrationAuthority)
        self.assertEqual(transitioned.organizationalRepair, record.organizationalRepair)

    def _forced_terminal_transition(
        self,
    ) -> tuple[LifecycleOperationRecord, LifecycleOperationRecord]:
        record = _organizational_repair_record()
        transitioned = terminal_operation_record(
            record,
            {"reason": "later downstream symptom"},
            ok=False,
            stamp="2026-08-25T00:00:01+00:00",
        )
        return record, transitioned

    def test_source_derivation_excludes_observer_and_independent_nodes(self) -> None:
        dependencies = _real_dependencies()

        self.assertEqual([item.node_id for item in dependencies], list(DEPENDENT_NODES))
        self.assertNotIn(
            SAME_FILE_INDEPENDENT_NODE,
            {item.node_id for item in dependencies},
        )
        self.assertNotIn(
            UNRELATED_REVERSE_IMPORTER_NODE,
            {item.node_id for item in dependencies},
        )
        for dependency in dependencies:
            self.assertEqual(
                dependency.dependency_chain,
                (
                    f"{PREFLIGHTS[0].owner.as_posix()}::{PREFLIGHTS[0].owner_symbol}",
                    "mcp/tests/test_causal_failure_localization.py::"
                    "CausalFailureLocalizationTests::_forced_terminal_transition",
                    dependency.node_id,
                ),
            )

    def test_failed_owner_blocks_only_source_proved_exact_nodes(self) -> None:
        payload = _failed_payload()

        self.assertEqual(payload["schemaVersion"], CAUSAL_REPORT_SCHEMA)
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["acceptanceEligible"])
        self.assertEqual(payload["firstCausalFailure"], PREFLIGHTS[0].cause_id)
        blocked = cast(list[dict[str, object]], payload["blockedGroups"])
        self.assertEqual([row["nodeId"] for row in blocked], list(DEPENDENT_NODES))
        self.assertEqual(blocked[0]["evidenceAltitude"], PREFLIGHTS[0].evidence_altitude)
        self.assertEqual(blocked[0]["correctiveOwner"], PREFLIGHTS[0].owner.as_posix())

    def test_collection_suppresses_one_exact_node_not_its_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "causal-failures.json"
            write_causal_report(path, _failed_payload())
            dependent = _Item(DEPENDENT_NODE)
            independent = _Item(SAME_FILE_INDEPENDENT_NODE)

            causal_failures.pytest_collection_modifyitems(
                cast(pytest.Config, _Config(path)),
                cast(list[pytest.Item], [dependent, independent]),
            )

        self.assertEqual(len(dependent.markers), 1)
        self.assertEqual(independent.markers, [])
        self.assertIn(
            ("arCausalCauseId", PREFLIGHTS[0].cause_id),
            dependent.user_properties,
        )
        self.assertNotIn(
            "arCausalCauseId",
            {name for name, _ in independent.user_properties},
        )

    def test_machine_and_human_artifacts_render_from_one_payload(self) -> None:
        payload = _failed_payload()
        payload["runtimeEvidence"] = {
            "pytestExitCode": 1,
            "blockedNodes": [{"nodeId": DEPENDENT_NODE, "causeId": PREFLIGHTS[0].cause_id}],
            "independentFailures": [runtime_failure_record(_RuntimeReport())],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "causal-failures.json"
            write_causal_report(path, payload)

            self.assertEqual(load_causal_report(path), payload)
            rendered = path.with_suffix(".md").read_text(encoding="utf-8")

        self.assertIn(f"First causal failure: `{PREFLIGHTS[0].cause_id}`", rendered)
        self.assertIn(f"Evidence altitude: `{PREFLIGHTS[0].evidence_altitude}`", rendered)
        self.assertIn(PREFLIGHTS[0].corrective_owner.as_posix(), rendered)
        for node_id in DEPENDENT_NODES:
            self.assertIn(node_id, rendered)
        self.assertIn("Exact dependency chain", rendered)
        self.assertIn(_RuntimeReport.nodeid, rendered)
        self.assertIn("not granted by this artifact", rendered)

    def test_observed_runtime_failures_retain_exact_retry_inputs(self) -> None:
        sensitive_profile = execution_profile(
            ExceptionGroup(
                "observed runtime failures",
                [ConnectionResetError(), TimeoutError(), PermissionError()],
            )
        )
        deterministic_profile = execution_profile(AssertionError("ordinary assertion"))
        unclassified_profile = execution_profile(None)

        self.assertEqual(sensitive_profile.failure_class, FailureClass.PROCESS_ENVIRONMENT)
        self.assertEqual(
            sensitive_profile.families,
            ("socket-runtime", "timeout-runtime", "environment-os-runtime"),
        )
        self.assertEqual(deterministic_profile.failure_class, FailureClass.INDEPENDENT)
        self.assertEqual(unclassified_profile.failure_class, FailureClass.UNCLASSIFIED)
        self.assertEqual(
            unclassified_profile.retry_semantics,
            "classify-observed-failure-before-retry",
        )

        record = runtime_failure_record(_RuntimeReport())
        self.assertEqual(record["workerId"], "gw3")
        self.assertEqual(record["randomOrderSeed"], "260824")
        self.assertEqual(record["durationSeconds"], 1.25)
        self.assertEqual(record["processTopology"], "xdist:gw3/4")
        self.assertEqual(
            record["retrySemantics"],
            "repeat-exact-node-with-seed-worker-timing-and-process-topology",
        )

    def test_runtime_failure_families_have_distinct_repair_owners(self) -> None:
        cases = (
            (asyncio.CancelledError(), "async-runtime"),
            (multiprocessing.ProcessError(), "multiprocessing-runtime"),
            (subprocess.SubprocessError(), "subprocess-runtime"),
            (ChildProcessError(), "process-runtime"),
            (ConnectionResetError(), "socket-runtime"),
            (TimeoutError(), "timeout-runtime"),
            (PermissionError(), "environment-os-runtime"),
        )

        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(execution_profile(error).families, (expected,))


def _real_dependencies() -> tuple[CausalNodeDependency, ...]:
    spec = PREFLIGHTS[0]
    return derive_causal_nodes(
        REPOSITORY_ROOT,
        owner=spec.owner,
        owner_module=spec.owner_module,
        owner_symbol=spec.owner_symbol,
    )


def _failed_payload() -> dict[str, object]:
    def fail() -> None:
        raise RuntimeError("forced incompatible owner contract")

    spec: PreflightSpec = replace(PREFLIGHTS[0], validator=fail)
    return evaluate_preflights((spec,), REPOSITORY_ROOT, candidate=CANDIDATE)


if __name__ == "__main__":
    unittest.main()
