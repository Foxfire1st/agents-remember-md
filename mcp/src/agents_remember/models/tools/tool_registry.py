"""Registry that maps MCP tool payload builders to declared response models.

Response-model convention (one place, deliberately two tiers):

- Tools whose response shape is fully owned by this package use a STRICT model
  (``StrictResponseModel`` / ``ResponseModel`` / ``ToolResponse``, ``extra="forbid"``)
  so the field set is a real, drift-proof contract. ``context_packet`` (the
  fully-typed ``ContextPacketV2``), ``ping``, and ``server_info`` are the
  exemplars; new AR-owned responses should follow them.
- Tools that surface provider-native / raw diagnostic detail (CodeGraphContext,
  GrepAI, Docker, watcher output) use a FLEXIBLE model
  (``FlexibleResponseModel`` / ``FlexibleToolResponse``, ``extra="allow"``) ON
  PURPOSE: that payload is owned by the upstream provider and must not be
  rejected when the provider adds a field. This is a tolerated-drift surface,
  not an un-validated one -- the envelope (ok/operation/tokens) is still typed.

Application entry points return plain dicts; ``_tool_payload`` validates them against the
model registered here and finalizes token counts. Pick STRICT unless the
payload genuinely embeds provider-native detail.
"""

from __future__ import annotations

from agents_remember.models.base import ResponseEnvelope
from agents_remember.models.benchmarks import (
    CodexBenchmarkPrepareResponse,
    CodexBenchmarkRunResponse,
)
from agents_remember.models.context_packet import ContextPacketV2
from agents_remember.models.core import PingResponse, ServerInfoResponse
from agents_remember.models.direct_landing import DirectLandingResponse
from agents_remember.models.lifecycles.curator_coherence import CuratorCoherenceResponse
from agents_remember.models.lifecycles.door_response import CloseoutDoorResponse
from agents_remember.models.lifecycles.finalize import LifecycleFinalizeTaskResponse
from agents_remember.models.lifecycles.responses import (
    LifecycleBlockResponse,
    LifecycleEndResponse,
    LifecyclePhaseResponse,
    LifecycleResumeResponse,
    LifecycleStartResponse,
    LifecycleTurnEndNotificationResponse,
    SwitchLifecycleResponse,
)
from agents_remember.models.memory import (
    CitationFixResponse,
    DriftCheckResponse,
    MemoryBaselineAdoptResponse,
    MemoryBaselineStatusResponse,
    MemoryCarryoverApplyResponse,
    MemoryCarryoverPlanResponse,
    MemoryInitResponse,
    MemoryQualityCheckResponse,
    RouteIndexRefreshResponse,
)
from agents_remember.models.operator_inbox import (
    OperatorInboxConsumeResponse,
    OperatorInboxPollResponse,
    OperatorInboxPostResponse,
    OperatorInboxSupersedeResponse,
)
from agents_remember.models.orchestration import OrchestrationNudgeManagerResponse
from agents_remember.models.providers import (
    CGCCalleesResponse,
    CGCCallersResponse,
    CGCComplexityResponse,
    CGCDependenciesResponse,
    CGCSymbolSearchResponse,
    CGCVisualizeResponse,
    GrepAISearchResponse,
    GrepAITraceResponse,
    ProviderDiagnosticsResponse,
    ProviderStatusResponse,
    ProviderWatchersResponse,
)
from agents_remember.models.queue.closeout_queue import CloseoutQueueResponse
from agents_remember.models.read_files import ReadArFilesResponse
from agents_remember.models.runtime import ResolveContextResponse, RuntimeInstallResponse
from agents_remember.models.skills import SkillsInstallResponse
from agents_remember.models.structural.agent import (
    DispatchAgentResponse,
    MessageChildResponse,
    MessageParentResponse,
    RenameChildResponse,
    RenameSelfResponse,
    RetireChildResponse,
)
from agents_remember.models.structural.gates import (
    GateCreateResponse,
    GateDecideResponse,
    GateListResponse,
    GateResponseWaitResponse,
    GateWaitResponse,
    InternalGateDecideResponse,
    InternalGateListResponse,
    InternalLifecycleGateResponse,
    LifecycleGateResponse,
)
from agents_remember.models.task_doc import TaskDocResponse, TaskReopenResponse
from agents_remember.models.terminal import (
    AttachTerminalSessionToTaskResponse,
    HostedSessionReadinessResponse,
    SessionRenameResponse,
    SessionRetireResponse,
    SpawnAgentSessionResponse,
)
from agents_remember.models.worktree import (
    WorktreeAbandonResponse,
    WorktreeAttachResponse,
    WorktreeCleanupResponse,
    WorktreeCloseoutApplyResponse,
    WorktreeCloseoutPreviewResponse,
    WorktreeEnclosureAdoptResponse,
    WorktreeIntegrateResponse,
    WorktreeLegacyOperationResponse,
    WorktreeOperationControlResponse,
    WorktreeStartResponse,
    WorktreeStatusResponse,
    WorktreeStatusWaitResponse,
    WorktreeSyncResponse,
)

INTERNAL_COMPAT_TOOL_NAMES = frozenset(
    {
        "lifecycle_block",
        "lifecycle_gate_internal",
        "gate_decide_internal",
        "gate_list_internal",
        "gate_create",
        "gate_wait",
        "gate_response_wait",
        # Trusted exact-id/admin payload builders retained below the public MCP boundary.
        "attach_terminal_session_to_task",
        "spawn_agent_session",
        "hosted_session_readiness",
        "session_retire",
        "session_rename",
        "operator_inbox_post",
        "operator_inbox_poll",
        "operator_inbox_consume",
        "operator_inbox_supersede",
        "orchestration_nudge_manager",
    }
)
"""Lower-level compatibility payload builders, not advertised MCP tools."""

# ``type[ResponseEnvelope]``, not ``type[BaseModel]``: every registered model is a
# ``ResponseModel`` or a ``FlexibleResponseEnvelope``, and saying so is what lets the choke
# point set ``nextStep``/``agentNotifierBanner`` on the validated response before the dump.
# ``BaseModel`` here made those two fields unreachable by type, which is how they ended up
# being written into the already-dumped dict instead.
TOOL_RESPONSE_MODELS: dict[str, type[ResponseEnvelope]] = {
    "ping": PingResponse,
    "server_info": ServerInfoResponse,
    "context_packet": ContextPacketV2,
    "read_ar_files": ReadArFilesResponse,
    "attach_terminal_session_to_task": AttachTerminalSessionToTaskResponse,
    "spawn_agent_session": SpawnAgentSessionResponse,
    "hosted_session_readiness": HostedSessionReadinessResponse,
    "session_retire": SessionRetireResponse,
    "session_rename": SessionRenameResponse,
    "dispatch_agent": DispatchAgentResponse,
    "retire_child": RetireChildResponse,
    "rename_child": RenameChildResponse,
    "rename_self": RenameSelfResponse,
    "runtime_install": RuntimeInstallResponse,
    "resolve_context": ResolveContextResponse,
    "drift_check": DriftCheckResponse,
    "memory_quality_check": MemoryQualityCheckResponse,
    "citation_fix": CitationFixResponse,
    "route_index_refresh": RouteIndexRefreshResponse,
    "memory_init": MemoryInitResponse,
    "skills_install": SkillsInstallResponse,
    "provider_status": ProviderStatusResponse,
    "provider_diagnostics": ProviderDiagnosticsResponse,
    "grepai_search": GrepAISearchResponse,
    "grepai_trace": GrepAITraceResponse,
    "cgc_symbol_search": CGCSymbolSearchResponse,
    "cgc_callers": CGCCallersResponse,
    "cgc_callees": CGCCalleesResponse,
    "cgc_dependencies": CGCDependenciesResponse,
    "cgc_complexity": CGCComplexityResponse,
    "provider_watchers": ProviderWatchersResponse,
    "cgc_visualize": CGCVisualizeResponse,
    "worktree_start": WorktreeStartResponse,
    "worktree_attach": WorktreeAttachResponse,
    "worktree_status": WorktreeStatusResponse,
    "worktree_status_wait": WorktreeStatusWaitResponse,
    "worktree_enclosure_adopt": WorktreeEnclosureAdoptResponse,
    "worktree_sync": WorktreeSyncResponse,
    "worktree_closeout_preview": WorktreeCloseoutPreviewResponse,
    "worktree_closeout_apply": WorktreeCloseoutApplyResponse,
    "worktree_integrate": WorktreeIntegrateResponse,
    "worktree_operation_control": WorktreeOperationControlResponse,
    "worktree_legacy_operation": WorktreeLegacyOperationResponse,
    "worktree_cleanup": WorktreeCleanupResponse,
    "worktree_abandon": WorktreeAbandonResponse,
    "task_reopen": TaskReopenResponse,
    "memory_baseline_status": MemoryBaselineStatusResponse,
    "memory_baseline_adopt": MemoryBaselineAdoptResponse,
    "memory_carryover_plan": MemoryCarryoverPlanResponse,
    "memory_carryover_apply": MemoryCarryoverApplyResponse,
    "codex_benchmark_prepare": CodexBenchmarkPrepareResponse,
    "codex_benchmark_run": CodexBenchmarkRunResponse,
    "lifecycle_start": LifecycleStartResponse,
    "lifecycle_block": LifecycleBlockResponse,
    "lifecycle_resume": LifecycleResumeResponse,
    "lifecycle_turn_end_notification": LifecycleTurnEndNotificationResponse,
    "lifecycle_end": LifecycleEndResponse,
    "switch_lifecycle": SwitchLifecycleResponse,
    "lifecycle_phase": LifecyclePhaseResponse,
    "lifecycle_finalize_task": LifecycleFinalizeTaskResponse,
    "task_doc": TaskDocResponse,
    "curator_coherence": CuratorCoherenceResponse,
    "closeout_door": CloseoutDoorResponse,
    "closeout_queue": CloseoutQueueResponse,
    "direct_landing": DirectLandingResponse,
    "lifecycle_gate": LifecycleGateResponse,
    "lifecycle_gate_internal": InternalLifecycleGateResponse,
    "gate_create": GateCreateResponse,
    "gate_decide": GateDecideResponse,
    "gate_decide_internal": InternalGateDecideResponse,
    "gate_wait": GateWaitResponse,
    "gate_response_wait": GateResponseWaitResponse,
    "gate_list": GateListResponse,
    "gate_list_internal": InternalGateListResponse,
    "operator_inbox_post": OperatorInboxPostResponse,
    "operator_inbox_poll": OperatorInboxPollResponse,
    "operator_inbox_consume": OperatorInboxConsumeResponse,
    "operator_inbox_supersede": OperatorInboxSupersedeResponse,
    "orchestration_nudge_manager": OrchestrationNudgeManagerResponse,
    "message_parent": MessageParentResponse,
    "message_child": MessageChildResponse,
}

PUBLIC_TOOL_RESPONSE_MODELS: dict[str, type[ResponseEnvelope]] = {
    name: model
    for name, model in TOOL_RESPONSE_MODELS.items()
    if name not in INTERNAL_COMPAT_TOOL_NAMES
}
