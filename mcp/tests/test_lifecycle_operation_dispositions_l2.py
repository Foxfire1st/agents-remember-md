"""Public L2 forcing for completed closeout scheduling dispositions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.application.lifecycle.lifecycle_control_authority import (
    completed_disposition_authorized,
)
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.worktree_tools import (
    OperationControlRequest,
    worktree_operation_control_tool,
    worktree_status_tool,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.operation import (
    IntegrateOperationInput,
    require_lifecycle_operation_dependencies,
)
from agents_remember.models.lifecycles.operation_kinds import LifecycleControlAction
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.worktrees.integration.closeout import door as closeout_door
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_projection import (
    LifecycleControlProjectionContext,
    legal_operation_controls,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls import (
    control_operation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from agents_remember.worktrees.modules.git import branch_commit
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.worktree_contract import load_contract
from lifecycle_control_test_support import publish_completed_disposition_task_authority
from test_closeout_generation_boundary import _publish_mutated_code_generation
from test_lifecycle_operation_controls_l2 import (
    _command,
    _integration_source_ready_contract,
    _public_control,
    _standalone_owner,
)
from test_lifecycle_operations import _contract


def _sprint_owner(contract) -> DeclaredCaller:
    return publish_completed_disposition_task_authority(
        contract,
        sprint_owned=True,
    )


def _disposition_preserved_artifacts(contract, record) -> dict[str, object]:
    contract_payload = asdict(load_contract(contract.contract_path))
    contract_payload.pop("closeout_door")
    record_payload = record.model_dump(mode="json")
    # recordRevision is journal-mutable under L18 (advanced exactly once per
    # accepted store mutation) and meaningfulRevision is journal-mutable under
    # CCR-R15 (advanced exactly once per meaningful state mutation);
    # disposition artifacts exclude both.
    for mutable in (
        "recordRevision",
        "meaningfulRevision",
        "generationDisposition",
        "supersedeDeclarationFingerprint",
        "doorPublication",
        "doorPublicationHistory",
        "guidance",
        "dependencies",
    ):
        record_payload.pop(mutable)
    refs = {
        "code-work": branch_commit(contract.code_repo_path, contract.code_work_branch),
        "code-source": branch_commit(contract.code_repo_path, contract.code_source_branch),
    }
    if contract.memory_repo_path is not None:
        refs.update(
            {
                "memory-work": branch_commit(
                    contract.memory_repo_path,
                    contract.memory_work_branch,
                ),
                "memory-source": branch_commit(
                    contract.memory_repo_path,
                    contract.memory_source_branch,
                ),
            }
        )
    report_path = Path(record.reportPath)
    return {
        "contract": contract_payload,
        "commits": (
            contract.code_commit,
            contract.memory_content_commit,
            contract.ledger_commit,
        ),
        "approval": (
            contract.approved_for_commit,
            contract.commit_approval_note,
            contract.human_review_status,
            contract.closeout_status,
            contract.integration_status,
        ),
        "refs": refs,
        "record": record_payload,
        "report": report_path.read_bytes(),
    }


@pytest.mark.parametrize("action", ["retire", "supersede"])
def test_completed_unintegrated_disposition_preserves_artifacts(
    tmp_path: Path,
    action: LifecycleControlAction,
) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    _operation_input, store, finalized = _publish_mutated_code_generation(contract)
    record = store.read()
    assert record is not None
    owner = _sprint_owner(finalized) if action == "supersede" else _standalone_owner(finalized)
    assert completed_disposition_authorized(finalized, owner)
    config = load_config(Path(record.input.configPath))
    accepted_contract = load_contract(finalized.contract_path)
    assert accepted_contract.closeout_door is not None
    accepted_door = accepted_contract.closeout_door.model_dump(mode="json")
    assert accepted_door["disposition"] == "claimed"
    report_path = Path(record.reportPath)
    report_path.write_bytes(b"accepted closeout worker log\n")
    preserved = _disposition_preserved_artifacts(finalized, record)
    row = next(
        item
        for item in legal_operation_controls(
            finalized,
            record,
            context=LifecycleControlProjectionContext(
                allow_completed_disposition=True,
                caller=owner,
            ),
        )
        if item["action"] == action
    )
    assert row["arguments"]["caller"] == owner.model_dump(mode="json")
    result = _public_control(config, row)
    assert result["ok"] is True
    current = store.read()
    assert current is not None and current.doorPublication is not None
    require_lifecycle_operation_dependencies(current)
    assert result["lifecycleOperation"]["generation"] == 1
    assert current.generationDisposition == ("retired" if action == "retire" else "superseded")
    # retire is one accepted store mutation, supersede two (door-intent update
    # then door-locked completion): the journal advances exactly once per write.
    assert current.recordRevision == record.recordRevision + (1 if action == "retire" else 2)
    # CCR-R15: disposition and door-boundary changes are meaningful state, so the
    # wait cursor advances exactly once per accepted mutation as well.
    assert current.meaningfulRevision == record.meaningfulRevision + (
        1 if action == "retire" else 2
    )
    observed_contract = load_contract(finalized.contract_path)
    assert _disposition_preserved_artifacts(observed_contract, current) == preserved
    if action == "supersede":
        assert current.dependencies != record.dependencies
        assert record.doorPublication in current.doorPublicationHistory
    else:
        assert current.dependencies == record.dependencies
        assert current.doorPublication == record.doorPublication
        assert current.doorPublicationHistory == record.doorPublicationHistory


def test_sprint_orchestrator_status_payload_executes_public_disposition(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    _operation_input, store, finalized = _publish_mutated_code_generation(contract)
    record = store.read()
    assert record is not None
    owner = _sprint_owner(finalized)
    assert completed_disposition_authorized(finalized, owner)
    config = load_config(Path(record.input.configPath))
    with mock.patch(
        "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
        return_value=WorktreeCommandResult(
            0,
            {
                "contract_path": finalized.contract_path.as_posix(),
                "task_name": finalized.task_name,
            },
        ),
    ):
        status = worktree_status_tool(
            config,
            TaskRef(
                repo_id=finalized.repo_name,
                contract_path=finalized.contract_path.as_posix(),
            ),
            caller=owner,
        )
    closeout = next(item for item in status["lifecycleOperations"] if item["kind"] == "closeout")
    retire = next(item for item in closeout["legalControls"] if item["action"] == "retire")
    assert retire["arguments"]["caller"] == owner.model_dump(mode="json")
    result = _public_control(config, retire)
    assert result["ok"] is True
    terminal = store.read()
    assert terminal is not None and terminal.generationDisposition == "retired"


def test_completed_disposition_is_not_advertised_or_executable_by_leaf(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    _operation_input, store, finalized = _publish_mutated_code_generation(contract)
    record = store.read()
    assert record is not None
    _standalone_owner(finalized)
    leaf = DeclaredCaller(
        role="worker",
        task_document_ref=TaskDocumentRef(
            repository=finalized.repo_name,
            path=f"{finalized.task_name}/task.json",
        ),
    )
    assert not completed_disposition_authorized(finalized, leaf)
    controls = legal_operation_controls(
        finalized,
        record,
        context=LifecycleControlProjectionContext(caller=leaf),
    )
    assert [row["action"] for row in controls] == ["integrate"]
    config = load_config(Path(record.input.configPath))
    refused = worktree_operation_control_tool(
        config,
        OperationControlRequest(
            contract_path=finalized.contract_path.as_posix(),
            operation_kind="closeout",
            action="retire",
            expected_generation=record.generation,
            intent_note="leaf attempts unauthorized retirement",
            caller=leaf,
        ),
    )
    assert refused["ok"] is False
    assert refused["status"] == "lifecycle-disposition-caller-unauthorized"
    assert store.read() == record


def test_status_keeps_completed_closeout_actionable_beside_newer_cancelled_integrate(
    tmp_path: Path,
) -> None:
    contract = _integration_source_ready_contract(_contract(tmp_path, selected_profile=True))
    _operation_input, _closeout_store, finalized = _publish_mutated_code_generation(contract)
    owner = _sprint_owner(finalized)
    start_or_observe_operation(
        IntegrateOperationInput(
            configPath=(tmp_path / "settings.json").as_posix(),
            contractPath=finalized.contract_path.as_posix(),
        ),
        finalized,
        launcher=lambda *_: None,
    )
    integrate_store = LifecycleOperationStore(
        operation_record_path(finalized.worktree_group, "integrate")
    )
    integrate = integrate_store.read()
    assert integrate is not None
    control_operation(_command(finalized, integrate, "cancel"))
    config = load_config(tmp_path / "settings.json")
    with mock.patch(
        "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
        return_value=WorktreeCommandResult(
            0,
            {
                "contract_path": finalized.contract_path.as_posix(),
                "task_name": finalized.task_name,
            },
        ),
    ):
        status = worktree_status_tool(
            config,
            TaskRef(
                repo_id=finalized.repo_name,
                contract_path=finalized.contract_path.as_posix(),
            ),
            caller=owner,
        )
    assert "lifecycleOperation" not in status
    operations = {row["kind"]: row for row in status["lifecycleOperations"]}
    assert set(operations) == {"closeout", "integrate"}
    closeout_actions = [row["action"] for row in operations["closeout"]["legalControls"]]
    assert closeout_actions == ["integrate", "retire", "supersede"]
    for row in operations["closeout"]["legalControls"]:
        if row["tool"] == "worktree_operation_control":
            assert row["arguments"]["caller"] == owner.model_dump(mode="json")


@pytest.mark.parametrize("after_write", [False, True])
def test_public_supersede_recovers_before_and_after_contract_publication(
    tmp_path: Path,
    after_write: bool,
) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    _operation_input, store, finalized = _publish_mutated_code_generation(contract)
    record = store.read()
    assert record is not None
    owner = _sprint_owner(finalized)
    config = load_config(Path(record.input.configPath))
    row = next(
        item
        for item in legal_operation_controls(
            finalized,
            record,
            context=LifecycleControlProjectionContext(
                allow_completed_disposition=True,
                caller=owner,
            ),
        )
        if item["action"] == "supersede"
    )
    original = closeout_door.write_contract
    calls = 0

    def interrupted(path, updated):
        nonlocal calls
        calls += 1
        if after_write:
            original(path, updated)
        raise RuntimeError("forced disposition publication cut")

    with mock.patch.object(closeout_door, "write_contract", side_effect=interrupted):
        cut = _public_control(config, row)
    if after_write:
        assert cut["ok"] is True
        completed = cut
    else:
        assert cut["ok"] is False
        assert cut["status"] == "closeout-door-publication-interrupted"
        assert cut["nextTool"] == "worktree_operation_control"
        assert cut["nextArgs"]["caller"] == owner.model_dump(mode="json")
        pending = store.read()
        assert pending is not None
        with mock.patch(
            "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
            return_value=WorktreeCommandResult(
                0,
                {
                    "contract_path": finalized.contract_path.as_posix(),
                    "task_name": finalized.task_name,
                },
            ),
        ):
            public_status = worktree_status_tool(
                config,
                TaskRef(
                    repo_id=finalized.repo_name,
                    contract_path=finalized.contract_path.as_posix(),
                ),
            )
        projected = next(
            item for item in public_status["lifecycleOperations"] if item["kind"] == "closeout"
        )
        assert not {item["action"] for item in projected["legalControls"]} & {
            "retire",
            "supersede",
        }
        completed = worktree_operation_control_tool(
            config,
            OperationControlRequest(**cut["nextArgs"]),
        )
    assert completed["ok"] is True
    terminal = store.read()
    assert terminal is not None and terminal.doorPublication is not None
    assert terminal.generationDisposition == "superseded"
    assert terminal.doorPublication.state == "proven"
    assert terminal.doorPublication.generation.disposition == "waiting"


def test_supersede_exact_replay_converges_and_competing_declaration_refuses(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    _operation_input, store, finalized = _publish_mutated_code_generation(contract)
    record = store.read()
    assert record is not None
    owner = _sprint_owner(finalized)
    config = load_config(Path(record.input.configPath))
    row = next(
        item
        for item in legal_operation_controls(
            finalized,
            record,
            context=LifecycleControlProjectionContext(
                allow_completed_disposition=True,
                caller=owner,
            ),
        )
        if item["action"] == "supersede"
    )

    first = _public_control(config, row)
    accepted = store.read()
    accepted_door = load_contract(finalized.contract_path).closeout_door
    assert first["ok"] is True
    assert accepted is not None and accepted.supersedeDeclarationFingerprint is not None
    assert accepted_door is not None and accepted_door.disposition == "waiting"

    replay = _public_control(config, row)
    assert replay["ok"] is True
    assert store.read() == accepted
    assert load_contract(finalized.contract_path).closeout_door == accepted_door

    competing = deepcopy(row)
    competing["arguments"]["grade"] = {
        "priority": "critical",
        "judgmentId": "competing-supersede-declaration",
    }
    refused = _public_control(config, competing)
    assert refused["ok"] is False
    assert refused["status"] == "lifecycle-supersede-declaration-conflict"
    assert store.read() == accepted
    assert load_contract(finalized.contract_path).closeout_door == accepted_door


def test_completed_unintegrated_supersede_dry_run_previews_would_supersede(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    _operation_input, store, finalized = _publish_mutated_code_generation(contract)
    record = store.read()
    assert record is not None
    owner = _sprint_owner(finalized)
    assert completed_disposition_authorized(finalized, owner)
    config = load_config(Path(record.input.configPath))
    row = next(
        item
        for item in legal_operation_controls(
            finalized,
            record,
            context=LifecycleControlProjectionContext(
                allow_completed_disposition=True,
                caller=owner,
            ),
        )
        if item["action"] == "supersede"
    )
    preview_arguments = {**row["arguments"], "dry_run": True}
    previewed = worktree_operation_control_tool(
        config,
        OperationControlRequest(**preview_arguments),
    )
    assert previewed["ok"] is True
    assert previewed["lifecycleOperation"]["result"]["state"] == "would-supersede"
    # A dry-run supersede preview leaves the completed journal untouched.
    assert store.read() == record
