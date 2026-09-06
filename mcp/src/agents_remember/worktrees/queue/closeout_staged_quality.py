"""Exact staged-candidate quality enforcement for worktree closeout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agents_remember.models.test_evidence import EvidenceConsumer
from agents_remember.worktrees.modules.git import (
    require_git,
    run_pre_commit_hook_if_configured,
    worktree_candidate_tree,
)
from agents_remember.worktrees.modules.models import PATH_SAMPLE_LIMIT
from agents_remember.worktrees.modules.quality.clean_executor import (
    require_published_quality_evidence,
)
from agents_remember.worktrees.modules.quality.gate import (
    QualityGatePlan,
    QualityGateTarget,
    run_strict_code_quality_gate,
)


def _refuse_outside_a_linked_worktree(code_worktree: Path) -> None:
    """Refuse to stage anywhere except a task's own throwaway worktree."""
    git_dir, common_dir = require_git(
        code_worktree, ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"]
    ).splitlines()
    if git_dir == common_dir:
        raise RuntimeError(
            "closeout refuses to run the strict code-quality gate here: it stages the whole code"
            f" checkout before the gate, and {code_worktree} is not a task worktree -- git"
            f" reports its git dir as {git_dir}, which is the repository's own, so this is a"
            " checkout a person works in. Staging it would overwrite a partial 'git add -p'"
            " selection, stage files deliberately held back, and resolve any merge in progress"
            " to whatever is on disk. Nothing was staged and nothing was committed. Closeout"
            " stages only the disposable worktree a task was started with; run it against the"
            " leaf contract whose code_worktree is that worktree. A series or master contract"
            " records the repository path itself and is not closed out this way."
        )


def _refuse_conflicted_worktree(code_worktree: Path) -> None:
    """Refuse before staging when the checkout has unresolved conflicts."""
    conflicted = require_git(code_worktree, ["diff", "--name-only", "--diff-filter=U"]).splitlines()
    if conflicted:
        raise RuntimeError(
            "closeout cannot stage the code worktree for the strict code-quality gate: the"
            f" index has {len(conflicted)} unmerged path(s), so a merge, rebase, cherry-pick or"
            " revert is still in progress with its conflicts unresolved"
            f" ({', '.join(conflicted[:PATH_SAMPLE_LIMIT])})."
            " Nothing was staged and nothing was committed. Resolve the conflicts, stage the"
            " resolutions, then rerun closeout -- staging a conflicted worktree would commit the"
            " conflict markers themselves."
        )


def _require_candidate_match(expected: str, actual: str, *, message: str) -> None:
    if actual != expected:
        raise RuntimeError(f"{message}: expected {expected}, found {actual}")


def _run_reviewed_pre_commit_hook(code_worktree: Path, candidate_tree: str | None) -> bool:
    pre_commit_hook_ran = run_pre_commit_hook_if_configured(code_worktree)
    if not pre_commit_hook_ran:
        return False
    require_git(code_worktree, ["add", "-A"])
    if candidate_tree is not None:
        hooked_tree = require_git(code_worktree, ["write-tree"])
        _require_candidate_match(
            candidate_tree,
            hooked_tree,
            message=(
                "pre-commit hook changed the independently reviewed candidate: "
                "nothing was committed; review the hook-updated candidate before retrying"
            ),
        )
    return True


@dataclass(frozen=True)
class PreparedStagedCode:
    """The actual strict preparation outcome, before any admission is frozen."""

    candidate_tree: str
    pre_commit_hook_ran: bool


def prepare_staged_code(
    target: QualityGateTarget,
    *,
    candidate_tree: str | None = None,
) -> PreparedStagedCode:
    """Settle the actual strict hook before freezing certification admission.

    Both refusals precede the mixed reset: the checkout must be disposable and its index
    conflict-free before closeout is allowed to replace the task index. Every retry resets
    and restages the working tree so ignored or deleted paths cannot leak in from an earlier
    refused attempt. A candidate tree, when supplied, is proven before staging, after
    staging, and after any configured pre-commit hook.
    """
    code_worktree = target.code_worktree
    worktree_group = target.worktree_group
    _refuse_outside_a_linked_worktree(code_worktree)
    _refuse_conflicted_worktree(code_worktree)
    if candidate_tree is not None:
        current = worktree_candidate_tree(
            code_worktree,
            worktree_group / "reports" / ".closeout-verify.index",
        )
        _require_candidate_match(
            candidate_tree,
            current,
            message=(
                "closeout candidate changed after the asynchronous operation was accepted: "
                "nothing was committed; restart closeout to bind the updated candidate"
            ),
        )
    require_git(code_worktree, ["reset", "--mixed", "--quiet", "HEAD"])
    require_git(code_worktree, ["add", "-A"])
    if candidate_tree is not None:
        staged = require_git(code_worktree, ["write-tree"])
        _require_candidate_match(
            candidate_tree,
            staged,
            message=(
                "closeout candidate changed while materializing the accepted tree: "
                "nothing was committed; restart closeout to bind the updated candidate"
            ),
        )
    pre_commit_hook_ran = _run_reviewed_pre_commit_hook(code_worktree, candidate_tree)
    return PreparedStagedCode(
        candidate_tree=require_git(code_worktree, ["write-tree"]),
        pre_commit_hook_ran=pre_commit_hook_ran,
    )


def gate_staged_code(
    target: QualityGateTarget,
    *,
    diff_base: str,
    candidate_tree: str | None = None,
) -> dict[str, object]:
    """Prepare and certify a fresh candidate through the ordinary gate entry point."""
    prepared = prepare_staged_code(target, candidate_tree=candidate_tree)
    code_worktree = target.code_worktree
    worktree_group = target.worktree_group
    result = run_strict_code_quality_gate(
        target,
        diff_base=diff_base,
        plan=QualityGatePlan(mode="targeted"),
    )
    certified_tree = require_git(code_worktree, ["write-tree"])
    evidence = require_published_quality_evidence(
        worktree_group / "reports",
        candidate_tree=certified_tree,
        consumer=EvidenceConsumer.CLOSEOUT,
    )
    if candidate_tree is not None and evidence.candidate_tree != candidate_tree:
        raise RuntimeError("closeout quality evidence does not match the reviewed candidate")
    return {
        **result,
        "preCommitHook": "passed" if prepared.pre_commit_hook_ran else "not-configured",
    }
