"""Typed corrective dispositions crossing the lifecycle admission boundary."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.models.certification.base import (
    FrozenContractModel,
    RailIdentity,
    SemanticText,
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

_INPUT_DIGEST_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"

CorrectiveDispositionKind = Literal["direct-repair", "repaired-root"]


class CorrectiveInputChange(FrozenContractModel):
    """One exact semantic input changed by a corrective repair."""

    inputKind: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=128)
    inputId: SemanticText = Field(max_length=1024)
    beforeDigest: str = Field(pattern=_INPUT_DIGEST_PATTERN)
    afterDigest: str = Field(pattern=_INPUT_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _require_real_change(self) -> Self:
        if self.beforeDigest == self.afterDigest:
            raise ValueError("corrective input change must move an exact semantic input")
        return self

    @property
    def key(self) -> tuple[str, str]:
        return self.inputKind, self.inputId


class RedCatalogDisposition(FrozenContractModel):
    """Corrective authority for one enforcing failed or blocked rail."""

    rail: RailIdentity
    priorStatus: Literal["fail", "blocked"]
    priorResultDigest: str = Field(pattern=_DIGEST_PATTERN)
    correctiveOwner: str = Field(
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
        max_length=128,
    )
    disposition: CorrectiveDispositionKind
    repairedRoot: RailIdentity | None = None
    changedInputs: tuple[CorrectiveInputChange, ...] = Field(
        default_factory=tuple,
        max_length=4096,
    )
    rationale: SemanticText = Field(max_length=4096)

    @model_validator(mode="after")
    def _require_disposition_shape(self) -> Self:
        if self.disposition == "direct-repair":
            if self.repairedRoot is not None or not self.changedInputs:
                raise ValueError("direct repair requires changed inputs and no repaired root")
        elif self.repairedRoot is None or self.changedInputs:
            raise ValueError("repaired-root disposition requires only its root rail")
        if self.changedInputs != tuple(
            sorted(self.changedInputs, key=lambda item: (*item.key, item.afterDigest))
        ) or len({item.key for item in self.changedInputs}) != len(self.changedInputs):
            raise ValueError("corrective input changes must be unique and canonical")
        return self
