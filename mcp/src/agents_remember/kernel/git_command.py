"""The one git command runner for this package.

Every ``git`` subprocess goes through :func:`run_git`. It used to go through six
near-identical copies of this function, and the copies had drifted: only this one
stripped the repository-selector variables below, so with ``GIT_DIR`` exported the
kernel's ``git commit`` landed in the real repository while the worktree module's
``git commit`` -- the same function with ``env=`` omitted -- landed in whatever
repository ``GIT_DIR`` named. The runner is singular now so that guard cannot be
absent from one path and present in another.

``mcp/tests/test_git_command.py`` sets the selectors against a decoy repository and
proves the real one is untouched; ``mcp/tests/conftest.py`` also strips them at
import so fixtures are safe, but that guard is deliberately *not* what makes the
production paths safe -- the decoy test re-sets the variables inside its own scope
precisely so it cannot pass on the conftest's account.

"Singular" was true of twenty-one modules and false of one until L6. The benchmark
runner kept its own argv builder and its own spawn as a sanctioned exception, which
meant the guard on this rule had to carry a matching blind spot -- an argv composed by
a helper is invisible at the spawn -- and a sanctioned exception is still a second
owner. The builder is gone; ``work_dir`` and ``core.longpaths=true`` below are the two
things this runner had to grow to take its commands. What keeps the count at one now is
``mcp/test_support/agents_remember_test_support/code_quality/single_owner.py``, which reports the *construction* of a git argv
anywhere in the package rather than only a spawn it can see the argv of.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload

from agents_remember.kernel.git_closeout_publication import (
    _CLOSEOUT_PUBLICATION_AUTHORITY,
    GitCloseoutPublicationBinding,
    GitCloseoutPublicationCapability,
    GitCloseoutPublicationError,
    GitCloseoutPublicationObservation,
    GitCloseoutPublicationResult,
)
from agents_remember.kernel.git_preparation import (
    _PREPARATION_AUTHORITY,
    GitPreparationError,
    PreparationAction,
    PrivateGitPreparationBinding,
    PrivateGitPreparationCapability,
    PrivateGitPreparationObservation,
    require_git_object_id,
    require_physical_tree,
)

GIT_REPOSITORY_SELECTOR_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)

# Timeout classes. One number cannot bound every git command, and picking one
# anyway is how a correctness fix becomes an outage: this runner was hard-coded to
# five seconds, which is a fine bound for `rev-parse` and an absurd one for
# `rebase`, `merge`, `worktree add` or `push --delete`, so consolidating onto it
# unchanged would have killed every integrate at five seconds. Both numbers name
# the slowest *legitimate* case in their band, so tripping one is always a stall
# and never healthy work.
#
# Local work gets the larger bound: a rebase or a status over a large tree can
# legitimately churn for minutes. Remote work gets the smaller one: bytes either
# move or the connection is wedged, and a wedged connection inside an MCP tool call
# has no cancellation path, so it must not be allowed to sit there for minutes.
# A third band for constant-time metadata reads -- `rev-parse`, `branch --show-current`,
# `ls-files` -- that sit on interactive paths. Measured on this repository they return in
# under a millisecond, so 30s is roughly thirty thousand times the observed cost and can
# only be reached when git is blocked on an index lock another process holds. Inheriting
# the local bound would let one wedged `rev-parse` hold an MCP tool call for five minutes,
# which is the wrong failure for a query that is either instant or stuck.
#
# A fourth band for bulk network work that is NOT inside a tool call. The benchmark runner
# clones and fetches whole third-party repositories as a foreground batch step, and the
# remote band's argument does not hold for it: 120s is sized for a wedged connection an MCP
# client cannot cancel, whereas this is a CLI step a developer can interrupt and a large
# repository legitimately takes longer than two minutes to clone. Those spawns had **no**
# timeout at all before they were routed here, so this is the first bound they have ever
# carried rather than a relaxation of an existing one.
GIT_LOCAL_TIMEOUT_SECONDS = 300
GIT_REMOTE_TIMEOUT_SECONDS = 120
GIT_METADATA_TIMEOUT_SECONDS = 30
GIT_BULK_REMOTE_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class GitCommandPlan:
    """Exact runner input and complete argv for the caller's durable command intent."""

    cwd: Path
    args: tuple[str, ...]
    argv: tuple[str, ...]


@dataclass(frozen=True)
class _GitRun:
    work_dir: Path
    input_text: str | None
    timeout: float
    environment: dict[str, str]


@dataclass(frozen=True)
class IsolatedGitState:
    """Disposable Git state with the repository object database as read-only input."""

    index_path: Path
    object_directory: Path
    alternate_object_directory: Path


def git_environment() -> dict[str, str]:
    """Return the ambient environment without Git repository selectors."""

    environment = os.environ.copy()
    for name in GIT_REPOSITORY_SELECTOR_ENV:
        environment.pop(name, None)
    return environment


def run_git(
    repo_root: Path,
    args: list[str],
    *,
    work_dir: Path | None = None,
    input_text: str | None = None,
    timeout: float = GIT_LOCAL_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``git args`` against ``repo_root``, never against an inherited selector.

    ``input_text`` feeds git's stdin (``git patch-id`` is the only caller that needs
    it). Without it stdin is ``DEVNULL``: under the stdio MCP transport the parent's
    stdin IS the JSON-RPC request pipe, and a child holding or reading it wedges the
    tool call (GitHub #49).

    ``work_dir`` separates *where git runs* from *which repository the command is about*,
    which are the same directory for every caller but one. ``git clone <url> <dest>``
    cannot run inside ``<dest>``, because ``<dest>`` is what it is about to create, and
    ``cwd=`` a directory that does not exist raises before git is ever reached. The
    benchmark runner therefore hands the destination's parent here while ``repo_root``
    stays the repository being cloned, so ``safe.directory`` still names the right tree.
    It is passed as git's own ``-C`` as well as the child's ``cwd``: ``-C`` is the form
    the benchmark runner's argv already used on the most destructive commands in the
    package (``reset --hard``, ``clean -fdx``), and reproducing them as a bare ``cwd=``
    would have silently swapped the mechanism that aims them.

    BOTH PATHS MUST BE ABSOLUTE, and every caller passes absolute paths today: ``-C`` is
    resolved against the child's cwd, which this already sets to ``work``, so a relative
    ``work`` would select ``work/work``. Not fixed with ``Path.resolve`` because that also
    collapses symlinks, which would leave ``-C`` and ``safe.directory`` naming different
    trees on a symlinked worktree.

    ``core.longpaths=true`` was likewise carried by that argv and is now carried for
    every caller. It is what lets git check out a path past Windows' MAX_PATH; git
    ignores it everywhere else, so the cost off Windows is two argv words.

    ``safe.directory`` names ``repo_root`` and not the ``*`` the benchmark runner used.
    That is narrower, not weaker: it is the exact tree every one of these commands
    operates on, and a wildcard additionally disarms the ownership check for any *other*
    repository the command happens to reach.
    """

    return _run_git(
        repo_root,
        args,
        _GitRun(
            work_dir=repo_root if work_dir is None else work_dir,
            input_text=input_text,
            timeout=timeout,
            environment=git_environment(),
        ),
    )


def run_git_with_index(
    repo_root: Path, args: list[str], index_path: Path
) -> subprocess.CompletedProcess[str]:
    """Run Git against one explicit isolated index after stripping ambient selectors."""
    environment = git_environment()
    environment["GIT_INDEX_FILE"] = index_path.as_posix()
    return _run_git(
        repo_root,
        args,
        _GitRun(
            work_dir=repo_root,
            input_text=None,
            timeout=GIT_LOCAL_TIMEOUT_SECONDS,
            environment=environment,
        ),
    )


def run_git_with_isolated_index_and_objects(
    repo_root: Path,
    args: list[str],
    *,
    state: IsolatedGitState,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Git against disposable index/object storage and a read-only real ODB."""

    state.object_directory.mkdir(parents=True, exist_ok=True)
    environment = git_environment()
    environment["GIT_INDEX_FILE"] = state.index_path.as_posix()
    environment["GIT_OBJECT_DIRECTORY"] = state.object_directory.as_posix()
    environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = state.alternate_object_directory.as_posix()
    return _run_git(
        repo_root,
        args,
        _GitRun(
            work_dir=repo_root,
            input_text=input_text,
            timeout=GIT_LOCAL_TIMEOUT_SECONDS,
            environment=environment,
        ),
    )


def _git_argv(repo_root: Path, args: list[str], work_dir: Path) -> list[str]:
    return [
        "git",
        "-C",
        work_dir.as_posix(),
        "-c",
        "core.longpaths=true",
        "-c",
        f"safe.directory={repo_root.as_posix()}",
        *args,
    ]


@overload
def _run_git(
    repo_root: Path, args: list[str], execution: _GitRun, *, raw_output: Literal[True]
) -> subprocess.CompletedProcess[bytes]: ...


@overload
def _run_git(
    repo_root: Path, args: list[str], execution: _GitRun, *, raw_output: Literal[False] = False
) -> subprocess.CompletedProcess[str]: ...


def _run_git(
    repo_root: Path, args: list[str], execution: _GitRun, *, raw_output: bool = False
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    stdin_kwargs: dict[str, object] = (
        {"input": execution.input_text}
        if execution.input_text is not None
        else {"stdin": subprocess.DEVNULL}
    )
    return subprocess.run(
        _git_argv(repo_root, args, execution.work_dir),
        cwd=execution.work_dir,
        text=not raw_output,
        encoding=None if raw_output else "utf-8",
        errors=None if raw_output else "surrogateescape",
        capture_output=True,
        env=execution.environment,
        timeout=execution.timeout,
        check=False,
        **stdin_kwargs,  # type: ignore[arg-type]
    )


def _read_git_bytes(root: Path, args: list[str]) -> bytes:
    result = _run_git(
        root,
        args,
        _GitRun(root, None, GIT_LOCAL_TIMEOUT_SECONDS, git_environment()),
        raw_output=True,
    )
    if result.returncode:
        raise GitPreparationError(f"exact Git observation failed: {result.stderr!r}")
    return result.stdout


def read_git_configuration_bytes(root: Path) -> bytes:
    """Read exact effective config bytes with this runner's normal config/environment."""
    return _read_git_bytes(root, ["config", "--null", "--list"])


def read_git_commit_bytes(root: Path, commit: str) -> bytes:
    """Read one fully named commit's original bytes, without newline normalization."""
    require_git_object_id(commit)
    return _read_git_bytes(root, ["cat-file", "commit", commit])


def read_git_blob_bytes(root: Path, blob_id: str) -> bytes:
    """Read one fully named original blob without decoding or newline normalization."""
    require_git_object_id(blob_id)
    return _read_git_bytes(root, ["cat-file", "blob", blob_id])


def read_git_tree_bytes(root: Path, tree: str) -> bytes:
    """Read recursive NUL-delimited tree rows with original pathname bytes."""
    require_git_object_id(tree)
    return _read_git_bytes(root, ["ls-tree", "-r", "-z", tree])


def _preparation_output(root: Path, args: list[str]) -> str:
    result = run_git(root, args)
    if result.returncode:
        raise GitPreparationError(f"private Git observation failed: {result.stderr.strip()}")
    return result.stdout


def _require_logical_preparation_binding(binding: PrivateGitPreparationBinding) -> None:
    root = binding.logical_root
    _preparation_output(root, ["check-ref-format", binding.logical_ref])
    facts = (
        (["rev-parse", "--show-toplevel"], root.as_posix()),
        (
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            binding.common_directory.as_posix(),
        ),
        (["symbolic-ref", "--quiet", "HEAD"], binding.logical_ref),
        (["rev-parse", "--verify", binding.logical_ref], binding.expected_logical_commit),
        (["rev-parse", "--verify", f"{binding.parent_commit}^{{commit}}"], binding.parent_commit),
        (["rev-parse", "--verify", f"{binding.admitted_tree}^{{tree}}"], binding.admitted_tree),
    )
    for args, expected in facts:
        if _preparation_output(root, args).strip() != expected:
            raise GitPreparationError("logical Git preparation binding changed")


def _preparation_tree_entries(root: Path, tree: str) -> dict[str, tuple[str, str]]:
    output = read_git_tree_bytes(root, tree)
    entries: dict[str, tuple[str, str]] = {}
    for row in output.split(b"\0"):
        if not row:
            continue
        metadata, path_bytes = row.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split(" ")
        path = os.fsdecode(path_bytes)
        if kind != "blob" or mode not in ("100644", "100755", "120000"):
            raise GitPreparationError("preparation does not admit submodules or special sources")
        if (
            path in entries
            or Path(path).is_absolute()
            or any(part in ("", ".", "..", ".git") for part in path.split("/"))
        ):
            raise GitPreparationError("preparation tree source path is not canonical")
        entries[path] = (mode, object_id)
    return entries


def _require_preparation_index(root: Path, entries: dict[str, tuple[str, str]]) -> None:
    index = Path(
        _preparation_output(
            root, ["rev-parse", "--path-format=absolute", "--git-path", "index"]
        ).strip()
    )
    git_dir = Path(_preparation_output(root, ["rev-parse", "--absolute-git-dir"]).strip())
    if index != git_dir / "index" or index.resolve() != index or not index.is_file():
        raise GitPreparationError("preparation index is not its regular worktree-owned file")
    output = _read_git_bytes(root, ["ls-files", "--stage", "-z"])
    actual: dict[str, tuple[str, str]] = {}
    for row in output.split(b"\0"):
        if not row:
            continue
        metadata, path_bytes = row.split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split(" ")
        path = os.fsdecode(path_bytes)
        if stage != "0" or path in actual:
            raise GitPreparationError("preparation index contains unmerged entries")
        actual[path] = (mode, object_id)
    if actual != entries:
        raise GitPreparationError("preparation index differs from admitted tree")
    flags = _read_git_bytes(root, ["ls-files", "-v", "-z"])
    if any(row and not row.startswith(b"H ") for row in flags.split(b"\0")):
        raise GitPreparationError("preparation index contains hidden source flags")
    require_physical_tree(root, entries)


def _require_logical_git_root(
    root: Path, common_directory: Path, logical_ref: str, commit: str
) -> None:
    for path in (root, common_directory):
        if not path.is_absolute() or path.resolve() != path:
            raise GitPreparationError("existing preparation paths must be absolute and canonical")
    require_git_object_id(commit)
    if not logical_ref.startswith("refs/heads/"):
        raise GitPreparationError("existing preparation requires an exact logical branch ref")
    _preparation_output(root, ["check-ref-format", logical_ref])
    for args, expected in (
        (["rev-parse", "--show-toplevel"], root.as_posix()),
        (["rev-parse", "--path-format=absolute", "--git-common-dir"], common_directory.as_posix()),
        (["symbolic-ref", "--quiet", "HEAD"], logical_ref),
        (["rev-parse", "--verify", logical_ref], commit),
        (["rev-parse", "--verify", "HEAD^{commit}"], commit),
    ):
        if _preparation_output(root, args).strip() != expected:
            raise GitPreparationError("existing logical preparation binding changed")


def _require_existing_preparation(
    root: Path, common_directory: Path, logical_ref: str, commit: str, tree: str
) -> None:
    _require_logical_git_root(root, common_directory, logical_ref, commit)
    require_git_object_id(tree)
    if _preparation_output(root, ["rev-parse", "--verify", "HEAD^{tree}"]).strip() != tree:
        raise GitPreparationError("existing logical preparation tree changed")
    _require_preparation_index(root, _preparation_tree_entries(root, tree))


def inspect_existing_git_preparation(
    root: Path, *, common_directory: Path, logical_ref: str, commit: str, tree: str
) -> bytes:
    """Prove one existing logical output and return original bytes without private authority.

    The caller owns live journal/config authorization around this read-only observation.
    Both censuses prove the exact original logical ref, HEAD, index and physical tree.
    """
    _require_existing_preparation(root, common_directory, logical_ref, commit, tree)
    raw = read_git_commit_bytes(root, commit)
    _require_existing_preparation(root, common_directory, logical_ref, commit, tree)
    return raw


def _named_private_registration(binding: PrivateGitPreparationBinding) -> bool:
    output = _preparation_output(binding.logical_root, ["worktree", "list", "--porcelain", "-z"])
    matching = [
        field for field in output.split("\0") if field == f"worktree {binding.private_root}"
    ]
    if len(matching) > 1:
        raise GitPreparationError("private checkout has ambiguous Git registration")
    return bool(matching)


def _require_private_repository(binding: PrivateGitPreparationBinding) -> Path:
    for args, expected in (
        (["rev-parse", "--show-toplevel"], binding.private_root.as_posix()),
        (
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            binding.common_directory.as_posix(),
        ),
    ):
        if _preparation_output(binding.private_root, args).strip() != expected:
            raise GitPreparationError("private checkout repository identity changed")
    symbolic = run_git(binding.private_root, ["symbolic-ref", "--quiet", "HEAD"])
    if symbolic.returncode != 1 or symbolic.stdout:
        raise GitPreparationError("private checkout must retain a detached HEAD")
    git_dir = Path(
        _preparation_output(binding.private_root, ["rev-parse", "--absolute-git-dir"]).strip()
    )
    if git_dir.resolve() != git_dir or not git_dir.is_relative_to(
        binding.common_directory / "worktrees"
    ):
        raise GitPreparationError("private Git administration is outside linked-worktree storage")
    backlink = git_dir / "gitdir"
    if (
        backlink.is_symlink()
        or backlink.read_bytes() != os.fsencode(binding.private_root / ".git") + b"\n"
    ):
        raise GitPreparationError("private linked-worktree backlink changed")
    if (git_dir / "HEAD").is_symlink():
        raise GitPreparationError("private HEAD must not redirect to another output")
    return git_dir


def _observe_private_preparation(
    binding: PrivateGitPreparationBinding,
) -> PrivateGitPreparationObservation:
    _require_logical_preparation_binding(binding)
    entries = _preparation_tree_entries(binding.logical_root, binding.admitted_tree)
    registered = _named_private_registration(binding)
    if not binding.private_root.exists():
        if registered:
            raise GitPreparationError(
                "named private checkout registration exists without its directory"
            )
        return PrivateGitPreparationObservation("absent")
    if (
        not registered
        or not (binding.private_root / ".git").is_file()
        or (binding.private_root / ".git").is_symlink()
    ):
        raise GitPreparationError("private checkout lacks its exact linked-worktree registration")
    git_dir = _require_private_repository(binding)
    head = _preparation_output(binding.private_root, ["rev-parse", "--verify", "HEAD"]).strip()
    index = Path(
        _preparation_output(
            binding.private_root, ["rev-parse", "--path-format=absolute", "--git-path", "index"]
        ).strip()
    )
    if index != git_dir / "index":
        raise GitPreparationError("private index redirects outside its named worktree")
    if not index.exists():
        if head != binding.parent_commit or set(binding.private_root.iterdir()) != {
            binding.private_root / ".git"
        }:
            raise GitPreparationError("unmaterialized private checkout contains unexpected state")
        return PrivateGitPreparationObservation("created", head=head)
    if index.is_symlink() or not index.is_file():
        raise GitPreparationError("private index is not a regular file")
    _require_preparation_index(binding.private_root, entries)
    if head == binding.parent_commit:
        return PrivateGitPreparationObservation(
            "materialized", head=head, tree=binding.admitted_tree
        )
    return _observe_private_commit(binding, head)


def _observe_private_commit(
    binding: PrivateGitPreparationBinding, head: str
) -> PrivateGitPreparationObservation:
    tree = _preparation_output(
        binding.private_root, ["rev-parse", "--verify", "HEAD^{tree}"]
    ).strip()
    lineage = (
        _preparation_output(binding.private_root, ["rev-list", "--parents", "-n", "1", "HEAD"])
        .strip()
        .split()
    )
    parents = tuple(lineage[1:])
    if tree != binding.admitted_tree or parents != (binding.parent_commit,):
        raise GitPreparationError("private commit differs from the admitted tree or sole parent")
    raw = read_git_commit_bytes(binding.private_root, head)
    return PrivateGitPreparationObservation("committed", head, tree, parents, raw)


def admit_private_git_preparation(
    binding: PrivateGitPreparationBinding,
    *,
    authorize: Callable[[PrivateGitPreparationBinding], None],
) -> PrivateGitPreparationCapability:
    """Bind one journal-authorized detached output; this grants no logical publication.

    The upper owner must hold its actual operation lease, admit the exact durable intent
    and check hook/config equivalence in ``authorize``. Recovery supplies that same intent;
    this factory neither discovers another output nor claims an incomplete command worked.
    """
    capability = PrivateGitPreparationCapability(binding, authorize, _PREPARATION_AUTHORITY)
    inspect_git_preparation(capability)
    return capability


def inspect_git_preparation(
    capability: PrivateGitPreparationCapability,
) -> PrivateGitPreparationObservation:
    """Observe only the named output, preserving original raw Git commit bytes."""
    capability.require_authority()
    try:
        observed = _observe_private_preparation(capability.binding)
        _require_logical_preparation_binding(capability.binding)
        return observed
    finally:
        capability.require_authority()


def preparation_command(
    binding: PrivateGitPreparationBinding, action: PreparationAction
) -> GitCommandPlan:
    """Describe the sole allowed command for a journal to persist before execution."""
    binding.validate()
    if action == "create":
        root = binding.logical_root
        args = [
            "worktree",
            "add",
            "--detach",
            "--no-checkout",
            binding.private_root.as_posix(),
            binding.parent_commit,
        ]
    elif action == "materialize":
        root = binding.private_root
        args = ["read-tree", "--reset", "-u", binding.admitted_tree]
    elif action == "commit":
        root = binding.private_root
        args = ["commit"]
        if binding.hook_policy == "strict-code-no-verify":
            args.append("--no-verify")
        args.extend(["-m", binding.message])
    else:
        raise GitPreparationError("unknown private preparation action")
    return GitCommandPlan(root, tuple(args), tuple(_git_argv(root, args, root)))


def run_git_preparation(
    capability: PrivateGitPreparationCapability,
    action: PreparationAction,
) -> subprocess.CompletedProcess[str]:
    """Perform one admitted command once, with exact before/after readback.

    Timeouts propagate; callers must retain the durable intent and inspect its one named
    output. An existing committed output is observation-only and can never be recommitted.
    Normal identity, signing, prepare-commit-msg and post-commit semantics remain Git-owned.
    """
    before = inspect_git_preparation(capability)
    binding = capability.binding
    required = {"create": "absent", "materialize": "created", "commit": "materialized"}
    if action not in required or before.state != required[action]:
        raise GitPreparationError(
            f"private preparation action {action!r} is not admitted from {before.state}"
        )
    command = preparation_command(binding, action)
    capability.require_authority()
    result = run_git(command.cwd, list(command.args))
    after = inspect_git_preparation(capability)
    expected = {"create": "created", "materialize": "materialized", "commit": "committed"}
    if result.returncode == 0 and after.state != expected[action]:
        raise GitPreparationError("successful Git command did not produce its exact private state")
    return result


def _observe_closeout_publication(
    binding: GitCloseoutPublicationBinding,
) -> GitCloseoutPublicationObservation:
    current = _preparation_output(
        binding.root, ["rev-parse", "--verify", binding.logical_ref]
    ).strip()
    if current not in (binding.expected_old_commit, binding.prepared_commit):
        raise GitCloseoutPublicationError(
            "named closeout ref is outside its original old/new publication states"
        )
    _require_logical_git_root(binding.root, binding.common_directory, binding.logical_ref, current)
    binding.require_prepared_bytes(read_git_commit_bytes(binding.root, binding.prepared_commit))
    _require_preparation_index(
        binding.root, _preparation_tree_entries(binding.root, binding.prepared_tree)
    )
    _require_logical_git_root(binding.root, binding.common_directory, binding.logical_ref, current)
    state = (
        "existing"
        if binding.expected_old_commit == binding.prepared_commit
        else "new"
        if current == binding.prepared_commit
        else "old"
    )
    return GitCloseoutPublicationObservation(state, current, binding.prepared_tree)


def admit_git_closeout_publication(
    binding: GitCloseoutPublicationBinding,
    *,
    authorize: Callable[[GitCloseoutPublicationBinding], None],
) -> GitCloseoutPublicationCapability:
    """Admit one actual closeout journal/approval owner, never integration authority."""
    capability = GitCloseoutPublicationCapability(
        binding, authorize, _CLOSEOUT_PUBLICATION_AUTHORITY
    )
    inspect_git_closeout_publication(capability)
    return capability


def inspect_git_closeout_publication(
    capability: GitCloseoutPublicationCapability,
) -> GitCloseoutPublicationObservation:
    """Reprove only the named old/new ref and its already-materialized candidate."""
    capability.require_authority()
    try:
        return _observe_closeout_publication(capability.binding)
    except GitPreparationError as error:
        raise GitCloseoutPublicationError(str(error)) from error
    finally:
        capability.require_authority()


def closeout_publication_command(binding: GitCloseoutPublicationBinding) -> GitCommandPlan:
    """Describe the one expected-old CAS for the caller's durable publication intent."""
    binding.validate()
    if binding.expected_old_commit == binding.prepared_commit:
        raise GitCloseoutPublicationError("existing closeout output is observation-only")
    args = ["update-ref", binding.logical_ref, binding.prepared_commit, binding.expected_old_commit]
    return GitCommandPlan(
        binding.root, tuple(args), tuple(_git_argv(binding.root, args, binding.root))
    )


def publish_git_closeout_ref(
    capability: GitCloseoutPublicationCapability,
) -> GitCloseoutPublicationResult:
    """CAS one ref once; this neither materializes files nor promises cross-repo atomicity.

    A timeout remains the upper journal owner's unknown command. Already-new and
    no-op existing states return read-only evidence with no command. A failed after
    proof retains an available actual command result on the typed refusal.
    """
    before = inspect_git_closeout_publication(capability)
    if before.state != "old":
        return GitCloseoutPublicationResult(before, before, None)
    command = closeout_publication_command(capability.binding)
    capability.require_authority()
    result = run_git(command.cwd, list(command.args))
    try:
        after = inspect_git_closeout_publication(capability)
        if result.returncode == 0 and after.state != "new":
            raise GitCloseoutPublicationError(
                "successful closeout CAS did not retain the prepared ref"
            )
    except GitCloseoutPublicationError as error:
        raise GitCloseoutPublicationError(str(error), command=result) from error
    return GitCloseoutPublicationResult(before, after, result)
