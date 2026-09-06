"""Public totality forcing for strict current and successor lifecycle journals."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from unittest import mock

import pytest
from agents_remember.application.context_packet import ContextPacketRequest, build_context_packet
from agents_remember.application.lifecycle.direct_landing import direct_landing_tool
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.worktree_status import worktree_status_packet
from agents_remember.application.worktree_tools import (
    CloseoutApproval,
    CloseoutCommitMessages,
    OperationControlRequest,
    worktree_closeout_apply_tool,
    worktree_integrate_tool,
    worktree_operation_control_tool,
    worktree_status_tool,
)
from agents_remember.kernel.primitives.runtime_config import (
    McpRuntimeConfig,
    RepositoryScope,
    load_config,
)
from agents_remember.models.lifecycles.operation import IntegrateOperationInput
from agents_remember.worktrees.direct_landing import DirectLandingRequest
from agents_remember.worktrees.integration.direct_landing.direct_landing_operation import (
    direct_landing_store,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    resolve_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from closeout_input_test_support import closeout_operation_input, start_closeout_operation
from selected_lifecycle_test_support import (
    completed_selected_closeout_for_integration,
    selected_contract,
)
from test_direct_landing import _series_fixture
from test_lifecycle_operations import _contract

JournalMode = Literal["malformed", "invalid-schema-3", "os-error"]
UnreadableContractJournalMode = Literal["valid", "malformed"]
ContextOperationKind = Literal["closeout", "integrate", "direct-landing"]
LocationFailureMode = Literal["missing-locator", "manifest-mismatch", "unreadable-manifest"]
ContractLossMode = Literal["missing", "unreadable"]


def _byte_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _status(config, contract) -> dict[str, Any]:
    with mock.patch(
        "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
        return_value=WorktreeCommandResult(
            0,
            {
                "contract_path": contract.contract_path.as_posix(),
                "task_name": contract.task_name,
            },
        ),
    ):
        return worktree_status_tool(
            config,
            TaskRef(
                repo_id=contract.repo_name,
                contract_path=contract.contract_path.as_posix(),
            ),
        )


def _admit(tmp_path: Path, kind: Literal["closeout", "integrate"]):
    contract = selected_contract(tmp_path)
    if kind == "closeout":
        operation_input = closeout_operation_input(contract)
        start_closeout_operation(operation_input, launcher=lambda *_: None)
    else:
        contract = completed_selected_closeout_for_integration(contract)
        operation_input = IntegrateOperationInput(
            configPath=(contract.code_repo_path.parent / "settings.json").as_posix(),
            contractPath=contract.contract_path.as_posix(),
        )
        start_or_observe_operation(operation_input, contract, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, kind))
    record = store.read()
    assert record is not None
    config = load_config(Path(record.input.configPath))
    status = _status(config, contract)
    operation = next(row for row in status["lifecycleOperations"] if row["kind"] == kind)
    assert operation["legalControls"]
    return contract, config, store, cast(dict[str, Any], operation["legalControls"][0])


def _context_config(config, contract):
    payload = json.loads(config.config_path.read_text(encoding="utf-8"))
    repository = payload["repositories"][contract.repo_name]
    repository["contractPath"] = contract.contract_path.as_posix()
    config.config_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return load_config(config.config_path)


def _admit_direct(tmp_path: Path):
    fixture = _series_fixture(tmp_path / "direct")
    contract = fixture["contract"]
    request = DirectLandingRequest(
        contract_path=contract.contract_path.as_posix(),
        code_commit=fixture["code_head"],
        memory_commit_message="direct memory",
        ledger_commit_message="direct ledger",
        candidate_tree=fixture["candidate_tree"],
        intent_note="approve retained direct landing",
    )
    with (
        mock.patch("agents_remember.worktrees.direct_landing.require_first_ready_generation"),
        mock.patch(
            "agents_remember.worktrees.direct_landing.execute_or_require_direct_landing_recovery",
            return_value={"ok": True, "state": "admitted"},
        ),
    ):
        admitted = direct_landing_tool(fixture["config"], request)
    assert admitted["ok"] is True
    config = McpRuntimeConfig(
        config_path=fixture["config"].config_path,
        coordination_root=contract.coordination_root,
        workspace_root=contract.code_repo_path.parent,
        transcript_root=tmp_path / "transcripts",
        repositories={
            contract.repo_name: RepositoryScope(
                repo_id=contract.repo_name,
                path=contract.code_repo_path,
                memory_root=contract.memory_repo_path,
                contract_path=contract.contract_path,
            )
        },
        direct_execution_enabled=True,
    )
    return contract, config, direct_landing_store(contract)


def _admit_context_operation(tmp_path: Path, kind: ContextOperationKind):
    if kind == "direct-landing":
        return _admit_direct(tmp_path)
    contract, config, store, _stale = _admit(tmp_path, kind)
    return contract, _context_config(config, contract), store


def _install_journal_failure(
    store: LifecycleOperationStore,
    mode: JournalMode,
    private: str,
) -> tuple[Path, AbstractContextManager[object]]:
    path = store.path
    if mode == "malformed":
        path.write_text(f'{{"private":"{private}"', encoding="utf-8")
        return path, nullcontext()
    if mode == "invalid-schema-3":
        path.write_text(
            json.dumps({"schemaVersion": "3.0", "private": private}),
            encoding="utf-8",
        )
        return path, nullcontext()
    real_read_text = Path.read_text

    def unreadable(
        current: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if current == path:
            raise PermissionError(private)
        return real_read_text(current, encoding=encoding, errors=errors)

    return path, mock.patch.object(Path, "read_text", new=unreadable)


def test_registered_public_reader_refuses_empty_proven_claim_timestamp(
    tmp_path: Path,
) -> None:
    contract, config, store, stale = _admit(tmp_path, "integrate")
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    authority = payload["integrationAuthority"]
    payload["recoveryCommits"] = {
        "codeCommit": authority["codeCandidateCommit"],
        "memoryContentCommit": authority["memoryContentCommit"],
        "ledgerCommit": authority["ledgerCommit"],
    }
    payload["integrationPublication"] = {
        "operationKey": payload["operationKey"],
        "generation": payload["generation"],
        "preparedAt": "2026-08-23T00:00:00+00:00",
        "claimState": "proven",
        "claimTransferredAt": "",
        "queueSprintTaskDocument": "tasks/repo/sprint/task.md",
        "queueCandidateTaskDocument": "tasks/repo/leaf/task.md",
        "queueCandidateSha256": "1" * 64,
        "closeoutDoorGenerationId": "2" * 64,
        "closeoutOperationFingerprint": "3" * 64,
        "closeoutOperationKey": "4" * 64,
    }
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    before = _byte_tree(tmp_path)

    fresh = _status(config, contract)
    projected = next(row for row in fresh["lifecycleOperations"] if row["kind"] == "integrate")
    repeated = worktree_integrate_tool(
        config,
        contract_path=contract.contract_path.as_posix(),
    )
    stale_result = worktree_operation_control_tool(
        config,
        OperationControlRequest(**cast(dict[str, Any], stale["arguments"])),
    )

    decision = cast(dict[str, Any], projected["result"])
    assert projected["status"] == "unreadable"
    assert projected["legalControls"] == []
    assert decision["nextAction"] == "developer-decision"
    for response in (repeated, stale_result):
        assert response["ok"] is False
        assert response["status"] == decision["state"]
        for key in (
            "developerDecisionRequired",
            "decisionSurface",
            "nextAction",
            "expected",
            "observed",
        ):
            assert response[key] == decision[key]
    assert _byte_tree(tmp_path) == before


@pytest.mark.parametrize("kind", ["closeout", "integrate"])
@pytest.mark.parametrize("mode", ["malformed", "invalid-schema-3", "os-error"])
def test_strict_journal_failure_status_stale_control_and_context_are_total(
    tmp_path: Path,
    kind: Literal["closeout", "integrate"],
    mode: JournalMode,
) -> None:
    contract, config, store, stale = _admit(tmp_path, kind)
    private = f"PRIVATE_{kind}_{mode}_JOURNAL /tmp/private parser input"
    _path, patcher = _install_journal_failure(store, mode, private)
    before = _byte_tree(tmp_path)

    with patcher:
        fresh = _status(config, contract)
        projected = next(row for row in fresh["lifecycleOperations"] if row["kind"] == kind)
        arguments = cast(dict[str, Any], stale["arguments"])
        refused = worktree_operation_control_tool(
            config,
            OperationControlRequest(**arguments),
        )
        repeated = (
            worktree_closeout_apply_tool(
                config,
                contract.contract_path.as_posix(),
                CloseoutCommitMessages(code="repeat exact closeout"),
                CloseoutApproval(intent_note="repeat accepted closeout"),
            )
            if kind == "closeout"
            else worktree_integrate_tool(
                config,
                contract_path=contract.contract_path.as_posix(),
            )
        )
        packet = worktree_status_packet(config, contract.contract_path).model_dump(
            mode="json", exclude_none=True
        )

    self_result = cast(dict[str, Any], projected["result"])
    assert projected["status"] == "unreadable"
    assert projected["legalControls"] == []
    assert self_result["nextAction"] == "developer-decision"
    for response in (refused, repeated):
        assert response["ok"] is False
        assert response["status"] == self_result["state"]
        for key in (
            "developerDecisionRequired",
            "decisionSurface",
            "nextAction",
            "expected",
            "observed",
        ):
            assert response[key] == self_result[key]
    assert packet["lifecycleOperation"]["result"] == self_result
    assert private not in repr([fresh, refused, repeated, packet])
    assert store.path.as_posix() not in repr([fresh, refused, repeated, packet])
    assert _byte_tree(tmp_path) == before


@pytest.mark.parametrize("kind", ["closeout", "integrate"])
@pytest.mark.parametrize("mode", ["valid", "malformed"])
def test_unreadable_contract_public_start_status_and_real_context_use_locator_journal(
    tmp_path: Path,
    kind: Literal["closeout", "integrate"],
    mode: UnreadableContractJournalMode,
) -> None:
    contract, config, store, stale = _admit(tmp_path, kind)
    config = _context_config(config, contract)
    private = f"PRIVATE_UNREADABLE_{kind}_{mode} /tmp/contract parser input"
    patcher: AbstractContextManager[object] = nullcontext()
    if mode != "valid":
        _path, patcher = _install_journal_failure(store, mode, private)
    contract.contract_path.write_text(private, encoding="utf-8")
    before = _byte_tree(tmp_path)

    with (
        patcher,
        mock.patch(
            "agents_remember.worktrees.integration.lifecycle.lifecycle_operations.launch_detached_worker"
        ) as launch,
    ):
        fresh = _status(config, contract)
        projected = next(row for row in fresh["lifecycleOperations"] if row["kind"] == kind)
        repeated = (
            worktree_closeout_apply_tool(
                config,
                contract.contract_path.as_posix(),
                CloseoutCommitMessages(code="repeat exact closeout"),
                CloseoutApproval(intent_note="repeat accepted closeout"),
            )
            if kind == "closeout"
            else worktree_integrate_tool(
                config,
                contract_path=contract.contract_path.as_posix(),
            )
        )
        packet = build_context_packet(
            config,
            ContextPacketRequest(repo_id=contract.repo_name, include_providers=False),
        )
        arguments = cast(dict[str, Any], stale["arguments"])
        stale_result = worktree_operation_control_tool(
            config,
            OperationControlRequest(**arguments),
        )

    result = cast(dict[str, Any], projected["result"])
    assert projected["legalControls"] == []
    assert result["nextAction"] == "developer-decision"
    for response in (repeated, stale_result):
        assert response["ok"] is False
        assert response["status"] == result["state"]
        for key in (
            "developerDecisionRequired",
            "decisionSurface",
            "nextAction",
            "expected",
            "observed",
        ):
            assert response[key] == result[key]
    assert packet["worktree"]["state"] == "invalidContract"
    assert packet["worktree"]["lifecycleOperation"]["result"] == result
    assert private not in repr([fresh, repeated, stale_result, packet])
    assert store.path.as_posix() not in repr([fresh, repeated, stale_result, packet])
    launch.assert_not_called()
    assert _byte_tree(tmp_path) == before


@pytest.mark.parametrize("kind", ["closeout", "integrate", "direct-landing"])
def test_deleted_contract_real_context_retains_exact_root_journal_operation(
    tmp_path: Path,
    kind: ContextOperationKind,
) -> None:
    contract, config, store = _admit_context_operation(tmp_path, kind)
    record = store.read()
    assert record is not None
    contract.contract_path.unlink()
    before = _byte_tree(tmp_path)

    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.lifecycle_operations.launch_detached_worker"
    ) as launch:
        fresh = worktree_status_tool(
            config,
            TaskRef(
                repo_id=contract.repo_name,
                contract_path=contract.contract_path.as_posix(),
            ),
        )
        packet = build_context_packet(
            config,
            ContextPacketRequest(repo_id=contract.repo_name, include_providers=False),
        )

    projected = next(row for row in fresh["lifecycleOperations"] if row["kind"] == kind)
    context_operation = packet["worktree"]["lifecycleOperation"]
    assert packet["worktree"]["state"] == "missingContract"
    assert packet["worktree"]["errorEvidence"]["observed"] == {"state": "missing"}
    assert context_operation["kind"] == kind
    assert context_operation["generation"] == record.generation
    assert context_operation["generation"] == projected["generation"]
    assert context_operation["result"] == projected["result"]
    assert context_operation["legalControls"] == []
    assert context_operation["result"]["nextAction"] == "developer-decision"
    assert "nextTool" not in context_operation["result"]
    assert "nextArgs" not in context_operation["result"]
    public = repr([fresh, packet])
    assert store.path.as_posix() not in public
    for private_key in ("operationKey", "claimedOperationKey", "legacyOperationKey"):
        assert private_key not in public
    launch.assert_not_called()
    assert _byte_tree(tmp_path) == before


def _install_location_failure(
    contract,
    mode: LocationFailureMode,
    private: str,
) -> AbstractContextManager[object]:
    location = resolve_lifecycle_operation_location(
        contract.coordination_root,
        contract.contract_path,
    )
    if mode == "missing-locator":
        location.locator_path.unlink()
        return nullcontext()
    if mode == "manifest-mismatch":
        payload = json.loads(location.manifest_path.read_text(encoding="utf-8"))
        payload["bindingFingerprint"] = "f" * 64
        location.manifest_path.write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )
        return nullcontext()
    real_read_text = Path.read_text

    def unreadable(
        current: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if current.resolve(strict=False) == location.manifest_path.resolve(strict=False):
            raise PermissionError(private)
        return real_read_text(current, encoding=encoding, errors=errors)

    return mock.patch.object(Path, "read_text", new=unreadable)


@pytest.mark.parametrize(
    "mode",
    ["missing-locator", "manifest-mismatch", "unreadable-manifest"],
)
def test_real_context_preserves_exact_locator_decision_parity_without_mutation(
    tmp_path: Path,
    mode: LocationFailureMode,
) -> None:
    contract, config, _store, _stale = _admit(tmp_path, "closeout")
    config = _context_config(config, contract)
    private = f"PRIVATE_{mode}_LOCATOR_BACKEND /tmp/arbitrary internal path"
    patcher = _install_location_failure(contract, mode, private)
    before = _byte_tree(tmp_path)

    with (
        patcher,
        mock.patch(
            "agents_remember.worktrees.integration.lifecycle.lifecycle_operations.launch_detached_worker"
        ) as launch,
    ):
        fresh = _status(config, contract)
        packet = build_context_packet(
            config,
            ContextPacketRequest(repo_id=contract.repo_name, include_providers=False),
        )

    worktree = packet["worktree"]
    assert fresh["lifecycleOperations"] == []
    assert "lifecycleOperation" not in worktree
    for key in (
        "status",
        "expected",
        "observed",
        "developerDecisionRequired",
        "decisionSurface",
        "nextAction",
    ):
        assert worktree[key] == fresh[key]
    assert worktree["developerDecisionRequired"] is True
    assert worktree["nextAction"] == "developer-decision"
    assert "nextTool" not in worktree
    assert "nextArgs" not in worktree
    public = repr([fresh, packet])
    assert private not in public
    for private_key in ("operationKey", "claimedOperationKey", "legacyOperationKey"):
        assert private_key not in public
    launch.assert_not_called()
    assert _byte_tree(tmp_path) == before


@pytest.mark.parametrize("mode", ["missing", "unreadable"])
def test_addressable_contract_loss_without_journal_is_one_public_decision(
    tmp_path: Path,
    mode: ContractLossMode,
) -> None:
    contract, config, location, private = _addressable_contract_loss(tmp_path, mode)
    before = _byte_tree(tmp_path)

    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.lifecycle_operations.launch_detached_worker"
    ) as launch:
        fresh = worktree_status_tool(
            config,
            TaskRef(
                repo_id=contract.repo_name,
                contract_path=contract.contract_path.as_posix(),
            ),
        )
        packet = build_context_packet(
            config,
            ContextPacketRequest(repo_id=contract.repo_name, include_providers=False),
        )

    _assert_contract_loss_decision(
        fresh,
        packet,
        _ContractLossCase(contract, location, mode, private),
    )
    launch.assert_not_called()
    assert _byte_tree(tmp_path) == before


def _addressable_contract_loss(tmp_path: Path, mode: ContractLossMode):
    contract = _contract(tmp_path)
    config = _context_config(load_config(tmp_path / "settings.json"), contract)
    location = resolve_lifecycle_operation_location(
        contract.coordination_root,
        contract.contract_path,
    )
    assert all(
        not location.journal_path(kind).exists()
        for kind in ("closeout", "integrate", "direct-landing")
    )
    private = f"PRIVATE_{mode.upper()}_CONTRACT_BACKEND /tmp/arbitrary secret"
    if mode == "missing":
        contract.contract_path.unlink()
    else:
        contract.contract_path.write_text(private, encoding="utf-8")
    return contract, config, location, private


@dataclass(frozen=True)
class _ContractLossCase:
    contract: Any
    location: Any
    mode: ContractLossMode
    private: str


def _assert_contract_loss_decision(fresh, packet, case: _ContractLossCase) -> None:
    contract = case.contract
    location = case.location
    mode = case.mode
    private = case.private
    expected = {
        "contractPath": location.contract_path.resolve(strict=False).as_posix(),
        "route": "locator -> root manifest -> root journal",
        "publicationState": "addressable",
        "locatorId": location.locator.locatorId,
        "publicationRequestId": location.locator.publicationRequestId,
        "bindingFingerprint": location.locator.bindingFingerprint,
        "expectedInitialContractSha256": location.locator.expectedInitialContractSha256,
        "provenInitialContractSha256": location.locator.provenInitialContractSha256,
        "manifestInitialContractSha256": location.manifest.initialContractSha256,
    }
    worktree = packet["worktree"]
    assert fresh["lifecycleOperations"] == []
    assert "lifecycleOperation" not in worktree
    assert worktree["state"] == ("missingContract" if mode == "missing" else "invalidContract")
    assert worktree["status"] == "operation-contract-publication-lost"
    surface = "the addressable enclosure's proven initial contract is missing or unreadable"
    assert worktree["decisionSurface"] == surface
    assert worktree["summary"] == surface
    assert worktree["detail"] == surface
    assert worktree["expected"] == expected
    assert worktree["observed"] == {
        "stage": "contract-read",
        "side": "contract",
        "name": contract.contract_path.name,
        "errorType": "ContractError",
        "state": mode,
    }
    for key in (
        "status",
        "summary",
        "detail",
        "expected",
        "observed",
        "developerDecisionRequired",
        "decisionSurface",
        "nextAction",
    ):
        assert fresh[key] == worktree[key]
    assert worktree["developerDecisionRequired"] is True
    assert worktree["nextAction"] == "developer-decision"
    for response in (fresh, worktree):
        assert "nextTool" not in response
        assert "nextArgs" not in response
        assert "legalControls" not in response
    public = repr([fresh, packet])
    assert private not in public
    for private_key in ("operationKey", "claimedOperationKey", "legacyOperationKey"):
        assert private_key not in public
