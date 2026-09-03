"""Immutable contracts for R07 affected-closure execution and reuse."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from agents_remember.certification.certificate_models import GateCertificateIdentity

from .models import (
    CheckerScopePolicy,
    ScopeEdge,
    ScopeManifest,
    ScopeNode,
    _StrictModel,
    canonical_digest,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Hash = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
UnitStatus = Literal["pass", "fail", "blocked"]


class CheckerExecutionPolicy(_StrictModel):
    """R07-owned execution identity for one R06 incremental checker."""

    checker: str = Field(min_length=1)
    validatorVersion: str = Field(min_length=1)
    runtimeVersion: str = Field(min_length=1)
    correctiveOwner: str = Field(min_length=1)


class ClosureDependencyIdentity(_StrictModel):
    """One exact transitive input to an affected checker unit."""

    nodeId: str = Field(min_length=1)
    contentDigest: Sha256
    authorityNamespace: str = Field(min_length=1)
    validatorVersion: str = Field(min_length=1)


class AffectedUnitPlan(_StrictModel):
    """One checker/document unit whose identity permits only dependency-exact reuse."""

    checker: str = Field(min_length=1)
    document: str = Field(min_length=1)
    node: ClosureDependencyIdentity
    dependencies: tuple[ClosureDependencyIdentity, ...] = Field(max_length=16384)
    dependencyEdges: tuple[ScopeEdge, ...] = Field(max_length=16384)
    extractorVersion: str = Field(min_length=1)
    validatorVersion: str = Field(min_length=1)
    runtimeVersion: str = Field(min_length=1)
    correctiveOwner: str = Field(min_length=1)
    checkerRegistryVersion: Sha256
    executionRegistryVersion: Sha256
    sourceIndexSnapshot: Sha256
    codeTree: Hash
    unitDigest: Sha256

    @field_validator("document")
    @classmethod
    def _canonical_document(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.suffix != ".md"
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("affected-unit document must be one canonical relative .md path")
        return value

    @model_validator(mode="after")
    def _require_canonical_unit(self) -> Self:
        dependency_ids = tuple(item.nodeId for item in self.dependencies)
        if dependency_ids != tuple(sorted(set(dependency_ids))):
            raise ValueError("affected-unit dependencies must be unique and canonical")
        edge_keys = tuple(
            (item.source, item.target, item.edgeClass) for item in self.dependencyEdges
        )
        if edge_keys != tuple(sorted(set(edge_keys))):
            raise ValueError("affected-unit dependency edges must be unique and canonical")
        if self.node.nodeId in set(dependency_ids):
            raise ValueError("affected-unit dependencies must exclude the checked node")
        payload = self.model_dump(mode="json", by_alias=True, exclude={"unitDigest"})
        if self.unitDigest != canonical_digest(payload):
            raise ValueError("affected-unit digest does not match its exact inputs")
        return self


class AffectedMemberPlan(_StrictModel):
    """Every R06 closure member, including inputs that are not checker documents."""

    node: ScopeNode
    disposition: Literal["check-target", "dependency-input"]
    unitDigests: tuple[Sha256, ...] = Field(max_length=256)

    @model_validator(mode="after")
    def _require_member_shape(self) -> Self:
        if self.unitDigests != tuple(sorted(set(self.unitDigests))):
            raise ValueError("affected-member unit digests must be unique and canonical")
        if (self.disposition == "check-target") != bool(self.unitDigests):
            raise ValueError("only a check-target member may name affected units")
        return self


class PendingFinalFullCheck(_StrictModel):
    """A declared R06 full-only checker, visible without masquerading as a green rail."""

    checker: str = Field(min_length=1)
    disposition: Literal["pending-final-full"] = "pending-final-full"
    reason: str = Field(min_length=1)


class AffectedClosurePlan(_StrictModel):
    """Self-contained non-certifying plan for the proven R06 affected closure."""

    schemaVersion: Literal["memory-affected-closure-plan/v1"] = "memory-affected-closure-plan/v1"
    candidateDigest: Sha256
    codeRoot: str = Field(min_length=1)
    memoryRoot: str = Field(min_length=1)
    onboardingRoot: str = Field(min_length=1)
    codeTree: Hash
    memoryTree: Hash
    scope: ScopeManifest
    executionRegistryVersion: Sha256
    gateCertificates: tuple[GateCertificateIdentity, ...] = Field(min_length=4, max_length=4)
    certificateReusePlanDigest: Sha256
    members: tuple[AffectedMemberPlan, ...] = Field(max_length=16384)
    units: tuple[AffectedUnitPlan, ...] = Field(max_length=16384)
    pendingFinalFull: tuple[PendingFinalFullCheck, ...] = Field(max_length=256)
    affectedCoherenceSubrecords: tuple[str, ...] = Field(max_length=4096)
    acceptanceEligible: Literal[False] = False
    fullFinalRequired: Literal[True] = True
    planDigest: Sha256

    @field_validator("codeRoot", "memoryRoot", "onboardingRoot")
    @classmethod
    def _absolute_root(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise ValueError("affected-closure roots must be normalized absolute POSIX paths")
        return value

    @model_validator(mode="after")
    def _require_complete_population(self) -> Self:
        self._require_member_population()
        self._require_pending_population()
        self._require_exact_incremental_units()
        self._require_authority_inputs()
        payload = self.model_dump(mode="json", by_alias=True, exclude={"planDigest"})
        if self.planDigest != canonical_digest(payload):
            raise ValueError("affected-closure plan digest does not match its content")
        return self

    def _require_member_population(self) -> None:
        member_ids = tuple(item.node.nodeId for item in self.members)
        selected_ids = tuple(item.nodeId for item in self.scope.selectedNodes)
        if member_ids != selected_ids:
            raise ValueError("affected members must equal the exact R06 selected population")
        unit_digests = tuple(item.unitDigest for item in self.units)
        if unit_digests != tuple(sorted(set(unit_digests))):
            raise ValueError("affected units must be unique and canonical")
        member_units = tuple(digest for member in self.members for digest in member.unitDigests)
        if tuple(sorted(member_units)) != unit_digests:
            raise ValueError("affected members must account for every exact unit once")
        units_by_node: dict[str, list[str]] = {}
        for unit in self.units:
            units_by_node.setdefault(unit.node.nodeId, []).append(unit.unitDigest)
        if any(
            member.unitDigests != tuple(sorted(units_by_node.get(member.node.nodeId, ())))
            for member in self.members
        ):
            raise ValueError("affected-member units must belong to their exact selected node")

    def _require_pending_population(self) -> None:
        pending = tuple(item.checker for item in self.pendingFinalFull)
        if pending != tuple(sorted(set(pending))):
            raise ValueError("pending-final-full checkers must be unique and canonical")
        if pending != self.scope.fullOnlyCheckers:
            raise ValueError("affected plan cannot hide an R06 full-only checker")

    def _require_authority_inputs(self) -> None:
        if self.affectedCoherenceSubrecords != tuple(sorted(set(self.affectedCoherenceSubrecords))):
            raise ValueError("affected coherence subrecords must be unique and canonical")
        if tuple(item.gate for item in self.gateCertificates) != (1, 2, 3, 4):
            raise ValueError("affected closure requires the exact green Gate 1-4 prefix")
        if (
            self.scope.candidateDigest != self.candidateDigest
            or self.scope.sourceIndex.candidateDigest != self.candidateDigest
            or self.scope.sourceIndex.codeRoot != self.codeRoot
            or self.scope.sourceIndex.memoryRoot != self.memoryRoot
        ):
            raise ValueError("affected plan must retain the exact R06 candidate and roots")

    def _require_exact_incremental_units(self) -> None:
        prefix = self._onboarding_node_prefix()
        self._require_unit_population(prefix)
        for unit in self.units:
            self._require_unit_document(unit, prefix)
        for unit in self.units:
            self._require_unit_inputs(unit)

    def _onboarding_node_prefix(self) -> str:
        try:
            onboarding = Path(self.onboardingRoot).relative_to(self.memoryRoot).as_posix()
        except ValueError as error:
            raise ValueError("affected onboarding root must be inside the memory root") from error
        return "memory:" if onboarding == "." else f"memory:{onboarding}/"

    def _require_unit_population(self, prefix: str) -> None:
        documents = self._incremental_documents(prefix)
        incrementals = self._incremental_checkers()
        observed = {(unit.node.nodeId, unit.checker) for unit in self.units}
        expected = {(document, checker) for document in documents for checker in incrementals}
        if observed != expected or len(observed) != len(self.units):
            raise ValueError("affected units must equal every proven incremental document/checker")

    def _incremental_documents(self, prefix: str) -> set[str]:
        return {
            node.nodeId
            for node in self.scope.selectedNodes
            if node.nodeId.startswith(prefix) and node.nodeId.endswith(".md")
        }

    def _incremental_checkers(self) -> set[str]:
        return {
            policy.checker for policy in self.scope.checkerPolicies if policy.mode == "incremental"
        }

    @staticmethod
    def _require_unit_document(unit: AffectedUnitPlan, prefix: str) -> None:
        if unit.node.nodeId != f"{prefix}{unit.document}":
            raise ValueError("affected-unit document must name its exact selected memory node")

    def _require_unit_inputs(self, unit: AffectedUnitPlan) -> None:
        if unit.codeTree != self.codeTree:
            raise ValueError("affected units must retain exact plan input identities")
        if unit.sourceIndexSnapshot != self.scope.sourceIndex.snapshotId:
            raise ValueError("affected units must retain exact plan input identities")
        if unit.checkerRegistryVersion != self.scope.checkerRegistryVersion:
            raise ValueError("affected units must retain exact plan input identities")
        if unit.executionRegistryVersion != self.executionRegistryVersion:
            raise ValueError("affected units must retain exact plan input identities")


class AffectedUnitResult(_StrictModel):
    """Content-addressed terminal result for one exact affected unit."""

    schemaVersion: Literal["memory-affected-unit-result/v1"] = "memory-affected-unit-result/v1"
    unit: AffectedUnitPlan
    status: UnitStatus
    code: str = Field(min_length=1)
    correctiveOwner: str = Field(min_length=1)
    filesChecked: int = Field(ge=0)
    findingCount: int = Field(ge=0)
    evidence: JsonValue
    evidenceDigest: Sha256
    resultDigest: Sha256

    @model_validator(mode="after")
    def _verify_result(self) -> Self:
        if (self.status == "pass" and self.findingCount != 0) or (
            self.status == "fail" and self.findingCount == 0
        ):
            raise ValueError("affected-unit status must match its finding count")
        if (self.status == "blocked" and self.filesChecked != 0) or (
            self.status != "blocked" and self.filesChecked != 1
        ):
            raise ValueError("affected-unit status must match its selected-document count")
        if self.evidenceDigest != canonical_digest(self.evidence):
            raise ValueError("affected-unit evidence digest does not match its content")
        payload = self.model_dump(mode="json", by_alias=True, exclude={"resultDigest"})
        if self.resultDigest != canonical_digest(payload):
            raise ValueError("affected-unit result digest does not match its content")
        return self


class AffectedMemberResult(_StrictModel):
    """One explicit result for every selected R06 closure member."""

    nodeId: str = Field(min_length=1)
    disposition: Literal["checked", "dependency-input"]
    status: UnitStatus | None = None
    unitResultDigests: tuple[Sha256, ...] = Field(max_length=256)
    resultDigest: Sha256

    @model_validator(mode="after")
    def _require_result_shape(self) -> Self:
        if self.unitResultDigests != tuple(sorted(set(self.unitResultDigests))):
            raise ValueError("member-result unit digests must be unique and canonical")
        checked = self.disposition == "checked"
        if checked != (self.status is not None and bool(self.unitResultDigests)):
            raise ValueError("checked members require terminal unit results")
        payload = self.model_dump(mode="json", by_alias=True, exclude={"resultDigest"})
        if self.resultDigest != canonical_digest(payload):
            raise ValueError("affected-member result digest does not match its content")
        return self


class SubresultReusePlan(_StrictModel):
    """Explicit unit-level invalidation; no path/latest search or implicit broadening."""

    schemaVersion: Literal["memory-affected-subresult-reuse/v1"] = (
        "memory-affected-subresult-reuse/v1"
    )
    closurePlanDigest: Sha256
    reusedUnitDigests: tuple[Sha256, ...]
    unitsToExecute: tuple[Sha256, ...]
    ignoredPriorResultDigests: tuple[Sha256, ...]
    reusePlanDigest: Sha256

    @model_validator(mode="after")
    def _verify_reuse(self) -> Self:
        populations = (
            self.reusedUnitDigests,
            self.unitsToExecute,
            self.ignoredPriorResultDigests,
        )
        if any(items != tuple(sorted(set(items))) for items in populations):
            raise ValueError("subresult-reuse populations must be unique and canonical")
        if set(self.reusedUnitDigests) & set(self.unitsToExecute):
            raise ValueError("a unit cannot be both reused and executed")
        payload = self.model_dump(mode="json", by_alias=True, exclude={"reusePlanDigest"})
        if self.reusePlanDigest != canonical_digest(payload):
            raise ValueError("subresult-reuse digest does not match its content")
        return self


class AffectedClosureResult(_StrictModel):
    """Non-certifying aggregate over every planned member and pending final-only check."""

    schemaVersion: Literal["memory-affected-closure-result/v1"] = (
        "memory-affected-closure-result/v1"
    )
    plan: AffectedClosurePlan
    subresults: tuple[AffectedUnitResult, ...] = Field(max_length=16384)
    memberResults: tuple[AffectedMemberResult, ...] = Field(max_length=16384)
    reusedUnitDigests: tuple[Sha256, ...]
    executedUnitDigests: tuple[Sha256, ...]
    pendingFinalFull: tuple[PendingFinalFullCheck, ...]
    terminalStatus: UnitStatus
    incrementalMemoryReady: bool
    closeoutReady: Literal[False] = False
    acceptanceEligible: Literal[False] = False
    fullFinalRequired: Literal[True] = True
    resultDigest: Sha256

    @model_validator(mode="after")
    def _require_complete_result(self) -> Self:
        planned = tuple(item.unitDigest for item in self.plan.units)
        self._require_result_population(planned)
        results_by_unit = {item.unit.unitDigest: item for item in self.subresults}
        self._require_member_results(results_by_unit)
        self._require_execution_partition(planned)
        self._require_pending_population()
        self._require_terminal_state()
        payload = self.model_dump(mode="json", by_alias=True, exclude={"resultDigest"})
        if self.resultDigest != canonical_digest(payload):
            raise ValueError("affected-closure result digest does not match its content")
        return self

    def _require_result_population(self, planned: tuple[str, ...]) -> None:
        observed = tuple(item.unit.unitDigest for item in self.subresults)
        if observed != planned:
            raise ValueError("closure subresults must equal the exact planned unit population")
        if tuple(item.nodeId for item in self.memberResults) != tuple(
            item.node.nodeId for item in self.plan.members
        ):
            raise ValueError("closure member results must equal the exact planned population")

    def _require_execution_partition(self, planned: tuple[str, ...]) -> None:
        covered = tuple(sorted((*self.reusedUnitDigests, *self.executedUnitDigests)))
        if covered != planned:
            raise ValueError("reused and executed units must partition the plan")
        if set(self.reusedUnitDigests) & set(self.executedUnitDigests):
            raise ValueError("reused and executed units must partition the plan")

    def _require_pending_population(self) -> None:
        if self.pendingFinalFull != self.plan.pendingFinalFull:
            raise ValueError("closure result cannot hide pending-final-full work")

    def _require_terminal_state(self) -> None:
        ready = all(item.status == "pass" for item in self.subresults)
        if self.incrementalMemoryReady != ready:
            raise ValueError("incremental readiness must match every exact unit result")
        expected_status = _aggregate_status(self.subresults)
        if self.terminalStatus != expected_status:
            raise ValueError("closure terminal status must aggregate exact unit results")

    def _require_member_results(
        self,
        results_by_unit: dict[str, AffectedUnitResult],
    ) -> None:
        for member, result in zip(self.plan.members, self.memberResults, strict=True):
            units = tuple(results_by_unit[digest] for digest in member.unitDigests)
            expected_digests = tuple(sorted(item.resultDigest for item in units))
            if result.unitResultDigests != expected_digests:
                raise ValueError("member result must bind every exact planned unit result")
            expected_disposition = "checked" if units else "dependency-input"
            member_status = _aggregate_status(units) if units else None
            if result.disposition != expected_disposition or result.status != member_status:
                raise ValueError("member result disposition and status must derive from its units")


def _aggregate_status(results: tuple[AffectedUnitResult, ...]) -> UnitStatus:
    if any(item.status == "blocked" for item in results):
        return "blocked"
    if any(item.status == "fail" for item in results):
        return "fail"
    return "pass"


def dependency_identity(node: ScopeNode) -> ClosureDependencyIdentity:
    return ClosureDependencyIdentity(
        nodeId=node.nodeId,
        contentDigest=node.contentDigest,
        authorityNamespace=node.authorityNamespace,
        validatorVersion=node.validatorVersion,
    )


def full_only_disposition(policy: CheckerScopePolicy) -> PendingFinalFullCheck:
    return PendingFinalFullCheck(checker=policy.checker, reason=policy.reason)


__all__ = [
    "AffectedClosurePlan",
    "AffectedClosureResult",
    "AffectedMemberPlan",
    "AffectedMemberResult",
    "AffectedUnitPlan",
    "AffectedUnitResult",
    "CheckerExecutionPolicy",
    "ClosureDependencyIdentity",
    "PendingFinalFullCheck",
    "SubresultReusePlan",
    "UnitStatus",
    "dependency_identity",
    "full_only_disposition",
]
