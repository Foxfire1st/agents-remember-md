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

import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path, PurePosixPath
from unittest.mock import patch

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.benchmarks.runner_modules import commands, workspace
from agents_remember.benchmarks.runner_modules.commands import (
    repo_has_commit,
    run_git_command,
)
from agents_remember.kernel import git_command as git_command_module
from agents_remember.kernel import git_facts, git_freshness
from agents_remember.kernel.coordination_context import cross_repo
from agents_remember.kernel.git_command import (
    GIT_BULK_REMOTE_TIMEOUT_SECONDS,
    GIT_LOCAL_TIMEOUT_SECONDS,
    GIT_METADATA_TIMEOUT_SECONDS,
    GIT_REMOTE_TIMEOUT_SECONDS,
    GIT_REPOSITORY_SELECTOR_ENV,
    admit_private_git_preparation,
    git_environment,
    inspect_existing_git_preparation,
    inspect_git_preparation,
    preparation_command,
    read_git_commit_bytes,
    read_git_configuration_bytes,
    run_git,
    run_git_preparation,
    run_git_with_index,
)
from agents_remember.kernel.git_preparation import (
    GitPreparationError,
    PrivateGitPreparationBinding,
    PrivateGitPreparationCapability,
)
from agents_remember.worktrees.modules import cleanup
from agents_remember.worktrees.modules.git import (
    commit_if_dirty,
    head_commit,
    require_git,
    run_pre_commit_hook_if_configured,
    worktree_candidate_tree,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    default_contract,
)
from agents_remember_test_support.code_quality import check as quality_check
from agents_remember_test_support.code_quality import diff_coverage
from agents_remember_test_support.code_quality.single_owner import module_git_offenders

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


def _spawn_aliases(tree: ast.AST) -> set[str]:
    """Bare names this module bound to a subprocess spawn via ``from subprocess import ...``.

    Binding the name is the whole bypass: ``from subprocess import run`` then ``run([...])``
    is the same spawn with no ``subprocess.`` in front of it for an attribute match to find.
    Resolving it per module keeps the sweep from flagging any unrelated local ``run``.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name in SPAWN_FUNCTIONS
            )
    return aliases


def _spawn_calls(tree: ast.AST) -> list[ast.Call]:
    """Every call in this module that starts a child process."""
    aliases = _spawn_aliases(tree)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        attribute_spawn = (
            isinstance(function, ast.Attribute)
            and function.attr in SPAWN_FUNCTIONS
            and isinstance(function.value, ast.Name)
            and function.value.id == "subprocess"
        )
        imported_spawn = isinstance(function, ast.Name) and function.id in aliases
        if attribute_spawn or imported_spawn:
            calls.append(node)
    return calls


def _spawns_git(node: ast.Call) -> bool:
    """Whether this spawn's argv literal names the git program."""
    argv = node.args[0] if node.args else None
    head = argv.elts[0] if isinstance(argv, ast.List) and argv.elts else None
    if not isinstance(head, ast.Constant) or not isinstance(head.value, str):
        return False
    # ``/usr/bin/git`` is the same program as ``git``. Matching only the bare word let an
    # absolute path -- the ordinary way to pin a tool version -- walk straight past a guard
    # whose entire job is to notice git spawns.
    return PurePosixPath(head.value).name == "git"


def _passes_env(node: ast.Call) -> bool:
    """Whether this spawn names an ``env=``, which is the only form that proves anything.

    A ``**kwargs`` splat used to count. It cannot: the sweep sees the splat, not its
    contents, so ``subprocess.run(["git", ...], **kw)`` was accepted on the strength of a
    dictionary the guard never looked inside.
    """
    return any(keyword.arg == "env" for keyword in node.keywords)


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

    def test_reads_answer_from_the_real_repository_not_the_decoy(self) -> None:
        # The read half of the same defect: a redirected `rev-parse`/`log` reports the
        # decoy's history, and every decision built on it (is this merged? what changed?)
        # is then made about the wrong repository.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real, decoy = root / "real", root / "decoy"
            _init(real)
            _init(decoy)
            real_head = _commit(real, "real.txt", "one\n")
            decoy_head = _commit(decoy, "decoy.txt", "decoy\n")
            self.assertNotEqual(real_head, decoy_head)

            with patch.dict(os.environ, _selectors(decoy)):
                observed = run_git(real, ["rev-parse", "HEAD"]).stdout.strip()
                tracked = run_git(real, ["ls-files"]).stdout.split()

            self.assertEqual(observed, real_head)
            self.assertEqual(tracked, ["real.txt"])

    def test_git_environment_removes_every_selector_it_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _selectors(Path(tmp))):
                environment = git_environment()

            self.assertTrue(set(GIT_REPOSITORY_SELECTOR_ENV).isdisjoint(environment))
            # Only the selectors go: PATH and friends must survive or git cannot run.
            self.assertIn("PATH", environment)


class RunnerContractTests(unittest.TestCase):
    def test_candidate_tree_reports_each_isolated_index_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            _commit(repo, "file.txt", "one\n")
            index = root / "candidate.index"
            failed = subprocess.CompletedProcess(["git"], 1, stdout="", stderr="candidate refused")
            with (
                patch(
                    "agents_remember.worktrees.modules.git.run_git_with_index",
                    return_value=failed,
                ),
                self.assertRaisesRegex(RuntimeError, "could not seed candidate index"),
            ):
                worktree_candidate_tree(repo, index)

            success = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
            with (
                patch(
                    "agents_remember.worktrees.modules.git.run_git_with_index",
                    side_effect=[success, success, failed],
                ),
                self.assertRaisesRegex(RuntimeError, "could not resolve candidate tree"),
            ):
                worktree_candidate_tree(repo, index)

    def test_only_an_explicit_isolated_index_override_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "file.txt", "one\n")
            index = Path(tmp) / "candidate.index"

            self.assertEqual(run_git_with_index(repo, ["read-tree", "HEAD"], index).returncode, 0)
            self.assertTrue(index.is_file())

    def test_stdin_is_devnull_unless_input_text_is_given(self) -> None:
        # `git patch-id` reads a diff from stdin; everything else must get DEVNULL,
        # because under the stdio MCP transport the inherited descriptor is the
        # JSON-RPC request pipe and a child reading it wedges the tool call (#49).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            base = _commit(repo, "file.txt", "one\n")
            head = _commit(repo, "file.txt", "two\n")
            diff = run_git(repo, ["diff", base, head]).stdout

            piped = run_git(repo, ["patch-id", "--stable"], input_text=diff)
            self.assertEqual(piped.returncode, 0)
            self.assertTrue(piped.stdout.split())

            # Same command with no input: git sees EOF immediately and emits nothing,
            # rather than blocking on an inherited descriptor.
            empty = run_git(repo, ["patch-id", "--stable"])
            self.assertEqual(empty.stdout.strip(), "")

    def test_undecodable_output_is_carried_rather_than_raising(self) -> None:
        # errors="surrogateescape" with an explicit utf-8: a path that is not valid
        # UTF-8 must not turn a status query into a UnicodeDecodeError, and the encoding
        # must not vary with the platform's locale.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "file.txt", "one\n")
            try:
                (repo / os.fsdecode(b"odd-\xff-name.txt")).write_bytes(b"x\n")
            except (OSError, UnicodeEncodeError):  # pragma: no cover - platform dependent
                self.skipTest("filesystem rejects non-UTF-8 names")

            result = run_git(repo, ["status", "--porcelain"])

            self.assertEqual(result.returncode, 0)
            self.assertIn("odd-", result.stdout)

    def test_the_timeout_is_per_call_and_a_slow_command_is_not_cut_off_at_five_seconds(
        self,
    ) -> None:
        # The consolidation's precondition (L3-R6). The single runner used to hard-code
        # timeout=5, which is not a plausible bound for rebase/merge/push --delete, so
        # moving those call sites onto it unchanged would have replaced a redirection bug
        # with a five-second failure on every integrate. A command that deliberately takes
        # longer than the old bound must complete.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "file.txt", "one\n")
            slow = ["-c", "alias.slow=!sleep 6 && git rev-parse HEAD", "slow"]

            started = time.monotonic()
            result = run_git(repo, slow)
            elapsed = time.monotonic() - started

            self.assertEqual(result.returncode, 0)
            self.assertGreater(elapsed, 5.0)  # it really did outlive the old bound
            self.assertGreaterEqual(GIT_LOCAL_TIMEOUT_SECONDS, 60)

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

    def test_the_remote_bound_is_shorter_than_the_local_one(self) -> None:
        # Local work can legitimately churn for minutes; a remote that has not moved
        # bytes in two is wedged, and a wedged remote inside an MCP tool call cannot be
        # cancelled by the client.
        self.assertLess(GIT_REMOTE_TIMEOUT_SECONDS, GIT_LOCAL_TIMEOUT_SECONDS)

    def test_failed_hook_diagnostic_with_invalid_bytes_is_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "file.txt", "one\n")
            hook = repo / ".git" / "hooks" / "pre-commit"
            hook.write_bytes(b"#!/bin/sh\nprintf '\\201' >&2\nexit 1\n")
            hook.chmod(0o755)

            raw = run_git(repo, ["hook", "run", "pre-commit"])
            self.assertIn("\udc81", raw.stderr)

            with self.assertRaises(RuntimeError) as raised:
                run_pre_commit_hook_if_configured(repo)

            diagnostic = str(raised.exception)
            self.assertIn(r"\udc81", diagnostic)
            json.dumps({"error": diagnostic}, ensure_ascii=False).encode("utf-8")


class RemoteBranchStallTests(unittest.TestCase):
    """L3-R7: the two remote calls in cleanup used to run with no timeout at all."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        _init(self.repo)
        head = _commit(self.repo, "seed.txt", "seed\n")
        assert run_git(self.repo, ["branch", "feat/x", head]).returncode == 0
        assert run_git(self.repo, ["update-ref", "refs/remotes/origin/main", head]).returncode == 0
        assert (
            run_git(
                self.repo,
                ["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
            ).returncode
            == 0
        )
        contract = default_contract(
            ContractTask("terminal", "repo", self.root / "coordination", "light-task", "disabled"),
            leaf=LeafIdentity("terminal", leaf_id="T"),
            code=RepoBranchPlan(self.repo, "main", "feat/x", head),
        )
        self.authority = cleanup._terminal_mutation_authority(
            contract, operation="worktree_cleanup"
        )

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_a_stalled_probe_reports_unreachable_instead_of_hanging(self) -> None:
        with patch.object(
            cleanup, "run_git", side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=120)
        ):
            out = cleanup.delete_remote_branch_if_present(
                self.repo,
                "feat/x",
                dry_run=False,
                authority=self.authority,
            )

        self.assertEqual(out, {"remote_deleted": False, "reason": "remote-unreachable"})

    def test_a_stalled_push_reports_unreachable_instead_of_hanging(self) -> None:
        present = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="sha\tref\n")
        with patch.object(
            cleanup,
            "run_git",
            side_effect=[present, subprocess.TimeoutExpired(cmd=["git"], timeout=120)],
        ):
            out = cleanup.delete_remote_branch_if_present(
                self.repo,
                "feat/x",
                dry_run=False,
                authority=self.authority,
            )

        self.assertEqual(out, {"remote_deleted": False, "reason": "remote-unreachable"})

    def test_both_remote_calls_carry_the_remote_bound(self) -> None:
        present = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="sha\tref\n")
        pushed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="")
        with patch.object(cleanup, "run_git", side_effect=[present, pushed]) as runner:
            cleanup.delete_remote_branch_if_present(
                self.repo,
                "feat/x",
                dry_run=False,
                authority=self.authority,
            )

        self.assertEqual(
            [call.kwargs["timeout"] for call in runner.call_args_list],
            [GIT_REMOTE_TIMEOUT_SECONDS, GIT_REMOTE_TIMEOUT_SECONDS],
        )


class QualityGateGitTests(unittest.TestCase):
    """The gate's own git calls: they run from the pre-push hook, where GIT_DIR IS set."""

    def test_the_coverage_gate_reads_the_repository_it_was_pointed_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real, decoy = root / "real", root / "decoy"
            _init(real)
            _init(decoy)
            _commit(real, "real.py", "REAL = 1\n")
            _commit(decoy, "decoy.py", "DECOY = 1\n")

            with patch.dict(os.environ, _selectors(decoy)):
                tracked = quality_check.git_ls_files(real, "*.py")
                listed = diff_coverage.run_git(real, ["ls-files"]).split()

            self.assertEqual(tracked, [Path("real.py")])
            self.assertEqual(listed, ["real.py"])

    def test_the_closeout_gate_resolves_the_common_dir_of_the_worktree_it_was_given(self) -> None:
        # The shared required-git probe decides which repository the closeout quality
        # gate certifies. An inherited GIT_DIR must not redirect it to another repo.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real, decoy = root / "real", root / "decoy"
            _init(real)
            _init(decoy)
            _commit(real, "real.py", "REAL = 1\n")
            _commit(decoy, "decoy.py", "DECOY = 1\n")

            plain = root / "plain"
            plain.mkdir()

            with patch.dict(os.environ, _selectors(decoy)):
                common = Path(
                    require_git(
                        real,
                        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                    )
                )
                # A directory that is not a repository refuses rather than falling
                # through to the decoy that GIT_DIR is pointing at.
                with self.assertRaisesRegex(RuntimeError, "not a git repository"):
                    require_git(
                        plain,
                        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                    )

            self.assertEqual(common.resolve(), (real / ".git").resolve())

    def test_a_failed_gate_git_call_raises_the_typed_domain_error(self) -> None:
        # Not a repository: both wrappers must keep converting failure into their own
        # error rather than returning an empty scope, which would certify nothing.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(diff_coverage.DiffScopeError):
                diff_coverage.run_git(Path(tmp), ["rev-parse", "HEAD"])
            with self.assertRaises(quality_check.ScopeError):
                quality_check.git_ls_files(Path(tmp), "*.py")

    def test_an_unrunnable_git_also_raises_the_typed_domain_error(self) -> None:
        # The OSError path (git absent, cwd gone): it used to arrive as CalledProcessError
        # from check=True and must still not escape as a raw OSError.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(diff_coverage.git_command, "run_git", side_effect=OSError("no git")):
                with self.assertRaises(diff_coverage.DiffScopeError):
                    diff_coverage.run_git(root, ["ls-files"])
                with self.assertRaises(quality_check.ScopeError):
                    quality_check.git_ls_files(root, "*.py")


class SingleRunnerTests(unittest.TestCase):
    """L3-R8: production is safe on its own, and stays that way.

    ``test_subprocess_hygiene`` is the model: an AST sweep is what keeps a package-wide
    property from decaying one call site at a time. Six runners is exactly how this
    defect was born, so the guard is that no module may grow a seventh.

    Its reach is stated rather than assumed, and the statement is exact because a guard
    with an unstated hole reports zero offenders on a tree that has one. It recognises a
    subprocess spawn reached either through the module (``subprocess.run``) or through a
    name imported from it (``from subprocess import run``), whose argv is a **list literal**
    whose head names the git program bare or path-qualified (``git``, ``/usr/bin/git``),
    and it accepts only a literal ``env=`` as proof -- never a ``**kwargs`` splat, whose
    contents it cannot see. :class:`SingleRunnerGuardReachTests` plants each of those forms
    and fails if the sweep stops catching it.

    The environment sweep cannot see helper-composed argv. L6 removed a real second builder
    from ``benchmarks/runner_modules/commands.py``; the canonical owner detector now catches
    argv construction anywhere. This class uses that detector for ownership, as does
    ``test_single_owner_primitives.py``, while retaining the separate literal-env guard.
    """

    def _package_modules(self) -> list[Path]:
        return [
            path
            for path in sorted(PACKAGE_ROOT.rglob("*.py"))
            if "package_data" not in path.parts  # runtime assets, run outside this process
        ]

    def test_no_module_spawns_git_with_the_ambient_environment(self) -> None:
        # A spawn whose argv literally starts with "git" and which does not name an
        # `env=` inherits GIT_DIR and friends, and is therefore one exported variable
        # away from operating on a repository nobody chose.
        offenders: list[str] = []
        for path in self._package_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in _spawn_calls(tree):
                if _spawns_git(node) and not _passes_env(node):
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT).as_posix()}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            msg=(
                "git spawned with the ambient environment: an inherited GIT_DIR / "
                "GIT_WORK_TREE redirects the command onto another repository. Call "
                "agents_remember.kernel.git_command.run_git, or pass "
                "env=git_environment(): " + ", ".join(offenders)
            ),
        )

    def test_only_the_kernel_module_defines_a_git_runner(self) -> None:
        # The canonical owner sees direct spawns and the exact shared argv builder alike.
        spawners: list[str] = []
        for path in self._package_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            module = path.relative_to(PACKAGE_ROOT).as_posix()
            spawners.extend(finding.module for finding in module_git_offenders(tree, module))
        self.assertEqual(
            sorted(set(spawners)),
            ["kernel/git_command.py"],
            msg=(
                "a second git runner appeared. Six of these had drifted apart once, and "
                "only one of the six stripped the repository selectors; route the call "
                "through kernel.git_command.run_git instead of spawning git here."
            ),
        )


class SingleRunnerGuardReachTests(unittest.TestCase):
    """The guard on the guard: every bypass form is planted here and must be caught.

    :class:`SingleRunnerTests` is what stops a seventh runner appearing, and it does that by
    reporting an empty offender list. An empty list is also what it reports when its sweep
    cannot see the offender -- so a hole in the sweep does not look like a failure, it looks
    like a clean tree. These tests are the difference: each one plants a spawn the sweep is
    supposed to catch and fails if it does not, so the reach documented on that class is
    checked rather than asserted.
    """

    @staticmethod
    def _offenders(source: str) -> list[int]:
        """Line numbers the sweep would report for ``source``."""
        tree = ast.parse(source)
        return [
            node.lineno
            for node in _spawn_calls(tree)
            if _spawns_git(node) and not _passes_env(node)
        ]

    def test_a_spawn_imported_off_subprocess_is_still_a_spawn(self) -> None:
        # Missed before: the sweep required a `subprocess.<attr>` attribute access, so
        # dropping the module prefix at the import was enough to disappear from it.
        source = "from subprocess import run\nrun(['git', 'status'])\n"
        self.assertEqual(self._offenders(source), [2])

    def test_an_import_alias_is_followed_to_the_name_it_binds(self) -> None:
        source = "from subprocess import run as spawn\nspawn(['git', 'status'])\n"
        self.assertEqual(self._offenders(source), [2])

    def test_an_absolute_path_to_git_is_git(self) -> None:
        # Missed before: the argv head had to be the literal "git", so pinning the binary
        # by path -- an ordinary thing to do -- exempted the call from the guard.
        source = "import subprocess\nsubprocess.run(['/usr/bin/git', 'status'])\n"
        self.assertEqual(self._offenders(source), [2])

    def test_a_kwargs_splat_is_not_proof_that_env_was_passed(self) -> None:
        # Missed before: a `**kwargs` splat parses as `keyword.arg is None`, which the sweep
        # counted as an `env=`. It is the opposite -- the contents are exactly what it
        # cannot read.
        source = "import subprocess\nsubprocess.run(['git', 'status'], **kw)\n"
        self.assertEqual(self._offenders(source), [2])

    def test_every_spawn_entry_point_is_swept_not_only_run(self) -> None:
        source = (
            "import subprocess\n"
            "subprocess.Popen(['git', 'log'])\n"
            "subprocess.check_output(['git', 'log'])\n"
            "subprocess.check_call(['git', 'log'])\n"
            "subprocess.call(['git', 'log'])\n"
        )
        self.assertEqual(self._offenders(source), [2, 3, 4, 5])

    def test_a_named_env_clears_the_sweep(self) -> None:
        # The guard must still be passable the intended way, or the only way to satisfy it
        # is to stop spawning git -- and then it gets suppressed instead of obeyed.
        source = "import subprocess\nsubprocess.run(['git', 'status'], env=git_environment())\n"
        self.assertEqual(self._offenders(source), [])

    def test_a_program_that_merely_starts_with_git_is_not_git(self) -> None:
        source = "import subprocess\nsubprocess.run(['gitk'])\nsubprocess.run(['/usr/bin/gh'])\n"
        self.assertEqual(self._offenders(source), [])

    def test_a_local_function_named_run_is_not_a_subprocess_spawn(self) -> None:
        # The aliases are resolved per module rather than assumed from the name, so a module
        # with its own `run` that takes a git argv -- a dry-run printer, a queue -- is not
        # reported. A guard that cries wolf is a guard that gets an exemption list.
        source = "def run(argv):\n    return argv\n\n\nrun(['git', 'status'])\n"
        self.assertEqual(self._offenders(source), [])

    def test_a_computed_argv_is_this_sweeps_documented_blind_spot(self) -> None:
        # Stated, and closed elsewhere: this sweep reads argv list literals at the call site,
        # so an argv assembled beforehand is invisible to it. That is why the ownership rule
        # lives in `code_quality/single_owner.py`, which reports the list literal itself --
        # `argv = ['git', 'status']` on line 2 here -- rather than only the spawn. This test
        # exists so the limitation documented on SingleRunnerTests cannot quietly stop being
        # the true one.
        source = "import subprocess\nargv = ['git', 'status']\nsubprocess.run(argv)\n"
        self.assertEqual(self._offenders(source), [])


class TimeoutClassTests(unittest.TestCase):
    """The timeout class belongs to the command, not to the module it is called from.

    Before this leaf the kernel's runner hard-coded ``timeout=5``, so every read below was
    bounded at five seconds. Consolidating onto a runner whose default is the local bound
    silently moved them to 300s -- a 60x loosening on the reads that sit under
    ``resolve_context``, which runs on essentially every tool call. The band exists for
    exactly these commands, and which band a command gets is asserted here per command
    rather than left to whichever module happens to hold the call.
    """

    @staticmethod
    def _recorder(
        calls: list[tuple[str, float]], stdout: dict[str, str]
    ) -> Callable[..., subprocess.CompletedProcess[str]]:
        """A ``run_git`` stand-in that records ``(command, timeout)`` for every call.

        ``timeout`` is a required keyword here on purpose: a call site that leaves the class
        to the default fails this recorder rather than quietly recording the default.
        """

        def record(
            _root: Path, args: list[str], *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            command = " ".join(args)
            calls.append((command, float(timeout)))
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=0, stdout=stdout.get(command, ""), stderr=""
            )

        return record

    def test_read_git_facts_bounds_its_three_ref_reads_at_the_metadata_band(self) -> None:
        # The concrete failure this prevents: four probes on the local bound let one
        # `resolve_context` sit for twenty minutes behind a held index lock, with no
        # cancellation path for the MCP client.
        calls: list[tuple[str, float]] = []
        stdout = {"rev-parse --is-inside-work-tree": "true\n", "rev-parse HEAD": "c0ffee\n"}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(git_facts, "run_git", side_effect=self._recorder(calls, stdout)),
        ):
            facts = git_facts.read_git_facts("repo-a", Path(tmp))

        self.assertEqual(facts.head, "c0ffee")
        self.assertEqual(
            dict(calls),
            {
                "rev-parse --is-inside-work-tree": GIT_METADATA_TIMEOUT_SECONDS,
                "rev-parse HEAD": GIT_METADATA_TIMEOUT_SECONDS,
                "branch --show-current": GIT_METADATA_TIMEOUT_SECONDS,
                # not constant time -- it stats the whole work tree -- so it keeps the local bound
                "status --porcelain": GIT_LOCAL_TIMEOUT_SECONDS,
            },
        )

    def test_branch_freshness_classes_each_of_its_commands_by_what_it_does(self) -> None:
        calls: list[tuple[str, float]] = []
        stdout = {
            "branch --show-current": "main\n",
            "rev-parse --abbrev-ref main@{upstream}": "origin/main\n",
            "rev-list --left-right --count main...origin/main": "0\t0\n",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(git_freshness, "run_git", side_effect=self._recorder(calls, stdout)),
        ):
            freshness = git_freshness.read_branch_freshness(Path(tmp), fetch=False)

        self.assertEqual(freshness.state, "current")
        self.assertEqual(
            dict(calls),
            {
                "branch --show-current": GIT_METADATA_TIMEOUT_SECONDS,
                "rev-parse --abbrev-ref main@{upstream}": GIT_METADATA_TIMEOUT_SECONDS,
                # walks history, and how far depends on how far the refs have drifted
                "rev-list --left-right --count main...origin/main": GIT_LOCAL_TIMEOUT_SECONDS,
            },
        )

    def test_one_command_means_one_bound_across_the_kernel(self) -> None:
        # The drift this leaf exists to end. `branch --show-current` and `rev-parse HEAD` are
        # called from two kernel modules; they were bounded at 30s in cross_repo.py and 300s
        # in git_facts.py. Two answers for one command inside kernel/ is how the six runners
        # got here in the first place.
        cross_repo_calls: list[tuple[str, float]] = []
        facts_calls: list[tuple[str, float]] = []
        stdout = {"rev-parse --is-inside-work-tree": "true\n", "rev-parse HEAD": "c0ffee\n"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(
                cross_repo, "run_git", side_effect=self._recorder(cross_repo_calls, stdout)
            ):
                cross_repo.git_branch(root)
                cross_repo.git_head_or_empty(root)
            with patch.object(
                git_facts, "run_git", side_effect=self._recorder(facts_calls, stdout)
            ):
                git_facts.read_git_facts("repo-a", root)

        shared = {"branch --show-current", "rev-parse HEAD"}
        self.assertEqual(
            {command: bound for command, bound in cross_repo_calls if command in shared},
            {command: bound for command, bound in facts_calls if command in shared},
        )

    def test_the_metadata_bound_is_the_shortest_of_the_three(self) -> None:
        # A constant-time read that has not returned in 30s is blocked, not busy; letting it
        # inherit either of the working bounds turns "instant or stuck" into a long wait.
        self.assertLess(GIT_METADATA_TIMEOUT_SECONDS, GIT_REMOTE_TIMEOUT_SECONDS)
        self.assertLess(GIT_METADATA_TIMEOUT_SECONDS, GIT_LOCAL_TIMEOUT_SECONDS)


class BenchmarkRunnerEnvironmentTests(unittest.TestCase):
    """The most destructive argv in the package, proven safe through the one runner.

    ``clone``, ``checkout --detach``, ``reset --hard`` and ``clean -fdx`` used to be built
    and spawned by this module's own builder; they now go through ``run_git``. The property
    is asserted end to end rather than inferred from the routing, because "it calls the safe
    runner now" is exactly the kind of claim that stops being true one refactor later.
    """

    def test_a_hard_reset_reverts_the_named_repository_not_an_inherited_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real, decoy = root / "real", root / "decoy"
            _init(real)
            _init(decoy)
            _commit(real, "file.txt", "one\n")
            _commit(decoy, "file.txt", "decoy-one\n")
            (real / "file.txt").write_text("two\n", encoding="utf-8")
            (decoy / "file.txt").write_text("decoy-two\n", encoding="utf-8")

            with patch.dict(os.environ, _selectors(decoy)):
                run_git_command(real, ["reset", "--hard"], dry_run=False)

            self.assertEqual((real / "file.txt").read_text(encoding="utf-8"), "one\n")
            # The decoy's uncommitted work is exactly what a redirected reset destroys.
            self.assertEqual((decoy / "file.txt").read_text(encoding="utf-8"), "decoy-two\n")

    def test_commit_presence_is_answered_by_the_named_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real, decoy = root / "real", root / "decoy"
            _init(real)
            _init(decoy)
            real_head = _commit(real, "real.txt", "one\n")
            decoy_head = _commit(decoy, "decoy.txt", "decoy\n")

            with patch.dict(os.environ, _selectors(decoy)):
                self.assertTrue(repo_has_commit(real, real_head))
                self.assertFalse(repo_has_commit(real, decoy_head))

    def test_a_clone_into_a_fresh_destination_is_redirection_safe_too(self) -> None:
        # The command that could not go through `run_git` before L6, and the reason the
        # runner grew `work_dir`: its destination does not exist when it starts.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream, decoy = root / "upstream", root / "decoy"
            _init(upstream)
            _init(decoy)
            head = _commit(upstream, "file.txt", "one\n")
            destination = root / "clones" / "repo-a"
            destination.parent.mkdir(parents=True)

            with patch.dict(os.environ, _selectors(decoy)):
                run_git_command(
                    destination,
                    ["clone", str(upstream), str(destination)],
                    dry_run=False,
                    work_dir=destination.parent,
                )

            self.assertTrue((destination / ".git").exists())
            self.assertEqual(run_git(destination, ["rev-parse", "HEAD"]).stdout.strip(), head)

    def test_the_work_dir_is_what_makes_that_clone_possible(self) -> None:
        # Without it the child's cwd is the repository being created, which does not exist
        # yet -- so this is the failure the parameter exists to avoid, not a style choice.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream = root / "upstream"
            _init(upstream)
            _commit(upstream, "file.txt", "one\n")
            destination = root / "clones" / "repo-a"
            destination.parent.mkdir(parents=True)

            with self.assertRaises(FileNotFoundError):
                run_git(destination, ["clone", str(upstream), str(destination)])

    def test_a_failed_command_raises_with_gits_own_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "file.txt", "one\n")
            with self.assertRaises(RuntimeError) as raised:
                run_git_command(repo, ["checkout", "--detach", "deadbeefdeadbeef"], dry_run=False)
            self.assertIn("git checkout --detach deadbeefdeadbeef", str(raised.exception))

    def test_a_dry_run_prints_the_command_and_spawns_nothing(self) -> None:
        printed = io.StringIO()
        with (
            patch.object(commands, "run_git", side_effect=AssertionError("spawned")),
            redirect_stdout(printed),
        ):
            run_git_command(Path("/repo"), ["clean", "-fdx"], dry_run=True)
        self.assertIn("git clean -fdx", printed.getvalue())

    def test_benchmark_preparation_bounds_each_command_by_what_it_does(self) -> None:
        # Routing these onto the runner imposed a timeout where there had been none, so which
        # bound each command gets is asserted rather than left to the runner's default.
        calls: list[tuple[str, float]] = []

        def record(
            _root: Path, args: list[str], *, work_dir: Path | None = None, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            calls.append((args[0], float(timeout)))
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=0, stdout="", stderr=""
            )

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(commands, "run_git", side_effect=record),
        ):
            workspace.prepare_repo(
                {"url": "https://example.invalid/repo.git", "commit": "c0ffee"},
                Path(tmp) / "repo-a",
                dry_run=False,
            )

        self.assertEqual(
            dict(calls),
            {
                # network work outside a tool call: bounded, but not by the in-tool-call band
                "clone": GIT_BULK_REMOTE_TIMEOUT_SECONDS,
                "checkout": GIT_LOCAL_TIMEOUT_SECONDS,
                "reset": GIT_LOCAL_TIMEOUT_SECONDS,
                "clean": GIT_LOCAL_TIMEOUT_SECONDS,
            },
        )
        self.assertGreater(GIT_BULK_REMOTE_TIMEOUT_SECONDS, GIT_REMOTE_TIMEOUT_SECONDS)


class RunnerArgvTests(unittest.TestCase):
    """What the one runner puts on every git command line, and why each word is there."""

    def _argv(self, repo_root: Path, args: list[str], **kwargs: object) -> list[str]:
        with patch.object(git_command_module.subprocess, "run") as spawn:
            spawn.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            run_git(repo_root, args, **kwargs)  # type: ignore[arg-type]
        return list(spawn.call_args.args[0])

    def test_the_repository_is_named_to_git_itself_not_only_through_cwd(self) -> None:
        argv = self._argv(Path("/repo"), ["status", "--porcelain"])
        self.assertEqual(argv[:3], ["git", "-C", "/repo"])
        self.assertIn("safe.directory=/repo", argv)
        self.assertEqual(argv[-2:], ["status", "--porcelain"])

    def test_a_work_dir_aims_the_process_while_the_repository_stays_the_subject(self) -> None:
        argv = self._argv(Path("/repo"), ["clone", "url", "/repo"], work_dir=Path("/parent"))
        self.assertEqual(argv[:3], ["git", "-C", "/parent"])
        self.assertIn("safe.directory=/repo", argv)

    def test_long_paths_are_enabled_and_git_really_receives_the_setting(self) -> None:
        # Asserted through git rather than through the argv: `core.longpaths` is what lets a
        # checkout land a path past Windows' MAX_PATH, and the benchmark runner carried it
        # before L6 routed those commands here.
        self.assertIn("core.longpaths=true", self._argv(Path("/repo"), ["status"]))
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            listed = run_git(repo, ["config", "--list"]).stdout
        self.assertIn("core.longpaths=true", listed)


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

    def test_config_read_retains_crlf_and_exact_commit_read_rejects_refs_and_missing_objects(
        self,
    ) -> None:
        self.assertEqual(
            run_git(self.repo, ["config", "preparation.payload", "one\r\ntwo"]).returncode, 0
        )
        crlf = read_git_configuration_bytes(self.repo)
        self.assertIn(b"preparation.payload\none\r\ntwo\0", crlf)
        self.assertEqual(
            run_git(self.repo, ["config", "preparation.payload", "one\ntwo"]).returncode, 0
        )
        self.assertNotEqual(read_git_configuration_bytes(self.repo), crlf)
        for identity in ("HEAD", self.parent[:12], "0" * 40):
            with self.subTest(identity=identity), self.assertRaises(GitPreparationError):
                read_git_commit_bytes(self.repo, identity)
        self._unchanged_logical()
        self.assertEqual(run_git(self.repo, ["reset", "--hard", self.parent]).returncode, 0)
        tree = run_git(self.repo, ["rev-parse", "HEAD^{tree}"]).stdout.strip()
        raw = inspect_existing_git_preparation(
            self.repo,
            common_directory=self.common,
            logical_ref="refs/heads/main",
            commit=self.parent,
            tree=tree,
        )
        self.assertEqual(raw, read_git_commit_bytes(self.repo, self.parent))
        self.assertFalse(self.binding.private_root.exists())

    def test_parent_may_be_an_existing_private_commit_while_logical_tip_stays_old(self) -> None:
        first = self._materialize()
        self.assertEqual(run_git_preparation(first, "commit").returncode, 0)
        prepared = inspect_git_preparation(first)
        assert prepared.head is not None
        self.binding = replace(
            self.binding, parent_commit=prepared.head, private_root=self.root / "ledger-output"
        )
        second = self._materialize()
        self.assertEqual(inspect_git_preparation(second).head, prepared.head)
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
