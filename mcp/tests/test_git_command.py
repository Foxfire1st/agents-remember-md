"""The one git runner: redirection safety, timeouts, stdin, and no second runner.

This module exists because the guard it tests used to live in the wrong place. Six
copies of ``run_git`` had drifted apart -- only the kernel's stripped the repository
selectors -- and the suite could not see it, because ``mcp/tests/conftest.py`` removes
those variables from ``os.environ`` at import. That strip is correct for fixture safety
and stays, but it also meant no test anywhere could observe a call site that failed to
strip them: the mitigation for the production hazard was installed in the only place
that could have detected it.
So every redirection test below re-SETS the selectors inside its own scope, defeating
the conftest strip on purpose. They pass because production strips, not because the
harness did; delete the conftest lines and these still pass, and delete the ``env=``
from the runner and these fail.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.kernel.git_command import (
    GIT_REPOSITORY_SELECTOR_ENV,
    admit_private_git_preparation,
    inspect_git_preparation,
    preparation_command,
    read_git_commit_bytes,
    run_git,
    run_git_preparation,
)
from agents_remember.kernel.git_preparation import (
    GitPreparationError,
    PrivateGitPreparationBinding,
    PrivateGitPreparationCapability,
)
from agents_remember.worktrees.modules.git import (
    commit_if_dirty,
    head_commit,
    worktree_candidate_tree,
)

PACKAGE_ROOT = MCP_SRC / "agents_remember"

# The spawn entry points of the stdlib's subprocess module. A module reaches one of these
# either through the module object (``subprocess.run``) or through a name it imported from
# it (``from subprocess import run``); the sweep below follows both.
SPAWN_FUNCTIONS = frozenset({"run", "Popen", "check_output", "check_call", "call"})


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "git-command@example.invalid"],
        ["config", "user.name", "Git Command Tests"],
    ):
        assert run_git(repo, args).returncode == 0, args


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    assert run_git(repo, ["add", "-A"]).returncode == 0
    assert run_git(repo, ["commit", "-m", f"add {name}"]).returncode == 0
    return run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()


def _selectors(decoy: Path) -> dict[str, str]:
    """Every selector pointed at ``decoy``, i.e. the worst case the runner must survive."""
    return {
        "GIT_DIR": str(decoy / ".git"),
        "GIT_WORK_TREE": str(decoy),
        "GIT_INDEX_FILE": str(decoy / ".git" / "index"),
        "GIT_OBJECT_DIRECTORY": str(decoy / ".git" / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(decoy / ".git" / "objects"),
        "GIT_COMMON_DIR": str(decoy / ".git"),
        "GIT_NAMESPACE": "redirected",
        "GIT_PREFIX": "redirected/",
    }


class DecoyRepositoryTests(unittest.TestCase):
    """A decoy repository named by GIT_DIR must never receive the real repo's writes."""

    def test_a_commit_lands_in_the_real_repository_not_the_decoy(self) -> None:
        # The exact defect: `commit_if_dirty` is what closeout runs, and it used to go
        # through a runner with no `env=`. With GIT_DIR exported that commit landed in
        # the decoy -- the real branch never moved and the decoy grew a commit nobody
        # asked for. Both halves are asserted; checking only the real repo would still
        # pass if the write were duplicated into the decoy.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real, decoy = root / "real", root / "decoy"
            _init(real)
            _init(decoy)
            real_before = _commit(real, "real.txt", "one\n")
            decoy_before = _commit(decoy, "decoy.txt", "decoy\n")
            (real / "real.txt").write_text("two\n", encoding="utf-8")

            with patch.dict(os.environ, _selectors(decoy)):
                # The conftest strip is undone inside this block on purpose.
                self.assertTrue(set(GIT_REPOSITORY_SELECTOR_ENV).issubset(os.environ))
                committed = commit_if_dirty(real, "carry the change")

            self.assertNotEqual(committed, real_before)  # the real branch advanced
            self.assertEqual(head_commit(real), committed)
            self.assertEqual(head_commit(decoy), decoy_before)  # the decoy did NOT
            self.assertEqual((real / "real.txt").read_text(encoding="utf-8"), "two\n")
            self.assertFalse((decoy / "real.txt").exists())


class RunnerContractTests(unittest.TestCase):
    def test_an_explicit_timeout_still_bounds_a_stalled_command(self) -> None:
        # The other side of per-call timeouts: raising the default must not amount to
        # removing the bound. A caller that names a short one still gets it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "file.txt", "one\n")
            stall = ["-c", "alias.stall=!sleep 5", "stall"]

            with self.assertRaises(subprocess.TimeoutExpired):
                run_git(repo, stall, timeout=1)


class CandidateTreeConcurrencyTests(unittest.TestCase):
    def test_candidate_tree_isolates_concurrent_observers_with_one_scratch_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            _commit(repo, "file.txt", "one\n")
            (repo / "untracked.txt").write_text("candidate\n", encoding="utf-8")
            index = root / "candidate.index"

            with ThreadPoolExecutor(max_workers=8) as executor:
                trees = list(
                    executor.map(lambda _: worktree_candidate_tree(repo, index), range(24))
                )

            self.assertEqual(len(set(trees)), 1)
            self.assertFalse(index.exists())
            self.assertEqual(list(root.glob(f".{index.name}-*")), [])


class PrivateGitPreparationTests(unittest.TestCase):
    """Real Git state behind the primitive; lifecycle admission belongs to its caller."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "logical"
        _init(self.repo)
        self.parent = _commit(self.repo, "source.txt", "original\n")
        (self.repo / "source.txt").write_text("candidate\n", encoding="utf-8")
        self.assertEqual(run_git(self.repo, ["add", "source.txt"]).returncode, 0)
        tree = run_git(self.repo, ["write-tree"]).stdout.strip()
        self.common = self.repo / ".git"
        self.before_index = (self.common / "index").read_bytes()
        self.binding = PrivateGitPreparationBinding(
            self.repo,
            "refs/heads/main",
            self.parent,
            self.common,
            self.root / "named-output",
            self.parent,
            tree,
            "prepared output",
            "strict-code-no-verify",
            "operation-1",
            1,
            "a" * 64,
        )
        self.authorizations: list[PrivateGitPreparationBinding] = []
        self.cancelled = False

    def authorize(self, binding: PrivateGitPreparationBinding) -> None:
        self.authorizations.append(binding)
        if self.cancelled:
            raise RuntimeError("operation cancelled")

    def _materialize(self) -> PrivateGitPreparationCapability:
        capability = admit_private_git_preparation(self.binding, authorize=self.authorize)
        self.assertEqual(run_git_preparation(capability, "create").returncode, 0)
        self.assertEqual(run_git_preparation(capability, "materialize").returncode, 0)
        return capability

    def _unchanged_logical(self) -> None:
        self.assertEqual(run_git(self.repo, ["rev-parse", "HEAD"]).stdout.strip(), self.parent)
        self.assertEqual((self.common / "index").read_bytes(), self.before_index)
        self.assertEqual((self.repo / "source.txt").read_text(), "candidate\n")

    def test_exact_private_commit_preserves_logical_state_and_normal_hook_policy(self) -> None:
        log = self.common / "hook-invocations"
        for hook in ("pre-commit", "commit-msg", "prepare-commit-msg", "post-commit"):
            path = self.common / "hooks" / hook
            path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{hook}' >> '{log}'\n", encoding="utf-8")
            path.chmod(0o755)
        for policy, expected in (
            ("strict-code-no-verify", ["prepare-commit-msg", "post-commit"]),
            ("ordinary", ["pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"]),
        ):
            with self.subTest(policy=policy):
                self.binding = replace(
                    self.binding, private_root=self.root / policy, hook_policy=policy
                )
                if log.exists():
                    log.unlink()
                capability = self._materialize()
                command = preparation_command(self.binding, "commit")
                with patch.object(subprocess, "run", wraps=subprocess.run) as spawned:
                    self.assertEqual(run_git_preparation(capability, "commit").returncode, 0)
                self.assertIn(list(command.argv), [call.args[0] for call in spawned.call_args_list])
                self.assertEqual(command.cwd, self.binding.private_root)
                observed = inspect_git_preparation(capability)
                self.assertEqual(observed.state, "committed")
                self.assertEqual(observed.tree, self.binding.admitted_tree)
                self.assertEqual(observed.parents, (self.parent,))
                self.assertIsNotNone(observed.raw_commit)
                self.assertEqual(log.read_text().splitlines(), expected)
                with self.assertRaisesRegex(GitPreparationError, "not admitted from committed"):
                    run_git_preparation(capability, "commit")
                self.assertEqual(log.read_text().splitlines(), expected)
                self._unchanged_logical()
        self.assertGreater(len(self.authorizations), 12)

    def test_raw_commit_readback_preserves_crlf_and_opaque_signature_header(self) -> None:
        capability = self._materialize()
        self.assertEqual(run_git_preparation(capability, "commit").returncode, 0)
        original = inspect_git_preparation(capability)
        assert original.raw_commit is not None and original.head is not None
        headers = original.raw_commit.split(b"\n\n", 1)[0]
        raw = (
            headers
            + b"\ngpgsig -----BEGIN PGP SIGNATURE-----\r\n opaque\r\n -----END PGP SIGNATURE-----\n\nopaque\r\nmessage\xff\r\n"
        )
        # An actual Git object tests lossless transport; the opaque header claims no verified signature.
        written = run_git(
            self.repo,
            ["hash-object", "-t", "commit", "-w", "--stdin"],
            input_text=raw.decode("utf-8", "surrogateescape"),
        )
        self.assertEqual(written.returncode, 0, written.stderr)
        object_id = written.stdout.strip()
        self.assertEqual(
            run_git(
                self.binding.private_root, ["update-ref", "HEAD", object_id, original.head]
            ).returncode,
            0,
        )
        observed = inspect_git_preparation(capability)
        self.assertEqual(observed.raw_commit, raw)
        self.assertEqual(read_git_commit_bytes(self.repo, object_id), raw)
        self.assertEqual(observed.head, object_id)
        self._unchanged_logical()

    def test_cancelled_owner_and_forged_capability_start_no_commit(self) -> None:
        capability = self._materialize()
        forged = PrivateGitPreparationCapability(self.binding, self.authorize, object())
        with self.assertRaisesRegex(GitPreparationError, "not admitted"):
            run_git_preparation(forged, "commit")
        self.cancelled = True
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            run_git_preparation(capability, "commit")
        self.assertEqual(
            run_git(self.binding.private_root, ["rev-parse", "HEAD"]).stdout.strip(), self.parent
        )
        self._unchanged_logical()

    def test_hidden_index_flags_and_changed_physical_bytes_refuse_commit(self) -> None:
        capability = self._materialize()
        private = self.binding.private_root
        self.assertEqual(
            run_git(private, ["update-index", "--assume-unchanged", "source.txt"]).returncode, 0
        )
        with self.assertRaisesRegex(GitPreparationError, "hidden source flags"):
            run_git_preparation(capability, "commit")
        self.assertEqual(
            run_git(private, ["update-index", "--no-assume-unchanged", "source.txt"]).returncode, 0
        )
        (private / "source.txt").write_text("tampered!\n", encoding="utf-8")
        with self.assertRaisesRegex(GitPreparationError, "bytes differ"):
            run_git_preparation(capability, "commit")
        self.assertEqual(run_git(private, ["rev-parse", "HEAD"]).stdout.strip(), self.parent)
        self._unchanged_logical()

    def test_stale_logical_tip_and_rebound_private_metadata_refuse_before_mutation(self) -> None:
        capability = self._materialize()
        other = self.root / "other-output"
        self.assertEqual(
            run_git(
                self.repo, ["worktree", "add", "--detach", "--no-checkout", str(other), self.parent]
            ).returncode,
            0,
        )
        dot_git = self.binding.private_root / ".git"
        original_pointer = dot_git.read_bytes()
        dot_git.write_bytes((other / ".git").read_bytes())
        with self.assertRaisesRegex(GitPreparationError, "backlink changed"):
            run_git_preparation(capability, "commit")
        dot_git.write_bytes(original_pointer)
        self.assertEqual(
            run_git(self.repo, ["commit", "-m", "foreign logical update"]).returncode, 0
        )
        moved = run_git(self.repo, ["rev-parse", "HEAD"]).stdout.strip()
        with self.assertRaisesRegex(GitPreparationError, "logical Git preparation binding changed"):
            run_git_preparation(capability, "commit")
        self.assertEqual(run_git(self.repo, ["rev-parse", "HEAD"]).stdout.strip(), moved)
        self.assertEqual(
            run_git(self.binding.private_root, ["rev-parse", "HEAD"]).stdout.strip(), self.parent
        )

    def test_failed_hook_returns_original_failure_and_does_not_retry(self) -> None:
        self.binding = replace(self.binding, hook_policy="ordinary")
        capability = self._materialize()
        hook = self.common / "hooks" / "pre-commit"
        log = self.common / "failed-hook-count"
        hook.write_text(
            f"#!/bin/sh\necho called >> '{log}'\necho original-refusal >&2\nexit 1\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        result = run_git_preparation(capability, "commit")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("original-refusal", result.stderr)
        self.assertEqual(log.read_text().splitlines(), ["called"])
        self.assertEqual(inspect_git_preparation(capability).state, "materialized")
        self._unchanged_logical()


if __name__ == "__main__":
    unittest.main()
