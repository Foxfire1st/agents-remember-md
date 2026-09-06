"""Bounded private Git preparation capabilities and physical tree observations.

The caller supplies live journal authorization. This module owns no lifecycle decision,
Git argv, subprocess, retry, or publication authority.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal

PreparationAction = Literal["create", "materialize", "commit"]
PreparationState = Literal["absent", "created", "materialized", "committed"]
_PREPARATION_AUTHORITY = object()


class GitPreparationError(ValueError):
    """An exact Git read or named private checkout violates its required binding."""


def require_git_object_id(value: str) -> None:
    """Require one complete object ID, never a ref, abbreviation, option or expression."""
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
        raise GitPreparationError("Git observation requires a complete object identity")


@dataclass(frozen=True)
class PrivateGitPreparationBinding:
    logical_root: Path
    logical_ref: str
    expected_logical_commit: str
    common_directory: Path
    private_root: Path
    parent_commit: str
    admitted_tree: str
    message: str
    hook_policy: Literal["strict-code-no-verify", "ordinary"]
    operation_key: str
    generation: int
    intent_digest: str

    def validate(self) -> None:
        for path in (self.logical_root, self.common_directory, self.private_root):
            if not path.is_absolute() or path.resolve() != path:
                raise GitPreparationError("preparation paths must be absolute and canonical")
        if not self.private_root.parent.is_dir():
            raise GitPreparationError("private checkout parent must already exist")
        for other in (self.logical_root, self.common_directory):
            if self.private_root.is_relative_to(other) or other.is_relative_to(self.private_root):
                raise GitPreparationError("private checkout overlaps logical repository storage")
        self._validate_identity()

    def _validate_identity(self) -> None:
        for value in (self.expected_logical_commit, self.parent_commit, self.admitted_tree):
            require_git_object_id(value)
        if not self.logical_ref.startswith("refs/heads/"):
            raise GitPreparationError("preparation requires an exact logical branch ref")
        if not self.message.strip() or "\x00" in self.message:
            raise GitPreparationError("preparation requires a nonempty commit message")
        if self.hook_policy not in ("strict-code-no-verify", "ordinary"):
            raise GitPreparationError("unknown preparation hook policy")
        if not self.operation_key or self.generation < 1:
            raise GitPreparationError("preparation requires an exact operation generation")
        if re.fullmatch(r"[0-9a-f]{64}", self.intent_digest) is None:
            raise GitPreparationError("preparation requires an exact durable intent digest")


@dataclass(frozen=True)
class PrivateGitPreparationCapability:
    binding: PrivateGitPreparationBinding
    authorize: Callable[[PrivateGitPreparationBinding], None] = field(repr=False)
    _authority: object = field(repr=False)

    def require_authority(self) -> None:
        if self._authority is not _PREPARATION_AUTHORITY:
            raise GitPreparationError("private preparation capability was not admitted")
        self.binding.validate()
        self.authorize(self.binding)


@dataclass(frozen=True)
class PrivateGitPreparationObservation:
    state: PreparationState
    head: str | None = None
    tree: str | None = None
    parents: tuple[str, ...] = ()
    raw_commit: bytes | None = None


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _hash_file_bytes(stream: BinaryIO, size: int, update: Callable[[bytes], None]) -> None:
    remaining = size
    while remaining:
        block = stream.read(min(1024 * 1024, remaining))
        if not block:
            raise GitPreparationError("private source shortened during read")
        update(block)
        remaining -= len(block)
    if stream.read(1):
        raise GitPreparationError("private source grew during read")


def _physical_blob(directory: int, name: str, mode: str, object_id: str) -> None:
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    digest = hashlib.new("sha1" if len(object_id) == 40 else "sha256")
    if mode == "120000" and stat.S_ISLNK(before.st_mode):
        content = os.fsencode(os.readlink(name, dir_fd=directory))
        digest.update(f"blob {len(content)}\0".encode())
        digest.update(content)
    elif mode in ("100644", "100755") and stat.S_ISREG(before.st_mode):
        actual_mode = "100755" if before.st_mode & 0o111 else "100644"
        if actual_mode != mode:
            raise GitPreparationError(f"private source mode differs: {name}")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            if _stat_identity(os.fstat(stream.fileno())) != _stat_identity(before):
                raise GitPreparationError(f"private source changed before read: {name}")
            digest.update(f"blob {before.st_size}\0".encode())
            _hash_file_bytes(stream, before.st_size, digest.update)
            if _stat_identity(os.fstat(stream.fileno())) != _stat_identity(before):
                raise GitPreparationError(f"private source changed during read: {name}")
    else:
        raise GitPreparationError(f"unsupported private source mode: {name}")
    if _stat_identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _stat_identity(
        before
    ):
        raise GitPreparationError(f"private source changed after read: {name}")
    if digest.hexdigest() != object_id:
        raise GitPreparationError(f"private source bytes differ from admitted tree: {name}")


def require_physical_tree(root: Path, entries: Mapping[str, tuple[str, str]]) -> None:
    """Read no-follow bytes, modes and membership; never trust index stat caches.

    Submodules and checkout transformations that change admitted blob bytes are refused.
    Neither ignored files nor hidden index flags can serve as private candidate evidence.
    """
    remaining = set(entries)
    directories = {
        str(parent) for path in entries for parent in Path(path).parents if str(parent) != "."
    }

    def visit(directory: int, prefix: str) -> None:
        before = _stat_identity(os.fstat(directory))
        for name in os.listdir(directory):
            if not prefix and name == ".git":
                continue
            relative = f"{prefix}/{name}" if prefix else name
            value = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(value.st_mode) and relative in directories:
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
            elif relative in remaining:
                _physical_blob(directory, name, *entries[relative])
                remaining.remove(relative)
            else:
                raise GitPreparationError(f"unexpected private checkout entry: {relative}")
        if _stat_identity(os.fstat(directory)) != before:
            raise GitPreparationError("private checkout membership changed during read")

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(descriptor, "")
    finally:
        os.close(descriptor)
    if remaining:
        raise GitPreparationError("admitted private checkout sources are missing")
