"""L1: normalized closeout input and durable Git mutation evidence."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.application import worktree_tools
from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.errors import (
    CertificationContractError,
    CuratorCoherenceError,
    MemoryCandidatePairError,
    MemoryCandidatePairFailure,
)
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.closeout.input import CloseoutCorrectedCall
from agents_remember.models.lifecycles.mutation_evidence import CloseoutMutationLeg
from agents_remember.models.lifecycles.operation import CloseoutOperationInput
from agents_remember.worktrees.closeout_input import (
    CloseoutCandidateSnapshot,
    CloseoutInputError,
    normalize_closeout_input,
    raw_closeout_messages,
)
from agents_remember.worktrees.integration.closeout import (
    operation_admission as closeout_operation_admission,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.mutation_evidence import (
    begin_git_mutation,
    bind_expected_output_tree,
    prove_git_commit,
)
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.route_review import code_candidate_tree
from agents_remember.worktrees.worktree_contract import write_contract
from closeout_fixture_test_support import selected_fixture as _selected_fixture
from closeout_input_test_support import (
    closeout_operation_input,
    start_closeout_operation,
)
from test_closeout_queue import MASTER_A
from test_worktree_support import git


@pytest.mark.parametrize(
    ("value", "observation"),
    [(None, "omitted"), ("", "empty"), (" \n ", "whitespace-only")],
)
@pytest.mark.parametrize("leg", ["code", "memory", "ledger"])
def test_enabled_message_observations_refuse_with_exact_correction(
    tmp_path: Path, value: str | None, observation: str, leg: str
) -> None:
    fixture = _selected_fixture(tmp_path / f"{leg}-{observation}", memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    messages: dict[str, str | None] = {
        "code": "code",
        "memory": "memory",
        "ledger": "ledger",
    }
    messages[leg] = value

    with pytest.raises(CloseoutInputError) as raised:
        normalize_closeout_input(
            contract,
            raw_closeout_messages(
                code=messages["code"],
                memory=messages["memory"],
                ledger=messages["ledger"],
            ),
            route="worktree",
            corrected_call=CloseoutCorrectedCall(
                tool="worktree_closeout_apply",
                arguments={"contract_path": contract.contract_path.as_posix()},
            ),
        )

    error = raised.value
    assert [item.model_dump(mode="json") for item in error.invalid_fields] == [
        {
            "field": f"{leg}_commit_message",
            "leg": leg,
            "observation": observation,
            "code": f"enabled-{leg}-message-required",
        }
    ]
    assert error.resolved_plan.code.state == "enabled"
    assert error.resolved_plan.memory.state == "enabled"
    assert error.resolved_plan.ledger.state == "enabled"
    corrected_arguments = error.corrected_call.arguments
    assert corrected_arguments[f"{leg}_commit_message"] == (f"<nonblank {leg} commit message>")


def test_plan_uses_lifecycle_possible_writes_and_typed_not_applicable(
    tmp_path: Path,
) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    external = fixture.contracts[MASTER_A]
    with mock.patch(
        "agents_remember.worktrees.closeout_input.capture_closeout_candidate",
        return_value=CloseoutCandidateSnapshot("a" * 40, "b" * 40, "a" * 40),
    ):
        normalized = normalize_closeout_input(
            external,
            raw_closeout_messages(code=None, memory=" memory ", ledger=" ledger "),
            route="worktree",
            corrected_call=CloseoutCorrectedCall(tool="worktree_closeout_preview", arguments={}),
        )
    assert normalized.code.state == "not-applicable"
    assert normalized.memory.state == "enabled"
    assert normalized.message_for("memory") == "memory"
    assert normalized.message_for("ledger") == "ledger"

    internal = replace(
        external,
        memory_mode="internal",
        memory_repo_path=None,
        memory_worktree=None,
        ledger_path=None,
    )
    with mock.patch(
        "agents_remember.worktrees.closeout_input.capture_closeout_candidate",
        return_value=CloseoutCandidateSnapshot("a" * 40, "b" * 40, "a" * 40),
    ):
        first = normalize_closeout_input(
            internal,
            raw_closeout_messages(code=None, memory="ignored one", ledger="ignored one"),
            route="worktree",
            corrected_call=CloseoutCorrectedCall(tool="worktree_closeout_preview", arguments={}),
        )
        second = normalize_closeout_input(
            internal,
            raw_closeout_messages(code=" ", memory="ignored two", ledger=None),
            route="worktree",
            corrected_call=CloseoutCorrectedCall(tool="worktree_closeout_preview", arguments={}),
        )
    assert first == second
    assert first.memory.state == first.ledger.state == "not-applicable"

    disabled = replace(internal, memory_mode="disabled")
    disabled_input = normalize_closeout_input(
        disabled,
        raw_closeout_messages(code="code", memory=None, ledger=None),
        route="worktree",
        corrected_call=CloseoutCorrectedCall(tool="worktree_closeout_preview", arguments={}),
    )
    assert disabled_input.code.state == "enabled"
    assert disabled_input.memory.state == "not-applicable"
    assert disabled_input.ledger.state == "not-applicable"

    series = replace(external, kind="series")
    series_input = normalize_closeout_input(
        series,
        raw_closeout_messages(code=None, memory=None, ledger=None),
        route="worktree",
        corrected_call=CloseoutCorrectedCall(tool="worktree_closeout_preview", arguments={}),
    )
    assert series_input.code.state == "not-applicable"
    assert series_input.memory.state == "not-applicable"
    assert series_input.ledger.state == "not-applicable"


def test_preview_apply_and_duplicate_fingerprints_share_one_normalized_input(
    tmp_path: Path,
) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    config = load_config(fixture.config_path)
    messages = worktree_tools.CloseoutCommitMessages(
        code="  code subject  ",
        memory="  memory subject  ",
        ledger="  ledger subject  ",
    )
    with (
        mock.patch.object(
            worktree_tools.git_worktree_manager,
            "closeout_result",
            return_value=WorktreeCommandResult(0, {"state": "would-closeout"}),
        ) as preview_call,
    ):
        preview = worktree_tools.worktree_closeout_preview_tool(
            config, contract.contract_path.as_posix(), messages
        )
    preview_input = preview_call.call_args.args[0].closeout_input
    assert preview["state"] == "would-closeout"
    assert preview_input is not None
    launcher = mock.Mock()

    def admit(admission, admitted_contract):
        assert admitted_contract.contract_path == contract.contract_path
        return lifecycle_operations.start_or_observe_closeout_operation(
            admission,
            admitted_contract,
            launcher=launcher,
        )

    with (
        mock.patch.object(
            worktree_tools,
            "start_or_observe_closeout_operation",
            side_effect=admit,
        ) as start,
        mock.patch.object(
            worktree_tools,
            "_operation_acknowledgement",
            return_value={"ok": True},
        ),
    ):
        first_apply = worktree_tools.worktree_closeout_apply_tool(
            config,
            contract.contract_path.as_posix(),
            messages,
            worktree_tools.CloseoutApproval(intent_note="approved"),
        )
        worktree_tools.worktree_closeout_apply_tool(
            config,
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(
                code="code subject",
                memory="memory subject",
                ledger="ledger subject",
            ),
            worktree_tools.CloseoutApproval(intent_note="approved"),
        )
        invalid = worktree_tools.worktree_closeout_apply_tool(
            config,
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(
                code="code subject", memory=" \n", ledger="ledger subject"
            ),
            worktree_tools.CloseoutApproval(intent_note="approved"),
        )

    record = LifecycleOperationStore(
        operation_record_path(contract.worktree_group, "closeout")
    ).read()
    assert record is not None
    assert isinstance(record.input, CloseoutOperationInput)
    assert record.input.effectiveInput == preview_input
    assert record.fingerprint
    assert first_apply["pairIdentity"]["contractPath"] == (
        contract.contract_path.resolve().as_posix()
    )
    assert first_apply["pairIdentity"]["codeRoot"] == contract.code_worktree.resolve().as_posix()
    assert invalid["status"] == "closeout-input-invalid"
    # Invalid commit input is rejected before an operation can be started or observed.
    assert start.call_count == 2
    launcher.assert_called_once()


def test_invalid_apply_after_selection_changes_no_authority_or_git_fact(tmp_path: Path) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    config = load_config(fixture.config_path)
    before_coordination = _bytes_under(fixture.coord)
    before_code = _git_facts(contract.code_worktree)
    assert contract.memory_worktree is not None
    before_memory = _git_facts(contract.memory_worktree)
    operation_path = operation_record_path(contract.worktree_group, "closeout")

    with mock.patch.object(
        lifecycle_operations.lifecycle_worker_launch, "launch_or_fail"
    ) as launch:
        refusals = [
            worktree_tools.worktree_closeout_apply_tool(
                config,
                contract.contract_path.as_posix(),
                worktree_tools.CloseoutCommitMessages(code="code", memory=" \n ", ledger="ledger"),
                worktree_tools.CloseoutApproval(intent_note="approved"),
            )
            for _ in range(2)
        ]

    assert [item["status"] for item in refusals] == [
        "closeout-input-invalid",
        "closeout-input-invalid",
    ]
    launch.assert_not_called()
    assert not operation_path.exists()
    assert _bytes_under(fixture.coord) == before_coordination
    assert _git_facts(contract.code_worktree) == before_code
    assert _git_facts(contract.memory_worktree) == before_memory


def test_preview_translates_curator_coherence_refusal_at_its_shared_boundary(
    tmp_path: Path,
) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    error = CuratorCoherenceError(
        "curator-coherence-authority-stale",
        "coherence authority belongs to an older candidate",
        next_action="prepare",
    )
    with mock.patch.object(
        worktree_tools.git_worktree_manager,
        "closeout_result",
        side_effect=error,
    ):
        result = worktree_tools.worktree_closeout_preview_tool(
            load_config(fixture.config_path),
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(
                code="code",
                memory="memory",
                ledger="ledger",
            ),
        )
    assert result["status"] == error.status
    assert result["state"] == "refused"
    assert result["nextAction"] == "prepare"


def test_apply_pair_refusal_names_field_and_exact_repair_route(tmp_path: Path) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    config = load_config(fixture.config_path)
    error = MemoryCandidatePairError(
        "memory-candidate-pair-base-stale",
        "the recorded code base no longer equals its source branch",
        failure=MemoryCandidatePairFailure(
            field="codeBaseCommit",
            contract_path=contract.contract_path.resolve().as_posix(),
            expected={"baseCommit": contract.code_base_commit},
            observed={"sourceCommit": "f" * 40},
            next_action="worktree_sync",
            next_args={
                "contract_path": contract.contract_path.resolve().as_posix(),
                "dry_run": True,
            },
        ),
    )

    with (
        mock.patch.object(
            worktree_tools,
            "resolve_closeout_memory_pair",
            side_effect=error,
        ),
        mock.patch.object(worktree_tools, "start_or_observe_closeout_operation") as start,
    ):
        refused = worktree_tools.worktree_closeout_apply_tool(
            config,
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(
                code="code",
                memory="memory",
                ledger="ledger",
            ),
            worktree_tools.CloseoutApproval(intent_note="approved"),
        )

    assert refused["status"] == "memory-candidate-pair-base-stale"
    assert refused["pairField"] == "codeBaseCommit"
    assert refused["nextAction"] == "worktree_sync"
    assert refused["nextArgs"] == {
        "contract_path": contract.contract_path.resolve().as_posix(),
        "dry_run": True,
    }
    start.assert_not_called()


@pytest.mark.parametrize("drift", ["dirty-to-clean", "clean-to-dirty"])
def test_candidate_drift_at_normalization_capture_seam_refuses_without_authority(
    tmp_path: Path,
    drift: str,
) -> None:
    fixture = _selected_fixture(tmp_path / drift, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    candidate = contract.code_worktree / "admission-drift.py"
    if drift == "dirty-to-clean":
        candidate.write_text("VALUE = 'dirty'\n", encoding="utf-8")
        code_message = "close stable candidate"
    else:
        git(contract.code_worktree, "add", "-A")
        git(contract.code_worktree, "commit", "-m", "prepare clean admission candidate")
        code_message = None
    before_coordination = _bytes_under(fixture.coord)
    before_code = _git_facts(contract.code_worktree)
    assert contract.memory_worktree is not None
    before_memory = _git_facts(contract.memory_worktree)
    record_path = operation_record_path(contract.worktree_group, "closeout")
    capture = closeout_operation_admission.capture_closeout_admission_snapshot
    calls = 0

    def cross_barrier(loaded):
        nonlocal calls
        calls += 1
        if calls == 1:
            return capture(loaded)
        if drift == "dirty-to-clean":
            original = candidate.read_bytes()
            candidate.unlink()
            try:
                return capture(loaded)
            finally:
                candidate.write_bytes(original)
        candidate.write_text("VALUE = 'late'\n", encoding="utf-8")
        try:
            return capture(loaded)
        finally:
            candidate.unlink()

    with (
        mock.patch.object(
            closeout_operation_admission,
            "capture_closeout_admission_snapshot",
            side_effect=cross_barrier,
        ),
        mock.patch.object(lifecycle_operations.lifecycle_worker_launch, "launch_or_fail") as launch,
    ):
        refused = worktree_tools.worktree_closeout_apply_tool(
            load_config(fixture.config_path),
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(
                code=code_message,
                memory="close memory",
                ledger="record mapping",
            ),
            worktree_tools.CloseoutApproval(intent_note="approved"),
        )

    assert refused["status"] == "closeout-input-invalid"
    assert refused["invalidFields"][0]["code"] == ("closeout-candidate-changed-during-admission")
    assert not record_path.exists()
    launch.assert_not_called()
    assert _bytes_under(fixture.coord) == before_coordination
    assert _git_facts(contract.code_worktree) == before_code
    assert _git_facts(contract.memory_worktree) == before_memory


def test_preview_and_direct_apply_return_the_same_typed_refusal(tmp_path: Path) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    config = load_config(fixture.config_path)
    messages = worktree_tools.CloseoutCommitMessages(
        code="code",
        memory="",
        ledger="ledger",
    )
    with (
        mock.patch.object(lifecycle_operations.lifecycle_worker_launch, "launch_or_fail") as launch,
        mock.patch.object(worktree_tools.git_worktree_manager, "closeout_result") as closeout,
    ):
        preview = worktree_tools.worktree_closeout_preview_tool(
            config,
            contract.contract_path.as_posix(),
            messages,
        )
        apply = worktree_tools.worktree_closeout_apply_tool(
            config,
            contract.contract_path.as_posix(),
            messages,
            worktree_tools.CloseoutApproval(intent_note="approved"),
        )

    for field in ("status", "invalidFields", "resolvedPlan"):
        assert preview[field] == apply[field]
    assert preview["correctedCall"]["arguments"]["memory_commit_message"] == (
        "<nonblank memory commit message>"
    )
    assert apply["correctedCall"]["arguments"]["intent_note"] == "<developer intent>"
    launch.assert_not_called()
    closeout.assert_not_called()


def test_dry_run_apply_refusal_preserves_the_non_mutating_corrected_call(
    tmp_path: Path,
) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    config = load_config(fixture.config_path)
    messages = worktree_tools.CloseoutCommitMessages(code=None, memory=None, ledger=None)
    refused = worktree_tools.worktree_closeout_apply_tool(
        config,
        contract.contract_path.as_posix(),
        messages,
        worktree_tools.CloseoutApproval(intent_note="review only", dry_run=True),
    )

    assert refused["status"] == "closeout-input-invalid"
    corrected_arguments = refused["correctedCall"]["arguments"]
    assert corrected_arguments["intent_note"] == "<developer intent>"
    assert corrected_arguments["dry_run"] is True


def test_valid_crash_cuts_before_and_after_record_publication(tmp_path: Path) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    record_path = operation_record_path(contract.worktree_group, "closeout")
    launcher = mock.Mock()

    with (
        mock.patch.object(
            closeout_operation_admission,
            "closeout_contract_sha256",
            side_effect=RuntimeError("cut before record publication"),
        ),
        pytest.raises(RuntimeError, match="cut before record publication"),
    ):
        start_closeout_operation(operation_input, launcher=launcher)
    assert not record_path.exists()
    launcher.assert_not_called()

    with pytest.raises(RuntimeError, match="lifecycle-worker-launch-failed") as raised:
        start_closeout_operation(
            operation_input,
            launcher=lambda *_: (_ for _ in ()).throw(RuntimeError("cut after record publication")),
        )
    assert "cut after record publication" not in str(raised.value)
    store = LifecycleOperationStore(record_path)
    published = store.read()
    assert published is not None
    assert published.status == "failed"
    assert published.input == operation_input
    assert set(published.mutationEvidence) == {"code", "memory", "ledger"}

    resumed_launcher = mock.Mock()
    resumed = start_closeout_operation(operation_input, launcher=resumed_launcher)
    recovered = store.read()
    assert recovered is not None
    assert recovered.operationKey == published.operationKey
    assert resumed.status == "queued"
    recovered_after_resume = store.read()
    assert recovered_after_resume is not None
    assert recovered_after_resume.attempt == 2
    resumed_launcher.assert_called_once()


def test_valid_duplicate_keeps_one_generation_and_invalid_duplicate_cannot_observe_it(
    tmp_path: Path,
) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    first = start_closeout_operation(operation_input, launcher=lambda *_: None)
    record_path = operation_record_path(contract.worktree_group, "closeout")
    before = record_path.read_bytes()
    before_coordination = _bytes_under(fixture.coord)
    before_code = _git_facts(contract.code_worktree)
    assert contract.memory_worktree is not None
    before_memory = _git_facts(contract.memory_worktree)
    duplicate_launcher = mock.Mock()

    duplicate = start_closeout_operation(
        operation_input,
        launcher=duplicate_launcher,
    )

    assert duplicate.status == first.status == "queued"
    current = LifecycleOperationStore(record_path).read()
    assert current is not None and current.attempt == 1
    duplicate_launcher.assert_not_called()
    assert record_path.read_bytes() == before

    with mock.patch.object(
        lifecycle_operations.lifecycle_worker_launch, "launch_or_fail"
    ) as launch:
        invalid = worktree_tools.worktree_closeout_apply_tool(
            load_config(fixture.config_path),
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(
                code="close code candidate",
                memory=" ",
                ledger="record code-to-memory mapping",
            ),
            worktree_tools.CloseoutApproval(intent_note="approved"),
        )

    assert invalid["status"] == "closeout-input-invalid"
    launch.assert_not_called()
    assert record_path.read_bytes() == before
    assert _bytes_under(fixture.coord) == before_coordination
    assert _git_facts(contract.code_worktree) == before_code
    assert _git_facts(contract.memory_worktree) == before_memory


def test_same_tree_different_head_conflicts_with_pre_mutation_generation(
    tmp_path: Path,
) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    before = store.path.read_bytes()
    accepted_tree = code_candidate_tree(contract)
    git(contract.code_worktree, "commit", "--allow-empty", "-m", "move candidate parent")
    assert code_candidate_tree(contract) == accepted_tree

    with pytest.raises(
        RuntimeError,
        match="closeout candidate changed outside the accepted generation's proven output",
    ):
        start_closeout_operation(operation_input, launcher=lambda *_: None)

    assert store.path.read_bytes() == before


@pytest.mark.parametrize("output_cut", ["code", "memory", "ledger"])
def test_public_retry_uses_accepted_plan_after_each_proven_output_cut(
    tmp_path: Path,
    output_cut: CloseoutMutationLeg,
) -> None:
    fixture = _selected_fixture(tmp_path / output_cut, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    runtime.start()
    _publish_outputs_through(contract, operation_input, runtime, output_cut)
    proven = store.read()
    assert proven is not None and proven.recoveryCommits is not None
    assert getattr(
        proven.recoveryCommits,
        {"code": "codeCommit", "memory": "memoryContentCommit", "ledger": "ledgerCommit"}[
            output_cut
        ],
    )
    messages = {
        "code": operation_input.effectiveInput.message_for("code"),
        "memory": operation_input.effectiveInput.message_for("memory"),
        "ledger": operation_input.effectiveInput.message_for("ledger"),
    }
    before_retry = store.path.read_bytes()
    with mock.patch.object(
        lifecycle_operations.lifecycle_worker_launch, "launch_or_fail"
    ) as launch:
        observed = worktree_tools.worktree_closeout_apply_tool(
            load_config(fixture.config_path),
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(**messages),
            worktree_tools.CloseoutApproval(intent_note=operation_input.approvalNote),
        )
    if output_cut == "code":
        assert observed["lifecycleOperation"]["status"] == "running"
    else:
        # L33 recognizes the exact proven code output. L34 owns the later
        # prepared-memory/publication view; a raw memory or ledger writer must
        # not make its changed source edge acceptable to selected admission.
        assert observed["status"] == "certification-admission-refused", observed
        assert observed["gateStarts"] == 0
        assert {item["code"] for item in observed["findings"]} == {
            "selected-candidate-authority-moved"
        }
        assert store.path.read_bytes() == before_retry
        assert store.read() == proven
    launch.assert_not_called()
    before_invalid = store.path.read_bytes()
    messages[output_cut] = " "

    invalid = worktree_tools.worktree_closeout_apply_tool(
        load_config(fixture.config_path),
        contract.contract_path.as_posix(),
        worktree_tools.CloseoutCommitMessages(**messages),
        worktree_tools.CloseoutApproval(intent_note=operation_input.approvalNote),
    )

    assert invalid["status"] == "closeout-input-invalid"
    assert invalid["invalidFields"][0]["field"] == f"{output_cut}_commit_message"
    assert store.path.read_bytes() == before_invalid


def test_valid_retry_refuses_candidate_content_added_after_proven_code_output(
    tmp_path: Path,
) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    runtime.start()
    _publish_outputs_through(contract, operation_input, runtime, "code")
    before = store.path.read_bytes()
    (contract.code_worktree / "late-candidate.py").write_text(
        "VALUE = 'outside-generation'\n", encoding="utf-8"
    )

    with pytest.raises(
        RuntimeError,
        match=("closeout candidate changed outside the accepted generation's proven output"),
    ):
        start_closeout_operation(operation_input, launcher=lambda *_: None)

    assert store.path.read_bytes() == before


def test_valid_retry_refuses_contract_drift_after_proven_code_output(tmp_path: Path) -> None:
    fixture = _selected_fixture(tmp_path, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    runtime.start()
    _publish_outputs_through(contract, operation_input, runtime, "code")
    before = store.path.read_bytes()
    write_contract(contract.contract_path, replace(contract, cleanup="completed"))

    with pytest.raises(RuntimeError, match="contract identity changed outside"):
        start_closeout_operation(operation_input, launcher=lambda *_: None)

    assert store.path.read_bytes() == before


@pytest.mark.parametrize("output_cut", ["code", "memory", "ledger"])
def test_atomic_proof_publication_and_restart_repair_each_recovery_projection(
    tmp_path: Path,
    output_cut: CloseoutMutationLeg,
) -> None:
    fixture = _selected_fixture(tmp_path / f"proof-{output_cut}", memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    running = runtime.start()
    ordered: tuple[CloseoutMutationLeg, ...] = ("code", "memory", "ledger")
    for leg in ordered[: ordered.index(output_cut)]:
        _publish_output(
            contract,
            operation_input,
            runtime.progress,
            leg,
            operation_key=running.operationKey,
        )

    def crash_after_atomic_proof(phase: str, evidence: Mapping[str, object]) -> None:
        runtime.progress(phase, evidence)
        raw = evidence.get("mutation_evidence")
        if isinstance(raw, Mapping) and raw.get("state") == "commit-proven":
            raise RuntimeError("cut immediately after atomic proof publication")

    with pytest.raises(RuntimeError, match="cut immediately after atomic proof publication"):
        _publish_output(
            contract,
            operation_input,
            crash_after_atomic_proof,
            output_cut,
            operation_key=running.operationKey,
        )

    durable = store.read()
    assert durable is not None and durable.recoveryCommits is not None
    field = {
        "code": "codeCommit",
        "memory": "memoryContentCommit",
        "ledger": "ledgerCommit",
    }[output_cut]
    proof = durable.mutationEvidence[output_cut]
    assert proof.state == "commit-proven"
    assert getattr(durable.recoveryCommits, field) == proof.commit

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["status"] = "input-required"
    payload["workerPid"] = None
    if output_cut == "code":
        payload["recoveryCommits"] = None
    else:
        payload["recoveryCommits"][field] = ""
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    launches: list[int] = []

    def launcher(_contract, recovered):
        launches.append(recovered.attempt)

    if output_cut == "code":
        start_closeout_operation(operation_input, launcher=launcher)
        assert launches == [2]
    else:
        before_retry = store.path.read_bytes()
        with pytest.raises(CertificationContractError) as raised:
            start_closeout_operation(operation_input, launcher=launcher)
        assert {item["code"] for item in raised.value.findings} == {
            "selected-candidate-authority-moved"
        }
        assert store.path.read_bytes() == before_retry
        assert launches == []
        # Exercise the actual restart reconciler while L34's prepared memory view
        # remains unavailable to public selected admission.
        lifecycle_operations._reconcile_closeout_store(
            LifecycleOperationStore(store.path), now=datetime.now(UTC), fresh_dead_worker=False
        )

    repaired = store.read()
    assert repaired is not None and repaired.recoveryCommits is not None
    assert repaired.mutationEvidence == durable.mutationEvidence
    assert getattr(repaired.recoveryCommits, field) == proof.commit


def _publish_outputs_through(
    contract,
    operation_input: CloseoutOperationInput,
    runtime: lifecycle_operation_worker.OperationRuntime,
    output_cut: CloseoutMutationLeg,
) -> None:
    current = runtime.store.read()
    assert current is not None
    ordered: tuple[CloseoutMutationLeg, ...] = ("code", "memory", "ledger")
    for leg in ordered:
        _publish_output(
            contract,
            operation_input,
            runtime.progress,
            leg,
            operation_key=current.operationKey,
        )
        if leg == output_cut:
            return
    raise AssertionError(f"unknown output cut: {output_cut}")


def _publish_output(
    contract,
    operation_input: CloseoutOperationInput,
    progress: Callable[[str, Mapping[str, object]], None],
    leg: CloseoutMutationLeg,
    *,
    operation_key: str,
) -> None:
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=operation_input.effectiveInput,
        operation_progress=progress,
        operation_key=operation_key,
    )
    if leg == "code":
        repository = contract.code_worktree
        intent = begin_git_mutation(
            args,
            leg="code",
            repository=repository,
            expected_output_tree=None,
            use_current_candidate=True,
        )
    elif leg == "memory":
        assert contract.memory_worktree is not None
        repository = contract.memory_worktree
        (repository / "proof-memory.txt").write_text("memory output\n", encoding="utf-8")
        intent = begin_git_mutation(
            args,
            leg="memory",
            repository=repository,
            expected_output_tree=None,
            use_current_candidate=True,
        )
    else:
        assert contract.memory_worktree is not None and contract.ledger_path is not None
        repository = contract.memory_worktree
        intent = begin_git_mutation(
            args,
            leg="ledger",
            repository=repository,
            expected_output_tree=None,
        )
        contract.ledger_path.write_text(
            contract.ledger_path.read_text(encoding="utf-8") + "\n# journal projection proof\n",
            encoding="utf-8",
        )
        git(repository, "add", "memory.md")
        intent = bind_expected_output_tree(args, intent, repository=repository)
    git(repository, "add", "-A")
    git(repository, "commit", "-m", f"prove {leg} output")
    prove_git_commit(
        args,
        intent,
        repository=repository,
        commit=git(repository, "rev-parse", "HEAD"),
    )


def _git_facts(
    repository: Path,
) -> tuple[str, str, str, str, bytes, dict[str, bytes]]:
    index_path = Path(git(repository, "rev-parse", "--git-path", "index"))
    if not index_path.is_absolute():
        index_path = repository / index_path
    files = {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    return (
        git(repository, "symbolic-ref", "HEAD"),
        git(repository, "rev-parse", "HEAD"),
        git(repository, "write-tree"),
        git(repository, "status", "--porcelain=v1", "-z"),
        index_path.read_bytes(),
        files,
    )


def _bytes_under(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.suffix != ".lock"
    }
