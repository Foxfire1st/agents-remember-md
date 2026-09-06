"""Focused publication and quality-gate branch proofs for repository profiles."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest
from agents_remember.worktrees.integration.integration_quality import (
    IntegrationQualityFailure,
)
from agents_remember.worktrees.modules import integrate as integrate_mod
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.quality import clean_executor, published_manifest
from agents_remember.worktrees.modules.quality import gate as gate_mod
from agents_remember.worktrees.modules.quality.clean_executor import CleanQualityOutcome
from gate_certification_test_support import _green_outcome_factory, _lane_for
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    agents_remember_profile_execution,
    write_source_selection_artifacts,
)
from test_worktree_closeout_quality_gate import _checkout_with_profile, _quality_target
from test_worktree_support import git

_CANDIDATE = "c" * 40


def _valid_manifest(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "clean-quality-results.json").write_text(
        json.dumps({"status": "passed", "exitCode": 0}) + "\n",
        encoding="utf-8",
    )
    execution = agents_remember_profile_execution(candidate_tree=_CANDIDATE)
    write_source_selection_artifacts(source, execution.plan)
    return clean_executor._publish_reports(
        source,
        tmp_path / "reports",
        candidate_tree=_CANDIDATE,
        profile_execution=execution,
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("invalid-decoder", "result decoder is invalid"),
        ("decoder-file-absent", "names no published file"),
        ("generation-drift", "generation id does not match"),
        ("files-not-object", "files must be an object"),
        ("nonstring-file-name", "file names must be strings"),
        ("bad-profile-digest", "profileDigest is invalid"),
        ("blank-selection", "profileSelectionId is invalid"),
    ),
)
def test_manifest_parser_refuses_each_uncovered_bound_field(
    tmp_path: Path,
    case: str,
    expected: str,
) -> None:
    manifest = _valid_manifest(tmp_path)
    if case == "invalid-decoder":
        manifest["resultDecoder"] = {}
    elif case == "decoder-file-absent":
        decoder = manifest["resultDecoder"]
        assert isinstance(decoder, dict)
        files = manifest["files"]
        assert isinstance(files, dict)
        files.pop(str(decoder["artifactPath"]))
    elif case == "generation-drift":
        manifest["generation"] = "0" * 64
    elif case == "files-not-object":
        manifest["files"] = []
    elif case == "nonstring-file-name":
        files = manifest["files"]
        assert isinstance(files, dict)
        files[1] = {"sha256": "0" * 64, "size": 0}
    elif case == "bad-profile-digest":
        manifest["profileDigest"] = "not-a-digest"
    else:
        manifest["profileSelectionId"] = " "

    with pytest.raises(ValueError, match=expected):
        published_manifest.parse_published_quality_manifest(manifest)


def test_manifest_digest_and_dependency_helpers_require_exact_field_sets(
    tmp_path: Path,
) -> None:
    manifest = _valid_manifest(tmp_path)
    with pytest.raises(ValueError, match="fields are incomplete"):
        published_manifest.quality_generation_digest({"candidateTree": _CANDIDATE})

    files = manifest["files"]
    assert isinstance(files, dict)
    with pytest.raises(ValueError, match="dependency fields are incomplete"):
        published_manifest.quality_report_dependencies(
            _CANDIDATE,
            files,
            None,
            {"profileDigest": manifest["profileDigest"]},
        )


def test_report_publication_refuses_oversized_and_missing_required_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    result = source / "clean-quality-results.json"
    result.write_text('{"status":"passed","exitCode":0}\n', encoding="utf-8")
    execution = agents_remember_profile_execution(candidate_tree=_CANDIDATE)
    artifacts = tuple(
        artifact.model_copy(update={"maxBytes": 1}) if artifact.path == result.name else artifact
        for artifact in execution.published_artifacts
    )
    with pytest.raises(RuntimeError, match="declared size limits"):
        clean_executor._validated_export_inventory(source, artifacts)

    required = next(artifact for artifact in artifacts if artifact.path == result.name)
    with pytest.raises(RuntimeError, match="omitted required profile artifacts"):
        clean_executor._require_pass_publications(set(), (required,))


def test_strict_gate_refuses_success_without_a_manifest_before_and_after_reporting(
    tmp_path: Path,
) -> None:
    worktree = _checkout_with_profile(tmp_path / "code")
    git(worktree, "add", "-A")
    diff_base = git(worktree, "rev-parse", "HEAD")
    target = _quality_target(worktree, tmp_path / "enclosure")
    missing = CleanQualityOutcome(
        subprocess.CompletedProcess(["dagger"], 0, stdout="passed\n"),
        None,
        None,
    )
    with (
        mock.patch.object(gate_mod, "run_clean_quality", return_value=missing),
        pytest.raises(RuntimeError, match="passed without a published manifest"),
    ):
        gate_mod.run_strict_code_quality_gate(target, diff_base=diff_base)

    _admitted, lane, candidate_tree = _lane_for(worktree, diff_base=diff_base)
    publish = _green_outcome_factory(target.worktree_group, lane, candidate_tree)
    flapping = SimpleNamespace(returncode=0, manifest=None, evidence=None)

    def _published_outcome(request):
        outcome = publish(request)
        flapping.manifest = outcome.manifest
        flapping.evidence = outcome.evidence
        return flapping

    def _drop_manifest(_report) -> None:
        flapping.manifest = None

    with (
        mock.patch.object(gate_mod, "run_clean_quality", side_effect=_published_outcome),
        mock.patch.object(gate_mod, "_write_test_results_report", side_effect=_drop_manifest),
        pytest.raises(RuntimeError, match="passed without a published manifest"),
    ):
        gate_mod.run_strict_code_quality_gate(target, diff_base=diff_base)


def test_recovery_refuses_a_typed_published_failed_terminal_result(tmp_path: Path) -> None:
    worktree = _checkout_with_profile(tmp_path / "code")
    target = _quality_target(worktree, tmp_path / "enclosure")
    candidate_tree = subprocess.run(
        ["git", "write-tree"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    source = tmp_path / "failed-source"
    source.mkdir()
    (source / "clean-quality-results.json").write_text(
        json.dumps({"status": "failed", "exitCode": 1}) + "\n",
        encoding="utf-8",
    )
    attestation = {"operation": "failed-recovery-proof"}
    clean_executor._publish_reports(
        source,
        target.worktree_group / "reports",
        candidate_tree=candidate_tree,
        profile_execution=agents_remember_profile_execution(
            candidate_tree=candidate_tree,
            mode="targeted",
            repository_root=worktree,
        ),
        bindings=clean_executor.ReportBindings(
            attestation=attestation, runtime_authority_digest=None
        ),
    )

    manifest = clean_executor.load_published_quality_manifest(target.worktree_group / "reports")
    with pytest.raises(RuntimeError, match="did not pass acceptance"):
        clean_executor.certifying_evidence_from_published_manifest(
            target.worktree_group / "reports",
            manifest,
            candidate_tree=candidate_tree,
        )


@pytest.mark.parametrize("with_progress", (False, True))
def test_organizational_quality_failure_records_repair_only_with_progress(
    tmp_path: Path,
    with_progress: bool,
) -> None:
    contract = cast(Any, SimpleNamespace())
    progress = (lambda _phase, _evidence: None) if with_progress else None
    args = WorktreeArgs(
        certification_profile=AGENTS_REMEMBER_PROFILE_REFERENCE,
        operation_key="a" * 64,
        operation_generation=1,
        operation_progress=progress,
    )
    failure = IntegrationQualityFailure(
        stage="integration-quality-execution",
        error_type="RuntimeError",
        organizational_completion=True,
    )
    with (
        mock.patch.object(
            integrate_mod,
            "run_integration_quality_gate",
            side_effect=failure,
        ),
        mock.patch.object(
            integrate_mod,
            "organizational_quality_failure_payload",
            return_value={"state": "blocked-quality-gate"},
        ),
        mock.patch.object(integrate_mod, "record_organizational_completion_repair") as record,
    ):
        result, blocked = integrate_mod._run_integration_quality_gate(
            contract,
            completion=SimpleNamespace(),
            args=args,
        )

    assert result == {}
    assert blocked == {"state": "blocked-quality-gate"}
    assert record.call_count == int(with_progress)
