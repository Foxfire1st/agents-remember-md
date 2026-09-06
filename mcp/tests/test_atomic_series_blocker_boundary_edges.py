"""Focused public-boundary branches for atomic selection and resumable sync."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

import pytest
from agents_remember.application import worktree_status as status_application
from agents_remember.application import worktree_tools as worktree_application
from agents_remember.application.lifecycle.configured_contract_admission import (
    ConfiguredContractRefused,
)
from agents_remember.application.lifecycle.lifecycle_control_authority import (
    LifecycleCallerError,
)
from agents_remember.application.structural import agent_tools
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.models.lifecycles.enclosure import (
    TerminalWorktreeAbandonArguments,
    TerminalWorktreeCleanupArguments,
)
from agents_remember.models.worktree import WorktreeSummary
from agents_remember.tasks.document_refs import (
    ResolvedTaskDocument,
    TaskDocumentRef,
    TaskDocumentTopology,
)
from agents_remember.worktrees.integration import integration_branch_authority
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location_errors import (
    LifecycleOperationLocationError,
)
from agents_remember.worktrees.modules import abandon as abandon_module
from agents_remember.worktrees.modules import cleanup as cleanup_module
from agents_remember.worktrees.modules import finalize as finalize_module
from agents_remember.worktrees.modules import start as start_module
from agents_remember.worktrees.modules import sync as sync_module
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.modules.startup import start_contract
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    WorktreeContract,
    default_contract,
    default_series_contract,
)

SHA = "0" * 40
OTHER_SHA = "1" * 40


def _contract(root: Path, *, series: bool = False) -> WorktreeContract:
    task = ContractTask(
        name="Master" if series else "Leaf",
        repo_name="repo",
        coordination_root=root / "coordination",
        workflow_kind="light-task",
        memory_mode="disabled",
        parent_task_name="sprint",
    )
    plan = RepoBranchPlan(
        repo_path=root / "code",
        source_branch="super",
        work_branch="ar/master" if series else "ar/leaf",
        base_commit=SHA,
    )
    if series:
        return default_series_contract(
            task,
            code=plan,
            task_root=root / "coordination" / "tasks" / "repo" / "master",
        )
    return default_contract(
        task,
        leaf=LeafIdentity(worktree_name="leaf", leaf_id="L1"),
        code=plan,
    )


def _config() -> McpRuntimeConfig:
    return cast(McpRuntimeConfig, mock.Mock(spec=McpRuntimeConfig))


def test_dispatch_requires_a_leaf_owner_and_derives_standalone_source(tmp_path: Path) -> None:
    topology = mock.Mock(spec=TaskDocumentTopology)
    topology.altitude.return_value = "leaf"
    topology.parent.return_value = None
    resolved = cast(
        ResolvedTaskDocument,
        SimpleNamespace(
            ref=TaskDocumentRef(repository="repo", path="master/L1/task.json"),
        ),
    )
    with pytest.raises(ValueError, match="no canonical owning master"):
        agent_tools._dispatch_owning_master(topology, resolved, "worker")

    with mock.patch.object(agent_tools, "repository_default_branch", return_value="main"):
        assert agent_tools._series_source_spec(None, tmp_path) == ("", "main")


def test_status_packet_preserves_locator_mismatch_with_sync_observation(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    location = SimpleNamespace(worktree_group=tmp_path)
    error = LifecycleOperationLocationError(
        "operation-location-invalid",
        "contract moved",
        expected={"contractPath": contract.contract_path.as_posix()},
        observed={"state": "mismatch"},
    )
    expected = WorktreeSummary(state="inactive")
    with (
        mock.patch.object(
            status_application,
            "configured_lifecycle_operation_location",
            return_value=(contract.contract_path, location),
        ),
        mock.patch.object(status_application, "observe_sync_operation", return_value=None),
        mock.patch.object(status_application, "load_contract", return_value=contract),
        mock.patch.object(
            status_application,
            "require_contract_matches_lifecycle_operation_location",
            side_effect=error,
        ),
        mock.patch.object(
            status_application,
            "_location_decision_summary",
            return_value=expected,
        ) as project,
    ):
        assert (
            status_application.worktree_status_packet(_config(), contract.contract_path) is expected
        )
    assert project.call_args.kwargs["sync_operation"] is None


def test_status_tool_projects_sync_and_controlled_admission_refusals(tmp_path: Path) -> None:
    config = _config()
    contract = _contract(tmp_path)
    result: dict[str, object] = {"contract_path": contract.contract_path.as_posix()}
    projection = mock.Mock()
    projection.model_dump.return_value = {"state": "running"}
    location = SimpleNamespace(worktree_group=tmp_path)
    with (
        mock.patch.object(worktree_application, "_task_ref_namespace", return_value=WorktreeArgs()),
        mock.patch.object(
            worktree_application.git_worktree_manager,
            "status_result",
            return_value=WorktreeCommandResult(0, {}),
        ),
        mock.patch.object(worktree_application, "_worktree_result", return_value=result),
        mock.patch.object(
            worktree_application,
            "configured_lifecycle_operation_location",
            return_value=(contract.contract_path, location),
        ),
        mock.patch.object(worktree_application, "observe_sync_operation", return_value=projection),
        mock.patch.object(
            worktree_application,
            "project_contract_status",
            side_effect=lambda _config, payload, _path, _caller: payload,
        ),
    ):
        projected = worktree_application.worktree_status_tool(config, TaskRef(repo_id="repo"))
    assert projected["syncOperation"] == {"state": "running"}

    terminal_refusal = ConfiguredContractRefused(
        reason="authority-invalid",
        status="terminal-archive-invalid",
        detail="archive invalid",
        expected={},
        observed={},
    )
    terminal_result: dict[str, object] = {}
    with (
        mock.patch.object(
            status_application,
            "admit_configured_terminal_contract",
            return_value=terminal_refusal,
        ),
        mock.patch.object(
            status_application,
            "project_configured_contract_refusal",
            return_value={"status": "terminal-archive-invalid"},
        ),
        mock.patch.object(status_application, "_replace_operation_status") as replace_status,
    ):
        assert (
            status_application.project_contract_status(
                config,
                terminal_result,
                contract.contract_path,
                None,
            )
            is terminal_result
        )
    assert terminal_result["status"] == "terminal-archive-invalid"
    replace_status.assert_called_once_with(terminal_result, [])

    caller_error = LifecycleCallerError("caller-invalid", "caller identity is invalid")
    ordinary_refusal = replace(terminal_refusal, status="configured-contract-authority-invalid")
    with (
        mock.patch.object(
            status_application,
            "admit_configured_terminal_contract",
            return_value=ordinary_refusal,
        ),
        mock.patch.object(
            status_application,
            "resolve_lifecycle_caller",
            side_effect=caller_error,
        ),
    ):
        refused = status_application.project_contract_status(
            config,
            {},
            contract.contract_path,
            None,
        )
    assert refused["status"] == "caller-invalid"


def test_sync_authority_refuses_unknown_contract_kind(tmp_path: Path) -> None:
    contract = replace(_contract(tmp_path), kind="unknown")
    with pytest.raises(RuntimeError, match="unsupported contract kind"):
        integration_branch_authority.require_sync_worktree(contract)


def test_terminal_replays_release_exact_selection_for_abandon_and_cleanup(tmp_path: Path) -> None:
    contract = _contract(tmp_path, series=True)
    abandon_expected = WorktreeCommandResult(0, {"state": "abandoned"})
    abandon_terminal = SimpleNamespace(
        archive=SimpleNamespace(
            cleanupOperation="worktree_abandon",
            cleanupArguments=TerminalWorktreeAbandonArguments(force=False),
        ),
        state="cleanup-completed",
        archived_contract=contract,
    )
    with (
        mock.patch.object(abandon_module, "load_contract", return_value=contract),
        mock.patch.object(
            abandon_module,
            "terminal_contract_authority_if_present",
            return_value=abandon_terminal,
        ),
        mock.patch.object(
            abandon_module,
            "_already_abandoned",
            return_value=WorktreeCommandResult(0, {"state": "already-abandoned"}),
        ),
        mock.patch.object(
            abandon_module,
            "with_terminal_atomic_series_release",
            return_value=abandon_expected,
        ),
    ):
        result = abandon_module.abandon_result(
            WorktreeArgs(contract_path=contract.contract_path, approved=True)
        )
    assert result is abandon_expected

    cleanup_expected = WorktreeCommandResult(0, {"state": "cleaned"})
    cleanup_terminal = SimpleNamespace(
        archive=SimpleNamespace(
            cleanupOperation="worktree_cleanup",
            cleanupArguments=TerminalWorktreeCleanupArguments(teardown_providers=True),
        ),
        state="cleanup-completed",
        archived_contract=contract,
    )
    with (
        mock.patch.object(cleanup_module, "load_contract", return_value=contract),
        mock.patch.object(
            cleanup_module,
            "terminal_contract_authority_if_present",
            return_value=cleanup_terminal,
        ),
        mock.patch.object(
            cleanup_module,
            "_already_completed_cleanup",
            return_value=WorktreeCommandResult(0, {"state": "already-clean"}),
        ),
        mock.patch.object(
            cleanup_module,
            "with_terminal_atomic_series_release",
            return_value=cleanup_expected,
        ),
    ):
        result = cleanup_module.cleanup_result(
            WorktreeArgs(contract_path=contract.contract_path, approved=True)
        )
    assert result is cleanup_expected


def test_finalization_returns_a_typed_activation_release_refusal(tmp_path: Path) -> None:
    contract = _contract(tmp_path, series=True)
    blocked = WorktreeCommandResult(2, {"detail": "release failed"})
    with mock.patch.object(
        finalize_module,
        "with_terminal_atomic_series_release",
        return_value=blocked,
    ):
        result = finalize_module._finalized_result(
            contract,
            finalize_module.FinalizeArgs(contract.contract_path),
            cleanup={},
            updates={},
            projection_effects=[],
        )
    assert result.returncode == 2
    assert result.payload["state"] == "activation-release-blocked"


def test_attach_and_bootstrap_return_atomic_admission_refusals(tmp_path: Path) -> None:
    leaf = _contract(tmp_path)
    parent = _contract(tmp_path, series=True)
    blocked = WorktreeCommandResult(2, {"state": "atomic-series-admission-blocked"})
    with (
        mock.patch.object(start_module, "load_contract_from_args", return_value=leaf),
        mock.patch.object(start_module, "require_matching_lifecycle_operation_location"),
        mock.patch.object(start_module, "require_ordinary_worktree"),
        mock.patch.object(
            start_module,
            "require_parent_series_accepting_leaves",
            return_value=parent,
        ),
        mock.patch.object(start_module, "activate_atomic_series_contract", return_value=blocked),
    ):
        assert start_module.attach_result(WorktreeArgs()) is blocked

    spec = start_contract.MasterSeriesContractSpec(
        coordination_root=tmp_path / "coordination",
        repo_name="repo",
        code_repo=tmp_path / "code",
        memory_root=None,
        task_root=parent.task_root,
        task_name="master",
        parent_task_name="sprint",
        protected_branch="super",
    )
    with (
        mock.patch.object(start_contract, "_require_commanded_atomic_master"),
        mock.patch.object(start_contract, "_bootstrap_preflight_contract", return_value=parent),
        mock.patch.object(
            start_contract,
            "atomic_series_activation_input_refusal",
            return_value=blocked,
        ),
    ):
        assert start_contract.ensure_master_series_contract(spec) is blocked


def test_sync_public_boundary_covers_missing_changed_and_series_routes(tmp_path: Path) -> None:
    leaf = _contract(tmp_path)
    with (
        mock.patch.object(sync_module, "load_contract_from_args", return_value=leaf),
        mock.patch.object(sync_module, "require_sync_worktree"),
    ):
        missing = sync_module.sync_result(WorktreeArgs())
    assert missing.payload["state"] == "blocked"

    changed = replace(leaf, code_base_commit=OTHER_SHA)
    with (
        mock.patch.object(sync_module, "integration_authority_lock", return_value=nullcontext()),
        mock.patch.object(sync_module, "load_contract_from_args", return_value=changed),
    ):
        retry = sync_module._sync_live(leaf, WorktreeArgs(), {})
    assert retry.payload["state"] == "sync-contract-changed-retry"

    series = _contract(tmp_path, series=True)
    synced = WorktreeCommandResult(0, {"state": "synced"})
    with (
        mock.patch.object(sync_module, "integration_authority_lock", return_value=nullcontext()),
        mock.patch.object(sync_module, "load_contract_from_args", return_value=series),
        mock.patch.object(sync_module, "require_sync_worktree"),
        mock.patch.object(
            sync_module,
            "sync_selected_atomic_series_under_authority",
            return_value=synced,
        ),
    ):
        assert sync_module._sync_live(series, WorktreeArgs(), {}) is synced
