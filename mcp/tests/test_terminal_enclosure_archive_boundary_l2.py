"""Public cleanup/abandon forcing across the terminal-enclosure archive boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from unittest import mock

import pytest
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.worktree_tools import (
    worktree_abandon_tool,
    worktree_cleanup_tool,
    worktree_status_tool,
)
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig, load_config
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules import abandon as abandon_module
from agents_remember.worktrees.modules import cleanup as cleanup_module
from agents_remember.worktrees.worktree_contract import (
    ContractCells,
    WorktreeContract,
    amend_contract,
    load_contract,
    write_contract,
)
from closeout_input_test_support import (
    start_closeout_operation,
    with_mutation_intent,
)
from selected_lifecycle_test_support import selected_closeout_operation_input
from test_lifecycle_operation_controls_l2 import _integration_source_ready_contract
from test_lifecycle_operations import _contract
from test_worktree_support import git

TerminalOperation = Literal["worktree_cleanup", "worktree_abandon"]


def _prepare_terminal_operation(
    contract: WorktreeContract,
    operation: TerminalOperation,
) -> WorktreeContract:
    if operation == "worktree_abandon":
        return contract
    prepared = amend_contract(
        contract,
        ContractCells(closeout_status="completed", integration_status="completed"),
    )
    write_contract(prepared.contract_path, prepared)
    return prepared


def _call_terminal_operation(
    config: McpRuntimeConfig,
    contract: WorktreeContract,
    operation: TerminalOperation,
    *,
    teardown_providers: bool = True,
    force: bool = False,
) -> dict[str, object]:
    path = contract.contract_path.as_posix()
    if operation == "worktree_cleanup":
        return worktree_cleanup_tool(
            config,
            contract_path=path,
            dry_run=False,
            teardown_providers=teardown_providers,
        )
    return worktree_abandon_tool(
        config,
        contract_path=path,
        dry_run=False,
        force=force,
    )


def _terminal_status(
    config: McpRuntimeConfig,
    contract: WorktreeContract,
) -> dict[str, object]:
    return worktree_status_tool(
        config,
        TaskRef(
            repo_id=contract.repo_name,
            contract_path=contract.contract_path.as_posix(),
        ),
    )


def _terminal_arguments(
    contract: WorktreeContract,
    operation: TerminalOperation,
    *,
    teardown_providers: bool = True,
    force: bool = False,
) -> dict[str, object]:
    arguments: dict[str, object] = {
        "contract_path": contract.contract_path.as_posix(),
        "dry_run": False,
    }
    if operation == "worktree_cleanup":
        arguments["teardown_providers"] = teardown_providers
    else:
        arguments["force"] = force
    return arguments


def _assert_destructive_cut(
    result: dict[str, object],
    contract: WorktreeContract,
    operation: TerminalOperation,
) -> dict[str, object]:
    expected_state = "blocked" if operation == "worktree_cleanup" else "abandon-blocked"
    assert result["ok"] is False
    assert result["operation"] == operation
    assert result["state"] == expected_state
    terminal_archive = result["terminalArchive"]
    assert isinstance(terminal_archive, dict)
    assert terminal_archive["state"] == "terminal-archive-proven"
    assert terminal_archive["cleanupOperation"] == operation
    assert not contract.code_worktree.exists()
    return terminal_archive


def _assert_archive_ready(
    result: dict[str, object],
    contract: WorktreeContract,
    operation: TerminalOperation,
    *,
    force: bool,
) -> None:
    assert result["ok"] is True, result
    assert result["state"] == "terminal-archive-ready"
    assert result["status"] == "terminal-archive-ready"
    status_archive = result["terminalArchive"]
    assert isinstance(status_archive, dict)
    assert status_archive["contractState"] == "archive-ready"
    expected_arguments = _terminal_arguments(contract, operation, force=force)
    expected_arguments.pop("contract_path")
    expected_arguments.pop("dry_run")
    assert status_archive["cleanupArguments"] == expected_arguments
    assert result["nextTool"] == operation
    assert result["nextArgs"] == _terminal_arguments(contract, operation, force=force)


@pytest.mark.parametrize("operation", ["worktree_cleanup", "worktree_abandon"])
def test_public_terminal_operation_retries_same_disposition_after_destructive_cut(
    tmp_path: Path,
    operation: TerminalOperation,
) -> None:
    contract = _prepare_terminal_operation(_contract(tmp_path), operation)
    config = load_config(tmp_path / "settings.json")
    writes = 0

    def fail_terminal_publication_once(path: Path, value: WorktreeContract) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise RuntimeError("forced cut after destructive outputs")
        write_contract(path, value)

    publication_module = cleanup_module if operation == "worktree_cleanup" else abandon_module
    accepted_force = operation == "worktree_abandon"
    with mock.patch.object(
        publication_module,
        "write_contract",
        side_effect=fail_terminal_publication_once,
    ):
        cut = _call_terminal_operation(
            config,
            contract,
            operation,
            force=accepted_force,
        )

    terminal_archive = _assert_destructive_cut(cut, contract, operation)

    archive_ready = _terminal_status(config, contract)
    _assert_archive_ready(
        archive_ready,
        contract,
        operation,
        force=accepted_force,
    )
    assert load_contract(contract.contract_path).cleanup == contract.cleanup

    argument_conflict = _call_terminal_operation(
        config,
        contract,
        operation,
        teardown_providers=False,
        force=False,
    )
    assert argument_conflict["ok"] is False
    assert argument_conflict["status"] == "terminal-archive-request-conflict"
    assert argument_conflict["nextTool"] == operation
    assert argument_conflict["nextArgs"] == _terminal_arguments(
        contract,
        operation,
        force=accepted_force,
    )

    conflicting_operation: TerminalOperation = (
        "worktree_abandon" if operation == "worktree_cleanup" else "worktree_cleanup"
    )
    conflict = _call_terminal_operation(config, contract, conflicting_operation)
    assert conflict["ok"] is False
    assert conflict["status"] == "terminal-archive-operation-conflict"
    assert conflict["nextTool"] == operation
    assert conflict["nextArgs"] == _terminal_arguments(
        contract,
        operation,
        force=accepted_force,
    )

    recovered = _call_terminal_operation(
        config,
        contract,
        operation,
        force=accepted_force,
    )

    assert recovered["ok"] is True
    assert recovered["operation"] == operation
    if operation == "worktree_cleanup":
        assert recovered["state"] in {"already-clean", "cleanup-completed"}
        expected_cleanup = "completed"
    else:
        assert recovered["state"] == "abandoned"
        expected_cleanup = "abandoned"
    recovered_archive = recovered["terminalArchive"]
    assert isinstance(recovered_archive, dict)
    assert recovered_archive["cleanupRequestId"] == terminal_archive["cleanupRequestId"]
    assert load_contract(contract.contract_path).cleanup == expected_cleanup

    completed = _terminal_status(config, contract)
    assert completed["ok"] is True
    assert completed["state"] == "terminal-cleanup-completed"
    assert completed["status"] == "terminal-cleanup-completed"
    completed_archive = completed["terminalArchive"]
    assert isinstance(completed_archive, dict)
    assert completed_archive["contractState"] == "cleanup-completed"
    assert "nextAction" not in completed


def _start_closeout_record(contract: WorktreeContract) -> LifecycleOperationStore:
    start_closeout_operation(
        selected_closeout_operation_input(contract),
        launcher=lambda *_: None,
    )
    return LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))


def _assert_status_route(result: dict[str, object], contract: WorktreeContract) -> None:
    assert result["ok"] is False
    assert result["nextTool"] == "worktree_status"
    assert result["nextArgs"] == {
        "repo_id": contract.repo_name,
        "contract_path": contract.contract_path.as_posix(),
    }


def test_public_abandon_refuses_active_operation_with_executable_status_route(
    tmp_path: Path,
) -> None:
    contract = _integration_source_ready_contract(_contract(tmp_path, selected_profile=True))
    config = load_config(tmp_path / "settings.json")
    _start_closeout_record(contract)

    refused = _call_terminal_operation(config, contract, "worktree_abandon")

    assert refused["state"] == "terminal-archive-operation-active"
    assert refused["status"] == "terminal-archive-operation-active"
    _assert_status_route(refused, contract)
    assert contract.code_worktree.exists()


def test_public_abandon_refuses_ambiguous_mutation_with_executable_status_route(
    tmp_path: Path,
) -> None:
    contract = _integration_source_ready_contract(_contract(tmp_path, selected_profile=True))
    config = load_config(tmp_path / "settings.json")
    (contract.code_worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    store = _start_closeout_record(contract)
    store.update(with_mutation_intent)
    store.update(lambda record: record.model_copy(update={"status": "failed", "phase": "failed"}))
    (contract.code_worktree / "candidate.txt").unlink()
    git(contract.code_worktree, "add", "-u", "--", "candidate.txt")
    assert git(contract.code_worktree, "status", "--porcelain") == ""

    refused = _call_terminal_operation(config, contract, "worktree_abandon")

    assert refused["state"] == "terminal-archive-operation-mutation-ambiguous"
    assert refused["status"] == "terminal-archive-operation-mutation-ambiguous"
    _assert_status_route(refused, contract)
    assert contract.code_worktree.exists()
