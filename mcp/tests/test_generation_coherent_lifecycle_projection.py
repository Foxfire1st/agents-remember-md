"""CCR-R18 revision, state-matrix, guidance, and cleanup forcing tests."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import agents_remember.models.lifecycles.operation_projection as op_projection_module
import pytest
from agents_remember.application.tool_response import bound_next_step
from agents_remember.models.base import NextStep
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationProjection,
    LifecycleOperationRecord,
)
from agents_remember.models.lifecycles.operation_projection import (
    STATE_MATRIX,
    LifecycleProjectionIncoherence,
    _require_coherent_projection_components,
    _require_component_bindings_match_envelope,
    _require_projection_task_addresses,
    _validate_projection_cell,
    validate_state_matrix_is_exhaustive,
)
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.models.worktree import WorktreeStatusResponse
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    OperationProjectionContext,
    _require_all_or_none_worker_binding,
    _termination_bound_worker_observation,
    _validate_dependent_identities,
    bind_projection_decision,
    bind_projection_result,
    operation_projection,
    operation_projection_identity,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    _advance_record_revision,
    _validate_identity_and_evidence_transition,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.worker.state import (
    release_worker_after_exit,
)
from agents_remember.worktrees.integration.terminal_enclosure_archive import (
    _require_archivable_operation,
)
from closeout_input_test_support import closeout_operation_input, start_closeout_operation
from selected_lifecycle_test_support import replace_selected_fixture_generation, selected_contract

_LEASE = "6" * 64
_WORKER_FINGERPRINT = "7" * 64


def _claimed_contract_and_record(tmp_path: Path):
    """Compose one canonical claimed closeout generation like the L2 suites do."""

    contract = selected_contract(tmp_path, candidate_file=("candidate.py", "VALUE = 1\n"))
    operation_input = closeout_operation_input(contract, code="close exact projection fixture")
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    record = store.read()
    assert record is not None
    assert record.doorPublication is not None
    return contract, store, record


def _with_worker(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
    return record.model_copy(
        update={
            "workerPid": 4242,
            "workerLease": _LEASE,
            "workerProcessFingerprint": _WORKER_FINGERPRINT,
        }
    )


def _with_termination(
    record: LifecycleOperationRecord,
    *,
    state: str,
    cancel_requested: bool,
) -> LifecycleOperationRecord:
    updated = record.model_copy(
        update={
            "workerTermination": WorkerTerminationEvidence(
                state=state,  # type: ignore[arg-type]
                pid=4242,
                lease=_LEASE,
                processFingerprint=_WORKER_FINGERPRINT,
                requestedAt="2026-09-01T00:00:30+00:00",
                observedAt=("2026-09-01T00:01:10+00:00" if state == "exited" else None),
            ),
            "cancelRequested": cancel_requested,
        }
    )
    if record.status == "termination-required":
        updated = updated.model_copy(
            update={
                "terminationReturnStatus": "running",
                "terminationReturnPhase": "quality",
            }
        )
    return updated


def _validate(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
    return LifecycleOperationRecord.model_validate(record.model_dump(mode="json"))


@pytest.mark.parametrize(
    "status,phase,result,worker",
    [
        ("queued", "queued", None, True),
        ("running", "quality", None, True),
        (
            "input-required",
            "failed",
            {"state": "repair-required", "nextAction": "retry"},
            False,
        ),
        ("termination-required", "termination-required", None, True),
        ("completed", "completed", {"state": "completed"}, False),
        ("failed", "failed", {"state": "failed"}, False),
        ("cancelled", "cancelled", None, False),
    ],
)
def test_every_lifecycle_status_projects_one_bound_matrix_cell(
    tmp_path: Path,
    status: str,
    phase: str,
    result: dict[str, object] | None,
    worker: bool,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    base = record.model_copy(
        update={
            "status": status,
            "phase": phase,
            "result": result,
            "finishedAt": (
                "2026-09-01T00:01:00+00:00" if status in {"completed", "failed"} else None
            ),
        }
    )
    if status == "termination-required":
        base = _with_termination(base, state="requested", cancel_requested=True)
    if worker:
        base = _with_worker(base)
    variant = _validate(base)

    projected = operation_projection(variant, contract=contract)

    assert projected.status == variant.status
    assert projected.identity is not None
    assert projected.identity.recordRevision == variant.recordRevision
    assert projected.identity.candidateTupleDigest == variant.fingerprint
    assert projected.componentBindings is not None
    digest = projected.identity.identityDigest
    assert projected.componentBindings.approval == digest
    assert projected.componentBindings.worker == digest
    assert projected.componentBindings.legalControls == [digest] * len(projected.legalControls)


def test_state_matrix_exhausts_status_and_phase_vocabularies() -> None:
    validate_state_matrix_is_exhaustive()
    assert len(STATE_MATRIX) == 7


def test_healthy_live_worker_is_legal_to_cancel_but_recommended_to_observe(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    variant = _validate(
        _with_worker(record).model_copy(update={"status": "running", "phase": "quality"})
    )

    projected = operation_projection(variant, contract=contract)

    assert projected.status == "running"
    assert projected.worker is not None and projected.worker.state == "live"
    assert projected.result is None
    assert [item["action"] for item in projected.legalControls] == ["cancel"]
    assert projected.recommendedAction is not None
    assert projected.recommendedAction.action == "observe"
    assert projected.recommendedAction.mutating is False


@pytest.mark.parametrize("phase", ["quality", "memory-preflight"])
def test_generation_11_and_15_contradictions_refuse_without_controls(
    tmp_path: Path,
    phase: str,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    contradictory = _validate(
        _with_termination(
            _with_worker(record).model_copy(update={"status": "running", "phase": phase}),
            state="termination-required",
            cancel_requested=False,
        )
    )

    projected = operation_projection(contradictory, contract=contract)

    assert projected.status == "incoherent"
    assert projected.result is not None
    assert projected.result["state"] == "lifecycle-projection-incoherent"
    assert projected.legalControls == []
    assert projected.recommendedAction is None
    assert projected.cancellable is False


def test_real_termination_recovery_recommends_its_exact_legal_cancel(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    terminating = _validate(
        _with_termination(
            _with_worker(record).model_copy(
                update={"status": "termination-required", "phase": "termination-required"}
            ),
            state="requested",
            cancel_requested=True,
        )
    )

    projected = operation_projection(terminating, contract=contract)

    assert projected.status == "termination-required"
    assert projected.result is not None
    assert projected.result["state"] == "worker-termination-required"
    assert projected.recommendedAction is not None
    assert projected.recommendedAction.action == "cancel"
    assert projected.recommendedAction.arguments == projected.legalControls[0]["arguments"]


def test_exit_proven_cancellation_keeps_only_same_generation_cancel(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    terminating = _validate(
        _with_termination(
            record.model_copy(
                update={"status": "termination-required", "phase": "termination-required"}
            ),
            state="exited",
            cancel_requested=True,
        )
    )

    projected = operation_projection(terminating, contract=contract)

    assert projected.status == "termination-required"
    assert projected.worker is not None and projected.worker.state == "exited"
    assert [control["action"] for control in projected.legalControls] == ["cancel"]
    assert projected.recommendedAction is not None
    assert projected.recommendedAction.action == "cancel"


def test_adjacent_revision_race_refuses_stale_result_approval_and_worker_facts(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    stale = _validate(_with_worker(record).model_copy(update={"status": "running"}))
    current = stale.model_copy(update={"recordRevision": 2})
    stale_identity = operation_projection_identity(stale)

    for context in (
        OperationProjectionContext(resultIdentity=stale_identity),
        OperationProjectionContext(approvalIdentity=stale_identity),
        OperationProjectionContext(workerIdentity=stale_identity),
    ):
        projected = operation_projection(current, contract=contract, context=context)

        assert projected.status == "incoherent"
        assert projected.result is not None
        assert projected.result["state"] == "lifecycle-projection-incoherent"
        assert projected.legalControls == []
        assert projected.recommendedAction is None


def test_adjacent_generation_candidate_fact_cannot_splice_into_current_projection(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    predecessor = _validate(_with_worker(record).model_copy(update={"status": "running"}))
    successor = replace_selected_fixture_generation(contract, store)

    projected = operation_projection(
        successor,
        contract=contract,
        context=OperationProjectionContext(
            resultIdentity=operation_projection_identity(predecessor)
        ),
    )

    assert projected.status == "incoherent"
    assert projected.identity is not None
    assert projected.identity.generation == 2
    assert projected.result is not None
    assert projected.result["state"] == "lifecycle-projection-incoherent"
    assert projected.legalControls == []


def test_store_revision_is_monotonic_and_stale_cas_cannot_publish(tmp_path: Path) -> None:
    _contract, store, _record = _claimed_contract_and_record(tmp_path)
    del _contract, _record
    initial = store.read()
    assert initial is not None
    advanced = store.update(
        lambda current: current.model_copy(update={"currentCommand": "revision two"})
    )
    assert advanced.recordRevision == initial.recordRevision + 1

    observed, matched = store.update_if_current(
        initial,
        lambda current: current.model_copy(update={"currentCommand": "stale write"}),
    )
    assert matched is False
    assert observed == advanced
    assert store.read() == advanced


def test_cross_task_next_step_is_omitted_while_exact_and_external_guidance_remain() -> None:
    l11 = "/coordination/tasks/repo/master/enclosures/l11/series-contract.md"
    l01 = "/coordination/tasks/repo/master/enclosures/l01/series-contract.md"
    response = WorktreeStatusResponse(
        ok=True,
        contractPath=l11,
        enclosurePath=l11,
    )
    contaminated = NextStep(
        summary="Observe the other leaf.",
        nextTool="worktree_status",
        nextArgs={"contract_path": l01, "enclosure_path": l01},
    )
    exact = contaminated.model_copy(
        update={"nextArgs": {"contract_path": l11, "enclosure_path": l11}}
    )

    assert bound_next_step(response, contaminated) is None
    assert bound_next_step(response, exact) == exact
    assert bound_next_step(WorktreeStatusResponse(ok=True), contaminated) == contaminated


def test_projection_owned_decision_rebinds_every_component_and_clears_controls(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    projected = operation_projection(_validate(_with_worker(record)), contract=contract)
    result = {
        "state": "contract-invalid",
        "developerDecisionRequired": True,
        "nextAction": "developer-decision",
    }

    decision = bind_projection_decision(projected, result, "inspect the exact contract")

    assert decision.componentBindings is not None
    assert decision.identity is not None
    digest = decision.identity.identityDigest
    assert decision.componentBindings.result == digest
    assert decision.componentBindings.recommendedAction == digest
    assert decision.recommendedAction is not None
    assert decision.recommendedAction.action == "developer-decision"
    assert decision.legalControls == []
    assert decision.cancellable is False


def test_projection_rejects_recommendation_or_control_for_another_task(tmp_path: Path) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    projected = operation_projection(_validate(_with_worker(record)), contract=contract)
    assert projected.identity is not None
    other = "/coordination/tasks/repo/other/enclosures/l01/series-contract.md"

    recommendation_payload = projected.model_dump(mode="json")
    recommendation_payload["recommendedAction"] = {
        "action": "observe",
        "tool": "worktree_status",
        "arguments": {"contract_path": other},
        "summary": "observe another task",
    }
    recommendation_payload["componentBindings"]["recommendedAction"] = (
        projected.identity.identityDigest
    )
    with pytest.raises(ValueError, match="different task address"):
        LifecycleOperationProjection.model_validate(recommendation_payload)

    control_payload = projected.model_dump(mode="json")
    control_payload["legalControls"][0]["arguments"]["contract_path"] = other
    with pytest.raises(ValueError, match="different task address"):
        LifecycleOperationProjection.model_validate(control_payload)


def test_projection_rejects_live_cancel_guidance_and_invalid_control_matrix(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    projected = operation_projection(_validate(_with_worker(record)), contract=contract)
    assert projected.identity is not None
    control = projected.legalControls[0]

    required_cancel = projected.model_dump(mode="json")
    required_cancel["recommendedAction"] = {
        "action": "cancel",
        "tool": control["tool"],
        "arguments": control["arguments"],
        "summary": "cancel healthy work",
        "mutating": True,
    }
    required_cancel["componentBindings"]["recommendedAction"] = projected.identity.identityDigest
    with pytest.raises(ValueError, match="healthy live work"):
        LifecycleOperationProjection.model_validate(required_cancel)

    invalid_control = projected.model_dump(mode="json")
    invalid_control["legalControls"][0]["action"] = "integrate"
    invalid_control["cancellable"] = False
    with pytest.raises(ValueError, match="state matrix"):
        LifecycleOperationProjection.model_validate(invalid_control)

    stale_cancellable = projected.model_dump(mode="json")
    stale_cancellable["cancellable"] = False
    with pytest.raises(ValueError, match="exact legal cancel"):
        LifecycleOperationProjection.model_validate(stale_cancellable)

    unreadable_control = projected.model_dump(mode="json")
    unreadable_control["status"] = "unreadable"
    unreadable_control["identity"] = None
    unreadable_control["componentBindings"] = None
    unreadable_control["recommendedAction"] = None
    with pytest.raises(ValueError, match="identity or control authority"):
        LifecycleOperationProjection.model_validate(unreadable_control)


def test_terminal_archive_uses_observed_exit_without_rewriting_audit_identity(
    tmp_path: Path,
) -> None:
    _contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    retained = LifecycleOperationRecord.model_validate(
        _with_worker(record)
        .model_copy(
            update={
                "status": "completed",
                "phase": "completed",
                "result": {"state": "completed"},
                "finishedAt": "2026-09-01T00:01:00+00:00",
            }
        )
        .model_dump(mode="json")
    )
    exit_proof = WorkerTerminationEvidence(
        state="exited",
        pid=4242,
        lease=_LEASE,
        processFingerprint=_WORKER_FINGERPRINT,
        requestedAt="2026-09-01T00:00:30+00:00",
        signal="none",
        observedAt="2026-09-01T00:01:10+00:00",
    )
    original = retained.model_dump_json()
    observed = release_worker_after_exit(retained, exit_proof)

    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.worker.state.observe_worker_termination",
        return_value=exit_proof,
    ) as observe:
        _require_archivable_operation(
            retained,
            operation="worktree_cleanup",
            current=True,
            name="closeout-operation.json",
        )

    observe.assert_called_once()
    assert retained.model_dump_json() == original
    assert retained.workerPid == 4242
    assert observed.workerPid is None


# ---------------------------------------------------------------------------
# L18 diff-coverage closure: refusal/edge cells on the changed projection,
# tool-response, and store surfaces.
# ---------------------------------------------------------------------------


def test_projection_cell_refuses_non_cancel_controls_on_termination_required() -> None:
    # Empty controls pass the invalid-action cell but still violate the exact
    # ["cancel"] requirement, so only [] reaches the termination-required cell.
    with pytest.raises(LifecycleProjectionIncoherence):
        _validate_projection_cell(
            status="termination-required",
            phase="termination-required",
            worker_state="exited",
            result={"state": "worker-termination-required"},
            legal_controls=[],
        )


def test_state_matrix_exhaustiveness_failure_is_raised(monkeypatch) -> None:
    narrowed = {
        status: rule
        for status, rule in op_projection_module.STATE_MATRIX.items()
        if status != "cancelled"
    }
    monkeypatch.setattr(op_projection_module, "STATE_MATRIX", narrowed)
    with pytest.raises(RuntimeError, match="does not exhaust its public vocabulary"):
        validate_state_matrix_is_exhaustive()


def test_validate_projection_state_refuses_cancel_request_outside_termination(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    variant = _validate(
        record.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": "2026-09-01T00:01:00+00:00",
                "cancelRequested": True,
            }
        )
    )
    projected = operation_projection(variant, contract=contract)
    assert projected.status == "incoherent"
    assert projected.result is not None
    assert projected.result["state"] == "lifecycle-projection-incoherent"
    assert projected.legalControls == []


def test_projection_envelope_refusal_cells_are_bounded(tmp_path: Path) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    base = operation_projection(_validate(_with_worker(record)), contract=contract)
    assert base.identity is not None
    assert base.generation is not None

    missing_identity = base.model_dump(mode="json")
    missing_identity["identity"] = None
    with pytest.raises(ValueError, match="requires its exact journal identity"):
        LifecycleOperationProjection.model_validate(missing_identity)

    generation_conflict = base.model_dump(mode="json")
    generation_conflict["generation"] = base.generation + 1
    with pytest.raises(ValueError, match="contradicts its envelope"):
        LifecycleOperationProjection.model_validate(generation_conflict)

    result_binding_conflict = base.model_dump(mode="json")
    result_binding_conflict["result"] = {"state": "fabricated"}
    with pytest.raises(ValueError, match="do not match their envelope"):
        LifecycleOperationProjection.model_validate(result_binding_conflict)

    controls_binding_conflict = base.model_dump(mode="json")
    controls_binding_conflict["legalControls"] = [
        *controls_binding_conflict["legalControls"],
        {"action": "retire", "tool": "worktree_operation_control", "arguments": {}},
    ]
    with pytest.raises(ValueError, match="do not bind the projection envelope"):
        LifecycleOperationProjection.model_validate(controls_binding_conflict)


def test_incoherent_envelope_refuses_advertised_authority(tmp_path: Path) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    contradictory = _validate(
        _with_termination(
            _with_worker(record).model_copy(update={"status": "running", "phase": "quality"}),
            state="termination-required",
            cancel_requested=False,
        )
    )
    incoherent = operation_projection(contradictory, contract=contract)
    assert incoherent.status == "incoherent"
    assert incoherent.identity is not None
    payload = incoherent.model_dump(mode="json")
    payload["legalControls"] = [{"action": "cancel", "tool": "worktree_operation_control"}]
    payload["componentBindings"]["legalControls"] = [payload["identity"]["identityDigest"]]
    with pytest.raises(ValueError, match="cannot advertise mutating authority"):
        LifecycleOperationProjection.model_validate(payload)


def test_component_bindings_helper_refuses_missing_identity() -> None:
    identityless = LifecycleOperationProjection.model_construct(
        status="running",
        phase="quality",
        kind="closeout",
        identity=None,
        componentBindings=None,
    )
    with pytest.raises(ValueError, match="requires its exact journal identity"):
        _require_component_bindings_match_envelope(identityless)


def test_coherent_components_refuse_non_coherent_status() -> None:
    incoherent_envelope = LifecycleOperationProjection.model_construct(
        status="incoherent",
        phase="quality",
        kind="closeout",
    )
    with pytest.raises(ValueError, match="requires a lifecycle status"):
        _require_coherent_projection_components(incoherent_envelope)


def test_coherent_components_require_an_explicit_worker_observation(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    base = operation_projection(_validate(_with_worker(record)), contract=contract)
    workerless = base.model_dump(mode="json")
    workerless["worker"] = None
    workerless["componentBindings"]["worker"] = None
    with pytest.raises(ValueError, match="requires an explicit worker observation"):
        LifecycleOperationProjection.model_validate(workerless)


def test_recommendation_must_match_one_exact_mutating_legal_control(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    base = operation_projection(_validate(record), contract=contract)
    assert base.identity is not None
    digest = base.identity.identityDigest
    control = base.legalControls[0]
    payload = base.model_dump(mode="json")

    payload["recommendedAction"] = {
        "action": control["action"],
        "tool": control["tool"],
        "arguments": control["arguments"],
        "summary": "read-only recommendation",
        "mutating": False,
    }
    payload["componentBindings"]["recommendedAction"] = digest
    with pytest.raises(ValueError, match="recommended control does not match"):
        LifecycleOperationProjection.model_validate(payload)

    payload["recommendedAction"] = {
        "action": control["action"],
        "tool": "worktree_status",
        "arguments": control["arguments"],
        "summary": "wrong tool",
        "mutating": True,
    }
    payload["componentBindings"]["recommendedAction"] = digest
    with pytest.raises(ValueError, match="recommended control does not match"):
        LifecycleOperationProjection.model_validate(payload)


def test_task_address_validation_requires_an_identity() -> None:
    identityless = LifecycleOperationProjection.model_construct(
        status="running",
        phase="quality",
        kind="closeout",
        identity=None,
    )
    with pytest.raises(ValueError, match="requires a projection identity"):
        _require_projection_task_addresses(identityless)


def test_bound_next_step_omits_multi_address_guidance() -> None:
    response = WorktreeStatusResponse(ok=True, contractPath="/one", enclosurePath="/two")
    step = NextStep(
        summary="conflicting addresses",
        nextTool="worktree_status",
        nextArgs={"contract_path": "/one"},
    )
    assert bound_next_step(response, step) is None


def test_bind_projection_result_without_guidance_keeps_envelope(
    tmp_path: Path,
) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    projected = operation_projection(_validate(_with_worker(record)), contract=contract)
    rebound = bind_projection_result(projected, {"state": "terminal-edge"})
    assert rebound.guidance is None
    assert rebound.result == {"state": "terminal-edge"}


def test_rebind_projection_refuses_identityless_envelope(tmp_path: Path) -> None:
    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    projected = operation_projection(_validate(_with_worker(record)), contract=contract)
    unreadable = LifecycleOperationProjection.model_validate(
        {
            **projected.model_dump(mode="json"),
            "status": "unreadable",
            "identity": None,
            "componentBindings": None,
            "recommendedAction": None,
            "legalControls": [],
            "cancellable": False,
        }
    )
    with pytest.raises(LifecycleProjectionIncoherence):
        bind_projection_result(unreadable, {"state": "not-visible"})


def test_dependent_identity_requires_door_binding(tmp_path: Path) -> None:
    _contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    identity = operation_projection_identity(record)
    context = OperationProjectionContext(
        door=mock.Mock(),
        doorIdentity=None,
    )
    with pytest.raises(LifecycleProjectionIncoherence) as raised:
        _validate_dependent_identities(identity, context=context)
    assert raised.value.observed == {"doorIdentity": "unbound"}


def test_worker_binding_projection_guards_refuse_incoherent_cells() -> None:
    # The record model already refuses partial bindings, unproven termination
    # identity mismatches, and retained bindings after proven exit; these direct
    # calls pin the projection layer's defensive refusal on the same cells.
    partial = LifecycleOperationRecord.model_construct(
        workerPid=4242,
        workerLease=None,
        workerProcessFingerprint=None,
    )
    with pytest.raises(LifecycleProjectionIncoherence):
        _require_all_or_none_worker_binding(partial, (4242, None, None))

    mismatched_termination = WorkerTerminationEvidence(
        state="requested",
        pid=9999,
        lease=_LEASE,
        processFingerprint=_WORKER_FINGERPRINT,
        requestedAt="2026-09-01T00:00:30+00:00",
    )
    with pytest.raises(LifecycleProjectionIncoherence):
        _termination_bound_worker_observation(
            (4242, _LEASE, _WORKER_FINGERPRINT),
            mismatched_termination,
        )

    exited_retained = WorkerTerminationEvidence(
        state="exited",
        pid=4242,
        lease=_LEASE,
        processFingerprint=_WORKER_FINGERPRINT,
        requestedAt="2026-09-01T00:00:30+00:00",
        observedAt="2026-09-01T00:01:10+00:00",
    )
    with pytest.raises(LifecycleProjectionIncoherence):
        _termination_bound_worker_observation(
            (4242, _LEASE, _WORKER_FINGERPRINT),
            exited_retained,
        )


def test_store_revision_discipline_guards_are_enforced(tmp_path: Path) -> None:
    _contract, store, record = _claimed_contract_and_record(tmp_path)
    del record
    current = store.read()
    assert current is not None

    with pytest.raises(RuntimeError, match="transforms cannot assign record revision"):
        store.update(
            lambda value: value.model_copy(update={"recordRevision": value.recordRevision + 5})
        )

    non_monotonic = current.model_copy(update={"recordRevision": current.recordRevision + 3})
    with pytest.raises(RuntimeError, match="must advance exactly once"):
        _validate_identity_and_evidence_transition(current, non_monotonic)

    same, changed = store.update_if_current(current, lambda value: value)
    assert changed is True
    assert same == current
    assert _advance_record_revision(current, current).recordRevision == (current.recordRevision + 1)
