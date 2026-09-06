"""Public forcing for the explicit, preview-bound pre-locator enclosure adoption."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents_remember.application.lifecycle.legacy_operation_tool import (
    LegacyOperationRequest,
    worktree_legacy_operation_tool,
)
from agents_remember.application.lifecycle.lifecycle_enclosure_tools import (
    EnclosureAdoptionRequest,
    worktree_enclosure_adopt_tool,
)
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig, load_config
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_enclosure_adoption as adoption_module,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    operation_key,
    operation_state_fingerprint,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleOperationLocation,
    lifecycle_operation_locator_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.terminal_enclosure_archive import _canonical_entries
from agents_remember.worktrees.modules.git import head_commit, require_git
from agents_remember.worktrees.worktree_contract import WorktreeContract
from closeout_fixture_test_support import selected_fixture
from lifecycle_enclosure_test_support import byte_tree
from test_closeout_queue import MASTER_A
from test_task_intent_consumers_and_legacy import _closeout_record_payload
from test_worktree_support import git


class _LostAdoptionResponse(BaseException):
    pass


@dataclass(frozen=True)
class _MigrationOrder:
    config: McpRuntimeConfig
    contract: WorktreeContract
    source: Path
    request: EnclosureAdoptionRequest
    preview: dict[str, Any]
    migrate: LegacyOperationRequest


def _legacy_enclosure(
    tmp_path: Path,
) -> tuple[McpRuntimeConfig, WorktreeContract, Path, bytes, EnclosureAdoptionRequest]:
    fixture = selected_fixture(tmp_path / "fixture", memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    git(contract.code_worktree, "add", "-A")
    git(contract.code_worktree, "commit", "-m", "legacy exact code output")
    code_commit = head_commit(contract.code_worktree)
    candidate_tree = require_git(
        contract.code_worktree,
        ["rev-parse", f"{code_commit}^{{tree}}"],
    )
    locator = lifecycle_operation_locator_path(contract.coordination_root, contract.contract_path)
    if locator.exists():
        locator.unlink()
    if (contract.worktree_group / ".lifecycle").exists():
        shutil.rmtree(contract.worktree_group / ".lifecycle")
    fingerprint = hashlib.sha256(b"explicit legacy adoption").hexdigest()
    payload = {
        "schemaVersion": "1.0",
        "taskId": contract.task_id,
        "taskName": contract.task_name,
        "contractPath": contract.contract_path.as_posix(),
        "operationKind": "closeout",
        "candidateState": operation_state_fingerprint(contract),
        "candidateTree": candidate_tree,
        "fingerprint": fingerprint,
        "operationKey": operation_key(contract.contract_path, "closeout", fingerprint),
        "input": {
            "kind": "closeout",
            "configPath": fixture.config_path.as_posix(),
            "contractPath": contract.contract_path.as_posix(),
            "codeCommitMessage": "legacy exact code output",
            "memoryCommitMessage": "",
            "ledgerCommitMessage": "",
            "approvalNote": "legacy owner approval",
            "gatePolicy": [],
        },
        "status": "input-required",
        "phase": "contract-finalization",
        "queuedAt": "2026-08-23T00:00:00+00:00",
        "startedAt": "2026-08-23T00:00:01+00:00",
        "heartbeatAt": "2026-08-23T00:00:02+00:00",
        "reportPath": (contract.worktree_group / "reports" / "closeout-operation.log").as_posix(),
        "irreversibleBoundaryEntered": True,
        "approvalClaimed": True,
        "recoveryCommits": {"codeCommit": code_commit},
    }
    source = contract.worktree_group / "reports" / "closeout-operation.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(payload, sort_keys=True).encode("utf-8")
    source.write_bytes(original)
    config = load_config(fixture.config_path)
    request = EnclosureAdoptionRequest(
        contract_path=contract.contract_path.as_posix(),
        expected_worktree_group=contract.worktree_group.as_posix(),
        rationale="adopt the explicit known pre-locator enclosure",
    )
    return config, contract, source, original, request


@pytest.mark.parametrize("migration_first", [True, False])
def test_adoption_and_schema_migration_execute_in_either_order(
    tmp_path: Path,
    migration_first: bool,
) -> None:
    config, contract, source, original, request = _legacy_enclosure(tmp_path)
    before = byte_tree(contract.coordination_root)
    inspected = worktree_legacy_operation_tool(
        config,
        contract.contract_path.as_posix(),
        LegacyOperationRequest(operation_kind="closeout", action="inspect"),
    )
    assert inspected["ok"] is True
    assert inspected["state"] == "inspected"
    preview = worktree_enclosure_adopt_tool(config, request)
    assert preview["state"] == "would-adopt-enclosure"
    assert preview["publicationRequestId"] == preview["nextArgs"]["expected_publication_request_id"]
    assert byte_tree(contract.coordination_root) == before
    migrate = LegacyOperationRequest(
        operation_kind="closeout",
        action="migrate",
        expected_digest=inspected["legacyDigest"],
        memory_commit_message="finish adopted memory",
        ledger_commit_message="map adopted memory",
        audit_reason="repair exact historic schema before normal operation reads",
    )
    order = _MigrationOrder(config, contract, source, request, preview, migrate)
    applied, migrated_bytes = (
        _migrate_then_adopt(order) if migration_first else _adopt_then_migrate(order)
    )
    assert applied["state"] == "enclosure-adopted"
    target = contract.worktree_group / ".lifecycle" / source.name
    assert target.read_bytes() == migrated_bytes
    assert not source.exists()
    current = LifecycleOperationStore(target).read()
    assert current is not None
    assert current.schemaVersion == "3.0"
    assert current.legacyMigration is not None
    assert current.legacyMigration.originalSha256 == hashlib.sha256(original).hexdigest()


def test_adoption_preserves_exact_missing_intent_generation_archive(
    tmp_path: Path,
) -> None:
    config, contract, source, _original, request = _legacy_enclosure(tmp_path)
    archive = source.with_name("closeout-operation.legacy-missing-intent-generation-1.json")
    archive_bytes = (
        json.dumps(
            {"schemaVersion": "3.0", **_closeout_record_payload()},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    archive.write_bytes(archive_bytes)
    near_match = source.with_name("closeout-operation.legacy-missing-intent-generation-1.log")
    near_match.write_bytes(b"not an owned missing-intent generation\n")

    preview = worktree_enclosure_adopt_tool(config, request)

    assert archive.name in {item["name"] for item in preview["artifacts"]}
    assert near_match.name not in {item["name"] for item in preview["artifacts"]}
    applied = worktree_enclosure_adopt_tool(
        config,
        EnclosureAdoptionRequest(**preview["nextArgs"]),
    )
    assert applied["state"] == "enclosure-adopted"
    target = contract.worktree_group / ".lifecycle" / archive.name
    assert target.read_bytes() == archive_bytes
    assert not archive.exists()
    assert near_match.read_bytes() == b"not an owned missing-intent generation\n"

    (target.parent / source.name).unlink()
    entries = _canonical_entries(
        cast(
            LifecycleOperationLocation,
            SimpleNamespace(lifecycle_directory=target.parent),
        ),
        operation="worktree_cleanup",
    )
    terminal = next(item for item in entries if item.relativePath == archive.name)
    assert terminal.content.encode() == archive_bytes
    assert terminal.sha256 == hashlib.sha256(archive_bytes).hexdigest()


def _migrate_then_adopt(order: _MigrationOrder):
    config = order.config
    contract = order.contract
    source = order.source
    request = order.request
    preview = order.preview
    migrate = order.migrate
    migrated = worktree_legacy_operation_tool(
        config,
        contract.contract_path.as_posix(),
        migrate,
    )
    assert migrated["ok"] is True
    assert migrated["state"] == "migrated"
    migrated_bytes = source.read_bytes()
    assert json.loads(migrated_bytes)["schemaVersion"] == "3.0"
    stale = worktree_enclosure_adopt_tool(
        config,
        EnclosureAdoptionRequest(**preview["nextArgs"]),
    )
    assert stale["status"] == "enclosure-adoption-preview-changed"
    refreshed = worktree_enclosure_adopt_tool(config, request)
    applied = worktree_enclosure_adopt_tool(
        config,
        EnclosureAdoptionRequest(**refreshed["nextArgs"]),
    )
    return applied, migrated_bytes


def _adopt_then_migrate(order: _MigrationOrder):
    config = order.config
    contract = order.contract
    source = order.source
    preview = order.preview
    migrate = order.migrate
    applied = worktree_enclosure_adopt_tool(
        config,
        EnclosureAdoptionRequest(**preview["nextArgs"]),
    )
    migrated = worktree_legacy_operation_tool(
        config,
        contract.contract_path.as_posix(),
        migrate,
    )
    assert migrated["ok"] is True
    assert migrated["state"] == "migrated"
    migrated_bytes = contract.worktree_group.joinpath(
        ".lifecycle",
        source.name,
    ).read_bytes()
    return applied, migrated_bytes


def test_adoption_binds_exact_preview_and_refuses_changed_bytes_without_publication(
    tmp_path: Path,
) -> None:
    config, contract, _source, _original, request = _legacy_enclosure(tmp_path)
    preview = worktree_enclosure_adopt_tool(config, request)
    contract.contract_path.write_text(
        contract.contract_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    before_apply = byte_tree(contract.coordination_root)
    refused = worktree_enclosure_adopt_tool(
        config,
        EnclosureAdoptionRequest(**preview["nextArgs"]),
    )
    assert refused["status"] == "enclosure-adoption-preview-changed"
    assert refused["expected"]["publicationRequestId"] == preview["publicationRequestId"]
    assert refused["observed"]["publicationRequestId"] != preview["publicationRequestId"]
    assert byte_tree(contract.coordination_root) == before_apply


def test_lost_response_and_idempotent_replay_converge_to_one_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, contract, _source, original, request = _legacy_enclosure(tmp_path)
    preview = worktree_enclosure_adopt_tool(config, request)
    apply_request = EnclosureAdoptionRequest(**preview["nextArgs"])
    original_retire = adoption_module._retire_legacy_source
    raised = False

    def lose_after_retire(source, artifact) -> None:
        nonlocal raised
        original_retire(source, artifact)
        if not raised:
            raised = True
            raise _LostAdoptionResponse()

    monkeypatch.setattr(adoption_module, "_retire_legacy_source", lose_after_retire)
    with pytest.raises(_LostAdoptionResponse):
        worktree_enclosure_adopt_tool(config, apply_request)
    monkeypatch.setattr(adoption_module, "_retire_legacy_source", original_retire)
    recovered = worktree_enclosure_adopt_tool(config, apply_request)
    repeated = worktree_enclosure_adopt_tool(config, apply_request)
    assert recovered["state"] == repeated["state"] == "enclosure-adopted"
    assert recovered["publicationRequestId"] == repeated["publicationRequestId"]
    receipt = contract.worktree_group / ".lifecycle" / "enclosure-adoption-receipt.json"
    assert receipt.is_file()
    assert (
        contract.worktree_group / ".lifecycle" / "closeout-operation.json"
    ).read_bytes() == original


def test_adoption_conflict_refuses_before_locator_or_manifest_publication(
    tmp_path: Path,
) -> None:
    config, contract, source, _original, request = _legacy_enclosure(tmp_path)
    preview = worktree_enclosure_adopt_tool(config, request)
    target = contract.worktree_group / ".lifecycle" / source.name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"third operation bytes")
    before = byte_tree(contract.coordination_root)
    refused = worktree_enclosure_adopt_tool(
        config,
        EnclosureAdoptionRequest(**preview["nextArgs"]),
    )
    assert refused["status"] == "operation-location-adoption-conflict"
    assert refused["nextAction"] == "developer-decision"
    assert byte_tree(contract.coordination_root) == before
