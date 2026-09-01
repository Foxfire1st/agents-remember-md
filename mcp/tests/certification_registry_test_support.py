"""Single repository-neutral composition owner for certification contract tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from agents_remember.certification import (
    build_rail_result,
    canonicalize_registry,
    compile_certification_plan,
    compile_gate_result_manifest,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    ArtifactDeclaration,
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationPlan,
    GateId,
    GatePlan,
    GateResultAdmission,
    GateResultManifest,
    ProfileKind,
    RailAdapterDefinition,
    RailApplicability,
    RailArtifactResult,
    RailClass,
    RailDefinition,
    RailEvidenceContract,
    RailEvidenceReference,
    RailIdentity,
    RailPosture,
    RailRegistry,
    RailResult,
    RailRuntimeInputs,
    RailStatus,
    RailTerminalObservation,
    RegistryProfile,
)
from agents_remember.errors import CertificationContractError

_DIGEST = "a" * 64
_CANDIDATE = CandidateIdentity(kind="content-digest", value="c" * 64)
_CLASS_BY_GATE: dict[GateId, RailClass] = {
    1: "pre-test-quality",
    2: "ordinary-test-suite",
    3: "post-test-quality",
    4: "integration-test",
    5: "memory-quality",
}


@dataclass(frozen=True)
class RailSpec:
    rail_id: str
    gate: GateId
    prerequisites: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    output_artifacts: tuple[str, ...] = ()
    profile_id: str = "portable-ci"
    applicability: Literal["applicable", "not-applicable"] = "applicable"
    posture: RailPosture = "enforcing"


@dataclass(frozen=True)
class ObservationSpec:
    rail_id: str
    status: RailStatus = "pass"
    blocked_by: tuple[str, ...] = ()
    include_artifacts: bool = True
    evidence_size: int = 16
    evidence_id: str | None = None


def _identity(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def _rail(spec: RailSpec) -> RailDefinition:
    applicable = spec.applicability == "applicable"
    return RailDefinition(
        identity=_identity(spec.rail_id),
        gate=spec.gate,
        railClass=_CLASS_BY_GATE[spec.gate],
        authority="memory-domain" if spec.gate == 5 else "repository-profile",
        ownerClass="portable-owner",
        correctiveOwner="portable-owner",
        posture=spec.posture,
        orderKey=spec.rail_id,
        prerequisites=tuple(_identity(item) for item in spec.prerequisites),
        requiredArtifacts=spec.required_artifacts,
        adapter=RailAdapterDefinition(
            adapterKind="process-adapter",
            adapterId=f"{spec.rail_id}-adapter",
            configurationDigest=_DIGEST,
            executionEvidence=f"adapter://{spec.rail_id}",
        ),
        runtimeInputs=RailRuntimeInputs(runtimeIdentity="portable-runtime"),
        applicability=(
            RailApplicability(
                profileId=spec.profile_id,
                status=spec.applicability,
                selectionIdentity=f"selection:{spec.rail_id}",
                population="portable population" if applicable else None,
                reason=None if applicable else "repository does not use this rail",
            ),
        ),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId=f"{spec.rail_id}-evidence",
                mediaType="application/json",
                maxBytes=128,
            ),
        ),
        outputArtifacts=tuple(
            ArtifactDeclaration(
                artifactId=artifact_id,
                schemaVersion="artifact/v1",
                mediaType="application/json",
            )
            for artifact_id in spec.output_artifacts
        ),
    )


def _portable_specs() -> tuple[RailSpec, ...]:
    return (
        RailSpec("lint", 1),
        RailSpec("types", 1),
        RailSpec("package", 1, prerequisites=("lint",)),
        RailSpec("suite", 2, prerequisites=("types",), output_artifacts=("suite-data",)),
        RailSpec(
            "coverage",
            3,
            prerequisites=("suite",),
            required_artifacts=("suite-data",),
        ),
        RailSpec("clean-room", 4, prerequisites=("coverage",)),
        RailSpec("memory", 5, prerequisites=("clean-room",)),
    )


def _registry(
    specs: tuple[RailSpec, ...] | None = None,
    *,
    profile: RegistryProfile | None = None,
) -> RailRegistry:
    selected_profile = profile or RegistryProfile(
        profileId="portable-ci",
        kind="certifying",
        gates=(5, 3, 1, 4, 2),
    )
    return RailRegistry(
        registryId="portable-closeout",
        repositoryId="sample-repository",
        profiles=(selected_profile,),
        rails=tuple(_rail(spec) for spec in (specs or _portable_specs())),
    )


def _canonical_registry(registry: RailRegistry | None = None) -> CanonicalRailRegistry:
    return canonicalize_registry(registry or _registry())


def _plan(registry: RailRegistry | None = None) -> CertificationPlan:
    canonical = _canonical_registry(registry)
    return compile_certification_plan(
        canonical,
        profile_id=canonical.registry.profiles[0].profileId,
        candidate_identity=_CANDIDATE,
    )


def _gate(plan: CertificationPlan, gate: GateId) -> GatePlan:
    return next(item for item in plan.gates if item.gate == gate)


def _result(gate_plan: GatePlan, spec: ObservationSpec) -> RailResult:
    compiled_by_id = {item.identity.railId: item for item in gate_plan.rails}
    compiled = compiled_by_id[spec.rail_id]
    artifacts = (
        tuple(
            RailArtifactResult(
                artifactId=item.artifactId,
                sha256=_DIGEST,
                size=16,
                evidenceRef=f"artifact://{item.artifactId}",
            )
            for item in compiled.outputArtifacts
        )
        if spec.include_artifacts
        else ()
    )
    evidence_id = spec.evidence_id or compiled.evidenceContract[0].evidenceId
    observation = RailTerminalObservation(
        rail=compiled.identity,
        status=spec.status,
        code=f"{spec.rail_id}-{spec.status}",
        blockedBy=tuple(compiled_by_id[item].identity for item in spec.blocked_by),
        artifacts=artifacts,
        evidence=(
            RailEvidenceReference(
                evidenceId=evidence_id,
                sha256=_DIGEST,
                size=spec.evidence_size,
                reference=f"evidence://{spec.rail_id}",
            ),
        ),
    )
    return build_rail_result(gate_plan, observation)


def _manifest(
    plan: CertificationPlan,
    gate_plan: GatePlan,
    results: Sequence[RailResult],
    *,
    altitude: ProfileKind,
    registry: RailRegistry | None = None,
) -> GateResultManifest:
    canonical = _canonical_registry(registry)
    admission = GateResultAdmission(
        profileId=canonical.registry.profiles[0].profileId,
        candidateIdentity=_CANDIDATE,
        altitude=altitude,
    )
    return compile_gate_result_manifest(canonical, plan, gate_plan, results, admission)


def _finding_codes(error: CertificationContractError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


def _rebuild_gate_plan(gate_plan: GatePlan, **updates: object) -> GatePlan:
    payload = gate_plan.model_dump(mode="json", exclude={"planDigest"})
    payload.update(updates)
    return GatePlan.model_validate({**payload, "planDigest": content_digest(payload)})


def _rebuild_certification_plan(
    plan: CertificationPlan,
    gates: tuple[GatePlan, ...],
) -> CertificationPlan:
    payload = plan.model_dump(mode="json", exclude={"planDigest"})
    payload["gates"] = [gate.model_dump(mode="json") for gate in gates]
    return CertificationPlan.model_validate({**payload, "planDigest": content_digest(payload)})


def _gate_one_graph_registry(size: int, *, dense: bool) -> RailRegistry:
    rail_ids = [f"node-{index:04d}" for index in range(size)]
    specs = tuple(
        RailSpec(
            rail_id,
            1,
            prerequisites=tuple(
                rail_ids[max(0, index - 256) : index]
                if dense
                else rail_ids[max(0, index - 1) : index]
            ),
        )
        for index, rail_id in enumerate(rail_ids)
    )
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    return _registry(specs, profile=profile)


def _artifact_chain_registry(size: int, *, extra_evidence: int = 0) -> RailRegistry:
    rail_ids = [f"node-{index:04d}" for index in range(size)]
    rails = [
        _rail(
            RailSpec(
                rail_id,
                1,
                prerequisites=tuple(rail_ids[index - 1 : index]),
                required_artifacts=() if index == 0 else ("root-artifact",),
                output_artifacts=("root-artifact",) if index == 0 else (),
            )
        )
        for index, rail_id in enumerate(rail_ids)
    ]
    root = rails[0]
    rails[0] = root.model_copy(
        update={
            "evidenceContract": (
                *root.evidenceContract,
                *tuple(
                    RailEvidenceContract(
                        evidenceId=f"root-extra-{index}",
                        mediaType="application/json",
                        maxBytes=128,
                    )
                    for index in range(extra_evidence)
                ),
            )
        }
    )
    return RailRegistry(
        registryId="artifact-chain",
        repositoryId="sample-repository",
        profiles=(RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,)),),
        rails=tuple(rails),
    )


def _distinct_artifact_chain_registry(size: int) -> RailRegistry:
    """Build the reviewer counterexample with many adjacent producer queries."""

    producer_count = (size * 2) // 3
    rail_ids = [f"node-{index:04d}" for index in range(size)]
    specs = tuple(
        RailSpec(
            rail_id,
            1,
            prerequisites=tuple(rail_ids[index - 1 : index]),
            required_artifacts=(f"artifact-{index - 1:04d}",)
            if 0 < index <= producer_count
            else (),
            output_artifacts=(f"artifact-{index:04d}",) if index < producer_count else (),
        )
        for index, rail_id in enumerate(rail_ids)
    )
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    return _registry(specs, profile=profile)


def _self_artifact_registry(size: int) -> RailRegistry:
    """Build one exact self-produced/self-consumed artifact query per rail."""

    specs = tuple(
        RailSpec(
            f"self-{index:04d}",
            1,
            required_artifacts=(f"self-artifact-{index:04d}",),
            output_artifacts=(f"self-artifact-{index:04d}",),
        )
        for index in range(size)
    )
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    return _registry(specs, profile=profile)


def _dense_self_artifact_registry(size: int) -> RailRegistry:
    """Build a bounded-dense graph with one exact self artifact query."""

    registry = _gate_one_graph_registry(size, dense=True)
    rails = list(registry.rails)
    root = rails[0]
    rails[0] = root.model_copy(
        update={
            "requiredArtifacts": ("dense-self-artifact",),
            "outputArtifacts": (
                ArtifactDeclaration(
                    artifactId="dense-self-artifact",
                    schemaVersion="artifact/v1",
                    mediaType="application/json",
                ),
            ),
        }
    )
    return registry.model_copy(update={"rails": tuple(rails)})


def _pad_evidence_work_units(registry: RailRegistry, units: int) -> RailRegistry:
    """Add legal inert evidence declarations to reach an exact work boundary."""

    remaining = units
    rails = []
    for rail in registry.rails:
        additions = min(remaining, 128 - len(rail.evidenceContract))
        start_index = len(rail.evidenceContract)
        evidence = tuple(
            RailEvidenceContract(
                evidenceId=f"{rail.identity.railId}-padding-{index:03d}",
                mediaType="application/json",
                maxBytes=128,
            )
            for index in range(start_index, start_index + additions)
        )
        rails.append(
            rail.model_copy(update={"evidenceContract": (*rail.evidenceContract, *evidence)})
        )
        remaining -= additions
    if remaining:
        raise ValueError("registry schema has insufficient evidence capacity for padding")
    return registry.model_copy(update={"rails": tuple(rails)})


def _raw_declaration_overflow_registry() -> RailRegistry:
    """Reach exactly 131,073 raw units with shared immutable declarations."""

    base = _rail(RailSpec("overflow", 1))
    evidence = base.evidenceContract[0]
    saturated = base.model_copy(update={"evidenceContract": (evidence,) * 128})
    partial = base.model_copy(update={"evidenceContract": (evidence,) * 29})
    return RailRegistry(
        registryId="raw-declaration-overflow",
        repositoryId="sample-repository",
        profiles=(RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,)),),
        rails=(*((saturated,) * 1008), partial),
    )


def _pad_inert_output_declarations(registry: RailRegistry, units: int) -> RailRegistry:
    """Add unique unconsumed outputs, one declaration unit each for graph-peak fixtures."""

    remaining = units
    next_index = 0
    rails = []
    for rail in registry.rails:
        additions = min(remaining, 256 - len(rail.outputArtifacts))
        artifacts = tuple(
            ArtifactDeclaration(
                artifactId=f"inert-output-{index:05d}",
                schemaVersion="artifact/v1",
                mediaType="application/json",
            )
            for index in range(next_index, next_index + additions)
        )
        rails.append(
            rail.model_copy(update={"outputArtifacts": (*rail.outputArtifacts, *artifacts)})
        )
        next_index += additions
        remaining -= additions
    if remaining:
        raise ValueError("registry schema has insufficient output capacity for padding")
    return registry.model_copy(update={"rails": tuple(rails)})


def _artifact_query_cross_product_registry(
    producer_count: int,
    consumer_count: int,
) -> RailRegistry:
    """Build an invalid hostile product without allocating its reachability pairs."""

    producers = tuple(
        RailSpec(f"producer-{index:04d}", 1, output_artifacts=("shared-artifact",))
        for index in range(producer_count)
    )
    consumers = tuple(
        RailSpec(f"consumer-{index:04d}", 1, required_artifacts=("shared-artifact",))
        for index in range(consumer_count)
    )
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    return _registry((*producers, *consumers), profile=profile)
