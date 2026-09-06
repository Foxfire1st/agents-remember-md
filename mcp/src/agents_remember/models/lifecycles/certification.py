"""Typed immutable certification selections owned by one lifecycle journal generation."""

from __future__ import annotations

import hashlib
from itertools import pairwise
from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.models.certification.base import FrozenContractModel, GateId
from agents_remember.models.certification.references import CertificateObjectReference

_DIGEST = r"^[0-9a-f]{64}$"


class RetainedCertificationBytes(FrozenContractModel):
    """Exact owner-produced bytes, decoded by their domain owner before selection or use."""

    schemaVersion: Literal["closeout-retained-certification-bytes/v1"] = (
        "closeout-retained-certification-bytes/v1"
    )
    canonicalBytes: str = Field(min_length=1, max_length=8_388_608)
    contentSha256: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def _require_bytes(self) -> Self:
        if hashlib.sha256(self.canonicalBytes.encode("utf-8")).hexdigest() != self.contentSha256:
            raise ValueError("retained certification bytes differ from their selected hash")
        return self


class SelectedRecoveryDecision(FrozenContractModel):
    """One original R21 decision and the exact memory observation used to compile it."""

    reference: CertificateObjectReference
    memoryInputs: RetainedCertificationBytes | None = None

    @model_validator(mode="after")
    def _require_recovery_reference(self) -> Self:
        if self.reference.kind != "recovery":
            raise ValueError("recovery selection contains another object kind")
        return self


class CertificationPredecessor(FrozenContractModel):
    operationKey: str = Field(pattern=_DIGEST)
    generation: int = Field(ge=1)
    frozenRun: CertificateObjectReference
    candidateAuthorities: CertificateObjectReference
    lifecycleAdmission: CertificateObjectReference

    @model_validator(mode="after")
    def _require_admission(self) -> Self:
        for reference, kind in (
            (self.frozenRun, "frozen-run"),
            (self.candidateAuthorities, "candidate-authorities"),
            (self.lifecycleAdmission, "lifecycle-admission"),
        ):
            if reference.kind != kind:
                raise ValueError(f"certification predecessor requires an exact {kind} reference")
        return self


class SelectedGateTerminal(FrozenContractModel):
    gate: GateId
    result: CertificateObjectReference
    certificate: CertificateObjectReference | None = None
    publication: RetainedCertificationBytes
    reusedFrom: CertificationPredecessor | None = None

    @model_validator(mode="after")
    def _require_reference_kinds(self) -> Self:
        if self.result.kind != "result-manifest":
            raise ValueError("gate terminal requires an exact result manifest reference")
        if self.certificate is not None and self.certificate.kind != "certificate":
            raise ValueError("gate certificate requires an exact certificate reference")
        if self.reusedFrom is not None and self.certificate is None:
            raise ValueError("only certified predecessor terminals can be reused")
        return self


class OperationCertificationState(FrozenContractModel):
    """Only this CAS-selected state authorizes certification; object existence never does."""

    schemaVersion: Literal["lifecycle-certification-selection/v1"] = (
        "lifecycle-certification-selection/v1"
    )
    operationKey: str = Field(pattern=_DIGEST)
    generation: int = Field(ge=1)
    frozenRun: CertificateObjectReference
    candidateAuthorities: CertificateObjectReference
    lifecycleAdmission: CertificateObjectReference
    priorRedDisposition: CertificateObjectReference | None = None
    predecessor: CertificationPredecessor | None = None
    inputTerminals: tuple[SelectedGateTerminal, ...] = Field(default_factory=tuple, max_length=5)
    recoveryDecisions: tuple[SelectedRecoveryDecision, ...] = Field(min_length=1, max_length=256)
    terminals: tuple[SelectedGateTerminal, ...] = Field(default_factory=tuple, max_length=5)
    terminalHistory: tuple[SelectedGateTerminal, ...] = Field(default_factory=tuple, max_length=256)

    @model_validator(mode="after")
    def _require_selected_shape(self) -> Self:
        for reference, kind in (
            (self.frozenRun, "frozen-run"),
            (self.candidateAuthorities, "candidate-authorities"),
            (self.lifecycleAdmission, "lifecycle-admission"),
            (self.priorRedDisposition, "prior-red-disposition"),
        ):
            if reference is not None and reference.kind != kind:
                raise ValueError(f"certification selection requires an exact {kind} reference")
        if any(before == after for before, after in pairwise(self.recoveryDecisions)):
            raise ValueError("adjacent recovery decisions cannot be selected twice")
        for terminals in (self.inputTerminals, self.terminals):
            gates = tuple(item.gate for item in terminals)
            if gates != tuple(sorted(set(gates))):
                raise ValueError("selected terminal gates must be unique and ordered")
        if self.inputTerminals and self.predecessor is None:
            raise ValueError("inherited terminal inputs require an explicit predecessor")
        if any(
            item.certificate is not None or item.reusedFrom is not None
            for item in self.terminalHistory
        ):
            raise ValueError("terminal history retains only original uncertified attempts")
        history_gates = tuple(item.gate for item in self.terminalHistory)
        if history_gates != tuple(sorted(history_gates)) or len(set(self.terminalHistory)) != len(
            self.terminalHistory
        ):
            raise ValueError(
                "terminal history must be ordered and contain each original attempt once"
            )
        return self


def validate_certification_owner(
    selected: OperationCertificationState | None,
    operation_kind: str,
    operation_key: str,
    generation: int,
) -> None:
    if selected is not None and (
        operation_kind != "closeout"
        or (selected.operationKey, selected.generation) != (operation_key, generation)
    ):
        raise ValueError(
            "certification selection must name its exact closeout operation generation"
        )


def validate_certification_transition(
    before: OperationCertificationState | None,
    after: OperationCertificationState | None,
    *,
    operation_key: str,
    generation: int,
    can_select: bool,
) -> None:
    """Selection is generation-bound and append-only, independently of worker metadata."""
    if before == after:
        return
    if not can_select or after is None:
        raise RuntimeError("certification selection requires the live noncancelled operation")
    if (after.operationKey, after.generation) != (operation_key, generation):
        raise RuntimeError("certification selection belongs to another operation generation")
    if before is None:
        return
    for name in (
        "frozenRun",
        "candidateAuthorities",
        "lifecycleAdmission",
        "priorRedDisposition",
        "predecessor",
        "inputTerminals",
    ):
        if getattr(before, name) != getattr(after, name):
            raise RuntimeError(f"selected certification authority cannot change {name}")
    if after.recoveryDecisions[: len(before.recoveryDecisions)] != before.recoveryDecisions:
        raise RuntimeError("selected recovery decision history cannot be replaced")
    _validate_terminal_transition(before, after)


def _validate_terminal_transition(
    before: OperationCertificationState, after: OperationCertificationState
) -> None:
    expected_history = before.terminalHistory
    if after.terminals[: len(before.terminals)] != before.terminals:
        if (
            not before.terminals
            or before.terminals[-1].certificate is not None
            or len(after.terminals) < len(before.terminals)
            or after.terminals[: len(before.terminals) - 1] != before.terminals[:-1]
            or after.terminals[len(before.terminals) - 1].gate != before.terminals[-1].gate
        ):
            raise RuntimeError("selected terminal evidence cannot be replaced")
        expected_history = (*expected_history, before.terminals[-1])
    if after.terminalHistory != expected_history:
        raise RuntimeError(
            "uncertified terminal replacement must retain the exact original history"
        )
