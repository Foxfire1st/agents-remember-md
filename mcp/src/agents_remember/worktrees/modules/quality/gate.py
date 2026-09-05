"""Strict repository-profile quality enforcement for code commits."""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles import (
    AdmittedRepositoryProfile,
    DaggerModuleExecutorAdapter,
    JsonExitStatusDecoder,
    RepositoryExecutionRequest,
    compile_repository_profile_plan,
    load_repository_profile,
    resolve_repository_profile_selection,
)
from agents_remember.certification.repository_profiles.models import (
    ProfileMode,
    RepositoryProfileSelection,
)
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.models.quality import QualityGateResult
from agents_remember.models.test_evidence import (
    CertifyingTestEvidence,
    EvidenceConsumer,
    evidence_payload,
    require_certifying_evidence,
)
from agents_remember.worktrees.modules.git import require_git
from agents_remember.worktrees.modules.quality import certification_records
from agents_remember.worktrees.modules.quality.clean_executor import (
    CleanQualityOutcome,
    CleanQualityRequest,
    certifying_evidence_from_published_manifest,
    published_generation_root,
    published_report_path_from_manifest,
    run_clean_quality,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    PublishedQualityManifest,
    load_published_quality_manifest,
)

FAILURE_OUTPUT_LINES = 40
REPORT_DIRECTORY_NAME = "reports"
TEST_RESULTS_REPORT_NAME = "test-results.md"

GATE_ENFORCED = "enforced"
GATE_NO_CODE_COMMIT = "no-code-commit"
GATE_TARGETED = "targeted"
GATE_FULL = "full"


@dataclass(frozen=True)
class QualityGatePlan:
    """The requested repository-profile altitude plus an optional full-run cap."""

    mode: ProfileMode = GATE_TARGETED
    memory_cap_bytes: int | None = None


@dataclass(frozen=True)
class QualityGateTarget:
    """The exact repository context and enclosure selected for certification."""

    code_worktree: Path
    worktree_group: Path
    repository_id: str
    profile_reference: Path | None


@dataclass(frozen=True)
class _QualityGateReport:
    path: Path
    result: CleanQualityOutcome
    command: list[str]
    invocation: str
    mode: str
    executor_adapter_id: str
    profile_digest: str
    profile_plan_digest: str
    profile_selection_id: str
    runtime_authority_digest: str | None
    diff_base: str
    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float
    requested_memory_cap_bytes: int | None


def test_results_report_path(worktree_group: Path) -> Path:
    """Return the enclosure-owned report for the latest completed quality run."""
    return worktree_group / REPORT_DIRECTORY_NAME / TEST_RESULTS_REPORT_NAME


def _gate_command(
    target: QualityGateTarget,
    diff_base: str,
    *,
    mode: ProfileMode = GATE_TARGETED,
    memory_cap_bytes: int | None = None,
) -> str:
    """The command as reported, so a payload reader can rerun exactly what ran."""
    if mode not in {GATE_TARGETED, GATE_FULL}:
        raise ValueError(f"unknown quality gate mode: {mode}")
    return shlex.join(
        _profile_report_command(
            target,
            QualityGatePlan(
                mode=mode,
                memory_cap_bytes=memory_cap_bytes,
            ),
            diff_base,
        )
    )


def requires_strict_code_quality(
    target: QualityGateTarget,
    *,
    code_would_commit: bool,
) -> bool:
    """Require one valid configured profile whenever code would be committed."""

    if not code_would_commit:
        return False
    load_repository_profile(
        target.repository_id,
        target.code_worktree,
        target.profile_reference,
    )
    return True


def code_quality_gate_preview(
    target: QualityGateTarget,
    *,
    code_would_commit: bool,
    diff_base: str = "",
    plan: QualityGatePlan | None = None,
) -> dict[str, object]:
    """Preview the exact configured adapter, or fail profile admission before execution."""
    if not code_would_commit:
        return {
            "required": False,
            "status": GATE_NO_CODE_COMMIT,
            "command": "",
            "reason": "no code commit would be created",
        }
    plan = plan or QualityGatePlan()
    admitted, selection = _admitted_selection(target, plan.mode)
    memory_cap_payload: dict[str, object] = {}
    if plan.mode == GATE_FULL:
        memory_cap_payload = _memory_policy_payload(
            requested_cap_bytes=plan.memory_cap_bytes,
        )
    return {
        "required": True,
        "status": GATE_ENFORCED,
        "command": _gate_command(
            target,
            diff_base,
            mode=plan.mode,
            memory_cap_bytes=plan.memory_cap_bytes,
        ),
        "diffBase": diff_base,
        "mode": plan.mode,
        "executor": "dagger",
        "executorAdapterId": selection.executorAdapterId,
        "profileDigest": admitted.canonical.profileDigest,
        "profileSelectionId": selection.selectionId,
        "runtimeAuthorityDigest": None,
        **memory_cap_payload,
        "reason": (
            "closeout stages the whole task worktree, admits its exact repository-owned "
            "profile, and executes that profile's selected candidate-scoped adapter."
        ),
    }


def _validated_quality_gate_plan(plan: QualityGatePlan | None) -> QualityGatePlan:
    resolved = plan or QualityGatePlan()
    if resolved.mode not in {GATE_TARGETED, GATE_FULL}:
        raise ValueError(f"unknown quality gate mode: {resolved.mode}")
    return resolved


@dataclass(frozen=True)
class _GateRunReportInputs:
    """Run facts one completed strict gate report binds."""

    result: CleanQualityOutcome
    command: list[str]
    invocation: str
    plan: QualityGatePlan
    diff_base: str
    started_at: datetime
    started: float


def _publish_gate_test_report(
    report_path: Path,
    inputs: _GateRunReportInputs,
) -> None:
    """Write the one durable strict-gate report for a completed run."""
    manifest = inputs.result.manifest
    _write_test_results_report(
        _QualityGateReport(
            path=report_path,
            result=inputs.result,
            command=inputs.command,
            invocation=inputs.invocation,
            mode=inputs.plan.mode,
            executor_adapter_id=(
                manifest.executor_adapter_id if manifest is not None else "unpublished"
            ),
            profile_digest=(manifest.profile_digest if manifest is not None else "unpublished"),
            profile_plan_digest=(
                manifest.profile_plan_digest if manifest is not None else "unpublished"
            ),
            profile_selection_id=(
                manifest.profile_selection_id if manifest is not None else "unpublished"
            ),
            runtime_authority_digest=(
                manifest.runtime_authority_digest if manifest is not None else None
            ),
            diff_base=inputs.diff_base,
            started_at=inputs.started_at,
            finished_at=datetime.now(UTC),
            elapsed_seconds=time.monotonic() - inputs.started,
            requested_memory_cap_bytes=inputs.plan.memory_cap_bytes,
        )
    )


def run_strict_code_quality_gate(
    target: QualityGateTarget,
    *,
    diff_base: str = "",
    plan: QualityGatePlan | None = None,
    invocation: str = "closeout-staged",
    attestation: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Run the altitude-routed quality contract or refuse before the commit.

    ``diff_base`` must be the recorded base commit so the coverage floor measures
    this change set, not the whole integration branch. The wrapper certifies the
    index/working tree it is handed; on failure nothing is committed and closeout
    deliberately leaves its staging in place.
    """
    code_worktree = target.code_worktree
    plan = _validated_quality_gate_plan(plan)
    command, invocation = _gate_command_parts(target, plan, diff_base, invocation)
    candidate_tree = require_git(code_worktree, ["write-tree"])
    _freeze_certification_records(target, plan=plan, candidate_tree=candidate_tree)
    started_at = datetime.now(UTC)
    started = time.monotonic()
    result = run_clean_quality(
        CleanQualityRequest(
            code_worktree=code_worktree,
            worktree_group=target.worktree_group,
            repository_id=target.repository_id,
            profile_reference=target.profile_reference,
            mode=plan.mode,
            diff_base=diff_base,
            memory_cap_bytes=plan.memory_cap_bytes,
            attestation=attestation,
        )
    )
    if result.manifest is None and result.returncode == 0:
        raise RuntimeError("repository certification passed without a published manifest")
    report_path = test_results_report_path(target.worktree_group)
    _publish_gate_test_report(
        report_path,
        _GateRunReportInputs(
            result=result,
            command=command,
            invocation=invocation,
            plan=plan,
            diff_base=diff_base,
            started_at=started_at,
            started=started,
        ),
    )
    if result.returncode != 0:
        raise RuntimeError(
            _gate_failure_message(
                result,
                report_path,
                requested_memory_cap_bytes=plan.memory_cap_bytes,
            )
        )
    manifest = result.manifest
    if manifest is None:
        raise RuntimeError("repository certification passed without a published manifest")
    evidence = require_certifying_evidence(
        result.evidence,
        consumer=EvidenceConsumer.LIFECYCLE,
    )
    current_tree = require_git(code_worktree, ["write-tree"])
    if evidence.candidate_tree != candidate_tree or current_tree != candidate_tree:
        raise RuntimeError(
            "quality candidate changed while Dagger certification was being published"
        )
    _record_certification_generation(
        target,
        plan=plan,
        candidate_tree=candidate_tree,
        manifest=manifest,
    )
    return _strict_quality_success_payload(
        target,
        diff_base=diff_base,
        plan=plan,
        evidence=evidence,
        manifest=manifest,
    )


def recover_strict_code_quality_gate(
    target: QualityGateTarget,
    *,
    diff_base: str,
    plan: QualityGatePlan,
    attestation: Mapping[str, str],
) -> dict[str, object] | None:
    """Recover one exact passed Dagger generation after its caller crashed."""

    reports = target.worktree_group / REPORT_DIRECTORY_NAME
    try:
        manifest = load_published_quality_manifest(reports)
    except RuntimeError:
        return None
    published = None if manifest.attestation is None else dict(manifest.attestation)
    if published != dict(attestation):
        return None
    candidate_tree = require_git(target.code_worktree, ["write-tree"])
    admitted, selection = _admitted_selection(target, plan.mode)
    expected_plan = compile_repository_profile_plan(
        admitted.canonical,
        selection_id=selection.selectionId,
        candidate_identity=CandidateIdentity(kind="git-tree", value=candidate_tree),
    )
    expected_decoder = next(
        decoder
        for decoder in admitted.canonical.profile.resultDecoders
        if decoder.decoderId == selection.resultDecoderId
    )
    if (
        manifest.candidate_tree != candidate_tree
        or manifest.profile_digest != admitted.canonical.profileDigest
        or manifest.profile_plan_digest != expected_plan.planDigest
        or manifest.profile_selection_id != selection.selectionId
        or manifest.executor_adapter_id != selection.executorAdapterId
        or manifest.result_decoder != expected_decoder
    ):
        return None
    published_report_path_from_manifest(
        reports,
        manifest,
        manifest.result_decoder.artifactPath,
    )
    if (
        JsonExitStatusDecoder()
        .decode(
            manifest.result_decoder,
            published_generation_root(reports, manifest),
            manifest.files,
        )
        .exit_code
        != 0
    ):
        return None
    evidence = certifying_evidence_from_published_manifest(
        reports,
        manifest,
        candidate_tree=candidate_tree,
    )
    require_certifying_evidence(evidence, consumer=EvidenceConsumer.LIFECYCLE)
    _record_certification_generation(
        target,
        plan=plan,
        candidate_tree=candidate_tree,
        manifest=manifest,
    )
    return _strict_quality_success_payload(
        target,
        diff_base=diff_base,
        plan=plan,
        evidence=evidence,
        manifest=manifest,
    )


def _strict_quality_success_payload(
    target: QualityGateTarget,
    *,
    diff_base: str,
    plan: QualityGatePlan,
    evidence: CertifyingTestEvidence,
    manifest: PublishedQualityManifest,
) -> dict[str, object]:
    published_result_path = published_report_path_from_manifest(
        target.worktree_group / REPORT_DIRECTORY_NAME,
        manifest,
        manifest.result_decoder.artifactPath,
    )
    payload: dict[str, object] = {
        "required": True,
        "status": GATE_ENFORCED,
        "passed": True,
        "command": _gate_command(
            target,
            diff_base,
            mode=plan.mode,
            memory_cap_bytes=plan.memory_cap_bytes,
        ),
        "diffBase": diff_base,
        "mode": plan.mode,
        "executor": "dagger",
        "executorAdapterId": manifest.executor_adapter_id,
        "profileDigest": manifest.profile_digest,
        "profilePlanDigest": manifest.profile_plan_digest,
        "profileSelectionId": manifest.profile_selection_id,
        "runtimeAuthorityDigest": manifest.runtime_authority_digest,
        "resultArtifact": manifest.result_decoder.artifactPath,
        "certifyingTestEvidence": evidence_payload(evidence),
        "reportPath": test_results_report_path(target.worktree_group).as_posix(),
        "publishedResultPath": published_result_path.as_posix(),
        **(
            _memory_policy_payload(
                requested_cap_bytes=plan.memory_cap_bytes,
            )
            if plan.mode == GATE_FULL
            else {}
        ),
    }
    return QualityGateResult.model_validate(payload).model_dump(
        mode="json",
        exclude_none=True,
    )


def run_local_quality_diagnostic(
    target: QualityGateTarget,
    *,
    diff_base: str = "",
    plan: QualityGatePlan | None = None,
) -> NoReturn:
    """Refuse host quality execution; only the Dagger graph certifies acceptance."""
    del target, diff_base, plan
    raise RuntimeError(
        "host quality execution is forbidden; run acceptance through the pinned Dagger graph"
    )


def _freeze_certification_records(target, *, plan, candidate_tree) -> None:
    """Freeze the exact R21 admission authority before Gate-1 execution."""
    certification_records.prepare_certification_records(
        certification_records.CertificationRunTarget(
            repository_id=target.repository_id,
            code_worktree=target.code_worktree,
            profile_reference=target.profile_reference,
            worktree_group=target.worktree_group,
        ),
        mode=plan.mode,
        candidate_tree=candidate_tree,
    )


def _record_certification_generation(target, *, plan, candidate_tree, manifest) -> None:
    """Publish typed gate result/certificate records for one green generation."""
    prepared = certification_records.prepare_certification_records(
        certification_records.CertificationRunTarget(
            repository_id=target.repository_id,
            code_worktree=target.code_worktree,
            profile_reference=target.profile_reference,
            worktree_group=target.worktree_group,
        ),
        mode=plan.mode,
        candidate_tree=candidate_tree,
    )
    if prepared is None:
        return
    reports = target.worktree_group / REPORT_DIRECTORY_NAME
    try:
        artifact = published_report_path_from_manifest(
            reports,
            manifest,
            manifest.result_decoder.artifactPath,
        )
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "green repository certification generation has no readable decoder artifact"
        ) from error
    if isinstance(payload, dict):
        certification_records.record_published_generation(prepared, manifest, payload)


def _write_test_results_report(report: _QualityGateReport) -> None:
    """Atomically replace the one durable report for a completed strict gate run."""
    output = (report.result.stdout or "").rstrip()
    lines = [
        "# Strict Quality Test Results",
        "",
        f"- Status: **{'passed' if report.result.returncode == 0 else 'failed'}**",
        f"- Invocation: `{report.invocation}`",
        f"- Mode: `{report.mode}`",
        f"- Executor adapter: `{report.executor_adapter_id}`",
        f"- Repository profile digest: `{report.profile_digest}`",
        f"- Repository profile plan digest: `{report.profile_plan_digest}`",
        f"- Repository profile selection: `{report.profile_selection_id}`",
        f"- Runtime authority digest: `{report.runtime_authority_digest}`",
        f"- Diff base: `{report.diff_base or '(none)'}`",
        f"- Exit code: `{report.result.returncode}`",
        f"- Started: `{report.started_at.replace(microsecond=0).isoformat()}`",
        f"- Finished: `{report.finished_at.replace(microsecond=0).isoformat()}`",
        f"- Elapsed seconds: `{report.elapsed_seconds:.3f}`",
        f"- Command: `{shlex.join(report.command)}`",
    ]
    if report.requested_memory_cap_bytes is not None:
        lines.extend(
            [
                "- Memory-cap policy: `dagger-inner-wrapper`",
                f"- Memory-cap bytes: `{report.requested_memory_cap_bytes}`",
                "- Swap policy: `container-host-managed`",
            ]
        )
    elif report.mode == GATE_FULL:
        lines.extend(
            [
                "- Memory policy: `container-host-managed`",
                "- Swap policy: `container-host-managed`",
            ]
        )
    lines.extend(
        [
            "",
            (
                "This file contains the latest completed repository-profile quality run. "
                "The next completed run atomically replaces it; worktree cleanup "
                "removes it with the enclosure."
            ),
            "",
            "## Output",
            "",
        ]
    )
    if output:
        lines.extend(f"    {line}" for line in output.splitlines())
    else:
        lines.append("_No output was captured._")
    atomic_write_text(report.path, "\n".join(lines) + "\n")


def _gate_command_parts(
    target: QualityGateTarget,
    plan: QualityGatePlan,
    diff_base: str,
    invocation: str,
) -> tuple[list[str], str]:
    """The profile-declared symbolic command and its invocation label."""
    return (
        _profile_report_command(target, plan, diff_base),
        "master-integration" if plan.mode == GATE_FULL else invocation,
    )


def _admitted_selection(
    target: QualityGateTarget,
    mode: ProfileMode,
) -> tuple[AdmittedRepositoryProfile, RepositoryProfileSelection]:
    admitted = load_repository_profile(
        target.repository_id,
        target.code_worktree,
        target.profile_reference,
    )
    selection = resolve_repository_profile_selection(
        admitted.canonical,
        purpose="closeout",
        mode=mode,
    )
    return admitted, selection


def _profile_report_command(
    target: QualityGateTarget,
    plan: QualityGatePlan,
    diff_base: str,
) -> list[str]:
    admitted, selection = _admitted_selection(target, plan.mode)
    executor = next(
        definition
        for definition in admitted.canonical.profile.executorAdapters
        if definition.adapterId == selection.executorAdapterId
    )
    return list(
        DaggerModuleExecutorAdapter().command(
            executor,
            RepositoryExecutionRequest(
                candidate_source=Path("<exact-staged-candidate>"),
                repository_bundle=Path("<exact-git-ancestry-bundle>"),
                execution_manifest=Path("<exact-admission-manifest>"),
                mode=selection.mode,
                diff_base=diff_base,
                export_root=Path("<profile-report-export>"),
                memory_cap_bytes=plan.memory_cap_bytes,
            ),
        )
    )


def _memory_policy_payload(
    *,
    requested_cap_bytes: int | None = None,
) -> dict[str, object]:
    policy: dict[str, object] = {
        "mode": "container-host-managed" if requested_cap_bytes is None else "explicit-cap",
        "processPolicy": "profile-adapter-owned",
        "swap": "container-host-managed",
    }
    if requested_cap_bytes is None:
        return {"memoryPolicy": policy}
    return {
        "memoryPolicy": policy,
        "memoryCap": {
            "capBytes": requested_cap_bytes,
            "policy": "dagger-inner-wrapper",
            "mechanism": "container-wrapper",
        },
    }


def _gate_failure_message(
    result: CleanQualityOutcome,
    report_path: Path,
    *,
    requested_memory_cap_bytes: int | None = None,
) -> str:
    """One refusal message: nothing committed, plus Dagger cap policy when requested."""
    details = _failure_output(result.stdout or "")
    if requested_memory_cap_bytes is not None:
        killed = (
            " The Dagger container scope was killed by the memory cap."
            if result.returncode in (137, -9)
            else ""
        )
        details += (
            "\nfull gate memory policy: dagger-inner-wrapper; "
            f"cap={requested_memory_cap_bytes} bytes; exit code {result.returncode}."
            f"{killed}"
        )
    return (
        "strict code-quality gate failed before code commit"
        f" with exit code {result.returncode}; code, memory, and ledger remain uncommitted."
        f" Full output: {report_path.as_posix()}."
        f"{details}"
    )


def _failure_output(output: str) -> str:
    lines = output.strip().splitlines()
    if not lines:
        return ""
    return "\nQuality output tail:\n" + "\n".join(lines[-FAILURE_OUTPUT_LINES:])
