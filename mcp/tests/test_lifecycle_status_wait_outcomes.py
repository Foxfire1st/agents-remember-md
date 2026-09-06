"""CCR-R18 revision, state-matrix, guidance, and cleanup forcing tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.application.lifecycle import lifecycle_status_wait as wait_app_module
from agents_remember.application.lifecycle.lifecycle_status_wait import (
    LifecycleStatusWaitRequest,
    _coherent_wait_payload,
    _loaded_contract,
    _refusal_expected,
    worktree_status_wait_tool,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.mcp.tools.base import PUBLIC_TOOLS
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationRecord,
    LifecycleOperationRecoveryCommits,
    meaningful_state_changed,
)
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.models.tools.tool_registry import TOOL_RESPONSE_MODELS
from agents_remember.models.worktree import WorktreeStatusWaitResponse
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.observation.status_wait import (
    OUTCOME_CHANGED,
    OUTCOME_JOURNAL_REPLACED,
    OUTCOME_JOURNAL_UNREADABLE,
    OUTCOME_NO_OPERATION,
    OUTCOME_SUCCESSOR,
    OUTCOME_UNCHANGED,
    OUTCOME_WRONG_CURSOR,
    OUTCOME_WRONG_GENERATION,
    LifecycleWaitClock,
    LifecycleWaitDecision,
    validate_wait_cursor,
    wait_for_lifecycle_change,
)
from agents_remember.worktrees.worktree_contract import load_contract
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


# ---------------------------------------------------------------------------
# CCR-R15 typed outcomes and the meaningful-state classification map: changed /
# unchanged(timeout) / successor / wrong-contract / wrong-generation /
# wrong-cursor / journal-replaced / journal-unreadable, plus the falsifier that
# heartbeat/current-command/history noise never moves the classification digest.
# ---------------------------------------------------------------------------


def _store_on(tmp_path: Path, name: str = "closeout-operation.json"):
    journal = tmp_path / ".lifecycle" / name
    journal.parent.mkdir(parents=True, exist_ok=True)
    return journal, LifecycleOperationStore(journal)


def _write_record(journal: Path, record: LifecycleOperationRecord) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(record.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")


def _rewrite_record(journal: Path, record: LifecycleOperationRecord, **updates):
    if "generation" in updates:
        previous_generation = record.generation
        record = replace_selected_fixture_generation(
            load_contract(Path(record.input.contractPath)), LifecycleOperationStore(journal)
        )
        assert record.generation == updates["generation"]
        # Each archive scenario below supplies its own present, missing, or damaged
        # predecessor proof. The successor itself retains a real immutable selection.
        journal.with_name(f"{journal.stem}.generation-{previous_generation}.json").unlink()
    variant = LifecycleOperationRecord.model_validate(
        record.model_copy(update=updates).model_dump(mode="json")
    )
    _write_record(journal, variant)
    return variant


def test_wrong_cursor_is_refused_before_any_journal_read(tmp_path: Path) -> None:

    assert validate_wait_cursor(0) is not None
    assert validate_wait_cursor(-3) is not None
    assert validate_wait_cursor(1) is None
    journal, store = _store_on(tmp_path)
    del journal
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=1,
        after_revision=0,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_WRONG_CURSOR
    assert decision.record is None


def test_no_operation_refuses_typed_when_journal_is_absent(tmp_path: Path) -> None:

    journal, store = _store_on(tmp_path)
    assert not journal.exists()
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=1,
        after_revision=1,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_NO_OPERATION


def test_malformed_journal_is_a_typed_unreadable_refusal(tmp_path: Path) -> None:

    journal, store = _store_on(tmp_path)
    journal.write_text("{ not json", encoding="utf-8")
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=1,
        after_revision=1,
        timeout_seconds=0.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_JOURNAL_UNREADABLE
    assert decision.readError is not None


def test_meaningful_classification_ignores_noise_and_flags_meaningful_fields(
    tmp_path: Path,
) -> None:

    contract, _store, record = _claimed_contract_and_record(tmp_path)
    del contract
    noise_fields = {
        "heartbeatAt": "2026-09-04T00:01:00+00:00",
        "currentCommand": "a new current command",
        "recordRevision": record.recordRevision + 1,
        "meaningfulRevision": record.meaningfulRevision + 1,
    }
    for field, value in noise_fields.items():
        assert not meaningful_state_changed(record, record.model_copy(update={field: value})), field
    meaningful = {
        "generation": record.generation + 1,
        "generationDisposition": "superseded",
        "status": "running",
        "phase": "preflight",
        "attempt": record.attempt + 1,
        "approvalClaimed": True,
        "irreversibleBoundaryEntered": True,
        "cancelRequested": True,
        "failure": "typed failure",
        "result": {"state": "failed", "reason": "typed"},
        "recoveryCommits": LifecycleOperationRecoveryCommits(codeCommit="a" * 40),
        "workerTermination": WorkerTerminationEvidence(
            state="requested",
            pid=4242,
            lease=_LEASE,
            processFingerprint=_WORKER_FINGERPRINT,
            requestedAt="2026-09-04T00:00:00+00:00",
        ),
        "closeoutFinalizedContractSha256": "b" * 64,
    }
    for field, value in meaningful.items():
        assert meaningful_state_changed(record, record.model_copy(update={field: value})), field


def test_wait_reports_unchanged_timeout_without_failure(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    unchanged = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision,
        timeout_seconds=0.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert unchanged.outcome == OUTCOME_UNCHANGED
    assert unchanged.record == record


def test_wait_reports_changed_with_next_cursor_after_meaningful_advance(
    tmp_path: Path,
) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    advanced = _rewrite_record(
        store.path,
        record,
        status="running",
        phase="preflight",
        meaningfulRevision=record.meaningfulRevision + 1,
    )
    changed = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert changed.outcome == OUTCOME_CHANGED
    assert changed.record == advanced


def test_journal_replaced_behind_cursor_refuses_typed(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision + 5,
        timeout_seconds=0.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_JOURNAL_REPLACED


def test_wrong_generation_refuses_without_silently_watching(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation + 1,
        after_revision=record.meaningfulRevision,
        timeout_seconds=0.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_WRONG_GENERATION


def test_generation_successor_wakes_old_wait_with_explicit_information(
    tmp_path: Path,
) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    journal = store.path
    successor = _rewrite_record(
        journal,
        record,
        generation=record.generation + 1,
        meaningfulRevision=record.meaningfulRevision + 1,
        attempt=1,
        predecessorFingerprint=record.fingerprint,
        recordRevision=record.recordRevision + 2,
    )
    # Archive the waited predecessor with its exact successor fingerprint.
    archive = journal.with_name(f"{journal.stem}.generation-{record.generation}.json")
    archived = LifecycleOperationRecord.model_validate(
        record.model_copy(update={"successorFingerprint": successor.fingerprint}).model_dump(
            mode="json"
        )
    )
    archive.write_text(
        archived.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8"
    )
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_SUCCESSOR
    assert decision.successorGeneration == successor.generation
    assert decision.record == successor


def test_unproven_successor_archive_refuses_wrong_generation(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    journal = store.path
    _rewrite_record(
        journal,
        record,
        generation=record.generation + 1,
        meaningfulRevision=record.meaningfulRevision + 1,
        attempt=1,
        recordRevision=record.recordRevision + 2,
    )
    # No archive: the successor claim is unproven and the wait must refuse typed.
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_WRONG_GENERATION


def test_application_refusals_never_recommend_mutation(tmp_path: Path) -> None:
    """Every typed refusal names the exact read-only next snapshot action."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    config = load_config(tmp_path / "settings.json")
    path = contract.contract_path.as_posix()
    refusals = [
        worktree_status_wait_tool(
            config,
            LifecycleStatusWaitRequest(
                contract_path=path,
                operation_kind="closeout",
                expected_generation=record.generation,
                after_revision=0,
                timeout_seconds=0.0,
            ),
        ),
        worktree_status_wait_tool(
            config,
            LifecycleStatusWaitRequest(
                contract_path=path,
                operation_kind="closeout",
                expected_generation=record.generation + 1,
                after_revision=record.meaningfulRevision,
                timeout_seconds=0.0,
            ),
        ),
        worktree_status_wait_tool(
            config,
            LifecycleStatusWaitRequest(
                contract_path=path,
                operation_kind="closeout",
                expected_generation=record.generation,
                after_revision=record.meaningfulRevision + 5,
                timeout_seconds=0.0,
            ),
        ),
    ]
    for refusal in refusals:
        assert refusal["ok"] is False
        assert refusal["state"] == "refused"
        assert refusal["nextAction"] == "snapshot"
        assert refusal["nextTool"] == "worktree_status"
        serialized = json.dumps(refusal)
        for forbidden in (
            "recommendedAction",
            "retry",
            "recover",
            "cancelRequested",
            "operationKey",
            "workerPid",
        ):
            assert forbidden not in serialized, (refusal["outcome"], forbidden)


def test_unreadable_journal_application_refusal_is_typed(tmp_path: Path) -> None:

    contract, store, record = _claimed_contract_and_record(tmp_path)
    config = load_config(tmp_path / "settings.json")
    store.path.write_text("{ broken", encoding="utf-8")
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=contract.contract_path.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is False
    assert payload["outcome"] == "journal-unreadable"
    assert "expected" in payload and "observed" in payload


def test_application_wrong_contract_path_is_a_typed_address_refusal(
    tmp_path: Path,
) -> None:
    """Path confinement failure returns the typed wrong-contract refusal."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    config = load_config(tmp_path / "settings.json")
    outside = contract.code_repo_path / "outside-contract.md"
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=outside.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is False
    assert payload["outcome"] == "wrong-contract"
    assert payload["state"] == "refused"
    assert payload["nextTool"] == "worktree_status"
    assert "operationKey" not in json.dumps(payload)


def test_application_unpublished_location_is_a_typed_refusal(tmp_path: Path) -> None:
    """A coordination-confined path with no published locator refuses typed."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    config = load_config(tmp_path / "settings.json")
    unpublished = contract.coordination_root / "tasks/repo/unpublished/unpublished.md"
    unpublished.parent.mkdir(parents=True, exist_ok=True)
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=unpublished.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is False
    assert payload["status"] == "operation-location-adoption-required"
    assert payload["nextAction"] == "developer-decision"


def test_application_successor_outcome_carries_successor_generation(tmp_path: Path) -> None:
    """The successor wire payload returns explicit successor information."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    config = load_config(tmp_path / "settings.json")
    journal = store.path
    successor = _rewrite_record(
        journal,
        record,
        generation=record.generation + 1,
        meaningfulRevision=record.meaningfulRevision + 1,
        attempt=1,
        predecessorFingerprint=record.fingerprint,
        recordRevision=record.recordRevision + 2,
    )
    archive = journal.with_name(f"{journal.stem}.generation-{record.generation}.json")
    archived = LifecycleOperationRecord.model_validate(
        record.model_copy(update={"successorFingerprint": successor.fingerprint}).model_dump(
            mode="json"
        )
    )
    archive.write_text(
        archived.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=contract.contract_path.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is True
    assert payload["outcome"] == "successor"
    assert payload["successorGeneration"] == successor.generation
    assert "lifecycleOperation" in payload
    assert "operationKey" not in json.dumps(payload)


def test_application_incoherent_projection_is_a_read_only_refusal(tmp_path: Path) -> None:
    """A meaningful advance whose projection is incoherent refuses without action."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    config = load_config(tmp_path / "settings.json")
    # running + phase failed is not a declared state-matrix cell, so the record
    # projects incoherent even though the journal advanced meaningfully.
    _rewrite_record(
        store.path,
        record,
        status="running",
        phase="failed",
        meaningfulRevision=record.meaningfulRevision + 1,
    )
    payload = worktree_status_wait_tool(
        config,
        LifecycleStatusWaitRequest(
            contract_path=contract.contract_path.as_posix(),
            operation_kind="closeout",
            expected_generation=record.generation,
            after_revision=record.meaningfulRevision,
            timeout_seconds=0.0,
        ),
    )
    assert payload["ok"] is False
    assert payload["outcome"] == "projection-incoherent"
    assert payload["nextTool"] == "worktree_status"
    projection = payload["lifecycleOperation"]
    assert projection["status"] == "incoherent"
    # The typed R18 refusal carries no mutating recommendation or legal control:
    # the matrix diagnostic cells may name vocabulary, but nothing actionable.
    assert projection["legalControls"] == []
    assert projection["cancellable"] is False
    assert "recommendedAction" not in json.dumps(payload)


def test_loaded_contract_degrades_to_none_when_unreadable(tmp_path: Path) -> None:
    """A torn contract degrades to no contract; the envelope stays projectable."""
    bogus = tmp_path / "bogus.md"
    bogus.write_text("not: [a valid contract\n", encoding="utf-8")
    assert _loaded_contract(bogus) is None


def test_coherent_payload_refuses_a_missing_record(tmp_path: Path) -> None:
    """A coherent outcome without its durable record is a programmer error."""

    decision = LifecycleWaitDecision(outcome="changed", record=None)
    with pytest.raises(RuntimeError, match="must carry its durable record"):
        _coherent_wait_payload(
            tmp_path / "contract.md",
            operation_kind="closeout",
            decision=decision,
            timeout_seconds=0.0,
        )


def test_coherent_payload_omits_projection_when_observation_is_unavailable(
    tmp_path: Path,
) -> None:
    """A None observed projection simply omits the envelope, staying coherent."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del store
    decision = LifecycleWaitDecision(outcome="changed", record=record)
    with mock.patch.object(
        wait_app_module,
        "observed_operation_projection",
        return_value=None,
    ):
        payload = wait_app_module._coherent_wait_payload(
            contract.contract_path,
            operation_kind="closeout",
            decision=decision,
            timeout_seconds=0.0,
        )
    assert payload["ok"] is True
    assert "lifecycleOperation" not in payload


def test_refusal_expected_names_a_successor_proof_cell() -> None:
    """The typed refusal guidance for successor proof is total even when unused."""

    expected = _refusal_expected(LifecycleWaitDecision(outcome="successor"))
    assert expected["generation"] == "waited generation + 1 with archived successor proof"


@pytest.mark.parametrize("archive_kind", ["oversized", "malformed", "non-object"])
def test_unproven_successor_archive_variants_refuse_wrong_generation(
    tmp_path: Path,
    archive_kind: str,
) -> None:
    """Oversized, malformed, and non-object predecessor archives never wake a wait."""

    contract, store, record = _claimed_contract_and_record(tmp_path)
    del contract
    journal = store.path
    _rewrite_record(
        journal,
        record,
        generation=record.generation + 1,
        meaningfulRevision=record.meaningfulRevision + 1,
        attempt=1,
        recordRevision=record.recordRevision + 2,
    )
    archive = journal.with_name(f"{journal.stem}.generation-{record.generation}.json")
    if archive_kind == "oversized":
        archive.write_text("x" * (4 * 1024 * 1024 + 1), encoding="utf-8")
    elif archive_kind == "malformed":
        archive.write_text("{ not json", encoding="utf-8")
    else:
        archive.write_text("[]", encoding="utf-8")
    decision = wait_for_lifecycle_change(
        store,
        expected_generation=record.generation,
        after_revision=record.meaningfulRevision,
        timeout_seconds=1.0,
        clock=LifecycleWaitClock(poll_seconds=0.01),
    )
    assert decision.outcome == OUTCOME_WRONG_GENERATION


def test_wait_tool_is_registered_and_response_model_is_typed() -> None:

    assert "worktree_status_wait" in PUBLIC_TOOLS
    assert TOOL_RESPONSE_MODELS["worktree_status_wait"] is WorktreeStatusWaitResponse
    names = list(PUBLIC_TOOLS)
    assert names.index("worktree_status") + 1 == names.index("worktree_status_wait")
    model = WorktreeStatusWaitResponse.model_validate(
        {
            "ok": True,
            "operation": "worktree_status_wait",
            "outcome": "unchanged",
            "state": "unchanged",
            "contractPath": "/tmp/example/contract.md",
            "meaningfulRevision": 3,
            "timeoutSeconds": 0.0,
        }
    )
    assert model.operation == "worktree_status_wait"
    assert model.outcome == "unchanged"
