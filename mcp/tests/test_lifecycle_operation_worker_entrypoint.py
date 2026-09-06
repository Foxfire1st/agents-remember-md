"""Task-addressed lifecycle worker parser and process entrypoint."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember_test_support.testing.global_state import preserve_owned_mutable_state
from closeout_input_test_support import start_closeout_operation
from selected_lifecycle_test_support import selected_closeout_operation_input
from test_lifecycle_operations import _contract


def test_worker_parser_main_and_script_entry_use_task_addressing(tmp_path: Path) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    worker_lease = "a" * 64
    parsed = lifecycle_operation_worker.build_parser().parse_args(
        [
            "--contract-path",
            contract.contract_path.as_posix(),
            "--kind",
            "closeout",
            "--worker-lease",
            worker_lease,
        ]
    )
    assert parsed.contract_path == contract.contract_path

    services = Mock()
    entry_order: list[str] = []
    with (
        patch.object(
            lifecycle_operation_worker,
            "declare_lifecycle_operation_process",
            side_effect=lambda: entry_order.append("declare"),
        ) as declare_operation,
        patch.object(
            lifecycle_operation_worker,
            "build_default_worktree_services",
            side_effect=lambda: (entry_order.append("build"), services)[1],
        ) as build_services,
        patch.object(lifecycle_operation_worker, "bind_worktree_services") as bind_services,
        patch.object(lifecycle_operation_worker, "run_worker", return_value=7) as run,
    ):
        assert (
            lifecycle_operation_worker.main(
                [
                    "--contract-path",
                    contract.contract_path.as_posix(),
                    "--kind",
                    "closeout",
                    "--worker-lease",
                    worker_lease,
                ]
            )
            == 7
        )
    declare_operation.assert_called_once_with()
    assert entry_order == ["declare", "build"]
    build_services.assert_called_once_with()
    bind_services.assert_called_once_with(services)
    run.assert_called_once_with(contract.contract_path, "closeout", worker_lease)

    start_closeout_operation(
        selected_closeout_operation_input(contract, code="close L23"), launcher=lambda *_: None
    )
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    store.update(
        lambda record: record.model_copy(update={"status": "cancelled", "phase": "cancelled"})
    )
    argv = [
        "lifecycle_operation_worker.py",
        "--contract-path",
        contract.contract_path.as_posix(),
        "--kind",
        "closeout",
        "--worker-lease",
        worker_lease,
    ]
    with (
        preserve_owned_mutable_state(),
        patch.object(sys, "argv", argv),
        pytest.raises(SystemExit) as exited,
    ):
        runpy.run_path(
            Path(lifecycle_operation_worker.__file__).as_posix(),
            run_name="__main__",
        )
    assert exited.value.code == 0
