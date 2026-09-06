"""L1 mutation authority and Git reconciliation boundaries."""

from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.worktree_tools import (
    OperationControlRequest,
    worktree_operation_control_tool,
    worktree_status_tool,
)
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.git_command import run_git
from agents_remember.kernel.memory_ledger import (
    ledger_to_text,
    load_ledger,
    prepend_mapping,
    write_ledger,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.mutation_evidence import CloseoutMutationLeg
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
)
from agents_remember.worktrees.closeout_input import capture_closeout_candidate
from agents_remember.worktrees.integration.closeout import (
    ledger_recovery as closeout_ledger_recovery,
)
from agents_remember.worktrees.integration.closeout.initial_door_recovery import (
    classify_initial_closeout_door_recovery,
)
from agents_remember.worktrees.integration.closeout.ledger_recovery import (
    CloseoutLedgerRecoveryDecision,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.mutation_evidence import (
    JOURNALED_CLOSEOUT_REQUIRED,
    begin_exact_file_git_mutation,
    begin_git_mutation,
    bind_expected_output_tree,
    closeout_cancellable,
    prove_git_commit,
    reconcile_closeout_mutations,
)
from agents_remember.worktrees.modules import cli as worktree_cli
from agents_remember.worktrees.modules import closeout as closeout_module
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.queue.closeout_recovery import resume_external_commits
from agents_remember.worktrees.worktree_contract import load_contract
from closeout_fixture_test_support import (
    running_code_operation as _running_code_operation,
)
from closeout_fixture_test_support import selected_fixture
from closeout_input_test_support import (
    MutationEvidenceRecorder,
    closeout_operation_input,
    closeout_worktree_args,
    start_closeout_operation,
)
from test_closeout_certification_entrypoint import _review_and_declare
from test_closeout_queue import MASTER_A, QueueFixture
from test_worktree_support import git


def test_direct_closeout_apply_without_journal_authority_refuses_before_route_or_git(
    tmp_path: Path,
) -> None:
    fixture = selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    args = closeout_worktree_args(
        contract,
        approved=True,
        approval_note="approved",
    )

    with (
        mock.patch.object(closeout_module, "_closeout_contract") as contract_route,
        mock.patch.object(closeout_module, "_closeout_commit_phase") as commit_action,
        mock.patch.object(closeout_module, "require_closeout_mutation_authority") as claim,
        pytest.raises(CertificationContractError) as raised,
    ):
        closeout_module.closeout_result(args, contract)

    assert raised.value.findings == (
        {
            "code": "selected-closeout-operation-required",
            "path": str(contract.contract_path),
            "gateStarts": 0,
        },
    )
    contract_route.assert_not_called()
    commit_action.assert_not_called()
    claim.assert_not_called()


def test_generic_lifecycle_start_cannot_bypass_raw_closeout_admission(tmp_path: Path) -> None:
    fixture = selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    launcher = mock.Mock()

    with pytest.raises(RuntimeError, match="lease-bound raw-input admission"):
        lifecycle_operations.start_or_observe_operation(
            operation_input,
            contract,
            launcher=launcher,
        )

    launcher.assert_not_called()
    assert not operation_record_path(contract.worktree_group, "closeout").exists()


def test_lease_bound_closeout_start_proves_the_exact_claimed_door(tmp_path: Path) -> None:
    fixture = selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    expected_tree = capture_closeout_candidate(contract).candidate_tree
    projection = start_closeout_operation(operation_input, launcher=lambda *_: None)

    record = LifecycleOperationStore(
        operation_record_path(contract.worktree_group, "closeout")
    ).read()
    assert projection.status == "queued"
    assert record is not None
    assert record.candidateTree == expected_tree
    assert record.candidateState == closeout_contract_sha256(contract)
    assert record.doorPublication is not None
    assert record.doorPublication.state == "proven"
    assert record.doorPublication.generation.disposition == "claimed"
    assert record.doorPublication.generation.operationKind == "closeout"
    assert record.doorPublication.generation.operationFingerprint == record.fingerprint
    assert record.doorPublication.generation.claimedOperationKey == record.operationKey
    assert classify_initial_closeout_door_recovery(contract, record).state == "not-applicable"


def test_stale_unchanged_intent_observes_attempt_one_without_relaunch(tmp_path: Path) -> None:
    contract, operation_input, store, runtime = _running_code_operation(tmp_path)
    running = store.read()
    assert running is not None
    begin_git_mutation(
        WorktreeArgs(
            contract_path=contract.contract_path,
            closeout_input=operation_input.effectiveInput,
            operation_key=running.operationKey,
            operation_progress=runtime.progress,
        ),
        leg="code",
        repository=contract.code_worktree,
        expected_output_tree=None,
        use_current_candidate=True,
    )
    store.update(
        lambda record: record.model_copy(
            update={"heartbeatAt": "2026-08-22T00:00:00+00:00", "workerPid": None}
        )
    )
    launcher = mock.Mock()

    projection = start_closeout_operation(
        operation_input,
        launcher=launcher,
        now=datetime(2026, 8, 22, 1, 0, tzinfo=UTC),
    )

    reconciled = store.read()
    assert reconciled is not None
    assert reconciled.mutationEvidence["code"].state == "reconciled-unchanged"
    assert reconciled.attempt == 1
    launcher.assert_not_called()
    assert projection.cancellable is True


def test_git_mutation_status_failure_has_no_durable_progress(tmp_path: Path) -> None:
    contract, operation_input, store, runtime = _running_code_operation(tmp_path)
    running = store.read()
    assert running is not None
    git_marker = contract.code_worktree / ".git"
    git_marker.rename(contract.code_worktree / ".git-disabled")
    status = run_git(contract.code_worktree, ["status", "--porcelain=v1", "-z"])
    assert status.returncode != 0 and status.stderr.strip()
    before = store.path.read_bytes()

    with pytest.raises(RuntimeError) as raised:
        begin_git_mutation(
            WorktreeArgs(
                contract_path=contract.contract_path,
                closeout_input=operation_input.effectiveInput,
                operation_key=running.operationKey,
                operation_progress=runtime.progress,
            ),
            leg="code",
            repository=contract.code_worktree,
            expected_output_tree=None,
            use_current_candidate=True,
        )

    assert str(raised.value) == "Git status mutation evidence is unreadable"
    assert store.path.read_bytes() == before
    current = store.read()
    assert current is not None
    assert current.mutationEvidence["code"].state == "pre-mutation"


def test_git_mutation_ref_log_failure_has_no_durable_progress(tmp_path: Path) -> None:
    contract, operation_input, store, runtime = _running_code_operation(tmp_path)
    running = store.read()
    assert running is not None
    head_ref = git(contract.code_worktree, "symbolic-ref", "--quiet", "HEAD")
    ref_log = Path(
        git(
            contract.code_worktree,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            f"logs/{head_ref}",
        )
    )
    ref_log.unlink()
    before = store.path.read_bytes()

    with pytest.raises(RuntimeError) as raised:
        begin_git_mutation(
            WorktreeArgs(
                contract_path=contract.contract_path,
                closeout_input=operation_input.effectiveInput,
                operation_key=running.operationKey,
                operation_progress=runtime.progress,
            ),
            leg="code",
            repository=contract.code_worktree,
            expected_output_tree=None,
            use_current_candidate=True,
        )

    assert str(raised.value) == "Git ref-log mutation evidence is unreadable"
    assert store.path.read_bytes() == before
    current = store.read()
    assert current is not None
    assert current.mutationEvidence["code"].state == "pre-mutation"


def test_legacy_cli_apply_cannot_bypass_the_journaled_operation(tmp_path: Path) -> None:
    fixture = selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    args = Namespace(
        contract_path=contract.contract_path,
        approved=True,
        approval_note="approved",
        code_commit_message="code",
        memory_commit_message="memory",
        ledger_commit_message="ledger",
        dry_run=False,
        operation_progress=MutationEvidenceRecorder(),
    )

    with pytest.raises(RuntimeError) as raised:
        worktree_cli.command_closeout(args)

    assert str(raised.value) == JOURNALED_CLOSEOUT_REQUIRED


def test_git_mutation_helper_cannot_silently_run_without_evidence_authority() -> None:
    with pytest.raises(RuntimeError) as raised:
        begin_git_mutation(
            WorktreeArgs(contract_path=Path("/coordination/contract.md")),
            leg="code",
            repository=Path("/repository"),
            expected_output_tree="a" * 40,
        )

    assert str(raised.value) == JOURNALED_CLOSEOUT_REQUIRED


@pytest.mark.parametrize("case", ["disabled", "missing-contract", "foreign-repository"])
def test_git_mutation_authority_refuses_before_snapshot(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = selected_fixture(
        tmp_path,
        memory_mode="internal" if case == "disabled" else "external",
    )
    contract = fixture.contracts[MASTER_A]
    if case != "disabled":
        (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    leg: CloseoutMutationLeg = "memory" if case == "disabled" else "code"
    repository = contract.code_worktree
    contract_path = contract.contract_path
    if case == "missing-contract":
        contract_path = None
    elif case == "foreign-repository":
        repository = tmp_path / "foreign"
    args = WorktreeArgs(
        contract_path=contract_path,
        closeout_input=operation_input.effectiveInput,
        operation_progress=MutationEvidenceRecorder(),
    )

    expected = {
        "disabled": "closeout memory mutation leg is not enabled",
        "missing-contract": "closeout mutation evidence requires a contract path",
        "foreign-repository": "closeout code mutation repository is outside contract authority",
    }[case]
    with pytest.raises(RuntimeError) as raised:
        begin_git_mutation(
            args,
            leg=leg,
            repository=repository,
            expected_output_tree=None,
        )

    assert str(raised.value) == expected


def test_bind_and_proof_refuse_changed_or_incomplete_intent(tmp_path: Path) -> None:
    fixture = selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=operation_input.effectiveInput,
        operation_progress=MutationEvidenceRecorder(),
    )
    intent = begin_git_mutation(
        args,
        leg="code",
        repository=contract.code_worktree,
        expected_output_tree=None,
        use_current_candidate=True,
    )
    with pytest.raises(RuntimeError, match="only fill pending intent"):
        bind_expected_output_tree(args, intent, repository=contract.code_worktree)
    with pytest.raises(RuntimeError, match="changed after intent"):
        bind_expected_output_tree(
            args,
            intent.model_copy(update={"repository": (tmp_path / "foreign").as_posix()}),
            repository=contract.code_worktree,
        )
    pre_mutation = intent.model_copy(
        update={"state": "pre-mutation", "before": None, "expectedOutputTree": None}
    )
    with pytest.raises(RuntimeError, match="pre-command Git evidence"):
        prove_git_commit(
            args,
            pre_mutation,
            repository=contract.code_worktree,
            commit=git(contract.code_worktree, "rev-parse", "HEAD"),
        )
    with pytest.raises(RuntimeError, match="does not match its mutation-intent"):
        prove_git_commit(
            args,
            intent,
            repository=contract.code_worktree,
            commit=git(contract.code_worktree, "rev-parse", "HEAD"),
        )


@pytest.mark.parametrize("leg", ["code", "memory", "ledger"])
def test_reconciliation_distinguishes_unchanged_ambiguous_and_proven_output(
    tmp_path: Path,
    leg: CloseoutMutationLeg,
) -> None:
    unchanged = _intent_record(tmp_path / f"{leg}-unchanged", leg=leg, prepare_output=False)
    unchanged_result = reconcile_closeout_mutations(unchanged)
    assert unchanged_result[leg].state == "reconciled-unchanged"
    assert (
        reconcile_closeout_mutations(
            unchanged.model_copy(update={"mutationEvidence": unchanged_result})
        )
        == unchanged_result
    )
    assert closeout_cancellable(unchanged.model_copy(update={"mutationEvidence": unchanged_result}))

    ambiguous = _intent_record(tmp_path / f"{leg}-ambiguous", leg=leg, prepare_output=True)
    ambiguous_repo = Path(ambiguous.mutationEvidence[leg].repository)
    if leg == "code":
        # Selected admission already stages code. A later unknown index change, rather
        # than restaging those same bytes, supplies the genuinely ambiguous observation.
        (ambiguous_repo / "unproven-output.py").write_text("VALUE = 'unproven'\n")
    git(ambiguous_repo, "add", "-A")
    ambiguous_result = reconcile_closeout_mutations(ambiguous)
    assert ambiguous_result[leg].state == "mutation-intent"
    assert ambiguous_result[leg].observed is None
    assert not closeout_cancellable(ambiguous)

    proven = _intent_record(tmp_path / f"{leg}-proven", leg=leg, prepare_output=True)
    proven_repo = Path(proven.mutationEvidence[leg].repository)
    git(proven_repo, "add", "-A")
    git(proven_repo, "commit", "-m", "commit after published intent")
    proven_result = reconcile_closeout_mutations(proven)
    assert proven_result[leg].state == "commit-proven"
    assert proven_result[leg].commit == git(proven_repo, "rev-parse", "HEAD")
    assert not closeout_cancellable(proven.model_copy(update={"mutationEvidence": proven_result}))


def test_reconciliation_preserves_bound_output_after_exact_restore(tmp_path: Path) -> None:
    fixture = QueueFixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    assert contract.memory_worktree is not None and contract.ledger_path is not None
    repository = contract.memory_worktree
    git(repository, "add", "-A")
    git(repository, "commit", "-m", "prepare memory content before ledger")
    _review_and_declare(fixture)
    contract = fixture.contracts[MASTER_A]
    assert contract.memory_worktree is not None and contract.ledger_path is not None
    operation_input = closeout_operation_input(
        contract,
        config_path=fixture.config_path,
        approval_note="approved",
    )
    start_closeout_operation(
        operation_input,
        launcher=lambda *_: None,
    )
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    running = runtime.start()
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=operation_input.effectiveInput,
        operation_key=running.operationKey,
        operation_progress=runtime.progress,
    )
    intent = begin_git_mutation(
        args,
        leg="ledger",
        repository=repository,
        expected_output_tree=None,
    )
    contract.ledger_path.write_text(
        contract.ledger_path.read_text(encoding="utf-8") + "\n# prepared ledger output\n",
        encoding="utf-8",
    )
    git(repository, "add", "memory.md")
    bound = bind_expected_output_tree(args, intent, repository=repository)
    record = store.read()
    assert record is not None
    evidence = record.mutationEvidence["ledger"]
    assert evidence.before is not None
    assert evidence.expectedOutputTree is not None
    assert evidence == bound
    assert evidence.expectedOutputTree != evidence.before.headTree

    git(repository, "restore", "--staged", "--worktree", "memory.md")
    reconciled = reconcile_closeout_mutations(record)
    restored = reconciled["ledger"]

    assert restored.state == "reconciled-unchanged"
    assert restored.observed is not None
    assert restored.observed == evidence.before
    assert restored.expectedOutputTree == evidence.expectedOutputTree
    assert restored.expectedOutputTree != restored.observed.headTree
    assert closeout_cancellable(record.model_copy(update={"mutationEvidence": reconciled}))


@pytest.mark.parametrize("stage_intended", [False, True])
def test_public_recover_finishes_ordinary_external_ledger_precommit_intent(
    tmp_path: Path,
    stage_intended: bool,
) -> None:
    fixture = QueueFixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    assert contract.memory_worktree is not None and contract.ledger_path is not None
    (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(contract.code_worktree, "add", "-A")
    git(contract.code_worktree, "commit", "-m", "prepare accepted code")
    code_commit = git(contract.code_worktree, "rev-parse", "HEAD")
    _review_and_declare(fixture)
    contract = fixture.contracts[MASTER_A]
    assert contract.memory_worktree is not None and contract.ledger_path is not None
    operation_input = closeout_operation_input(
        contract,
        config_path=fixture.config_path,
        code="commit accepted code",
        memory="commit accepted memory",
        ledger="commit accepted ledger",
        approval_note="approved",
    )
    start_closeout_operation(
        operation_input,
        launcher=lambda *_: None,
    )
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    current = runtime.start()
    git(contract.memory_worktree, "add", "-A")
    if run_git(contract.memory_worktree, ["diff", "--cached", "--quiet"]).returncode != 0:
        git(contract.memory_worktree, "commit", "-m", "commit accepted memory")
    memory_commit = git(contract.memory_worktree, "rev-parse", "HEAD")
    runtime.progress(
        "memory-commit",
        {
            "current_command": "memory output proven before ledger",
            "recovery_commits": {
                "codeCommit": code_commit,
                "memoryContentCommit": memory_commit,
                "ledgerCommit": "",
            },
        },
    )
    staged_record = store.read()
    assert staged_record is not None
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=operation_input.effectiveInput,
        operation_key=current.operationKey,
        operation_progress=runtime.progress,
        recovery_commits=staged_record.recoveryCommits,
    )
    intended_ledger = prepend_mapping(
        load_ledger(contract.ledger_path),
        code_commit,
        memory_commit,
    )
    intent = begin_exact_file_git_mutation(
        args,
        leg="ledger",
        repository=contract.memory_worktree,
        path=contract.ledger_path,
        intended_text=ledger_to_text(intended_ledger),
    )
    assert intent.expectedOutputTree is not None
    write_ledger(contract.ledger_path, intended_ledger)
    if stage_intended:
        git(contract.memory_worktree, "add", "memory.md")
    runtime.fail(RuntimeError("cut after exact ledger write before commit"))
    interrupted = store.read()
    assert interrupted is not None and interrupted.status == "input-required"

    config = load_config(fixture.config_path)
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
    recover = next(row for row in projected["legalControls"] if row["action"] == "recover")

    def resume_ledger(_contract, requeued):
        resumed_runtime = lifecycle_operation_worker.OperationRuntime(store)
        accepted = resumed_runtime.start()
        resume_args = WorktreeArgs(
            contract_path=contract.contract_path,
            closeout_input=operation_input.effectiveInput,
            operation_key=accepted.operationKey,
            operation_progress=resumed_runtime.progress,
            recovery_commits=accepted.recoveryCommits,
        )
        resume_external_commits(
            contract,
            resume_args,
            operation_input.effectiveInput,
            code_commit=code_commit,
            memory_commit=memory_commit,
        )
        resumed_runtime.finish({"state": "closed"}, ok=True)

    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls.launch_detached_worker",
        side_effect=resume_ledger,
    ):
        completed = worktree_operation_control_tool(
            config,
            OperationControlRequest(**recover["arguments"]),
        )
    assert completed["ok"] is True
    final = store.read()
    assert final is not None and final.status == "completed"
    assert final.mutationEvidence["ledger"].state == "commit-proven"
    assert final.recoveryCommits is not None and final.recoveryCommits.ledgerCommit


def test_journal_before_ledger_write_retains_only_same_generation_recover(
    tmp_path: Path,
) -> None:
    (
        contract,
        operation_input,
        store,
        _runtime,
        interrupted,
        config,
        stale_cancel,
        stale_recover,
        code_commit,
        memory_commit,
    ) = _journal_before_ledger_write_cut(tmp_path)
    journal_before_stale_cancel = store.path.read_bytes()
    refused_cancel = worktree_operation_control_tool(
        config,
        OperationControlRequest(**stale_cancel["arguments"]),
    )
    assert refused_cancel["ok"] is False
    assert refused_cancel["nextAction"] == "recover"
    assert store.path.read_bytes() == journal_before_stale_cancel

    finish_ledger = _finish_pending_ledger(
        contract,
        operation_input,
        store,
        code_commit,
        memory_commit,
    )
    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls.launch_detached_worker",
        side_effect=finish_ledger,
    ) as launch:
        completed = worktree_operation_control_tool(
            config,
            OperationControlRequest(**stale_recover["arguments"]),
        )
    assert completed["ok"] is True
    launch.assert_called_once()
    final = store.read()
    assert final is not None and final.status == "completed"
    assert final.generation == interrupted.generation
    assert final.mutationEvidence["ledger"].state == "commit-proven"


def _journal_before_ledger_write_cut(tmp_path: Path):
    fixture = QueueFixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    assert contract.memory_worktree is not None and contract.ledger_path is not None
    (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    _review_and_declare(fixture)
    contract = fixture.contracts[MASTER_A]
    assert contract.memory_worktree is not None and contract.ledger_path is not None
    operation_input = closeout_operation_input(
        contract,
        config_path=fixture.config_path,
        approval_note="approved",
    )
    start_closeout_operation(
        operation_input,
        launcher=lambda *_: None,
    )
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    accepted = runtime.start()
    config = load_config(fixture.config_path)
    stale_cancel = next(
        row
        for row in _closeout_status_projection(config, contract)["legalControls"]
        if row["action"] == "cancel"
    )
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=operation_input.effectiveInput,
        operation_key=accepted.operationKey,
        operation_progress=runtime.progress,
    )
    code_commit, memory_commit = _prove_code_and_memory_output(contract, args)
    proven = store.read()
    assert proven is not None and proven.irreversibleBoundaryEntered
    assert proven.recoveryCommits is not None
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=operation_input.effectiveInput,
        operation_key=accepted.operationKey,
        operation_progress=runtime.progress,
        recovery_commits=proven.recoveryCommits,
    )
    intended_ledger = prepend_mapping(load_ledger(contract.ledger_path), code_commit, memory_commit)
    ledger_intent = begin_exact_file_git_mutation(
        args,
        leg="ledger",
        repository=contract.memory_worktree,
        path=contract.ledger_path,
        intended_text=ledger_to_text(intended_ledger),
    )
    runtime.fail(RuntimeError("cut after exact journal intent before ledger write"))

    interrupted = store.read()
    assert interrupted is not None
    assert interrupted.mutationEvidence["ledger"] == ledger_intent
    projected = _closeout_status_projection(config, contract)
    assert projected["generation"] == interrupted.generation
    assert [row["action"] for row in projected["legalControls"]] == ["recover"]
    stale_recover = projected["legalControls"][0]
    assert _closeout_status_projection(config, contract)["legalControls"] == [stale_recover]
    still_interrupted = store.read()
    assert still_interrupted is not None
    assert still_interrupted.mutationEvidence["ledger"] == ledger_intent
    return (
        contract,
        operation_input,
        store,
        runtime,
        interrupted,
        config,
        stale_cancel,
        stale_recover,
        code_commit,
        memory_commit,
    )


def _prove_code_and_memory_output(contract, args) -> tuple[str, str]:
    code_intent = begin_git_mutation(
        args,
        leg="code",
        repository=contract.code_worktree,
        expected_output_tree=None,
        use_current_candidate=True,
    )
    git(contract.code_worktree, "add", "-A")
    git(contract.code_worktree, "commit", "-m", "close accepted code")
    code_commit = git(contract.code_worktree, "rev-parse", "HEAD")
    prove_git_commit(args, code_intent, repository=contract.code_worktree, commit=code_commit)
    contract.memory_worktree.joinpath("accepted-memory.txt").write_text(
        "accepted memory output\n",
        encoding="utf-8",
    )
    memory_intent = begin_git_mutation(
        args,
        leg="memory",
        repository=contract.memory_worktree,
        expected_output_tree=None,
        use_current_candidate=True,
    )
    git(contract.memory_worktree, "add", "-A")
    git(contract.memory_worktree, "commit", "-m", "close accepted memory")
    memory_commit = git(contract.memory_worktree, "rev-parse", "HEAD")
    prove_git_commit(args, memory_intent, repository=contract.memory_worktree, commit=memory_commit)
    return code_commit, memory_commit


def _finish_pending_ledger(contract, operation_input, store, code_commit, memory_commit):
    def finish_ledger(_contract, requeued):
        resumed = lifecycle_operation_worker.OperationRuntime(store)
        current = resumed.start()
        resume_args = WorktreeArgs(
            contract_path=contract.contract_path,
            closeout_input=operation_input.effectiveInput,
            operation_key=current.operationKey,
            operation_progress=resumed.progress,
            recovery_commits=current.recoveryCommits,
        )
        resume_external_commits(
            contract,
            resume_args,
            operation_input.effectiveInput,
            code_commit=code_commit,
            memory_commit=memory_commit,
        )
        resumed.finish({"state": "closed"}, ok=True)

    return finish_ledger


@pytest.mark.parametrize("stage_intended", [False, True])
@pytest.mark.parametrize("live_state", ["third-bytes", "unreadable-git"])
def test_ordinary_ledger_third_bytes_block_fresh_and_stale_public_recover(
    tmp_path: Path,
    stage_intended: bool,
    live_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        contract,
        operation_input,
        store,
        runtime,
        args,
        intent,
        code_commit,
        memory_commit,
        config,
        stale_recover,
    ) = _ordinary_ledger_conflict_fixture(tmp_path, stage_intended)
    memory_worktree = contract.memory_worktree
    ledger_path = contract.ledger_path
    assert memory_worktree is not None and ledger_path is not None
    private_sentinel = _inject_ledger_conflict(
        contract,
        live_state,
        monkeypatch,
    )
    protected_head = git(memory_worktree, "rev-parse", "HEAD")
    protected_index = git(memory_worktree, "write-tree")
    protected_ledger = ledger_path.read_bytes()
    with pytest.raises(CloseoutLedgerRecoveryDecision) as protected:
        resume_external_commits(
            contract,
            args,
            operation_input.effectiveInput,
            code_commit=code_commit,
            memory_commit=memory_commit,
        )
    assert git(memory_worktree, "rev-parse", "HEAD") == protected_head
    assert git(memory_worktree, "write-tree") == protected_index
    assert ledger_path.read_bytes() == protected_ledger
    runtime.fail(protected.value)
    durable_decision = store.read()
    _assert_durable_ledger_decision(durable_decision, protected.value)
    record_before = store.path.read_bytes()
    head_before = git(memory_worktree, "rev-parse", "HEAD")
    index_before = git(memory_worktree, "write-tree")
    ledger_before = ledger_path.read_bytes()

    refused_status = _closeout_status_projection(config, contract)
    decision = _assert_ledger_conflict_projection(refused_status, intent, live_state)
    with mock.patch(
        "agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls.launch_detached_worker"
    ) as launch:
        refused = worktree_operation_control_tool(
            config,
            OperationControlRequest(**stale_recover["arguments"]),
        )
    _assert_stale_ledger_refusal(
        refused,
        decision,
        protected.value,
        durable_decision,
        private_sentinel,
    )
    launch.assert_not_called()
    assert store.path.read_bytes() == record_before
    assert git(memory_worktree, "rev-parse", "HEAD") == head_before
    assert git(memory_worktree, "write-tree") == index_before
    assert ledger_path.read_bytes() == ledger_before


def _ordinary_ledger_conflict_fixture(tmp_path: Path, stage_intended: bool):
    fixture = QueueFixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    assert contract.memory_worktree is not None and contract.ledger_path is not None
    (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(contract.code_worktree, "add", "-A")
    git(contract.code_worktree, "commit", "-m", "prepare accepted code")
    code_commit = git(contract.code_worktree, "rev-parse", "HEAD")
    _review_and_declare(fixture)
    contract = fixture.contracts[MASTER_A]
    assert contract.memory_worktree is not None and contract.ledger_path is not None
    operation_input = closeout_operation_input(
        contract,
        config_path=fixture.config_path,
        code="commit accepted code",
        memory="commit accepted memory",
        ledger="commit accepted ledger",
        approval_note="approved",
    )
    start_closeout_operation(
        operation_input,
        launcher=lambda *_: None,
    )
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    current = runtime.start()
    git(contract.memory_worktree, "add", "-A")
    if run_git(contract.memory_worktree, ["diff", "--cached", "--quiet"]).returncode != 0:
        git(contract.memory_worktree, "commit", "-m", "commit accepted memory")
    memory_commit = git(contract.memory_worktree, "rev-parse", "HEAD")
    runtime.progress(
        "memory-commit",
        {
            "current_command": "memory output proven before ledger",
            "recovery_commits": {
                "codeCommit": code_commit,
                "memoryContentCommit": memory_commit,
                "ledgerCommit": "",
            },
        },
    )
    accepted = store.read()
    assert accepted is not None
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=operation_input.effectiveInput,
        operation_key=current.operationKey,
        operation_progress=runtime.progress,
        recovery_commits=accepted.recoveryCommits,
    )
    intended_ledger = prepend_mapping(
        load_ledger(contract.ledger_path),
        code_commit,
        memory_commit,
    )
    intent = begin_exact_file_git_mutation(
        args,
        leg="ledger",
        repository=contract.memory_worktree,
        path=contract.ledger_path,
        intended_text=ledger_to_text(intended_ledger),
    )
    assert intent.expectedOutputTree is not None
    write_ledger(contract.ledger_path, intended_ledger)
    if stage_intended:
        git(contract.memory_worktree, "add", "memory.md")

    config = load_config(fixture.config_path)
    convergent = _closeout_status_projection(config, contract)
    stale_recover = next(row for row in convergent["legalControls"] if row["action"] == "recover")
    return (
        contract,
        operation_input,
        store,
        runtime,
        args,
        intent,
        code_commit,
        memory_commit,
        config,
        stale_recover,
    )


def _inject_ledger_conflict(contract, live_state: str, monkeypatch) -> str:
    private_sentinel = "PRIVATE-LEDGER-GIT-STDERR-/secret/path"
    if live_state == "third-bytes":
        contract.ledger_path.write_text("# unrelated same-path bytes\n", encoding="utf-8")
    else:

        def unreadable_git(_repository):
            raise OSError(private_sentinel)

        monkeypatch.setattr(
            closeout_ledger_recovery,
            "ephemeral_git_mutation_snapshot",
            unreadable_git,
        )
    return private_sentinel


def _assert_durable_ledger_decision(durable_decision, protected) -> None:
    assert durable_decision is not None
    assert durable_decision.result is not None
    assert durable_decision.result["state"] == "closeout-ledger-recovery-conflict"
    assert durable_decision.result["developerDecisionRequired"] is True
    assert durable_decision.result["expected"] == protected.classification.expected
    assert durable_decision.result["observed"] == protected.classification.observed


def _assert_ledger_conflict_projection(refused_status, intent, live_state: str):
    assert refused_status["legalControls"] == []
    decision = refused_status["result"]
    assert decision["nextAction"] == "developer-decision"
    assert decision["developerDecisionRequired"] is True
    assert decision["expected"]["intendedOutputTree"] == intent.expectedOutputTree
    if live_state == "third-bytes":
        assert decision["observed"]["ledgerSha256"]
    else:
        assert decision["observed"]["readFailure"]["errorType"] == "OSError"
    return decision


def _assert_stale_ledger_refusal(
    refused,
    decision,
    protected,
    durable_decision,
    private_sentinel: str,
) -> None:
    assert refused["ok"] is False
    assert refused["nextAction"] == "developer-decision"
    assert refused["expected"] == decision["expected"]
    assert refused["observed"] == decision["observed"]
    assert private_sentinel not in repr(
        [
            protected.classification.decision_payload(),
            durable_decision.result,
            decision,
            refused,
        ]
    )


def _closeout_status_projection(config: Any, contract: Any) -> dict[str, Any]:
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
    return next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")


def test_reconciliation_does_not_claim_an_unexpected_ref_tree(tmp_path: Path) -> None:
    record = _intent_record(tmp_path, leg="code", prepare_output=True)
    repository = Path(record.mutationEvidence["code"].repository)
    (repository / "unexpected.txt").write_text("different output\n", encoding="utf-8")
    git(repository, "add", "-A")
    git(repository, "commit", "-m", "unexpected commit after intent")

    reconciled = reconcile_closeout_mutations(record)

    assert reconciled["code"].state == "mutation-intent"
    assert reconciled["code"].commit is None
    assert reconciled["code"].observed is None


def test_reconciliation_detects_a_ref_that_moved_and_returned(tmp_path: Path) -> None:
    record = _intent_record(tmp_path, leg="code", prepare_output=True)
    evidence = record.mutationEvidence["code"]
    assert evidence.before is not None
    repository = Path(evidence.repository)
    candidate = repository / "feature.txt"
    candidate_bytes = candidate.read_bytes()
    git(repository, "add", "-A")
    git(repository, "commit", "-m", "transient output")
    git(repository, "reset", "--hard", evidence.before.head)
    candidate.write_bytes(candidate_bytes)

    reconciled = reconcile_closeout_mutations(record)

    assert reconciled["code"].state == "mutation-intent"
    assert reconciled["code"].observed is None


def test_reconciliation_records_an_unexpected_checked_out_ref(tmp_path: Path) -> None:
    record = _intent_record(tmp_path, leg="code", prepare_output=True)
    repository = Path(record.mutationEvidence["code"].repository)
    git(repository, "switch", "-c", "unexpected-recovery-ref")

    reconciled = reconcile_closeout_mutations(record)

    assert reconciled["code"].state == "mutation-intent"
    assert reconciled["code"].observed is None


def test_one_unchanged_leg_cannot_hide_another_legs_proven_commit(tmp_path: Path) -> None:
    record = _intent_record(tmp_path, leg="memory", prepare_output=False)
    contract = load_contract(Path(record.contractPath))
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    accepted = record.input
    assert isinstance(accepted, CloseoutOperationInput)
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=accepted.effectiveInput,
        operation_key=record.operationKey,
        operation_progress=runtime.progress,
    )
    code_intent = begin_git_mutation(
        args,
        leg="code",
        repository=contract.code_worktree,
        expected_output_tree=None,
        use_current_candidate=True,
    )
    git(contract.code_worktree, "add", "-A")
    git(contract.code_worktree, "commit", "-m", accepted.effectiveInput.message_for("code"))
    prove_git_commit(
        args,
        code_intent,
        repository=contract.code_worktree,
        commit=git(contract.code_worktree, "rev-parse", "HEAD"),
    )
    current = store.read()
    assert current is not None
    reconciled = reconcile_closeout_mutations(current)
    runtime.progress(
        "memory-commit",
        {"mutation_evidence": reconciled["memory"].model_dump(mode="json")},
    )
    runtime.fail(RuntimeError("cut after mixed Git outcomes"))

    launches: list[int] = []
    observed = start_closeout_operation(
        accepted,
        launcher=lambda _contract, recovered: launches.append(recovered.attempt),
    )

    assert observed.status == "queued"
    assert launches == [2]


def test_reconciliation_refuses_a_repository_outside_contract_authority(
    tmp_path: Path,
) -> None:
    record = _intent_record(tmp_path, leg="code", prepare_output=False)
    evidence = dict(record.mutationEvidence)
    evidence["code"] = evidence["code"].model_copy(
        update={"repository": (tmp_path / "outside").as_posix()}
    )
    forged = record.model_copy(update={"mutationEvidence": evidence})

    with pytest.raises(RuntimeError, match="outside contract authority"):
        reconcile_closeout_mutations(forged)


def test_reconciliation_keeps_intent_when_authorized_repository_is_unreadable(
    tmp_path: Path,
) -> None:
    record = _intent_record(tmp_path, leg="code", prepare_output=False)
    repository = Path(record.mutationEvidence["code"].repository)
    repository.rename(tmp_path / "temporarily-moved-worktree")

    reconciled = reconcile_closeout_mutations(record)

    assert reconciled == record.mutationEvidence


def _intent_record(root: Path, *, leg: CloseoutMutationLeg, prepare_output: bool):
    fixture = QueueFixture(
        root,
        memory_mode="internal" if leg == "code" else "external",
    )
    contract = fixture.contracts[MASTER_A]
    repository = contract.code_worktree
    if leg != "code":
        assert contract.memory_worktree is not None
        repository = contract.memory_worktree
    if leg == "ledger":
        git(repository, "add", "-A")
        git(repository, "commit", "-m", "prepare memory content before ledger")
    _review_and_declare(fixture)
    contract = fixture.contracts[MASTER_A]
    if fixture.memory_mode == "external":
        assert contract.memory_worktree is not None
        assert contract.ledger_path is not None
    operation_input = closeout_operation_input(
        contract,
        config_path=fixture.config_path,
        code="commit intent",
        approval_note="approved",
    )
    start_closeout_operation(
        operation_input,
        launcher=lambda *_: None,
    )
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    runtime.start()
    running = store.read()
    assert running is not None
    intent = begin_git_mutation(
        WorktreeArgs(
            contract_path=contract.contract_path,
            closeout_input=operation_input.effectiveInput,
            operation_key=running.operationKey,
            operation_progress=runtime.progress,
        ),
        leg=leg,
        repository=repository,
        expected_output_tree=None,
        use_current_candidate=leg != "ledger",
    )
    if leg == "ledger" and prepare_output:
        assert contract.ledger_path is not None
        contract.ledger_path.write_text(
            contract.ledger_path.read_text(encoding="utf-8") + "\n# prepared ledger output\n",
            encoding="utf-8",
        )
        git(repository, "add", "memory.md")
        intent = bind_expected_output_tree(
            WorktreeArgs(
                contract_path=contract.contract_path,
                closeout_input=operation_input.effectiveInput,
                operation_key=running.operationKey,
                operation_progress=runtime.progress,
            ),
            intent,
            repository=repository,
        )
        assert intent is not None
    record = store.read()
    assert record is not None
    return record
