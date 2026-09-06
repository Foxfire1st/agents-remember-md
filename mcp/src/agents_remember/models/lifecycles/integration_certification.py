"""Operation-selected original certification authority for full integration gates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.models.certification.base import FrozenContractModel
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.certification import SelectedGateTerminal


class IntegrationCertificationSelection(FrozenContractModel):
    schemaVersion: Literal["integration-certification-selection/v1"] = (
        "integration-certification-selection/v1"
    )
    operationKey: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(ge=1)
    integrationAuthorityDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozenRun: CertificateObjectReference
    profileReference: str = Field(min_length=1, max_length=4096)
    diffBase: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    mode: Literal["full"] = "full"
    memoryCapBytes: int | None = Field(default=None, gt=0)
    completionFingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminals: tuple[SelectedGateTerminal, ...] = Field(default_factory=tuple, max_length=4)
    terminalHistory: tuple[SelectedGateTerminal, ...] = Field(default_factory=tuple, max_length=256)

    @model_validator(mode="after")
    def _require_exact_shape(self) -> Self:
        if self.frozenRun.kind != "frozen-run":
            raise ValueError("integration selection requires an exact frozen-run reference")
        gates = tuple(item.gate for item in self.terminals)
        if gates != tuple(range(1, len(gates) + 1)):
            raise ValueError("integration terminals require one exact code gate prefix")
        if any(item.certificate is None for item in self.terminals[:-1]):
            raise ValueError("integration cannot select a later gate after an uncertified terminal")
        if any(item.reusedFrom is not None for item in (*self.terminals, *self.terminalHistory)):
            raise ValueError("integration selection retains originals within its exact generation")
        if any(item.certificate is not None for item in self.terminalHistory):
            raise ValueError("integration terminal history contains only interrupted attempts")
        if len(set(self.terminalHistory)) != len(self.terminalHistory):
            raise ValueError("integration terminal history cannot repeat an original attempt")
        return self


def validate_integration_completion_identity(
    selected: IntegrationCertificationSelection,
    completion_fingerprint: str,
    attestation: Mapping[str, str],
) -> None:
    """Bind a completion proposal to the original journal-selected execution identity."""
    expected = (
        selected.completionFingerprint,
        selected.diffBase,
        "" if selected.memoryCapBytes is None else str(selected.memoryCapBytes),
    )
    observed = (
        completion_fingerprint,
        attestation["diffBase"],
        attestation["memoryCapBytes"],
    )
    if observed != expected:
        raise ValueError(
            "selected completion fingerprint, comparison base and memory cap must match"
        )


def validate_integration_certification_transition(
    before: IntegrationCertificationSelection | None,
    after: IntegrationCertificationSelection | None,
    *,
    operation_key: str,
    generation: int,
    can_select: bool,
) -> None:
    if before == after:
        return
    if not can_select or after is None:
        raise RuntimeError("integration certification selection requires its live operation owner")
    if (after.operationKey, after.generation) != (operation_key, generation):
        raise RuntimeError("integration certification selection belongs to another operation")
    if before is None:
        return
    if before.model_copy(update={"terminals": (), "terminalHistory": ()}) != after.model_copy(
        update={"terminals": (), "terminalHistory": ()}
    ):
        raise RuntimeError("integration frozen authority cannot be replaced")
    expected_history = before.terminalHistory
    if after.terminals[: len(before.terminals)] != before.terminals:
        if (
            not before.terminals
            or before.terminals[-1].certificate is not None
            or len(after.terminals) < len(before.terminals)
            or after.terminals[: len(before.terminals) - 1] != before.terminals[:-1]
            or after.terminals[len(before.terminals) - 1].gate != before.terminals[-1].gate
        ):
            raise RuntimeError("integration terminal references can only append an exact suffix")
        expected_history = (*expected_history, before.terminals[-1])
    if after.terminalHistory != expected_history:
        raise RuntimeError(
            "integration interrupted replacement must preserve its original terminal"
        )
