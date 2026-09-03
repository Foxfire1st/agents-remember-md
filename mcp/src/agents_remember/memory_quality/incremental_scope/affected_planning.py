"""Compile one exact R07 affected-closure plan after current Gate 1-4 proof."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    plan_certificate_reuse,
)
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    GateCertificate,
    GateCertificateIdentity,
)
from agents_remember.errors import CertificationContractError

from .affected_models import (
    AffectedClosurePlan,
    AffectedMemberPlan,
    AffectedUnitPlan,
    CheckerExecutionPolicy,
    ClosureDependencyIdentity,
    dependency_identity,
    full_only_disposition,
)
from .compiler import ScopeAuthority
from .errors import GateFiveClosureRefusedError, ScopeFailure
from .execution_registry import (
    checker_execution_registry,
    checker_execution_registry_version,
)
from .models import (
    CheckerScopePolicy,
    ScopeCandidateIdentity,
    ScopeEdge,
    ScopeManifest,
    ScopeNode,
    canonical_digest,
)
from .registry import checker_registry_version, checker_scope_registry


@dataclass(frozen=True)
class AffectedClosureAdmission:
    """Current R06/R21 authorities needed to admit non-certifying Gate-5 work."""

    candidateAuthority: ScopeAuthority
    certificationAdmission: CertificationAdmissionManifest
    gateCertificates: tuple[GateCertificate, ...]
    changes: tuple[CertificateInputChange, ...] = ()


@dataclass(frozen=True)
class _UnitInputs:
    scope: ScopeManifest
    nodes: dict[str, ScopeNode]
    executions: dict[str, CheckerExecutionPolicy]
    codeTree: str


def compile_affected_closure_plan(
    admission: AffectedClosureAdmission,
    scope: ScopeManifest,
) -> AffectedClosurePlan:
    """Admit exact Gate 1-4 proof and compile every affected R07 checker unit."""

    candidate = admission.candidateAuthority.observe()
    _validate_scope(candidate, scope)
    certificates, reuse_digest = _admit_gate_certificates(admission, candidate)
    policies = checker_scope_registry()
    executions = {item.checker: item for item in checker_execution_registry()}
    units = _compile_units(candidate, scope, policies, executions)
    members = _compile_members(scope.selectedNodes, units)
    pending = tuple(full_only_disposition(item) for item in policies if item.mode == "full-only")
    coherence = tuple(
        sorted(
            {
                subrecord
                for change in admission.changes
                for subrecord in change.affectedGateFiveSubrecords
            }
        )
    )
    payload = {
        "schemaVersion": "memory-affected-closure-plan/v1",
        "candidateDigest": candidate.digest,
        "codeRoot": candidate.code.root,
        "memoryRoot": candidate.memory.root,
        "onboardingRoot": candidate.pairIdentity.onboardingRoot,
        "codeTree": candidate.code.candidateTree,
        "memoryTree": candidate.memory.candidateTree,
        "scope": scope.model_dump(mode="json", by_alias=True),
        "executionRegistryVersion": checker_execution_registry_version(),
        "gateCertificates": [item.model_dump(mode="json") for item in certificates],
        "certificateReusePlanDigest": reuse_digest,
        "members": [item.model_dump(mode="json") for item in members],
        "units": [item.model_dump(mode="json") for item in units],
        "pendingFinalFull": [item.model_dump(mode="json") for item in pending],
        "affectedCoherenceSubrecords": list(coherence),
        "acceptanceEligible": False,
        "fullFinalRequired": True,
    }
    plan = AffectedClosurePlan(
        candidateDigest=candidate.digest,
        codeRoot=candidate.code.root,
        memoryRoot=candidate.memory.root,
        onboardingRoot=candidate.pairIdentity.onboardingRoot,
        codeTree=candidate.code.candidateTree,
        memoryTree=candidate.memory.candidateTree,
        scope=scope,
        executionRegistryVersion=checker_execution_registry_version(),
        gateCertificates=certificates,
        certificateReusePlanDigest=reuse_digest,
        members=members,
        units=units,
        pendingFinalFull=pending,
        affectedCoherenceSubrecords=coherence,
        planDigest=canonical_digest(payload),
    )
    if admission.candidateAuthority.observe() != candidate:
        _refuse(
            "candidate-moved-during-affected-planning",
            "code, memory, task, or pair authority changed during affected-closure planning",
            candidate=candidate.digest,
        )
    return plan


def _validate_scope(candidate: ScopeCandidateIdentity, scope: ScopeManifest) -> None:
    _validate_scope_identity(candidate, scope)
    expected = checker_scope_registry()
    _validate_scope_registry(scope, expected)
    _validate_scope_edges(scope)


def _validate_scope_identity(candidate: ScopeCandidateIdentity, scope: ScopeManifest) -> None:
    if scope.candidateDigest != candidate.digest:
        _refuse(
            "affected-scope-candidate-mismatch",
            "R06 scope belongs to another code/memory/task candidate",
            candidate=candidate.digest,
        )
    if scope.sourceIndex.candidateDigest != candidate.digest:
        _refuse(
            "affected-source-index-stale",
            "R06 scope source-index identity is stale for this candidate",
            snapshot=scope.sourceIndex.snapshotId,
        )
    payload = scope.model_dump(mode="json", by_alias=True, exclude={"manifestDigest"})
    if canonical_digest(payload) != scope.manifestDigest:
        _refuse("affected-scope-digest-invalid", "R06 scope manifest does not self-verify")


def _validate_scope_registry(
    scope: ScopeManifest,
    expected: tuple[CheckerScopePolicy, ...],
) -> None:
    if scope.checkerPolicies != expected:
        _refuse(
            "affected-checker-population-incomplete",
            "R06 scope does not contain the complete current checker registry",
        )
    if scope.checkerRegistryVersion != checker_registry_version():
        _refuse(
            "affected-checker-population-incomplete",
            "R06 scope does not contain the complete current checker registry",
        )
    full_only = tuple(item.checker for item in expected if item.mode == "full-only")
    if scope.fullOnlyCheckers != full_only:
        _refuse(
            "affected-checker-disposition-invalid",
            "R06 full-only checker declarations are missing or misclassified",
        )
    if scope.incrementalReady != (not full_only):
        _refuse(
            "affected-checker-disposition-invalid",
            "R06 full-only checker declarations are missing or misclassified",
        )


def _validate_scope_edges(scope: ScopeManifest) -> None:
    selected = {item.nodeId for item in scope.selectedNodes}
    if any(
        edge.source not in selected or edge.target not in selected for edge in scope.selectedEdges
    ):
        _refuse(
            "affected-edge-endpoint-missing",
            "R06 selected edge refers outside the published selected population",
        )


def _admit_gate_certificates(
    admission: AffectedClosureAdmission,
    candidate: ScopeCandidateIdentity,
) -> tuple[tuple[GateCertificateIdentity, ...], str]:
    certificates = admission.gateCertificates
    if tuple(item.semanticEnvelope.gate for item in certificates) != (1, 2, 3, 4):
        _refuse(
            "gate-certificate-prefix-incomplete",
            "affected Gate-5 work requires the exact current green Gate 1-4 prefix",
        )
    code_identity = admission.certificationAdmission.semanticEnvelope.candidateCodeTree
    if code_identity.value != candidate.code.candidateTree:
        _refuse(
            "gate-certificate-code-candidate-mismatch",
            "Gate 1-4 admission belongs to another exact code tree",
            candidate=candidate.digest,
        )
    try:
        reuse = plan_certificate_reuse(
            admission.certificationAdmission,
            certificates,
            admission.changes,
        )
    except (CertificationContractError, ValueError) as error:
        _refuse(
            "gate-certificate-currentness-unproven",
            f"R21 refused the Gate 1-4 certificate prefix: {type(error).__name__}",
            candidate=candidate.digest,
        )
    identities = tuple(item.identity for item in certificates)
    if (
        reuse.reusedCertificates != identities
        or reuse.firstGateToRun != 5
        or reuse.invalidatedGates != (5,)
    ):
        _refuse(
            "gate-certificate-prefix-invalidated",
            "R21 requires an earlier code gate; this candidate is not a memory-only Gate-5 start",
            candidate=candidate.digest,
        )
    return identities, canonical_digest(reuse.model_dump(mode="json"))


def _compile_units(
    candidate: ScopeCandidateIdentity,
    scope: ScopeManifest,
    policies: tuple[CheckerScopePolicy, ...],
    executions: dict[str, CheckerExecutionPolicy],
) -> tuple[AffectedUnitPlan, ...]:
    nodes = {item.nodeId: item for item in scope.selectedNodes}
    onboarding = _onboarding_relative(candidate)
    documents = {
        item.nodeId: item
        for item in scope.selectedNodes
        if _document_relative(item.nodeId, onboarding) is not None
    }
    inputs = _UnitInputs(
        scope=scope,
        nodes=nodes,
        executions=executions,
        codeTree=candidate.code.candidateTree,
    )
    units = [
        _unit(
            node,
            _document_relative(node.nodeId, onboarding),
            policy,
            inputs,
        )
        for node in documents.values()
        for policy in policies
        if policy.mode == "incremental"
    ]
    return tuple(sorted(units, key=lambda item: item.unitDigest))


def _unit(
    node: ScopeNode,
    document: str | None,
    policy: CheckerScopePolicy,
    inputs: _UnitInputs,
) -> AffectedUnitPlan:
    if document is None or policy.extractorVersion is None:
        _refuse(
            "incremental-checker-contract-incomplete",
            "incremental checker lacks a document target or extractor identity",
            checker=policy.checker,
            node=node.nodeId,
        )
    execution = inputs.executions[policy.checker]
    dependencies, edges = _dependency_closure(
        node.nodeId,
        inputs.scope.selectedEdges,
        inputs.nodes,
    )
    payload = {
        "checker": policy.checker,
        "document": document,
        "node": dependency_identity(node).model_dump(mode="json"),
        "dependencies": [item.model_dump(mode="json") for item in dependencies],
        "dependencyEdges": [item.model_dump(mode="json") for item in edges],
        "extractorVersion": policy.extractorVersion,
        "validatorVersion": execution.validatorVersion,
        "runtimeVersion": execution.runtimeVersion,
        "correctiveOwner": execution.correctiveOwner,
        "checkerRegistryVersion": inputs.scope.checkerRegistryVersion,
        "executionRegistryVersion": checker_execution_registry_version(),
        "sourceIndexSnapshot": inputs.scope.sourceIndex.snapshotId,
        "codeTree": inputs.codeTree,
    }
    return AffectedUnitPlan(unitDigest=canonical_digest(payload), **payload)


def _dependency_closure(
    target: str,
    edges: tuple[ScopeEdge, ...],
    nodes: dict[str, ScopeNode],
) -> tuple[tuple[ClosureDependencyIdentity, ...], tuple[ScopeEdge, ...]]:
    incoming: dict[str, list[ScopeEdge]] = defaultdict(list)
    for edge in edges:
        incoming[edge.target].append(edge)
    selected_nodes: set[str] = set()
    selected_edges: set[ScopeEdge] = set()
    pending = deque((target,))
    while pending:
        current = pending.popleft()
        for edge in incoming.get(current, ()):
            selected_edges.add(edge)
            if edge.source != target and edge.source not in selected_nodes:
                selected_nodes.add(edge.source)
                pending.append(edge.source)
    dependencies = tuple(dependency_identity(nodes[item]) for item in sorted(selected_nodes))
    ordered_edges = tuple(
        sorted(selected_edges, key=lambda item: (item.source, item.target, item.edgeClass))
    )
    return dependencies, ordered_edges


def _compile_members(
    nodes: tuple[ScopeNode, ...],
    units: tuple[AffectedUnitPlan, ...],
) -> tuple[AffectedMemberPlan, ...]:
    by_node: dict[str, list[str]] = defaultdict(list)
    for unit in units:
        by_node[unit.node.nodeId].append(unit.unitDigest)
    return tuple(
        AffectedMemberPlan(
            node=node,
            disposition="check-target" if node.nodeId in by_node else "dependency-input",
            unitDigests=tuple(sorted(by_node[node.nodeId])),
        )
        for node in nodes
    )


def _onboarding_relative(candidate: ScopeCandidateIdentity) -> str:
    try:
        relative = Path(candidate.pairIdentity.onboardingRoot).relative_to(candidate.memory.root)
    except ValueError:
        _refuse(
            "onboarding-root-outside-memory",
            "candidate onboarding root is outside its exact memory repository",
            candidate=candidate.digest,
        )
    if relative == Path("."):
        return ""
    return relative.as_posix()


def _document_relative(node_id: str, onboarding: str) -> str | None:
    prefix = f"memory:{onboarding}/" if onboarding else "memory:"
    if not node_id.startswith(prefix) or not node_id.endswith(".md"):
        return None
    relative = node_id.removeprefix(prefix)
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        return None
    return relative


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


__all__ = ["AffectedClosureAdmission", "compile_affected_closure_plan"]
