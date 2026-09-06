"""Public forcing for L2 task-addressed lifecycle controls and dispositions."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.worktree_tools import (
    OperationControlRequest,
    worktree_integrate_tool,
    worktree_operation_control_tool,
    worktree_status_tool,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.closeout.input import CloseoutMessageInput
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    GatePolicyRuleSnapshot,
    LifecycleOperationRecord,
)
from agents_remember.models.lifecycles.operation_kinds import LifecycleControlAction
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.models.task_intent import MissingTaskIntent
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operation_controls as controls_module,
)
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operation_recovery as recovery_module,
)
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operations as operations_module,
)
from agents_remember.worktrees.integration.lifecycle.control import (
    cancellation as cancellation_module,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls import (
    LifecycleControlCommand,
    control_operation,
    legal_operation_controls,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    require_matching_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.worker.state import (
    reconcile_worker_exit,
)
from agents_remember.worktrees.integration.lifecycle.worker.termination import (
    worker_process_fingerprint,
)
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.worktree_contract import load_contract, write_contract
from closeout_input_test_support import start_closeout_operation
from lifecycle_control_test_support import publish_completed_disposition_task_authority
from selected_lifecycle_test_support import selected_closeout_operation_input
from test_closeout_generation_boundary import _publish_mutated_code_generation
from test_lifecycle_operations import _contract, _publish_integration_branch_authority


def _command(
    contract,
    record,
    action: LifecycleControlAction,
    **values,
) -> LifecycleControlCommand:
    return LifecycleControlCommand(
        admitted_contract=contract,
        admitted_location=require_matching_lifecycle_operation_location(contract),
        configured_authority=record.input.configPath,
        kind=record.operationKind,
        action=action,
        expected_generation=record.generation,
        intent_note=values.pop("intent_note", f"exercise {action} for exact generation"),
        **values,
    )


def _dirty_closeout(tmp_path: Path):
    contract = _contract(tmp_path, selected_profile=True)
    (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    operation_input = selected_closeout_operation_input(contract, code="close first intent")
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    record = store.read()
    assert record is not None
    return contract, operation_input, store, record


def _integration_source_ready_contract(contract):
    _publish_integration_branch_authority(contract)
    configured = replace(load_contract(contract.contract_path), code_source_branch="super")
    write_contract(configured.contract_path, configured)
    return configured


def _public_control(config, row: dict) -> dict:
    assert row["tool"] == "worktree_operation_control"
    return worktree_operation_control_tool(
        config,
        OperationControlRequest(**row["arguments"]),
    )


def _byte_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _standalone_owner(contract) -> DeclaredCaller:
    return publish_completed_disposition_task_authority(
        contract,
        sprint_owned=False,
    )


def test_retry_preserves_generation_input_candidate_and_approval(tmp_path: Path) -> None:
    contract, operation_input, store, record = _dirty_closeout(tmp_path)
    store.update(
        lambda current: current.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": "2026-08-22T10:00:00+00:00",
            }
        )
    )
    accepted = store.read()
    assert accepted is not None
    retry = next(
        row for row in legal_operation_controls(contract, accepted) if row["action"] == "retry"
    )
    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls.launch_detached_worker"
    ) as launch:
        result = _public_control(load_config(Path(accepted.input.configPath)), retry)
    current = store.read()
    assert current is not None
    assert result["ok"] is True
    assert (
        result["lifecycleOperation"]["generation"] == current.generation == record.generation == 1
    )
    assert current.attempt == 2
    assert current.input == operation_input
    assert current.fingerprint == accepted.fingerprint
    assert current.candidateState == accepted.candidateState
    assert current.candidateTree == accepted.candidateTree
    launch.assert_called_once()


def test_public_control_refuses_stale_recover_for_legacy_missing_intent(
    tmp_path: Path,
) -> None:
    contract, _operation_input, store, record = _dirty_closeout(tmp_path)
    recover = next(
        row for row in legal_operation_controls(contract, record) if row["action"] == "recover"
    )
    publication = record.doorPublication
    assert publication is not None
    legacy_record = record.model_copy(
        update={
            "taskIntent": MissingTaskIntent(),
            "doorPublication": publication.model_copy(
                update={
                    "generation": publication.generation.model_copy(
                        update={"taskIntent": MissingTaskIntent()}
                    )
                }
            ),
        }
    )
    store.path.write_text(
        json.dumps(legacy_record.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    legacy = store.read()
    assert legacy is not None
    config = load_config(Path(record.input.configPath))

    with mock.patch.object(controls_module, "launch_detached_worker") as launch:
        refused = _public_control(config, recover)

    assert refused["ok"] is False
    assert refused["status"] == "lifecycle-control-not-legal"
    assert refused["nextAction"] != "recover"
    assert store.read() == legacy
    launch.assert_not_called()


@pytest.mark.parametrize("after_contract_write", [False, True])
def test_initial_claimed_door_crash_recovers_before_worker_launch(
    tmp_path: Path,
    after_contract_write: bool,
) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    operation_input = selected_closeout_operation_input(contract, code="publish claimed door first")
    original_publish = operations_module.publish_door_intent

    def cut_publication(path, intent):
        if after_contract_write:
            original_publish(path, intent)
        raise SystemExit("initial claimed-door publication cut")

    first_launch = mock.Mock()
    with (
        mock.patch.object(
            operations_module,
            "publish_door_intent",
            side_effect=cut_publication,
        ),
        pytest.raises(SystemExit, match="claimed-door publication cut"),
    ):
        start_closeout_operation(operation_input, launcher=first_launch)
    first_launch.assert_not_called()
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    interrupted = store.read()
    assert interrupted is not None and interrupted.doorPublication is not None
    assert interrupted.doorPublication.state == "intent"
    observed_contract = load_contract(contract.contract_path)
    assert observed_contract.closeout_door is not None
    if after_contract_write:
        assert observed_contract.closeout_door == interrupted.doorPublication.generation
    else:
        assert observed_contract.closeout_door.disposition == "waiting"
        assert (
            observed_contract.closeout_door.generationId
            == interrupted.doorPublication.generation.generationId
        )

    config = load_config(Path(interrupted.input.configPath))
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
        status = worktree_status_tool(
            config,
            TaskRef(repo_id=contract.repo_name, contract_path=contract.contract_path.as_posix()),
        )
    projected = next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")
    assert [row["action"] for row in projected["legalControls"]] == ["recover"]
    with mock.patch.object(controls_module, "launch_detached_worker") as recovery_launch:
        recovered = _public_control(config, projected["legalControls"][0])
    assert recovered["ok"] is True
    proven = store.read()
    assert proven is not None and proven.doorPublication is not None
    assert proven.doorPublication.state == "proven"
    assert load_contract(contract.contract_path).closeout_door == proven.doorPublication.generation
    recovery_launch.assert_called_once()


def test_initial_claimed_door_journal_create_cut_recovers_before_first_launch(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    operation_input = selected_closeout_operation_input(contract, code="publish claimed door first")
    original_create = LifecycleOperationStore.create

    def cut_after_record_create(
        target: LifecycleOperationStore,
        candidate: LifecycleOperationRecord,
        **options,
    ) -> tuple[LifecycleOperationRecord, bool]:
        created = original_create(target, candidate, **options)
        if target.path == operation_record_path(contract.worktree_group, "closeout"):
            raise SystemExit("cut after accepted journal create")
        return created

    first_launch = mock.Mock()
    with (
        mock.patch.object(
            LifecycleOperationStore,
            "create",
            new=cut_after_record_create,
        ),
        pytest.raises(SystemExit, match="accepted journal create"),
    ):
        start_closeout_operation(operation_input, launcher=first_launch)
    first_launch.assert_not_called()
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    interrupted = store.read()
    assert interrupted is not None
    assert (interrupted.generation, interrupted.attempt, interrupted.status) == (1, 1, "queued")
    assert interrupted.workerPid is None
    assert interrupted.doorPublication is not None
    assert interrupted.doorPublication.state == "intent"
    waiting = load_contract(contract.contract_path).closeout_door
    assert waiting is not None
    assert waiting.disposition == "waiting"
    assert waiting.generationId == interrupted.doorPublication.generation.generationId

    config = load_config(Path(interrupted.input.configPath))
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
        status = worktree_status_tool(
            config,
            TaskRef(repo_id=contract.repo_name, contract_path=contract.contract_path.as_posix()),
        )
    projected = next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")
    recover = projected["legalControls"]
    assert [row["action"] for row in recover] == ["recover"]
    assert recover[0]["arguments"]["expected_generation"] == 1
    with mock.patch.object(controls_module, "launch_detached_worker") as launch:
        recovered = _public_control(config, recover[0])
    assert recovered["ok"] is True
    proven = store.read()
    assert proven is not None and proven.doorPublication is not None
    assert proven.doorPublication.state == "proven"
    assert load_contract(contract.contract_path).closeout_door == proven.doorPublication.generation
    launch.assert_called_once()


def test_initial_door_without_journal_intent_is_exact_public_decision(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    operation_input = selected_closeout_operation_input(
        contract, code="reject unjournaled live door"
    )
    start_closeout_operation(operation_input, launcher=mock.Mock())
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    accepted = store.read()
    assert accepted is not None and accepted.doorPublication is not None
    malformed = accepted.model_copy(update={"doorPublication": None})
    store.path.write_text(malformed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    interrupted = store.read()
    assert interrupted is not None and interrupted.doorPublication is None
    live = load_contract(contract.contract_path)
    unjournaled = live.closeout_door
    assert unjournaled is not None and unjournaled.disposition == "claimed"
    stale_recover = {
        "tool": "worktree_operation_control",
        "arguments": {
            "contract_path": live.contract_path.as_posix(),
            "operation_kind": "closeout",
            "action": "recover",
            "expected_generation": interrupted.generation,
            "intent_note": "probe malformed create-time door intent",
        },
    }
    journal_before = store.path.read_bytes()
    config = load_config(Path(interrupted.input.configPath))
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
        status = worktree_status_tool(
            config,
            TaskRef(repo_id=contract.repo_name, contract_path=contract.contract_path.as_posix()),
        )
    projected = next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")
    assert projected["legalControls"] == []
    assert projected["result"]["state"] == "closeout-initial-door-intent-missing"
    assert projected["result"]["nextAction"] == "developer-decision"
    public_door = projected["result"]["observed"]["contractDoor"]
    assert public_door["generationId"] == unjournaled.generationId
    assert public_door["operationKind"] == "closeout"
    assert public_door["operationIdentityDigests"]
    assert "claimedOperationKey" not in public_door
    with mock.patch.object(controls_module, "launch_detached_worker") as launch:
        refused = _public_control(config, stale_recover)
    assert refused["ok"] is False
    assert refused["status"] == "closeout-initial-door-intent-missing"
    assert refused["nextAction"] == "developer-decision"
    assert refused["expected"] == projected["result"]["expected"]
    assert refused["observed"] == projected["result"]["observed"]
    assert store.path.read_bytes() == journal_before
    launch.assert_not_called()


def test_advertised_integrate_control_starts_task_addressed_generation(
    tmp_path: Path,
) -> None:
    contract = _integration_source_ready_contract(_contract(tmp_path, selected_profile=True))
    _operation_input, closeout_store, finalized = _publish_mutated_code_generation(contract)
    completed = closeout_store.read()
    assert completed is not None
    assert completed.doorPublication is not None
    assert completed.doorPublication.state == "proven"
    assert completed.doorPublication.generation.disposition == "claimed"
    assert completed.closeoutFinalizedContractSha256 == closeout_contract_sha256(finalized)
    config = load_config(Path(completed.input.configPath))
    refused = worktree_operation_control_tool(
        config,
        OperationControlRequest(
            contract_path=finalized.contract_path.as_posix(),
            operation_kind="closeout",
            action="cancel",
            expected_generation=completed.generation,
            intent_note="completed closeout must route to integration",
        ),
    )
    assert refused["ok"] is False
    assert refused["nextTool"] == "worktree_integrate"
    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.lifecycle_operations.launch_detached_worker"
    ) as launch:
        result = worktree_integrate_tool(config, **refused["nextArgs"])
    assert result["ok"] is True
    integrate = LifecycleOperationStore(
        operation_record_path(finalized.worktree_group, "integrate")
    ).read()
    assert integrate is not None
    assert (integrate.generation, integrate.status) == (1, "queued")
    launch.assert_called_once()


def test_cancel_dry_run_and_revise_only_advertise_the_waiting_successor(
    tmp_path: Path,
) -> None:
    contract, _operation_input, store, record = _dirty_closeout(tmp_path)
    cancelled_record = _cancelled_closeout_generation(tmp_path, contract, store, record)
    revise = _revision_command(contract, cancelled_record)
    before_preview = _byte_tree(tmp_path)
    dry = control_operation(replace(revise, dry_run=True))
    assert dry.generation == 1
    assert dry.result is not None and dry.result["state"] == "would-revise"
    assert before_preview == _byte_tree(tmp_path)
    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls.launch_detached_worker"
    ) as launch:
        ready = control_operation(revise)
    current = store.read()
    assert current is not None and current.doorPublication is not None
    assert ready.generation == current.generation == 1
    assert ready.result is not None and ready.result["state"] == "revision-ready"
    assert ready.result["nextTool"] == "worktree_closeout_apply"
    assert current.doorPublication.generation.predecessorGenerationId
    launch.assert_not_called()


def _cancelled_closeout_generation(tmp_path, contract, store, record):
    claimed_publication = record.doorPublication
    assert claimed_publication is not None
    assert claimed_publication.state == "proven"
    assert claimed_publication.generation.disposition == "claimed"
    before = _byte_tree(tmp_path)
    preview = control_operation(_command(contract, record, "cancel", dry_run=True))
    assert preview.generation == 1
    assert _byte_tree(tmp_path) == before
    cancelled = control_operation(_command(contract, record, "cancel"))
    assert cancelled.status == "cancelled"
    cancelled_record = store.read()
    assert cancelled_record is not None
    assert cancelled_record.doorPublication is not None
    assert cancelled_record.doorPublication.state == "proven"
    assert cancelled_record.doorPublication.generation.disposition == "waiting"
    assert cancelled_record.doorPublicationHistory[-1] == claimed_publication
    assert cancelled_record.cancellationEvidence is not None
    return cancelled_record


def _revision_command(contract, cancelled_record):
    return _command(
        load_contract(contract.contract_path),
        cancelled_record,
        "revise",
        revision_messages=CloseoutMessageInput(code="close revised exact intent"),
        revision_gate_policy=[
            GatePolicyRuleSnapshot(kind="code-review", requireReviewerVerdict=True)
        ],
        intent_note="approve revised exact candidate",
    )


def test_public_active_revise_returns_executable_apply_arguments_without_launching(
    tmp_path: Path,
) -> None:
    contract, _operation_input, store, record = _dirty_closeout(tmp_path)
    config = load_config(Path(record.input.configPath))
    revise = next(
        row for row in legal_operation_controls(contract, record) if row["action"] == "revise"
    )
    revise["arguments"]["code_commit_message"] = "fresh direct revision"
    with mock.patch.object(controls_module, "launch_detached_worker") as launch:
        ready = _public_control(config, revise)
    assert ready["ok"] is True
    current = store.read()
    assert current is not None
    assert current.generation == record.generation == 1
    assert current.status == "cancelled"
    assert ready["lifecycleOperation"]["result"]["state"] == "revision-ready"
    assert ready["lifecycleOperation"]["result"]["nextTool"] == "worktree_closeout_apply"
    launch.assert_not_called()


@pytest.mark.parametrize(
    ("revision_case", "expected_status"),
    (("blank", "closeout-input-invalid"), ("unchanged", "lifecycle-revision-unchanged")),
)
def test_refused_active_revise_leaves_safe_cancelled_and_executable_revise(
    tmp_path: Path,
    revision_case: str,
    expected_status: str,
) -> None:
    contract, _operation_input, store, record = _dirty_closeout(tmp_path)
    config = load_config(Path(record.input.configPath))
    revise = next(
        row for row in legal_operation_controls(contract, record) if row["action"] == "revise"
    )
    if revision_case == "blank":
        revise["arguments"]["code_commit_message"] = ""
    else:
        assert isinstance(record.input, CloseoutOperationInput)
        revise["arguments"]["code_commit_message"] = record.input.effectiveInput.message_for("code")
        revise["arguments"]["intent_note"] = record.input.approvalNote
    with (
        mock.patch.object(controls_module, "launch_detached_worker") as launch,
        mock.patch.object(cancellation_module, "signal_worker_and_prove_exit") as signal_worker,
    ):
        refused = _public_control(config, revise)
    assert refused["ok"] is False
    assert refused["status"] == expected_status
    assert refused["nextTool"] == "worktree_operation_control"
    assert refused["nextArgs"]["action"] == "revise"
    assert refused["nextArgs"]["expected_generation"] == 1
    cancelled = store.read()
    assert cancelled is not None
    assert cancelled.generation == 1
    assert cancelled.status == "cancelled"
    assert cancelled.doorPublication is not None
    assert cancelled.doorPublication.state == "proven"
    assert [
        row["action"]
        for row in legal_operation_controls(
            load_contract(contract.contract_path),
            cancelled,
        )
    ] == ["revise"]
    launch.assert_not_called()
    signal_worker.assert_not_called()
    if revision_case == "unchanged":
        next_args = dict(refused["nextArgs"])
        next_args.update(
            code_commit_message="fresh successor after unchanged refusal",
            intent_note="fresh approval after unchanged refusal",
        )
        with mock.patch.object(controls_module, "launch_detached_worker") as relaunch:
            ready = worktree_operation_control_tool(
                config,
                OperationControlRequest(**next_args),
            )
        assert ready["ok"] is True
        assert ready["lifecycleOperation"]["generation"] == 1
        assert ready["lifecycleOperation"]["result"]["state"] == "revision-ready"
        assert ready["lifecycleOperation"]["result"]["nextTool"] == "worktree_closeout_apply"
        relaunch.assert_not_called()


def test_signal_denial_retains_worker_authority_and_only_advertises_cancel(
    tmp_path: Path,
) -> None:
    contract, _operation_input, store, _record = _dirty_closeout(tmp_path)
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    runtime.start()
    running = _bind_test_worker(store)

    config = load_config(Path(running.input.configPath))
    advertised = legal_operation_controls(contract, running)
    cancel = next(row for row in advertised if row["action"] == "cancel")
    with (
        mock.patch(
            "agents_remember.worktrees.integration.lifecycle.worker.state."
            "observe_worker_termination",
            return_value=None,
        ) as observation,
        mock.patch(
            "agents_remember.worktrees.integration.lifecycle.control.cancellation."
            "signal_worker_and_prove_exit",
            side_effect=_denied_worker_termination,
        ),
    ):
        refused = _public_control(config, cancel)
    observation.assert_called()
    _assert_denied_termination_refusal(observation.call_args.args[0], running, refused)
    retained = store.read()
    _assert_retained_worker_authority(contract, running, retained)
    retained_bytes = store.path.read_bytes()
    with pytest.raises(lifecycle_operation_worker.OperationCancelled):
        runtime.progress(
            "git-mutation",
            {"current_command": "must not run after durable cancel request"},
        )
    runtime.finish({"ok": True}, ok=True)
    runtime.fail(RuntimeError("late worker failure after cancel request"))
    assert store.path.read_bytes() == retained_bytes


def _bind_test_worker(store):
    running = store.update(
        lambda current: current.model_copy(
            update={
                "workerPid": 4242,
                "workerLease": "a" * 64,
                "workerProcessFingerprint": "b" * 64,
            }
        )
    )
    assert running.workerTermination is None
    return running


def _denied_worker_termination(request):
    return WorkerTerminationEvidence(
        state="termination-required",
        pid=request.pid,
        lease=request.lease,
        processFingerprint=request.processFingerprint,
        requestedAt=request.requestedAt,
        detail="PRIVATE_PERMISSION_SENTINEL /tmp/worker stderr",
    )


def _assert_denied_termination_refusal(request, running, refused) -> None:
    assert request.pid == running.workerPid
    assert request.lease == running.workerLease
    assert request.processFingerprint == running.workerProcessFingerprint
    assert refused["ok"] is False
    assert refused["status"] == "worker-termination-required"
    assert refused["nextTool"] == "worktree_operation_control"
    assert refused["nextArgs"]["action"] == "cancel"
    assert "PRIVATE_PERMISSION_SENTINEL" not in str(refused)


def _assert_retained_worker_authority(contract, running, retained) -> None:
    assert retained is not None
    assert retained.status == "termination-required"
    assert retained.workerPid == running.workerPid
    assert retained.workerLease == running.workerLease
    assert retained.workerProcessFingerprint == running.workerProcessFingerprint
    assert retained.workerTermination is not None
    assert retained.workerTermination.state == "termination-required"
    assert "PRIVATE_PERMISSION_SENTINEL" not in retained.workerTermination.detail
    assert retained.workerTermination.failureEvidence is not None
    assert [item["action"] for item in legal_operation_controls(contract, retained)] == ["cancel"]


def test_cancel_preserves_repaired_candidate_after_reconciled_unchanged(tmp_path: Path) -> None:
    contract, _operation_input, store, record = _dirty_closeout(tmp_path)
    code = record.mutationEvidence["code"]
    assert code.acceptedBefore is not None
    intent = code.model_copy(
        update={
            "state": "mutation-intent",
            "before": code.acceptedBefore,
            "expectedOutputTree": code.acceptedBefore.candidateTree,
        }
    )
    store.update(
        lambda current: current.model_copy(
            update={"mutationEvidence": {**current.mutationEvidence, "code": intent}}
        )
    )
    original = recovery_module.reconcile_closeout_mutations

    def reconcile_then_drift(current):
        reconciled = original(current)
        (contract.code_worktree / "after-reconcile.txt").write_text("drift\n", encoding="utf-8")
        return reconciled

    current = store.read()
    assert current is not None
    config = load_config(Path(current.input.configPath))
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
        status = worktree_status_tool(
            config,
            TaskRef(repo_id=contract.repo_name, contract_path=contract.contract_path.as_posix()),
        )
    projected = next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")
    cancel = next(row for row in projected["legalControls"] if row["action"] == "cancel")
    with mock.patch.object(
        recovery_module,
        "reconcile_closeout_mutations",
        side_effect=reconcile_then_drift,
    ):
        cancelled = _public_control(config, cancel)
    assert cancelled["ok"] is True
    assert (contract.code_worktree / "after-reconcile.txt").read_text() == "drift\n"
    retained = store.read()
    assert retained is not None and retained.status == "cancelled"
    assert retained.cancellationEvidence is not None
    facts = retained.cancellationEvidence.observed
    assert facts["codeAcceptedCandidateTree"] != facts["codeObservedCandidateTree"]


def test_cancel_refuses_protected_head_change_after_reconciliation(tmp_path: Path) -> None:
    contract, _operation_input, store, record = _dirty_closeout(tmp_path)
    code = record.mutationEvidence["code"]
    assert code.acceptedBefore is not None
    intent = code.model_copy(
        update={
            "state": "mutation-intent",
            "before": code.acceptedBefore,
            "expectedOutputTree": code.acceptedBefore.candidateTree,
        }
    )
    store.update(
        lambda current: current.model_copy(
            update={"mutationEvidence": {**current.mutationEvidence, "code": intent}}
        )
    )
    original = recovery_module.reconcile_closeout_mutations

    def reconcile_then_commit(current):
        reconciled = original(current)
        (contract.code_worktree / "unattributed.txt").write_text("commit\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=contract.code_worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "unattributed concurrent output"],
            cwd=contract.code_worktree,
            check=True,
            capture_output=True,
        )
        return reconciled

    current = store.read()
    assert current is not None
    config = load_config(Path(current.input.configPath))
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
        status = worktree_status_tool(
            config,
            TaskRef(repo_id=contract.repo_name, contract_path=contract.contract_path.as_posix()),
        )
    projected = next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")
    cancel = next(row for row in projected["legalControls"] if row["action"] == "cancel")
    with mock.patch.object(
        recovery_module,
        "reconcile_closeout_mutations",
        side_effect=reconcile_then_commit,
    ):
        refused = _public_control(config, cancel)
    assert refused["ok"] is False
    assert refused["status"] == "lifecycle-cancellation-git-changed"
    assert refused["nextAction"] == "developer-decision"
    assert refused["expected"]["leg"] == "code"
    assert refused["observed"]["leg"] == "code"
    assert refused["expected"]["head"] != refused["observed"]["head"]


def test_public_cancel_proves_group_exit_after_leader_exits(tmp_path: Path) -> None:
    contract, _operation_input, store, _record = _dirty_closeout(tmp_path)
    script = (
        "import subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "sys.stdin.read(1)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert leader.stdout is not None
        child_pid = int(leader.stdout.readline().strip())
        fingerprint = worker_process_fingerprint(leader.pid)
        assert fingerprint is not None
        store.update(
            lambda current: current.model_copy(
                update={
                    "status": "running",
                    "phase": "preflight",
                    "workerPid": leader.pid,
                    "workerLease": "c" * 64,
                    "workerProcessFingerprint": fingerprint,
                }
            )
        )
        assert leader.stdin is not None
        leader.stdin.close()
        leader.wait(timeout=5)
        assert Path(f"/proc/{child_pid}").exists()
        running = store.read()
        assert running is not None
        cancel = next(
            row for row in legal_operation_controls(contract, running) if row["action"] == "cancel"
        )
        result = _public_control(load_config(Path(running.input.configPath)), cancel)
        assert result["ok"] is True
        terminal = store.read()
        assert terminal is not None and terminal.workerTermination is not None
        assert terminal.status == "cancelled"
        assert terminal.workerPid is None
        assert terminal.workerTermination.state == "exited"
    finally:
        if leader.stdout is not None:
            leader.stdout.close()
        with suppress(ProcessLookupError):
            os.killpg(leader.pid, signal.SIGKILL)
        if leader.poll() is None:
            leader.kill()
            leader.wait(timeout=5)


def test_completed_worker_exit_reconciliation_preserves_terminal_outcome(
    tmp_path: Path,
) -> None:
    _contract_value, _operation_input, store, _record = _dirty_closeout(tmp_path)
    store.update(
        lambda current: current.model_copy(
            update={
                "status": "running",
                "phase": "preflight",
                "workerPid": 4242,
                "workerLease": "d" * 64,
                "workerProcessFingerprint": "e" * 64,
            }
        )
    )
    lifecycle_operation_worker.OperationRuntime(store).finish({"ok": True}, ok=True)
    completed = store.read()
    assert completed is not None and completed.status == "completed"

    def exited(request):
        return request.model_copy(
            update={
                "state": "exited",
                "signal": "none",
                "observedAt": "2026-08-22T12:00:00+00:00",
                "detail": "exact worker process group exited",
            }
        )

    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.worker.state.observe_worker_termination",
        side_effect=exited,
    ):
        reconciled = reconcile_worker_exit(store)
    assert reconciled is not None
    assert (reconciled.status, reconciled.phase) == ("completed", "completed")
    assert reconciled.workerPid is None
    assert reconciled.workerLease is None
    assert reconciled.workerProcessFingerprint is None
    assert reconciled.workerTermination is not None
    assert reconciled.workerTermination.signal == "none"


def test_status_advertised_integrate_persists_natural_closeout_exit_on_execution(
    tmp_path: Path,
) -> None:
    contract = _integration_source_ready_contract(_contract(tmp_path, selected_profile=True))
    _operation_input, store, finalized = _publish_mutated_code_generation(contract)
    store.update(
        lambda current: current.model_copy(
            update={
                "workerPid": 4242,
                "workerLease": "4" * 64,
                "workerProcessFingerprint": "5" * 64,
            }
        )
    )
    record = store.read()
    assert record is not None
    assert record.doorPublication is not None
    assert record.doorPublication.state == "proven"
    assert record.doorPublication.generation.disposition == "claimed"
    assert record.closeoutFinalizedContractSha256 == closeout_contract_sha256(finalized)
    config = load_config(Path(record.input.configPath))

    def exited(request):
        return request.model_copy(
            update={
                "state": "exited",
                "observedAt": "2026-08-22T14:00:00+00:00",
                "detail": "exact completed worker exited",
            }
        )

    with (
        mock.patch(
            "agents_remember.worktrees.integration.lifecycle.worker.state."
            "observe_worker_termination",
            side_effect=exited,
        ),
        mock.patch(
            "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
            return_value=WorktreeCommandResult(
                0,
                {
                    "contract_path": finalized.contract_path.as_posix(),
                    "task_name": finalized.task_name,
                },
            ),
        ),
    ):
        status = worktree_status_tool(
            config,
            TaskRef(
                repo_id=finalized.repo_name,
                contract_path=finalized.contract_path.as_posix(),
            ),
        )
        closeout = next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")
        integrate = next(
            row for row in closeout["legalControls"] if row["tool"] == "worktree_integrate"
        )
        with mock.patch(
            "agents_remember.worktrees.integration.lifecycle.lifecycle_operations.launch_detached_worker"
        ) as launch:
            started = worktree_integrate_tool(config, **integrate["arguments"])
    assert started["ok"] is True
    durable = store.read()
    assert durable is not None
    assert durable.status == "completed"
    assert durable.workerPid is None
    launch.assert_called_once()


@pytest.mark.parametrize("action", ["retry", "revise"])
def test_replacement_is_blocked_until_exact_worker_exit_proof(
    tmp_path: Path,
    action: LifecycleControlAction,
) -> None:
    contract, _operation_input, store, _record = _dirty_closeout(tmp_path)
    store.update(
        lambda current: current.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": "2026-08-22T12:00:00+00:00",
                "workerPid": 4242,
                "workerLease": "1" * 64,
                "workerProcessFingerprint": "2" * 64,
            }
        )
    )
    retained = store.read()
    assert retained is not None
    config = load_config(Path(retained.input.configPath))
    request = OperationControlRequest(
        contract_path=contract.contract_path.as_posix(),
        operation_kind="closeout",
        action=action,
        expected_generation=retained.generation,
        intent_note="replacement must wait for worker exit proof",
        code_commit_message="fresh replacement" if action == "revise" else None,
    )
    with (
        mock.patch(
            "agents_remember.worktrees.integration.lifecycle.worker.state."
            "observe_worker_termination",
            return_value=None,
        ) as observation,
        mock.patch(
            "agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls."
            "launch_detached_worker"
        ) as launch,
    ):
        refused = worktree_operation_control_tool(config, request)
    observation.assert_called()
    observed_request = observation.call_args.args[0]
    assert observed_request.pid == retained.workerPid
    assert observed_request.lease == retained.workerLease
    assert observed_request.processFingerprint == retained.workerProcessFingerprint
    assert refused["ok"] is False
    assert refused["status"] == "lifecycle-control-not-legal"
    assert refused["nextArgs"]["action"] == "cancel"
    launch.assert_not_called()
    current = store.read()
    assert current is not None
    assert current.workerPid == retained.workerPid
    assert current.workerLease == retained.workerLease


def test_status_projects_natural_worker_exit_without_any_filesystem_write(
    tmp_path: Path,
) -> None:
    contract, _operation_input, store, record = _dirty_closeout(tmp_path)
    code = record.mutationEvidence["code"]
    assert code.acceptedBefore is not None
    intent = code.model_copy(
        update={
            "state": "mutation-intent",
            "before": code.acceptedBefore,
            "expectedOutputTree": code.acceptedBefore.candidateTree,
        }
    )
    store.update(
        lambda current: current.model_copy(
            update={
                "status": "running",
                "phase": "preflight",
                "workerPid": 4242,
                "workerLease": "8" * 64,
                "workerProcessFingerprint": "9" * 64,
                "mutationEvidence": {**current.mutationEvidence, "code": intent},
            }
        )
    )
    before = _byte_tree(tmp_path)
    config = load_config(Path(record.input.configPath))

    def exited(request):
        return request.model_copy(
            update={
                "state": "exited",
                "observedAt": "2026-08-22T13:00:00+00:00",
                "detail": "exact worker process exited naturally",
            }
        )

    with (
        mock.patch(
            "agents_remember.worktrees.integration.lifecycle.worker.state."
            "observe_worker_termination",
            side_effect=exited,
        ),
        mock.patch(
            "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
            return_value=WorktreeCommandResult(
                0,
                {
                    "contract_path": contract.contract_path.as_posix(),
                    "task_name": contract.task_name,
                },
            ),
        ),
    ):
        status = worktree_status_tool(
            config,
            TaskRef(repo_id=contract.repo_name, contract_path=contract.contract_path.as_posix()),
        )
    projected = next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")
    assert projected["status"] == "running"
    assert _byte_tree(tmp_path) == before
    durable = store.read()
    assert durable is not None and durable.workerPid == 4242
