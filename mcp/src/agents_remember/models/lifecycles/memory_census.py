"""Exact governed-artifact identities and noncertifying structural census results."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ArtifactType = Literal["file-sidecar", "inline-onboarding", "route-overview", "entity-row"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def require_git_relative_path(value: str) -> str:
    """Validate exact UTF-8 Git spelling without silently normalizing an identity.

    Filesystem confinement, including symbolic-link ancestry, is checked by the
    census source reader against its contract-resolved root.
    """

    value.encode("utf-8", errors="strict")
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("artifact path must be a nonempty UTF-8 Git path with / separators")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("artifact path must be relative without empty, . or .. segments")
    if len(value) >= 2 and value[0].isascii() and value[0].isalpha() and value[1] == ":":
        raise ValueError("artifact path must not have a drive prefix")
    return value


class GovernedArtifactIdentity(_StrictModel):
    artifactType: ArtifactType
    memoryRootRelativePath: str = Field(min_length=1)
    subIdentity: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("memoryRootRelativePath")
    @classmethod
    def _path_is_exact(cls, value: str) -> str:
        return require_git_relative_path(value)

    @model_validator(mode="after")
    def _subidentity_matches_artifact(self) -> Self:
        if self.artifactType == "entity-row":
            if self.memoryRootRelativePath.rsplit("/", 1)[-1] != "entities.md":
                raise ValueError("entity-row identity must address its entities.md catalog")
            if self.subIdentity is None or not self.subIdentity:
                raise ValueError("entity-row identity requires its nonempty decoded Entity key")
            self.subIdentity.encode("utf-8", errors="strict")
            if "\x00" in self.subIdentity:
                raise ValueError("entity key must not contain NUL")
        elif "subIdentity" in self.model_fields_set:
            raise ValueError("whole-document artifact identity prohibits subIdentity")
        return self

    @property
    def sort_key(self) -> tuple[bytes, bytes, int, bytes]:
        """The normative byte ordering; expected presence is deliberately excluded."""

        return (
            self.artifactType.encode("utf-8"),
            self.memoryRootRelativePath.encode("utf-8"),
            int(self.subIdentity is not None),
            self.subIdentity.encode("utf-8") if self.subIdentity is not None else b"",
        )


class MemoryCensusRow(_StrictModel):
    """Structural observation, awaiting R05/R07 semantic removal authority.

    A physically absent pre-existing artifact remains enumerable before adjudication.
    The builder emits removal-disposition-required until canonical curator authority
    supplies removed; this model does not mint that semantic decision.
    """

    identity: GovernedArtifactIdentity
    expectedFinalPresence: Literal["present", "absent"] = "present"
    sourcePaths: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    preExisting: bool = False

    @field_validator("sourcePaths")
    @classmethod
    def _source_paths_are_exact(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            require_git_relative_path(value)
        return values

    @model_validator(mode="after")
    def _absence_has_existing_identity(self) -> Self:
        if self.expectedFinalPresence == "absent" and not self.preExisting:
            raise ValueError("absent requires a pre-existing governed artifact")
        return self


class MemoryCensusBlocker(_StrictModel):
    code: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    sourcePath: str | None = None
    identity: GovernedArtifactIdentity | None = None


class MemoryCensusResult(_StrictModel):
    """Derived worklist only: neither semantic acceptance nor final certification."""

    rows: tuple[MemoryCensusRow, ...] = ()
    blockers: tuple[MemoryCensusBlocker, ...] = ()
    unonboarded: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _rows_are_unique_and_ordered(self) -> Self:
        keys = [row.identity.sort_key for row in self.rows]
        if len(keys) != len(set(keys)):
            raise ValueError("governed-artifact census identities must be unique")
        if keys != sorted(keys):
            raise ValueError("governed-artifact census rows must follow exact UTF-8 ordering")
        return self


__all__ = [
    "ArtifactType",
    "GovernedArtifactIdentity",
    "MemoryCensusBlocker",
    "MemoryCensusResult",
    "MemoryCensusRow",
    "require_git_relative_path",
]
