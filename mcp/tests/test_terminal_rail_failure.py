"""Fully standalone CCR-R20 typed terminal rail-failure propagation tests.

Every fixture builds its own published rail report (schema-3.1 quality manifest)
from canonical helpers; no certification-run, evidence-lifecycle, telemetry
stream, or Dagger artifact is shared with these tests.  The tests falsify the
typed census and envelope translation the detached lifecycle worker applies in
OperationRuntime.fail.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.application.lifecycle.terminal_rail_failure import (
    TERMINAL_RAIL_FAILURE_SCHEMA_VERSION,
    TERMINAL_RESULT_CLASSES,
    telemetry_terminal_facts,
    terminal_worker_failure_result,
    worker_failure_result_class,
)
from agents_remember.certification.repository_profiles.models import (
    JsonExitStatusDecoderDefinition,
)
from agents_remember.errors import (
    CertificationContractError,
    CertificationExecutorPrerequisiteError,
    CertificationProfileError,
)
from agents_remember.models.lifecycles.operation import (
    IntegrateOperationInput,
    IntegrationOperationAuthority,
    LifecycleOperationRecord,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    operation_projection,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_lifecycle_evidence,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    REPORT_SET_MANIFEST,
    PublishedQualityManifestError,
    load_published_quality_manifest,
    quality_generation_digest,
    quality_report_dependencies,
)

_ARTIFACT = "clean-quality-results.json"
_PROFILE = {
    "profileDigest": "0" * 64,
    "profilePlanDigest": "1" * 64,
    "profileSelectionId": "local-targeted",
    "executorAdapterId": "certifying-dagger",
}
_RUNTIME_AUTHORITY = "2" * 64


def _decoder() -> JsonExitStatusDecoderDefinition:
    return JsonExitStatusDecoderDefinition(
        decoderId="quality-terminal-result",
        artifactPath=_ARTIFACT,
        statusField="status",
        exitCodeField="exitCode",
        passedValue="passed",
        failedValue="failed",
        consumingGates=(1,),
    )


def publish_rail_report(
    reports_dir: Path,
    candidate_tree: str,
    payload: dict[str, Any] | None = None,
    *,
    payload_bytes: bytes | None = None,
    runtime_authority: str | None = None,
) -> None:
    """Publish one canonical schema-3.1 quality report set for a red/green run."""

    decoder = _decoder()
    profile_identity = {
        **_PROFILE,
        "resultDecoder": decoder.model_dump(mode="json"),
    }
    if payload_bytes is None:
        encoded = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
        payload_bytes = encoded.encode("utf-8")
    files = {
        _ARTIFACT: {
            "sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "size": len(payload_bytes),
        }
    }
    dependencies = quality_report_dependencies(
        candidate_tree,
        files,
        None,
        profile_identity,
        runtime_authority,
    )
    generation = quality_generation_digest(
        {
            "candidateTree": candidate_tree,
            **profile_identity,
            "files": files,
            "dependencies": dependencies.model_dump(mode="json"),
            "runtimeAuthorityDigest": runtime_authority,
        }
    )
    generation_root = reports_dir / ".quality-report-generations" / generation
    generation_root.mkdir(parents=True, exist_ok=True)
    (generation_root / _ARTIFACT).write_bytes(payload_bytes)
    pointer: dict[str, Any] = {
        "schemaVersion": "3.1",
        "generation": generation,
        "candidateTree": candidate_tree,
        **profile_identity,
        "files": files,
        "dependencies": dependencies.model_dump(mode="json"),
        "runtimeAuthorityDigest": runtime_authority,
    }
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / REPORT_SET_MANIFEST).write_text(
        json.dumps(pointer, sort_keys=True) + "\n",
        encoding="utf-8",
    )


_RAIL_FIELD_NAMES = {
    "code": "failureCode",
    "owner": "correctiveOwner",
    "blocked_by": "blockedBy",
    "exit_code": "exitCode",
}


def _rail(rail_id: str, version: str, status: str, **facts: Any) -> dict[str, Any]:
    """One typed rail catalog row; keyword facts map to canonical row keys."""

    row: dict[str, Any] = {
        "identity": {"railId": rail_id, "version": version},
        "posture": "enforcing",
        "status": status,
    }
    extra = facts.get("extra")
    for key, value in facts.items():
        if key == "extra":
            if isinstance(extra, dict):
                row.update(extra)
            continue
        if key in _RAIL_FIELD_NAMES:
            row[_RAIL_FIELD_NAMES[key]] = value
        else:
            row[key] = value
    return row


def _red_payload(*, failed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    failed_rows = failed or [
        _rail(
            "file-size",
            "1",
            "fail",
            code="CCR-P1-FILE-SIZE",
            owner="code-quality-owner",
            exit_code=3,
            extra={"detail": "a bounded failure detail"},
        )
    ]
    rows = [
        _rail("ruff", "1", "pass", exit_code=0),
        *failed_rows,
        _rail(
            "pytest",
            "1",
            "blocked",
            blocked_by=["file-size@1"],
            code="same-gate-prerequisite-failed",
        ),
        _rail(
            "diff-coverage",
            "1",
            "skipped",
            code="coverage-not-applicable",
            exit_code=0,
        ),
    ]
    for row in rows:
        row["gate"] = 1
    return {
        "status": "failed",
        "exitCode": 1,
        "attemptedSteps": ["ruff", "file-size", "pytest"],
        "completedSteps": ["ruff"],
        "failedStep": "file-size",
        "skippedSteps": [],
        "stepExitCodes": {"ruff": 0, "file-size": 3},
        "gates": [
            {
                "gate": 1,
                "applicability": "applicable",
                "started": True,
                "disposition": "red",
                "rails": rows,
            }
        ],
    }


def _tree(reports_dir: Path, candidate_tree: str) -> dict[str, Any]:
    del reports_dir
    return {
        "operation_kind": "closeout",
        "generation": 1,
        "candidate_tree": candidate_tree,
    }


def _envelope(**kwargs: Any) -> dict[str, Any]:
    """One terminal envelope, statically typed as the JSON dict tests inspect."""

    result = terminal_worker_failure_result(**kwargs)
    assert isinstance(result, dict)
    return result


class TestPublishedGateResult:
    def test_red_rail_facts_never_collapse_to_a_generic_worker_exception(
        self, tmp_path: Path
    ) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "a" * 40
        publish_rail_report(reports, candidate_tree, _red_payload())
        assert load_published_quality_manifest(reports).candidate_tree == candidate_tree
        error = RuntimeError(
            "strict code-quality gate failed before code commit with exit code 1; "
            "Full output: reports/test-results.md.\nQuality output tail:\n"
            "some raw pytest output line\n"
        )
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=error,
            reports_dir=reports,
        )
        assert envelope["terminalClass"] == "gate-result"
        assert worker_failure_result_class(envelope) == "gate-result"
        rails = envelope["rails"]
        assert isinstance(rails, dict)
        failed = rails["failed"]
        assert [row["railId"] for row in failed] == ["file-size"]
        file_size = failed[0]
        assert file_size["code"] == "CCR-P1-FILE-SIZE"
        assert file_size["owner"] == "code-quality-owner"
        assert file_size["version"] == "1"
        assert file_size["status"] == "fail"
        blocked = rails["blocked"]
        assert [row["railId"] for row in blocked] == ["pytest"]
        assert blocked[0]["blockedBy"] == ["file-size@1"]
        skipped = rails["skipped"]
        assert [row["railId"] for row in skipped] == ["diff-coverage"]
        assert skipped[0]["code"] == "coverage-not-applicable"
        assert "pass" not in {row["status"] for row in skipped}
        assert envelope["counts"] == {
            "passed": 1,
            "failed": 1,
            "blocked": 1,
            "skipped": 1,
            "notApplicable": 0,
        }
        report = envelope["report"]
        assert isinstance(report, dict)
        assert report["candidateTree"] == candidate_tree
        assert report["profilePlanDigest"] == "1" * 64
        terminal_id = envelope["terminalId"]
        assert isinstance(terminal_id, str) and len(terminal_id) == 64
        serialized = json.dumps(envelope)
        assert "raw pytest output line" not in serialized
        assert "operation worker failed before publishing a typed domain result" not in (serialized)
        assert envelope["terminalClass"] in TERMINAL_RESULT_CLASSES

    def test_all_independent_failed_rails_are_preserved(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "b" * 40
        failed = [
            _rail(
                "file-size",
                "1",
                "fail",
                code="CCR-P1-FILE-SIZE",
                owner="code-quality-owner",
                exit_code=3,
            ),
            _rail(
                "pyright",
                "1",
                "fail",
                code="CCR-P1-PYRIGHT",
                owner="code-quality-owner",
                exit_code=4,
            ),
            _rail(
                "evidence-lifecycle",
                "1",
                "fail",
                code="CCR-P1-EVIDENCE",
                owner="certification-owner",
                exit_code=5,
            ),
        ]
        publish_rail_report(reports, candidate_tree, _red_payload(failed=failed))
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        failed_rows = envelope["rails"]["failed"]
        assert [row["railId"] for row in failed_rows] == [
            "file-size",
            "pyright",
            "evidence-lifecycle",
        ]
        assert {row["code"] for row in failed_rows} == {
            "CCR-P1-FILE-SIZE",
            "CCR-P1-PYRIGHT",
            "CCR-P1-EVIDENCE",
        }

    def test_published_report_is_never_truncated_to_one_catalog(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "c" * 40
        payload = _red_payload()
        second_gate = {
            "gate": 3,
            "applicability": "applicable",
            "started": True,
            "disposition": "red",
            "rails": [
                _rail("crap", "1", "fail", code="CCR-P3-CRAP", exit_code=7),
            ],
        }
        second_gate["rails"][0]["gate"] = 3
        payload["gates"] = [*payload["gates"], second_gate]
        publish_rail_report(reports, candidate_tree, payload)
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        failed_rows = envelope["rails"]["failed"]
        assert [row["railId"] for row in failed_rows] == ["file-size", "crap"]
        assert [row["gate"] for row in failed_rows] == [1, 3]


class TestUnclassifiedAndUnavailable:
    def test_pre_rail_crash_is_typed_worker_execution_unclassified(self, tmp_path: Path) -> None:
        envelope = _envelope(
            **_tree(tmp_path, "d" * 40),
            error=RuntimeError("worker died before rails"),
            reports_dir=tmp_path / "absent-reports",
        )
        assert envelope["terminalClass"] == "worker-execution-unclassified"
        error = envelope["error"]
        assert isinstance(error, dict)
        assert error["type"] == "RuntimeError"
        assert error["stage"] == "worker-execution"
        assert envelope["expected"] == {
            "publishedRailReport": "current valid report",
            "candidateTree": "d" * 40,
        }
        assert envelope["observed"] == {"publishedRailReport": None}
        assert envelope["schemaVersion"] == TERMINAL_RAIL_FAILURE_SCHEMA_VERSION

    def test_profile_error_family_is_censused_not_collapsed(self) -> None:
        error = CertificationProfileError(
            "profile invalid",
            [{"code": "profile-rail-unknown", "path": "rails.0", "detail": "no such rail"}],
        )
        envelope = _envelope(
            **_tree(Path("/tmp/none"), "e" * 40),
            error=error,
            reports_dir=None,
        )
        assert envelope["terminalClass"] == "worker-execution-unclassified"
        census = envelope["error"]
        assert isinstance(census, dict)
        assert census["type"] == "CertificationProfileError"
        assert census["status"] == "certification-profile-invalid"
        assert census["stage"] == "profile-admission"
        assert census["findings"] == [
            {"code": "profile-rail-unknown", "path": "rails.0", "detail": "no such rail"}
        ]

    def test_executor_prerequisite_family_is_censused_not_collapsed(self) -> None:
        error = CertificationExecutorPrerequisiteError(
            "executor missing",
            [
                {
                    "code": "executor-prerequisite-unavailable",
                    "path": "executor",
                    "detail": "no pinned dagger executable",
                }
            ],
        )
        envelope = _envelope(
            **_tree(Path("/tmp/none"), "f" * 40),
            error=error,
            reports_dir=None,
        )
        assert envelope["terminalClass"] == "worker-execution-unclassified"
        census = envelope["error"]
        assert isinstance(census, dict)
        assert census["type"] == "CertificationExecutorPrerequisiteError"
        assert census["status"] == "certification-executor-prerequisite-failed"
        assert census["findings"][0]["code"] == "executor-prerequisite-unavailable"

    def test_candidate_mismatch_report_is_terminal_unavailable(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        published_tree = "1" * 40
        expected_tree = "2" * 40
        publish_rail_report(reports, published_tree, _red_payload())
        envelope = _envelope(
            **_tree(reports, expected_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        assert envelope["terminalClass"] == "terminal-rail-result-unavailable"
        assert envelope["expected"] == {"candidateTree": expected_tree}
        observed = envelope["observed"]
        assert isinstance(observed, dict)
        assert observed["candidateTree"] == published_tree
        assert "rails" not in envelope
        assert "counts" not in envelope
        assert isinstance(envelope["error"], dict)
        assert envelope["error"]["type"] == "RuntimeError"

    def test_unreadable_manifest_is_terminal_unavailable(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        reports.mkdir(parents=True)
        (reports / REPORT_SET_MANIFEST).write_text("{not json", encoding="utf-8")
        envelope = _envelope(
            **_tree(reports, "a" * 40),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        assert envelope["terminalClass"] == "terminal-rail-result-unavailable"
        assert envelope["reason"] == (
            "published rail report manifest is missing, unreadable, or invalid"
        )

    def test_missing_terminal_artifact_is_terminal_unavailable(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "b" * 40
        publish_rail_report(reports, candidate_tree, _red_payload())
        generation_dir = next((reports / ".quality-report-generations").iterdir())
        (generation_dir / _ARTIFACT).unlink()
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        assert envelope["terminalClass"] == "terminal-rail-result-unavailable"
        assert envelope["error"]["type"] == "RuntimeError"

    def test_malformed_terminal_artifact_is_terminal_unavailable(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "c" * 40
        publish_rail_report(
            reports,
            candidate_tree,
            payload_bytes=b"{not json",
        )
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        assert envelope["terminalClass"] == "terminal-rail-result-unavailable"
        observed = envelope["observed"]
        assert isinstance(observed, dict)
        assert observed["artifactPath"] == _ARTIFACT
        assert observed["artifactState"] == "undecodable"

    def test_contradictory_terminal_artifact_is_terminal_unavailable(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "d" * 40
        payload = _red_payload()
        payload["status"] = "passed"
        publish_rail_report(reports, candidate_tree, payload)
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        assert envelope["terminalClass"] == "terminal-rail-result-unavailable"


class TestBoundednessAndParity:
    def test_raw_log_and_secret_keys_never_enter_the_envelope(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "e" * 40
        payload = _red_payload()
        payload["gates"][0]["rails"].append(
            _rail(
                "leaky",
                "1",
                "fail",
                code="CCR-P1-LEAKY",
                extra={
                    "stdout": "raw pytest output",
                    "secret": "super-secret-value",
                    "transcript": "full session transcript",
                },
            )
        )
        payload["logs"] = {"dagger": "raw dagger log"}
        publish_rail_report(reports, candidate_tree, payload)
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer failure"),
            reports_dir=reports,
        )
        serialized = json.dumps(envelope)
        assert "super-secret-value" not in serialized
        assert "raw pytest output" not in serialized
        assert "full session transcript" not in serialized
        assert "raw dagger log" not in serialized
        leaked_row = envelope["rails"]["failed"][-1]
        assert "stdout" not in leaked_row
        assert "secret" not in leaked_row
        assert "transcript" not in leaked_row

    def test_log_tails_are_stripped_from_error_and_reason(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "f" * 40
        publish_rail_report(reports, candidate_tree, _red_payload())
        error = RuntimeError("outer failure\nQuality output tail:\nsecret-log-tail-line\n")
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=error,
            reports_dir=reports,
        )
        serialized = json.dumps(envelope)
        assert "secret-log-tail-line" not in serialized
        assert envelope["error"]["type"] == "RuntimeError"
        assert envelope["error"]["stage"] == "worker-execution"

    def test_telemetry_facts_reflect_the_same_terminal_identity(self) -> None:
        envelope = _envelope(
            operation_kind="closeout",
            generation=3,
            candidate_tree="a" * 40,
            error=RuntimeError("boom"),
        )
        facts = telemetry_terminal_facts(envelope)
        assert facts["terminalId"] == envelope["terminalId"]
        assert facts["terminalResultClass"] == "worker-execution-unclassified"
        assert "gateResultManifestId" not in facts
        with pytest.raises(ValueError):
            telemetry_terminal_facts({**envelope, "terminalClass": "made-up"})

    def test_gate_result_telemetry_view_carries_the_manifest_identity(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "9" * 40
        publish_rail_report(reports, candidate_tree, _red_payload())
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        facts = telemetry_terminal_facts(envelope)
        assert facts["terminalResultClass"] == "gate-result"
        assert facts["terminalId"] == envelope["terminalId"]
        assert facts["gateResultManifestId"] == envelope["gateResultManifestId"]
        assert facts["terminalResultClass"] in TERMINAL_RESULT_CLASSES

    def test_worker_failure_result_class_accepts_only_envelopes(self) -> None:
        assert worker_failure_result_class({"status": "failed"}) is None
        assert worker_failure_result_class(None) is None
        envelope = _envelope(
            operation_kind="closeout",
            generation=1,
            candidate_tree=None,
            error=RuntimeError("boom"),
        )
        assert worker_failure_result_class(envelope) == "worker-execution-unclassified"
        wrong = dict(envelope)
        wrong["schemaVersion"] = "other/v1"
        assert worker_failure_result_class(wrong) is None


class TestLifecycleJournalIntegration:
    def test_fail_records_typed_gate_result_in_the_journal(self, tmp_path: Path) -> None:
        candidate_tree = "5" * 40
        publish_rail_report(tmp_path / "worktree-group" / "reports", candidate_tree, _red_payload())
        store = _integrate_store(tmp_path, candidate_tree)
        runtime = lifecycle_operation_worker.OperationRuntime(store)
        runtime.fail(RuntimeError("outer worker failure after red rails"))

        current = store.read()
        assert current is not None
        assert current.status in {"failed", "input-required"}
        result = current.result
        assert isinstance(result, dict)
        assert result["terminalClass"] == "gate-result"
        assert result["candidateTree"] == candidate_tree
        assert result["operationKind"] == "integrate"
        assert worker_failure_result_class(result) == "gate-result"
        failed = result["rails"]["failed"]
        assert [row["railId"] for row in failed] == ["file-size"]
        assert "operation worker failed before publishing a typed domain result" not in (
            current.failure or ""
        )

    def test_status_and_wait_projection_expose_the_same_envelope(self, tmp_path: Path) -> None:
        candidate_tree = "6" * 40
        publish_rail_report(tmp_path / "worktree-group" / "reports", candidate_tree, _red_payload())
        store = _integrate_store(tmp_path, candidate_tree)
        runtime = lifecycle_operation_worker.OperationRuntime(store)
        runtime.fail(RuntimeError("outer worker failure"))

        current = store.read()
        assert current is not None and current.result is not None
        projection = operation_projection(current)
        assert projection.result == current.result
        assert projection.failure == current.failure
        assert projection.result is not None
        assert projection.result["terminalId"] == current.result["terminalId"]
        public = public_lifecycle_evidence(current.result)
        assert public == current.result


def _integrate_store(tmp_path: Path, candidate_tree: str) -> LifecycleOperationStore:
    """One validator-clean queued integrate journal without scheduling authority."""

    group = tmp_path / "worktree-group"
    record_path = operation_record_path(group, "integrate")
    record_path.parent.mkdir(parents=True)
    store = LifecycleOperationStore(record_path)
    contract_path = (group / "contract.md").as_posix()
    queued_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    record = LifecycleOperationRecord(
        taskId="l20-terminal-fixture",
        taskName="terminal-rail-failure-fixture",
        contractPath=contract_path,
        operationKind="integrate",
        candidateState="a" * 64,
        candidateTree=candidate_tree,
        fingerprint="b" * 64,
        operationKey="c" * 64,
        generation=1,
        recordRevision=1,
        integrationAuthority=IntegrationOperationAuthority(
            targetKind="atomic-integration",
            codeRepository="fixture-repo",
            codeSourceBranch="main",
            codeSourceRef="refs/heads/main",
            codeSourceCommit="8" * 40,
            codeCandidateCommit="9" * 40,
        ),
        input=IntegrateOperationInput(
            configPath=(group / "settings.json").as_posix(),
            contractPath=contract_path,
        ),
        status="queued",
        phase="queued",
        queuedAt=queued_at,
        reportPath=(group / "reports" / "closeout-operation.md").as_posix(),
    )
    store.create(record)
    return store


def _variant_rows() -> list[Any]:
    return [
        _rail("plain", "1", "pass", exit_code=0),
        {
            "key": "key-only@9",
            "status": "skipped",
            "failureCode": "scope-skip",
        },
        {
            "identity": {"id": "id-only", "version": "2"},
            "status": "fail",
            "failureCode": "CCR-ID-ONLY",
            "exitCode": 4,
        },
        {
            "rail": {"railId": "no-version"},
            "status": "pass",
            "exitCode": 0,
            "detail": "",
        },
        {"identity": 5, "status": "pass"},
        5,
        {},
        {"identity": {"railId": "mystery", "version": "1"}, "status": "mystery"},
        {
            "identity": {"railId": "blocked-r", "version": "1"},
            "status": "blocked",
            "blockedBy": [
                {"railId": "a", "version": "1"},
                {"id": "b"},
                {"kind": "selector"},
                "c@1",
                "",
                7,
            ],
            "failureCode": "same-gate-prerequisite-failed",
        },
        {
            "identity": {"railId": "rich", "version": "1"},
            "status": "fail",
            "failureCode": "CCR-RICH",
            "correctiveOwner": "certification-owner",
            "exitCode": 8,
            "resultDigest": "0" * 64,
            "detail": "detail-" + ("x" * 4000),
            "posture": "report-only",
            "evidence": [
                {
                    "evidenceId": "ev1",
                    "sha256": "a" * 64,
                    "size": 12,
                    "reference": "path/evidence.bin",
                    "stdout": "raw output must not copy",
                    "secret": "super-secret",
                },
                "plain",
                7,
                {"evidenceId": "ev2", "sha256": "b" * 64, "size": 0, "reference": ""},
                {"evidenceId": "long", "reference": "z" * 600},
                {"sha256": "c" * 64, "size": 1},
                {"evidenceId": "null-ref", "reference": None},
                {},
            ],
            "artifacts": [
                {
                    "artifactId": "art1",
                    "sha256": "d" * 64,
                    "size": 4,
                    "evidenceRef": "ref1",
                    "stdout": "raw",
                }
            ],
        },
        {
            "identity": {"railId": "na", "version": "1"},
            "posture": "report-only",
            "status": "not-applicable",
        },
        {
            "key": "both@5",
            "version": "9",
            "status": "pass",
            "exitCode": 0,
        },
        {
            "identity": {"railId": "bl-ro", "version": "1"},
            "posture": "report-only",
            "status": "blocked",
            "blockedBy": ["z@1"],
            "failureCode": "same-gate-prerequisite-failed",
        },
        {
            "identity": {"railId": "bl-na", "version": "1"},
            "status": "blocked",
            "blockedBy": "not-a-list",
            "failureCode": "same-gate-prerequisite-failed",
        },
    ]


def _variant_payload() -> dict[str, Any]:
    rows = _variant_rows()
    payload = {
        "status": "failed",
        "exitCode": 1,
        "failedStep": "rich",
        "gates": [
            17,
            {"gate": 2},
            {"gate": 1, "disposition": "red", "rails": rows},
        ],
    }
    return payload


class TestCatalogRowVariants:
    """Corner rails of published catalogs: identity, blockers, evidence rows."""

    def test_row_identity_blocker_and_evidence_variants(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "3" * 40
        payload = _variant_payload()
        publish_rail_report(reports, candidate_tree, payload)
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        assert envelope["terminalClass"] == "gate-result"
        rails = envelope["rails"]
        failed = rails["failed"]
        assert [row["railId"] for row in failed] == ["id-only", "rich"]
        assert failed[0].get("owner") is None
        assert failed[1]["owner"] == "certification-owner"
        assert failed[1]["posture"] == "report-only"
        assert len(failed[1]["detail"]) <= 2049
        assert failed[1]["detail"].endswith("…")
        assert failed[1]["resultDigest"] == "0" * 64
        blocked = rails["blocked"]
        assert [row["railId"] for row in blocked] == ["blocked-r", "bl-ro", "bl-na"]
        assert blocked[0]["blockedBy"] == ["a@1", "b", "c@1"]
        assert blocked[1]["blockedBy"] == ["z@1"]
        assert "blockedBy" not in blocked[2]
        skipped = rails["skipped"]
        assert [row["railId"] for row in skipped] == ["key-only"]
        assert skipped[0]["key"] == "key-only@9"
        assert envelope["counts"] == {
            "passed": 3,
            "failed": 2,
            "blocked": 3,
            "skipped": 1,
            "notApplicable": 1,
        }
        serialized = json.dumps(envelope)
        assert "raw output must not copy" not in serialized
        assert "super-secret" not in serialized
        evidence = failed[1]["evidence"]
        assert isinstance(evidence, list)
        ids = [item.get("evidenceId") for item in evidence if "evidenceId" in item]
        assert ids == ["ev1", "ev2", "long", "null-ref"]
        by_id = {item["evidenceId"]: item for item in evidence if "evidenceId" in item}
        assert len(by_id["long"]["reference"]) <= 513
        assert "reference" not in by_id["null-ref"]
        assert failed[1]["artifacts"][0]["artifactId"] == "art1"
        assert failed[1]["exitCode"] == 8

    def test_published_payload_without_gate_catalog_is_typed_empty(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "4" * 40
        payload = {"status": "failed", "exitCode": 1}
        publish_rail_report(reports, candidate_tree, payload)
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        assert envelope["terminalClass"] == "gate-result"
        assert envelope["rails"] == {"failed": [], "blocked": [], "skipped": []}
        assert envelope["counts"] == {
            "passed": 0,
            "failed": 0,
            "blocked": 0,
            "skipped": 0,
            "notApplicable": 0,
        }

    def test_green_report_is_typed_without_rail_failures(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "8" * 40
        rows = [_rail("ruff", "1", "pass", exit_code=0)]
        for row in rows:
            row["gate"] = 1
        payload = {
            "status": "passed",
            "exitCode": 0,
            "gates": [{"gate": 1, "disposition": "green", "rails": rows}],
        }
        publish_rail_report(reports, candidate_tree, payload)
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("post-publish worker failure"),
            reports_dir=reports,
        )
        assert envelope["terminalClass"] == "gate-result"
        assert envelope["rails"]["failed"] == []
        assert envelope["counts"]["passed"] == 1
        terminal_result = envelope["terminalResult"]
        assert isinstance(terminal_result, dict)
        assert terminal_result["status"] == "passed"
        assert terminal_result["exitCode"] == 0
        assert "failed after its rail report passed" in str(envelope["reason"])


class TestClassifierAndCensusEdges:
    def test_telemetry_and_classifier_refuse_invalid_facts(self) -> None:
        envelope = _envelope(
            operation_kind="closeout",
            generation=2,
            candidate_tree="9" * 40,
            error=RuntimeError("boom"),
        )
        with pytest.raises(ValueError):
            telemetry_terminal_facts({**envelope, "terminalId": "short"})
        with pytest.raises(ValueError):
            telemetry_terminal_facts({**envelope, "terminalId": 7})
        wrong_class = dict(envelope)
        wrong_class["terminalClass"] = "made-up"
        assert worker_failure_result_class(wrong_class) is None

    def test_generic_contract_and_publication_families_are_censused(self) -> None:
        base_error = CertificationContractError(
            "generic contract failure",
            [
                {"code": "base-code", "path": 7, "detail": "detail-" + ("y" * 600)},
                {"other": "unexpected-key"},
            ],
        )
        envelope = _envelope(
            **_tree(Path("/tmp/none"), "0" * 40),
            error=base_error,
        )
        census = envelope["error"]
        assert isinstance(census, dict)
        assert census["type"] == "CertificationContractError"
        assert census["status"] is None
        assert census["stage"] == "certification-contract"
        findings = census["findings"]
        assert findings[0]["path"] == 7
        assert len(findings[0]["detail"]) <= 513

        publication = PublishedQualityManifestError("no generation is published")
        envelope = _envelope(
            **_tree(Path("/tmp/none"), "0" * 40),
            error=publication,
        )
        census = envelope["error"]
        assert isinstance(census, dict)
        assert census["type"] == "PublishedQualityManifestError"
        assert census["status"] == "published-quality-manifest-invalid"
        assert census["stage"] == "report-publication"

    def test_fail_without_a_durable_record_keeps_the_generic_guard(self, tmp_path: Path) -> None:
        store = LifecycleOperationStore(
            tmp_path / "absent" / ".lifecycle" / "closeout-operation.json"
        )
        runtime = lifecycle_operation_worker.OperationRuntime(store)
        with pytest.raises(RuntimeError):
            runtime.fail(RuntimeError("no durable journal exists"))


class TestClassifierCoverageEdges:
    def test_gate_result_view_omits_an_absent_manifest(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "c" * 40
        publish_rail_report(reports, candidate_tree, _red_payload())
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        reduced = {key: value for key, value in envelope.items() if key != "gateResultManifestId"}
        facts = telemetry_terminal_facts(reduced)
        assert facts["terminalResultClass"] == "gate-result"
        assert "gateResultManifestId" not in facts


class TestRemainingBranchEdges:
    def test_red_reason_omits_an_absent_failed_step(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        candidate_tree = "d" * 40
        row = _rail(
            "file-size",
            "1",
            "fail",
            code="CCR-P1-FILE-SIZE",
            owner="code-quality-owner",
            exit_code=3,
        )
        row["gate"] = 1
        payload = {
            "status": "failed",
            "exitCode": 1,
            "gates": [{"gate": 1, "disposition": "red", "rails": [row]}],
        }
        publish_rail_report(reports, candidate_tree, payload)
        envelope = _envelope(
            **_tree(reports, candidate_tree),
            error=RuntimeError("outer worker failure"),
            reports_dir=reports,
        )
        reason = str(envelope["reason"])
        assert "exitCode=1" in reason
        assert "failedStep=" not in reason
