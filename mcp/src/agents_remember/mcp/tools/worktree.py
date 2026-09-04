"""Worktree lifecycle payload builders."""

from __future__ import annotations

from typing import Any

from agents_remember.application.lifecycle.legacy_operation_tool import (
    LegacyOperationRequest,
    worktree_legacy_operation_tool,
)
from agents_remember.application.lifecycle.lifecycle_enclosure_tools import (
    EnclosureAdoptionRequest,
    worktree_enclosure_adopt_tool,
)
from agents_remember.application.lifecycle.lifecycle_status_wait import (
    LifecycleStatusWaitRequest,
    worktree_status_wait_tool,
)
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.worktree_tools import (
    DEFAULT_START_EXECUTION,
    DEFAULT_TASK_BASES,
    CloseoutApproval,
    CloseoutCommitMessages,
    OperationControlRequest,
    StartExecution,
    TaskBases,
    TaskIdentity,
    summarized_worktree_start_tool,
    worktree_abandon_tool,
    worktree_attach_tool,
    worktree_cleanup_tool,
    worktree_closeout_apply_tool,
    worktree_closeout_preview_tool,
    worktree_integrate_tool,
    worktree_operation_control_tool,
    worktree_status_tool,
    worktree_sync_tool,
)
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.operation import (
    IntegrateStrategy,
)
from agents_remember.models.worktree import MemorySyncChoice, SyncResolutionAction

from .base import _tool_payload


def worktree_start_payload(
    config: McpRuntimeConfig,
    identity: TaskIdentity,
    *,
    bases: TaskBases = DEFAULT_TASK_BASES,
    execution: StartExecution = DEFAULT_START_EXECUTION,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_start",
        summarized_worktree_start_tool(config, identity, bases=bases, execution=execution),
    )


def worktree_sync_payload(
    config: McpRuntimeConfig,
    contract_path: str,
    *,
    memory_sync_choice: MemorySyncChoice | None = None,
    resolution_action: SyncResolutionAction | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_sync",
        worktree_sync_tool(
            config,
            contract_path=contract_path,
            memory_sync_choice=memory_sync_choice,
            resolution_action=resolution_action,
            dry_run=dry_run,
        ),
    )


def worktree_attach_payload(
    config: McpRuntimeConfig,
    task: TaskRef,
    *,
    on_unsaved: str | None = None,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_attach",
        worktree_attach_tool(config, task, on_unsaved=on_unsaved),
    )


def worktree_status_payload(
    config: McpRuntimeConfig,
    task: TaskRef,
    *,
    caller: DeclaredCaller | None = None,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_status",
        worktree_status_tool(config, task, caller=caller),
    )


def worktree_status_wait_payload(
    config: McpRuntimeConfig,
    request: LifecycleStatusWaitRequest,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_status_wait",
        worktree_status_wait_tool(config, request),
    )


def worktree_enclosure_adopt_payload(
    config: McpRuntimeConfig,
    request: EnclosureAdoptionRequest,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_enclosure_adopt",
        worktree_enclosure_adopt_tool(config, request),
    )


def worktree_closeout_preview_payload(
    config: McpRuntimeConfig,
    contract_path: str,
    messages: CloseoutCommitMessages,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_closeout_preview",
        worktree_closeout_preview_tool(config, contract_path, messages),
    )


def worktree_closeout_apply_payload(
    config: McpRuntimeConfig,
    contract_path: str,
    messages: CloseoutCommitMessages,
    approval: CloseoutApproval,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_closeout_apply",
        worktree_closeout_apply_tool(config, contract_path, messages, approval),
    )


def worktree_integrate_payload(
    config: McpRuntimeConfig,
    contract_path: str,
    *,
    strategy: IntegrateStrategy = "ff-only",
    ledger_commit_message: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_integrate",
        worktree_integrate_tool(
            config,
            contract_path=contract_path,
            strategy=strategy,
            ledger_commit_message=ledger_commit_message,
            dry_run=dry_run,
        ),
    )


def worktree_operation_control_payload(
    config: McpRuntimeConfig,
    request: OperationControlRequest,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_operation_control",
        worktree_operation_control_tool(config, request),
    )


def worktree_legacy_operation_payload(
    config: McpRuntimeConfig,
    contract_path: str,
    request: LegacyOperationRequest,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_legacy_operation",
        worktree_legacy_operation_tool(config, contract_path, request),
    )


def worktree_cleanup_payload(
    config: McpRuntimeConfig,
    contract_path: str,
    *,
    dry_run: bool = False,
    teardown_providers: bool = True,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_cleanup",
        worktree_cleanup_tool(
            config,
            contract_path=contract_path,
            dry_run=dry_run,
            teardown_providers=teardown_providers,
        ),
    )


def worktree_abandon_payload(
    config: McpRuntimeConfig,
    contract_path: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    return _tool_payload(
        "worktree_abandon",
        worktree_abandon_tool(
            config,
            contract_path=contract_path,
            dry_run=dry_run,
            force=force,
        ),
    )
