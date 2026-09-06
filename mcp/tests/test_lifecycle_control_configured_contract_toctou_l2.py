"""Registered lifecycle control revalidates configured contract authority under lease."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.application import worktree_tools
from agents_remember.application.worktree_tools import (
    OperationControlRequest,
    worktree_operation_control_tool,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.enclosure import LifecycleEnclosureManifest
from agents_remember.models.lifecycles.operation import IntegrateOperationInput
from agents_remember.worktrees.integration import configured_contract_authority as authority_mod
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operation_controls as controls_mod,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    lifecycle_enclosure_manifest_path,
    lifecycle_operation_locator_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from agents_remember.worktrees.worktree_contract import write_contract
from test_closeout_generation_boundary import _publish_mutated_code_generation
from test_lifecycle_operation_controls_l2 import (
    _dirty_closeout,
    _integration_source_ready_contract,
)
from test_lifecycle_operations import _contract
from test_worktree_support import git

pytestmark = pytest.mark.integration

def _request(contract, record, *, dry_run: bool = False) -> OperationControlRequest:
    return OperationControlRequest(
        contract_path=contract.contract_path.as_posix(),
        operation_kind="closeout",
        action="cancel",
        expected_generation=record.generation,
        intent_note="revalidate current configured contract under lifecycle authority",
        dry_run=dry_run,
    )


def _authority_snapshot(contract, store) -> dict[str, object]:
    locator_path = lifecycle_operation_locator_path(
        contract.coordination_root,
        contract.contract_path,
    )
    manifest_path = lifecycle_enclosure_manifest_path(contract.worktree_group)
    return {
        "contract": (
            contract.contract_path.read_bytes() if contract.contract_path.exists() else None
        ),
        "locator": locator_path.read_bytes(),
        "manifest": manifest_path.read_bytes(),
        "journals": {
            path.relative_to(store.path.parent).as_posix(): path.read_bytes()
            for path in store.path.parent.rglob("*")
            if path.is_file()
        },
        "codeHead": git(contract.code_repo_path, "rev-parse", "HEAD"),
    }


def _public_cut(contract, record, store, cut) -> tuple[dict, dict[str, object]]:
    config = load_config(Path(record.input.configPath))
    captured: dict[str, object] = {}
    real_control = controls_mod.control_operation

    def cut_then_control(command):
        context = cut()
        captured.update(_authority_snapshot(contract, store))
        if context is None:
            return real_control(command)
        with context:
            return real_control(command)

    with (
        mock.patch.object(
            worktree_tools,
            "control_operation",
            side_effect=cut_then_control,
        ),
        mock.patch.object(controls_mod, "launch_detached_worker") as launch,
    ):
        result = worktree_operation_control_tool(config, _request(contract, record))
    launch.assert_not_called()
    return result, captured


def _assert_decision_refusal(result: dict, expected_status: str) -> None:
    assert result["ok"] is False
    assert result["status"] == expected_status
    assert result["nextAction"] == "developer-decision"
    assert result["developerDecisionRequired"] is True
    assert "nextTool" not in result
    assert "nextArgs" not in result


def test_unchanged_configured_contract_executes_public_control_preview(tmp_path: Path) -> None:
    contract, _operation_input, store, record = _dirty_closeout(tmp_path)
    config = load_config(Path(record.input.configPath))
    before = _authority_snapshot(contract, store)
    preview = worktree_operation_control_tool(
        config,
        _request(contract, record, dry_run=True),
    )
    assert preview["ok"] is True
    assert preview["lifecycleOperation"]["generation"] == record.generation
    assert _authority_snapshot(contract, store) == before


@pytest.mark.parametrize("cut", ["missing", "unreadable", "repository-mismatch"])
def test_public_control_refuses_contract_drift_before_journal_mutation(
    tmp_path: Path,
    cut: str,
) -> None:
    contract, _operation_input, store, record = _dirty_closeout(tmp_path)

    def drift():
        if cut == "missing":
            contract.contract_path.unlink()
            return None
        if cut == "unreadable":
            return mock.patch.object(
                authority_mod,
                "load_contract",
                side_effect=PermissionError("private unreadable contract sentinel"),
            )
        write_contract(
            contract.contract_path,
            replace(contract, code_repo_path=tmp_path / "foreign-code-repository"),
        )
        return None

    result, after_drift = _public_cut(contract, record, store, drift)
    _assert_decision_refusal(result, "closeout-contract-invalid")
    assert result["observed"]["observed"]["state"] in {
        "missing",
        "unreadable",
        "mismatch",
    }
    assert "private unreadable contract sentinel" not in repr(result)
    assert _authority_snapshot(contract, store) == after_drift


def test_public_control_refuses_fresh_manifest_mismatch_before_journal_mutation(
    tmp_path: Path,
) -> None:
    contract, _operation_input, store, record = _dirty_closeout(tmp_path)
    manifest_path = lifecycle_enclosure_manifest_path(contract.worktree_group)

    def drift():
        manifest = LifecycleEnclosureManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        changed = manifest.model_copy(update={"taskName": "different-task"})
        manifest_path.write_text(
            json.dumps(changed.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )

    result, after_drift = _public_cut(contract, record, store, drift)
    _assert_decision_refusal(result, "operation-location-mismatch")
    assert _authority_snapshot(contract, store) == after_drift


def test_integrate_control_revalidates_after_integration_authority_reload(
    tmp_path: Path,
) -> None:
    contract = _integration_source_ready_contract(_contract(tmp_path, selected_profile=True))
    _closeout_input, _closeout_store, finalized = _publish_mutated_code_generation(contract)
    start_or_observe_operation(
        IntegrateOperationInput(
            configPath=(tmp_path / "settings.json").as_posix(),
            contractPath=finalized.contract_path.as_posix(),
        ),
        finalized,
        launcher=lambda *_: None,
    )
    store = LifecycleOperationStore(operation_record_path(finalized.worktree_group, "integrate"))
    record = store.read()
    assert record is not None
    config = load_config(Path(record.input.configPath))
    real_lock = controls_mod.integration_authority_lock
    after_drift: dict[str, object] = {}

    @contextmanager
    def drift_inside_integration_authority(*args, **kwargs):
        with real_lock(*args, **kwargs):
            write_contract(
                finalized.contract_path,
                replace(finalized, code_repo_path=tmp_path / "foreign-code-repository"),
            )
            after_drift.update(_authority_snapshot(finalized, store))
            yield

    with (
        mock.patch.object(
            controls_mod,
            "integration_authority_lock",
            side_effect=drift_inside_integration_authority,
        ),
        mock.patch.object(controls_mod, "launch_detached_worker") as launch,
    ):
        result = worktree_operation_control_tool(
            config,
            OperationControlRequest(
                contract_path=finalized.contract_path.as_posix(),
                operation_kind="integrate",
                action="cancel",
                expected_generation=record.generation,
                intent_note="revalidate after integration authority reload",
            ),
        )
    _assert_decision_refusal(result, "integrate-contract-invalid")
    assert result["observed"]["observed"]["state"] == "mismatch"
    launch.assert_not_called()
    assert _authority_snapshot(finalized, store) == after_drift
