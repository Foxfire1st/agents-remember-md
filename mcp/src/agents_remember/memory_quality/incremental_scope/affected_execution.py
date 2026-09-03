"""Execute and aggregate only the exact incremental subset admitted by R07."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, cast

from pydantic import JsonValue

from agents_remember.memory_quality.style.citations import range_resolution, source_index

from .affected_models import (
    AffectedClosurePlan,
    AffectedClosureResult,
    AffectedMemberPlan,
    AffectedMemberResult,
    AffectedUnitPlan,
    AffectedUnitResult,
    SubresultReusePlan,
    UnitStatus,
)
from .compiler import ScopeAuthority
from .errors import GateFiveClosureRefusedError, ScopeFailure
from .execution_registry import checker_execution_registry_version
from .models import canonical_digest


class IncrementalCheckerExecutor(Protocol):
    """One runner bound to the exact R07 execution registry."""

    @property
    def registry_version(self) -> str: ...

    def execute(
        self,
        plan: AffectedClosurePlan,
        unit: AffectedUnitPlan,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class AffectedClosureExecution:
    candidateAuthority: ScopeAuthority
    checkerExecutor: IncrementalCheckerExecutor


@dataclass(frozen=True)
class RangeResolutionExecutionContext:
    codeRoot: Path
    memoryRoot: Path
    onboardingRoot: Path
    citationIndex: source_index.RepositoryIndex


@dataclass(frozen=True)
class _CheckerObservation:
    status: UnitStatus
    code: str
    files_checked: int
    finding_count: int


class RangeResolutionAffectedExecutor:
    """Run the sole currently proven selected-document memory checker."""

    def __init__(self, context: RangeResolutionExecutionContext) -> None:
        self._context = context

    @property
    def registry_version(self) -> str:
        return checker_execution_registry_version()

    def execute(
        self,
        plan: AffectedClosurePlan,
        unit: AffectedUnitPlan,
    ) -> Mapping[str, object]:
        self._validate_context(plan, unit)
        return range_resolution.check_onboarding_root(
            self._context.onboardingRoot,
            self._context.codeRoot,
            only=unit.document,
            index=self._context.citationIndex,
        )

    def _validate_context(
        self,
        plan: AffectedClosurePlan,
        unit: AffectedUnitPlan,
    ) -> None:
        roots = (
            self._context.codeRoot.resolve().as_posix(),
            self._context.memoryRoot.resolve().as_posix(),
            self._context.onboardingRoot.resolve().as_posix(),
        )
        if roots != (plan.codeRoot, plan.memoryRoot, plan.onboardingRoot):
            _refuse(
                "checker-execution-root-mismatch",
                "incremental checker roots differ from the exact affected-closure plan",
                checker=unit.checker,
                node=unit.node.nodeId,
            )
        if unit.checker != range_resolution.CHECK_NAME:
            _refuse(
                "checker-executor-unknown",
                "selected unit has no registered incremental executor",
                checker=unit.checker,
            )
        if self._context.citationIndex.snapshot_id != unit.sourceIndexSnapshot:
            _refuse(
                "checker-source-index-stale",
                "incremental checker lease differs from the unit source-index generation",
                checker=unit.checker,
                snapshot=unit.sourceIndexSnapshot,
            )


def plan_affected_subresult_reuse(
    plan: AffectedClosurePlan,
    prior_results: Sequence[AffectedUnitResult],
) -> SubresultReusePlan:
    """Select only byte-identical passing units supplied by exact result identity."""

    plan = _validated_plan(plan)
    prior_results = _validated_prior_results(prior_results)
    planned = {item.unitDigest: item for item in plan.units}
    observed: dict[str, AffectedUnitResult] = {}
    ignored: set[str] = set()
    for result in prior_results:
        unit_digest = result.unit.unitDigest
        previous = observed.get(unit_digest)
        if previous is not None and previous != result:
            _refuse(
                "subresult-authority-conflict",
                "two different prior results claim the same exact affected unit",
                checker=result.unit.checker,
                node=result.unit.node.nodeId,
            )
        observed[unit_digest] = result
        if unit_digest not in planned or result.status != "pass":
            ignored.add(result.resultDigest)
    reused = tuple(
        sorted(
            unit_digest
            for unit_digest, unit in planned.items()
            if (prior := observed.get(unit_digest)) is not None
            and prior.status == "pass"
            and prior.unit == unit
        )
    )
    execute = tuple(sorted(set(planned) - set(reused)))
    payload = {
        "schemaVersion": "memory-affected-subresult-reuse/v1",
        "closurePlanDigest": plan.planDigest,
        "reusedUnitDigests": list(reused),
        "unitsToExecute": list(execute),
        "ignoredPriorResultDigests": sorted(ignored),
    }
    return SubresultReusePlan(
        closurePlanDigest=plan.planDigest,
        reusedUnitDigests=reused,
        unitsToExecute=execute,
        ignoredPriorResultDigests=tuple(sorted(ignored)),
        reusePlanDigest=canonical_digest(payload),
    )


def execute_affected_closure(
    context: AffectedClosureExecution,
    plan: AffectedClosurePlan,
    prior_results: Sequence[AffectedUnitResult] = (),
) -> AffectedClosureResult:
    """Execute missing exact units, reuse valid ones, and publish a complete aggregate."""

    _require_current_candidate(context.candidateAuthority, plan)
    if context.checkerExecutor.registry_version != plan.executionRegistryVersion:
        _refuse(
            "checker-execution-registry-stale",
            "incremental checker executor differs from the admitted execution registry",
            candidate=plan.candidateDigest,
        )
    reuse = plan_affected_subresult_reuse(plan, prior_results)
    prior = {
        item.unit.unitDigest: item
        for item in prior_results
        if item.unit.unitDigest in set(reuse.reusedUnitDigests)
    }
    subresults: list[AffectedUnitResult] = []
    for unit in plan.units:
        if unit.unitDigest in prior:
            subresults.append(prior[unit.unitDigest])
            continue
        try:
            evidence = context.checkerExecutor.execute(plan, unit)
        except GateFiveClosureRefusedError:
            raise
        except Exception as error:
            _refuse(
                "checker-execution-failed",
                f"incremental checker failed before publishing a result: {type(error).__name__}",
                checker=unit.checker,
                node=unit.node.nodeId,
            )
        subresults.append(_unit_result(unit, evidence))
    _require_current_candidate(context.candidateAuthority, plan)
    ordered = tuple(subresults)
    member_results = _member_results(plan.members, ordered)
    terminal = _aggregate_status(ordered)
    payload = {
        "schemaVersion": "memory-affected-closure-result/v1",
        "plan": plan.model_dump(mode="json", by_alias=True),
        "subresults": [item.model_dump(mode="json", by_alias=True) for item in ordered],
        "memberResults": [item.model_dump(mode="json", by_alias=True) for item in member_results],
        "reusedUnitDigests": list(reuse.reusedUnitDigests),
        "executedUnitDigests": list(reuse.unitsToExecute),
        "pendingFinalFull": [
            item.model_dump(mode="json", by_alias=True) for item in plan.pendingFinalFull
        ],
        "terminalStatus": terminal,
        "incrementalMemoryReady": terminal == "pass",
        "closeoutReady": False,
        "acceptanceEligible": False,
        "fullFinalRequired": True,
    }
    return AffectedClosureResult(
        plan=plan,
        subresults=ordered,
        memberResults=member_results,
        reusedUnitDigests=reuse.reusedUnitDigests,
        executedUnitDigests=reuse.unitsToExecute,
        pendingFinalFull=plan.pendingFinalFull,
        terminalStatus=terminal,
        incrementalMemoryReady=terminal == "pass",
        resultDigest=canonical_digest(payload),
    )


def _unit_result(
    unit: AffectedUnitPlan,
    raw_evidence: Mapping[str, object],
) -> AffectedUnitResult:
    evidence = _canonical_evidence(raw_evidence, unit)
    _require_result_identity(evidence, unit)
    observation = _checker_observation(evidence, unit)
    evidence_digest = canonical_digest(evidence)
    payload = {
        "schemaVersion": "memory-affected-unit-result/v1",
        "unit": unit.model_dump(mode="json", by_alias=True),
        "status": observation.status,
        "code": observation.code,
        "correctiveOwner": unit.correctiveOwner,
        "filesChecked": observation.files_checked,
        "findingCount": observation.finding_count,
        "evidence": evidence,
        "evidenceDigest": evidence_digest,
    }
    return AffectedUnitResult(
        unit=unit,
        status=observation.status,
        code=observation.code,
        correctiveOwner=unit.correctiveOwner,
        filesChecked=observation.files_checked,
        findingCount=observation.finding_count,
        evidence=evidence,
        evidenceDigest=evidence_digest,
        resultDigest=canonical_digest(payload),
    )


def _require_result_identity(
    evidence: Mapping[str, JsonValue],
    unit: AffectedUnitPlan,
) -> None:
    if evidence.get("check") != unit.checker:
        _refuse(
            "checker-result-identity-mismatch",
            "incremental checker result names another checker",
            checker=unit.checker,
            node=unit.node.nodeId,
        )


def _checker_observation(
    evidence: Mapping[str, JsonValue],
    unit: AffectedUnitPlan,
) -> _CheckerObservation:
    blocked, ok = _checker_state(evidence, unit)
    files_checked = _result_count(evidence, "filesChecked", unit)
    finding_count = _result_count(evidence, "findingCount", unit)
    _require_files_checked(files_checked, blocked, unit)
    _require_finding_state(finding_count, blocked, ok, unit)
    status: UnitStatus = "blocked" if blocked else "pass" if ok else "fail"
    return _CheckerObservation(
        status=status,
        code=_result_code(evidence, status, unit),
        files_checked=files_checked,
        finding_count=finding_count,
    )


def _checker_state(
    evidence: Mapping[str, JsonValue],
    unit: AffectedUnitPlan,
) -> tuple[bool, bool]:
    state = evidence.get("status")
    if state not in ("checked", "blocked"):
        _refuse_unproven(unit)
    ok = evidence.get("ok")
    if type(ok) is not bool:
        _refuse_unproven(unit)
    blocked = state == "blocked"
    if blocked and ok:
        _refuse_unproven(unit)
    return blocked, cast(bool, ok)


def _result_count(
    evidence: Mapping[str, JsonValue],
    field: str,
    unit: AffectedUnitPlan,
) -> int:
    value = evidence.get(field)
    if type(value) is not int:
        _refuse_unproven(unit)
    count = cast(int, value)
    if count < 0:
        _refuse_unproven(unit)
    return count


def _require_files_checked(
    files_checked: int,
    blocked: bool,
    unit: AffectedUnitPlan,
) -> None:
    expected = 0 if blocked else 1
    if files_checked != expected:
        _refuse_unproven(unit)


def _require_finding_state(
    finding_count: int,
    blocked: bool,
    ok: bool,
    unit: AffectedUnitPlan,
) -> None:
    if blocked:
        return
    if ok == (finding_count > 0):
        _refuse_unproven(unit)


def _result_code(
    evidence: Mapping[str, JsonValue],
    status: UnitStatus,
    unit: AffectedUnitPlan,
) -> str:
    if status != "blocked":
        return "checker-pass" if status == "pass" else "checker-findings"
    code = evidence.get("code")
    if not isinstance(code, str) or not code.strip():
        _refuse(
            "checker-result-code-missing",
            "blocked incremental checker result requires a concrete failure code",
            checker=unit.checker,
            node=unit.node.nodeId,
        )
    return code


def _refuse_unproven(unit: AffectedUnitPlan) -> NoReturn:
    _refuse(
        "checker-result-unproven",
        "incremental checker did not return one complete selected-document result",
        checker=unit.checker,
        node=unit.node.nodeId,
    )


def _canonical_evidence(
    evidence: Mapping[str, object],
    unit: AffectedUnitPlan,
) -> dict[str, JsonValue]:
    try:
        encoded = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        _refuse(
            "checker-result-not-canonical-json",
            f"incremental checker evidence is not canonical JSON: {type(error).__name__}",
            checker=unit.checker,
            node=unit.node.nodeId,
        )
    if not isinstance(decoded, dict):
        _refuse(
            "checker-result-not-object",
            "incremental checker evidence must be one JSON object",
            checker=unit.checker,
            node=unit.node.nodeId,
        )
    return cast(dict[str, JsonValue], decoded)


def _member_results(
    members: tuple[AffectedMemberPlan, ...],
    subresults: tuple[AffectedUnitResult, ...],
) -> tuple[AffectedMemberResult, ...]:
    by_unit = {item.unit.unitDigest: item for item in subresults}
    results = []
    for member in members:
        units = tuple(by_unit[item] for item in member.unitDigests)
        status = _aggregate_status(units) if units else None
        result_digests = tuple(sorted(item.resultDigest for item in units))
        payload = {
            "nodeId": member.node.nodeId,
            "disposition": "checked" if units else "dependency-input",
            "status": status,
            "unitResultDigests": list(result_digests),
        }
        results.append(
            AffectedMemberResult(
                nodeId=member.node.nodeId,
                disposition="checked" if units else "dependency-input",
                status=status,
                unitResultDigests=result_digests,
                resultDigest=canonical_digest(payload),
            )
        )
    return tuple(results)


def _aggregate_status(results: Sequence[AffectedUnitResult]) -> UnitStatus:
    if any(item.status == "blocked" for item in results):
        return "blocked"
    if any(item.status == "fail" for item in results):
        return "fail"
    return "pass"


def _validated_plan(plan: AffectedClosurePlan) -> AffectedClosurePlan:
    try:
        return AffectedClosurePlan.model_validate(plan.model_dump(mode="json", by_alias=True))
    except ValueError as error:
        _refuse(
            "affected-plan-invalid",
            f"affected-closure plan failed exact validation: {type(error).__name__}",
        )


def _validated_prior_results(
    prior_results: Sequence[AffectedUnitResult],
) -> tuple[AffectedUnitResult, ...]:
    validated = []
    for result in prior_results:
        try:
            validated.append(
                AffectedUnitResult.model_validate(result.model_dump(mode="json", by_alias=True))
            )
        except ValueError as error:
            _refuse(
                "subresult-invalid",
                f"prior subresult failed exact validation: {type(error).__name__}",
            )
    return tuple(validated)


def _require_current_candidate(
    authority: ScopeAuthority,
    plan: AffectedClosurePlan,
) -> None:
    observed = authority.observe()
    if (
        observed.digest != plan.candidateDigest
        or observed.code.candidateTree != plan.codeTree
        or observed.memory.candidateTree != plan.memoryTree
    ):
        _refuse(
            "affected-candidate-stale",
            "code, memory, task, or pair authority changed after affected planning",
            candidate=plan.candidateDigest,
        )


def _refuse(code: str, detail: str, **evidence: str | None) -> NoReturn:
    raise GateFiveClosureRefusedError(
        ScopeFailure(
            code=code,
            detail=detail,
            checker=evidence.get("checker"),
            node=evidence.get("node"),
            snapshot=evidence.get("snapshot"),
            candidate=evidence.get("candidate"),
        )
    )


__all__ = [
    "AffectedClosureExecution",
    "IncrementalCheckerExecutor",
    "RangeResolutionAffectedExecutor",
    "RangeResolutionExecutionContext",
    "execute_affected_closure",
    "plan_affected_subresult_reuse",
]
