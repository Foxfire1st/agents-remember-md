"""Observe effective Git configuration and actual hooks for private preparation."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from agents_remember.kernel.git_command import read_git_configuration_bytes, run_git
from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256
from agents_remember.models.lifecycles.preparation import CloseoutPreparationIntent

# These are the hooks invoked by worktree creation and ordinary commit/ref updates.
# Strict code preparation skips pre-commit/commit-msg through its admitted argv;
# retaining their identities also proves that the policy itself was not replaced.
_HOOKS = (
    "post-checkout",
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "reference-transaction",
)
_IDENTITY_ENV = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
    "GIT_EXEC_PATH",
    "EMAIL",
)


class PreparationPolicyError(ValueError):
    """Actual preparation configuration or executable hook bytes cannot be proved."""


@dataclass(frozen=True)
class GitPreparationPolicy:
    git_config_sha256: str
    hooks_sha256: str

    def require_intent(self, intent: CloseoutPreparationIntent) -> None:
        if (self.git_config_sha256, self.hooks_sha256) != (
            intent.gitConfigSha256,
            intent.hooksSha256,
        ):
            raise PreparationPolicyError("Git preparation policy differs from selected intent")


def _configuration_digest(root: Path) -> str:
    raw = read_git_configuration_bytes(root)
    if raw and not raw.endswith(b"\0"):
        raise PreparationPolicyError("effective Git configuration is not NUL terminated")
    entries: list[str] = []
    for entry in raw.split(b"\0")[:-1]:
        key, _, _value = entry.partition(b"\n")
        # The singular runner adds the exact target as safe.directory. This is
        # authorization for each physical root, not a commit behavior difference.
        # All other effective keys, values and their order remain bound, including
        # worktree-local and conditional configuration and external filters.
        if key != b"safe.directory":
            entries.append(hashlib.sha256(entry).hexdigest())
    environment = {
        key: None
        if key not in os.environ
        else hashlib.sha256(os.fsencode(os.environ[key])).hexdigest()
        for key in _IDENTITY_ENV
    }
    return canonical_sha256({"entries": entries, "identityEnvironment": environment})


def _hook_observation(root: Path, name: str) -> dict[str, str | bool]:
    result = run_git(root, ["rev-parse", "--git-path", f"hooks/{name}"])
    if result.returncode != 0:
        raise PreparationPolicyError(f"cannot resolve configured Git hook: {name}")
    value = result.stdout.rstrip("\n")
    if not value or "\n" in value:
        raise PreparationPolicyError(f"ambiguous configured Git hook path: {name}")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    # A linked hook or linked ancestor can change its target independently of the
    # selected pathname. Require one canonical physical file for this proof.
    if path.resolve() != path:
        raise PreparationPolicyError(f"configured Git hook path is not canonical: {name}")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"present": False}
    except OSError as error:
        raise PreparationPolicyError(f"cannot read configured Git hook: {name}") from error
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise PreparationPolicyError(f"configured Git hook is not a regular file: {name}")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        if _file_identity(before) != _file_identity(after) or _file_identity(
            after
        ) != _file_identity(path.stat(follow_symlinks=False)):
            raise PreparationPolicyError(f"configured Git hook changed during read: {name}")
    return {"present": True, "sha256": digest, "executable": os.access(path, os.X_OK)}


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def observe_git_preparation_policy(root: Path) -> GitPreparationPolicy:
    """Read configured semantics without exposing config values or changing Git."""
    if not root.is_absolute() or root.resolve() != root:
        raise PreparationPolicyError("Git preparation policy requires a canonical root")
    configuration = _configuration_digest(root)
    hooks = {name: _hook_observation(root, name) for name in _HOOKS}
    if _configuration_digest(root) != configuration:
        raise PreparationPolicyError("effective Git configuration changed during observation")
    return GitPreparationPolicy(configuration, canonical_sha256(hooks))
