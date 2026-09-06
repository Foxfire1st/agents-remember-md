"""Actual admission publication cuts retain one atomic original certification selection."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
from agents_remember.application import worktree_tools
from agents_remember.application.closeout_door import closeout_door_tool
from agents_remember.application.lifecycle.lifecycle_operation_worker import (
    OperationRuntime,
    execute_operation,
)
from agents_remember.certification.certificate_invalidation import classify_certificate_invalidation
from agents_remember.certification.digests import content_digest
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.git_command import preparation_command, read_git_commit_bytes
from agents_remember.models.certification.corrective import (
    CorrectiveInputChange,
    RedCatalogDisposition,
)
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.certification import OperationCertificationState
from agents_remember.models.lifecycles.door import CloseoutDoorRequest
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    LifecycleOperationRecord,
)
from agents_remember.tasks import write_task_doc
from agents_remember.worktrees.integration.closeout.certification import (
    admission as admission_module,
)
from agents_remember.worktrees.integration.closeout.certification.admission import (
    initial_certification_state,
    prepare_closeout_certification,
)
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
    _observe_current_memory,
    _observed_recovery_changes,
    current_certification_handoff,
)
from agents_remember.worktrees.integration.closeout.certification.selection import (
    LoadedCertificationSelection,
    require_selected_certification,
)
from agents_remember.worktrees.integration.closeout.preparation import private_execution
from agents_remember.worktrees.integration.closeout.preparation.code_execution import (
    prepare_code_output,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.lifecycle.worker.termination import (
    worker_process_fingerprint,
)
from agents_remember.worktrees.integration.mutation_evidence import (
    begin_git_mutation,
    ephemeral_git_mutation_snapshot,
    prove_git_commit,
)
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.quality import clean_executor, gate
from agents_remember.worktrees.modules.quality.certification_records import certificate_store
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    published_report_path_from_manifest,
)
from agents_remember.worktrees.services import (
    CertificationContinuationPort,
    bind_worktree_services,
    worktree_services,
)
from agents_remember.worktrees.source_lineage import source_lineage_for_contract
from agents_remember.worktrees.worktree_contract import (
    WorktreeContract,
    load_contract,
    write_contract,
)
from repository_profile_test_support import NODE_FIXTURE, install_fixture_profile
from test_closeout_certification_entrypoint import _apply, _executor, _fixture, _git_state, _store
from test_closeout_certification_recovery import _memory
from test_closeout_queue import MASTER_A, QueueFixture, _grade, _leaf
from test_operation_certification_selection import _fixture as selected_fixture
from test_operation_certification_selection import _queued
from test_worktree_support import git

pytestmark = pytest.mark.integration


def _original_run_bytes(record: LifecycleOperationRecord) -> bytes:
    assert record.certification is not None
    contract = load_contract(Path(record.contractPath))
    reference = record.certification.frozenRun
    return (
        certificate_store(contract.worktree_group)
        .exact_path(reference.kind, reference.semanticDigest)
        .read_bytes()
    )


@pytest.mark.parametrize("observation", ["unchanged", "heartbeat", "cancelled", "invalid"])
def test_memory_observation_revalidates_inputs_and_live_journal_owner(
    tmp_path: Path, observation: str
) -> None:
    """Observe typed inputs only; this does not issue or select a Gate-5 certificate."""
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    running = OperationRuntime(store).start()
    handoff = current_certification_handoff(contract, running, store)
    original_journal = store.path.read_bytes()
    original_run = _original_run_bytes(running)
    inputs = _memory()
    observed_records = []

    class Observer:
        def observe_memory(self, actual):
            assert actual is handoff
            if observation in {"heartbeat", "cancelled"}:
                update = (
                    {"currentCommand": "observing current memory"}
                    if observation == "heartbeat"
                    else {"cancelRequested": True}
                )
                observed_records.append(store.update(lambda row: row.model_copy(update=update)))
            if observation == "invalid":
                return inputs.model_copy(update={"affectedClosurePlanDigest": "invalid"})
            return inputs

    # Only the read-only protocol operation is under test; no execution adapter is installed.
    observer = cast(CertificationContinuationPort, Observer())
    if observation in {"cancelled", "invalid"}:
        with pytest.raises(CertificationContractError) as refused:
            _observe_current_memory(handoff, observer)
        expected = (
            "certification-selection-observation-moved"
            if observation == "cancelled"
            else "certification-memory-inputs-invalid"
        )
        assert refused.value.findings[0]["code"] == expected
    else:
        actual = _observe_current_memory(handoff, observer)
        assert actual == inputs and actual is not inputs
    current = store.read()
    assert current is not None and current.certification == running.certification
    assert current.certification is not None and current.certification.terminals == ()
    assert _original_run_bytes(current) == original_run
    if observed_records:
        assert current == observed_records[-1]
        assert current.recordRevision == running.recordRevision + 1
    else:
        assert store.path.read_bytes() == original_journal


def test_queued_selected_owner_cannot_execute_before_worker_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    queued = store.read()
    assert queued is not None and queued.status == "queued"
    journal = store.path.read_bytes()
    original_run = _original_run_bytes(queued)
    calls: list[clean_executor.CleanQualityRequest] = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
    continuation = mock.Mock(spec=CertificationContinuationPort)
    bind_worktree_services(replace(worktree_services(), certification_continuation=continuation))
    with pytest.raises(CertificationContractError) as refused:
        execute_operation(queued, OperationRuntime(store))
    assert refused.value.findings[0]["code"] == "certification-worker-no-longer-current"
    assert calls == [] and continuation.mock_calls == []
    assert store.path.read_bytes() == journal
    assert _original_run_bytes(queued) == original_run


def test_nominal_success_with_missing_gate_four_cannot_enter_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    runtime = OperationRuntime(store)
    owner = runtime.start()
    original_run = _original_run_bytes(owner)
    calls: list[clean_executor.CleanQualityRequest] = []

    def omit_gate_four(terminal):
        assert [item["gate"] for item in terminal["gates"]] == [1, 2, 3, 4]
        terminal["gates"] = terminal["gates"][:3]

    monkeypatch.setattr(
        gate,
        "run_clean_quality",
        _executor(NODE_FIXTURE, calls, transform_terminal=omit_gate_four),
    )
    continuation = mock.Mock(spec=CertificationContinuationPort)
    bind_worktree_services(replace(worktree_services(), certification_continuation=continuation))
    with pytest.raises(CertificationContractError) as refused:
        execute_operation(owner, runtime)
    assert refused.value.findings[0]["code"] == "certification-code-prefix-incomplete"
    assert len(calls) == 1 and continuation.mock_calls == []
    current = store.read()
    assert current is not None and current.status == "running"
    selected = require_selected_certification(load_contract(contract.contract_path), current)
    assert [item.result.gate for item in selected.terminals] == [1, 2, 3]
    assert all(item.certificate is not None for item in selected.terminals)
    assert selected.recovery.semanticEnvelope.reusePlan.firstGateToRun == 4
    assert _original_run_bytes(current) == original_run


@pytest.mark.parametrize("cut", ["before-door", "after-door", "before-launch"])
def test_public_creation_selects_originals_before_every_door_and_launch_cut(
    tmp_path: Path, cut: str
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    store = _store(contract)
    original_write = LifecycleOperationStore._write
    original_publish = lifecycle_operations.publish_door_intent
    writes: list[LifecycleOperationRecord] = []

    def checked_write(owner: LifecycleOperationStore, record: LifecycleOperationRecord) -> None:
        if owner.path == store.path:
            assert record.certification is not None
            require_selected_certification(contract, record)
            writes.append(record)
        original_write(owner, record)

    def publish(path, publication):
        current = store.read()
        assert current is not None and current.certification is not None
        if cut == "before-door":
            raise RuntimeError("injected admission publication cut")
        proof = original_publish(path, publication)
        if cut == "after-door":
            raise RuntimeError("injected admission publication cut")
        return proof

    def launch(_contract, record):
        require_selected_certification(load_contract(contract.contract_path), record)
        assert record.doorPublication is not None and record.doorPublication.state == "proven"
        if cut == "before-launch":
            raise RuntimeError("injected admission publication cut")

    with (
        mock.patch.object(LifecycleOperationStore, "_write", checked_write),
        mock.patch.object(lifecycle_operations, "publish_door_intent", publish),
        mock.patch.object(lifecycle_operations, "launch_detached_worker", launch),
    ):
        if cut == "before-launch":
            _apply(fixture)
        else:
            with pytest.raises(RuntimeError, match="injected admission publication cut"):
                _apply(fixture)
    assert writes and writes[0].recordRevision == 1
    assert writes[0].doorPublication is not None
    assert writes[0].doorPublication.state == "intent"
    original = store.read()
    assert original is not None and original.certification is not None
    selected = require_selected_certification(load_contract(contract.contract_path), original)
    original_bytes = _original_run_bytes(original)
    with (
        mock.patch.object(
            admission_module,
            "prepare_staged_code",
            side_effect=AssertionError("duplicate ran hook"),
        ),
        mock.patch.object(lifecycle_operations, "launch_detached_worker") as retry_launch,
    ):
        result = _apply(fixture)
    assert result["ok"] is True
    reopened = store.read()
    assert reopened is not None
    assert reopened.generation == original.generation
    assert reopened.certification == original.certification
    assert _original_run_bytes(reopened) == original_bytes
    loaded = require_selected_certification(load_contract(contract.contract_path), reopened)
    assert (loaded.run.provenance, loaded.admission) == (
        selected.run.provenance,
        selected.admission,
    )
    retry_launch.assert_called_once()
    assert reopened.attempt == original.attempt + int(cut == "before-launch")


@pytest.mark.parametrize("fault", ["callback-failure", "wrong-owner", "missing-selection"])
def test_initial_selection_failure_cannot_publish_a_bare_queued_owner(
    tmp_path: Path, fault: str
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    store = _store(contract)
    before_contract = contract.contract_path.read_bytes()

    def initial(current_contract, record, frozen):
        selected = initial_certification_state(current_contract, record, frozen)
        if fault == "callback-failure":
            raise RuntimeError("injected initial selection failure")
        if fault == "missing-selection":
            return cast(OperationCertificationState, None)
        return selected.model_copy(update={"operationKey": "0" * 64})

    with (
        mock.patch.object(lifecycle_operations, "initial_certification_state", initial),
        mock.patch.object(lifecycle_operations, "publish_door_intent") as publish,
        mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch,
        pytest.raises((RuntimeError, ValueError)),
    ):
        _apply(fixture)
    assert store.read() is None
    assert contract.contract_path.read_bytes() == before_contract
    publish.assert_not_called()
    launch.assert_not_called()


def test_exact_failed_public_retry_reuses_the_original_selection_without_preparation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["ok"] is True
    store = _store(contract)
    original = store.update(
        lambda record: record.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": datetime.now(UTC).isoformat(),
            }
        )
    )
    original_bytes = _original_run_bytes(original)
    with (
        mock.patch.object(
            admission_module, "prepare_staged_code", side_effect=AssertionError("retry ran hook")
        ),
        mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch,
    ):
        assert _apply(fixture)["ok"] is True
    retried = store.read()
    assert retried is not None
    assert (retried.operationKey, retried.generation) == (
        original.operationKey,
        original.generation,
    )
    assert retried.attempt == original.attempt + 1
    assert retried.certification == original.certification
    assert _original_run_bytes(retried) == original_bytes
    launch.assert_called_once()


def _retained_code(root: Path, *, fault: str = "") -> QueueFixture:
    fixture = _fixture(root)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["ok"] is True
    store = _store(contract)
    runtime = OperationRuntime(store)
    running = runtime.start()
    assert isinstance(running.input, CloseoutOperationInput)
    runtime.progress("approval-claim", {"approval_claimed": True})
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        closeout_input=running.input.effectiveInput,
        operation_key=running.operationKey,
        operation_progress=runtime.progress,
    )
    if fault == "wrong-parent":
        git(contract.code_worktree, "commit", "-m", "unattributed intervening parent")
    intent = begin_git_mutation(
        args,
        leg="code",
        repository=contract.code_worktree,
        expected_output_tree=None,
        use_current_candidate=True,
    )
    git(contract.code_worktree, "commit", "--allow-empty", "-m", "certify exact candidate")
    commit = git(contract.code_worktree, "rev-parse", "HEAD")
    if fault != "unproven":
        prove_git_commit(args, intent, repository=contract.code_worktree, commit=commit)
    if fault == "changed-commit":
        git(contract.code_worktree, "commit", "--allow-empty", "-m", "unrelated later commit")
    elif fault == "changed-candidate":
        (contract.code_worktree / "feature.txt").write_text("changed after selected proof\n")
    runtime.finish({"reason": "injected interruption after physical code commit"}, ok=False)
    retained = store.read()
    assert retained is not None and retained.status == "input-required"
    assert retained.certification == running.certification
    return fixture


def test_public_retained_code_output_reopens_original_selection_without_hook_or_refreeze(
    tmp_path: Path,
) -> None:
    fixture = _retained_code(tmp_path)
    contract = fixture.contracts[MASTER_A]
    store = _store(contract)
    original = store.read()
    assert original is not None and original.recoveryCommits is not None
    original_bytes = _original_run_bytes(original)
    original_proof = original.mutationEvidence["code"]
    with (
        mock.patch.object(
            admission_module, "prepare_staged_code", side_effect=AssertionError("retry ran hook")
        ),
        mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch,
    ):
        assert _apply(fixture)["ok"] is True
    resumed = store.read()
    assert resumed is not None and resumed.status == "queued"
    assert (resumed.operationKey, resumed.generation) == (
        original.operationKey,
        original.generation,
    )
    assert resumed.attempt == original.attempt + 1
    assert resumed.certification == original.certification
    assert resumed.mutationEvidence["code"] == original_proof
    assert resumed.recoveryCommits == original.recoveryCommits
    assert _original_run_bytes(resumed) == original_bytes
    launch.assert_called_once()


@pytest.mark.parametrize(
    "fault", ["unproven", "wrong-parent", "changed-commit", "changed-candidate"]
)
def test_public_retained_code_refuses_incomplete_or_changed_original_proof_before_launch(
    tmp_path: Path, fault: str
) -> None:
    fixture = _retained_code(tmp_path, fault=fault)
    contract = fixture.contracts[MASTER_A]
    store = _store(contract)
    original = store.path.read_bytes()
    with (
        mock.patch.object(
            admission_module, "prepare_staged_code", side_effect=AssertionError("refusal ran hook")
        ),
        mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch,
    ):
        if fault in {"changed-commit", "changed-candidate"}:
            with pytest.raises(
                RuntimeError, match="closeout candidate changed outside the accepted generation"
            ):
                _apply(fixture)
        else:
            assert _apply(fixture)["ok"] is False
    assert store.path.read_bytes() == original
    launch.assert_not_called()


@pytest.mark.parametrize("movement", ["head", "index"])
def test_retained_output_rechecks_physical_git_after_candidate_observation(
    tmp_path: Path, movement: str
) -> None:
    fixture = _retained_code(tmp_path)
    contract = load_contract(fixture.contracts[MASTER_A].contract_path)
    store = _store(contract)
    record = store.read()
    assert record is not None and isinstance(record.input, CloseoutOperationInput)
    original_journal = store.path.read_bytes()
    original_run = _original_run_bytes(record)
    actual_check = admission_module.require_retained_output_currentness
    calls = []

    def move_after_observation(actual_contract, actual_record, selected, current):
        calls.append(current)
        if movement == "head":
            git(contract.code_worktree, "commit", "--allow-empty", "-m", "concurrent movement")
        else:
            (contract.code_worktree / "concurrent.txt").write_text("changed index\n")
            git(contract.code_worktree, "add", "concurrent.txt")
        return actual_check(actual_contract, actual_record, selected, current)

    with (
        mock.patch.object(
            admission_module, "require_retained_output_currentness", move_after_observation
        ),
        pytest.raises(CertificationContractError) as refused,
    ):
        admission_module.validate_selected_currentness(contract, record.input, record)
    assert refused.value.findings[0]["code"] == "selected-code-output-proof-mismatch"
    assert len(calls) == 1
    assert store.path.read_bytes() == original_journal
    assert _original_run_bytes(record) == original_run


def test_queued_proven_duplicate_does_not_infer_an_unstarted_worker(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["ok"] is True
    store = _store(fixture.contracts[MASTER_A])
    before = store.read()
    assert before is not None and before.doorPublication is not None
    assert before.doorPublication.state == "proven"
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        assert _apply(fixture)["ok"] is True
    assert store.read() == before
    launch.assert_not_called()


def test_pending_door_duplicate_preserves_a_real_bound_worker_without_launch(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with (
        mock.patch.object(
            lifecycle_operations,
            "publish_door_intent",
            side_effect=RuntimeError("injected before door"),
        ),
        pytest.raises(RuntimeError, match="injected before door"),
    ):
        _apply(fixture)
    store = _store(contract)
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        start_new_session=True,
    ) as child:
        try:
            fingerprint = worker_process_fingerprint(child.pid)
            assert fingerprint is not None
            before = store.update(
                lambda record: record.model_copy(
                    update={
                        "workerPid": child.pid,
                        "workerLease": "a" * 64,
                        "workerProcessFingerprint": fingerprint,
                    }
                )
            )
            with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
                assert _apply(fixture)["ok"] is True
            after = store.read()
            assert after is not None
            assert after.certification == before.certification
            assert (after.generation, after.attempt, after.workerPid, after.workerLease) == (
                before.generation,
                before.attempt,
                before.workerPid,
                before.workerLease,
            )
            launch.assert_not_called()
        finally:
            child.terminate()
            child.wait(timeout=5)


def test_successor_archive_precedes_atomic_selection_and_failed_callback_is_convergent(
    tmp_path: Path,
) -> None:
    fixture = selected_fixture(tmp_path)
    predecessor = fixture.store.update(
        lambda record: record.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": datetime.now(UTC).isoformat(),
            }
        )
    )
    operation_input = fixture.operation_input.model_copy(
        update={"approvalNote": "new exact request"}
    )
    frozen = prepare_closeout_certification(
        fixture.contract,
        operation_input,
        predecessor,
        candidate_tree=fixture.frozen.prepared.candidateTree,
    )
    assert frozen is not None
    queued = _queued(fixture.contract, operation_input, frozen.prepared.candidateTree)
    archive = fixture.store.path.with_name(
        f"{fixture.store.path.stem}.generation-{predecessor.generation}.json"
    )
    observed: list[OperationCertificationState] = []

    def select(record: LifecycleOperationRecord) -> OperationCertificationState:
        assert record.generation == predecessor.generation + 1
        assert archive.is_file()
        selected = initial_certification_state(fixture.contract, record, frozen)
        observed.append(selected)
        if len(observed) == 1:
            raise RuntimeError("injected after archive before queue publication")
        return selected

    before = fixture.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="injected after archive"):
        fixture.store.replace_terminal(queued, initial_certification=select)
    assert fixture.store.path.read_bytes() == before
    archive_bytes = archive.read_bytes()
    successor = fixture.store.replace_terminal(queued, initial_certification=select)
    assert observed[0] == observed[1] == successor.certification
    assert archive.read_bytes() == archive_bytes
    reopened = require_selected_certification(fixture.contract, successor)
    assert reopened.run.provenance == frozen.prepared.provenance
    assert reopened.state.predecessor is not None
    assert reopened.state.predecessor.generation == predecessor.generation
    assert fixture.store.read() == successor


@dataclass(frozen=True)
class _CorrectiveSuccessor:
    fixture: QueueFixture
    prior: LoadedCertificationSelection
    cancelled: LifecycleOperationRecord
    calls: list[clean_executor.CleanQualityRequest]
    original_files: dict[Path, bytes]
    repaired_tree: str
    waiting_generation: str


def _original_evidence(
    contract: WorktreeContract, selected: LoadedCertificationSelection
) -> dict[Path, bytes]:
    objects = certificate_store(contract.worktree_group)
    references = [
        selected.state.frozenRun,
        selected.state.candidateAuthorities,
        selected.state.lifecycleAdmission,
        *(decision.reference for decision in selected.state.recoveryDecisions),
    ]
    paths = []
    for terminal in selected.terminals:
        references.append(terminal.resultReference)
        if terminal.certificateReference is not None:
            references.append(terminal.certificateReference)
        paths.append(
            published_report_path_from_manifest(
                contract.worktree_group / "reports",
                terminal.publication,
                terminal.publication.result_decoder.artifactPath,
            )
        )
    paths.extend(
        objects.exact_path(reference.kind, reference.semanticDigest) for reference in references
    )
    return {path: path.read_bytes() for path in paths}


@pytest.mark.parametrize("observation", ["unavailable", "changed", "unchanged"])
def test_observed_recovery_changes_classify_fresh_memory_without_reusing_historical_authority(
    tmp_path: Path, observation: str
) -> None:
    fixture = selected_fixture(tmp_path)
    selected = require_selected_certification(fixture.contract, fixture.record)
    originals = _original_evidence(fixture.contract, selected)
    before = fixture.store.path.read_bytes()
    # This classifies compiler-only memory inputs against a genuine loaded run and
    # candidate. It neither issues a Gate-5 certificate nor selects a memory observation.
    original_memory = _memory()
    current_memory = (
        None if observation == "unavailable" else _memory("f" if observation == "changed" else "e")
    )
    assert current_memory is not original_memory
    changes = _observed_recovery_changes(selected, original_memory, current_memory)
    assert tuple(change.changeClass for change in changes) == (
        "unchanged-interruption" if observation == "unchanged" else "memory-onboarding",
    )
    invalidation = classify_certificate_invalidation(changes)
    assert invalidation.invalidatedGates == (() if observation == "unchanged" else (5,))
    assert invalidation.retainedGates == (
        (1, 2, 3, 4, 5) if observation == "unchanged" else (1, 2, 3, 4)
    )
    if observation == "changed":
        assert current_memory is not None and current_memory != original_memory
        assert (
            f"from {content_digest(original_memory)} to {content_digest(current_memory)}"
            in changes[0].reason
        )
    elif observation == "unchanged":
        assert current_memory == original_memory
    else:
        assert current_memory is None
    assert fixture.store.path.read_bytes() == before
    assert fixture.store.read() == fixture.record
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert require_selected_certification(fixture.contract, fixture.record) == selected


def _repair_waiting_candidate(fixture: QueueFixture) -> tuple[str, str]:
    contract = load_contract(fixture.contracts[MASTER_A].contract_path)
    waiting = contract.closeout_door
    assert waiting is not None and waiting.disposition == "waiting"
    (contract.code_worktree / "corrective-input.txt").write_text("repaired fixture input\n")
    git(contract.code_worktree, "add", "corrective-input.txt")
    tree = git(contract.code_worktree, "write-tree")
    assert tree != waiting.candidateTree
    leaf = fixture.leaf_refs[MASTER_A]
    write_task_doc(contract.task_root, _leaf(contract, Path(leaf.path).stem))
    closeout_door_tool(
        fixture.cfg,
        CloseoutDoorRequest.model_validate(
            {
                "action": "update-provenance",
                "contract_path": contract.contract_path.as_posix(),
                "expected_generation_id": waiting.generationId,
                "grade": _grade("normal", leaf),
                "admission": {},
                "caller": DeclaredCaller(role="manager", task_document_ref=MASTER_A),
            }
        ),
    )
    refreshed = load_contract(contract.contract_path)
    door = refreshed.closeout_door
    assert door is not None and door.disposition == "waiting"
    assert door.predecessorGenerationId == waiting.generationId
    assert door.candidateTree == tree
    fixture.contracts[MASTER_A] = refreshed
    fixture.rebuild()
    return tree, door.generationId


def _cancelled_red_successor(root: Path, monkeypatch: pytest.MonkeyPatch) -> _CorrectiveSuccessor:
    fixture = _fixture(root, candidate_file=("corrective-input.txt", "failing fixture input\n"))
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    runtime = OperationRuntime(store)
    calls: list[clean_executor.CleanQualityRequest] = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls, fail_gate=2))
    with pytest.raises(RuntimeError, match="code-quality gate failed") as failed:
        execute_operation(runtime.start(), runtime)
    runtime.fail(failed.value)
    prior_record = store.read()
    assert prior_record is not None and prior_record.status == "failed"
    prior = require_selected_certification(load_contract(contract.contract_path), prior_record)
    assert [item.result.gate for item in prior.terminals] == [1, 2]
    assert prior.terminals[0].certificate is not None
    assert prior.terminals[1].result.disposition == "red"
    assert prior.terminals[1].certificate is None
    original_files = _original_evidence(contract, prior)
    result = worktree_tools.worktree_operation_control_tool(
        fixture.cfg,
        worktree_tools.OperationControlRequest(
            contract_path=contract.contract_path.as_posix(),
            operation_kind="closeout",
            action="cancel",
            expected_generation=prior_record.generation,
            intent_note="Preserve this red generation and admit a repaired candidate separately.",
            caller=DeclaredCaller(role="manager", task_document_ref=MASTER_A),
        ),
    )
    assert result["ok"] is True, result
    cancelled = store.read()
    assert cancelled is not None and cancelled.status == "cancelled"
    assert cancelled.certification == prior_record.certification
    assert cancelled.cancellationEvidence is not None
    assert cancelled.cancellationEvidence.workerExitProven
    waiting = load_contract(contract.contract_path).closeout_door
    assert waiting is not None and waiting.disposition == "waiting"
    assert prior_record.doorPublication is not None
    assert waiting.predecessorGenerationId == prior_record.doorPublication.generation.generationId
    tree, generation = _repair_waiting_candidate(fixture)
    return _CorrectiveSuccessor(fixture, prior, cancelled, calls, original_files, tree, generation)


def _corrective_dispositions(case: _CorrectiveSuccessor) -> tuple[RedCatalogDisposition, ...]:
    change = CorrectiveInputChange(
        inputKind="candidate-code-tree",
        inputId="candidate",
        beforeDigest=case.prior.run.repositoryPlan.candidateIdentity.value,
        afterDigest=case.repaired_tree,
    )
    return tuple(
        RedCatalogDisposition(
            rail=result.rail,
            priorStatus="fail" if result.status == "fail" else "blocked",
            priorResultDigest=result.resultDigest,
            correctiveOwner=result.correctiveOwner,
            disposition="direct-repair",
            changedInputs=(change,),
            rationale="Replace the failing fixture input with the repaired input in the exact candidate tree.",
        )
        for result in case.prior.terminals[-1].result.railResults
        if result.status in {"fail", "blocked"}
    )


def _apply_correction(fixture: QueueFixture, dispositions: tuple[RedCatalogDisposition, ...]):
    return worktree_tools.worktree_closeout_apply_tool(
        fixture.cfg,
        fixture.contracts[MASTER_A].contract_path.as_posix(),
        worktree_tools.CloseoutCommitMessages(code="certify exact candidate"),
        worktree_tools.CloseoutApproval(intent_note="exercise explicit fixture closeout"),
        corrective_dispositions=dispositions,
    )


@pytest.mark.parametrize(
    "missing_dispositions", [False, True], ids=["corrected", "missing-dispositions"]
)
def test_public_red_successor_requires_correction_and_executes_with_original_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_dispositions: bool
) -> None:
    case = _cancelled_red_successor(tmp_path, monkeypatch)
    contract = case.fixture.contracts[MASTER_A]
    store = _store(contract)
    before = store.path.read_bytes()
    head = git(contract.code_worktree, "rev-parse", "HEAD")
    dispositions = () if missing_dispositions else _corrective_dispositions(case)
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        result = _apply_correction(case.fixture, dispositions)
    if missing_dispositions:
        assert result["ok"] is False and result["gateStarts"] == 0
        assert "prior-red-authority-incomplete" in {item["code"] for item in result["findings"]}
        assert store.path.read_bytes() == before
        launch.assert_not_called()
    else:
        assert result["ok"] is True and result["state"] == "queued", result
        launch.assert_called_once()
        _execute_corrected_successor(case, monkeypatch)
    assert git(contract.code_worktree, "rev-parse", "HEAD") == head
    assert git(contract.code_worktree, "write-tree") == case.repaired_tree
    assert all(path.read_bytes() == raw for path, raw in case.original_files.items())


def _execute_corrected_successor(
    case: _CorrectiveSuccessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = load_contract(case.fixture.contracts[MASTER_A].contract_path)
    store = _store(contract)
    successor = store.read()
    assert successor is not None and successor.certification is not None
    assert successor.generation == case.cancelled.generation + 1
    assert successor.predecessorFingerprint == case.cancelled.fingerprint
    assert contract.closeout_door is not None
    assert contract.closeout_door.generationId == case.waiting_generation
    assert contract.closeout_door.disposition == "claimed"
    archive = store.path.with_name(f"{store.path.stem}.generation-{case.cancelled.generation}.json")
    archive_bytes = archive.read_bytes()
    archived = LifecycleOperationRecord.model_validate_json(archive_bytes)
    assert archived.certification == case.cancelled.certification
    assert archived.successorFingerprint == successor.fingerprint
    selected = require_selected_certification(contract, successor)
    assert selected.state.inputTerminals == case.prior.state.terminals
    assert selected.state.priorRedDisposition is not None
    assert selected.admission.priorRedDisposition is not None
    disposition = selected.admission.priorRedDisposition
    assert (
        disposition.semanticEnvelope.priorCatalogDigest
        == case.prior.terminals[-1].result.manifestDigest
    )
    assert disposition.semanticEnvelope.dispositions == _corrective_dispositions(case)
    assert (
        certificate_store(contract.worktree_group).load_reference(
            selected.state.priorRedDisposition
        )
        == disposition
    )
    assert selected.recovery.semanticEnvelope.reusePlan.firstGateToRun == 1
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, case.calls))
    runtime = OperationRuntime(store)
    with pytest.raises(CertificationContractError) as pending:
        execute_operation(runtime.start(), runtime)
    assert pending.value.findings[0]["code"] == "certification-continuation-unbound"
    assert len(case.calls) == 2
    execution = case.calls[-1].execution
    assert execution is not None and execution.first_gate == 1
    assert execution.run == selected.run
    assert execution.certificates == (case.prior.terminals[0].certificate,)
    assert execution.retained == ()
    current = store.read()
    assert current is not None and current.status == "running"
    finished = require_selected_certification(contract, current)
    assert tuple(item.result.gate for item in finished.terminals) == (1, 2, 3, 4)
    assert all(item.certificate is not None for item in finished.terminals)
    assert finished.recovery.semanticEnvelope.reusePlan.firstGateToRun == 5
    assert archive.read_bytes() == archive_bytes


def test_public_correction_cannot_borrow_an_unselected_red_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _cancelled_red_successor(tmp_path / "red", monkeypatch)
    fresh = _fixture(tmp_path / "unselected")
    contract = fresh.contracts[MASTER_A]
    before_contract = contract.contract_path.read_bytes()
    before_tree = git(contract.code_worktree, "write-tree")
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        result = _apply_correction(fresh, _corrective_dispositions(case))
    assert result["ok"] is False and result["gateStarts"] == 0
    assert "prior-red-catalog-missing" in {item["code"] for item in result["findings"]}
    launch.assert_not_called()
    assert _store(contract).read() is None
    assert contract.contract_path.read_bytes() == before_contract
    assert git(contract.code_worktree, "write-tree") == before_tree
    assert len(case.calls) == 1
    assert all(path.read_bytes() == raw for path, raw in case.original_files.items())


def test_retained_output_refuses_persisted_repository_alias_with_unchanged_physical_git(
    tmp_path: Path,
) -> None:
    fixture = _retained_code(tmp_path)
    contract = load_contract(fixture.contracts[MASTER_A].contract_path)
    store = _store(contract)
    record = store.read()
    assert record is not None and isinstance(record.input, CloseoutOperationInput)
    original_journal = store.path.read_bytes()
    original_run = _original_run_bytes(record)
    selected = admission_module.validate_selected_currentness(contract, record.input, record)
    proof = record.mutationEvidence["code"]
    original_git = ephemeral_git_mutation_snapshot(contract.code_worktree)
    assert proof.state == "commit-proven" and original_git == proof.observed

    alias = tmp_path / "same-repository-alias"
    alias.symlink_to(contract.code_repo_path, target_is_directory=True)
    assert alias != contract.code_repo_path and alias.resolve() == contract.code_repo_path.resolve()
    write_contract(contract.contract_path, replace(contract, code_repo_path=alias))
    changed = load_contract(contract.contract_path)
    changed_bytes = changed.contract_path.read_bytes()
    assert changed.code_repo_path == alias
    assert changed.code_worktree == contract.code_worktree
    assert git(alias, "rev-parse", contract.code_work_branch) == proof.commit
    lineage = source_lineage_for_contract(changed)
    assert lineage is not None and lineage.state == "current"
    assert ephemeral_git_mutation_snapshot(changed.code_worktree) == original_git

    with pytest.raises(CertificationContractError) as refused:
        admission_module.validate_selected_currentness(changed, record.input, record)
    assert refused.value.findings[0]["code"] == "selected-code-output-lineage-mismatch"
    assert store.path.read_bytes() == original_journal and store.read() == record
    assert _original_run_bytes(record) == original_run
    assert require_selected_certification(changed, record) == selected
    assert ephemeral_git_mutation_snapshot(changed.code_worktree) == original_git
    assert changed.contract_path.read_bytes() == changed_bytes
    assert alias.is_symlink() and alias.resolve() == contract.code_repo_path.resolve()


def _run_preparation_in_worker_session(scenario: str, root: Path, value: bool | str) -> None:
    """Run a scenario under genuine worker ownership without recursively invoking pytest."""
    script = "\n".join(
        (
            "import json, sys",
            "from pathlib import Path",
            "import pytest",
            "from dataclasses import replace",
            "from agents_remember_test_support.testing.global_state import begin_pytest_process",
            "from agents_remember.application.worktree_services import build_default_worktree_services",
            "from agents_remember.worktrees.services import bind_worktree_services",
            "begin_pytest_process()",
            "services = replace(build_default_worktree_services(), certification_continuation=None)",
            "bind_worktree_services(services)",
            "import test_closeout_certification_admission_recovery as scenarios",
            "with pytest.MonkeyPatch.context() as patch:",
            "    getattr(scenarios, sys.argv[1])(Path(sys.argv[2]), patch, json.loads(sys.argv[3]))",
        )
    )
    with subprocess.Popen(
        [sys.executable, "-B", "-c", script, scenario, str(root), json.dumps(value)],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail(f"bounded lifecycle scenario timed out\n{stdout}\n{stderr}")
        assert process.returncode == 0, f"{stdout}\n{stderr}"


def _green_code_preparation_handoff(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, existing: bool = False
) -> CloseoutCertificationHandoff:
    if existing:
        fixture = QueueFixture(root, memory_mode="internal")
        contract = fixture.contracts[MASTER_A]
        install_fixture_profile(contract.code_worktree, contract.repo_name, NODE_FIXTURE)
        git(contract.code_worktree, "add", "-A")
        git(contract.code_worktree, "commit", "-m", "install actual certification profile")
        slug = Path(fixture.leaf_refs[MASTER_A].path).stem
        write_task_doc(contract.task_root, _leaf(contract, slug))
        fixture.declare(MASTER_A)
    else:
        fixture = _fixture(root)
        contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    fingerprint = worker_process_fingerprint(os.getpid())
    assert fingerprint is not None
    lease = "d" * 64
    store.update(
        lambda record: record.model_copy(
            update={
                "workerPid": os.getpid(),
                "workerLease": lease,
                "workerProcessFingerprint": fingerprint,
            }
        )
    )
    runtime = OperationRuntime(store, worker_lease=lease)
    owner = runtime.start()
    calls: list[clean_executor.CleanQualityRequest] = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
    with pytest.raises(CertificationContractError) as refused:
        execute_operation(owner, runtime)
    assert refused.value.findings[0]["code"] == "certification-continuation-unbound"
    assert len(calls) == 1
    handoff = current_certification_handoff(contract, owner, store)
    assert [terminal.result.gate for terminal in handoff.selected.terminals] == [1, 2, 3, 4]
    assert all(terminal.certificate is not None for terminal in handoff.selected.terminals)
    return handoff


@pytest.mark.parametrize("existing", [False, True], ids=["new-private-code", "existing-code"])
def test_preparation_selects_exact_real_code_without_publishing_logical_refs_or_recommitting(
    tmp_path: Path, existing: bool
) -> None:
    _run_preparation_in_worker_session("_prepared_code_scenario", tmp_path, existing)


def _prepared_code_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    handoff = _green_code_preparation_handoff(tmp_path, monkeypatch, existing=existing)
    before = _git_state(handoff.contract)
    original_run = _original_run_bytes(handoff.record)
    original_runner = private_execution.run_git_preparation
    starts = []

    def observe_started(capability, action):
        current = handoff.store.read()
        assert current is not None and current.preparation is not None
        leg = current.preparation.legs[0]
        assert leg.output is None
        assert leg.commands[-1].kind == action and leg.commands[-1].terminal is None
        assert leg.commands[-1].workerPid == os.getpid()
        plan = preparation_command(capability.binding, action)
        assert (leg.commands[-1].cwd, leg.commands[-1].argv) == (plan.cwd.as_posix(), plan.argv)
        starts.append(action)
        return original_runner(capability, action)

    monkeypatch.setattr(private_execution, "run_git_preparation", observe_started)
    selected, reference, output = prepare_code_output(handoff)
    state = selected.handoff.record.preparation
    assert state is not None and state.legs[0].output == reference
    assert output.intent == selected.reference
    assert output.disposition == ("existing" if existing else "created")
    assert selected.intent.writeEnabled is not existing
    assert _git_state(handoff.contract) == before
    assert _original_run_bytes(selected.handoff.record) == original_run
    assert (
        git(handoff.contract.code_worktree, "symbolic-ref", "HEAD")
        == f"refs/heads/{handoff.contract.code_work_branch}"
    )
    assert selected.handoff.record.status == "running"
    assert not selected.handoff.record.irreversibleBoundaryEntered
    assert certificate_store(handoff.contract.worktree_group).load_reference(reference) == output
    if existing:
        assert starts == [] and state.legs[0].commands == ()
        assert output.commit == before[0] and selected.intent.privateRoot is None
    else:
        assert starts == ["create", "materialize", "commit"]
        assert all(
            command.terminal is not None and command.terminal.outcome == "succeeded"
            for command in state.legs[0].commands
        )
        assert output.commit != before[0] and output.parents == (before[0],)
        assert selected.intent.privateRoot is not None
        private = Path(selected.intent.privateRoot)
        assert git(private, "rev-parse", "HEAD") == output.commit
        assert git(private, "rev-parse", "HEAD^{tree}") == selected.intent.admittedTree
        assert read_git_commit_bytes(private, output.commit) == read_git_commit_bytes(
            handoff.contract.code_worktree, output.commit
        )
    journal = handoff.store.path.read_bytes()
    with mock.patch.object(
        private_execution,
        "run_git_preparation",
        side_effect=AssertionError("recommitted original output"),
    ) as rerun:
        reopened, same_reference, same_output = prepare_code_output(selected.handoff)
    rerun.assert_not_called()
    assert same_reference == reference and same_output == output
    assert reopened.handoff.record.preparation == state
    assert handoff.store.path.read_bytes() == journal
    assert _git_state(handoff.contract) == before


@pytest.mark.parametrize(
    "cut", ["before-create", "before-commit", "after-commit", "cancel-after-commit"]
)
def test_preparation_retains_original_uncertain_commands_without_repeating_git(
    tmp_path: Path, cut: str
) -> None:
    _run_preparation_in_worker_session("_interrupted_preparation_scenario", tmp_path, cut)


def _interrupted_preparation_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    handoff = _green_code_preparation_handoff(tmp_path, monkeypatch)
    before = _git_state(handoff.contract)
    original_runner = private_execution.run_git_preparation
    starts = []
    committed_heads = []

    def cut_command(capability, action):
        starts.append(action)
        if (cut == "before-create" and action == "create") or (
            cut == "before-commit" and action == "commit"
        ):
            raise subprocess.TimeoutExpired(action, 300)
        result = original_runner(capability, action)
        if action == "commit":
            committed_heads.append(git(capability.binding.private_root, "rev-parse", "HEAD"))
            if cut == "after-commit":
                raise subprocess.TimeoutExpired(action, 300)
            if cut == "cancel-after-commit":
                handoff.store.update(
                    lambda record: record.model_copy(update={"cancelRequested": True})
                )
        return result

    monkeypatch.setattr(private_execution, "run_git_preparation", cut_command)
    error_type = (
        CertificationContractError if cut == "cancel-after-commit" else subprocess.TimeoutExpired
    )
    with pytest.raises(error_type):
        prepare_code_output(handoff)
    current = handoff.store.read()
    assert current is not None and current.preparation is not None
    leg = current.preparation.legs[0]
    assert leg.output is None and leg.commands[-1].terminal is not None
    expected = "succeeded" if cut == "cancel-after-commit" else "unknown"
    assert leg.commands[-1].terminal.outcome == expected
    assert starts == (["create"] if cut == "before-create" else ["create", "materialize", "commit"])
    commands = leg.commands
    assert _git_state(handoff.contract) == before
    with mock.patch.object(
        private_execution,
        "run_git_preparation",
        side_effect=AssertionError("repeated uncertain command"),
    ) as rerun:
        if cut == "after-commit":
            selected, reference, output = prepare_code_output(handoff)
            assert selected.handoff.record.preparation is not None
            assert selected.handoff.record.preparation.legs[0].output == reference
            assert output.commit != before[0] and committed_heads == [output.commit]
        else:
            with pytest.raises(CertificationContractError) as refused:
                prepare_code_output(handoff)
            assert (
                refused.value.findings[0]["code"]
                == {
                    "before-create": "private-preparation-command-unresolved",
                    "before-commit": "private-preparation-output-unfinished",
                    "cancel-after-commit": "certification-worker-no-longer-current",
                }[cut]
            )
    rerun.assert_not_called()
    final = handoff.store.read()
    assert final is not None and final.preparation is not None
    assert final.preparation.legs[0].commands == commands
    assert final.certification == current.certification
    assert _git_state(handoff.contract) == before
