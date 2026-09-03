"""CCR-R08 final full memory-coherence certification (Gate 5) contracts.

The full certify_final_full_memory_coherence orchestration (green, red,
blocked, and refusal routes). Split from the original single module
(repository file-size hard limit). Per the repository convention for file-size
splits (compare test_author_execution_graph importing from
test_task_execution_topology) this module also owns the shared Gate-5
fixture scaffold used by the sibling modules test_final_gate_prefix_adapter,
test_final_catalog_plan_attestation, and
test_final_catalog_readiness_projection, because a second non-test module
under mcp/tests would be governed evidence requiring lifecycle metadata.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from agents_remember.certification import (
    build_rail_result,
    canonicalize_registry,
    compile_certification_admission,
    compile_certification_plan,
    compile_gate_certificate,
    compile_gate_result_manifest,
    compile_repository_profile_plan,
)
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    CertificateReusePlan,
)
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    CreationProvenance,
    GateCertificate,
    GateCertificateIdentity,
    GateCertificateIssuanceContext,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    ArtifactDeclaration,
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationPlan,
    CompiledRail,
    GateId,
    GatePlan,
    GateResultAdmission,
    GateResultManifest,
    RailAdapterDefinition,
    RailApplicability,
    RailArtifactResult,
    RailClass,
    RailDefinition,
    RailEvidenceContract,
    RailEvidenceReference,
    RailIdentity,
    RailRegistry,
    RailRuntimeInputs,
    RailStatus,
    RailTerminalObservation,
    RegistryProfile,
)
from agents_remember.certification.repository_profiles import canonicalize_repository_profile
from agents_remember.certification.repository_profiles.canonical import repository_profile_digest
from agents_remember.certification.repository_profiles.models import (
    DaggerModuleExecutorDefinition,
    JsonExitStatusDecoderDefinition,
    ProfileMode,
    ProfilePurpose,
    PublishedArtifactDefinition,
    RepositoryCertificationProfile,
    RepositoryGateId,
    RepositoryGateSelection,
    RepositoryProfileSelection,
    RepositoryRailDefinition,
    RepositoryRailExecution,
    RepositorySelectorAuthority,
)
from agents_remember.errors import FinalCertificationError
from agents_remember.memory_quality.check import AVAILABLE_CHECKS
from agents_remember.memory_quality.final_certification import (
    certify_final_full_memory_coherence,
    complete_final_catalog,
)
from agents_remember.memory_quality.final_certification.certify import FinalCertificationEvidence
from agents_remember.memory_quality.incremental_scope import affected_planning
from agents_remember.memory_quality.incremental_scope.affected_models import AffectedClosurePlan
from agents_remember.memory_quality.incremental_scope.affected_planning import (
    AffectedClosureAdmission,
    compile_affected_closure_plan,
)
from agents_remember.memory_quality.incremental_scope.candidate import (
    GitTreeDelta,
    ScopeCandidateIdentity,
    TaskObservationPair,
)
from agents_remember.memory_quality.incremental_scope.compiler import (
    build_dependency_snapshot,
    compile_scope_manifest,
    dependency_edge,
)
from agents_remember.memory_quality.incremental_scope.models import (
    CanonicalTaskObservation,
    DependencySnapshot,
    GitPathChange,
    ScopeManifest,
    ScopeNode,
    SourceIndexObservation,
    canonical_digest,
)
from agents_remember.memory_quality.style.document_shape import tables
from agents_remember.models.lifecycles.curator_coherence import CuratorCoherenceRecord
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.integration.closeout.curator_coherence import (
    ValidatedCuratorCoherence,
)

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))


_CANDIDATE = CandidateIdentity(kind="git-tree", value="c" * 40)
_CODE_TREE = "c" * 40
_MEMORY_TREE = "7" * 40
_PROFILE_ID = "closeout-targeted"
_DIGEST = "a" * 64
_ALL_GATES: tuple[GateId, ...] = (1, 2, 3, 4, 5)


# ---------------------------------------------------------------------------
# Standalone repository certification profile builder (no test-support module,
# no fixture-repo files). The profile is built entirely from production model
# literals so the R21 admission/certificate chain used by these tests stays
# byte-deterministic without consuming shared test fixtures.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _InlineRailSpec:
    rail_id: str
    gate: RepositoryGateId
    rail_class: RailClass
    command: tuple[str, ...]
    prerequisites: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    output_artifacts: tuple[str, ...] = ()
    selector: str | None = None
    clean_room: bool = False


def _inline_profile() -> RepositoryCertificationProfile:
    """One minimal but fully validated Gate 1-4 repository profile."""

    rails = (
        _inline_rail(
            _InlineRailSpec(
                rail_id="static-quality",
                gate=1,
                rail_class="pre-test-quality",
                command=("node", "scripts/lint.mjs"),
            )
        ),
        _inline_rail(
            _InlineRailSpec(
                rail_id="ordinary-suite",
                gate=2,
                rail_class="ordinary-test-suite",
                command=(
                    "node",
                    "scripts/run-suite.mjs",
                    "{reports}/node-suite.json",
                    "{reports}/node-coverage.json",
                    "{selected-tests}",
                ),
                prerequisites=("static-quality",),
                output_artifacts=("node-suite-result", "node-coverage"),
                selector="repository-test-selector",
            )
        ),
        _inline_rail(
            _InlineRailSpec(
                rail_id="post-suite-quality",
                gate=3,
                rail_class="post-test-quality",
                command=(
                    "node",
                    "scripts/coverage-check.mjs",
                    "{reports}/node-coverage.json",
                ),
                prerequisites=("ordinary-suite",),
                required_artifacts=("node-coverage",),
            )
        ),
        _inline_rail(
            _InlineRailSpec(
                rail_id="clean-room-e2e",
                gate=4,
                rail_class="integration-test",
                command=("node", "scripts/run-e2e.mjs", "{reports}/node-e2e.json"),
                prerequisites=("post-suite-quality",),
                clean_room=True,
            )
        ),
    )
    rail_ids = tuple(rail.identity for rail in rails)
    selections = tuple(
        _inline_selection(
            selection_id,
            cast(ProfilePurpose, purpose),
            cast(ProfileMode, mode),
            rail_ids,
        )
        for selection_id, purpose, mode in (
            ("local-targeted", "local-precommit", "targeted"),
            ("closeout-targeted", "closeout", "targeted"),
            ("closeout-full", "closeout", "full"),
        )
    )
    profile = RepositoryCertificationProfile(
        semanticRevision="1.0.0",
        repositoryId="fixture-node",
        profileId="fixture-node-certification",
        profileDigest="0" * 64,
        selections=selections,
        rails=rails,
        selectors=(_inline_selector(),),
        executorAdapters=(_inline_executor(),),
        resultDecoders=(_inline_decoder(),),
        publishedArtifacts=_inline_publications(),
    )
    return profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})


def _inline_selector() -> RepositorySelectorAuthority:
    return RepositorySelectorAuthority(
        selectorId="repository-test-selector",
        schemaVersion="repository-selector-result/v2",
        version="2.0.0",
        configurationDigest=content_digest({"selector": "inline"}),
        inputUniverse=("typescript tracked inputs",),
        externalInputs=(),
        outputArtifacts=("selected-tests",),
        workingDirectory=".",
        command=(
            "sh",
            "scripts/select-tests.sh",
            "{selector-output}",
            "{selection-mode}",
            "{diff-base}",
            "{candidate-kind}",
            "{candidate-value}",
            "{selector-id}",
            "{selector-version}",
            "{selector-configuration-digest}",
        ),
        resultPath=".certification/selected-tests.json",
    )


def _inline_executor() -> DaggerModuleExecutorDefinition:
    return DaggerModuleExecutorDefinition(
        adapterId="certifying-dagger",
        version="0.21.8",
        executable="dagger",
        functionName="portable-certification",
        sourceArgument="source",
        repositoryBundleArgument="repository-bundle",
        diffBaseArgument="diff-base",
        memoryCapArgument="memory-cap-bytes",
        planArgument="execution-manifest",
        reportsField="reports",
        imageReference="example.invalid/runner:v1@sha256:" + "1" * 64,
        runtimeDigest=content_digest({"daggersrc": "inline"}),
        consumingGates=(1, 2, 3, 4),
    )


def _inline_decoder() -> JsonExitStatusDecoderDefinition:
    return JsonExitStatusDecoderDefinition(
        decoderId="terminal-result",
        artifactPath="result.json",
        statusField="status",
        exitCodeField="exit-code",
        passedValue="passed",
        failedValue="failed",
        consumingGates=(1, 2, 3, 4),
    )


def _inline_publications() -> tuple[PublishedArtifactDefinition, ...]:
    return (
        PublishedArtifactDefinition(
            path="result.json",
            mediaType="application/json",
            maxBytes=4096,
            publisherGates=(1, 2, 3, 4),
        ),
        PublishedArtifactDefinition(
            path="node-suite.json",
            mediaType="application/json",
            maxBytes=4096,
            publisherGates=(2,),
        ),
        PublishedArtifactDefinition(
            path="node-coverage.json",
            mediaType="application/json",
            maxBytes=4096,
            publisherGates=(2,),
        ),
        PublishedArtifactDefinition(
            path="node-e2e.json",
            mediaType="application/json",
            maxBytes=4096,
            publisherGates=(4,),
        ),
    )


def _inline_rail(spec: _InlineRailSpec) -> RepositoryRailDefinition:
    return RepositoryRailDefinition(
        identity=RailIdentity(railId=spec.rail_id, version="1.0.0"),
        gate=spec.gate,
        railClass=spec.rail_class,
        ownerClass="repository-maintainer",
        correctiveOwner="repository-maintainer",
        posture="enforcing",
        orderKey=spec.rail_id,
        prerequisites=tuple(
            RailIdentity(railId=identity, version="1.0.0") for identity in spec.prerequisites
        ),
        requiredArtifacts=spec.required_artifacts,
        runtimeInputs=RailRuntimeInputs(
            runtimeIdentity="node-playwright-v1.60.0-noble",
            toolchainDigest=content_digest({"runtimeIdentity": "node-playwright-v1.60.0-noble"}),
            imageDigest="1" * 64,
            lockDigest=content_digest({"lock": "inline"}),
            environmentDigest=content_digest({"required": ["CI"]}),
            secretPolicyDigest=content_digest({"policy": "no-secrets"}),
        ),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId=f"{spec.rail_id}-evidence",
                mediaType="application/json",
                maxBytes=4096,
            ),
        ),
        outputArtifacts=tuple(
            ArtifactDeclaration(
                artifactId=artifact,
                schemaVersion="fixture-artifact/v1",
                mediaType="application/json",
            )
            for artifact in spec.output_artifacts
        ),
        execution=RepositoryRailExecution(
            adapterId=f"{spec.rail_id}-command",
            workingDirectory=".",
            command=spec.command,
            environmentContract=("CI",),
            scopeProviderId=spec.selector,
            inputSelectors=("tracked-inputs",) if spec.selector else (),
            resultDecoderId="terminal-result",
            timeoutSeconds=600,
            resourcePolicyId="fixture-bounded",
            cleanRoom=spec.clean_room,
            teardownPolicy="always" if spec.clean_room else "not-required",
            executionEvidence=f"profile://rails/{spec.rail_id}",
        ),
    )


def _inline_selection(
    selection_id: str,
    purpose: ProfilePurpose,
    mode: ProfileMode,
    rail_ids: tuple[RailIdentity, ...],
) -> RepositoryProfileSelection:
    gates = tuple(
        RepositoryGateSelection(
            gate=gate,
            status="applicable",
            railIds=tuple(identity for identity in rail_ids if _inline_rail_gate(identity) == gate),
            selectionIdentity=f"{selection_id}:gate-{gate}",
            population=f"declared Gate {gate} population",
        )
        for gate in (1, 2, 3, 4)
    )
    return RepositoryProfileSelection(
        selectionId=selection_id,
        purpose=purpose,
        mode=mode,
        executorAdapterId="certifying-dagger",
        resultDecoderId="terminal-result",
        gates=gates,
    )


def _inline_rail_gate(identity: RailIdentity) -> int:
    return {
        "static-quality": 1,
        "ordinary-suite": 2,
        "post-suite-quality": 3,
        "clean-room-e2e": 4,
    }[identity.railId]


# ---------------------------------------------------------------------------
# R21 certificate scenario (same authoritative fixture as the R21 leaf tests)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scenario:
    registry: CanonicalRailRegistry
    plan: CertificationPlan
    admission: CertificationAdmissionManifest


def _provenance(marker: str = "one") -> CreationProvenance:
    return CreationProvenance(
        createdAt=f"2026-09-02T00:00:0{1 if marker == 'one' else 2}+00:00",
        producer=f"test-{marker}",
        evidenceRef=f"evidence://{marker}",
    )


def _scenario() -> _Scenario:
    raw_profile = _inline_profile()
    profile = canonicalize_repository_profile(raw_profile)
    repository_plan = compile_repository_profile_plan(
        profile,
        selection_id=_PROFILE_ID,
        candidate_identity=_CANDIDATE,
    )
    repository_rails = tuple(rail for gate in repository_plan.gates for rail in gate.rails)
    memory_prerequisite = repository_plan.gates[-1].rails[-1].identity
    registry = canonicalize_registry(
        RailRegistry(
            registryId="fixture-final-memory-coherence",
            repositoryId=profile.profile.repositoryId,
            profiles=(
                RegistryProfile(
                    profileId=_PROFILE_ID,
                    kind="certifying",
                    gates=_ALL_GATES,
                ),
            ),
            rails=(
                *(_registry_rail(rail) for rail in repository_rails),
                _memory_rail(memory_prerequisite),
            ),
        )
    )
    plan = compile_certification_plan(
        registry,
        profile_id=_PROFILE_ID,
        candidate_identity=_CANDIDATE,
    )
    admission = compile_certification_admission(
        registry,
        plan,
        profile,
        repository_plan,
        provenance=_provenance(),
    )
    return _Scenario(registry, plan, admission)


def _registry_rail(rail: CompiledRail) -> RailDefinition:
    return RailDefinition(
        identity=rail.identity,
        gate=rail.gate,
        railClass=rail.railClass,
        authority=rail.authority,
        ownerClass=rail.ownerClass,
        correctiveOwner=rail.correctiveOwner,
        posture=rail.posture,
        orderKey=rail.orderKey,
        prerequisites=rail.prerequisites,
        requiredArtifacts=rail.requiredArtifacts,
        adapter=rail.adapter,
        runtimeInputs=rail.runtimeInputs,
        applicability=(rail.applicability,),
        evidenceContract=rail.evidenceContract,
        outputArtifacts=rail.outputArtifacts,
    )


def _memory_rail(prerequisite: RailIdentity) -> RailDefinition:
    return RailDefinition(
        identity=RailIdentity(railId="memory-coherence", version="1.0.0"),
        gate=5,
        railClass="memory-quality",
        authority="memory-domain",
        ownerClass="memory-curator",
        correctiveOwner="memory-curator",
        posture="enforcing",
        orderKey="memory-coherence",
        prerequisites=(prerequisite,),
        adapter=RailAdapterDefinition(
            adapterKind="memory-checker",
            adapterId="memory-coherence-checker",
            configurationDigest=_DIGEST,
            executionEvidence="memory-checker://coherence",
        ),
        runtimeInputs=RailRuntimeInputs(runtimeIdentity="memory-quality-v1"),
        applicability=(
            RailApplicability(
                profileId=_PROFILE_ID,
                status="applicable",
                selectionIdentity="memory-affected-closure",
                population="exact candidate-pair affected memory closure",
            ),
        ),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId="memory-coherence-evidence",
                mediaType="application/json",
                maxBytes=4096,
            ),
        ),
    )


def _gate(plan: CertificationPlan, gate: GateId) -> GatePlan:
    return next(item for item in plan.gates if item.gate == gate)


def _manifest(
    scenario: _Scenario, gate: GateId, *, status: RailStatus = "pass"
) -> GateResultManifest:
    gate_plan = _gate(scenario.plan, gate)
    results = []
    for rail in gate_plan.rails:
        artifacts = (
            tuple(
                RailArtifactResult(
                    artifactId=item.artifactId,
                    sha256=content_digest({"artifact": item.artifactId}),
                    size=32,
                    evidenceRef=f"artifact://{item.artifactId}",
                )
                for item in rail.outputArtifacts
            )
            if status == "pass"
            else ()
        )
        observation = RailTerminalObservation(
            rail=rail.identity,
            status=status,
            code=f"{rail.identity.railId}-{status}",
            artifacts=artifacts,
            evidence=tuple(
                RailEvidenceReference(
                    evidenceId=item.evidenceId,
                    sha256=content_digest({"evidence": item.evidenceId}),
                    size=32,
                    reference=f"evidence://{item.evidenceId}",
                )
                for item in rail.evidenceContract
            ),
        )
        results.append(build_rail_result(gate_plan, observation))
    return compile_gate_result_manifest(
        scenario.registry,
        scenario.plan,
        gate_plan,
        results,
        GateResultAdmission(
            profileId=_PROFILE_ID,
            candidateIdentity=_CANDIDATE,
            altitude=scenario.plan.profileKind,
        ),
    )


def _green_prefix(scenario: _Scenario) -> tuple[GateCertificate, ...]:
    certificates: list[GateCertificate] = []
    for gate in (1, 2, 3, 4):
        certificates.append(
            compile_gate_certificate(
                scenario.admission,
                _gate(scenario.plan, gate),
                _manifest(scenario, gate),
                certificates,
                GateCertificateIssuanceContext(provenance=_provenance()),
            )
        )
    return tuple(certificates)


# ---------------------------------------------------------------------------
# R07 affected-closure plan fixture (same synthetic scope as the R07 leaf tests)
# ---------------------------------------------------------------------------

_R07_GATE_PREFIX: tuple[GateId, ...] = (1, 2, 3, 4)


@dataclass
class _StableAuthority:
    candidate: ScopeCandidateIdentity
    calls: int = 0

    def observe(self) -> ScopeCandidateIdentity:
        self.calls += 1
        return self.candidate


@dataclass(frozen=True)
class _StaticDependencies:
    snapshot: DependencySnapshot

    def observe(self, candidate: ScopeCandidateIdentity) -> DependencySnapshot:
        del candidate
        return self.snapshot


def _r07_candidate(*, code_tree: str = _CODE_TREE, memory_tree: str = _MEMORY_TREE):
    pair = MemoryCandidatePairIdentity(
        repoId="repo",
        contractPath="/coord/tasks/repo/master/enclosures/leaf/series-contract.md",
        contractDigest="d" * 64,
        codeRoot="/work/code",
        memoryRoot="/work/memory",
        codeSourceBranch="main",
        codeWorkBranch="work",
        codeBaseCommit="1" * 40,
        memorySourceBranch="main-memory",
        memoryWorkBranch="work-memory",
        memoryBaseCommit="2" * 40,
        onboardingRoot="/work/memory/onboarding",
        ledgerPath="/work/memory/memory.md",
    )
    observation = CanonicalTaskObservation(
        taskRoot="/coord/tasks/repo/master",
        taskDocumentRef=TaskDocumentRef(repository="repo", path="master/leaf.json"),
        sourceDigest="a" * 64,
        sourceAuthorityNamespace="agents-remember.task-document-source",
        sourceValidatorVersion="fixture/v1",
        semanticTopologyDigest="b" * 64,
        taskIntent=TaskIntentIdentity(digest="c" * 64),
    )
    candidate = ScopeCandidateIdentity(
        pairIdentity=pair,
        code=GitTreeDelta(
            namespace="code",
            root=pair.codeRoot,
            baseTree="3" * 40,
            candidateTree=code_tree,
            changes=(
                GitPathChange(
                    status="modified",
                    oldPath="src/a.py",
                    newPath="src/a.py",
                    oldBlob="5" * 40,
                    newBlob="6" * 40,
                ),
            ),
        ),
        memory=GitTreeDelta(
            namespace="memory",
            root=pair.memoryRoot,
            baseTree="7" * 40,
            candidateTree=memory_tree,
            changes=(),
        ),
        task=TaskObservationPair(base=observation, candidate=observation),
    )
    return candidate, pair


def _r07_node(node_id: str) -> ScopeNode:
    return ScopeNode(
        nodeId=node_id,
        contentDigest=canonical_digest({"node": node_id}),
        authorityNamespace=(
            "agents-remember.git-code-tree"
            if node_id.startswith("code:")
            else "agents-remember.git-memory-tree"
        ),
        validatorVersion="fixture/v1",
        reasons=("owner fixture",),
    )


def _r07_scope(candidate: ScopeCandidateIdentity) -> ScopeManifest:
    nodes = (
        _r07_node("code:src/a.py"),
        _r07_node("memory:onboarding/claims.md"),
        _r07_node("memory:onboarding/src/a.py.md"),
    )
    edges = (
        dependency_edge(
            "code:src/a.py",
            "memory:onboarding/src/a.py.md",
            "source-to-file-sidecar",
            ("sidecar",),
        ),
        dependency_edge(
            "memory:onboarding/src/a.py.md",
            "memory:onboarding/claims.md",
            "source-to-citing-memory-document",
            ("citation",),
        ),
    )
    source_index = SourceIndexObservation(
        snapshotId="e" * 64,
        codeRoot=candidate.code.root,
        memoryRoot=candidate.memory.root,
        candidateDigest=candidate.digest,
    )
    snapshot = build_dependency_snapshot(
        candidate=candidate,
        source_index=source_index,
        nodes=nodes,
        edges=edges,
    )
    return compile_scope_manifest(
        _StableAuthority(candidate),
        _StaticDependencies(snapshot),
        checkers=AVAILABLE_CHECKS,
    )


def _r07_admission(monkeypatch: pytest.MonkeyPatch, candidate: ScopeCandidateIdentity):
    identities = tuple(
        GateCertificateIdentity(gate=gate, certificateDigest=str(gate) * 64)
        for gate in _R07_GATE_PREFIX
    )
    monkeypatch.setattr(affected_planning, "plan_certificate_reuse", _reuse(identities))
    certificate_admission = cast(
        CertificationAdmissionManifest,
        SimpleNamespace(
            semanticEnvelope=SimpleNamespace(
                candidateCodeTree=SimpleNamespace(value=candidate.code.candidateTree)
            )
        ),
    )
    gate_certificates = cast(
        tuple[GateCertificate, ...],
        tuple(
            SimpleNamespace(
                semanticEnvelope=SimpleNamespace(gate=identity.gate),
                identity=identity,
            )
            for identity in identities
        ),
    )
    return AffectedClosureAdmission(
        candidateAuthority=_StableAuthority(candidate),
        certificationAdmission=certificate_admission,
        gateCertificates=gate_certificates,
        changes=(),
    )


def _reuse(identities: tuple[GateCertificateIdentity, ...]):
    def reuse(*args, **kwargs):
        del args, kwargs
        return CertificateReusePlan(
            reusedCertificates=identities,
            invalidatedGates=(5,),
            firstGateToRun=5,
            finalizationRevalidationRequired=True,
            zeroGateStarts=False,
        )

    return reuse


def _affected_plan() -> tuple[ScopeCandidateIdentity, AffectedClosurePlan]:
    candidate, _pair = _r07_candidate()
    plan = compile_affected_closure_plan(
        _r07_admission(pytest.MonkeyPatch(), candidate),
        _r07_scope(candidate),
    )
    return candidate, plan


# ---------------------------------------------------------------------------
# Minimal current curator-coherence authority fixture
# ---------------------------------------------------------------------------


def _pair() -> MemoryCandidatePairIdentity:
    _candidate, pair = _r07_candidate()
    return pair


def _coherence(pair: MemoryCandidatePairIdentity) -> ValidatedCuratorCoherence:
    record = CuratorCoherenceRecord(
        leafId="leaf",
        contractPath="/coord/tasks/repo/master/enclosures/leaf/series-contract.md",
        taskDocumentRef=TaskDocumentRef(repository="repo", path="master/leaf.json"),
        semanticRequirementRevision="CCR-R08@v3",
        deliveryAttempt="attempt-1",
        pairIdentity=pair,
        codeCandidateTree=_CODE_TREE,
        memoryCandidateTree=_MEMORY_TREE,
        taskTopologyFingerprint="12" * 32,
        taskIntent=TaskIntentIdentity(digest="c" * 64),
        attestationPath="/work/group/reports/curator-memory-quality.json",
        attestationSha256="a" * 64,
        attestationReportSha256="b" * 64,
        sourceCandidates=[],
        judgments=[],
        dependencies=None,
        predecessorAuthorityDigest="",
        publicationFingerprint="34" * 32,
        publishedBy="curator@leaf",
        reportSha256="56" * 32,
    )
    record_digest = content_digest(record.model_dump(mode="json", by_alias=True))
    return ValidatedCuratorCoherence(
        authority=SimpleNamespace(),  # type: ignore[arg-type]
        record=record,
        record_path=Path("/work/reports/generations/record.json"),
        report_path=Path("/work/reports/generations/report.md"),
        record_digest=record_digest,
        evidence=[],
    )


def _passing_checks() -> dict[str, dict[str, object]]:
    return {
        item.itemId: {"ok": True, "findingCount": 0}
        for item in complete_final_catalog()
        if item.itemId.startswith(("integrity.", "style."))
    }


# Final certification orchestration
# ---------------------------------------------------------------------------


_DEFAULT_COHERENCE_SENTINEL = object()


@dataclass(frozen=True)
class _EvidenceSpec:
    coherence: ValidatedCuratorCoherence | object | None = _DEFAULT_COHERENCE_SENTINEL
    changes: tuple[CertificateInputChange, ...] = ()
    failing_checks: tuple[str, ...] = ()
    full_only_rerun: bool = True
    missing_onboarding_count: int = 0
    stale_route_index_count: int = 0
    code_tree: str = _CODE_TREE


def _evidence(spec: _EvidenceSpec | None = None) -> FinalCertificationEvidence:
    spec = _EvidenceSpec() if spec is None else spec
    scenario = _scenario()
    certificates = _green_prefix(scenario)
    _candidate, plan = _affected_plan()
    if spec.coherence is _DEFAULT_COHERENCE_SENTINEL:
        resolved_coherence: ValidatedCuratorCoherence | None = _coherence(_pair())
    elif spec.coherence is None:
        resolved_coherence = None
    else:
        resolved_coherence = cast(ValidatedCuratorCoherence, spec.coherence)
    checks = _passing_checks()
    for failing_check in spec.failing_checks:
        checks[failing_check] = {"ok": False, "findingCount": 1}
    return FinalCertificationEvidence(
        admission=scenario.admission,
        gate_certificates=certificates,
        changes=spec.changes,
        candidate_code_tree=spec.code_tree,
        memory_tree=_MEMORY_TREE,
        pair_identity=_pair(),
        affected_closure=plan,
        coherence=resolved_coherence,
        executed_checks=checks,
        missing_onboarding_count=spec.missing_onboarding_count,
        stale_route_index_count=spec.stale_route_index_count,
        full_only_rerun=spec.full_only_rerun,
    )


def test_final_certification_green_binds_exact_pair_and_gate_five_inputs() -> None:
    evidence = _evidence()

    result = certify_final_full_memory_coherence(evidence)

    assert result.state == "green"
    assert result.finalizationEligible is True
    assert result.attestation.ok is True
    assert result.coherenceRecordDigest is not None
    assert tuple(item.gate for item in result.reusedGateOneToFour) == (1, 2, 3, 4)
    assert result.plan.memoryTree == _MEMORY_TREE
    assert result.gateFiveInputs is not None
    assert result.gateFiveInputs.memoryTree.value == _MEMORY_TREE
    assert (
        result.gateFiveInputs.candidatePairAuthorityDigest == evidence.pair_identity.contractDigest
    )
    assert result.gateFiveInputs.affectedClosurePlanDigest == result.plan.affectedClosurePlanDigest


def test_final_certification_red_blocks_finalization() -> None:
    evidence = _evidence(
        _EvidenceSpec(failing_checks=(tables.CHECK_NAME,), missing_onboarding_count=1)
    )

    result = certify_final_full_memory_coherence(evidence)

    assert result.state == "red"
    assert result.finalizationEligible is False
    assert result.gateFiveInputs is None
    assert result.reason == "at least one final catalog item failed"


def test_final_certification_blocked_when_full_only_rerun_not_consumed() -> None:
    evidence = _evidence(_EvidenceSpec(full_only_rerun=False))

    result = certify_final_full_memory_coherence(evidence)

    assert result.state == "blocked"
    assert result.finalizationEligible is False
    assert result.attestation.statusCounts["blocked"] == 1


def test_final_certification_refuses_without_current_coherence() -> None:
    evidence = _evidence(_EvidenceSpec(coherence=None))

    with pytest.raises(FinalCertificationError) as caught:
        certify_final_full_memory_coherence(evidence)
    assert caught.value.status == "gate-five-coherence-blocked"


def test_final_certification_refuses_stale_prefix_before_any_catalog_work() -> None:
    evidence = _evidence(
        _EvidenceSpec(changes=(CertificateInputChange(changeClass="code", reason="code changed"),))
    )

    with pytest.raises(FinalCertificationError) as caught:
        certify_final_full_memory_coherence(evidence)
    assert caught.value.status == "gate-five-prefix-invalidated"
