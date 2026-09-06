from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.worktrees.modules import closeout as closeout_module
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.queue import closeout_staged_quality
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    install_agents_remember_profile,
    install_fixture_profile,
)
from test_worktree_support import (
    git,
    init_repo,
)


def _checkout_with_profile(root: Path, *, repository_id: str = "agents-remember") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    repository = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if repository.returncode != 0:
        init_repo(root)
    if repository_id == "agents-remember":
        install_agents_remember_profile(root)
    else:
        install_fixture_profile(root, repository_id)
    return root


def _quality_target(
    worktree: Path,
    worktree_group: Path | None = None,
    *,
    repository_id: str = "agents-remember",
) -> code_quality_gate.QualityGateTarget:
    return code_quality_gate.QualityGateTarget(
        code_worktree=worktree,
        worktree_group=worktree_group or worktree / "enclosure",
        repository_id=repository_id,
        profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
    )


GATE_REFUSAL = "strict code-quality gate failed before code commit with exit code 1"


def _task_worktree(root: Path) -> tuple[Path, Path]:
    """A real repository and linked worktree off it -- the shape closeout stages in.
    Both are real: the precondition is git's own distinction, so a fake would test itself.
    """
    repo = root / "repo"
    init_repo(repo, "main")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    install_agents_remember_profile(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "Add a tracked file and certification profile")
    worktree = root / "task-worktree"
    git(repo, "worktree", "add", "-b", "ar/task", str(worktree), "main")
    return repo, worktree


class CertifiedIndexCommitTests(unittest.TestCase):
    def test_async_candidate_refuses_later_worktree_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            (worktree / "tracked.txt").write_text("accepted\n", encoding="utf-8")
            candidate = closeout_staged_quality.worktree_candidate_tree(
                worktree, worktree.parent / "candidate.index"
            )
            (worktree / "tracked.txt").write_text("later\n", encoding="utf-8")

            with (
                mock.patch.object(closeout_staged_quality, "run_strict_code_quality_gate") as gate,
                self.assertRaisesRegex(RuntimeError, "candidate changed"),
            ):
                closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                    candidate_tree=candidate,
                )

            gate.assert_not_called()

    def test_materialized_index_must_equal_the_accepted_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            (worktree / "accepted.py").write_text("VALUE = 1\n", encoding="utf-8")
            candidate = closeout_staged_quality.worktree_candidate_tree(
                worktree, worktree.parent / "candidate.index"
            )
            real_require_git = closeout_staged_quality.require_git

            def mismatched_write_tree(repo: Path, args: list[str]) -> str:
                if args == ["write-tree"]:
                    return "f" * 40
                return real_require_git(repo, args)

            with (
                mock.patch.object(
                    closeout_staged_quality, "worktree_candidate_tree", return_value=candidate
                ),
                mock.patch.object(
                    closeout_staged_quality, "require_git", side_effect=mismatched_write_tree
                ),
                self.assertRaisesRegex(RuntimeError, "while materializing the accepted tree"),
            ):
                closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                    candidate_tree=candidate,
                )

    def test_hook_mutation_invalidates_the_independently_reviewed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, worktree = _task_worktree(Path(tmp))
            hooks = worktree / ".githooks"
            hooks.mkdir()
            hook = hooks / "pre-commit"
            hook.write_text(
                "#!/bin/sh\nprintf 'hooked\\n' > tracked.txt\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            git(worktree, "config", "core.hooksPath", ".githooks")
            (worktree / "tracked.txt").write_text("reviewed\n", encoding="utf-8")
            candidate = closeout_staged_quality.worktree_candidate_tree(
                worktree, worktree.parent / "candidate.index"
            )

            with (
                mock.patch.object(closeout_staged_quality, "run_strict_code_quality_gate") as gate,
                self.assertRaisesRegex(
                    RuntimeError, "pre-commit hook changed the independently reviewed candidate"
                ),
            ):
                closeout_module._gate_staged_code(
                    _quality_target(worktree, worktree.parent),
                    diff_base="HEAD",
                    candidate_tree=candidate,
                )

            gate.assert_not_called()


DROPPED_TOOL_ARTEFACT = ".dmypy.json"
