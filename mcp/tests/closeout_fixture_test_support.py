"""Real waiting-door, selected-operation and writer fixtures for lifecycle boundary suites."""

from dataclasses import replace
from pathlib import Path
from unittest import mock

from agents_remember.application import worktree_tools
from agents_remember.application.lifecycle.lifecycle_operation_worker import OperationRuntime
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.worktrees.integration.closeout.certification import (
    execution as selected_execution,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules import closeout_external
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.context import contract_context
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.modules.quality import closeout_memory as closeout_memory_quality
from agents_remember.worktrees.queue import closeout_recovery
from agents_remember.worktrees.services import worktree_services
from agents_remember.worktrees.worktree_contract import load_contract
from closeout_input_test_support import closeout_operation_input, start_closeout_operation
from repository_profile_test_support import NODE_FIXTURE, install_fixture_profile
from test_closeout_certification_entrypoint import _review_and_declare, _store
from test_closeout_queue import MASTER_A, QueueFixture
from test_worktree_support import git


def selected_fixture(root: Path, *, memory_mode: str) -> QueueFixture:
    fixture = QueueFixture(root, memory_mode=memory_mode)
    fixture.declare(MASTER_A)
    return fixture


def _selected_fixture(root: Path, *, profile_repository_id: str | None = None) -> QueueFixture:
    """Actual external-memory public admission fixture; no completion is synthesized."""
    fixture = QueueFixture(root, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    if profile_repository_id is not None:
        install_fixture_profile(contract.code_worktree, profile_repository_id, NODE_FIXTURE)
    git(contract.code_worktree, "add", "-A")
    _review_and_declare(fixture)
    return fixture


def _public_apply(fixture: QueueFixture):
    return worktree_tools.worktree_closeout_apply_tool(
        load_config(fixture.config_path),
        fixture.contracts[MASTER_A].contract_path.as_posix(),
        worktree_tools.CloseoutCommitMessages(
            code="Add feature", memory="Document feature", ledger="Sync ledger"
        ),
        worktree_tools.CloseoutApproval(intent_note="developer approved exact closeout"),
    )


def _start_selected(fixture: QueueFixture):
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        result = _public_apply(fixture)
    assert result["ok"] is True and result["state"] == "queued", result
    launch.assert_called_once()
    contract = load_contract(fixture.contracts[MASTER_A].contract_path)
    store = _store(contract)
    runtime = OperationRuntime(store)
    return contract, store, runtime, runtime.start()


def _committed_state(contract):
    assert contract.memory_worktree is not None and contract.ledger_path is not None
    return (
        git(contract.code_worktree, "rev-parse", "HEAD"),
        git(contract.memory_worktree, "rev-parse", "HEAD"),
        contract.ledger_path.read_bytes(),
        load_contract(contract.contract_path).closeout_status,
    )


class _PendingMemory:
    """Injected downstream owner: exercise the handoff without claiming Gate 5 acceptance."""

    def __init__(self, events: list[str], *, run_checker: bool = False) -> None:
        self.events = events
        self.run_checker = run_checker
        self.received = []

    def run_memory(self, handoff) -> WorktreeCommandResult:
        assert handoff.selected.recovery.semanticEnvelope.reusePlan.firstGateToRun == 5
        self.received.append(handoff)
        self.events.append("memory")
        if self.run_checker:
            context = replace(
                contract_context(handoff.contract),
                code_repository_root=handoff.contract.code_worktree,
            )
            checks, _after = worktree_services().memory_quality.check_groups()
            closeout_memory_quality.run_memory_quality_phase(context, checks)
        return WorktreeCommandResult(1, {"state": "fixture-memory-owner-pending"})

    def finalize(self, _handoff) -> WorktreeCommandResult:
        raise AssertionError("Gate 5 has not accepted this candidate")


def _with_memory_owner(memory):
    return mock.patch.object(
        selected_execution,
        "worktree_services",
        return_value=replace(worktree_services(), certification_continuation=memory),
    )


def _component_code_and_memory(contract, args: WorktreeArgs) -> tuple[str, str]:
    """Real writer-component fixture only; this does not claim lifecycle/Gate-5 acceptance."""
    assert args.closeout_input is not None
    git(contract.code_worktree, "add", "-A")
    code = closeout_recovery.accepted_code_commit(
        contract, args, args.closeout_input, strict_code_quality_required=True
    )
    memory, created = closeout_external._commit_memory_content(
        contract, args, args.closeout_input, existing_mapping=None, resuming=False
    )
    assert created
    return code, memory


def _component_ledger(contract, args: WorktreeArgs, code: str, memory: str) -> str:
    assert args.closeout_input is not None
    facts = closeout_external._LedgerCommitFacts(
        closeout_external.load_ledger(contract.ledger_path), None, code, memory
    )
    ledger, created = closeout_external._commit_ledger_mapping(
        contract, args, args.closeout_input, facts
    )
    assert created
    return ledger


def running_code_operation(root: Path):
    fixture = QueueFixture(root, memory_mode="internal")
    contract = fixture.contracts[MASTER_A]
    (contract.code_worktree / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    _review_and_declare(fixture)
    contract = fixture.contracts[MASTER_A]
    if fixture.memory_mode == "external":
        assert contract.memory_worktree is not None
        assert contract.ledger_path is not None
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    start_closeout_operation(
        operation_input,
        launcher=lambda *_: None,
    )
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = OperationRuntime(store)
    runtime.start()
    return contract, operation_input, store, runtime
