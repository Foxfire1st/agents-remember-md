"""L1: validation ordering and no-op finalization generation ownership."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.application import worktree_tools
from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.kernel.memory_ledger import find_mapping, load_ledger
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.operation import IntegrateOperationInput
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    closeout_generation_retained,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
    operation_state_fingerprint,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    operation_projection,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from agents_remember.worktrees.integration.lifecycle.worker import launch as lifecycle_worker_launch
from agents_remember.worktrees.integration.mutation_evidence import (
    begin_git_mutation,
    prove_git_commit,
)
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.git import branch_commit, head_commit
from agents_remember.worktrees.worktree_contract import (
    load_contract,
    write_contract,
)
from closeout_input_test_support import (
    closeout_operation_input,
    start_closeout_operation,
)
from integration_branch_authority_test_support import (
    _authority_fixture,
    _closed_external_leaf_worktrees,
)
from test_closeout_input_boundary import _bytes_under, _git_facts
from test_lifecycle_operations import _contract


@pytest.mark.parametrize(
    ("value", "observation"),
    [(None, "omitted"), ("", "empty"), (" \n ", "whitespace-only")],
)
@pytest.mark.parametrize("field", ["code", "memory", "ledger"])
def test_invalid_closeout_precedes_active_integrate_decision_without_authority(
    tmp_path: Path,
    field: str,
    value: str | None,
    observation: str,
) -> None:
    root = tmp_path / f"{field}-{observation}"
    fixture = _authority_fixture(root, external_memory=True)
    contract = _closed_external_leaf_worktrees(fixture, root)
    integration = IntegrateOperationInput(
        configPath=fixture.config_path.as_posix(),
        contractPath=contract.contract_path.as_posix(),
    )
    start_or_observe_operation(integration, contract, launcher=lambda *_: None)
    integrate_path = operation_record_path(contract.worktree_group, "integrate")
    closeout_path = operation_record_path(contract.worktree_group, "closeout")
    if field == "code":
        (contract.code_worktree / "new-closeout-candidate.py").write_text(
            "VALUE = 'dirty while integrate owns lifecycle'\n",
            encoding="utf-8",
        )
    messages: dict[str, str | None] = {
        "code": "close current code candidate",
        "memory": "record external memory",
        "ledger": "record ledger mapping",
    }
    messages[field] = value
    before_contract = contract.contract_path.read_bytes()
    before_integrate = integrate_path.read_bytes()
    before_closeout = closeout_path.read_bytes()
    before_coordination = _bytes_under(fixture.coordination)
    before_code = _git_facts(contract.code_worktree)
    assert contract.memory_worktree is not None
    before_memory = _git_facts(contract.memory_worktree)

    with mock.patch.object(lifecycle_worker_launch, "launch_or_fail") as launch:
        refused = worktree_tools.worktree_closeout_apply_tool(
            load_config(fixture.config_path),
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(**messages),
            worktree_tools.CloseoutApproval(intent_note="approve exact closeout"),
        )

    assert refused["status"] == "closeout-input-invalid"
    assert refused["invalidFields"] == [
        {
            "field": f"{field}_commit_message",
            "leg": field,
            "observation": observation,
            "code": f"enabled-{field}-message-required",
        }
    ]
    assert refused["resolvedPlan"][field]["state"] == "enabled"
    assert refused["correctedCall"]["arguments"][f"{field}_commit_message"] == (
        f"<nonblank {field} commit message>"
    )
    launch.assert_not_called()
    assert closeout_path.read_bytes() == before_closeout
    assert integrate_path.read_bytes() == before_integrate
    assert contract.contract_path.read_bytes() == before_contract
    assert _bytes_under(fixture.coordination) == before_coordination
    assert _git_facts(contract.code_worktree) == before_code
    assert _git_facts(contract.memory_worktree) == before_memory


def test_valid_closeout_conflicts_with_active_integrate_only_after_validation(
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
    closeout_path = operation_record_path(contract.worktree_group, "closeout")
    before_closeout = closeout_path.read_bytes()

    with pytest.raises(RuntimeError, match=r"closeout cannot proceed.*integrate"):
        worktree_tools.worktree_closeout_apply_tool(
            load_config(fixture.config_path),
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(
                memory="close external memory",
                ledger="record code-to-memory mapping",
            ),
            worktree_tools.CloseoutApproval(intent_note="approved closed integration fixture"),
        )

    assert closeout_path.read_bytes() == before_closeout


@pytest.mark.parametrize(
    "case",
    ["ordinary-internal", "series", "external-mapped"],
)
@pytest.mark.parametrize("terminal", [False, True], ids=["running-cut", "completed-cut"])
def test_noop_finalization_retry_observes_the_same_generation(
    tmp_path: Path,
    case: str,
    terminal: bool,
) -> None:
    contract, config_path, commits = _generation_case(tmp_path / case, case)
    operation_input = closeout_operation_input(contract, config_path=config_path)
    runtime, store, finalized = _publish_noop_finalization(
        contract,
        operation_input,
        commits,
        terminal=terminal,
    )
    accepted = store.read()
    assert accepted is not None
    before = store.path.read_bytes()
    launcher = mock.Mock()

    observed = start_closeout_operation(operation_input, launcher=launcher)

    current = store.read()
    assert current is not None
    assert observed.status == ("completed" if terminal else "running")
    assert observed.cancellable is False
    assert current.operationKey == accepted.operationKey
    assert current.fingerprint == accepted.fingerprint
    assert current.attempt == 1
    assert current.irreversibleBoundaryEntered is False
    assert all(item.state == "pre-mutation" for item in current.mutationEvidence.values())
    assert current.closeoutFinalizedContractSha256 == (closeout_contract_sha256(finalized))
    assert hashlib.sha256(contract.contract_path.read_bytes()).hexdigest() == (
        current.closeoutFinalizedContractSha256
    )
    assert store.path.read_bytes() == before
    launcher.assert_not_called()
    assert runtime.store.path == store.path


@pytest.mark.parametrize("case", ["external-mapped"])
def test_invalid_duplicate_cannot_observe_noop_finalized_external_generation(
    tmp_path: Path,
    case: str,
) -> None:
    contract, config_path, commits = _generation_case(tmp_path, case)
    operation_input = closeout_operation_input(contract, config_path=config_path)
    _, store, _ = _publish_noop_finalization(
        contract,
        operation_input,
        commits,
        terminal=True,
    )
    before = store.path.read_bytes()

    refused = worktree_tools.worktree_closeout_apply_tool(
        load_config(Path(config_path)),
        contract.contract_path.as_posix(),
        worktree_tools.CloseoutCommitMessages(
            memory=" ",
            ledger=operation_input.effectiveInput.message_for("ledger"),
        ),
        worktree_tools.CloseoutApproval(intent_note=operation_input.approvalNote),
    )

    assert refused["status"] == "closeout-input-invalid"
    assert refused["invalidFields"][0]["field"] == "memory_commit_message"
    assert store.path.read_bytes() == before


def test_failure_after_noop_contract_finalization_recovers_the_same_generation(
    tmp_path: Path,
) -> None:
    contract, config_path, commits = _generation_case(tmp_path, "ordinary-internal")
    operation_input = closeout_operation_input(contract, config_path=config_path)
    runtime, store, _ = _publish_noop_finalization(
        contract,
        operation_input,
        commits,
        terminal=False,
    )
    runtime.fail(RuntimeError("cut after contract write before terminal worker finish"))
    failed = store.read()
    assert failed is not None
    assert failed.status == "input-required"
    assert failed.irreversibleBoundaryEntered is False
    launches: list[int] = []

    observed = start_closeout_operation(
        operation_input,
        launcher=lambda _, record: launches.append(record.attempt),
    )

    recovered = store.read()
    assert recovered is not None
    assert observed.status == "queued"
    assert recovered.operationKey == failed.operationKey
    assert recovered.attempt == 2
    assert launches == [2]


def test_retained_generation_stays_recoverable_when_worker_relaunch_fails(
    tmp_path: Path,
) -> None:
    contract, config_path, commits = _generation_case(tmp_path, "ordinary-internal")
    operation_input = closeout_operation_input(contract, config_path=config_path)
    runtime, store, _ = _publish_noop_finalization(
        contract,
        operation_input,
        commits,
        terminal=False,
    )
    runtime.fail(RuntimeError("cut before terminal worker finish"))

    private_launch = "PRIVATE_LAUNCH_STDERR_SENTINEL /tmp/native-runner"
    with pytest.raises(RuntimeError, match="lifecycle-worker-launch-failed") as raised:
        start_closeout_operation(
            operation_input,
            launcher=lambda *_: (_ for _ in ()).throw(RuntimeError(private_launch)),
        )
    assert private_launch not in str(raised.value)

    retained = store.read()
    assert retained is not None
    assert retained.status == "input-required"
    assert retained.phase == "contract-finalization"
    assert retained.attempt == 2
    assert retained.finishedAt is None
    assert retained.failure == "detached lifecycle worker could not start"
    assert private_launch not in str(retained.model_dump(mode="json"))
    assert retained.result["failureEvidence"]["errorType"] == "RuntimeError"  # type: ignore[index]


def test_recovery_cells_without_exact_finalization_state_do_not_retain_generation(
    tmp_path: Path,
) -> None:
    contract, config_path, commits = _generation_case(tmp_path, "ordinary-internal")
    operation_input = closeout_operation_input(contract, config_path=config_path)
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    runtime.start()
    runtime.progress(
        "approval-claim",
        {"current_command": "claim closeout approval", "approval_claimed": True},
    )
    runtime.progress(
        "code-commit",
        {
            "current_command": "verified-existing code commit",
            "recovery_commits": commits,
        },
    )

    recorded = store.read()
    assert recorded is not None and recorded.recoveryCommits is not None
    assert recorded.closeoutFinalizedContractSha256 is None
    assert recorded.irreversibleBoundaryEntered is False
    assert closeout_generation_retained(recorded) is False
    # L18: cancel is a task-addressed control derived from the contract; the
    # no-contract projection no longer advertises it (see _operation_cancellable).
    assert operation_projection(recorded, contract=contract).cancellable is True


def test_unrelated_contract_advancement_cannot_replace_completed_unintegrated_generation(
    tmp_path: Path,
) -> None:
    contract, config_path, commits = _generation_case(tmp_path, "ordinary-internal")
    operation_input = closeout_operation_input(contract, config_path=config_path)
    _, store, finalized = _publish_noop_finalization(
        contract,
        operation_input,
        commits,
        terminal=True,
    )
    first = store.read()
    assert first is not None
    advanced = replace(
        finalized,
        sync_log=(
            {
                "at": "2026-08-22T00:00:00+00:00",
                "codeBaseFrom": finalized.code_base_commit,
                "codeBaseTo": finalized.code_base_commit,
                "memoryBaseFrom": finalized.memory_base_commit,
                "memoryBaseTo": finalized.memory_base_commit,
                "code": "already-current",
                "memory": "no-external-memory",
            },
        ),
    )
    assert operation_state_fingerprint(advanced) == operation_state_fingerprint(finalized)
    assert closeout_contract_sha256(advanced) != closeout_contract_sha256(finalized)
    write_contract(advanced.contract_path, advanced)
    before = store.path.read_bytes()
    launcher = mock.Mock()

    with pytest.raises(RuntimeError, match="closeout-door-claim-owner-conflict"):
        start_closeout_operation(
            closeout_operation_input(advanced, config_path=config_path),
            launcher=launcher,
        )

    assert store.path.read_bytes() == before
    launcher.assert_not_called()


def test_candidate_advancement_cannot_replace_completed_unintegrated_generation(
    tmp_path: Path,
) -> None:
    contract, config_path, commits = _generation_case(tmp_path, "ordinary-internal")
    operation_input = closeout_operation_input(contract, config_path=config_path)
    _, store, finalized = _publish_noop_finalization(
        contract,
        operation_input,
        commits,
        terminal=True,
    )
    first = store.read()
    assert first is not None
    (finalized.code_worktree / "next-generation.py").write_text(
        "VALUE = 'new candidate'\n",
        encoding="utf-8",
    )
    advanced = load_contract(finalized.contract_path)
    before = store.path.read_bytes()
    launcher = mock.Mock()

    with pytest.raises(RuntimeError, match="closeout-door-claim-owner-conflict"):
        start_closeout_operation(
            closeout_operation_input(
                advanced,
                config_path=config_path,
                code="close the next generation",
            ),
            launcher=launcher,
        )

    assert store.path.read_bytes() == before
    launcher.assert_not_called()


def test_completed_mutated_generation_retains_intent_and_requires_explicit_disposition(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path)
    operation_input, store, finalized = _publish_mutated_code_generation(contract)

    observed = start_closeout_operation(operation_input, launcher=lambda *_: None)
    assert observed.status == "completed"
    changed_effective = operation_input.effectiveInput.model_copy(
        update={
            "code": operation_input.effectiveInput.code.model_copy(
                update={"message": "different closeout message"}
            )
        }
    )
    with pytest.raises(RuntimeError, match="conflicting closeout intent"):
        start_closeout_operation(
            operation_input.model_copy(update={"effectiveInput": changed_effective}),
            launcher=lambda *_: None,
        )

    advanced = replace(
        finalized,
        sync_log=(_sync_log_entry(finalized),),
    )
    write_contract(advanced.contract_path, advanced)
    before = store.path.read_bytes()
    launcher = mock.Mock()

    with pytest.raises(RuntimeError, match="closeout-door-claim-owner-conflict"):
        start_closeout_operation(
            closeout_operation_input(advanced),
            launcher=launcher,
        )

    assert store.path.read_bytes() == before
    launcher.assert_not_called()


def _publish_mutated_code_generation(contract):
    candidate = contract.code_worktree / "retained-generation.txt"
    candidate.write_text("retained\n", encoding="utf-8")
    operation_input = closeout_operation_input(contract, code="close retained generation")
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    running = runtime.start()
    runtime.progress("approval-claim", {"approval_claimed": True})
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=operation_input.effectiveInput,
        operation_key=running.operationKey,
        operation_progress=runtime.progress,
    )
    intent = begin_git_mutation(
        args,
        leg="code",
        repository=contract.code_worktree,
        expected_output_tree=None,
        use_current_candidate=True,
    )
    _git(contract.code_worktree, "add", "-A")
    _git(contract.code_worktree, "commit", "-m", "close retained generation")
    code_commit = _git(contract.code_worktree, "rev-parse", "HEAD")
    prove_git_commit(args, intent, repository=contract.code_worktree, commit=code_commit)
    finalized = replace(
        load_contract(contract.contract_path),
        approved_for_commit=True,
        commit_approval_note=operation_input.approvalNote,
        human_review_status="approved",
        closeout_status="completed",
        code_commit=code_commit,
    )
    runtime.progress(
        "contract-finalization",
        {"closeout_finalized_contract_sha256": closeout_contract_sha256(finalized)},
    )
    write_contract(finalized.contract_path, finalized)
    runtime.finish({"state": "closed"}, ok=True)
    return operation_input, store, finalized


def _sync_log_entry(contract) -> dict[str, str]:
    return {
        "at": "2026-08-12T12:03:00+00:00",
        "codeBaseFrom": contract.code_base_commit,
        "codeBaseTo": contract.code_base_commit,
        "memoryBaseFrom": "",
        "memoryBaseTo": "",
        "code": "already-current",
        "memory": "no-external-memory",
    }


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _generation_case(root: Path, case: str):
    if case == "ordinary-internal":
        contract = _contract(root)
        return (
            contract,
            contract.code_repo_path.parent / "settings.json",
            {
                "codeCommit": head_commit(contract.code_worktree),
                "memoryContentCommit": "",
                "ledgerCommit": "",
            },
        )
    fixture = _authority_fixture(root, external_memory=True)
    if case == "series":
        contract = fixture.master_contract
        code_commit = branch_commit(contract.code_repo_path, contract.code_work_branch)
        assert contract.ledger_path is not None and contract.memory_repo_path is not None
        mapping = find_mapping(load_ledger(contract.ledger_path), code_commit)
        assert mapping is not None
        return (
            contract,
            fixture.config_path,
            {
                "codeCommit": code_commit,
                "memoryContentCommit": mapping.memory_commit,
                "ledgerCommit": branch_commit(
                    contract.memory_repo_path,
                    contract.memory_work_branch,
                ),
            },
        )
    if case == "external-mapped":
        closed = _closed_external_leaf_worktrees(
            fixture,
            root,
            publish_closeout_evidence=False,
        )
        contract = replace(
            closed,
            closeout_status="not-started",
            code_commit="",
            memory_content_commit="",
            ledger_commit="",
            approved_for_commit=False,
            human_review_status="pending-review",
            commit_approval_note="",
        )
        write_contract(contract.contract_path, contract)
        return (
            contract,
            fixture.config_path,
            {
                "codeCommit": head_commit(contract.code_worktree),
                "memoryContentCommit": closed.memory_content_commit,
                "ledgerCommit": closed.ledger_commit,
            },
        )
    raise AssertionError(f"unknown generation case: {case}")


def _publish_noop_finalization(
    contract,
    operation_input,
    commits: dict[str, str],
    *,
    terminal: bool,
):
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    runtime.start()
    runtime.progress(
        "approval-claim",
        {"current_command": "claim closeout approval", "approval_claimed": True},
    )
    finalized = replace(
        load_contract(contract.contract_path),
        approved_for_commit=True,
        commit_approval_note=operation_input.approvalNote,
        human_review_status="approved",
        closeout_status="completed",
        code_commit=commits["codeCommit"],
        memory_content_commit=commits["memoryContentCommit"],
        ledger_commit=commits["ledgerCommit"],
    )
    prewrite_contract = finalized.contract_path.read_bytes()
    runtime.progress(
        "contract-finalization",
        {
            "current_command": "finalize closeout contract edge",
            "recovery_commits": commits,
            "closeout_finalized_contract_sha256": closeout_contract_sha256(finalized),
        },
    )
    journaled = store.read()
    assert journaled is not None
    expected_hash = closeout_contract_sha256(finalized)
    assert journaled.closeoutFinalizedContractSha256 == expected_hash
    assert finalized.contract_path.read_bytes() == prewrite_contract
    write_contract(finalized.contract_path, finalized)
    assert hashlib.sha256(finalized.contract_path.read_bytes()).hexdigest() == expected_hash
    if terminal:
        runtime.finish(
            {
                "state": "closed",
                "code_commit": commits["codeCommit"],
                "memory_content_commit": commits["memoryContentCommit"],
                "ledger_commit": commits["ledgerCommit"],
            },
            ok=True,
        )
    return runtime, store, finalized
