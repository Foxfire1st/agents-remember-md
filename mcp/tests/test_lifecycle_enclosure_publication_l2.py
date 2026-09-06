"""Forcing for immutable enclosure publication and locator-only operation discovery."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from unittest import mock

import pytest
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.worktree_tools import (
    OperationControlRequest,
    worktree_operation_control_tool,
    worktree_status_tool,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.enclosure import LifecycleEnclosureLocator
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operation_location as location_module,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleOperationLocation,
    LifecycleOperationLocationError,
    lifecycle_operation_locator_path,
    publish_new_lifecycle_operation_location,
    require_matching_lifecycle_operation_location,
    resolve_lifecycle_operation_location,
)
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.worktree_contract import contract_publication_text, write_contract
from closeout_input_test_support import start_closeout_operation
from lifecycle_enclosure_test_support import enclosure_contract
from selected_lifecycle_test_support import selected_closeout_operation_input
from test_lifecycle_operations import _contract


class _PublicationCut(BaseException):
    pass


def test_publication_refuses_invalid_or_identity_conflicting_contract_bytes(
    tmp_path: Path,
) -> None:
    invalid_contract, _text = enclosure_contract(tmp_path / "invalid")
    with pytest.raises(LifecycleOperationLocationError) as invalid:
        publish_new_lifecycle_operation_location(
            invalid_contract,
            contract_text="not a worktree contract\n",
        )
    assert invalid.value.status == "operation-location-invalid"
    assert invalid.value.observed["errorType"]

    requested, _text = enclosure_contract(tmp_path / "mismatch")
    contradictory = replace(requested, repo_name="other-repository")
    with pytest.raises(LifecycleOperationLocationError) as mismatch:
        publish_new_lifecycle_operation_location(
            requested,
            contract_text=contract_publication_text(
                requested.contract_path,
                contradictory,
            ),
        )
    assert mismatch.value.status == "operation-location-mismatch"
    assert mismatch.value.expected["repository"] == "repo"
    assert mismatch.value.observed["repository"] == "other-repository"


@pytest.mark.parametrize("kind", ["leaf", "series"])
@pytest.mark.parametrize(
    "cut",
    [
        "after-reserve",
        "after-manifest-write",
        "after-manifest-readback",
        "after-contract-write",
        "after-addressable-proof",
    ],
)
def test_leaf_and_series_publication_resumes_every_ordered_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    cut: str,
) -> None:
    contract, text = enclosure_contract(tmp_path, kind=kind)  # type: ignore[arg-type]
    locator_path = lifecycle_operation_locator_path(
        contract.coordination_root,
        contract.contract_path,
    )
    original_read = location_module._read_bytes
    original_prove_contract = location_module._prove_contract

    if cut == "after-reserve":
        monkeypatch.setattr(
            location_module,
            "_publish_or_prove_manifest",
            mock.Mock(side_effect=_PublicationCut()),
        )
    elif cut == "after-manifest-write":
        interrupted = False

        def cut_manifest_read(path: Path, owner: str, contract_path: Path) -> bytes:
            nonlocal interrupted
            if path.name == "enclosure-manifest.json" and path.exists() and not interrupted:
                interrupted = True
                raise _PublicationCut()
            return original_read(path, owner, contract_path)

        monkeypatch.setattr(location_module, "_read_bytes", cut_manifest_read)
    elif cut == "after-manifest-readback":
        monkeypatch.setattr(
            location_module,
            "_prove_manifest",
            mock.Mock(side_effect=_PublicationCut()),
        )
    elif cut == "after-contract-write":
        monkeypatch.setattr(
            location_module,
            "_prove_contract",
            mock.Mock(side_effect=_PublicationCut()),
        )
    else:

        def cut_after_proof(artifacts, locator):
            original_prove_contract(artifacts, locator)
            raise _PublicationCut()

        monkeypatch.setattr(location_module, "_prove_contract", cut_after_proof)

    with pytest.raises(_PublicationCut):
        publish_new_lifecycle_operation_location(contract, contract_text=text)

    assert locator_path.is_file()
    interrupted = LifecycleEnclosureLocator.model_validate_json(
        locator_path.read_text(encoding="utf-8")
    )
    assert (
        interrupted.state
        == {
            "after-reserve": "reserved",
            "after-manifest-write": "reserved",
            "after-manifest-readback": "reserved",
            "after-contract-write": "manifest-proven",
            "after-addressable-proof": "addressable",
        }[cut]
    )
    manifest = contract.worktree_group / ".lifecycle" / "enclosure-manifest.json"
    assert manifest.exists() is (cut != "after-reserve")
    assert contract.contract_path.exists() is (
        cut in {"after-contract-write", "after-addressable-proof"}
    )
    assert not list((contract.worktree_group / ".lifecycle").glob("*-operation.*"))
    monkeypatch.undo()
    location = publish_new_lifecycle_operation_location(contract, contract_text=text)
    assert location.locator.state == "addressable"
    assert location.contract_path.read_text(encoding="utf-8") == text
    assert (
        resolve_lifecycle_operation_location(
            contract.coordination_root,
            contract.contract_path,
        )
        == location
    )


@pytest.mark.parametrize("same_binding", [True, False])
def test_concurrent_locator_reservation_converges_or_types_exact_conflict(
    tmp_path: Path,
    same_binding: bool,
) -> None:
    first, first_text = enclosure_contract(tmp_path / "first")
    if same_binding:
        second, second_text = first, first_text
    else:
        second, second_text = enclosure_contract(
            tmp_path / "first",
            worktree_name="other-root",
            leaf_id=first.leaf_id,
        )
        assert second.contract_path == first.contract_path
        assert second.worktree_group != first.worktree_group
    barrier = Barrier(3)

    def publish(contract, text: str):
        barrier.wait(timeout=10)
        try:
            return publish_new_lifecycle_operation_location(contract, contract_text=text)
        except LifecycleOperationLocationError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(publish, first, first_text)
        right = pool.submit(publish, second, second_text)
        barrier.wait(timeout=10)
        outcomes = [left.result(timeout=10), right.result(timeout=10)]

    locations = [row for row in outcomes if isinstance(row, LifecycleOperationLocation)]
    errors = [row for row in outcomes if isinstance(row, LifecycleOperationLocationError)]
    if same_binding:
        assert len(locations) == 2
        assert not errors
        assert locations[0] == locations[1]
    else:
        assert len(locations) == 1
        assert len(errors) == 1
        assert errors[0].status == "operation-location-conflict"
        assert errors[0].expected != errors[0].observed
        loser = second if locations[0].worktree_group == first.worktree_group else first
        assert not (loser.worktree_group / ".lifecycle" / "enclosure-manifest.json").exists()


def test_present_nonregular_and_unreadable_locator_are_not_adoption_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, text = enclosure_contract(tmp_path)
    write_contract(contract.contract_path, contract)
    locator = lifecycle_operation_locator_path(contract.coordination_root, contract.contract_path)
    locator.mkdir(parents=True)
    with pytest.raises(LifecycleOperationLocationError) as nonregular:
        resolve_lifecycle_operation_location(contract.coordination_root, contract.contract_path)
    assert nonregular.value.status == "operation-location-invalid"
    assert nonregular.value.observed["fileType"] == "non-regular"

    locator.rmdir()
    publish_new_lifecycle_operation_location(contract, contract_text=text)
    original_read_text = Path.read_text

    def unreadable(path: Path, *args, **kwargs):
        if path == locator:
            raise PermissionError("forced unreadable locator")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(LifecycleOperationLocationError) as denied:
        resolve_lifecycle_operation_location(contract.coordination_root, contract.contract_path)
    assert denied.value.status == "operation-location-invalid"
    assert denied.value.observed["errorType"] == "PermissionError"


def test_locator_manifest_contract_and_root_contradictions_remain_distinct(
    tmp_path: Path,
) -> None:
    contract, text = enclosure_contract(tmp_path)
    with pytest.raises(LifecycleOperationLocationError) as missing:
        resolve_lifecycle_operation_location(contract.coordination_root, contract.contract_path)
    assert missing.value.status == "operation-location-adoption-required"

    location = publish_new_lifecycle_operation_location(contract, contract_text=text)
    mismatched = replace(contract, task_name="different task")
    with pytest.raises(LifecycleOperationLocationError) as contract_conflict:
        require_matching_lifecycle_operation_location(mismatched)
    assert contract_conflict.value.status == "operation-location-mismatch"

    location.manifest_path.write_text(
        location.manifest.model_copy(update={"taskName": "external mutation"}).model_dump_json(
            indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(LifecycleOperationLocationError) as manifest_conflict:
        resolve_lifecycle_operation_location(contract.coordination_root, contract.contract_path)
    assert manifest_conflict.value.status == "operation-location-mismatch"

    location.manifest_path.unlink()
    with pytest.raises(LifecycleOperationLocationError) as root_lost:
        resolve_lifecycle_operation_location(contract.coordination_root, contract.contract_path)
    assert root_lost.value.status == "operation-enclosure-root-missing"


def test_unreadable_contract_does_not_hide_exact_root_journal(tmp_path: Path) -> None:
    contract = _contract(tmp_path, selected_profile=True)
    operation_input = selected_closeout_operation_input(contract, code="retain exact root journal")
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    location = resolve_lifecycle_operation_location(
        contract.coordination_root,
        contract.contract_path,
    )
    journal_bytes = location.journal_path("closeout").read_bytes()
    assert location.journal_path("closeout").is_file()
    contract.contract_path.write_text("not a contract\n", encoding="utf-8")
    resolved = resolve_lifecycle_operation_location(
        contract.coordination_root,
        contract.contract_path,
    )
    assert resolved == location
    assert resolved.journal_path("closeout").read_bytes() == journal_bytes

    config = load_config(tmp_path / "settings.json")
    status_result = WorktreeCommandResult(
        2,
        {
            "contract_path": contract.contract_path.as_posix(),
            "contractReadFailure": {
                "errorType": "ContractError",
                "detail": "forced unreadable contract bytes",
            },
        },
    )
    with mock.patch(
        "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
        return_value=status_result,
    ):
        status = worktree_status_tool(
            config,
            TaskRef(repo_id=contract.repo_name, contract_path=contract.contract_path.as_posix()),
        )
    closeout = next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")
    assert closeout["generation"] == 1
    assert closeout["legalControls"] == []
    assert closeout["result"]["nextAction"] == "developer-decision"

    before_control = {
        path.relative_to(contract.coordination_root).as_posix(): path.read_bytes()
        for path in contract.coordination_root.rglob("*")
        if path.is_file()
    }
    refused = worktree_operation_control_tool(
        config,
        OperationControlRequest(
            contract_path=contract.contract_path.as_posix(),
            operation_kind="closeout",
            action="recover",
            expected_generation=1,
            intent_note="exercise captured stale recovery row",
        ),
    )
    assert refused["status"] == closeout["result"]["state"]
    assert refused["nextAction"] == "developer-decision"
    after_control = {
        path.relative_to(contract.coordination_root).as_posix(): path.read_bytes()
        for path in contract.coordination_root.rglob("*")
        if path.is_file()
    }
    assert after_control == before_control
