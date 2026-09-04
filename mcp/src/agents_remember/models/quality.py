"""Public response contract for lifecycle-owned quality-gate results."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agents_remember.models.base import StrictResponseModel
from agents_remember.models.task_document_ref import TaskDocumentRef


class QualityMemoryPolicy(StrictResponseModel):
    """How the admitted repository adapter's container memory is governed."""

    mode: Literal["container-host-managed", "explicit-cap"]
    processPolicy: Literal["profile-adapter-owned"]
    swap: Literal["container-host-managed"]


class QualityMemoryCap(StrictResponseModel):
    """One explicit lifecycle-requested cap inside the Dagger graph."""

    capBytes: int = Field(gt=0)
    policy: Literal["dagger-inner-wrapper"]
    mechanism: Literal["container-wrapper"]


class CertifyingTestEvidenceResult(StrictResponseModel):
    """Public description of the governed evidence; not a loadable capability."""

    schemaVersion: Literal["python-test-evidence/v1"]
    altitude: Literal["certifying"]
    candidateTree: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    resultSha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class QualityGateResult(StrictResponseModel):
    """Stable public meanings shared by closeout and integration quality results."""

    required: bool | None = None
    status: str | None = None
    passed: bool | None = None
    command: str | None = Field(default=None, max_length=32768)
    reason: str | None = Field(default=None, max_length=8192)
    diffBase: str | None = None
    mode: str | None = None
    executor: str | None = None
    executorAdapterId: str | None = None
    profileDigest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    profilePlanDigest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    profileSelectionId: str | None = None
    runtimeAuthorityDigest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resultArtifact: str | None = None
    reportPath: str | None = Field(default=None, max_length=16384)
    publishedResultPath: str | None = Field(default=None, max_length=16384)
    memoryPolicy: QualityMemoryPolicy | None = None
    memoryCap: QualityMemoryCap | None = None
    preCommitHook: str | None = None
    recoveredPublishedReport: bool | None = None
    reusedCertification: bool | None = None
    scope: str | None = None
    completionFingerprint: str | None = None
    masterTaskDocumentRef: TaskDocumentRef | None = None
    certifyingTestEvidence: CertifyingTestEvidenceResult | None = None
