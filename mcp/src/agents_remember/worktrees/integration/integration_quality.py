"""Altitude-aware acceptance for atomic and organizational integration candidates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agents_remember.errors import CertificationContractError
from agents_remember.kernel.agentic_settings import load_agentic_settings
from agents_remember.models.lifecycles.operation import IntegrationQualityCertification
from agents_remember.models.test_evidence import EvidenceConsumer
from agents_remember.worktrees.integration.integration_quality_checkout import (
    integration_quality_checkout,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)
from agents_remember.worktrees.integration.organizational_completion import (
    OrganizationalCompletionPlan,
)
from agents_remember.worktrees.integration.organizational_completion_integration import (
    preview_organizational_completion,
)
from agents_remember.worktrees.modules.git import require_git
from agents_remember.worktrees.modules.quality.clean_executor import (
    require_published_quality_evidence,
)
from agents_remember.worktrees.modules.quality.gate import (
    GATE_FULL,
    QualityGatePlan,
    QualityGateTarget,
    code_quality_gate_preview,
    recover_strict_code_quality_gate,
    requires_strict_code_quality,
    run_strict_code_quality_gate,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract

INTEGRATION_QUALITY_DECISION_SURFACE = (
    "The required integration quality gate failed for the exact integration candidate."
)


class IntegrationQualityFailure(RuntimeError):
    """The exact integration candidate failed its required acceptance."""

    def __init__(
        self,
        *,
        stage: str,
        error_type: str,
        organizational_completion: bool,
    ) -> None:
        self.organizational_completion = organizational_completion
        self.evidence = public_failure_evidence(
            stage=stage,
            side="quality-gate",
            name="integration-quality",
            error_type=error_type,
            expected={"state": "accepted"},
            observed={"state": "failed"},
        )
        super().__init__("the required integration quality gate failed")


def integration_quality_failure(
    error: Exception,
    *,
    stage: str,
    organizational_completion: bool,
) -> IntegrationQualityFailure:
    """Classify a private backend cause into the one stable public quality vocabulary."""

    return IntegrationQualityFailure(
        stage=stage,
        error_type=type(error).__name__,
        organizational_completion=organizational_completion,
    )


@dataclass(frozen=True)
class IntegrationQualityOutcome:
    result: dict[str, object]
    certification: IntegrationQualityCertification | None = None


@dataclass(frozen=True)
class _IntegrationGateExecution:
    result: dict[str, object]
    recovered: bool
    attestation: dict[str, str] | None


def quality_gate_mode(contract: WorktreeContract) -> str:
    """Return the accepting mode for a branch-owning master integration."""

    if contract.kind == "leaf":
        raise ValueError("leaf integration reuses the exact leaf-closeout acceptance")
    return GATE_FULL


def quality_gate_preview(
    contract: WorktreeContract,
    *,
    profile_reference: Path | None,
) -> dict[str, object]:
    completion = preview_organizational_completion(contract) if contract.kind == "leaf" else None
    if contract.kind == "leaf" and completion is None:
        return _leaf_closeout_certification()
    mode = GATE_FULL
    settings = quality_gate_settings(contract)
    with integration_quality_checkout(contract, commit=contract.code_commit) as checkout:
        preview = code_quality_gate_preview(
            QualityGateTarget(
                code_worktree=checkout,
                worktree_group=contract.worktree_group,
                repository_id=contract.repo_name,
                profile_reference=profile_reference,
            ),
            code_would_commit=True,
            diff_base=contract.code_base_commit,
            plan=QualityGatePlan(
                mode=mode,
                memory_cap_bytes=settings.memory_cap_bytes,
            ),
        )
    if completion is not None:
        preview["scope"] = "organizational-master-completion"
        preview["completionFingerprint"] = completion.fingerprint
        preview["masterTaskDocumentRef"] = completion.master_ref.model_dump(mode="json")
    return preview


def run_integration_quality_gate(
    contract: WorktreeContract,
    *,
    completion: OrganizationalCompletionPlan | None = None,
    certification: IntegrationQualityCertification | None = None,
    certification_sink: Callable[[IntegrationQualityCertification], None] | None = None,
    profile_reference: Path | None,
) -> IntegrationQualityOutcome:
    """Run or reuse the one exact full integration gate.

    Ordinary leaf integration consumes its targeted closeout certification. A final
    organizational leaf and an atomic series use a detached checkout of the exact commit.
    Only the organizational result is persisted for crash-safe reuse because its gate and
    logical-master publication occur inside the journal-owned organizational landing
    transaction.
    """

    if contract.kind == "leaf" and completion is None:
        return IntegrationQualityOutcome(_leaf_closeout_certification())
    settings = quality_gate_settings(contract)
    plan = QualityGatePlan(
        mode=GATE_FULL,
        memory_cap_bytes=settings.memory_cap_bytes,
    )
    if completion is not None and certification is not None:
        try:
            _require_matching_certification(contract, completion, certification, plan=plan)
        except RuntimeError as error:
            raise integration_quality_failure(
                error,
                stage="integration-quality-certification",
                organizational_completion=True,
            ) from error
        return IntegrationQualityOutcome(
            {**certification.result, "reusedCertification": True},
            certification,
        )
    try:
        execution = _execute_integration_gate(
            contract,
            completion=completion,
            plan=plan,
            profile_reference=profile_reference,
        )
    except (CertificationContractError, RuntimeError) as error:
        raise integration_quality_failure(
            error,
            stage="integration-quality-execution",
            organizational_completion=completion is not None,
        ) from error
    if completion is None or execution.attestation is None:
        return IntegrationQualityOutcome(execution.result)
    gate = execution.result
    if execution.recovered:
        gate = {**gate, "recoveredPublishedReport": True}
    certificate = _certification(completion, gate, attestation=execution.attestation)
    if certification_sink is not None:
        certification_sink(certificate)
    return IntegrationQualityOutcome(gate, certificate)


def _execute_integration_gate(
    contract: WorktreeContract,
    *,
    completion: OrganizationalCompletionPlan | None,
    plan: QualityGatePlan,
    profile_reference: Path | None,
) -> _IntegrationGateExecution:
    """Run or recover the gate against one detached exact-candidate checkout."""

    with integration_quality_checkout(contract, commit=contract.code_commit) as checkout:
        target = QualityGateTarget(
            code_worktree=checkout,
            worktree_group=contract.worktree_group,
            repository_id=contract.repo_name,
            profile_reference=profile_reference,
        )
        if not requires_strict_code_quality(
            target,
            code_would_commit=True,
        ):
            preview = code_quality_gate_preview(
                target,
                code_would_commit=True,
                diff_base=contract.code_base_commit,
                plan=plan,
            )
            return _IntegrationGateExecution(preview, False, None)
        attestation = (
            _quality_attestation(completion, contract, plan) if completion is not None else None
        )
        recovered = (
            recover_strict_code_quality_gate(
                target,
                diff_base=contract.code_base_commit,
                plan=plan,
                attestation=attestation,
            )
            if attestation is not None
            else None
        )
        gate = recovered
        if gate is None:
            gate = run_strict_code_quality_gate(
                target,
                diff_base=contract.code_base_commit,
                plan=plan,
                invocation="master-integration",
                attestation=attestation,
            )
        require_published_quality_evidence(
            contract.worktree_group / "reports",
            candidate_tree=require_git(checkout, ["write-tree"]),
            consumer=EvidenceConsumer.INTEGRATION,
        )
    return _IntegrationGateExecution(gate, recovered is not None, attestation)


def _leaf_closeout_certification() -> dict[str, object]:
    return {
        "required": False,
        "status": "certified-at-leaf-closeout",
        "command": "",
        "mode": "targeted",
        "reason": (
            "leaf integration lands the exact commit certified once at leaf closeout; "
            "integration does not rerun acceptance"
        ),
    }


def quality_gate_settings(contract: WorktreeContract):
    settings = load_agentic_settings(
        contract.coordination_root,
        repo_root=contract.code_repo_path,
    )
    return settings.quality_gate


def organizational_quality_failure_payload(
    contract: WorktreeContract,
    failure: IntegrationQualityFailure,
    *,
    expected_generation: int,
) -> dict[str, object]:
    """Return the repair handoff for a failed exact final-leaf gate."""

    summary = (
        "The exact proposed organizational-master completion failed its full quality gate. "
        "No sprint-super ref moved. Cancel this pre-boundary integration to reopen the same "
        "leaf, repair it there, then declare and close the leaf again."
    )
    cancel_note = (
        "Cancel the failed organizational completion so its journal authority is released "
        "and the same leaf contract and claimed door are reset for repair."
    )
    if expected_generation <= 0:
        return {
            "state": "organizational-completion-gate-planning-failed",
            "reason": "the required integration quality gate failed",
            "failureEvidence": failure.evidence,
            "summary": summary,
            "developerDecisionRequired": True,
            "decisionSurface": INTEGRATION_QUALITY_DECISION_SURFACE,
            "safeToReplace": False,
            "superRefsMoved": False,
            "nextOperation": "admit_integration_before_cancellation",
        }
    cancel_args = {
        "contract_path": contract.contract_path.as_posix(),
        "operation_kind": "integrate",
        "action": "cancel",
        "expected_generation": expected_generation,
        "intent_note": cancel_note,
        "dry_run": False,
    }
    return {
        "state": "organizational-completion-gate-failed",
        "reason": "the required integration quality gate failed",
        "failureEvidence": failure.evidence,
        "summary": summary,
        "developerDecisionRequired": True,
        "decisionSurface": INTEGRATION_QUALITY_DECISION_SURFACE,
        "safeToReplace": False,
        "superRefsMoved": False,
        "nextOperation": "cancel_failed_completion_for_leaf_repair",
        "nextTool": "worktree_operation_control",
        "nextArgs": {**cancel_args, "dry_run": True},
        "applyStep": {
            "summary": cancel_note,
            "nextOperation": "cancel_failed_completion_for_leaf_repair",
            "nextTool": "worktree_operation_control",
            "nextArgs": cancel_args,
        },
    }


def _certification(
    completion: OrganizationalCompletionPlan,
    result: dict[str, object],
    *,
    attestation: dict[str, str],
) -> IntegrationQualityCertification:
    payload = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return IntegrationQualityCertification(
        completionFingerprint=completion.fingerprint,
        codeCommit=completion.code_commit,
        candidateTree=completion.code_tree,
        attestation=attestation,
        resultSha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        result=result,
    )


def _quality_attestation(
    completion: OrganizationalCompletionPlan,
    contract: WorktreeContract,
    plan: QualityGatePlan,
) -> dict[str, str]:
    return {
        "kind": "organizational-master-completion",
        "completionFingerprint": completion.fingerprint,
        "codeCommit": completion.code_commit,
        "candidateTree": completion.code_tree,
        "diffBase": contract.code_base_commit,
        "mode": plan.mode,
        "executor": "dagger",
        "memoryCapBytes": "" if plan.memory_cap_bytes is None else str(plan.memory_cap_bytes),
    }


def _require_matching_certification(
    contract: WorktreeContract,
    completion: OrganizationalCompletionPlan,
    certification: IntegrationQualityCertification,
    *,
    plan: QualityGatePlan,
) -> None:
    expected_attestation = _quality_attestation(completion, contract, plan)
    try:
        validated = IntegrationQualityCertification.model_validate(
            certification.model_dump(mode="json")
        )
    except ValueError as error:
        raise RuntimeError(
            "recorded organizational full-gate certification is not an exact Dagger result"
        ) from error
    observed = (
        validated.completionFingerprint,
        validated.codeCommit,
        validated.candidateTree,
        validated.attestation,
    )
    expected = (
        completion.fingerprint,
        contract.code_commit,
        completion.code_tree,
        expected_attestation,
    )
    if observed != expected:
        raise RuntimeError(
            "recorded organizational full-gate certification targets another candidate or "
            "does not match the current Dagger quality plan"
        )
