"""Closed source-selector declarations and exact candidate/base observations."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import RailApplicability
from agents_remember.models.certification.base import FrozenContractModel, SemanticText

_GIT_ID = r"^[0-9a-f]{40,64}$"


def _require_relative(value: str, *, prefix: bool = False) -> None:
    path = value.removesuffix("/") if prefix else value
    if (
        not path
        or "\\" in path
        or "\0" in path
        or PurePosixPath(path).is_absolute()
        or PurePosixPath(path).as_posix() != path
        or any(part in {".", ".."} for part in path.split("/"))
        or (len(path) >= 2 and path[1] == ":")
    ):
        raise ValueError("source applicability requires canonical repository-relative paths")


class SourcePathApplicability(FrozenContractModel):
    schemaVersion: Literal["source-path-applicability/v1"] = "source-path-applicability/v1"
    selectorId: SemanticText = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=128)
    version: SemanticText = Field(max_length=128)
    dependencyPrefixes: tuple[SemanticText, ...] = Field(min_length=1, max_length=4096)
    evidencePath: SemanticText = Field(max_length=4096)
    notApplicableReason: SemanticText = Field(max_length=4096)

    @model_validator(mode="after")
    def _require_scope(self) -> Self:
        _require_relative(self.evidencePath)
        if self.dependencyPrefixes != tuple(sorted(set(self.dependencyPrefixes))):
            raise ValueError("source applicability prefixes must be unique and ordered")
        for prefix in self.dependencyPrefixes:
            _require_relative(prefix, prefix=True)
        return self


class CandidateSourceSelection(FrozenContractModel):
    schemaVersion: Literal["candidate-source-selection/v1"] = "candidate-source-selection/v1"
    baseCommit: str = Field(pattern=_GIT_ID)
    baseTree: str = Field(pattern=_GIT_ID)
    candidateTree: str = Field(pattern=_GIT_ID)
    changedPaths: tuple[SemanticText, ...] = Field(max_length=100_000)
    selectionDigest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _require_census(self) -> Self:
        if self.changedPaths != tuple(sorted(set(self.changedPaths))):
            raise ValueError("candidate source census must be complete, unique and ordered")
        for path in self.changedPaths:
            _require_relative(path)
        payload = self.model_dump(mode="json", exclude={"selectionDigest"})
        if self.selectionDigest != content_digest(payload):
            raise ValueError("candidate source selection digest differs from its exact observation")
        return self


class RailSourceSelection(FrozenContractModel):
    schemaVersion: Literal["rail-source-selection/v1"] = "rail-source-selection/v1"
    declaration: SourcePathApplicability
    sourceSelection: CandidateSourceSelection
    mode: Literal["targeted", "full"]
    selectedPaths: tuple[SemanticText, ...] = Field(max_length=100_000)
    applicability: RailApplicability
    decisionDigest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _require_decision(self) -> Self:
        selected = selected_paths(self.declaration, self.sourceSelection.changedPaths)
        if self.selectedPaths != selected:
            raise ValueError("rail selection differs from its declared exact source census")
        applicable = self.mode == "full" or bool(selected)
        identity = selection_identity(self.declaration, self.sourceSelection, self.mode)
        if self.applicability.selectionIdentity != identity or self.applicability.status != (
            "applicable" if applicable else "not-applicable"
        ):
            raise ValueError("rail applicability differs from its admitted source selection")
        if not applicable and self.applicability.reason != self.declaration.notApplicableReason:
            raise ValueError("rail non-applicability lacks its repository-declared reason")
        payload = self.model_dump(mode="json", exclude={"decisionDigest"})
        if self.decisionDigest != content_digest(payload):
            raise ValueError("rail source selection digest differs from its exact decision")
        return self


def selected_paths(declaration: SourcePathApplicability, paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        path
        for path in paths
        if any(path.startswith(prefix) for prefix in declaration.dependencyPrefixes)
    )


def selection_identity(
    declaration: SourcePathApplicability, source: CandidateSourceSelection, mode: str
) -> str:
    return content_digest(
        {
            "declaration": declaration.model_dump(mode="json"),
            "sourceSelection": source.model_dump(mode="json"),
            "mode": mode,
        }
    )
