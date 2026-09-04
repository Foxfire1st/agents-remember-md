"""Production-boundary forcing for proof-derived closeout recovery projection."""

from __future__ import annotations

from pathlib import Path

import pytest
from agents_remember.application.lifecycle.lifecycle_operation_worker import OperationRuntime
from agents_remember.models.lifecycles.mutation_evidence import (
    CloseoutMutationLeg,
    GitMutationSnapshot,
)
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from closeout_fixture_test_support import selected_fixture
from closeout_input_test_support import closeout_operation_input, start_closeout_operation
from pydantic import ValidationError
from test_closeout_queue import MASTER_A


def _external_runtime(root: Path) -> tuple[LifecycleOperationStore, OperationRuntime]:
    fixture = selected_fixture(root, memory_mode="external")
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    return store, OperationRuntime(store)


def _proof(
    record: LifecycleOperationRecord,
    leg: CloseoutMutationLeg,
    commit: str,
) -> dict[str, object]:
    current = record.mutationEvidence[leg]
    before = GitMutationSnapshot(
        headRef="refs/heads/test-closeout",
        head="a" * 40,
        headTree="b" * 40,
        refLogFingerprint="c" * 64,
        indexTree="b" * 40,
        candidateTree="d" * 40,
        statusFingerprint="e" * 64,
    )
    observed = before.model_copy(
        update={"head": commit, "headTree": "d" * 40, "refLogFingerprint": "f" * 64}
    )
    return current.model_copy(
        update={
            "state": "commit-proven",
            "before": before,
            "observed": observed,
            "expectedOutputTree": "d" * 40,
            "commit": commit,
        }
    ).model_dump(mode="json")


def _intent(record: LifecycleOperationRecord, leg: CloseoutMutationLeg) -> dict[str, object]:
    current = record.mutationEvidence[leg]
    before = GitMutationSnapshot(
        headRef="refs/heads/test-closeout",
        head="a" * 40,
        headTree="b" * 40,
        refLogFingerprint="c" * 64,
        indexTree="b" * 40,
        candidateTree="d" * 40,
        statusFingerprint="e" * 64,
    )
    return current.model_copy(
        update={
            "state": "mutation-intent",
            "before": before,
            "expectedOutputTree": "d" * 40,
        }
    ).model_dump(mode="json")


def test_memory_proof_cannot_precede_accepted_code_output(tmp_path: Path) -> None:
    store, runtime = _external_runtime(tmp_path)
    record = store.read()
    assert record is not None
    with pytest.raises(RuntimeError, match="accepted code commit"):
        runtime.progress(
            "memory-commit",
            {"mutation_evidence": _proof(record, "memory", "2" * 40)},
        )


def test_reported_commit_cannot_contradict_proof_or_durable_output(tmp_path: Path) -> None:
    store, runtime = _external_runtime(tmp_path)
    record = store.read()
    assert record is not None
    with pytest.raises(RuntimeError, match="contradicts Git proof"):
        runtime.progress(
            "code-commit",
            {
                "mutation_evidence": _proof(record, "code", "2" * 40),
                "recovery_commits": {"codeCommit": "3" * 40},
            },
        )
    runtime.progress("code-commit", {"recovery_commits": {"codeCommit": "4" * 40}})
    with pytest.raises(RuntimeError, match="changes durable output"):
        runtime.progress("code-commit", {"recovery_commits": {"codeCommit": "5" * 40}})


def test_proof_cannot_contradict_an_existing_verified_output(tmp_path: Path) -> None:
    store, runtime = _external_runtime(tmp_path)
    runtime.progress("code-commit", {"recovery_commits": {"codeCommit": "4" * 40}})
    record = store.read()
    assert record is not None
    with pytest.raises(RuntimeError, match="durable code recovery commit contradicts Git proof"):
        runtime.progress(
            "code-commit",
            {"mutation_evidence": _proof(record, "code", "5" * 40)},
        )


def test_store_requires_the_exact_projection_of_valid_commit_proof(tmp_path: Path) -> None:
    source, runtime = _external_runtime(tmp_path)
    record = source.read()
    assert record is not None
    runtime.progress(
        "code-commit",
        {"mutation_evidence": _intent(record, "code")},
    )
    record = source.read()
    assert record is not None
    runtime.progress(
        "code-commit",
        {"mutation_evidence": _proof(record, "code", "6" * 40)},
    )
    proven = source.read()
    assert proven is not None and proven.recoveryCommits is not None

    # L18 store discipline: a fresh lifecycle generation must begin at record
    # revision 1, so a mid-lifecycle snapshot (a later journal revision) can
    # never be replayed through create(); the revision gate refuses it before
    # any projection check or write.
    missing_projection = LifecycleOperationRecord.model_validate(
        {**proven.model_dump(), "recoveryCommits": None}
    )
    with pytest.raises(RuntimeError, match="must begin at record revision 1"):
        LifecycleOperationStore(tmp_path / "missing-projection.json").create(missing_projection)

    # The projection guard still refuses a revision-1 candidate whose code
    # evidence claims commit proof while its durable snapshot carries no
    # recovery output: durable output must be the exact projection of the
    # mutation evidence a new generation claims.
    coherent_identity = LifecycleOperationRecord.model_validate(
        {
            **proven.model_dump(),
            "recordRevision": 1,
            "attempt": 1,
            "generation": 1,
            "predecessorFingerprint": "",
            "successorFingerprint": "",
            "recoveryCommits": None,
        }
    )
    with pytest.raises(RuntimeError, match="exact projection"):
        LifecycleOperationStore(tmp_path / "missing-projection.json").create(coherent_identity)


def test_structurally_impossible_proof_is_refused_by_the_model(tmp_path: Path) -> None:
    store, _runtime = _external_runtime(tmp_path)
    record = store.read()
    assert record is not None
    payload = record.mutationEvidence["code"].model_dump()
    payload.update(
        {
            "state": "commit-proven",
            "before": GitMutationSnapshot(
                headRef="refs/heads/test-closeout",
                head="a" * 40,
                headTree="b" * 40,
                refLogFingerprint="c" * 64,
                indexTree="b" * 40,
                candidateTree="d" * 40,
                statusFingerprint="e" * 64,
            ).model_dump(),
            "observed": None,
            "expectedOutputTree": "d" * 40,
            "commit": None,
        }
    )
    with pytest.raises(ValidationError, match="observed output tree and commit"):
        record.mutationEvidence["code"].__class__.model_validate(payload)


def test_worker_progress_refuses_non_string_finalization_identity(tmp_path: Path) -> None:
    _store, runtime = _external_runtime(tmp_path)
    with pytest.raises(RuntimeError, match="must be a string"):
        runtime.progress("contract-finalization", {"closeout_finalized_contract_sha256": 3})
