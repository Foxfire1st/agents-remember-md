"""Validated closeout input, mutation evidence, and operation-model refusals."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from agents_remember.models.closeout.input import (
    CloseoutCorrectedCall,
    EffectiveCloseoutInput,
    EnabledCloseoutLeg,
    NotApplicableCloseoutLeg,
)
from agents_remember.models.lifecycles.mutation_evidence import (
    GitMutationEvidence,
    GitMutationSnapshot,
)
from agents_remember.models.lifecycles.operation import (
    IntegrateOperationInput,
    LifecycleOperationRecord,
)
from agents_remember.worktrees import closeout_input
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationReadError,
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from closeout_fixture_test_support import selected_fixture
from closeout_input_test_support import (
    closeout_operation_input,
    start_closeout_operation,
    with_commit_proven,
)
from pydantic import ValidationError
from selected_lifecycle_test_support import (
    completed_selected_closeout_for_integration,
    selected_contract,
)
from test_closeout_queue import MASTER_A


def _snapshot(seed: str = "a") -> GitMutationSnapshot:
    return GitMutationSnapshot(
        headRef="refs/heads/test-closeout",
        head=seed * 40,
        headTree="b" * 40,
        refLogFingerprint="c" * 64,
        indexTree="b" * 40,
        candidateTree="d" * 40,
        statusFingerprint="e" * 64,
    )


def _external_record(tmp_path: Path) -> LifecycleOperationRecord:
    fixture = selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    record = store.read()
    assert record is not None
    return record


@pytest.mark.parametrize("message", ["", "   ", " padded "])
def test_enabled_message_model_refuses_blank_or_unstripped_text(message: str) -> None:
    with pytest.raises(ValidationError):
        EnabledCloseoutLeg.model_validate({"reason": "enabled", "message": message})


def test_not_applicable_leg_has_no_message_authority() -> None:
    effective = EffectiveCloseoutInput(
        route="worktree",
        contractKind="leaf",
        memoryMode="internal",
        code=NotApplicableCloseoutLeg(reason="clean"),
        memory=NotApplicableCloseoutLeg(reason="internal"),
        ledger=NotApplicableCloseoutLeg(reason="internal"),
    )
    with pytest.raises(RuntimeError, match="not applicable"):
        effective.message_for("code")


def test_normalizer_and_consumer_share_exact_plan_identity(tmp_path: Path) -> None:
    fixture = selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    plan = closeout_input.resolved_plan_from_effective_input(operation_input.effectiveInput)
    forged = plan.model_copy(update={"route": "direct-landing"})
    corrected = CloseoutCorrectedCall(tool="worktree_closeout_apply", arguments={})
    with pytest.raises(closeout_input.CloseoutInputError, match="closeout-input-invalid"):
        closeout_input.normalize_closeout_input(
            contract,
            closeout_input.raw_closeout_messages(code="code", memory="memory", ledger="ledger"),
            route="worktree",
            corrected_call=corrected,
            resolved_plan=forged,
        )
    with pytest.raises(closeout_input.CloseoutInputError, match="closeout-input-invalid"):
        closeout_input.require_effective_closeout_plan(
            replace(contract, memory_mode="internal"),
            operation_input.effectiveInput,
            route="worktree",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"state": "mutation-intent"},
        {"state": "reconciled-unchanged", "before": _snapshot()},
        {"state": "mutation-intent", "before": _snapshot(), "observed": _snapshot()},
        {
            "state": "mutation-intent",
            "before": _snapshot(),
            "observed": _snapshot("f"),
            "commit": "f" * 40,
        },
    ],
)
def test_mutation_evidence_refuses_incomplete_state_facts(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GitMutationEvidence.model_validate({"leg": "code", "repository": "/code", **payload})


def test_reconciled_unchanged_evidence_cannot_name_a_commit() -> None:
    before = _snapshot()

    with pytest.raises(
        ValidationError,
        match="reconciled-unchanged evidence cannot name a commit",
    ):
        GitMutationEvidence.model_validate(
            {
                "leg": "code",
                "repository": "/code",
                "state": "reconciled-unchanged",
                "before": before,
                "observed": before,
                "expectedOutputTree": "f" * 40,
                "commit": "a" * 40,
            }
        )


def test_mutation_evidence_refuses_impossible_prestate_and_commit_proof() -> None:
    invalid = [
        {
            "leg": "code",
            "repository": "/code",
            "expectedOutputTree": "a" * 40,
        },
        {
            "leg": "code",
            "repository": "/code",
            "state": "commit-proven",
            "before": _snapshot(),
        },
        {
            "leg": "code",
            "repository": "/code",
            "state": "commit-proven",
            "before": _snapshot(),
            "observed": _snapshot().model_copy(update={"headRef": "refs/heads/other"}),
            "expectedOutputTree": "b" * 40,
            "commit": "a" * 40,
        },
    ]
    for payload in invalid:
        with pytest.raises(ValidationError):
            GitMutationEvidence.model_validate(payload)


def test_operation_model_refuses_cross_kind_authority_and_results(tmp_path: Path) -> None:
    closeout = _external_record(tmp_path / "closeout")
    contract = completed_selected_closeout_for_integration(
        selected_contract(tmp_path / "integrate")
    )
    integrate_input = IntegrateOperationInput(
        configPath=(contract.code_repo_path.parent / "settings.json").as_posix(),
        contractPath=contract.contract_path.as_posix(),
    )
    start_or_observe_operation(integrate_input, contract, launcher=lambda *_: None)
    integrate_store = LifecycleOperationStore(
        operation_record_path(contract.worktree_group, "integrate")
    )
    integrate = integrate_store.read()
    assert integrate is not None and integrate.integrationAuthority is not None
    cases = [
        ({**integrate.model_dump(), "integrationAuthority": None}, "requires exact"),
        (
            {**closeout.model_dump(), "integrationAuthority": integrate.integrationAuthority},
            "only integrate operations may carry integrationAuthority",
        ),
        ({**integrate.model_dump(), "closeoutFinalizedContractSha256": "a" * 64}, "belongs"),
        (
            {
                **closeout.model_dump(),
                "result": {"state": "organizational-completion-gate-failed"},
            },
            "quality failure belongs",
        ),
        (
            {**integrate.model_dump(), "mutationEvidence": closeout.mutationEvidence},
            "cannot carry closeout mutation evidence",
        ),
    ]
    for payload, message in cases:
        with pytest.raises(ValidationError, match=message):
            LifecycleOperationRecord.model_validate(payload)


def test_operation_model_refuses_closeout_input_and_evidence_mismatches(tmp_path: Path) -> None:
    record = _external_record(tmp_path)
    integrate_input = IntegrateOperationInput(
        configPath=record.input.configPath,
        contractPath=record.input.contractPath,
    )
    payloads = [
        (
            {**record.model_dump(), "input": integrate_input.model_dump()},
            "lifecycle operation kind must equal its accepted input kind",
        ),
        ({**record.model_dump(), "mutationEvidence": {}}, "match every enabled"),
        ({**record.model_dump(), "irreversibleBoundaryEntered": True}, "derived from"),
    ]
    for payload, message in payloads:
        with pytest.raises(ValidationError, match=message):
            LifecycleOperationRecord.model_validate(payload)

    proven = with_commit_proven(record, leg="code")
    payload = proven.model_dump()
    payload["recoveryCommits"]["codeCommit"] = "9" * 40
    with pytest.raises(ValidationError, match="contradicts commit-proven"):
        LifecycleOperationRecord.model_validate(payload)


def test_public_closeout_admission_refuses_a_non_closeout_journal(tmp_path: Path) -> None:
    contract = completed_selected_closeout_for_integration(selected_contract(tmp_path))
    integrate_input = IntegrateOperationInput(
        configPath=(contract.code_repo_path.parent / "settings.json").as_posix(),
        contractPath=contract.contract_path.as_posix(),
    )
    start_or_observe_operation(integrate_input, contract, launcher=lambda *_: None)
    integrate_store = LifecycleOperationStore(
        operation_record_path(contract.worktree_group, "integrate")
    )
    integrate = integrate_store.read()
    assert integrate is not None
    integrate_store.path.unlink()
    closeout_store = LifecycleOperationStore(
        operation_record_path(contract.worktree_group, "closeout")
    )
    with pytest.raises(LifecycleOperationReadError):
        closeout_store.create(integrate)
    closeout_store.path.parent.mkdir(parents=True, exist_ok=True)
    closeout_store.path.write_text(
        integrate.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(LifecycleOperationReadError) as raised:
        start_closeout_operation(
            closeout_operation_input(contract),
            launcher=lambda *_: None,
        )
    assert raised.value.expected["operationKind"] == "closeout"
    assert raised.value.observed == {
        "state": "operation-kind-mismatch",
        "operationKind": "integrate",
    }
