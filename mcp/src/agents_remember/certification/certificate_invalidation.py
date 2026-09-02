"""Deterministic gate invalidation and exact certificate reuse planning."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.certificate_authority import validate_certificate_chain
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    GateCertificate,
    GateCertificateIdentity,
    GateFiveSemanticInputs,
)
from agents_remember.certification.models import FrozenContractModel, GateId, SemanticText
from agents_remember.errors import CertificationContractError

InputChangeClass = Literal[
    "code",
    "gate-1-input",
    "gate-2-input",
    "gate-3-input",
    "gate-4-input",
    "runtime-toolchain-executor-image",
    "memory-onboarding",
    "coherence-evidence",
    "topology-intent",
    "journal-review-approval-attempt",
    "unchanged-interruption",
    "unclassified",
]
_ALL_GATES: tuple[GateId, ...] = (1, 2, 3, 4, 5)
_DOWNSTREAM_GATES: dict[GateId, tuple[GateId, ...]] = {
    gate: _ALL_GATES[index:] for index, gate in enumerate(_ALL_GATES)
}


class CertificateInputChange(FrozenContractModel):
    changeClass: InputChangeClass
    consumingGates: tuple[GateId, ...] = Field(default_factory=tuple, max_length=5)
    affectedGateFiveSubrecords: tuple[SemanticText, ...] = Field(
        default_factory=tuple,
        max_length=4096,
    )
    reason: SemanticText = Field(max_length=4096)

    @model_validator(mode="after")
    def _require_change_shape(self) -> Self:
        if self.consumingGates != tuple(sorted(set(self.consumingGates))):
            raise ValueError("input-change consuming gates must be unique and ordered")
        consumes_declared = self.changeClass in {
            "runtime-toolchain-executor-image",
            "topology-intent",
        }
        if not consumes_declared and self.consumingGates:
            raise ValueError("this input-change class has a fixed invalidation scope")
        if self.changeClass == "runtime-toolchain-executor-image" and not self.consumingGates:
            raise ValueError("runtime input changes require every declared consuming gate")
        if self.changeClass != "coherence-evidence" and self.affectedGateFiveSubrecords:
            raise ValueError("only coherence changes can name affected Gate-5 subrecords")
        if self.affectedGateFiveSubrecords != tuple(sorted(set(self.affectedGateFiveSubrecords))):
            raise ValueError("affected Gate-5 subrecords must be unique and ordered")
        return self


class CertificateInvalidationDecision(FrozenContractModel):
    schemaVersion: Literal["closeout-certificate-invalidation/v1"] = (
        "closeout-certificate-invalidation/v1"
    )
    invalidatedGates: tuple[GateId, ...]
    retainedGates: tuple[GateId, ...]
    affectedGateFiveSubrecords: tuple[SemanticText, ...]
    finalizationRevalidationRequired: bool


class CertificateReusePlan(FrozenContractModel):
    schemaVersion: Literal["closeout-certificate-reuse-plan/v1"] = (
        "closeout-certificate-reuse-plan/v1"
    )
    reusedCertificates: tuple[GateCertificateIdentity, ...]
    invalidatedGates: tuple[GateId, ...]
    firstGateToRun: GateId | None
    finalizationRevalidationRequired: bool
    zeroGateStarts: bool


def classify_certificate_invalidation(
    changes: Sequence[CertificateInputChange],
) -> CertificateInvalidationDecision:
    """Apply the normative matrix and downstream dependency closure."""

    invalidated: set[GateId] = set()
    subrecords: set[str] = set()
    revalidate_finalization = False
    for change in changes:
        starts = _change_start_gates(change)
        for gate in starts:
            invalidated.update(_DOWNSTREAM_GATES[gate])
        if change.changeClass == "coherence-evidence":
            subrecords.update(change.affectedGateFiveSubrecords)
        if change.changeClass not in {"unchanged-interruption"}:
            revalidate_finalization = True
        if change.changeClass in {
            "topology-intent",
            "journal-review-approval-attempt",
            "unchanged-interruption",
        }:
            revalidate_finalization = True
    ordered = tuple(sorted(invalidated))
    retained_items: list[GateId] = []
    for gate in _ALL_GATES:
        if gate not in invalidated:
            retained_items.append(gate)
    return CertificateInvalidationDecision(
        invalidatedGates=ordered,
        retainedGates=tuple(retained_items),
        affectedGateFiveSubrecords=tuple(sorted(subrecords)),
        finalizationRevalidationRequired=revalidate_finalization,
    )


def plan_certificate_reuse(
    admission: CertificationAdmissionManifest,
    certificates: Sequence[GateCertificate],
    changes: Sequence[CertificateInputChange],
    *,
    gate_five_inputs: GateFiveSemanticInputs | None = None,
) -> CertificateReusePlan:
    """Reuse only a current exact prefix and resume the first unfinished boundary."""

    chain = tuple(certificates)
    if tuple(item.semanticEnvelope.gate for item in chain) != tuple(range(1, len(chain) + 1)):
        raise ValueError("candidate certificates must form one exact ordered prefix")
    decision = classify_certificate_invalidation(changes)
    invalidated = set(decision.invalidatedGates)
    invalidated.update(_identity_drift_closure(admission, chain, gate_five_inputs))
    retained_count = 0
    for certificate in chain:
        if certificate.semanticEnvelope.gate in invalidated:
            break
        retained_count += 1
    retained = chain[:retained_count]
    validate_certificate_chain(
        admission,
        retained,
        gate_five_inputs=gate_five_inputs,
    )
    missing: set[GateId] = set(_ALL_GATES[len(chain) :])
    candidates = invalidated | missing
    first_gate = min(candidates) if candidates else None
    ordered_invalidated = _DOWNSTREAM_GATES[first_gate] if first_gate else ()
    return CertificateReusePlan(
        reusedCertificates=tuple(item.identity for item in retained),
        invalidatedGates=ordered_invalidated,
        firstGateToRun=first_gate,
        finalizationRevalidationRequired=(
            decision.finalizationRevalidationRequired or first_gate is None
        ),
        zeroGateStarts=first_gate is None,
    )


def _change_start_gates(change: CertificateInputChange) -> tuple[GateId, ...]:
    fixed: dict[InputChangeClass, tuple[GateId, ...]] = {
        "code": (1,),
        "gate-1-input": (1,),
        "gate-2-input": (2,),
        "gate-3-input": (3,),
        "gate-4-input": (4,),
        "memory-onboarding": (5,),
        "coherence-evidence": (5,),
        "journal-review-approval-attempt": (),
        "unchanged-interruption": (),
    }
    if change.changeClass in fixed:
        return fixed[change.changeClass]
    if change.changeClass == "unclassified":
        return (1,)
    return change.consumingGates


def _identity_drift_closure(
    admission: CertificationAdmissionManifest,
    chain: tuple[GateCertificate, ...],
    gate_five_inputs: GateFiveSemanticInputs | None,
) -> set[GateId]:
    for index, certificate in enumerate(chain):
        try:
            validate_certificate_chain(
                admission,
                chain[: index + 1],
                gate_five_inputs=gate_five_inputs,
            )
        except CertificationContractError:
            return set(_DOWNSTREAM_GATES[certificate.semanticEnvelope.gate])
    return set()


__all__ = [
    "CertificateInputChange",
    "CertificateInvalidationDecision",
    "CertificateReusePlan",
    "InputChangeClass",
    "classify_certificate_invalidation",
    "plan_certificate_reuse",
]
