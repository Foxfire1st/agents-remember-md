"""Real Git forcing for created and resumed external ledger outputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from agents_remember.application.lifecycle.lifecycle_operation_worker import OperationRuntime
from agents_remember.kernel.memory_ledger import find_mapping, load_ledger
from agents_remember.worktrees.integration.closeout.ledger_recovery import (
    classify_closeout_ledger_recovery,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules import closeout_external
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.models import VerifiedChange
from agents_remember.worktrees.queue import closeout_recovery as recovery_mod
from agents_remember.worktrees.queue.closeout_recovery import resume_external_commits
from closeout_input_test_support import closeout_operation_input, start_closeout_operation
from test_closeout_certification_entrypoint import _review_and_declare
from test_closeout_queue import MASTER_A, QueueFixture
from test_worktree_support import git


def _journaled_ledger_fixture(root: Path):
    fixture = QueueFixture(root, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    assert contract.memory_worktree is not None
    config_path = fixture.config_path
    (contract.code_worktree / "accepted.txt").write_text("accepted\n", encoding="utf-8")
    git(contract.code_worktree, "add", "-A")
    git(contract.code_worktree, "commit", "-m", "accepted code output")
    code_commit = git(contract.code_worktree, "rev-parse", "HEAD")
    git(contract.memory_worktree, "add", "-A")
    if git(contract.memory_worktree, "diff", "--cached", "--name-only"):
        git(contract.memory_worktree, "commit", "-m", "accepted memory output")
    memory_commit = git(contract.memory_worktree, "rev-parse", "HEAD")
    _review_and_declare(fixture)
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(
        contract,
        config_path=config_path,
        memory="record memory output",
        ledger="record ledger mapping",
    )
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = OperationRuntime(store)
    running = runtime.start()
    runtime.progress(
        "memory-commit",
        {
            "recovery_commits": {
                "codeCommit": code_commit,
                "memoryContentCommit": memory_commit,
                "ledgerCommit": "",
            }
        },
    )
    return (
        contract,
        operation_input,
        store,
        runtime,
        running.operationKey,
        code_commit,
        memory_commit,
    )


def test_created_ledger_output_publishes_intent_bind_commit_and_proof(tmp_path: Path) -> None:
    contract, operation_input, store, runtime, operation_key, code_commit, memory_commit = (
        _journaled_ledger_fixture(tmp_path)
    )
    events, progress = _recording_progress(runtime)

    memory_result, ledger_commit = resume_external_commits(
        contract,
        WorktreeArgs(
            contract_path=contract.contract_path,
            closeout_input=operation_input.effectiveInput,
            operation_progress=progress,
            operation_key=operation_key,
        ),
        operation_input.effectiveInput,
        code_commit=code_commit,
        memory_commit=memory_commit,
    )

    _assert_created_ledger_output(
        _CreatedLedgerOutput(
            contract,
            operation_input,
            store,
            events,
            memory_result,
            ledger_commit,
            code_commit,
            memory_commit,
        )
    )


def _recording_progress(runtime):
    events: list[tuple[str, dict[str, object]]] = []

    def progress(phase: str, evidence: Mapping[str, object]) -> None:
        events.append((phase, dict(evidence)))
        runtime.progress(phase, evidence)

    return events, progress


@dataclass(frozen=True)
class _CreatedLedgerOutput:
    contract: Any
    operation_input: Any
    store: Any
    events: list[tuple[str, dict[str, object]]]
    memory_result: str
    ledger_commit: str
    code_commit: str
    memory_commit: str


def _assert_created_ledger_output(output: _CreatedLedgerOutput) -> None:
    contract = output.contract
    operation_input = output.operation_input
    store = output.store
    events = output.events
    memory_result = output.memory_result
    ledger_commit = output.ledger_commit
    code_commit = output.code_commit
    memory_commit = output.memory_commit
    assert memory_result == memory_commit
    assert contract.memory_worktree is not None
    assert contract.ledger_path is not None
    assert ledger_commit == git(contract.memory_worktree, "rev-parse", "HEAD")
    assert git(contract.memory_worktree, "log", "-1", "--format=%s") == (
        operation_input.effectiveInput.message_for("ledger")
    )
    mapping = find_mapping(load_ledger(contract.ledger_path), code_commit)
    assert mapping is not None
    assert mapping.memory_commit == memory_commit
    mutation_events = [
        cast(dict[str, object], evidence["mutation_evidence"])
        for phase, evidence in events
        if phase == "ledger-commit" and "mutation_evidence" in evidence
    ]
    assert [event["state"] for event in mutation_events] == [
        "mutation-intent",
        "commit-proven",
    ]
    assert mutation_events[0]["expectedOutputTree"]
    assert mutation_events[1]["commit"] == ledger_commit
    before = cast(dict[str, object], mutation_events[0]["before"])
    observed = cast(dict[str, object], mutation_events[1]["observed"])
    assert before["head"] == memory_commit
    assert mutation_events[0]["expectedOutputTree"] == git(
        contract.memory_worktree, "rev-parse", f"{ledger_commit}^{{tree}}"
    )
    assert mutation_events[1]["expectedOutputTree"] == mutation_events[0]["expectedOutputTree"]
    assert observed["head"] == ledger_commit
    assert observed["headTree"] == mutation_events[0]["expectedOutputTree"]
    durable = store.read()
    assert durable is not None and durable.recoveryCommits is not None
    assert durable.recoveryCommits.model_dump() == {
        "codeCommit": code_commit,
        "memoryContentCommit": memory_commit,
        "ledgerCommit": ledger_commit,
    }
    assert durable.mutationEvidence["ledger"].state == "commit-proven"


@pytest.mark.parametrize(
    ("cut", "expected_state"),
    [
        ("before-write", "accepted-before"),
        ("before-stage", "prepared-unstaged"),
        ("before-commit", "prepared-staged"),
    ],
)
def test_ledger_intent_is_exact_before_real_write_or_stage(
    tmp_path: Path,
    cut: str,
    expected_state: str,
) -> None:
    contract, operation_input, store, runtime, operation_key, code_commit, memory_commit = (
        _journaled_ledger_fixture(tmp_path)
    )
    assert contract.memory_worktree is not None and contract.ledger_path is not None
    repository = contract.memory_worktree
    accepted = {
        "head": git(repository, "rev-parse", "HEAD"),
        "index": git(repository, "write-tree"),
        "status": git(repository, "status", "--porcelain=v1"),
        "ledger": contract.ledger_path.read_bytes(),
        "objects": _git_object_bytes(repository),
    }
    cut_write, cut_stage, cut_commit = _ledger_cut_callbacks(cut)

    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=operation_input.effectiveInput,
        operation_progress=runtime.progress,
        operation_key=operation_key,
    )

    with (
        mock.patch.object(recovery_mod, "write_ledger", side_effect=cut_write),
        mock.patch.object(recovery_mod, "require_git", side_effect=cut_stage),
        mock.patch.object(recovery_mod, "commit_if_dirty", side_effect=cut_commit),
        pytest.raises(SystemExit, match="cut after"),
    ):
        resume_external_commits(
            contract,
            args,
            operation_input.effectiveInput,
            code_commit=code_commit,
            memory_commit=memory_commit,
        )

    durable = store.read()
    assert durable is not None
    intent = durable.mutationEvidence["ledger"]
    assert intent.state == "mutation-intent"
    assert intent.expectedOutputTree is not None
    classification = classify_closeout_ledger_recovery(contract, durable)
    assert classification.state == expected_state
    _assert_ledger_cut_state(contract, repository, accepted, intent, cut)

    _memory_result, ledger_commit = resume_external_commits(
        contract,
        args,
        operation_input.effectiveInput,
        code_commit=code_commit,
        memory_commit=memory_commit,
    )
    completed = store.read()
    assert completed is not None
    assert completed.mutationEvidence["ledger"].state == "commit-proven"
    assert completed.recoveryCommits is not None
    assert completed.recoveryCommits.ledgerCommit == ledger_commit


def _ledger_cut_callbacks(cut: str):
    real_write = recovery_mod.write_ledger
    real_require_git = recovery_mod.require_git
    real_commit = recovery_mod.commit_if_dirty

    def cut_write(path, ledger) -> None:
        if cut == "before-write":
            raise SystemExit("cut after exact intent before real ledger write")
        real_write(path, ledger)

    def cut_stage(target: Path, arguments: list[str]) -> str:
        if cut == "before-stage" and arguments == ["add", "memory.md"]:
            raise SystemExit("cut after ledger write before real stage")
        return real_require_git(target, arguments)

    def cut_commit(target: Path, message: str) -> str:
        if cut == "before-commit":
            raise SystemExit("cut after real stage before ledger commit")
        return real_commit(target, message)

    return cut_write, cut_stage, cut_commit


def _assert_ledger_cut_state(contract, repository, accepted, intent, cut: str) -> None:
    assert git(repository, "rev-parse", "HEAD") == accepted["head"]
    if cut == "before-write":
        assert git(repository, "write-tree") == accepted["index"]
        assert _git_object_bytes(repository) == accepted["objects"]
        assert git(repository, "status", "--porcelain=v1") == accepted["status"]
        assert contract.ledger_path.read_bytes() == accepted["ledger"]
        return
    if cut == "before-stage":
        assert git(repository, "write-tree") == accepted["index"]
        assert _git_object_bytes(repository) == accepted["objects"]
        assert git(repository, "status", "--porcelain=v1").startswith("M memory.md")
        assert contract.ledger_path.read_bytes() != accepted["ledger"]
        return
    assert git(repository, "write-tree") == intent.expectedOutputTree
    assert git(repository, "status", "--porcelain=v1").startswith("M  memory.md")


def _git_object_bytes(repository: Path) -> dict[str, bytes]:
    common = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    objects = common / "objects"
    return {
        path.relative_to(objects).as_posix(): path.read_bytes()
        for path in objects.rglob("*")
        if path.is_file()
    }


def test_resumed_external_output_uses_exact_recovery_tuple_without_refresh(
    tmp_path: Path,
) -> None:
    contract, operation_input, store, runtime, operation_key, code_commit, memory_commit = (
        _journaled_ledger_fixture(tmp_path)
    )
    resume_external_commits(
        contract,
        WorktreeArgs(
            contract_path=contract.contract_path,
            closeout_input=operation_input.effectiveInput,
            operation_progress=runtime.progress,
            operation_key=operation_key,
        ),
        operation_input.effectiveInput,
        code_commit=code_commit,
        memory_commit=memory_commit,
    )
    durable = store.read()
    assert durable is not None and durable.recoveryCommits is not None
    events: list[tuple[str, dict[str, object]]] = []

    def progress(phase: str, evidence: Mapping[str, object]) -> None:
        events.append((phase, dict(evidence)))
        runtime.progress(phase, evidence)

    with mock.patch.object(closeout_external, "_refresh_external_memory") as refresh:
        outcome = closeout_external.external_closeout_commits(
            contract,
            WorktreeArgs(
                contract_path=contract.contract_path,
                closeout_input=operation_input.effectiveInput,
                recovery_commits=durable.recoveryCommits,
                operation_progress=progress,
                operation_key=operation_key,
            ),
            operation_input.effectiveInput,
            VerifiedChange(code_commit, "2026-08-22", []),
            closeout_external.ExternalCloseoutEvidence(
                memory_quality_before_refresh={},
                coherence_no_impact=closeout_external.CuratorCoherenceNoImpact(),
            ),
        )
    refresh.assert_not_called()
    assert outcome.memory_commit == memory_commit
    assert outcome.ledger_commit == durable.recoveryCommits.ledgerCommit
    assert events == [
        (
            "ledger-commit",
            {
                "current_command": "verified-existing ledger commit recorded for recovery",
                "recovery_commits": durable.recoveryCommits.model_dump(),
            },
        )
    ]
