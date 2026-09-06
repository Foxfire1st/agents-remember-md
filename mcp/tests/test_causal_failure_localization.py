"""Pure contracts for owner preflights, exact causal nodes, and retry evidence."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

from agents_remember_test_support.code_quality.causal_preflight import (
    PREFLIGHTS,
    PreflightSpec,
    evaluate_preflights,
)
from agents_remember_test_support.testing.causal_failures import (
    CAUSAL_REPORT_SCHEMA,
    FailureClass,
    execution_profile,
    runtime_failure_record,
)

CANDIDATE: dict[str, object] = {
    "tree": "a" * 40,
    "environmentId": "b" * 64,
    "attemptNonceSha256": "c" * 64,
}


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


class CausalFailureLocalizationTests(unittest.TestCase):
    def test_failed_owner_blocks_only_source_proved_exact_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = _failed_payload(Path(temporary))

        self.assertEqual(payload["schemaVersion"], CAUSAL_REPORT_SCHEMA)
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["acceptanceEligible"])
        self.assertEqual(payload["firstCausalFailure"], PREFLIGHTS[0].cause_id)
        blocked = cast(list[dict[str, object]], payload["blockedGroups"])
        self.assertEqual(
            [row["nodeId"] for row in blocked], ["tests/test_operation.py::test_dependent"]
        )
        self.assertEqual(blocked[0]["evidenceAltitude"], PREFLIGHTS[0].evidence_altitude)
        self.assertEqual(blocked[0]["correctiveOwner"], "pkg/owner.py")

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


def _failed_payload(root: Path) -> dict[str, object]:
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, stdin=subprocess.DEVNULL)
    (root / "pkg").mkdir()
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    (root / "pkg/__init__.py").write_text("", encoding="utf-8")
    (root / "pkg/owner.py").write_text("def operation(): return 1\n", encoding="utf-8")
    (root / "tests/test_operation.py").write_text(
        "from pkg.owner import operation\n"
        "def test_dependent(): assert operation() == 1\n"
        "def test_independent(): assert 2 + 2 == 4\n",
        encoding="utf-8",
    )

    def fail() -> None:
        raise RuntimeError("forced incompatible owner contract")

    spec: PreflightSpec = replace(
        PREFLIGHTS[0],
        validator=fail,
        owner=Path("pkg/owner.py"),
        owner_module="pkg.owner",
        owner_symbol="operation",
        corrective_owner=Path("pkg/owner.py"),
    )
    return evaluate_preflights((spec,), root, candidate=CANDIDATE)


if __name__ == "__main__":
    unittest.main()
