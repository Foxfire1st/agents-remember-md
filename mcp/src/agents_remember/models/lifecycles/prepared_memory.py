"""Exact in-flight memory inputs rooted in an observed real code output."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.models.certification.base import FrozenContractModel
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity


class PreparedCodeExecutionView(FrozenContractModel):
    """Logical pair authority and physical code reads remain separate identities.

    This record's consistency is checked here. Its owner must reprove the selected
    output, common repository, actual HEAD/tree and clean physical bytes before
    and after use. It grants no historical ledger mapping or memory certificate.
    """

    schemaVersion: Literal["prepared-code-execution-view/v1"] = "prepared-code-execution-view/v1"
    operationKey: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(strict=True, ge=1)
    logicalPair: MemoryCandidatePairIdentity
    preparationIntent: CertificateObjectReference
    preparedOutput: CertificateObjectReference
    physicalCodeRoot: str = Field(min_length=1, max_length=8192)
    repositoryIdentity: str = Field(min_length=1, max_length=8192)
    codeCommit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    codeTree: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    disposition: Literal["created", "existing"]
    viewDigest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _require_view(self) -> Self:
        if self.preparationIntent.kind != "preparation-intent":
            raise ValueError("prepared view requires its exact preparation intent")
        if self.preparedOutput.kind != "prepared-output":
            raise ValueError("prepared view requires its actual output reference")
        if len(self.codeCommit) != len(self.codeTree):
            raise ValueError("prepared code commit and tree use different object formats")
        if (self.physicalCodeRoot == self.logicalPair.codeRoot) != (self.disposition == "existing"):
            raise ValueError("prepared view physical root contradicts its output disposition")
        if self.viewDigest != canonical_sha256(
            self.model_dump(mode="json", exclude={"viewDigest"})
        ):
            raise ValueError("prepared view digest does not bind the complete record")
        return self


class PreparedMemoryCandidate(FrozenContractModel):
    """A current prepared-code/current-memory-tree pair, never a historical mapping."""

    schemaVersion: Literal["prepared-memory-candidate/v1"] = "prepared-memory-candidate/v1"
    codeView: PreparedCodeExecutionView
    memoryTree: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    candidateDigest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _require_candidate(self) -> Self:
        if self.candidateDigest != canonical_sha256(
            self.model_dump(mode="json", exclude={"candidateDigest"})
        ):
            raise ValueError("in-flight memory candidate digest does not match")
        return self
