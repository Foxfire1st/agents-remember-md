"""Models for worktree state included in context packets."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from agents_remember.kernel.coordination_context.models import MemoryMode
from agents_remember.models.base import FlexibleToolResponse, StrictResponseModel
from agents_remember.models.closeout.input import (
    CloseoutCorrectedCall,
    CloseoutInvalidField,
    ResolvedCloseoutPlan,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.lifecycles.operation import LifecycleOperationProjection
from agents_remember.models.lifecycles.operation_kinds import LifecycleOperationKind
from agents_remember.models.lifecycles.operation_wait import LifecycleWaitOutcome
from agents_remember.models.quality import QualityGateResult

# Worktree wire vocabulary (moved from worktrees.worktree_contract / modules.guidance).
WorkflowKind = Literal["chat-task", "light-task"]
HumanReviewStatus = Literal["pending-review", "approved"]
CloseoutStatus = Literal["not-started", "completed"]
LifecycleStatus = CloseoutStatus  # the published wire name for the closeout status
IntegrationStatus = Literal["not-started", "completed", "blocked"]
CleanupStatus = Literal["pending", "completed", "abandoned", "reopened"]
WorktreePhase = Literal[
    "worktree-started",
    "closeout-pending",
    "integration-pending",
    "integration-blocked",
    "carryover-pending",
    "cleanup-pending",
    "cleanup-completed",
    "abandoned",
]
NextOperation = Literal[
    "continue_work",
    "closeout",
    "request_integration_decision",
    "developer_decision",
    "request_carryover_decision",
    "request_cleanup_decision",
    "done",
]
NextTool = Literal[
    "worktree_status",
    "worktree_closeout_apply",
    "worktree_integrate",
    "memory_carryover_plan",
    "worktree_cleanup",
]
SourceLineageState = Literal["current", "blocked", "unavailable"]
SourceLineageEdgeState = Literal["current", "behind", "diverged", "unavailable"]
SourceLineageRelation = Literal["super-to-master", "master-to-leaf", "super-to-leaf"]
SourceLineageSide = Literal["code", "memory"]
SyncResolutionAction = Literal["continue", "cancel"]
MemorySyncChoice = Literal["merge-memory", "skip-memory"]
SyncSide = Literal["code", "memory"]
SyncPhase = Literal[
    "running-code",
    "code-resolution-required",
    "running-memory",
    "memory-resolution-required",
    "finalizing",
    "cancelling",
    "completed",
    "cancelled",
]
SyncOperationState = Literal[
    "running",
    "resolution-required",
    "cancelling",
    "completed",
    "cancelled",
    "journal-malformed",
    "journal-identity-invalid",
    "quarantined",
]


class SourceLineageEdge(StrictResponseModel):
    """One plane-resolved ancestry edge; agents never supply or retain commit ids."""

    relation: SourceLineageRelation
    side: SourceLineageSide
    state: SourceLineageEdgeState
    sourceBranch: str
    descendantBranch: str
    ahead: int | None = None
    behind: int | None = None
    contractPath: str
    syncContractPath: str
    detail: str | None = None


class SourceLineageRecovery(StrictResponseModel):
    """One ordered recovery derived from task identity, not model-carried Git state."""

    tool: Literal["worktree_sync"] = "worktree_sync"
    contractPath: str
    args: dict[str, object]


class SourceLineageProjection(StrictResponseModel):
    """Transitive super -> master -> leaf lineage projected into status and refusals."""

    state: SourceLineageState
    summary: str
    edges: list[SourceLineageEdge]
    recoveries: list[SourceLineageRecovery]


class SyncOperationProjection(StrictResponseModel):
    """Stable read-only view of the enclosure-root sync journal."""

    state: SyncOperationState
    phase: SyncPhase | Literal["journal-read", "quarantined"]
    contractPath: str
    journalContractPath: str | None = None
    identityMismatch: bool = False
    side: SyncSide | None = None
    conflictFiles: tuple[str, ...] = ()
    summary: str
    nextArgs: dict[str, object] | None = None
    cancelArgs: dict[str, object] | None = None
    evidencePath: str | None = None


class SyncResolutionProjection(StrictResponseModel):
    side: SyncSide
    owner: Literal["agent"] = "agent"
    worktree: str | None = None
    files: list[str] = Field(default_factory=list)


# Every vocabulary below is imported from whoever produces it, never retyped here. Retyped
# is what these were, and the copies had drifted apart in six places at once: `chat-task`
# (the kind `worktree_start`'s own docstring advertises, on 8 contracts), `reopened`,
# `carryover-pending`, `abandoned`, `request_carryover_decision` and `memory_carryover_apply`
# were all writable and none validated, which made this model reject 165 of the 213 series
# contracts on disk with a ValidationError no handler on the tool path catches.

# Produced entirely inside `application.worktree_status`, which constructs this model
# directly, so the projection there is already the single writer the checker can see.
WorktreeState = Literal["inactive", "active", "missingContract", "invalidContract"]


class WorktreeSummary(StrictResponseModel):
    state: WorktreeState
    contractPath: str | None = None
    enclosurePath: str | None = None
    taskId: str | None = None
    taskName: str | None = None
    leafId: str | None = None
    kind: str | None = None
    workflowKind: WorkflowKind | None = None
    memoryMode: MemoryMode | None = None
    worktreeGroup: str | None = None
    codeWorktree: str | None = None
    codeWorktreeExists: bool | None = None
    codeWorktreeDirty: bool | None = None
    memoryWorktree: str | None = None
    memoryWorktreeExists: bool | None = None
    memoryWorktreeDirty: bool | None = None
    ledgerPath: str | None = None
    humanReviewStatus: HumanReviewStatus | None = None
    approvedForCommit: bool | None = None
    closeoutStatus: LifecycleStatus | None = None
    integrationStatus: IntegrationStatus | None = None
    cleanup: CleanupStatus | None = None
    phase: WorktreePhase | None = None
    nextOperation: NextOperation | None = None
    nextTool: NextTool | None = None
    nextArgs: dict[str, Any] | None = None
    # Absent means the next call needs nothing beyond `nextArgs` -- the same thing the empty
    # list used to mean. `next_guidance` writes this key only when there is a required
    # argument, and the projection reports what the producer said rather than filling in a
    # value for it (`application.worktree_status._summary_from_status_payload` states the
    # measurement).
    nextRequiredArgs: list[str] | None = None
    # Present only when the contract file carried a cell outside its declared vocabulary, as
    # "<field>=<raw token> read as <fallback>". The `state` is still `active` and every other
    # field on this summary was computed from the substituted values -- this is the notice
    # that they were substituted, and the file heals the next time a lifecycle tool writes it.
    unknownContractCells: list[str] | None = None
    error: str | None = None
    errorEvidence: dict[str, object] | None = None
    status: str | None = None
    summary: str | None = Field(default=None, max_length=8192)
    detail: str | None = Field(default=None, max_length=8192)
    expected: dict[str, object] | None = None
    observed: dict[str, object] | None = None
    nextAction: Literal["developer-decision"] | None = None
    developerDecisionRequired: bool | None = None
    decisionSurface: str | None = Field(default=None, max_length=8192)
    lifecycleOperation: LifecycleOperationProjection | None = None
    sourceLineage: SourceLineageProjection | None = None
    syncOperation: SyncOperationProjection | None = None


class WorktreeCommandResponse(FlexibleToolResponse):
    repoId: str | None = None
    state: str | None = None
    dryRun: bool | None = None
    contractPath: str | None = None
    enclosurePath: str | None = None
    taskId: str | None = None
    taskName: str | None = None
    leafId: str | None = None
    kind: str | None = None
    worktreeName: str | None = None
    # The lifecycle this enclosure anchors (design §1.1): worktree_start promotes
    # it, worktree_attach resumes it. Emitted snake_case (lifecycle_id) like its
    # siblings; declared here for wire discoverability.
    lifecycleId: str | None = None
    # Background provider setup state (GitHub #53): worktree_start returns
    # 'starting' with a progressFile; worktree_status then projects the live
    # progress as running / stale (dead heartbeat) / ok /
    # ready-with-failed-phases / failed, with currentPhase and seedFallback.
    providers: dict[str, Any] | None = None
    source_lineage: SourceLineageProjection | None = None
    status: str | None = None
    detail: str | None = Field(default=None, max_length=8192)
    invalidFields: list[CloseoutInvalidField] | None = None
    resolvedPlan: ResolvedCloseoutPlan | None = None
    correctedCall: CloseoutCorrectedCall | None = None
    code_quality_gate: QualityGateResult | None = None
    quality_gate: QualityGateResult | None = None


class WorktreeStartResponse(WorktreeCommandResponse):
    operation: Literal["worktree_start"] = "worktree_start"


class WorktreeAttachResponse(WorktreeCommandResponse):
    operation: Literal["worktree_attach"] = "worktree_attach"


class WorktreeStatusResponse(WorktreeCommandResponse):
    operation: Literal["worktree_status"] = "worktree_status"
    lifecycleOperations: list[LifecycleOperationProjection] = Field(default_factory=list)
    syncOperation: SyncOperationProjection | None = None


class WorktreeEnclosureAdoptResponse(WorktreeCommandResponse):
    operation: Literal["worktree_enclosure_adopt"] = "worktree_enclosure_adopt"
    publicationRequestId: str | None = None
    locatorPath: str | None = None
    manifestPath: str | None = None
    contractSha256: str | None = None
    manifestSha256: str | None = None
    artifacts: list[dict[str, object]] = Field(default_factory=list)
    removalCondition: str | None = None


class WorktreeStatusWaitResponse(WorktreeCommandResponse):
    """Read-only bounded wait on lifecycle meaningful-state changes (CCR-R15).

    Addressed by canonical contract, operation kind, expected public generation,
    and an opaque typed after_revision cursor from a prior snapshot.  On
    change it returns the compact R18-coherent status plus the next cursor; on
    timeout it returns the unchanged snapshot and cursor without claiming
    failure.  Never carries an operation key, PID, or worker/queue/gate
    authority.
    """

    operation: Literal["worktree_status_wait"] = "worktree_status_wait"
    outcome: LifecycleWaitOutcome
    operationKind: LifecycleOperationKind | None = None
    successorGeneration: int | None = None
    meaningfulRevision: int | None = None
    timeoutSeconds: float | None = None
    elapsedSeconds: float | None = None
    lifecycleOperation: LifecycleOperationProjection | None = None
    nextArgs: dict[str, object] | None = None


class WorktreeSyncResponse(WorktreeCommandResponse):
    operation: Literal["worktree_sync"] = "worktree_sync"
    phase: SyncPhase | Literal["quarantined"] | None = None
    resolution: SyncResolutionProjection | None = None
    resolutionOwner: Literal["agent"] | None = None
    nextOperation: str | None = None
    nextTool: Literal["worktree_sync"] | None = None
    nextArgs: dict[str, object] | None = None
    cancelArgs: dict[str, object] | None = None
    evidencePath: str | None = None
    invalidField: Literal["memory_sync_choice", "resolution_action"] | None = None
    manualRepair: dict[str, object] | None = None


class _WorktreeCloseoutResponse(WorktreeCommandResponse):
    pairIdentity: MemoryCandidatePairIdentity | None = None
    pairStatus: str | None = Field(default=None, max_length=256)
    pairField: str | None = Field(default=None, max_length=256)
    expected: dict[str, Any] | None = Field(default=None, max_length=32)
    observed: dict[str, Any] | None = Field(default=None, max_length=32)
    nextAction: str | None = Field(default=None, max_length=8192)
    nextArgs: dict[str, Any] | None = Field(default=None, max_length=32)


class WorktreeCloseoutPreviewResponse(_WorktreeCloseoutResponse):
    operation: Literal["worktree_closeout_preview"] = "worktree_closeout_preview"


class WorktreeCloseoutApplyResponse(_WorktreeCloseoutResponse):
    operation: Literal["worktree_closeout_apply"] = "worktree_closeout_apply"
    lifecycleOperation: LifecycleOperationProjection | None = None


class WorktreeIntegrateResponse(WorktreeCommandResponse):
    operation: Literal["worktree_integrate"] = "worktree_integrate"
    lifecycleOperation: LifecycleOperationProjection | None = None
    # Declared even though the worktree envelope is intentionally flexible: these are stable
    # completion-cleanup products, not incidental worktree-module details.
    autoClosedSeats: list[str] = Field(default_factory=list)
    autoCloseDeferredSeats: list[str] = Field(default_factory=list)
    autoCloseFailedSeats: list[str] = Field(default_factory=list)
    autoLandedSeats: list[str] = Field(default_factory=list)


class WorktreeOperationControlResponse(WorktreeCommandResponse):
    operation: Literal["worktree_operation_control"] = "worktree_operation_control"
    lifecycleOperation: LifecycleOperationProjection | None = None
    expected: dict[str, object] = Field(default_factory=dict)
    observed: dict[str, object] = Field(default_factory=dict)
    nextAction: str = ""
    nextTool: (
        Literal["worktree_operation_control", "worktree_integrate", "direct_landing"] | None
    ) = None
    nextArgs: dict[str, object] | None = None
    developerDecisionRequired: bool = False
    decisionSurface: str | None = None


class WorktreeLegacyOperationResponse(WorktreeCommandResponse):
    operation: Literal["worktree_legacy_operation"] = "worktree_legacy_operation"
    lifecycleOperation: LifecycleOperationProjection | None = None
    operationKind: str | None = None
    legacyDigest: str | None = None
    migratable: bool | None = None
    migrationReason: str | None = None
    archivable: bool | None = None
    archiveReason: str | None = None
    archivePath: str | None = None
    terminalEvidence: dict[str, object] | None = None
    removalCondition: str | None = None
    removalGuard: dict[str, object] | None = None
    expected: dict[str, object] = Field(default_factory=dict)
    observed: dict[str, object] = Field(default_factory=dict)
    nextAction: str = ""
    nextTool: str | None = None
    nextArgs: dict[str, object] | None = None
    developerDecisionRequired: bool = False
    decisionSurface: str | None = None


class WorktreeCleanupResponse(WorktreeCommandResponse):
    operation: Literal["worktree_cleanup"] = "worktree_cleanup"


class WorktreeAbandonResponse(WorktreeCommandResponse):
    operation: Literal["worktree_abandon"] = "worktree_abandon"
