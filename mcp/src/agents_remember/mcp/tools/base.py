"""Shared protocol adapter primitives for Agents Remember MCP tools."""

from __future__ import annotations

from typing import Any

from agents_remember.application.tool_response import complete_tool_response

TRANSPORT = "stdio"
PUBLIC_TOOLS = (
    "ping",
    "server_info",
    "context_packet",
    "read_ar_files",
    "resolve_context",
    "runtime_install",
    "skills_install",
    "dispatch_agent",
    "retire_child",
    "rename_child",
    "rename_self",
    "drift_check",
    "memory_quality_check",
    "citation_fix",
    "route_index_refresh",
    "memory_init",
    "memory_baseline_status",
    "memory_baseline_adopt",
    "memory_carryover_plan",
    "memory_carryover_apply",
    "provider_status",
    "provider_diagnostics",
    "provider_watchers",
    "grepai_search",
    "grepai_trace",
    "cgc_symbol_search",
    "cgc_callers",
    "cgc_callees",
    "cgc_dependencies",
    "cgc_complexity",
    "cgc_visualize",
    "worktree_start",
    "worktree_enclosure_adopt",
    "worktree_attach",
    "worktree_status",
    "worktree_status_wait",
    "worktree_sync",
    "direct_landing",
    "worktree_closeout_preview",
    "worktree_closeout_apply",
    "worktree_integrate",
    "worktree_operation_control",
    "worktree_legacy_operation",
    "worktree_cleanup",
    "worktree_abandon",
    "task_reopen",
    "lifecycle_finalize_task",
    "task_doc",
    "curator_coherence",
    "closeout_door",
    "closeout_queue",
    "codex_benchmark_prepare",
    "codex_benchmark_run",
    "lifecycle_start",
    "lifecycle_resume",
    "lifecycle_turn_end_notification",
    "lifecycle_end",
    "switch_lifecycle",
    "lifecycle_phase",
    "lifecycle_gate",
    "gate_decide",
    "gate_list",
    "message_parent",
    "message_child",
)
RESERVED_TOOLS: tuple[str, ...] = ()


def _tool_payload(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Convert one application result into its protocol-ready response."""
    return complete_tool_response(tool_name, payload)
