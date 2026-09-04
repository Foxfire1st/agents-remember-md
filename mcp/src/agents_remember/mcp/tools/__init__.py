"""Pure payload builders for Agents Remember MCP tools.

This package is the stable import surface for tool payload builders. The
builders are grouped by domain into submodules; import them from
``agents_remember.mcp.tools`` regardless of which submodule owns them.
"""

from __future__ import annotations

from .base import PUBLIC_TOOLS, RESERVED_TOOLS, TRANSPORT
from .base import _tool_payload as _tool_payload  # re-exported for tool-response tests
from .benchmark import codex_benchmark_prepare_payload, codex_benchmark_run_payload
from .closeout_door import closeout_door_payload
from .closeout_queue import closeout_queue_payload
from .core import (
    context_packet_payload,
    ping_payload,
    resolve_context_payload,
    runtime_install_payload,
    server_info_payload,
    skills_install_payload,
)
from .curator_coherence import curator_coherence_payload
from .direct_landing import direct_landing_payload
from .gates import (
    gate_create_payload,
    gate_decide_payload,
    gate_list_payload,
    gate_response_wait_payload,
    gate_wait_payload,
    lifecycle_gate_payload,
    structural_gate_decide_payload,
    structural_gate_list_payload,
    structural_lifecycle_gate_payload,
)
from .hosted_readiness import hosted_session_readiness_payload
from .lifecycle import (
    lifecycle_block_payload,
    lifecycle_end_payload,
    lifecycle_phase_payload,
    lifecycle_resume_payload,
    lifecycle_start_payload,
    lifecycle_turn_end_notification_payload,
    switch_lifecycle_payload,
)
from .lifecycle_finalize import lifecycle_finalize_task_payload
from .memory import (
    citation_fix_payload,
    drift_check_payload,
    memory_baseline_adopt_payload,
    memory_baseline_status_payload,
    memory_carryover_apply_payload,
    memory_carryover_plan_payload,
    memory_init_payload,
    memory_quality_check_payload,
    memory_quality_check_poll_payload,
    memory_quality_check_start_payload,
    route_index_refresh_payload,
)
from .operator_inbox import (
    operator_inbox_consume_payload,
    operator_inbox_poll_payload,
    operator_inbox_post_payload,
    operator_inbox_supersede_payload,
)
from .orchestration import orchestration_nudge_manager_payload
from .providers import (
    cgc_callees_payload,
    cgc_callers_payload,
    cgc_complexity_payload,
    cgc_dependencies_payload,
    cgc_symbol_search_payload,
    cgc_visualize_payload,
    grepai_search_payload,
    grepai_trace_payload,
    provider_diagnostics_payload,
    provider_status_payload,
    provider_watchers_payload,
)
from .read_files import read_ar_files_payload
from .structural_agent import (
    dispatch_agent_payload,
    message_child_payload,
    message_parent_payload,
    rename_child_payload,
    rename_self_payload,
    retire_child_payload,
)
from .task_doc import task_doc_payload, task_reopen_payload
from .terminal import (
    attach_terminal_session_to_task_payload,
    session_rename_payload,
    session_retire_payload,
    spawn_agent_session_payload,
)
from .worktree import (
    worktree_abandon_payload,
    worktree_attach_payload,
    worktree_cleanup_payload,
    worktree_closeout_apply_payload,
    worktree_closeout_preview_payload,
    worktree_enclosure_adopt_payload,
    worktree_integrate_payload,
    worktree_legacy_operation_payload,
    worktree_operation_control_payload,
    worktree_start_payload,
    worktree_status_payload,
    worktree_status_wait_payload,
    worktree_sync_payload,
)

# The split gate/block/wait payload builders are retained for internal
# compatibility and tests. ``create_server`` advertises ``lifecycle_gate`` as the
# public agent-facing gate junction.

__all__ = [
    "PUBLIC_TOOLS",
    "RESERVED_TOOLS",
    "TRANSPORT",
    "attach_terminal_session_to_task_payload",
    "cgc_callees_payload",
    "cgc_callers_payload",
    "cgc_complexity_payload",
    "cgc_dependencies_payload",
    "cgc_symbol_search_payload",
    "cgc_visualize_payload",
    "citation_fix_payload",
    "closeout_door_payload",
    "closeout_queue_payload",
    "codex_benchmark_prepare_payload",
    "codex_benchmark_run_payload",
    "context_packet_payload",
    "curator_coherence_payload",
    "direct_landing_payload",
    "dispatch_agent_payload",
    "drift_check_payload",
    "gate_create_payload",
    "gate_decide_payload",
    "gate_list_payload",
    "gate_response_wait_payload",
    "gate_wait_payload",
    "grepai_search_payload",
    "grepai_trace_payload",
    "hosted_session_readiness_payload",
    "lifecycle_block_payload",
    "lifecycle_end_payload",
    "lifecycle_finalize_task_payload",
    "lifecycle_gate_payload",
    "lifecycle_phase_payload",
    "lifecycle_resume_payload",
    "lifecycle_start_payload",
    "lifecycle_turn_end_notification_payload",
    "memory_baseline_adopt_payload",
    "memory_baseline_status_payload",
    "memory_carryover_apply_payload",
    "memory_carryover_plan_payload",
    "memory_init_payload",
    "memory_quality_check_payload",
    "memory_quality_check_poll_payload",
    "memory_quality_check_start_payload",
    "message_child_payload",
    "message_parent_payload",
    "operator_inbox_consume_payload",
    "operator_inbox_poll_payload",
    "operator_inbox_post_payload",
    "operator_inbox_supersede_payload",
    "orchestration_nudge_manager_payload",
    "ping_payload",
    "provider_diagnostics_payload",
    "provider_status_payload",
    "provider_watchers_payload",
    "read_ar_files_payload",
    "rename_child_payload",
    "rename_self_payload",
    "resolve_context_payload",
    "retire_child_payload",
    "route_index_refresh_payload",
    "runtime_install_payload",
    "server_info_payload",
    "session_rename_payload",
    "session_retire_payload",
    "skills_install_payload",
    "spawn_agent_session_payload",
    "structural_gate_decide_payload",
    "structural_gate_list_payload",
    "structural_lifecycle_gate_payload",
    "switch_lifecycle_payload",
    "task_doc_payload",
    "task_reopen_payload",
    "worktree_abandon_payload",
    "worktree_attach_payload",
    "worktree_cleanup_payload",
    "worktree_closeout_apply_payload",
    "worktree_closeout_preview_payload",
    "worktree_enclosure_adopt_payload",
    "worktree_integrate_payload",
    "worktree_legacy_operation_payload",
    "worktree_operation_control_payload",
    "worktree_start_payload",
    "worktree_status_payload",
    "worktree_status_wait_payload",
    "worktree_sync_payload",
]
