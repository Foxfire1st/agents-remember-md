"""Canonical repository-neutral result contract for one test-selection provider."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    ArtifactId,
    CandidateIdentity,
    FrozenContractModel,
    SemanticText,
)
from agents_remember.certification.repository_profiles.models import ProfileMode

SelectionPopulation = Literal["empty", "targeted", "full"]
SelectionFailureCode = Literal["test-selection-ownership-incomplete"]
SelectionEffect = Literal["select", "global-invalidate", "irrelevant", "unresolved"]


@dataclass(frozen=True)
class RepositorySelectionDraft:
    """Typed provider inputs normalized into one immutable selector result."""

    selector_id: str
    selector_version: str
    configuration_digest: str
    candidate_identity: CandidateIdentity
    mode: ProfileMode
    base_revision: str
    population: SelectionPopulation
    complete: bool
    global_invalidators: Sequence[str]
    dependency_reasons: Sequence[RepositorySelectionReason]
    unresolved_inputs: Sequence[RepositorySelectionReason]
    outputs: Mapping[str, Sequence[str]]


class RepositorySelectionOutput(FrozenContractModel):
    """One profile-declared output population in a selector result."""

    artifactId: ArtifactId
    values: tuple[SemanticText, ...] = Field(default_factory=tuple, max_length=100_000)

    @model_validator(mode="after")
    def _require_canonical_values(self) -> Self:
        if self.values != tuple(sorted(set(self.values))):
            raise ValueError("selection output values must be unique and canonically ordered")
        return self


class RepositorySelectionReason(FrozenContractModel):
    """One auditable input-to-population decision made by the repository provider."""

    input: SemanticText = Field(max_length=4096)
    kind: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=128)
    effect: SelectionEffect
    outputArtifact: ArtifactId | None = None
    outputValue: SemanticText | None = Field(default=None, max_length=4096)
    detail: SemanticText = Field(max_length=4096)

    @model_validator(mode="after")
    def _require_output_pair(self) -> Self:
        paired = self.outputArtifact is not None and self.outputValue is not None
        if self.effect == "select" and not paired:
            raise ValueError("a selection reason must name its exact output artifact and value")
        if self.effect != "select" and (
            self.outputArtifact is not None or self.outputValue is not None
        ):
            raise ValueError("a non-selection reason cannot claim an output value")
        return self

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.input,
            self.kind,
            self.effect,
            self.outputArtifact or "",
            self.outputValue or "",
            self.detail,
        )


class RepositorySelectionResult(FrozenContractModel):
    """Exact candidate-bound result emitted by any repository selector implementation."""

    schemaVersion: Literal["repository-selector-result/v2"] = "repository-selector-result/v2"
    selectorId: str = Field(
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
        max_length=128,
    )
    selectorVersion: SemanticText = Field(max_length=128)
    configurationDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidateIdentity: CandidateIdentity
    mode: ProfileMode
    baseRevision: SemanticText = Field(max_length=4096)
    population: SelectionPopulation
    complete: bool
    failureCode: SelectionFailureCode | None = None
    globalInvalidators: tuple[SemanticText, ...] = Field(default_factory=tuple, max_length=100_000)
    dependencyReasons: tuple[RepositorySelectionReason, ...] = Field(
        default_factory=tuple,
        max_length=200_000,
    )
    unresolvedInputs: tuple[RepositorySelectionReason, ...] = Field(
        default_factory=tuple,
        max_length=100_000,
    )
    outputs: tuple[RepositorySelectionOutput, ...] = Field(min_length=1, max_length=1024)
    selectionDigest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _verify_contract(self) -> Self:
        _verify_population(self.mode, self.population)
        _verify_completion(self.complete, self.failureCode, self.unresolvedInputs)
        _verify_canonical_collections(self)
        _verify_reason_effects(self.dependencyReasons, self.unresolvedInputs)
        _verify_output_reasons(self.outputs, self.dependencyReasons)
        expected = repository_selection_result_digest(self)
        if self.selectionDigest != expected:
            raise ValueError("repository selector result digest does not match its content")
        return self

    def output_values(self) -> dict[str, tuple[str, ...]]:
        return {output.artifactId: output.values for output in self.outputs}


def _verify_population(mode: ProfileMode, population: SelectionPopulation) -> None:
    if mode == "full" and population != "full":
        raise ValueError("a declared full selector result must publish a full population")
    if mode == "targeted" and population == "full":
        raise ValueError("a targeted selector result cannot broaden itself to full")


def _verify_completion(
    complete: bool,
    failure_code: SelectionFailureCode | None,
    unresolved: tuple[RepositorySelectionReason, ...],
) -> None:
    if complete and (failure_code is not None or unresolved):
        raise ValueError("a complete selector result cannot carry unresolved inputs")
    if not complete and (failure_code != "test-selection-ownership-incomplete" or not unresolved):
        raise ValueError(
            "an incomplete selector result requires the ownership failure code and inputs"
        )


def _verify_canonical_collections(result: RepositorySelectionResult) -> None:
    collections = (
        (result.globalInvalidators, "global invalidators"),
        (tuple(reason.key for reason in result.dependencyReasons), "dependency reasons"),
        (tuple(reason.key for reason in result.unresolvedInputs), "unresolved inputs"),
        (tuple(output.artifactId for output in result.outputs), "selection outputs"),
    )
    for values, label in collections:
        if values != tuple(sorted(set(values))):
            raise ValueError(f"{label} must be unique and canonically ordered")


def _verify_reason_effects(
    resolved: tuple[RepositorySelectionReason, ...],
    unresolved: tuple[RepositorySelectionReason, ...],
) -> None:
    if any(reason.effect == "unresolved" for reason in resolved):
        raise ValueError("unresolved reasons belong only in unresolvedInputs")
    if any(reason.effect != "unresolved" for reason in unresolved):
        raise ValueError("unresolvedInputs may contain only unresolved reasons")


def _verify_output_reasons(
    outputs: tuple[RepositorySelectionOutput, ...],
    reasons: tuple[RepositorySelectionReason, ...],
) -> None:
    output_edges = {(output.artifactId, value) for output in outputs for value in output.values}
    reason_edges = {
        (reason.outputArtifact, reason.outputValue)
        for reason in reasons
        if reason.effect == "select"
    }
    if output_edges != reason_edges:
        raise ValueError("every selected output value must have an exact dependency reason")


def repository_selection_result_digest(
    result: RepositorySelectionResult | Mapping[str, object],
) -> str:
    """Digest canonical selector-result content without its declared digest field."""

    payload = (
        result.model_dump(mode="json")
        if isinstance(result, RepositorySelectionResult)
        else dict(result)
    )
    payload.pop("selectionDigest", None)
    return content_digest(payload)


def build_repository_selection_result(
    draft: RepositorySelectionDraft,
) -> RepositorySelectionResult:
    """Normalize and content-address one provider result."""

    normalized_reasons = {item.key: item for item in draft.dependency_reasons}
    normalized_unresolved = {item.key: item for item in draft.unresolved_inputs}
    payload = {
        "schemaVersion": "repository-selector-result/v2",
        "selectorId": draft.selector_id,
        "selectorVersion": draft.selector_version,
        "configurationDigest": draft.configuration_digest,
        "candidateIdentity": draft.candidate_identity.model_dump(mode="json"),
        "mode": draft.mode,
        "baseRevision": draft.base_revision,
        "population": draft.population,
        "complete": draft.complete,
        "failureCode": None if draft.complete else "test-selection-ownership-incomplete",
        "globalInvalidators": sorted(set(draft.global_invalidators)),
        "dependencyReasons": [
            item.model_dump(mode="json")
            for item in (normalized_reasons[key] for key in sorted(normalized_reasons))
        ],
        "unresolvedInputs": [
            item.model_dump(mode="json")
            for item in (normalized_unresolved[key] for key in sorted(normalized_unresolved))
        ],
        "outputs": [
            {
                "artifactId": artifact_id,
                "values": sorted(set(values)),
            }
            for artifact_id, values in sorted(draft.outputs.items())
        ],
    }
    return RepositorySelectionResult(
        **payload,
        selectionDigest=repository_selection_result_digest(payload),
    )


__all__ = [
    "RepositorySelectionDraft",
    "RepositorySelectionOutput",
    "RepositorySelectionReason",
    "RepositorySelectionResult",
    "build_repository_selection_result",
    "repository_selection_result_digest",
]
