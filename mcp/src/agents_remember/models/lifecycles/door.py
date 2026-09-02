"""Versioned contract-owned closeout-door source generations."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents_remember.models.closeout.source import (
    CandidateAdmissionFacts,
    SchedulingGradeInput,
)
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.operation_kinds import LifecycleOperationKind
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentState

CloseoutDoorDisposition = Literal["waiting", "deferred", "withdrawn", "claimed"]
DoorPublicationState = Literal["intent", "proven"]
DoorPriority = Literal["critical", "high", "normal", "low"]
CloseoutDoorAction = Literal[
    "status",
    "declare",
    "defer",
    "resume",
    "withdraw",
    "update-provenance",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DoorEvidenceFact(_StrictModel):
    path: str = Field(min_length=1, max_length=8192)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DoorProvenance(_StrictModel):
    """One typed, content-bound provenance leg."""

    state: Literal["proven", "not-applicable"]
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: list[DoorEvidenceFact] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def _not_applicable_has_no_evidence(self) -> DoorProvenance:
        if self.state == "not-applicable" and self.evidence:
            raise ValueError("not-applicable provenance cannot carry evidence")
        return self


class DoorAdmissionProvenance(_StrictModel):
    resourceReady: bool = True
    resourceReason: str = Field(default="", max_length=8192)
    admissionReady: bool = True
    admissionReason: str = Field(default="", max_length=8192)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _false_facts_are_explained(self) -> Self:
        self.resourceReason = self.resourceReason.strip()
        self.admissionReason = self.admissionReason.strip()
        if not self.resourceReady and not self.resourceReason:
            raise ValueError("resourceReason is required when resourceReady is false")
        if not self.admissionReady and not self.admissionReason:
            raise ValueError("admissionReason is required when admissionReady is false")
        return self


class DoorSchedulingProvenance(_StrictModel):
    priority: DoorPriority
    judgmentId: str = Field(min_length=1, max_length=256)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: list[DoorEvidenceFact] = Field(default_factory=list, max_length=256)


class CloseoutDoorGeneration(_StrictModel):
    """One complete immutable identity whose disposition is the only mutable source cell."""

    schemaVersion: Literal["ar-closeout-door/v1"] = "ar-closeout-door/v1"
    generationId: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessorGenerationId: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    disposition: CloseoutDoorDisposition
    taskId: str = Field(min_length=1, max_length=4096)
    taskName: str = Field(min_length=1, max_length=4096)
    taskDocumentRef: TaskDocumentRef
    owningMasterTaskDocumentRef: TaskDocumentRef
    sprintTaskDocumentRef: TaskDocumentRef
    contractPath: str = Field(min_length=1, max_length=8192)
    candidateTree: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    memoryCandidateTree: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    codeBaseCommit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    memoryBaseCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    ledgerMemoryCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    taskTopologyFingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    taskIntent: TaskIntentState
    reviewProvenance: DoorProvenance
    memoryProvenance: DoorProvenance
    ledgerProvenance: DoorProvenance
    admissionProvenance: DoorAdmissionProvenance
    schedulingProvenance: DoorSchedulingProvenance
    declaredBy: str = Field(min_length=1, max_length=8192)
    declaredAt: str = Field(min_length=1, max_length=256)
    operationKind: LifecycleOperationKind | None = None
    operationFingerprint: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    claimedOperationKey: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def _decode_legacy_missing_intent(cls, value: Any) -> Any:
        if isinstance(value, dict) and "taskIntent" not in value:
            return {**value, "taskIntent": {"state": "missing-intent"}}
        return value

    @model_validator(mode="after")
    def _identity_is_coherent(self) -> CloseoutDoorGeneration:
        claimed = self.disposition == "claimed"
        operation_cells = (
            self.operationKind is not None,
            bool(self.operationFingerprint),
            bool(self.claimedOperationKey),
        )
        if (claimed and not all(operation_cells)) or (not claimed and any(operation_cells)):
            raise ValueError(
                "claimed door requires one exact operation; source dispositions carry none"
            )
        if self.taskDocumentRef.repository != self.sprintTaskDocumentRef.repository or (
            self.owningMasterTaskDocumentRef.repository != self.sprintTaskDocumentRef.repository
        ):
            raise ValueError("door task, master, and sprint references must share a repository")
        return self


class DoorPublicationEvidence(_StrictModel):
    """Write-once intent/proof for one exact contract publication."""

    state: DoorPublicationState
    generation: CloseoutDoorGeneration
    expectedBeforeContractSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expectedPublishedContractSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observedPublishedContractSha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _proof_is_exact(self) -> DoorPublicationEvidence:
        if self.state == "intent" and self.observedPublishedContractSha256 is not None:
            raise ValueError("door publication intent cannot claim observed publication")
        if self.state == "proven" and (
            self.observedPublishedContractSha256 != self.expectedPublishedContractSha256
        ):
            raise ValueError("door publication proof must match the intended contract bytes")
        return self


class CloseoutDoorRequest(_StrictModel):
    """Contract-addressed source control, distinct from projection operations."""

    action: CloseoutDoorAction
    contract_path: str = Field(min_length=1, max_length=8192)
    candidate_task_document_ref: TaskDocumentRef | None = None
    expected_generation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    grade: SchedulingGradeInput | None = None
    admission: CandidateAdmissionFacts | None = None
    caller: DeclaredCaller | None = None

    @model_validator(mode="after")
    def _payload_matches_action(self) -> CloseoutDoorRequest:
        source_write = self.action in {"declare", "update-provenance"}
        if source_write != (self.grade is not None and self.admission is not None):
            raise ValueError(
                "declare/update-provenance require grade and admission; other actions forbid them"
            )
        if not source_write and self.candidate_task_document_ref is not None:
            raise ValueError("only source publication actions accept a candidate task assertion")
        generation_required = self.action in {
            "defer",
            "resume",
            "withdraw",
            "update-provenance",
        }
        if generation_required != (self.expected_generation_id is not None):
            raise ValueError(
                "door transition/update actions require expected_generation_id; status/declare forbid it"
            )
        return self
