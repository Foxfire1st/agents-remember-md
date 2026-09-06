"""Closed certification wire primitives, independent of domain execution."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

GateId = Literal[1, 2, 3, 4, 5]

_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"

_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _require_semantic_text(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("semantic text must be nonblank and unpadded")
    return value


SemanticText = Annotated[str, AfterValidator(_require_semantic_text)]


class FrozenContractModel(BaseModel):
    """Closed immutable base for registry, plan, and result records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RailIdentity(FrozenContractModel):
    railId: str = Field(pattern=_ID_PATTERN, max_length=128)
    version: SemanticText = Field(pattern=_VERSION_PATTERN)

    @property
    def key(self) -> str:
        return f"{self.railId}@{self.version}"
