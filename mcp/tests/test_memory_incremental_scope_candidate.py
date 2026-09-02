"""Exact Git-root authority tests for the R06 scope candidate."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from agents_remember.kernel.git_command import run_git
from agents_remember.memory_quality.incremental_scope.candidate import (
    observe_contract_task_pair,
    observe_git_tree_delta,
)
from agents_remember.memory_quality.incremental_scope.errors import ScopeUnprovenError
from agents_remember.worktrees.modules.git import worktree_candidate_tree
from agents_remember.worktrees.worktree_contract import WorktreeContract


def _git(repository: Path, *args: str) -> str:
    result = run_git(repository, list(args))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "R06 Test")
    _git(repository, "config", "user.email", "r06@example.invalid")
    for relative, content in {
        "changed.txt": "before\n",
        "deleted.txt": "deleted\n",
        "old-name.txt": "rename body\n",
        "untouched.txt": "same\n",
    }.items():
        (repository / relative).write_text(content, encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_exact_tree_diff_classifies_add_modify_delete_and_both_rename_ends(
    tmp_path: Path,
) -> None:
    repository, base = _repository(tmp_path)
    (repository / "changed.txt").write_text("after\n", encoding="utf-8")
    (repository / "deleted.txt").unlink()
    (repository / "added.txt").write_text("added\n", encoding="utf-8")
    (repository / "old-name.txt").rename(repository / "new-name.txt")
    candidate = worktree_candidate_tree(repository, tmp_path / "candidate.index")

    delta = observe_git_tree_delta(
        repository,
        namespace="code",
        root=repository.resolve().as_posix(),
        base_ref=base,
        candidate_tree=candidate,
    )

    observed = {(change.status, change.oldPath, change.newPath) for change in delta.changes}
    assert observed == {
        ("added", None, "added.txt"),
        ("modified", "changed.txt", "changed.txt"),
        ("deleted", "deleted.txt", None),
        ("renamed", "old-name.txt", "new-name.txt"),
    }
    renamed = next(change for change in delta.changes if change.status == "renamed")
    assert renamed.oldBlob == renamed.newBlob


def test_mtime_only_change_is_not_a_changed_root(tmp_path: Path) -> None:
    repository, base = _repository(tmp_path)
    untouched = repository / "untouched.txt"
    before = untouched.stat().st_mtime_ns
    os.utime(untouched, ns=(before + 1_000_000, before + 1_000_000))
    candidate = worktree_candidate_tree(repository, tmp_path / "candidate.index")

    delta = observe_git_tree_delta(
        repository,
        namespace="code",
        root=repository.resolve().as_posix(),
        base_ref=base,
        candidate_tree=candidate,
    )

    assert delta.baseTree == delta.candidateTree
    assert delta.changes == ()


def test_missing_canonical_task_baseline_refuses_before_current_identity_is_derived(
    tmp_path: Path,
) -> None:
    contract = WorktreeContract(
        task_id="task",
        task_name="master",
        repo_name="repo",
        workflow_kind="light-task",
        memory_mode="external",
        coordination_root=tmp_path / "coordination",
        task_root=tmp_path / "coordination/tasks/repo/master",
        contract_path=tmp_path
        / "coordination/tasks/repo/master/enclosures/leaf/series-contract.md",
        task_artifact=tmp_path / "coordination/tasks/repo/master/task.md",
        worktree_group=tmp_path / "group",
        code_repo_path=tmp_path / "code-repo",
        code_source_branch="main",
        code_work_branch="work",
        code_base_commit="1" * 40,
        code_worktree=tmp_path / "code",
        memory_repo_path=tmp_path / "memory-repo",
        memory_source_branch="memory",
        memory_work_branch="work-memory",
        memory_base_commit="2" * 40,
        memory_worktree=tmp_path / "memory",
        ledger_path=tmp_path / "memory/memory.md",
        leaf_id="leaf",
    )

    with pytest.raises(ScopeUnprovenError) as caught:
        observe_contract_task_pair(
            contract,
            code_candidate="3" * 40,
            memory_candidate="4" * 40,
        )

    assert caught.value.failure.code == "task-base-unavailable"
