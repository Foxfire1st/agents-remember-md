"""Strict repository-owned inputs for the configurable Gate 1-4 contribution."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    ArtifactDeclaration,
    CandidateIdentity,
    CompiledRail,
    FrozenContractModel,
    GateId,
    RailClass,
    RailEvidenceContract,
    RailIdentity,
    RailPosture,
    RailRuntimeInputs,
    RegistryValidationFinding,
    SemanticText,
    canonical_execution_waves,
)

RepositoryGateId = Literal[1, 2, 3, 4]
ProfileMode = Literal["targeted", "full"]
ProfilePurpose = Literal["local-precommit", "closeout"]
GateApplicabilityStatus = Literal["applicable", "not-applicable"]
SemanticInputKind = Literal[
    "rail-execution",
    "rail-runtime",
    "selector",
    "executor-adapter",
    "result-decoder",
    "publication-policy",
]

_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


def _require_safe_relative_path(value: str) -> str:
    if value == ".":
        return value
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        raise ValueError("path must be a canonical repository-relative POSIX path")
    parts = value.split("/")
    if (len(parts[0]) >= 2 and parts[0][1] == ":") or any(
        part in {"", ".", ".."} for part in parts
    ):
        raise ValueError("path must not be empty, dotted, or traversal-bearing")
    return value


RelativeRepositoryPath = Annotated[str, AfterValidator(_require_safe_relative_path)]


def require_relative_repository_file_path(value: str) -> str:
    value = _require_safe_relative_path(value)
    if value == ".":
        raise ValueError("file path must name a repository-relative file")
    return value


RelativeRepositoryFilePath = Annotated[
    str,
    AfterValidator(require_relative_repository_file_path),
]


class RepositoryGateSelection(FrozenContractModel):
    gate: RepositoryGateId
    status: GateApplicabilityStatus
    railIds: tuple[RailIdentity, ...] = Field(default_factory=tuple, max_length=4096)
    selectionIdentity: SemanticText = Field(max_length=1024)
    population: SemanticText | None = Field(default=None, max_length=4096)
    reason: SemanticText | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def _require_status_payload(self) -> Self:
        if self.status == "applicable":
            if not self.railIds or self.population is None or self.reason is not None:
                raise ValueError(
                    "applicable gate selection requires rails and population, but no reason"
                )
        elif self.railIds or self.population is not None or self.reason is None:
            raise ValueError(
                "not-applicable gate selection requires only a repository-owned reason"
            )
        return self


class RepositoryProfileSelection(FrozenContractModel):
    selectionId: str = Field(pattern=_ID_PATTERN, max_length=128)
    purpose: ProfilePurpose
    mode: ProfileMode
    executorAdapterId: str = Field(pattern=_ID_PATTERN, max_length=128)
    resultDecoderId: str = Field(pattern=_ID_PATTERN, max_length=128)
    gates: tuple[RepositoryGateSelection, ...] = Field(min_length=4, max_length=4)


class RepositoryRailExecution(FrozenContractModel):
    adapterKind: Literal["container-command"] = "container-command"
    adapterId: str = Field(pattern=_ID_PATTERN, max_length=256)
    workingDirectory: RelativeRepositoryPath
    command: tuple[SemanticText, ...] = Field(min_length=1, max_length=1024)
    environmentContract: tuple[SemanticText, ...] = Field(min_length=1, max_length=256)
    scopeProviderId: str | None = Field(default=None, pattern=_ID_PATTERN, max_length=128)
    inputSelectors: tuple[str, ...] = Field(default_factory=tuple, max_length=256)
    resultDecoderId: str = Field(pattern=_ID_PATTERN, max_length=128)
    timeoutSeconds: int = Field(gt=0, le=86_400)
    resourcePolicyId: str = Field(pattern=_ID_PATTERN, max_length=128)
    successExitCodes: tuple[int, ...] = Field(default=(0,), min_length=1, max_length=32)
    skippedExitCodes: tuple[int, ...] = Field(default_factory=tuple, max_length=32)
    cleanRoom: bool = False
    teardownPolicy: Literal["not-required", "always"] = "not-required"
    executionEvidence: SemanticText = Field(max_length=32768)


class RepositoryRailDefinition(FrozenContractModel):
    identity: RailIdentity
    gate: RepositoryGateId
    railClass: RailClass
    ownerClass: str = Field(pattern=_ID_PATTERN, max_length=128)
    correctiveOwner: str = Field(pattern=_ID_PATTERN, max_length=128)
    posture: RailPosture
    orderKey: SemanticText = Field(max_length=256)
    prerequisites: tuple[RailIdentity, ...] = Field(default_factory=tuple, max_length=256)
    requiredArtifacts: tuple[str, ...] = Field(default_factory=tuple, max_length=256)
    runtimeInputs: RailRuntimeInputs
    evidenceContract: tuple[RailEvidenceContract, ...] = Field(
        min_length=1,
        max_length=128,
    )
    outputArtifacts: tuple[ArtifactDeclaration, ...] = Field(
        default_factory=tuple,
        max_length=256,
    )
    execution: RepositoryRailExecution


class RepositorySelectorAuthority(FrozenContractModel):
    selectorId: str = Field(pattern=_ID_PATTERN, max_length=128)
    schemaVersion: Literal["repository-selector-result/v2"] = "repository-selector-result/v2"
    version: SemanticText = Field(pattern=_VERSION_PATTERN)
    configurationDigest: str = Field(pattern=_DIGEST_PATTERN)
    inputUniverse: tuple[SemanticText, ...] = Field(min_length=1, max_length=1024)
    externalInputs: tuple[SemanticText, ...] = Field(default_factory=tuple, max_length=1024)
    outputArtifacts: tuple[str, ...] = Field(min_length=1, max_length=256)
    workingDirectory: RelativeRepositoryPath
    command: tuple[SemanticText, ...] = Field(min_length=1, max_length=1024)
    resultPath: RelativeRepositoryFilePath


class DaggerModuleExecutorDefinition(FrozenContractModel):
    adapterId: str = Field(pattern=_ID_PATTERN, max_length=128)
    adapterKind: Literal["dagger-module"] = "dagger-module"
    version: SemanticText = Field(pattern=_VERSION_PATTERN)
    executable: SemanticText = Field(max_length=256)
    functionName: str = Field(pattern=_ID_PATTERN, max_length=128)
    sourceArgument: str = Field(pattern=_ID_PATTERN, max_length=128)
    repositoryBundleArgument: str = Field(pattern=_ID_PATTERN, max_length=128)
    diffBaseArgument: str = Field(pattern=_ID_PATTERN, max_length=128)
    memoryCapArgument: str = Field(pattern=_ID_PATTERN, max_length=128)
    planArgument: str = Field(pattern=_ID_PATTERN, max_length=128)
    reportsField: str = Field(pattern=_ID_PATTERN, max_length=128)
    imageReference: SemanticText = Field(
        pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$",
        max_length=2048,
    )
    runtimeDigest: str = Field(pattern=_DIGEST_PATTERN)
    consumingGates: tuple[RepositoryGateId, ...] = Field(min_length=1, max_length=4)


class JsonFieldPath(FrozenContractModel):
    """An unambiguous object-key path inside one decoded JSON result."""

    segments: tuple[SemanticText, ...] = Field(min_length=1, max_length=16)

    @property
    def key(self) -> str:
        return content_digest(self)

    @property
    def display(self) -> str:
        return ".".join(self.segments)


class JsonArtifactReferenceDefinition(FrozenContractModel):
    fieldPath: JsonFieldPath
    cardinality: Literal["single", "list"]
    nullPolicy: Literal["invalid", "ignore-reference"] = "invalid"
    nullParentPolicy: Literal["invalid", "ignore-reference"] = "invalid"


class JsonReferenceActivationDefinition(FrozenContractModel):
    """When a string-list field exists, require references iff it contains one value."""

    listFieldPath: JsonFieldPath
    containsValue: SemanticText = Field(max_length=256)
    nullPolicy: Literal["invalid", "ignore-activation"] = "invalid"
    referenceFieldPaths: tuple[JsonFieldPath, ...] = Field(min_length=1, max_length=256)


class JsonExitStatusDecoderDefinition(FrozenContractModel):
    decoderId: str = Field(pattern=_ID_PATTERN, max_length=128)
    decoderKind: Literal["json-exit-status"] = "json-exit-status"
    artifactPath: RelativeRepositoryFilePath
    statusField: SemanticText = Field(max_length=128)
    exitCodeField: SemanticText = Field(max_length=128)
    passedValue: SemanticText = Field(max_length=128)
    failedValue: SemanticText = Field(max_length=128)
    artifactReferences: tuple[JsonArtifactReferenceDefinition, ...] = Field(
        default_factory=tuple,
        max_length=256,
    )
    referenceActivations: tuple[JsonReferenceActivationDefinition, ...] = Field(
        default_factory=tuple,
        max_length=256,
    )
    consumingGates: tuple[RepositoryGateId, ...] = Field(min_length=1, max_length=4)


class PublishedArtifactDefinition(FrozenContractModel):
    path: RelativeRepositoryFilePath
    mediaType: SemanticText = Field(max_length=256)
    required: bool = True
    maxBytes: int = Field(gt=0, le=1_000_000_000)
    publisherGates: tuple[RepositoryGateId, ...] = Field(min_length=1, max_length=4)


class RepositoryCertificationProfile(FrozenContractModel):
    schemaVersion: Literal["repository-certification-profile/v1"] = (
        "repository-certification-profile/v1"
    )
    semanticRevision: SemanticText = Field(pattern=_VERSION_PATTERN)
    repositoryId: str = Field(pattern=_ID_PATTERN, max_length=128)
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    profileDigest: str = Field(pattern=_DIGEST_PATTERN)
    selections: tuple[RepositoryProfileSelection, ...] = Field(min_length=1, max_length=128)
    rails: tuple[RepositoryRailDefinition, ...] = Field(min_length=1, max_length=4096)
    selectors: tuple[RepositorySelectorAuthority, ...] = Field(
        default_factory=tuple,
        max_length=128,
    )
    executorAdapters: tuple[DaggerModuleExecutorDefinition, ...] = Field(
        min_length=1,
        max_length=128,
    )
    resultDecoders: tuple[JsonExitStatusDecoderDefinition, ...] = Field(
        min_length=1,
        max_length=128,
    )
    publishedArtifacts: tuple[PublishedArtifactDefinition, ...] = Field(
        min_length=1,
        max_length=4096,
    )


def _normalize_repository_profile(
    profile: RepositoryCertificationProfile,
) -> RepositoryCertificationProfile:
    return profile.model_copy(
        update={
            "selections": _sorted_contract_items(
                (_normalize_selection(item) for item in profile.selections),
                identity=lambda item: item.selectionId,
            ),
            "rails": _sorted_contract_items(
                (_normalize_repository_rail(item) for item in profile.rails),
                identity=lambda item: item.identity.key,
            ),
            "selectors": _sorted_contract_items(
                (_normalize_selector(item) for item in profile.selectors),
                identity=lambda item: item.selectorId,
            ),
            "executorAdapters": _sorted_contract_items(
                (
                    item.model_copy(update={"consumingGates": tuple(sorted(item.consumingGates))})
                    for item in profile.executorAdapters
                ),
                identity=lambda item: item.adapterId,
            ),
            "resultDecoders": _sorted_contract_items(
                (_normalize_decoder(item) for item in profile.resultDecoders),
                identity=lambda item: item.decoderId,
            ),
            "publishedArtifacts": _sorted_contract_items(
                (
                    item.model_copy(update={"publisherGates": tuple(sorted(item.publisherGates))})
                    for item in profile.publishedArtifacts
                ),
                identity=lambda item: item.path,
            ),
        }
    )


def repository_profile_digest(profile: RepositoryCertificationProfile) -> str:
    normalized = _normalize_repository_profile(profile)
    return content_digest(normalized.model_dump(mode="json", exclude={"profileDigest"}))


def _normalize_selection(selection: RepositoryProfileSelection) -> RepositoryProfileSelection:
    gates = tuple(
        gate.model_copy(
            update={
                "railIds": _sorted_contract_items(
                    gate.railIds,
                    identity=lambda item: item.key,
                ),
            }
        )
        for gate in sorted(selection.gates, key=lambda item: item.gate)
    )
    return selection.model_copy(update={"gates": gates})


def _normalize_repository_rail(rail: RepositoryRailDefinition) -> RepositoryRailDefinition:
    execution = rail.execution.model_copy(
        update={
            "environmentContract": tuple(sorted(rail.execution.environmentContract)),
            "inputSelectors": tuple(sorted(rail.execution.inputSelectors)),
            "successExitCodes": tuple(sorted(rail.execution.successExitCodes)),
            "skippedExitCodes": tuple(sorted(rail.execution.skippedExitCodes)),
        }
    )
    return rail.model_copy(
        update={
            "prerequisites": _sorted_contract_items(
                rail.prerequisites,
                identity=lambda item: item.key,
            ),
            "requiredArtifacts": tuple(sorted(rail.requiredArtifacts)),
            "evidenceContract": _sorted_contract_items(
                rail.evidenceContract,
                identity=lambda item: item.evidenceId,
            ),
            "outputArtifacts": _sorted_contract_items(
                rail.outputArtifacts,
                identity=lambda item: item.artifactId,
            ),
            "execution": execution,
        }
    )


def _normalize_selector(
    selector: RepositorySelectorAuthority,
) -> RepositorySelectorAuthority:
    return selector.model_copy(
        update={
            "inputUniverse": tuple(sorted(selector.inputUniverse)),
            "externalInputs": tuple(sorted(selector.externalInputs)),
            "outputArtifacts": tuple(sorted(selector.outputArtifacts)),
        }
    )


def _normalize_decoder(
    decoder: JsonExitStatusDecoderDefinition,
) -> JsonExitStatusDecoderDefinition:
    activations = tuple(
        activation.model_copy(
            update={
                "referenceFieldPaths": _sorted_contract_items(
                    activation.referenceFieldPaths,
                    identity=lambda item: item.key,
                )
            }
        )
        for activation in decoder.referenceActivations
    )
    return decoder.model_copy(
        update={
            "artifactReferences": _sorted_contract_items(
                decoder.artifactReferences,
                identity=lambda item: item.fieldPath.key,
            ),
            "referenceActivations": _sorted_contract_items(
                activations,
                identity=lambda item: f"{item.listFieldPath.key}:{item.containsValue}",
            ),
            "consumingGates": tuple(sorted(decoder.consumingGates)),
        }
    )


def _sorted_contract_items[Contract: BaseModel](
    items: Iterable[Contract],
    *,
    identity: Callable[[Contract], str],
) -> tuple[Contract, ...]:
    decorated = ((identity(item), content_digest(item), item) for item in items)
    return tuple(item for _, _, item in sorted(decorated, key=lambda entry: entry[:2]))


class CanonicalRepositoryCertificationProfile(FrozenContractModel):
    schemaVersion: Literal["canonical-repository-certification-profile/v1"] = (
        "canonical-repository-certification-profile/v1"
    )
    profile: RepositoryCertificationProfile
    profileDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        expected = repository_profile_digest(self.profile)
        if self.profileDigest != expected or self.profile.profileDigest != expected:
            raise ValueError("canonical repository profile digest does not match its content")
        return self


class RepositoryProfileValidationReport(FrozenContractModel):
    schemaVersion: Literal["repository-certification-profile-validation/v1"] = (
        "repository-certification-profile-validation/v1"
    )
    profileDigest: str = Field(pattern=_DIGEST_PATTERN)
    findings: tuple[RegistryValidationFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


class RepositorySemanticInputNode(FrozenContractModel):
    """One canonical semantic input and the exact gates that consume its bytes."""

    inputKind: SemanticInputKind
    inputId: SemanticText = Field(max_length=1024)
    consumingGates: tuple[RepositoryGateId, ...] = Field(min_length=1, max_length=4)
    contentDigest: str = Field(pattern=_DIGEST_PATTERN)
    canonicalJson: str = Field(min_length=2, max_length=1_000_000)

    @model_validator(mode="after")
    def _verify_content(self) -> Self:
        if self.consumingGates != tuple(sorted(set(self.consumingGates))):
            raise ValueError("semantic input consuming gates must be unique and ordered")
        try:
            parsed = json.loads(self.canonicalJson)
        except json.JSONDecodeError as error:
            raise ValueError("semantic input content must be canonical JSON") from error
        if not isinstance(parsed, dict):
            raise ValueError("semantic input content must be a JSON object")
        expected_json = json.dumps(
            parsed,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if self.canonicalJson != expected_json:
            raise ValueError("semantic input content is not canonical JSON")
        if self.contentDigest != content_digest(parsed):
            raise ValueError("semantic input digest does not match its canonical content")
        return self


class RepositoryGatePlan(FrozenContractModel):
    schemaVersion: Literal["repository-gate-plan/v1"] = "repository-gate-plan/v1"
    profileDigest: str = Field(pattern=_DIGEST_PATTERN)
    candidateIdentity: CandidateIdentity
    selectionId: str = Field(pattern=_ID_PATTERN, max_length=128)
    purpose: ProfilePurpose
    mode: ProfileMode
    gate: RepositoryGateId
    gatePrerequisites: tuple[GateId, ...]
    applicability: GateApplicabilityStatus
    reason: SemanticText | None = Field(default=None, max_length=4096)
    rails: tuple[CompiledRail, ...] = Field(default_factory=tuple, max_length=4096)
    semanticInputs: tuple[RepositorySemanticInputNode, ...] = Field(
        default_factory=tuple,
        max_length=16384,
    )
    waves: tuple[tuple[RailIdentity, ...], ...] = Field(default_factory=tuple, max_length=4096)
    planDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_plan(self) -> Self:
        expected_prerequisites = tuple(range(1, self.gate))
        if self.gatePrerequisites != expected_prerequisites:
            raise ValueError("repository gate prerequisites must be the complete earlier prefix")
        if self.applicability == "not-applicable":
            if self.rails or self.semanticInputs or self.waves or self.reason is None:
                raise ValueError("not-applicable gate plan must carry only its typed reason")
        elif not self.rails or self.reason is not None:
            raise ValueError("applicable gate plan requires rails and no reason")
        if self.waves != canonical_execution_waves(self.rails):
            raise ValueError("repository gate waves must be the earliest topological partition")
        if any(self.gate not in item.consumingGates for item in self.semanticInputs):
            raise ValueError("repository gate plan contains an input not consumed by this gate")
        ordered_inputs = tuple(
            sorted(
                self.semanticInputs,
                key=lambda item: (item.inputKind, item.inputId, item.contentDigest),
            )
        )
        if self.semanticInputs != ordered_inputs or len(
            {(item.inputKind, item.inputId) for item in self.semanticInputs}
        ) != len(self.semanticInputs):
            raise ValueError("repository gate semantic inputs must be unique and canonical")
        if self.planDigest != repository_gate_plan_digest(self.model_dump(mode="json")):
            raise ValueError("repository gate plan digest does not match its content")
        return self


class RepositoryProfilePlan(FrozenContractModel):
    schemaVersion: Literal["repository-profile-plan/v1"] = "repository-profile-plan/v1"
    profileDigest: str = Field(pattern=_DIGEST_PATTERN)
    candidateIdentity: CandidateIdentity
    selectionId: str = Field(pattern=_ID_PATTERN, max_length=128)
    purpose: ProfilePurpose
    mode: ProfileMode
    gates: tuple[RepositoryGatePlan, ...] = Field(min_length=4, max_length=4)
    planDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_plan(self) -> Self:
        if tuple(gate.gate for gate in self.gates) != (1, 2, 3, 4):
            raise ValueError("repository profile plans require exact ordered Gates 1-4")
        expected = (
            self.profileDigest,
            self.candidateIdentity,
            self.selectionId,
            self.purpose,
            self.mode,
        )
        if any(
            (
                gate.profileDigest,
                gate.candidateIdentity,
                gate.selectionId,
                gate.purpose,
                gate.mode,
            )
            != expected
            for gate in self.gates
        ):
            raise ValueError("repository gate plan identity does not match its profile plan")
        payload = self.model_dump(mode="json", exclude={"planDigest"})
        if self.planDigest != content_digest(payload):
            raise ValueError("repository profile plan digest does not match its content")
        return self


def repository_gate_plan_digest(payload: Mapping[str, object]) -> str:
    """Digest gate semantics without coupling them to unrelated profile edits.

    ``profileDigest`` remains in the immutable plan as its admission authority, while
    downstream certificate invalidation follows the explicit gate dependency chain.
    Excluding only that aggregate identity lets an unchanged earlier gate plan retain
    its semantic identity when a later gate's repository configuration changes.
    """

    return content_digest(
        {key: value for key, value in payload.items() if key not in {"planDigest", "profileDigest"}}
    )
