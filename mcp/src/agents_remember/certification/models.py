"""Strict, repository-neutral contracts for the five closeout gates."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from agents_remember.certification.digests import content_digest

GateId = Literal[1, 2, 3, 4, 5]
ProfileKind = Literal["certifying", "diagnostic"]
RailAuthority = Literal["repository-profile", "memory-domain"]
RailClass = Literal[
    "pre-test-quality",
    "ordinary-test-suite",
    "post-test-quality",
    "integration-test",
    "memory-quality",
]
RailPosture = Literal["enforcing", "report-only"]
RailStatus = Literal["pass", "fail", "blocked", "not-applicable"]

_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


def _require_semantic_text(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("semantic text must be nonblank and unpadded")
    return value


SemanticText = Annotated[str, AfterValidator(_require_semantic_text)]
ArtifactId = Annotated[str, Field(pattern=_ID_PATTERN, max_length=128)]


class FrozenContractModel(BaseModel):
    """Closed immutable base for registry, plan, and result records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateIdentity(FrozenContractModel):
    """Repository-neutral exact identity for the candidate a plan certifies."""

    kind: str = Field(pattern=_ID_PATTERN, max_length=128)
    value: SemanticText = Field(max_length=4096)


class RailIdentity(FrozenContractModel):
    railId: str = Field(pattern=_ID_PATTERN, max_length=128)
    version: SemanticText = Field(pattern=_VERSION_PATTERN)

    @property
    def key(self) -> str:
        return f"{self.railId}@{self.version}"


class ArtifactDeclaration(FrozenContractModel):
    artifactId: ArtifactId
    schemaVersion: SemanticText = Field(max_length=128)
    mediaType: SemanticText = Field(max_length=256)
    requiredOnPass: bool = True


class RailAdapterDefinition(FrozenContractModel):
    adapterKind: str = Field(pattern=_ID_PATTERN, max_length=128)
    adapterId: str = Field(pattern=_ID_PATTERN, max_length=256)
    configurationDigest: str = Field(pattern=_DIGEST_PATTERN)
    executionEvidence: SemanticText = Field(max_length=32768)


class RailRuntimeInputs(FrozenContractModel):
    runtimeIdentity: SemanticText | None = Field(default=None, max_length=512)
    toolchainDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    imageDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    lockDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    environmentDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    secretPolicyDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)


class RailEvidenceContract(FrozenContractModel):
    evidenceId: str = Field(pattern=_ID_PATTERN, max_length=128)
    mediaType: SemanticText = Field(max_length=256)
    maxBytes: int = Field(gt=0, le=1_000_000_000)


class RailApplicability(FrozenContractModel):
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    status: Literal["applicable", "not-applicable"]
    selectionIdentity: SemanticText = Field(max_length=1024)
    population: SemanticText | None = Field(default=None, max_length=4096)
    reason: SemanticText | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def _require_status_payload(self) -> Self:
        if self.status == "applicable":
            if self.population is None:
                raise ValueError("applicable rail requires a nonblank population")
            if self.reason is not None:
                raise ValueError("applicable rail cannot carry a non-applicability reason")
        elif self.population is not None or self.reason is None:
            raise ValueError("not-applicable rail requires only a nonblank reason")
        return self


class RailDefinition(FrozenContractModel):
    identity: RailIdentity
    gate: GateId
    railClass: RailClass
    authority: RailAuthority
    ownerClass: str = Field(pattern=_ID_PATTERN, max_length=128)
    correctiveOwner: str = Field(pattern=_ID_PATTERN, max_length=128)
    posture: RailPosture
    orderKey: SemanticText = Field(max_length=256)
    prerequisites: tuple[RailIdentity, ...] = Field(default_factory=tuple, max_length=256)
    requiredArtifacts: tuple[ArtifactId, ...] = Field(default_factory=tuple, max_length=256)
    adapter: RailAdapterDefinition
    runtimeInputs: RailRuntimeInputs
    applicability: tuple[RailApplicability, ...] = Field(min_length=1, max_length=128)
    evidenceContract: tuple[RailEvidenceContract, ...] = Field(
        min_length=1,
        max_length=128,
    )
    outputArtifacts: tuple[ArtifactDeclaration, ...] = Field(
        default_factory=tuple,
        max_length=256,
    )


class RegistryProfile(FrozenContractModel):
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    kind: ProfileKind
    gates: tuple[GateId, ...] = Field(min_length=1, max_length=5)


class RailRegistry(FrozenContractModel):
    schemaVersion: Literal["closeout-rail-registry/v1"] = "closeout-rail-registry/v1"
    registryId: str = Field(pattern=_ID_PATTERN, max_length=128)
    repositoryId: str = Field(pattern=_ID_PATTERN, max_length=128)
    profiles: tuple[RegistryProfile, ...] = Field(min_length=1, max_length=128)
    rails: tuple[RailDefinition, ...] = Field(min_length=1, max_length=4096)


class CanonicalRailRegistry(FrozenContractModel):
    schemaVersion: Literal["canonical-closeout-rail-registry/v1"] = (
        "canonical-closeout-rail-registry/v1"
    )
    registry: RailRegistry
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        expected = content_digest({"registry": self.registry.model_dump(mode="json")})
        if self.registryDigest != expected:
            raise ValueError("canonical rail registry digest does not match its content")
        return self


class CertificationContractFinding(FrozenContractModel):
    code: str = Field(pattern=_ID_PATTERN, max_length=128)
    path: SemanticText
    detail: SemanticText


class RegistryValidationFinding(CertificationContractFinding):
    """One fail-closed finding discovered while validating a registry."""


class RegistryValidationReport(FrozenContractModel):
    schemaVersion: Literal["closeout-rail-registry-validation/v1"] = (
        "closeout-rail-registry-validation/v1"
    )
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    findings: tuple[RegistryValidationFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


class CompiledRail(FrozenContractModel):
    identity: RailIdentity
    definitionDigest: str = Field(pattern=_DIGEST_PATTERN)
    gate: GateId
    railClass: RailClass
    authority: RailAuthority
    ownerClass: str = Field(pattern=_ID_PATTERN, max_length=128)
    correctiveOwner: str = Field(pattern=_ID_PATTERN, max_length=128)
    posture: RailPosture
    orderKey: SemanticText = Field(max_length=256)
    prerequisites: tuple[RailIdentity, ...]
    requiredArtifacts: tuple[ArtifactId, ...]
    adapter: RailAdapterDefinition
    runtimeInputs: RailRuntimeInputs
    applicability: RailApplicability
    evidenceContract: tuple[RailEvidenceContract, ...]
    outputArtifacts: tuple[ArtifactDeclaration, ...]


def canonical_execution_waves(
    rails: tuple[CompiledRail, ...],
) -> tuple[tuple[RailIdentity, ...], ...]:
    """Return the unique earliest topological partition for one gate catalog."""

    by_identity = {rail.identity.key: rail for rail in rails}
    prerequisites, dependants = _same_gate_dependencies(by_identity)
    ready = _ordered_compiled_rails(
        by_identity,
        (identity for identity, required in prerequisites.items() if not required),
    )
    waves: list[tuple[RailIdentity, ...]] = []
    resolved = 0
    while ready:
        waves.append(tuple(item.identity for item in ready))
        resolved += len(ready)
        ready = _ordered_compiled_rails(
            by_identity,
            _release_wave(ready, prerequisites, dependants),
        )
    if resolved != len(by_identity):
        raise ValueError("gate plan rails contain a same-gate dependency cycle")
    return tuple(waves)


def _same_gate_dependencies(
    catalog: dict[str, CompiledRail],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    prerequisites = {
        identity: {item.key for item in rail.prerequisites if item.key in catalog}
        for identity, rail in catalog.items()
    }
    dependants: dict[str, set[str]] = defaultdict(set)
    for identity, required in prerequisites.items():
        for prerequisite in required:
            dependants[prerequisite].add(identity)
    return prerequisites, dependants


def _release_wave(
    ready: list[CompiledRail],
    prerequisites: dict[str, set[str]],
    dependants: dict[str, set[str]],
) -> set[str]:
    next_ready: set[str] = set()
    for item in ready:
        for dependant in dependants[item.identity.key]:
            prerequisites[dependant].discard(item.identity.key)
            if not prerequisites[dependant]:
                next_ready.add(dependant)
    return next_ready


def _ordered_compiled_rails(
    catalog: dict[str, CompiledRail],
    identities: Iterable[str],
) -> list[CompiledRail]:
    return sorted(
        (catalog[identity] for identity in identities),
        key=lambda item: (item.orderKey, item.identity.key),
    )


class GatePlan(FrozenContractModel):
    schemaVersion: Literal["closeout-gate-plan/v1"] = "closeout-gate-plan/v1"
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    candidateIdentity: CandidateIdentity
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    profileKind: ProfileKind
    gate: GateId
    gatePrerequisites: tuple[GateId, ...]
    rails: tuple[CompiledRail, ...] = Field(min_length=1, max_length=4096)
    waves: tuple[tuple[RailIdentity, ...], ...] = Field(min_length=1, max_length=4096)
    planDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        _require_gate_plan_catalog(self)
        _require_gate_plan_waves(self)
        payload = self.model_dump(mode="json", exclude={"planDigest"})
        if self.planDigest != content_digest(payload):
            raise ValueError("gate plan digest does not match its content")
        return self


def _require_gate_plan_catalog(plan: GatePlan) -> None:
    rail_keys = [rail.identity.key for rail in plan.rails]
    if len(rail_keys) != len(set(rail_keys)):
        raise ValueError("gate plan rail identities must be unique")
    if any(rail.gate != plan.gate for rail in plan.rails):
        raise ValueError("gate plan rails must belong to the plan gate")
    if any(rail.applicability.profileId != plan.profileId for rail in plan.rails):
        raise ValueError("gate plan rail applicability must match the selected profile")
    expected_rails = tuple(
        sorted(
            plan.rails,
            key=lambda rail: (
                rail.orderKey,
                rail.identity.railId,
                rail.identity.version,
                rail.definitionDigest,
            ),
        )
    )
    if plan.rails != expected_rails:
        raise ValueError("gate plan rail catalog must use canonical deterministic order")
    expected_prerequisites = tuple(range(1, plan.gate))
    if plan.gatePrerequisites != expected_prerequisites:
        raise ValueError("gate prerequisites must be the exact complete earlier-gate prefix")


def _require_gate_plan_waves(plan: GatePlan) -> None:
    if plan.waves != canonical_execution_waves(plan.rails):
        raise ValueError("gate plan waves must equal the earliest topological partition")


class CertificationPlan(FrozenContractModel):
    schemaVersion: Literal["closeout-certification-plan/v1"] = "closeout-certification-plan/v1"
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    candidateIdentity: CandidateIdentity
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    profileKind: ProfileKind
    gates: tuple[GatePlan, ...] = Field(min_length=1, max_length=5)
    planDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        _require_certification_gate_catalog(self)
        _require_certification_gate_identities(self)
        payload = self.model_dump(mode="json", exclude={"planDigest"})
        if self.planDigest != content_digest(payload):
            raise ValueError("certification plan digest does not match its content")
        return self


def _require_certification_gate_catalog(plan: CertificationPlan) -> None:
    gate_ids = tuple(gate.gate for gate in plan.gates)
    if gate_ids != tuple(sorted(set(gate_ids))):
        raise ValueError("certification plan gates must be unique and ordered")
    if gate_ids != tuple(range(1, gate_ids[-1] + 1)):
        raise ValueError("certification plan gates must be prefix-closed")
    if plan.profileKind == "certifying" and gate_ids != (1, 2, 3, 4, 5):
        raise ValueError("certifying plans require all five gates")


def _require_certification_gate_identities(plan: CertificationPlan) -> None:
    expected = (
        plan.registryDigest,
        plan.candidateIdentity,
        plan.profileId,
        plan.profileKind,
    )
    for gate in plan.gates:
        observed = (
            gate.registryDigest,
            gate.candidateIdentity,
            gate.profileId,
            gate.profileKind,
        )
        if observed != expected:
            raise ValueError("gate plan identity does not match its certification plan")


class GateResultAdmission(FrozenContractModel):
    """External registry/profile/candidate authority for one manifest publication."""

    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    candidateIdentity: CandidateIdentity
    altitude: ProfileKind


class RailArtifactResult(FrozenContractModel):
    artifactId: ArtifactId
    sha256: str = Field(pattern=_DIGEST_PATTERN)
    size: int = Field(ge=0, le=1_000_000_000_000)
    evidenceRef: SemanticText = Field(max_length=16384)


class RailEvidenceReference(FrozenContractModel):
    evidenceId: str = Field(pattern=_ID_PATTERN, max_length=128)
    sha256: str = Field(pattern=_DIGEST_PATTERN)
    size: int = Field(ge=0, le=1_000_000_000)
    reference: SemanticText = Field(max_length=16384)


class RailTerminalObservation(FrozenContractModel):
    """Executor-neutral terminal facts for one planned rail."""

    rail: RailIdentity
    status: RailStatus
    code: str = Field(pattern=_ID_PATTERN, max_length=128)
    blockedBy: tuple[RailIdentity, ...] = Field(default_factory=tuple, max_length=256)
    artifacts: tuple[RailArtifactResult, ...] = Field(default_factory=tuple, max_length=256)
    evidence: tuple[RailEvidenceReference, ...] = Field(
        default_factory=tuple,
        max_length=128,
    )


class RailResult(FrozenContractModel):
    schemaVersion: Literal["closeout-rail-result/v1"] = "closeout-rail-result/v1"
    rail: RailIdentity
    gate: GateId
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    posture: RailPosture
    status: RailStatus
    code: str = Field(pattern=_ID_PATTERN, max_length=128)
    blockedBy: tuple[RailIdentity, ...] = Field(default_factory=tuple, max_length=256)
    correctiveOwner: str = Field(pattern=_ID_PATTERN, max_length=128)
    artifacts: tuple[RailArtifactResult, ...] = Field(default_factory=tuple, max_length=256)
    evidence: tuple[RailEvidenceReference, ...] = Field(
        default_factory=tuple,
        max_length=128,
    )
    resultDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_result(self) -> Self:
        _require_result_status_payload(self)
        _require_unique_result_members(self)
        payload = self.model_dump(mode="json", exclude={"resultDigest"})
        if self.resultDigest != content_digest(payload):
            raise ValueError("rail result digest does not match its content")
        return self


def _require_result_status_payload(result: RailResult) -> None:
    if result.status == "blocked" and not result.blockedBy:
        raise ValueError("blocked rail result requires blockedBy")
    if result.status != "blocked" and result.blockedBy:
        raise ValueError("only a blocked rail result may carry blockedBy")
    if result.status == "not-applicable" and result.artifacts:
        raise ValueError("not-applicable rail result cannot publish artifacts")


def _require_unique_result_members(result: RailResult) -> None:
    blocker_keys = [identity.key for identity in result.blockedBy]
    if len(blocker_keys) != len(set(blocker_keys)):
        raise ValueError("blockedBy identities must be unique")
    artifact_ids = [artifact.artifactId for artifact in result.artifacts]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ValueError("rail result artifact identities must be unique")
    evidence_ids = [item.evidenceId for item in result.evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("rail result evidence identities must be unique")


class GateResultManifest(FrozenContractModel):
    schemaVersion: Literal["closeout-gate-result-manifest/v1"] = "closeout-gate-result-manifest/v1"
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certificationPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    candidateIdentity: CandidateIdentity
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    profileKind: ProfileKind
    altitude: ProfileKind
    gate: GateId
    disposition: Literal["green", "red"]
    railResults: tuple[RailResult, ...] = Field(min_length=1, max_length=4096)
    manifestDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_manifest(self) -> Self:
        _require_manifest_result_identities(self)
        if self.disposition != _manifest_disposition(self.railResults):
            raise ValueError("gate disposition does not match its enforcing rail results")
        payload = self.model_dump(mode="json", exclude={"manifestDigest"})
        if self.manifestDigest != content_digest(payload):
            raise ValueError("gate result manifest digest does not match its content")
        return self


def _require_manifest_result_identities(manifest: GateResultManifest) -> None:
    if manifest.altitude != manifest.profileKind:
        raise ValueError("diagnostic and certifying manifest altitudes cannot be promoted")
    result_keys = [result.rail.key for result in manifest.railResults]
    if len(result_keys) != len(set(result_keys)):
        raise ValueError("gate result manifest rail identities must be unique")
    if any(result.gate != manifest.gate for result in manifest.railResults):
        raise ValueError("gate result manifest cannot mix gate identities")
    if any(result.gatePlanDigest != manifest.gatePlanDigest for result in manifest.railResults):
        raise ValueError("rail results must bind the manifest gate plan")


def _manifest_disposition(
    results: tuple[RailResult, ...],
) -> Literal["green", "red"]:
    if any(
        result.posture == "enforcing" and result.status in {"fail", "blocked"} for result in results
    ):
        return "red"
    return "green"
