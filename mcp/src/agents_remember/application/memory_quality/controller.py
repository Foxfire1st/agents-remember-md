"""Typed sync/start/poll controller for repository-scoped memory quality."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agents_remember.application.memory_quality.runs import (
    QualityRunIdentity,
    QualityRunSnapshot,
    poll_quality_run,
    start_quality_run,
)
from agents_remember.application.memory_scope import (
    MemoryScope,
    MemoryScopeIdentity,
    resolve_memory_candidate_scope,
    resolve_memory_scope,
    revalidate_memory_candidate_scope,
)
from agents_remember.errors import (
    CuratorCoherenceError,
    MemoryCandidatePairError,
    MemoryCandidatePairFailure,
)
from agents_remember.kernel.authority import require_repo
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.kernel.route_index import build_route_indexes
from agents_remember.memory_quality.check import (
    DRIFT_CHECK_NAME,
    DriftCheckContext,
    normalize_checks,
    run_memory_quality_check,
)
from agents_remember.memory_quality.curator_checklist import (
    CuratorChecklist,
    split_commit_owned_findings,
    write_curator_checklist,
)
from agents_remember.memory_quality.final_certification import final_catalog_readiness
from agents_remember.memory_quality.final_certification.catalog import (
    ReadinessProjectionInput,
)
from agents_remember.memory_quality.integrity.check_missing_onboarding import (
    check_missing_onboarding,
)
from agents_remember.models.memory import (
    MemoryQualityPollRequest,
    MemoryQualityStartRequest,
    MemoryQualitySyncRequest,
)
from agents_remember.worktrees.integration.closeout.curator_coherence import (
    curator_coherence_paths,
    require_current_curator_coherence,
)
from agents_remember.worktrees.modules.git import worktree_candidate_tree

_CAPACITY_GUIDANCE = (
    "Poll an existing run or wait for active memory-quality work to finish, then submit "
    "this start request again."
)
_RUN_NOT_FOUND_GUIDANCE = (
    "The run was evicted, belongs to another repository, or the server restarted; "
    "submit a new start request with the original contract_path for a worktree candidate. "
    "Repository id alone is not candidate-pair authority."
)


@dataclass(frozen=True)
class MemoryQualityExecution:
    """One canonical execution and the result-affecting publication decision."""

    config: McpRuntimeConfig
    scope: MemoryScope
    checks: tuple[str, ...]
    detail_limit: int
    publish_curator_report: bool

    @property
    def identity(self) -> QualityRunIdentity:
        return QualityRunIdentity(
            repo_id=self.scope.repo_id,
            scope=self.scope.identity,
            checks=self.checks,
            detail_limit=self.detail_limit,
            publish_curator_report=self.publish_curator_report,
        )


@dataclass(frozen=True)
class _CuratorCandidateInputs:
    code_tree: str
    memory_tree: str


def run_memory_quality_request(
    config: McpRuntimeConfig,
    request: MemoryQualitySyncRequest,
) -> dict[str, object]:
    """Resolve and synchronously execute one explicit sync request."""

    try:
        execution = _resolve_execution(config, request)
    except MemoryCandidatePairError as error:
        return _pair_refusal(request.repo_id, error)
    return _execute_or_refuse(execution)


def start_memory_quality_request(
    config: McpRuntimeConfig,
    request: MemoryQualityStartRequest,
) -> dict[str, object]:
    """Resolve and admit one explicit async-start request."""

    try:
        execution = _resolve_execution(config, request)
    except MemoryCandidatePairError as error:
        return _pair_refusal(request.repo_id, error)
    admission = start_quality_run(
        execution.identity,
        lambda: _execute_or_refuse(execution),
    )
    if admission.state == "capacity-reached":
        return {
            "ok": False,
            "operation": "memory_quality_check",
            "repoId": execution.scope.repo_id,
            "status": "capacity-reached",
            "guidance": _CAPACITY_GUIDANCE,
            **_scope_projection(execution.scope.identity),
        }
    if admission.run_id is None:
        raise RuntimeError("memory-quality admission did not retain its run identity")
    return {
        "ok": True,
        "operation": "memory_quality_check",
        "repoId": execution.scope.repo_id,
        "status": admission.state,
        "runId": admission.run_id,
        **_scope_projection(execution.scope.identity),
    }


def poll_memory_quality_request(
    config: McpRuntimeConfig,
    request: MemoryQualityPollRequest,
) -> dict[str, object]:
    """Poll one run only through its configured canonical repository."""

    repo_id = require_repo(config, request.repo_id).repo_id
    snapshot = poll_quality_run(repo_id, request.run_id)
    if snapshot is None:
        return {
            "ok": False,
            "operation": "memory_quality_check",
            "repoId": repo_id,
            "status": "run-not-found",
            "runId": request.run_id,
            "guidance": _RUN_NOT_FOUND_GUIDANCE,
        }
    scope_identity = snapshot.identity.scope
    mismatch = _poll_scope_mismatch(request, scope_identity)
    if mismatch is not None:
        return mismatch
    if scope_identity.pair_identity is not None:
        try:
            current = resolve_memory_candidate_scope(
                config,
                repo_id=repo_id,
                contract_path=scope_identity.pair_identity.contractPath,
            )
            if current.pair_identity != scope_identity.pair_identity:
                raise MemoryCandidatePairError(
                    "memory-candidate-pair-stale",
                    "the polled result belongs to a code/memory pair that is no longer current",
                    failure=MemoryCandidatePairFailure(
                        field="pairIdentity",
                        contract_path=scope_identity.pair_identity.contractPath,
                        expected={
                            "pairIdentity": scope_identity.pair_identity.model_dump(mode="json")
                        },
                        observed={
                            "pairIdentity": (
                                None
                                if current.pair_identity is None
                                else current.pair_identity.model_dump(mode="json")
                            )
                        },
                        next_action="worktree_sync",
                        next_args={
                            "contract_path": scope_identity.pair_identity.contractPath,
                            "dry_run": True,
                        },
                    ),
                )
        except MemoryCandidatePairError as error:
            return {
                **_pair_refusal(repo_id, error),
                "runId": snapshot.run_id,
            }
    if snapshot.status != "completed":
        return _unfinished_poll_payload(repo_id, snapshot, scope_identity)
    result = dict(snapshot.result or {})
    if result.get("status") == "scope-refused":
        return {**result, "runId": snapshot.run_id}
    return {**result, "status": "completed", "runId": snapshot.run_id}


def _unfinished_poll_payload(
    repo_id: str,
    snapshot: QualityRunSnapshot,
    scope_identity: MemoryScopeIdentity,
) -> dict[str, object]:
    result: dict[str, object] = {
        "ok": True,
        "operation": "memory_quality_check",
        "repoId": repo_id,
        "status": snapshot.status,
        "runId": snapshot.run_id,
        **_scope_projection(scope_identity),
    }
    if snapshot.status == "failed":
        result["error"] = snapshot.error
    return result


def _execute_or_refuse(execution: MemoryQualityExecution) -> dict[str, object]:
    try:
        return _execute_memory_quality(execution)
    except MemoryCandidatePairError as error:
        return _pair_refusal(execution.scope.repo_id, error)


def _pair_refusal(repo_id: str, error: MemoryCandidatePairError) -> dict[str, object]:
    result: dict[str, object] = {
        "ok": False,
        "operation": "memory_quality_check",
        "repoId": repo_id,
        "status": "scope-refused",
        **error.response_fields(),
    }
    result["status"] = "scope-refused"
    return result


def _scope_projection(identity: MemoryScopeIdentity) -> dict[str, object]:
    pair = identity.pair_identity
    if pair is None:
        return {
            "scopeAuthority": "official-diagnostic",
            "acceptanceEligible": False,
        }
    return {
        "scopeAuthority": "leaf-candidate",
        "acceptanceEligible": True,
        "contractPath": pair.contractPath,
        "pairIdentity": pair.model_dump(mode="json"),
    }


def _poll_scope_mismatch(
    request: MemoryQualityPollRequest,
    identity: MemoryScopeIdentity,
) -> dict[str, object] | None:
    pair = identity.pair_identity
    requested = request.contract_path
    if pair is None and requested is None:
        return None
    if (
        pair is not None
        and requested is not None
        and Path(requested).resolve() == Path(pair.contractPath).resolve()
    ):
        return None
    contract_path = pair.contractPath if pair is not None else str(requested or "")
    error = MemoryCandidatePairError(
        "memory-candidate-pair-poll-scope-mismatch",
        "poll must repeat the exact scope of the admitted memory-quality run",
        failure=MemoryCandidatePairFailure(
            field="contractPath",
            contract_path=contract_path,
            expected={"contractPath": None if pair is None else pair.contractPath},
            observed={"contractPath": requested},
            next_action="memory_quality_check",
        ),
    )
    return {
        **_pair_refusal(request.repo_id, error),
        "runId": request.run_id,
    }


def _resolve_execution(
    config: McpRuntimeConfig,
    request: MemoryQualitySyncRequest | MemoryQualityStartRequest,
) -> MemoryQualityExecution:
    checks = tuple(sorted(set(normalize_checks(request.checks, include_integrity=True))))
    scope = (
        resolve_memory_scope(config, repo_id=request.repo_id, contract_path=None)
        if request.contract_path is None
        else resolve_memory_candidate_scope(
            config,
            repo_id=request.repo_id,
            contract_path=request.contract_path,
        )
    )
    return MemoryQualityExecution(
        config=config,
        scope=scope,
        checks=checks,
        detail_limit=request.detail_limit,
        publish_curator_report=scope.curator_report_path is not None and not request.checks,
    )


def _execute_memory_quality(execution: MemoryQualityExecution) -> dict[str, object]:
    scope = revalidate_memory_candidate_scope(execution.config, execution.scope)
    candidate_inputs = (
        _curator_candidate_inputs(scope) if execution.publish_curator_report else None
    )
    payload = run_memory_quality_check(
        scope.onboarding_root,
        checks=execution.checks,
        drift_context=DriftCheckContext(
            code_repository_root=scope.code_root,
            context=scope.context,
            detail_limit=execution.detail_limit,
            unstamped_code_commit=scope.unstamped_code_commit,
            report_path=(scope.curator_report_path if execution.publish_curator_report else None),
            include_rows=execution.publish_curator_report,
            write_report=not execution.publish_curator_report,
        ),
        include_report_only_findings=execution.publish_curator_report,
    )
    scope = revalidate_memory_candidate_scope(execution.config, scope)
    if candidate_inputs is not None:
        _require_same_curator_candidate(
            scope,
            expected=candidate_inputs,
            observed=_curator_candidate_inputs(scope),
        )
    response: dict[str, object] = {
        "operation": "memory_quality_check",
        "repoId": scope.repo_id,
        "onboardingRoot": scope.onboarding_root.as_posix(),
        **_scope_projection(scope.identity),
        **payload,
    }
    if not execution.publish_curator_report:
        return response
    _attach_curator_checklist(
        execution.config,
        scope,
        payload,
        response,
        candidate_inputs=candidate_inputs,
    )
    return response


def _attach_curator_checklist(
    config: McpRuntimeConfig,
    scope: MemoryScope,
    payload: dict[str, Any],
    response: dict[str, object],
    *,
    candidate_inputs: _CuratorCandidateInputs | None = None,
) -> None:
    checks = payload.get("checks")
    drift_result = checks.get(DRIFT_CHECK_NAME, {}) if isinstance(checks, dict) else {}
    drift_rows = drift_result.pop("rows", []) if isinstance(drift_result, dict) else []
    report_only = payload.pop("reportOnlyFindings", [])
    findings = payload.get("findings")
    style_findings = (
        [
            finding
            for finding in findings
            if isinstance(finding, dict) and finding.get("check") != DRIFT_CHECK_NAME
        ]
        if isinstance(findings, list)
        else []
    )
    repair_findings, commit_owned_findings = split_commit_owned_findings(
        style_findings,
        scope.onboarding_root,
    )
    missing_onboarding = check_missing_onboarding(
        code_repository_root=scope.code_root,
        onboarding_root=scope.onboarding_root,
        settings=scope.context.storage,
        code_repository_name=scope.context.code_repository_name,
    )
    route_indexes = build_route_indexes(
        code_root=scope.code_root,
        onboarding_root=scope.onboarding_root,
        repository=scope.context.code_repository_name,
        storage=scope.context.storage,
        dry_run=True,
    )
    scope = revalidate_memory_candidate_scope(config, scope)
    if scope.curator_report_path is None:
        raise RuntimeError("curator publication has no enclosure-local report path")
    if scope.pair_identity is None:
        raise RuntimeError("curator publication has no exact code/memory pair identity")
    if candidate_inputs is None:
        raise RuntimeError("curator publication has no exact candidate tree inputs")
    _require_same_curator_candidate(
        scope,
        expected=candidate_inputs,
        observed=_curator_candidate_inputs(scope),
    )
    checklist = write_curator_checklist(
        CuratorChecklist(
            report_path=scope.curator_report_path,
            repo_id=scope.repo_id,
            code_root=scope.code_root,
            onboarding_root=scope.onboarding_root,
            pair_identity=scope.pair_identity,
            code_candidate_tree=candidate_inputs.code_tree,
            memory_candidate_tree=candidate_inputs.memory_tree,
            quality=payload,
            repair_findings=repair_findings,
            commit_owned_findings=commit_owned_findings,
            missing_onboarding=missing_onboarding,
            stale_route_indexes=route_indexes.stale_indexes,
            drift_rows=drift_rows,
            report_only_findings=report_only,
        )
    )
    response.pop("reportOnlyFindings", None)
    response.update(checklist)
    _attach_coherence_readiness(scope, response)
    _attach_final_full_catalog(
        scope,
        response,
        candidate_inputs=candidate_inputs,
        missing_onboarding=missing_onboarding,
        stale_route_indexes=route_indexes.stale_indexes,
    )


def _attach_final_full_catalog(
    scope: MemoryScope,
    response: dict[str, object],
    *,
    candidate_inputs: _CuratorCandidateInputs,
    missing_onboarding: dict[str, Any],
    stale_route_indexes: list[str],
) -> None:
    """Project the deterministic complete Gate-5 catalog onto the full run result.

    The interactive full contract-scoped run is the execution surface of the final full
    memory-coherence certification (CCR-R08). It cannot hold the R21 Gate 1-4
    certificates or the R07 affected-closure plan, so the projection names the exact
    complete catalog population, every item's typed status, and the still-missing
    certification authorities without claiming certification eligibility.
    """

    coherence_status = str(
        response.get("coherenceStatus") or "not-evaluated-quality-action-required"
    )
    coherence_record_digest = response.get("coherenceRecordDigest")
    if scope.pair_identity is None:
        raise RuntimeError("final full catalog projection requires the exact pair identity")
    response["finalFullCatalog"] = final_catalog_readiness(
        ReadinessProjectionInput(
            executed_checks=_catalog_checks(response),
            missing_onboarding_count=int(missing_onboarding.get("missingCount", 0) or 0),
            stale_route_index_count=len(stale_route_indexes),
            coherence_status=coherence_status if coherence_status == "current" else None,
            coherence_record_digest=(
                str(coherence_record_digest) if coherence_record_digest else None
            ),
            candidate_pair_authority_digest=scope.pair_identity.contractDigest,
            affected_closure_plan_digest=None,
            memory_tree=candidate_inputs.memory_tree,
        )
    )


def _catalog_checks(response: dict[str, object]) -> dict[str, dict[str, Any]]:
    checks = response.get("checks")
    if isinstance(checks, dict):
        return {name: result for name, result in checks.items() if isinstance(result, dict)}
    return {}


def _curator_candidate_inputs(scope: MemoryScope) -> _CuratorCandidateInputs:
    """Capture both working trees without mutating either repository index."""

    if scope.pair_identity is None:
        raise RuntimeError("curator publication has no exact code/memory pair identity")
    memory_root = scope.onboarding_root.parent
    with (
        TemporaryDirectory(prefix=".memory-quality-code-") as code_temporary,
        TemporaryDirectory(prefix=".memory-quality-memory-") as memory_temporary,
    ):
        return _CuratorCandidateInputs(
            code_tree=worktree_candidate_tree(
                scope.code_root,
                Path(code_temporary) / "index",
            ),
            memory_tree=worktree_candidate_tree(
                memory_root,
                Path(memory_temporary) / "index",
            ),
        )


def _require_same_curator_candidate(
    scope: MemoryScope,
    *,
    expected: _CuratorCandidateInputs,
    observed: _CuratorCandidateInputs,
) -> None:
    if observed == expected:
        return
    pair = scope.pair_identity
    raise MemoryCandidatePairError(
        "memory-quality-candidate-changed",
        "the exact code or memory candidate changed while memory quality was running",
        failure=MemoryCandidatePairFailure(
            field="candidateTrees",
            contract_path="" if pair is None else pair.contractPath,
            expected={
                "codeCandidateTree": expected.code_tree,
                "memoryCandidateTree": expected.memory_tree,
            },
            observed={
                "codeCandidateTree": observed.code_tree,
                "memoryCandidateTree": observed.memory_tree,
            },
            next_action="memory_quality_check",
            next_args=(
                None
                if pair is None
                else {"repo_id": scope.repo_id, "contract_path": pair.contractPath}
            ),
        ),
    )


def _attach_coherence_readiness(scope: MemoryScope, response: dict[str, object]) -> None:
    """Join raw memory quality with the same authority validator closeout calls."""

    quality_status = str(response.get("checklistStatus", ""))
    response["qualityChecklistStatus"] = quality_status
    response["closeoutReady"] = False
    if scope.contract is None:
        raise RuntimeError("contract-scoped curator publication lost its leaf contract")
    response["coherenceCanonicalPath"] = curator_coherence_paths(
        scope.contract
    ).canonical.as_posix()
    if quality_status != "ready-for-closeout":
        response["coherenceStatus"] = "not-evaluated-quality-action-required"
        return
    try:
        validated = require_current_curator_coherence(scope.contract)
    except CuratorCoherenceError as exc:
        response["ok"] = False
        response["checklistStatus"] = "coherence-required"
        response["coherenceStatus"] = exc.status
        response["guidance"] = (
            "Memory repairs are complete, but closeout is not ready: publish or refresh the "
            "exact structured curator-coherence authority with curator_coherence, then validate it."
        )
        return
    response["coherenceStatus"] = "current"
    response["coherenceRecordDigest"] = validated.record_digest
    response["closeoutReady"] = True
