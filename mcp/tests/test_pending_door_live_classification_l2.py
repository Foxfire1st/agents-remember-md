"""Public forcing for live classification of journaled closeout-door publications."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.worktree_tools import (
    CloseoutApproval,
    CloseoutCommitMessages,
    OperationControlRequest,
    worktree_closeout_apply_tool,
    worktree_operation_control_tool,
    worktree_status_tool,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.worktrees.integration.closeout import door as closeout_door
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operation_controls as controls_mod,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations as operations_mod
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_projection import (
    LifecycleControlProjectionContext,
    legal_operation_controls,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.worktree_contract import (
    ContractCells,
    amend_contract,
    load_contract,
    write_contract,
)
from closeout_input_test_support import start_closeout_operation
from lifecycle_control_test_support import publish_completed_disposition_task_authority
from selected_lifecycle_test_support import selected_closeout_operation_input
from test_closeout_generation_boundary import _publish_mutated_code_generation
from test_lifecycle_operation_controls_l2 import (
    _byte_tree,
    _dirty_closeout,
    _public_control,
)
from test_lifecycle_operations import _contract


def _public_status(config, contract, *, caller=None) -> dict:
    return worktree_status_tool(
        config,
        TaskRef(
            repo_id=contract.repo_name,
            contract_path=contract.contract_path.as_posix(),
        ),
        caller=caller,
    )


def _projected_closeout(status: dict) -> dict:
    return next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")


def _decision_semantics(decision: dict) -> dict:
    return {
        "status": decision["state"],
        "detail": decision["decisionSurface"],
        "developerDecisionRequired": decision["developerDecisionRequired"],
        "nextAction": decision["nextAction"],
        "expected": decision["expected"],
        "observed": decision["observed"],
    }


def _refusal_semantics(refusal: dict) -> dict:
    return {
        key: refusal[key]
        for key in (
            "status",
            "detail",
            "developerDecisionRequired",
            "nextAction",
            "expected",
            "observed",
        )
    }


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested for item in value.values() for nested in _recursive_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _recursive_keys(item)}
    return set()


def _pending_initial_door(tmp_path: Path):
    contract = _contract(tmp_path, selected_profile=True)
    (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    operation_input = selected_closeout_operation_input(contract, code="publish exact claimed door")
    with (
        mock.patch.object(
            operations_mod,
            "publish_door_intent",
            side_effect=SystemExit("cut before initial door contract write"),
        ),
        pytest.raises(SystemExit, match="before initial door contract write"),
    ):
        start_closeout_operation(operation_input, launcher=mock.Mock())
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    record = store.read()
    assert record is not None and record.doorPublication is not None
    assert record.doorPublication.state == "intent"
    stale = legal_operation_controls(load_contract(contract.contract_path), record)[0]
    assert stale["action"] == "recover"
    return contract, operation_input, store, record, stale


def test_initial_pending_door_third_contract_is_total_across_public_surfaces(
    tmp_path: Path,
) -> None:
    contract, operation_input, store, _record, stale = _pending_initial_door(tmp_path)
    third = amend_contract(
        load_contract(contract.contract_path),
        ContractCells(integration_status="blocked"),
    )
    write_contract(contract.contract_path, third)
    config = load_config(Path(operation_input.configPath))
    before = _byte_tree(tmp_path)

    projected = _projected_closeout(_public_status(config, third))
    assert projected["legalControls"] == []
    decision = projected["result"]
    assert decision["state"] == "closeout-door-publication-conflict"
    assert decision["nextAction"] == "developer-decision"
    assert not {"operationKey", "claimedOperationKey"} & _recursive_keys(decision)
    for stale_key in ("nextTool", "nextArgs", "arguments", "apply", "applyStep"):
        assert stale_key not in decision

    with (
        mock.patch.object(controls_mod, "launch_detached_worker") as control_launch,
        mock.patch.object(operations_mod, "launch_detached_worker") as apply_launch,
    ):
        refused = _public_control(config, stale)
        repeated_apply = worktree_closeout_apply_tool(
            config,
            contract.contract_path.as_posix(),
            CloseoutCommitMessages(code="publish exact claimed door"),
            CloseoutApproval(intent_note=operation_input.approvalNote),
        )
    assert refused["ok"] is False
    assert _refusal_semantics(refused) == _decision_semantics(decision)
    assert repeated_apply["ok"] is False
    assert _refusal_semantics(repeated_apply) == _decision_semantics(decision)
    assert not {"operationKey", "claimedOperationKey"} & _recursive_keys(refused)
    assert not {"operationKey", "claimedOperationKey"} & _recursive_keys(repeated_apply)
    assert _byte_tree(tmp_path) == before
    assert store.read() is not None
    control_launch.assert_not_called()
    apply_launch.assert_not_called()


def test_unreadable_pending_door_status_and_stale_handler_share_exact_decision(
    tmp_path: Path,
) -> None:
    contract, operation_input, store, _record, stale = _pending_initial_door(tmp_path)
    private_sentinel = "PRIVATE-CONTRACT-PARSER-/secret/path"
    contract.contract_path.write_text(
        f'{{ "{private_sentinel}": ',
        encoding="utf-8",
    )
    config = load_config(Path(operation_input.configPath))
    before = _byte_tree(tmp_path)

    status = _public_status(config, contract)
    assert status["ok"] is False
    assert status["status"] == "worktree-contract-unreadable"
    projected = _projected_closeout(status)
    assert projected["legalControls"] == []
    decision = projected["result"]
    assert decision["state"] == "closeout-door-publication-conflict"
    assert decision["observed"]["readStatus"] == "unreadable"
    refused = _public_control(config, stale)
    assert refused["ok"] is False
    assert _refusal_semantics(refused) == _decision_semantics(decision)
    assert private_sentinel not in repr([status, decision, refused, store.read()])
    assert _byte_tree(tmp_path) == before
    assert store.read() is not None


@pytest.mark.parametrize("mode", ["cancel", "supersede"])
def test_all_pending_door_dispositions_refuse_live_third_state_without_mutation(
    tmp_path: Path,
    mode: str,
) -> None:
    contract, store, config, row, caller = _pending_disposition_fixture(tmp_path, mode)
    interrupt_selected_publication = _interrupt_pending_disposition(mode)
    with mock.patch.object(
        closeout_door,
        "write_contract",
        side_effect=interrupt_selected_publication,
    ):
        interrupted = _public_control(config, row)
    assert interrupted["ok"] is False
    assert interrupted["status"] == "closeout-door-publication-interrupted"
    pending = store.read()
    assert pending is not None and pending.doorPublication is not None
    assert pending.doorPublication.state == "intent"
    live = load_contract(contract.contract_path)
    write_contract(
        contract.contract_path,
        amend_contract(live, ContractCells(integration_status="blocked")),
    )
    before = _byte_tree(tmp_path)

    projected = _projected_closeout(_public_status(config, contract, caller=caller))
    assert projected["legalControls"] == []
    decision = projected["result"]
    assert decision["state"] == "closeout-door-publication-conflict"
    refused = worktree_operation_control_tool(
        config,
        OperationControlRequest(**interrupted["nextArgs"]),
    )
    assert refused["ok"] is False
    assert _refusal_semantics(refused) == _decision_semantics(decision)
    assert not {"operationKey", "claimedOperationKey"} & _recursive_keys(decision)
    assert not {"operationKey", "claimedOperationKey"} & _recursive_keys(refused)
    assert _byte_tree(tmp_path) == before


def _pending_disposition_fixture(tmp_path: Path, mode: str):
    if mode == "cancel":
        contract, _operation_input, store, record = _dirty_closeout(tmp_path)
        config = load_config(Path(record.input.configPath))
        row = next(
            item
            for item in legal_operation_controls(contract, record)
            if item["action"] == "cancel"
        )
        caller = None
    else:
        contract = _contract(tmp_path, selected_profile=True)
        _operation_input, store, contract = _publish_mutated_code_generation(contract)
        record = store.read()
        assert record is not None
        config = load_config(Path(record.input.configPath))
        caller = publish_completed_disposition_task_authority(
            contract,
            sprint_owned=True,
        )
        row = next(
            item
            for item in legal_operation_controls(
                contract,
                record,
                context=LifecycleControlProjectionContext(
                    allow_completed_disposition=True,
                    caller=caller,
                ),
            )
            if item["action"] == mode
        )
    return contract, store, config, row, caller


def _interrupt_pending_disposition(_mode: str):
    def interrupt_selected_publication(path, updated):
        del path, updated
        raise OSError("forced pending door publication")

    return interrupt_selected_publication
