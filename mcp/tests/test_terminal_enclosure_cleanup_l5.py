"""High-value forcing for L5 terminal archive, receipt, and deletion recovery."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest import mock

from agents_remember.models.lifecycles.enclosure import (
    TerminalEnclosureArchive,
    TerminalWorktreeAbandonArguments,
    TerminalWorktreeCleanupArguments,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_enclosure_terminal import (
    terminal_enclosure_archive_paths,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    inspect_lifecycle_operation_locator,
)
from agents_remember.worktrees.integration.terminal_enclosure_archive import (
    terminal_archive_required_result,
)
from closeout_input_test_support import start_closeout_operation
from selected_lifecycle_test_support import selected_closeout_operation_input
from test_lifecycle_operations import _contract

ABANDON_ARGUMENTS = TerminalWorktreeAbandonArguments(force=False)
CLEANUP_ARGUMENTS = TerminalWorktreeCleanupArguments(teardown_providers=True)


def test_terminal_archive_reads_back_before_root_deletion_and_survives_retry(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path)

    published = terminal_archive_required_result(
        contract,
        operation="worktree_abandon",
        arguments=ABANDON_ARGUMENTS,
        dry_run=False,
    )

    assert published.returncode == 0
    assert published.payload["state"] == "terminal-archive-proven"
    observation = inspect_lifecycle_operation_locator(
        contract.coordination_root,
        contract.contract_path,
    )
    assert observation.state == "terminal-archived"
    assert observation.locator is not None
    archive_path = Path(str(published.payload["archivePath"]))
    archive = TerminalEnclosureArchive.model_validate_json(archive_path.read_bytes())
    assert archive.cleanupOperation == "worktree_abandon"
    assert {item.relativePath for item in archive.canonicalEntries} == {"enclosure-manifest.json"}

    shutil.rmtree(contract.worktree_group)
    retried = terminal_archive_required_result(
        contract,
        operation="worktree_abandon",
        arguments=ABANDON_ARGUMENTS,
        dry_run=False,
    )

    assert retried.returncode == 0
    assert retried.payload["cleanupRequestId"] == published.payload["cleanupRequestId"]
    assert retried.payload["archiveSha256"] == published.payload["archiveSha256"]


def test_terminal_archive_reuses_bytes_after_crash_before_locator_publication(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path)
    with mock.patch(
        "agents_remember.worktrees.integration.terminal_enclosure_archive."
        "publish_terminal_lifecycle_operation_location",
        side_effect=RuntimeError("forced cut after archive and receipt readback"),
    ):
        cut = terminal_archive_required_result(
            contract,
            operation="worktree_abandon",
            arguments=ABANDON_ARGUMENTS,
            dry_run=False,
        )

    assert cut.returncode == 2
    live = inspect_lifecycle_operation_locator(
        contract.coordination_root,
        contract.contract_path,
    )
    assert live.locator is not None
    accepted, _receipt = terminal_enclosure_archive_paths(
        contract.coordination_root,
        live.locator.publicationRequestId,
    )
    accepted_bytes = accepted.read_bytes()
    assert (
        inspect_lifecycle_operation_locator(
            contract.coordination_root,
            contract.contract_path,
        ).state
        == "addressable"
    )

    recovered = terminal_archive_required_result(
        contract,
        operation="worktree_abandon",
        arguments=ABANDON_ARGUMENTS,
        dry_run=False,
    )

    assert recovered.returncode == 0
    assert accepted.read_bytes() == accepted_bytes
    assert (
        inspect_lifecycle_operation_locator(
            contract.coordination_root,
            contract.contract_path,
        ).state
        == "terminal-archived"
    )


def test_terminal_archive_refuses_live_operation_and_preserves_enclosure(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    start_closeout_operation(
        selected_closeout_operation_input(contract),
        launcher=lambda *_: None,
    )

    refused = terminal_archive_required_result(
        contract,
        operation="worktree_abandon",
        arguments=ABANDON_ARGUMENTS,
        dry_run=False,
    )

    assert refused.returncode == 2
    assert refused.payload["state"] == "terminal-archive-operation-active"
    assert refused.payload["nextTool"] == "worktree_status"
    assert refused.payload["nextArgs"] == {
        "repo_id": contract.repo_name,
        "contract_path": contract.contract_path.as_posix(),
    }
    assert contract.worktree_group.is_dir()
    assert (
        inspect_lifecycle_operation_locator(
            contract.coordination_root,
            contract.contract_path,
        ).state
        == "addressable"
    )


def test_terminal_archive_binds_one_cleanup_disposition(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    accepted = terminal_archive_required_result(
        contract,
        operation="worktree_abandon",
        arguments=ABANDON_ARGUMENTS,
        dry_run=False,
    )
    assert accepted.returncode == 0

    changed = terminal_archive_required_result(
        contract,
        operation="worktree_cleanup",
        arguments=CLEANUP_ARGUMENTS,
        dry_run=False,
    )

    assert changed.returncode == 2
    assert changed.payload["state"] == "terminal-archive-operation-conflict"
    assert changed.payload["nextAction"] == "worktree_abandon"
    assert changed.payload["nextTool"] == "worktree_abandon"
    assert changed.payload["nextArgs"] == {
        "contract_path": contract.contract_path.as_posix(),
        "dry_run": False,
        "force": False,
    }
