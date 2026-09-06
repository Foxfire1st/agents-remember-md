"""Focused proof for lifecycle cancellation, replacement, and disposition helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest
from agents_remember.worktrees.integration.integration_ref_state import (
    IntegrationRefDecisionError,
)
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_completed_disposition as completed,
)
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operation_control_projection as controls,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations as operations
from agents_remember.worktrees.integration.lifecycle.control import cancellation
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_errors import (
    LifecycleControlError,
)
from agents_remember.worktrees.integration.organizational_completion_repair import (
    OrganizationalRepairPublicationError,
)
from agents_remember.worktrees.queue.closeout_queue import CloseoutQueueError


def _value(**fields: object) -> Any:
    return cast(Any, SimpleNamespace(**fields))


def _contract(**overrides: object) -> Any:
    fields: dict[str, object] = {
        "contract_path": Path("/tmp/contract.json"),
        "task_name": "task",
        "integration_status": "not-started",
        "closeout_door": None,
    }
    fields.update(overrides)
    return _value(**fields)


def test_organizational_cancellation_repair_runs_only_for_exact_apply() -> None:
    contract = _contract()
    repair = _value(acceptedContractSha256="a", resetContractSha256="b")
    record = _value(operationKind="integrate", organizationalRepair=repair)
    reset = _contract(contract_path=Path("/tmp/reset.json"))

    assert cancellation._complete_organizational_repair(contract, record, dry_run=True) is contract
    with mock.patch.object(
        cancellation, "_prepare_completed_organizational_repair", return_value=reset
    ) as prepare:
        assert (
            cancellation._complete_organizational_repair(contract, record, dry_run=False) is reset
        )
    prepare.assert_called_once_with(contract, record, repair)

    with (
        mock.patch.object(cancellation, "load_contract", return_value=contract),
        mock.patch.object(
            cancellation, "prepare_organizational_completion_repair", return_value=reset
        ),
    ):
        assert (
            cancellation._prepare_completed_organizational_repair(contract, record, repair) is reset
        )

    queue_error = CloseoutQueueError("repair-conflict", "conflict")
    with (
        mock.patch.object(cancellation, "load_contract", return_value=contract),
        mock.patch.object(
            cancellation,
            "prepare_organizational_completion_repair",
            side_effect=queue_error,
        ),
        mock.patch.object(
            cancellation,
            "_raise_organizational_repair_failure",
            side_effect=RuntimeError("translated"),
        ),
        pytest.raises(RuntimeError, match="translated"),
    ):
        cancellation._prepare_completed_organizational_repair(contract, record, repair)


def test_organizational_repair_failures_keep_their_decision_class() -> None:
    contract = _contract()
    repair = _value(acceptedContractSha256="a", resetContractSha256="b")
    record = _value(candidateState="candidate")

    ref_error = IntegrationRefDecisionError.__new__(IntegrationRefDecisionError)
    ref_error.classification = _value(decision_payload=lambda: {"state": "conflict"})
    with (
        mock.patch.object(
            cancellation, "raise_integration_decision", side_effect=RuntimeError("decision")
        ),
        pytest.raises(RuntimeError, match="decision"),
    ):
        cancellation._raise_organizational_repair_failure(contract, record, repair, ref_error)

    publication = OrganizationalRepairPublicationError.__new__(OrganizationalRepairPublicationError)
    publication.status = "publication-cut"
    publication.detail = "cut"
    publication.expected = dict[str, object](state="accepted")
    publication.observed = dict[str, object](state="third")
    publication.next_action = "developer-decision"
    with pytest.raises(LifecycleControlError, match="cut"):
        cancellation._raise_organizational_repair_failure(contract, record, repair, publication)

    observed = _contract(
        closeout_status="completed",
        integration_status="not-started",
        closeout_door=_value(disposition="claimed"),
    )
    with (
        mock.patch.object(cancellation, "load_contract", return_value=observed),
        mock.patch.object(cancellation, "closeout_contract_sha256", return_value="sha"),
        pytest.raises(LifecycleControlError, match="contradicts the live contract"),
    ):
        cancellation._raise_organizational_repair_failure(
            contract,
            record,
            repair,
            CloseoutQueueError("repair-conflict", "conflict"),
        )


def _replacement(kind: str, *, status: str = "cancelled", state: str = "next") -> Any:
    return _value(
        store=mock.Mock(),
        queued=_value(),
        current=_value(status=status, candidateState="current"),
        contract=_contract(),
        operation_input=_value(kind=kind),
        candidate=_value(state=state),
        initial_certification=mock.Mock(),
    )


def test_cancelled_generation_replacement_requires_an_exact_successor() -> None:
    integrate = _replacement("integrate")
    with mock.patch.object(
        operations, "_replace_cancelled_integrate", return_value=(integrate.queued, True)
    ) as replace_integrate:
        assert operations._replace_cancelled_generation(integrate) == (integrate.queued, True)
    replace_integrate.assert_called_once()

    closeout = _replacement("closeout")
    with mock.patch.object(
        operations, "_replace_cancelled_closeout", return_value=(closeout.queued, True)
    ) as replace_closeout:
        assert operations._replace_cancelled_generation(closeout) == (closeout.queued, True)
    replace_closeout.assert_called_once_with(
        closeout.store,
        closeout.queued,
        closeout.current,
        closeout.contract,
        closeout.initial_certification,
    )

    with pytest.raises(RuntimeError, match="explicit task-addressed"):
        operations._replace_cancelled_generation(_replacement("direct-landing"))

    with pytest.raises(RuntimeError, match="explicit task-addressed"):
        operations._replace_terminal_generation(_replacement("integrate", status="failed"))

    same = _replacement("integrate", state="current")
    with pytest.raises(RuntimeError, match="advanced task state"):
        operations._replace_cancelled_integrate(
            same.store, same.queued, same.current, same.candidate
        )
    advanced = _replacement("integrate", state="advanced")
    advanced.store.replace_terminal.return_value = advanced.queued
    assert operations._replace_cancelled_integrate(
        advanced.store,
        advanced.queued,
        advanced.current,
        advanced.candidate,
    ) == (advanced.queued, True)


def test_cancelled_closeout_and_completed_replacement_bind_release_proof() -> None:
    successor = _value(
        disposition="waiting",
        predecessorGenerationId="latest-provenance",
    )
    current = _value(
        generationDisposition="cancelled",
        cancellationEvidence=_value(workerExitProven=True),
    )
    store = mock.Mock()
    queued = _value()
    store.replace_terminal.return_value = queued
    initial_certification = mock.Mock()

    assert operations._replace_cancelled_closeout(
        store, queued, current, _contract(closeout_door=successor), initial_certification
    ) == (queued, True)
    store.replace_terminal.assert_called_once_with(
        queued, initial_certification=initial_certification
    )

    current.cancellationEvidence.workerExitProven = False
    with pytest.raises(RuntimeError, match="proven worker exit"):
        operations._replace_cancelled_closeout(
            store, queued, current, _contract(closeout_door=successor), initial_certification
        )

    current.cancellationEvidence.workerExitProven = True
    successor.disposition = "deferred"
    with pytest.raises(RuntimeError, match="current waiting door"):
        operations._replace_cancelled_closeout(
            store, queued, current, _contract(closeout_door=successor), initial_certification
        )
    store.replace_terminal.assert_called_once_with(
        queued, initial_certification=initial_certification
    )
    initial_certification.assert_not_called()

    replacement = _replacement("integrate", status="completed")
    replacement.store.replace_terminal.return_value = replacement.queued
    with (
        mock.patch.object(operations, "_require_released_closeout_output") as release,
        mock.patch.object(operations, "_require_advanced_integration_state") as advance,
    ):
        assert operations._replace_completed_generation(replacement) == (
            replacement.queued,
            True,
        )
    release.assert_called_once()
    advance.assert_called_once()
    replacement.store.replace_terminal.assert_called_once_with(
        replacement.queued, initial_certification=replacement.initial_certification
    )

    with (
        mock.patch.object(operations, "closeout_generation_retained", return_value=True),
        pytest.raises(RuntimeError, match="still owns unintegrated output"),
    ):
        operations._require_released_closeout_output(
            _value(generationDisposition="active"),
            _contract(integration_status="not-started"),
            _value(kind="closeout"),
        )
    with mock.patch.object(operations, "closeout_generation_retained", return_value=False):
        operations._require_released_closeout_output(
            _value(generationDisposition="active"),
            _contract(integration_status="not-started"),
            _value(kind="closeout"),
        )

    with pytest.raises(RuntimeError, match="task state has not advanced"):
        operations._require_advanced_integration_state(
            _value(status="completed", candidateState="same"),
            _contract(),
            _value(kind="integrate"),
            _value(state="same"),
        )
    operations._require_advanced_integration_state(
        _value(status="completed", candidateState="old"),
        _contract(),
        _value(kind="integrate"),
        _value(state="new"),
    )


def test_completed_disposition_requires_idle_exact_owner_and_worker_exit() -> None:
    contract = _contract(closeout_door=_value())
    publication = _value(claimState="claimed")
    active = _value(
        integrationPublication=publication,
        status="running",
        generation=3,
    )
    with pytest.raises(LifecycleControlError, match="must finish"):
        completed._require_idle_integration_claim(contract, active, "retire")
    completed._require_idle_integration_claim(
        contract,
        _value(integrationPublication=publication, status="failed"),
        "retire",
    )

    generation = _value(
        disposition="claimed",
        operationKind="direct-landing",
        operationFingerprint="fingerprint",
        claimedOperationKey="operation",
    )
    record = _value(
        operationKind="direct-landing",
        generationDisposition="active",
        fingerprint="fingerprint",
        operationKey="operation",
        doorPublication=_value(state="proven", generation=generation),
    )
    contract.closeout_door = generation
    assert completed._completed_direct_owner(contract, record)

    completed._require_completed_owner(
        _contract(integration_status="not-started"),
        _value(status="completed"),
        True,
    )
    with pytest.raises(LifecycleControlError, match="exact completed"):
        completed._require_completed_owner(
            _contract(integration_status="completed"),
            _value(status="completed"),
            True,
        )

    worker_record = _value(workerPid=123)
    with (
        mock.patch.object(
            completed,
            "located_lifecycle_operation_store",
            return_value=_value(read=lambda: None),
        ),
        mock.patch.object(completed, "_completed_closeout_owner", return_value=True),
        mock.patch.object(completed, "_completed_direct_owner", return_value=False),
        mock.patch.object(completed, "_require_completed_owner"),
        pytest.raises(LifecycleControlError, match="worker exit"),
    ):
        completed.require_completed_disposition(contract, worker_record, "retire")


def test_completed_direct_controls_distinguish_successor_owner_and_absence() -> None:
    contract = _contract()
    record = _value(doorPublication=None)
    assert not controls._is_exact_direct_owner(contract, record)

    with (
        mock.patch.object(controls, "_is_exact_direct_successor", return_value=True),
        mock.patch.object(
            controls, "_direct_successor_control", return_value={"action": "successor"}
        ),
    ):
        assert controls._completed_direct_controls(
            contract, record, {}, allow_completed_disposition=False
        ) == [{"action": "successor"}]

    with (
        mock.patch.object(controls, "_is_exact_direct_successor", return_value=False),
        mock.patch.object(controls, "_is_exact_direct_owner", return_value=True),
        mock.patch.object(
            controls,
            "_completed_direct_owner_controls",
            return_value=[{"action": "retire"}],
        ),
    ):
        assert controls._completed_direct_controls(
            contract, record, {}, allow_completed_disposition=True
        ) == [{"action": "retire"}]
        assert (
            controls._completed_direct_controls(
                contract, record, {}, allow_completed_disposition=False
            )
            == []
        )
