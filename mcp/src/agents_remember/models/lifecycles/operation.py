"""Wire and durable vocabularies for task-addressed lifecycle operations."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents_remember.models.closeout.input import EffectiveCloseoutInput, EnabledCloseoutLeg
from agents_remember.models.lifecycles.direct_landing import (
    DirectLandingLedgerIntent,
    DirectLandingOperationInput,
)
from agents_remember.models.lifecycles.door import DoorPublicationEvidence
from agents_remember.models.lifecycles.evidence_dependencies import (
    EVIDENCE_DEPENDENCY_VALIDATOR,
    EvidenceDependencies,
    EvidenceDependencyError,
    EvidenceRecordType,
    build_evidence_dependencies,
    canonical_sha256,
    dependency,
    require_evidence_dependencies,
)
from agents_remember.models.lifecycles.legacy import LegacyCloseoutMigrationProof
from agents_remember.models.lifecycles.mutation_evidence import (
    CloseoutMutationLeg,
    GitMutationEvidence,
)
from agents_remember.models.lifecycles.operation_kinds import (
    LifecycleOperationKind,
    LifecycleOperationPhase,
    LifecycleOperationStatus,
)
from agents_remember.models.lifecycles.operation_projection import (
    LifecycleOperationProjection,  # noqa: F401 - public re-export
)
from agents_remember.models.lifecycles.policy import GatePolicyRuleSnapshot
from agents_remember.models.lifecycles.termination import (
    LifecycleCancellationEvidence,
    WorkerTerminationEvidence,
)
from agents_remember.models.task_intent import TaskIntentIdentity, TaskIntentState

IntegrateStrategy = Literal["ff-only", "replay"]


class LifecycleOperationRecoveryCommits(BaseModel):
    """Exact irreversible outputs persisted before contract finalization."""

    model_config = ConfigDict(extra="forbid")

    codeCommit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    memoryContentCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    ledgerCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")


class OrganizationalTaskPublicationIntent(BaseModel):
    """Exact before/intended bytes for one organizational master completion."""

    model_config = ConfigDict(extra="forbid")

    masterTaskDocument: str = Field(min_length=1, max_length=4096)
    sprintTaskDocument: str = Field(min_length=1, max_length=4096)
    candidateTaskDocument: str = Field(min_length=1, max_length=4096)
    completionFingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    certificationResultSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completedAt: str = Field(min_length=1, max_length=128)
    acceptedJson: str
    acceptedJsonSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intendedJson: str
    intendedJsonSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptedMarkdown: str
    acceptedMarkdownSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intendedMarkdown: str
    intendedMarkdownSha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _bytes_match_hashes(self) -> OrganizationalTaskPublicationIntent:
        for content, expected in (
            (self.acceptedJson, self.acceptedJsonSha256),
            (self.intendedJson, self.intendedJsonSha256),
            (self.acceptedMarkdown, self.acceptedMarkdownSha256),
            (self.intendedMarkdown, self.intendedMarkdownSha256),
        ):
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != expected:
                raise ValueError("organizational task publication byte digest does not match")
        return self


class IntegrationPublicationIntent(BaseModel):
    """Journal authority transferred from one claimed door and source operation."""

    model_config = ConfigDict(extra="forbid")

    operationKey: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(ge=1)
    preparedAt: str = Field(min_length=1, max_length=128)
    claimState: Literal["not-applicable", "intent", "proven"]
    claimTransferredAt: str | None = Field(default=None, min_length=1, max_length=128)
    sprintTaskDocument: str = Field(default="", max_length=4096)
    candidateTaskDocument: str = Field(default="", max_length=4096)
    doorGenerationId: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    sourceOperationKind: LifecycleOperationKind | None = None
    sourceOperationGeneration: int | None = Field(default=None, ge=1)
    sourceOperationFingerprint: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    sourceOperationKey: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    sourceJournalSha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    organizationalCompletion: OrganizationalTaskPublicationIntent | None = None

    @model_validator(mode="after")
    def _source_identity_is_complete(self) -> IntegrationPublicationIntent:
        cells = (
            self.sprintTaskDocument,
            self.candidateTaskDocument,
            self.doorGenerationId,
            self.sourceOperationKind,
            self.sourceOperationGeneration,
            self.sourceOperationFingerprint,
            self.sourceOperationKey,
            self.sourceJournalSha256,
        )
        if any(cells) != all(cells):
            raise ValueError("integration publication source claim identity is partial")
        if bool(self.doorGenerationId) != (self.claimState != "not-applicable"):
            raise ValueError("integration publication claim state contradicts source identity")
        if (self.claimState == "proven") != (self.claimTransferredAt is not None):
            raise ValueError("proven integration claim transfer requires its timestamp")
        return self


class IntegrationConflictTransaction(BaseModel):
    """A durable, non-mutating handoff back to the exact leaf worktree."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["resolution-required"] = "resolution-required"
    codeReplayRequired: bool
    memoryReplayRequired: bool
    codeSourceRef: str = Field(pattern=r"^refs/heads/.+$", max_length=4096)
    codeSourceCommit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    codeCandidateCommit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    memorySourceRef: str = Field(default="", pattern=r"^$|^refs/heads/.+$", max_length=4096)
    memorySourceCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    memoryContentCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    ledgerCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    codeWorktree: str = Field(min_length=1, max_length=4096)
    memoryWorktree: str = Field(default="", max_length=4096)
    resolutionOwner: Literal["leaf-closeout"] = "leaf-closeout"


class IntegrationOperationAuthority(BaseModel):
    """Exact source tips and closed candidate accepted by one integration operation."""

    model_config = ConfigDict(extra="forbid")

    targetKind: Literal["sprint-super", "atomic-integration"]
    codeRepository: str = Field(min_length=1, max_length=4096)
    codeSourceBranch: str = Field(min_length=1, max_length=4096)
    codeSourceRef: str = Field(pattern=r"^refs/heads/.+$", max_length=4096)
    codeSourceCommit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    codeCandidateCommit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    memoryRepository: str = Field(default="", max_length=4096)
    memorySourceBranch: str = Field(default="", max_length=4096)
    memorySourceRef: str = Field(default="", pattern=r"^$|^refs/heads/.+$", max_length=4096)
    memorySourceCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    memoryContentCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    ledgerCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    conflictTransaction: IntegrationConflictTransaction | None = None


class OrganizationalCompletionRepairEvidence(BaseModel):
    """Immutable identity of the one reset generation authorized by cancellation."""

    model_config = ConfigDict(extra="forbid")

    operationKey: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidateState: str = Field(pattern=r"^[0-9a-f]{64}$")
    contractPath: str = Field(min_length=1, max_length=4096)
    taskId: str = Field(min_length=1, max_length=4096)
    taskName: str = Field(min_length=1, max_length=4096)
    sprintTaskDocument: str = Field(min_length=1, max_length=4096)
    candidateTaskDocument: str = Field(min_length=1, max_length=4096)
    owningMasterTaskDocument: str = Field(min_length=1, max_length=4096)
    codeCommit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    memoryContentCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    ledgerCommit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")
    acceptedContractSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resetContractSha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class IntegrationQualityCertification(BaseModel):
    """Durable proof that one exact organizational completion candidate passed full Dagger."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["organizational-master-completion"] = "organizational-master-completion"
    completionFingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    codeCommit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    candidateTree: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    attestation: dict[str, str]
    resultSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: dict[str, Any]

    @model_validator(mode="after")
    def _passed_result_is_exact(self) -> IntegrationQualityCertification:
        payload = json.dumps(self.result, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        _require_quality_certification_attestation(self)
        _require_quality_certification_result(self)
        _require_quality_certification_memory(self)
        if self.resultSha256 != digest:
            raise ValueError("integration quality certification result digest does not match")
        return self


_QUALITY_ATTESTATION_KEYS = {
    "kind",
    "completionFingerprint",
    "codeCommit",
    "candidateTree",
    "diffBase",
    "mode",
    "executor",
    "memoryCapBytes",
}


def _require_quality_certification_attestation(
    certification: IntegrationQualityCertification,
) -> None:
    attestation = certification.attestation
    if set(attestation) != _QUALITY_ATTESTATION_KEYS:
        raise ValueError("integration quality certification attestation is incomplete")
    if (
        attestation["kind"] != certification.kind
        or attestation["completionFingerprint"] != certification.completionFingerprint
        or attestation["codeCommit"] != certification.codeCommit
        or attestation["candidateTree"] != certification.candidateTree
        or attestation["mode"] != "full"
        or attestation["executor"] != "dagger"
    ):
        raise ValueError("integration quality certification attestation is inconsistent")


def _require_quality_certification_result(
    certification: IntegrationQualityCertification,
) -> None:
    result = certification.result
    if (
        result.get("required") is not True
        or result.get("status") != "enforced"
        or result.get("passed") is not True
        or result.get("mode") != "full"
        or result.get("executor") != "dagger"
        or result.get("diffBase") != certification.attestation["diffBase"]
    ):
        raise ValueError("integration quality certification requires the exact full Dagger gate")


def _require_quality_certification_memory(
    certification: IntegrationQualityCertification,
) -> None:
    cap = certification.attestation["memoryCapBytes"]
    memory_cap = certification.result.get("memoryCap")
    memory_policy = certification.result.get("memoryPolicy")
    if not isinstance(memory_policy, dict):
        raise ValueError("integration quality certification has no exact memory policy")
    if cap:
        if (
            memory_policy.get("mode") != "explicit-cap"
            or not isinstance(memory_cap, dict)
            or str(memory_cap.get("capBytes")) != cap
        ):
            raise ValueError("integration quality certification memory cap does not match")
    elif memory_cap is not None or memory_policy.get("mode") != "container-host-managed":
        raise ValueError("integration quality certification memory policy does not match")


class CloseoutOperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["closeout"] = "closeout"
    configPath: str
    contractPath: str
    effectiveInput: EffectiveCloseoutInput
    approvalNote: str
    gatePolicy: list[GatePolicyRuleSnapshot] = Field(default_factory=list)


class IntegrateOperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["integrate"] = "integrate"
    configPath: str
    contractPath: str
    strategy: IntegrateStrategy = "ff-only"
    ledgerCommitMessage: str = ""
    gatePolicy: list[GatePolicyRuleSnapshot] = Field(default_factory=list)
    autoCompleteSeats: bool = True


LifecycleOperationInput = Annotated[
    CloseoutOperationInput | IntegrateOperationInput | DirectLandingOperationInput,
    Field(discriminator="kind"),
]


class LifecycleOperationRecord(BaseModel):
    """The validated, internal operation snapshot stored in an enclosure."""

    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal["3.0"] = "3.0"
    taskId: str
    taskName: str
    contractPath: str
    operationKind: LifecycleOperationKind
    candidateState: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidateTree: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    taskIntent: TaskIntentState | None = None
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    operationKey: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(default=1, ge=1)
    recordRevision: int = Field(default=1, ge=1)
    predecessorFingerprint: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    successorFingerprint: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    generationDisposition: Literal["active", "cancelled", "retired", "superseded"] = "active"
    supersedeDeclarationFingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    integrationAuthority: IntegrationOperationAuthority | None = None
    input: LifecycleOperationInput
    status: LifecycleOperationStatus
    phase: LifecycleOperationPhase
    queuedAt: str
    startedAt: str | None = None
    heartbeatAt: str | None = None
    finishedAt: str | None = None
    currentCommand: str = ""
    reportPath: str
    result: dict[str, Any] | None = None
    failure: str | None = None
    guidance: str | None = None
    cancelRequested: bool = False
    irreversibleBoundaryEntered: bool = False
    approvalClaimed: bool = False
    mutationEvidence: dict[CloseoutMutationLeg, GitMutationEvidence] = Field(default_factory=dict)
    mutationHistory: dict[CloseoutMutationLeg, list[GitMutationEvidence]] = Field(
        default_factory=dict
    )
    recoveryCommits: LifecycleOperationRecoveryCommits | None = None
    closeoutFinalizedContractSha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    qualityCertification: IntegrationQualityCertification | None = None
    integrationPublication: IntegrationPublicationIntent | None = None
    organizationalRepair: OrganizationalCompletionRepairEvidence | None = None
    doorPublication: DoorPublicationEvidence | None = None
    doorPublicationHistory: list[DoorPublicationEvidence] = Field(
        default_factory=list,
        max_length=256,
    )
    directLandingLedgerIntent: DirectLandingLedgerIntent | None = None
    attempt: int = Field(default=1, ge=1)
    workerPid: int | None = Field(default=None, ge=1)
    workerLease: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    workerProcessFingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    workerTermination: WorkerTerminationEvidence | None = None
    workerTerminationHistory: list[WorkerTerminationEvidence] = Field(default_factory=list)
    terminationReturnStatus: (
        Literal["queued", "running", "input-required", "failed", "completed"] | None
    ) = None
    terminationReturnPhase: LifecycleOperationPhase | None = None
    cancellationEvidence: LifecycleCancellationEvidence | None = None
    legacyMigration: LegacyCloseoutMigrationProof | None = None
    dependencies: EvidenceDependencies | None = None

    @model_validator(mode="before")
    @classmethod
    def _decode_legacy_missing_intent(cls, value: Any) -> Any:
        if (
            isinstance(value, dict)
            and value.get("operationKind") in {"closeout", "direct-landing"}
            and "taskIntent" not in value
        ):
            return {**value, "taskIntent": {"state": "missing-intent"}}
        return value

    @model_validator(mode="after")
    def _require_altitude_authority(self) -> LifecycleOperationRecord:
        _require_altitude_authority(self)
        return self


def lifecycle_operation_dependencies(
    record: LifecycleOperationRecord,
) -> EvidenceDependencies:
    """Declare the admitted candidate, door, plan, and normalized operation input."""

    record_type: EvidenceRecordType
    if record.operationKind == "closeout":
        record_type = "lifecycle-closeout-operation/v3"
    elif record.operationKind == "direct-landing":
        record_type = "lifecycle-direct-landing-operation/v3"
    else:
        record_type = "lifecycle-integration-operation/v3"
    input_value = record.input.model_dump(mode="json")
    edges = [
        dependency("candidate-state", "accepted-candidate", record.candidateState),
        dependency("operation-input", record.operationKind, canonical_sha256(input_value)),
        dependency(
            "rail-plan",
            "gate-policy",
            canonical_sha256([rule.model_dump(mode="json") for rule in record.input.gatePolicy]),
        ),
        dependency(
            "validator",
            EVIDENCE_DEPENDENCY_VALIDATOR,
            canonical_sha256(EVIDENCE_DEPENDENCY_VALIDATOR),
        ),
    ]
    if record.operationKind in {"closeout", "direct-landing"}:
        if record.candidateTree is None or not isinstance(record.taskIntent, TaskIntentIdentity):
            raise EvidenceDependencyError(
                "lifecycle-operation-candidate-dependencies-missing",
                "commit operation lacks a code tree or digest-bearing task intent",
            )
        admitted = record.doorPublication
        if admitted is None:
            raise EvidenceDependencyError(
                "lifecycle-operation-door-dependency-missing",
                "commit operation has no admitted closeout-door generation",
            )
        edges.extend(
            (
                dependency(
                    "code-tree",
                    "candidate",
                    record.candidateTree,
                    algorithm="git-object",
                ),
                dependency("task-intent", "leaf", record.taskIntent.digest),
                dependency(
                    "door-generation",
                    "admitted-door",
                    admitted.generation.generationId,
                ),
            )
        )
    return build_evidence_dependencies(record_type, edges)


def require_lifecycle_operation_dependencies(
    record: LifecycleOperationRecord,
) -> EvidenceDependencies:
    """Refuse an operation whose declared edges differ from its admitted immutable inputs."""

    expected = lifecycle_operation_dependencies(record)
    observed = require_evidence_dependencies(
        record.dependencies,
        record_type=expected.recordType,
    )
    if observed != expected:
        raise EvidenceDependencyError(
            "lifecycle-operation-dependencies-stale",
            "lifecycle operation direct dependencies do not match its admitted inputs",
        )
    return observed


def _require_altitude_authority(record: LifecycleOperationRecord) -> None:
    if record.operationKind != record.input.kind:
        raise ValueError("lifecycle operation kind must equal its accepted input kind")
    if record.operationKind != "direct-landing" and record.directLandingLedgerIntent is not None:
        raise ValueError("direct landing ledger intent belongs only to direct landing")
    if record.operationKind not in {"closeout", "direct-landing"} and (
        record.doorPublication is not None or record.doorPublicationHistory
    ):
        raise ValueError("door publication belongs only to schedulable commit operations")
    if record.operationKind == "integrate" and record.integrationAuthority is None:
        raise ValueError("integrate operation requires exact integrationAuthority")
    if record.operationKind != "integrate" and record.integrationAuthority is not None:
        raise ValueError("only integrate operations may carry integrationAuthority")
    _require_task_intent_state(record)
    if record.operationKind != "closeout" and record.closeoutFinalizedContractSha256 is not None:
        raise ValueError("closeout finalized contract SHA-256 belongs to closeout operations only")
    if (
        record.operationKind != "integrate"
        and isinstance(record.result, dict)
        and record.result.get("state") == "organizational-completion-gate-failed"
    ):
        raise ValueError("organizational completion quality failure belongs to integration only")
    _require_organizational_repair_evidence(record)
    _require_integration_publication(record)
    if record.operationKind in {"closeout", "direct-landing"}:
        _require_closeout_mutation_evidence(record)
    elif record.mutationEvidence:
        raise ValueError("integration operation cannot carry closeout mutation evidence")
    _require_worker_authority(record)
    _require_cancellation_evidence(record)
    _require_legacy_migration(record)


def _require_task_intent_state(record: LifecycleOperationRecord) -> None:
    if record.operationKind == "integrate" and record.taskIntent is not None:
        raise ValueError("integrate operations do not carry leaf task intent")
    if record.operationKind in {"closeout", "direct-landing"} and record.taskIntent is None:
        raise ValueError("commit operations require a task-intent state")
    if record.operationKind in {"closeout", "direct-landing"}:
        publications = [
            publication
            for publication in [record.doorPublication, *record.doorPublicationHistory]
            if publication is not None
        ]
        if any(
            publication.generation.taskIntent != record.taskIntent for publication in publications
        ):
            raise ValueError("operation and door publication must bind the same task intent")


def _require_closeout_mutation_evidence(record: LifecycleOperationRecord) -> None:
    closeout_input = _required_commit_operation_input(record.input)
    expected_legs = _expected_commit_legs(closeout_input)
    _require_mutation_leg_sets(record, expected_legs)
    _require_mutation_history(record)
    _require_irreversible_boundary(record)
    _require_recovery_commit_evidence(record)


def _required_commit_operation_input(
    operation_input: LifecycleOperationInput,
) -> CloseoutOperationInput | DirectLandingOperationInput:
    if not isinstance(operation_input, (CloseoutOperationInput, DirectLandingOperationInput)):
        raise ValueError("commit operation requires normalized closeout input")
    return operation_input


def _require_irreversible_boundary(record: LifecycleOperationRecord) -> None:
    irreversible = _commit_proven(record) or record.legacyMigration is not None
    if record.irreversibleBoundaryEntered != irreversible:
        raise ValueError(
            "closeout irreversible boundary must be derived from commit proof or legacy output proof"
        )


def _expected_commit_legs(
    closeout_input: CloseoutOperationInput | DirectLandingOperationInput,
) -> set[str]:
    return {
        leg for leg in ("code", "memory", "ledger") if closeout_input.effectiveInput.enabled(leg)
    }


def _require_mutation_leg_sets(record: LifecycleOperationRecord, expected_legs: set[str]) -> None:
    if set(record.mutationEvidence) != expected_legs:
        raise ValueError("closeout mutation evidence must match every enabled commit leg")
    _require_mutation_history_legs(record, expected_legs)


def _require_mutation_history_legs(
    record: LifecycleOperationRecord,
    expected_legs: set[str],
) -> None:
    if any(leg not in expected_legs for leg in record.mutationHistory):
        raise ValueError("closeout mutation history must match an enabled commit leg")


def _require_mutation_history(record: LifecycleOperationRecord) -> None:
    for leg, attempts in record.mutationHistory.items():
        _require_mutation_attempts(leg, attempts)


def _require_mutation_attempts(
    leg: str,
    attempts: list[GitMutationEvidence],
) -> None:
    for item in attempts:
        if (item.leg, item.state) != (leg, "reconciled-unchanged"):
            raise ValueError(
                "closeout mutation history may preserve only reconciled-unchanged attempts"
            )


def _commit_proven(record: LifecycleOperationRecord) -> bool:
    return any(evidence.state == "commit-proven" for evidence in record.mutationEvidence.values())


def _require_recovery_commit_evidence(record: LifecycleOperationRecord) -> None:
    if record.recoveryCommits is None:
        return
    recovery_field = {
        "code": "codeCommit",
        "memory": "memoryContentCommit",
        "ledger": "ledgerCommit",
    }
    for leg, evidence in record.mutationEvidence.items():
        _require_recovered_leg(record.recoveryCommits, recovery_field[leg], evidence)


def _require_recovered_leg(
    commits: LifecycleOperationRecoveryCommits,
    field: str,
    evidence: GitMutationEvidence,
) -> None:
    if evidence.state != "commit-proven":
        return
    if getattr(commits, field) not in (None, "", evidence.commit):
        raise ValueError("closeout recovery commit contradicts commit-proven evidence")


def _require_legacy_migration(record: LifecycleOperationRecord) -> None:
    proof = record.legacyMigration
    if proof is None:
        return
    closeout_input = _required_legacy_closeout_input(record)
    _require_legacy_generation_identity(record, proof)
    _require_legacy_recovery_commit(record, proof)
    _require_legacy_effective_input(closeout_input, proof)
    _require_active_legacy_generation(record)


def _required_legacy_closeout_input(record: LifecycleOperationRecord) -> CloseoutOperationInput:
    if (record.operationKind, isinstance(record.input, CloseoutOperationInput)) != (
        "closeout",
        True,
    ):
        raise ValueError("legacy migration proof belongs only to a closeout operation")
    assert isinstance(record.input, CloseoutOperationInput)
    return record.input


def _require_active_legacy_generation(record: LifecycleOperationRecord) -> None:
    if (record.status == "cancelled", record.generationDisposition) != (False, "active"):
        raise ValueError("legacy migration proof cannot be cancelled, retired, or superseded")


def _require_legacy_generation_identity(
    record: LifecycleOperationRecord,
    proof: LegacyCloseoutMigrationProof,
) -> None:
    observed = (
        record.operationKey,
        record.fingerprint,
        record.candidateState,
        record.candidateTree,
    )
    expected = (
        proof.legacyOperationKey,
        proof.legacyFingerprint,
        proof.legacyCandidateState,
        proof.legacyCandidateTree,
    )
    if observed != expected:
        raise ValueError("legacy migration proof must retain the legacy generation identity")


def _require_legacy_recovery_commit(
    record: LifecycleOperationRecord,
    proof: LegacyCloseoutMigrationProof,
) -> None:
    commits = record.recoveryCommits
    if getattr(commits, "codeCommit", None) != proof.codeCommit:
        raise ValueError("legacy migration recovery code commit must equal its live proof")


def _require_legacy_effective_input(
    operation_input: CloseoutOperationInput,
    proof: LegacyCloseoutMigrationProof,
) -> None:
    effective = operation_input.effectiveInput
    if (effective.code.state, effective.code.reason) != (
        "not-applicable",
        "verified-existing legacy code output",
    ):
        raise ValueError("legacy migration code leg must be typed verified-existing")
    if not isinstance(effective.memory, EnabledCloseoutLeg) or not isinstance(
        effective.ledger,
        EnabledCloseoutLeg,
    ):
        raise ValueError("legacy migration must keep memory and ledger enabled")
    observed = (
        effective.memory.state,
        effective.ledger.state,
        effective.memory.message,
        effective.ledger.message,
        operation_input.approvalNote,
    )
    expected = (
        "enabled",
        "enabled",
        proof.memoryCommitMessage,
        proof.ledgerCommitMessage,
        proof.legacyApprovalNote,
    )
    if observed != expected:
        raise ValueError("legacy migration must bind both unfinished message cells exactly")


def _require_worker_authority(record: LifecycleOperationRecord) -> None:
    if record.operationKind == "direct-landing":
        _require_no_direct_worker_authority(record)
        return
    binding = (record.workerPid, record.workerLease, record.workerProcessFingerprint)
    _require_complete_worker_binding(binding)
    _require_termination_return_identity(record)
    termination = record.workerTermination
    if termination is None:
        _require_no_termination_return(record)
        return
    _require_live_termination_identity(record, termination)
    _require_exited_worker_release(termination, binding)


def _require_complete_worker_binding(binding: tuple[int | None, str | None, str | None]) -> None:
    present = tuple(value is not None for value in binding)
    if any(present) != all(present):
        raise ValueError("detached worker pid, lease, and process fingerprint are one authority")


def _require_termination_return_identity(record: LifecycleOperationRecord) -> None:
    return_identity = (
        record.terminationReturnStatus is not None,
        record.terminationReturnPhase is not None,
    )
    expected = (True, True) if record.status == "termination-required" else (False, False)
    if return_identity != expected:
        raise ValueError(
            "termination-required status must retain one exact return status and phase identity"
        )


def _require_no_termination_return(record: LifecycleOperationRecord) -> None:
    if record.terminationReturnStatus is not None:
        raise ValueError("termination return status requires durable termination evidence")


def _require_live_termination_identity(
    record: LifecycleOperationRecord,
    termination: WorkerTerminationEvidence,
) -> None:
    if termination.state == "exited":
        return
    if (termination.lease, termination.pid) != (record.workerLease, record.workerPid):
        raise ValueError("unproven worker termination must retain the exact pid and lease")


def _require_exited_worker_release(
    termination: WorkerTerminationEvidence,
    binding: tuple[int | None, str | None, str | None],
) -> None:
    if termination.state != "exited":
        return
    if _binding_present(binding):
        raise ValueError("proven worker exit must release pid and lease authority")


def _binding_present(binding: tuple[int | None, str | None, str | None]) -> bool:
    return any(value is not None for value in binding)


def _require_no_direct_worker_authority(record: LifecycleOperationRecord) -> None:
    if any(
        value is not None
        for value in (
            record.workerPid,
            record.workerLease,
            record.workerProcessFingerprint,
            record.workerTermination,
            record.terminationReturnStatus,
            record.terminationReturnPhase,
        )
    ):
        raise ValueError("synchronous direct landing cannot carry detached worker authority")


def _require_cancellation_evidence(record: LifecycleOperationRecord) -> None:
    evidence = record.cancellationEvidence
    if evidence is None:
        return
    if (
        evidence.operationKind != record.operationKind
        or evidence.generation != record.generation
        or not evidence.workerExitProven
    ):
        raise ValueError("cancellation evidence must bind this generation and proven worker exit")
    if record.status != "cancelled":
        raise ValueError("cancellation evidence belongs only to a cancelled generation")


def _require_organizational_repair_evidence(record: LifecycleOperationRecord) -> None:
    if record.organizationalRepair is None:
        return
    if record.operationKind != "integrate":
        raise ValueError("organizational completion repair evidence belongs to integration")
    if (
        record.integrationPublication is not None
        or record.recoveryCommits is not None
        or record.irreversibleBoundaryEntered
    ):
        raise ValueError(
            "organizational repair is an exact preclaim mode without publication or output"
        )
    if record.status not in {"queued", "running", "input-required", "cancelled"}:
        raise ValueError("organizational repair evidence has an invalid lifecycle status")
    if (
        record.result is None
        or record.result.get("state") != "organizational-completion-gate-failed"
    ):
        raise ValueError("organizational repair evidence requires its exact failure result")
    _require_canonical_cancellation_handoff(
        record.result,
        record.organizationalRepair.contractPath,
        record.generation,
    )


def _require_integration_publication(record: LifecycleOperationRecord) -> None:
    publication = record.integrationPublication
    if publication is None:
        return
    if record.operationKind != "integrate" or record.integrationAuthority is None:
        raise ValueError("integration publication intent belongs only to integration")
    if (
        publication.operationKey != record.operationKey
        or publication.generation != record.generation
        or record.recoveryCommits is None
    ):
        raise ValueError("integration publication intent does not bind this generation")
    authority = record.integrationAuthority
    commits = record.recoveryCommits
    if (
        commits.codeCommit != authority.codeCandidateCommit
        or commits.memoryContentCommit != authority.memoryContentCommit
        or commits.ledgerCommit != authority.ledgerCommit
    ):
        raise ValueError("integration publication ref intent contradicts accepted authority")
    organizational = publication.organizationalCompletion
    if organizational is None:
        return
    certification = record.qualityCertification
    if certification is None or (
        certification.completionFingerprint != organizational.completionFingerprint
        or certification.resultSha256 != organizational.certificationResultSha256
    ):
        raise ValueError("organizational publication lacks its exact quality certification")


def _require_canonical_cancellation_handoff(
    result: dict[str, Any],
    expected_path: str,
    expected_generation: int,
) -> None:
    next_args = result.get("nextArgs")
    apply_step = result.get("applyStep")
    next_args = next_args if isinstance(next_args, dict) else {}
    apply_step = apply_step if isinstance(apply_step, dict) else {}
    apply_args = apply_step.get("nextArgs")
    apply_args = apply_args if isinstance(apply_args, dict) else {}
    canonical = all(
        (
            result.get("developerDecisionRequired") is True,
            result.get("safeToReplace") is False,
            result.get("superRefsMoved") is False,
            result.get("ok") is False,
            result.get("operation") == "worktree_integrate",
            result.get("nextTool") == "worktree_operation_control",
            next_args.get("contract_path") == expected_path,
            next_args.get("operation_kind") == "integrate",
            next_args.get("action") == "cancel",
            next_args.get("expected_generation") == expected_generation,
            next_args.get("dry_run") is True,
            apply_step.get("nextTool") == "worktree_operation_control",
            apply_args.get("contract_path") == expected_path,
            apply_args.get("operation_kind") == "integrate",
            apply_args.get("action") == "cancel",
            apply_args.get("expected_generation") == expected_generation,
            apply_args.get("dry_run") is False,
        )
    )
    if not canonical:
        raise ValueError(
            "organizational repair evidence requires its canonical cancellation handoff"
        )
