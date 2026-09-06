"""Focused proof for lifecycle operation mutation and worker invariants."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents_remember.models.lifecycles import operation


def _value(**fields: object) -> Any:
    return cast(Any, SimpleNamespace(**fields))


def test_mutation_history_and_irreversible_boundary_require_exact_proof() -> None:
    unchanged = _value(leg="code", state="reconciled-unchanged")
    operation._require_mutation_attempts("code", [unchanged])
    record = _value(mutationHistory={"code": [unchanged]})
    operation._require_mutation_history(record)
    with pytest.raises(ValueError, match="reconciled-unchanged"):
        operation._require_mutation_attempts("code", [_value(leg="memory", state="commit-proven")])

    reversible = _value(
        mutationEvidence={"code": _value(state="pre-mutation")},
        legacyMigration=None,
        irreversibleBoundaryEntered=False,
    )
    operation._require_irreversible_boundary(reversible)
    proven = _value(
        mutationEvidence={"code": _value(state="commit-proven")},
        legacyMigration=None,
        irreversibleBoundaryEntered=True,
    )
    operation._require_irreversible_boundary(proven)
    assert operation._commit_proven(proven)
    proven.irreversibleBoundaryEntered = False
    with pytest.raises(ValueError, match="irreversible boundary"):
        operation._require_irreversible_boundary(proven)


def test_recovery_commits_cannot_contradict_commit_proof() -> None:
    proof = _value(state="commit-proven", commit="a" * 40)
    pending = _value(state="pre-mutation", commit=None)
    commits = _value(codeCommit="a" * 40, memoryContentCommit="", ledgerCommit="")
    operation._require_recovered_leg(commits, "codeCommit", proof)
    operation._require_recovered_leg(commits, "memoryContentCommit", pending)

    record = _value(recoveryCommits=commits, mutationEvidence={"code": proof})
    operation._require_recovery_commit_evidence(record)
    operation._require_recovery_commit_evidence(_value(recoveryCommits=None))
    commits.codeCommit = "b" * 40
    with pytest.raises(ValueError, match="contradicts"):
        operation._require_recovered_leg(commits, "codeCommit", proof)


def test_worker_binding_and_termination_evidence_are_one_authority() -> None:
    none_binding = (None, None, None)
    full_binding = (123, "a" * 64, "b" * 64)
    operation._require_complete_worker_binding(none_binding)
    operation._require_complete_worker_binding(full_binding)
    assert not operation._binding_present(none_binding)
    assert operation._binding_present(full_binding)
    with pytest.raises(ValueError, match="one authority"):
        operation._require_complete_worker_binding((123, None, None))

    normal = _value(status="running", terminationReturnStatus=None, terminationReturnPhase=None)
    requested = _value(
        status="termination-required",
        terminationReturnStatus="running",
        terminationReturnPhase="source-merge",
    )
    operation._require_termination_return_identity(normal)
    operation._require_termination_return_identity(requested)
    with pytest.raises(ValueError, match="return status"):
        operation._require_termination_return_identity(
            _value(
                status="termination-required",
                terminationReturnStatus="running",
                terminationReturnPhase=None,
            )
        )

    operation._require_no_termination_return(normal)
    with pytest.raises(ValueError, match="durable termination"):
        operation._require_no_termination_return(requested)

    live_record = _value(workerPid=123, workerLease="a" * 64)
    live = _value(state="requested", pid=123, lease="a" * 64)
    exited = _value(state="exited", pid=123, lease="a" * 64)
    operation._require_live_termination_identity(live_record, live)
    operation._require_live_termination_identity(live_record, exited)
    with pytest.raises(ValueError, match="exact pid and lease"):
        operation._require_live_termination_identity(
            live_record, _value(state="requested", pid=456, lease="a" * 64)
        )

    operation._require_exited_worker_release(live, full_binding)
    operation._require_exited_worker_release(exited, none_binding)
    with pytest.raises(ValueError, match="release pid"):
        operation._require_exited_worker_release(exited, full_binding)
