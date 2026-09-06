"""Byte-preserving L2 lifecycle previews across sibling operation authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest import mock

from agents_remember.application.worktree_tools import (
    OperationControlRequest,
    worktree_operation_control_tool,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.operation import IntegrateOperationInput
from agents_remember.worktrees.integration.integration_ref_transaction import (
    IntegrationSources,
)
from agents_remember.worktrees.integration.integration_resolution_handoff import (
    integration_resolution_required,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls import (
    LifecycleControlCommand,
    control_operation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_lease import (
    require_lifecycle_operation_compatible,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    require_matching_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from agents_remember.worktrees.modules.args import WorktreeArgs
from integration_branch_authority_test_support import (
    _authority_fixture,
    _closed_external_leaf_worktrees,
)
from test_closeout_generation_boundary import _publish_mutated_code_generation
from test_lifecycle_operation_controls_l2 import (
    _dirty_closeout,
    _integration_source_ready_contract,
)
from test_lifecycle_operations import _contract


def _byte_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_control_dry_run_projects_dead_sibling_without_publishing_exit(
    tmp_path: Path,
) -> None:
    contract = _integration_source_ready_contract(_contract(tmp_path, selected_profile=True))
    _operation_input, closeout_store, finalized = _publish_mutated_code_generation(contract)
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
    closeout_store.update(
        lambda current: current.model_copy(
            update={
                "workerPid": 4242,
                "workerLease": "a" * 64,
                "workerProcessFingerprint": "b" * 64,
            }
        )
    )
    before = _byte_tree(tmp_path)

    def exited(request):
        return request.model_copy(
            update={
                "state": "exited",
                "observedAt": "2026-08-22T16:30:00+00:00",
                "detail": "dead closeout sibling projected read-only",
            }
        )

    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.worker.state.observe_worker_termination",
        side_effect=exited,
    ):
        preview = control_operation(
            LifecycleControlCommand(
                admitted_contract=finalized,
                admitted_location=require_matching_lifecycle_operation_location(finalized),
                configured_authority=integrate.input.configPath,
                kind="integrate",
                action="cancel",
                expected_generation=integrate.generation,
                intent_note="preview exact integrate cancellation",
                dry_run=True,
            )
        )
    assert preview.generation == integrate.generation
    assert _byte_tree(tmp_path) == before
    durable_closeout = closeout_store.read()
    assert durable_closeout is not None and durable_closeout.workerPid == 4242


def test_only_mutating_compatibility_publishes_proven_terminal_worker_exit(
    tmp_path: Path,
) -> None:
    contract, _operation_input, store, _record = _dirty_closeout(tmp_path)
    store.update(
        lambda current: current.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": "2026-08-23T01:05:00+00:00",
                "workerPid": 4242,
                "workerLease": "a" * 64,
                "workerProcessFingerprint": "b" * 64,
            }
        )
    )
    before = _byte_tree(tmp_path)

    def exited(request):
        return request.model_copy(
            update={
                "state": "exited",
                "observedAt": "2026-08-23T01:06:00+00:00",
                "detail": "exact worker process group exited",
            }
        )

    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.worker.state.observe_worker_termination",
        side_effect=exited,
    ):
        require_lifecycle_operation_compatible(
            contract,
            operation_kind="integrate",
            publish_worker_exits=False,
        )
        assert _byte_tree(tmp_path) == before
        require_lifecycle_operation_compatible(
            contract,
            operation_kind="integrate",
            publish_worker_exits=True,
        )
    durable = store.read()
    assert durable is not None
    assert durable.workerPid is None
    assert durable.workerTermination is not None
    assert durable.workerTermination.state == "exited"


def test_integration_resolution_emits_exact_generation_and_public_controls(
    tmp_path: Path,
) -> None:
    fixture = _authority_fixture(tmp_path, external_memory=True)
    contract = _closed_external_leaf_worktrees(fixture, tmp_path)
    start_or_observe_operation(
        IntegrateOperationInput(
            configPath=fixture.config_path.as_posix(),
            contractPath=contract.contract_path.as_posix(),
        ),
        contract,
        launcher=lambda *_: None,
    )
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "integrate"))
    record = store.read()
    assert record is not None
    sources = IntegrationSources(
        current_code_source=contract.code_base_commit,
        current_memory_source=contract.memory_base_commit,
        code_replay_required=True,
        memory_replay_required=False,
    )
    result = integration_resolution_required(
        contract,
        WorktreeArgs(contract_path=contract.contract_path, strategy="replay"),
        sources,
        record,
    )
    assert result.returncode == 2
    payload = cast(dict[str, Any], result.payload)
    emitted = [
        payload["nextArgs"],
        payload["applyStep"]["nextArgs"],
        *(step["args"] for step in payload["resolutionSteps"]),
        payload["nextStep"]["nextArgs"],
    ]
    assert all(item["expected_generation"] == record.generation > 0 for item in emitted)
    preview = worktree_operation_control_tool(
        load_config(fixture.config_path),
        OperationControlRequest(**cast(dict[str, Any], payload["nextArgs"])),
    )
    assert preview["ok"] is True
    assert store.read() == record
    applied = worktree_operation_control_tool(
        load_config(fixture.config_path),
        OperationControlRequest(**cast(dict[str, Any], payload["applyStep"]["nextArgs"])),
    )
    assert applied["ok"] is True
    terminal = store.read()
    assert terminal is not None and terminal.status == "cancelled"

    planning = integration_resolution_required(
        contract,
        WorktreeArgs(contract_path=contract.contract_path, dry_run=True, strategy="replay"),
        sources,
        None,
    )
    assert planning.payload["state"] == "integration-resolution-planning-required"
    assert "nextTool" not in planning.payload
