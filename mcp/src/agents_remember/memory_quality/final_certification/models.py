"""Typed contracts for the final full memory-coherence certification (Gate 5)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents_remember.certification.certificate_models import (
    CoherenceSubrecordIdentity,
    GateCertificateIdentity,
    GateFiveSemanticInputs,
)
from agents_remember.certification.digests import content_digest

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_TREE_PATTERN = r"^[0-9a-f]{40,64}$"
_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

FinalItemStatus = Literal["pass", "fail", "blocked", "not-applicable"]
FinalCertificationState = Literal["green", "red", "blocked"]


class FinalCertificationModel(BaseModel):
    """Closed immutable base for one final-certification contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FinalCatalogItemIdentity(FinalCertificationModel):
    """One closed final-catalog member; identity never implies execution."""

    itemId: str = Field(pattern=_ID_PATTERN, max_length=256)
    version: str = Field(pattern=_VERSION_PATTERN)

    @property
    def key(self) -> tuple[str, str]:
        return (self.itemId, self.version)


class FinalCatalogItemResult(FinalCertificationModel):
    """One typed Gate-5 catalog result with its content-addressed subresult."""

    item: FinalCatalogItemIdentity
    status: FinalItemStatus
    findingCount: int = Field(ge=0, le=1_000_000_000)
    subresultDigest: str = Field(pattern=_DIGEST_PATTERN)
    blockedBy: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    reference: str | None = Field(default=None, max_length=16384)

    @model_validator(mode="after")
    def _require_status_shape(self) -> Self:
        if (self.status == "blocked") != bool(self.blockedBy):
            raise ValueError("a blocked final-catalog item requires a nonempty blockedBy")
        if self.status == "pass" and self.findingCount != 0:
            raise ValueError("a passing final-catalog item cannot carry findings")
        if self.status == "not-applicable" and self.findingCount != 0:
            raise ValueError("a not-applicable final-catalog item cannot carry findings")
        return self


class FinalFullCatalogPlan(FinalCertificationModel):
    """The deterministic complete final catalog planned for one exact candidate pair."""

    schemaVersion: Literal["memory-final-full-catalog-plan/v1"] = (
        "memory-final-full-catalog-plan/v1"
    )
    candidateCodeTree: str = Field(pattern=_TREE_PATTERN)
    memoryTree: str = Field(pattern=_TREE_PATTERN)
    checkerRegistryDigest: str = Field(pattern=_DIGEST_PATTERN)
    affectedClosurePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    candidatePairAuthorityDigest: str = Field(pattern=_DIGEST_PATTERN)
    catalog: tuple[FinalCatalogItemIdentity, ...] = Field(min_length=1, max_length=256)
    coherenceSubrecords: tuple[CoherenceSubrecordIdentity, ...] = Field(
        min_length=1, max_length=4096
    )
    planDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _require_canonical_plan(self) -> Self:
        _require_unique_canonical(
            self.catalog,
            key=lambda item: (item.itemId, item.version),
            label="final-catalog items",
        )
        _require_unique_canonical(
            self.coherenceSubrecords,
            key=lambda item: item.subrecordId,
            label="coherence subrecords",
        )
        payload = self.model_dump(mode="json", exclude={"planDigest"})
        if self.planDigest != content_digest(payload):
            raise ValueError("final full catalog plan digest does not match its content")
        return self


class FinalFullCatalogAttestation(FinalCertificationModel):
    """One executed complete final catalog; exhaustion of the planned population is provable."""

    schemaVersion: Literal["memory-final-full-catalog-attestation/v1"] = (
        "memory-final-full-catalog-attestation/v1"
    )
    planDigest: str = Field(pattern=_DIGEST_PATTERN)
    codeTree: str = Field(pattern=_TREE_PATTERN)
    memoryTree: str = Field(pattern=_TREE_PATTERN)
    plannedCatalog: tuple[FinalCatalogItemIdentity, ...] = Field(min_length=1, max_length=256)
    catalog: tuple[FinalCatalogItemResult, ...] = Field(min_length=1, max_length=256)
    findingCount: int = Field(ge=0, le=1_000_000_000)
    statusCounts: dict[str, int] = Field(max_length=16)
    ok: bool
    fullFinalCompleted: Literal[True] = True

    @model_validator(mode="after")
    def _require_complete_population(self) -> Self:
        _require_unique_canonical(
            self.catalog,
            key=lambda item: (item.item.itemId, item.item.version),
            label="attested final-catalog items",
        )
        planned = tuple(sorted(self.plannedCatalog, key=lambda item: item.key))
        observed = tuple(item.item for item in self.catalog)
        if observed != planned:
            raise ValueError(
                "final full catalog attestation must exhaust exactly its planned population"
            )
        counts = {
            status: sum(1 for item in self.catalog if item.status == status)
            for status in ("pass", "fail", "blocked", "not-applicable")
        }
        if self.statusCounts != counts:
            raise ValueError("final full catalog status counts must derive from its results")
        if self.ok != (counts["fail"] == 0 and counts["blocked"] == 0):
            raise ValueError("final full catalog ok must reflect every red or blocked item")
        return self


class FinalCertificationResult(FinalCertificationModel):
    """One typed Gate-5 certification: green, red, or blocked over the exact pair."""

    schemaVersion: Literal["memory-final-full-coherence-certification/v1"] = (
        "memory-final-full-coherence-certification/v1"
    )
    state: FinalCertificationState
    reason: str | None = Field(default=None, max_length=8192)
    plan: FinalFullCatalogPlan
    attestation: FinalFullCatalogAttestation
    gateFiveInputs: GateFiveSemanticInputs | None = None
    coherenceRecordDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    reusedGateOneToFour: tuple[GateCertificateIdentity, ...] = Field(max_length=4)
    certificateReusePlanDigest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    finalizationEligible: bool

    @model_validator(mode="after")
    def _require_state_shape(self) -> Self:
        self._require_memory_binding()
        if self.state == "green":
            self._require_green_state()
        elif self.finalizationEligible is not False:
            raise ValueError("only a green final certification can be finalization-eligible")
        return self

    def _require_memory_binding(self) -> None:
        if self.plan.memoryTree != self.attestation.memoryTree:
            raise ValueError("final certification must bind the exact attested memory tree")
        if self.attestation.planDigest != self.plan.planDigest:
            raise ValueError("final certification attestation must bind its exact catalog plan")
        if self.gateFiveInputs is None:
            return
        if self.gateFiveInputs.memoryTree.kind != "git-tree":
            raise ValueError("final certification memory input must be an exact Git tree")
        if self.gateFiveInputs.memoryTree.value != self.plan.memoryTree:
            raise ValueError("final certification memory input must name the exact bound tree")

    def _require_green_state(self) -> None:
        if not self.attestation.ok:
            raise ValueError("a green final certification requires a fully passing catalog")
        if self.gateFiveInputs is None:
            raise ValueError("a green final certification requires assembled Gate-5 inputs")
        if self.coherenceRecordDigest is None:
            raise ValueError("a green final certification requires a current coherence record")
        if not self.reusedGateOneToFour:
            raise ValueError("a green final certification reuses the exact green Gate 1-4 prefix")
        if self.finalizationEligible is not True:
            raise ValueError("a green final certification is finalization-eligible")


def _require_unique_canonical(values: tuple, *, key, label: str) -> None:
    observed = tuple(key(item) for item in values)
    if observed != tuple(sorted(observed)) or len(observed) != len(set(observed)):
        raise ValueError(f"{label} must be unique and canonical")


__all__ = [
    "FinalCatalogItemIdentity",
    "FinalCatalogItemResult",
    "FinalCertificationResult",
    "FinalCertificationState",
    "FinalFullCatalogAttestation",
    "FinalFullCatalogPlan",
    "FinalItemStatus",
]
