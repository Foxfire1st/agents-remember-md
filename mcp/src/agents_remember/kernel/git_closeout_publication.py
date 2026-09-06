"""Capability vocabulary for one journal-authorized closeout ref publication.

This authority is distinct from private preparation and integration. The caller owns
live approval, selected certificates and per-leg journal transitions; the Git runner
owns the exact physical checks and single expected-old ref command.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agents_remember.kernel.git_preparation import require_git_object_id

_CLOSEOUT_PUBLICATION_AUTHORITY = object()


class GitCloseoutPublicationError(ValueError):
    """Named publication could not prove its exact ref or physical checkout state."""

    def __init__(
        self, message: str, *, command: subprocess.CompletedProcess[str] | None = None
    ) -> None:
        super().__init__(message)
        self.command = command


@dataclass(frozen=True)
class GitCloseoutPublicationBinding:
    root: Path
    logical_ref: str
    common_directory: Path
    expected_old_commit: str
    prepared_commit: str
    prepared_tree: str
    operation_key: str
    generation: int
    intent_digest: str

    def validate(self) -> None:
        for path in (self.root, self.common_directory):
            if not path.is_absolute() or path.resolve() != path:
                raise GitCloseoutPublicationError(
                    "publication paths must be absolute and canonical"
                )
        for value in (self.expected_old_commit, self.prepared_commit, self.prepared_tree):
            require_git_object_id(value)
        if not self.logical_ref.startswith("refs/heads/"):
            raise GitCloseoutPublicationError("publication requires an exact logical branch ref")
        if not self.operation_key or self.generation < 1:
            raise GitCloseoutPublicationError("publication requires an exact operation generation")
        if re.fullmatch(r"[0-9a-f]{64}", self.intent_digest) is None:
            raise GitCloseoutPublicationError("publication requires an exact durable intent digest")

    def require_prepared_bytes(self, raw_commit: bytes) -> None:
        """Prove this actual object, tree and sole direct parent without history discovery."""
        digest = hashlib.new("sha1" if len(self.prepared_commit) == 40 else "sha256")
        digest.update(f"commit {len(raw_commit)}\0".encode())
        digest.update(raw_commit)
        if digest.hexdigest() != self.prepared_commit:
            raise GitCloseoutPublicationError(
                "prepared commit bytes do not match their object identity"
            )
        headers = raw_commit.split(b"\n\n", 1)[0].split(b"\n")
        trees = tuple(line[5:] for line in headers if line.startswith(b"tree "))
        parents = tuple(line[7:] for line in headers if line.startswith(b"parent "))
        if trees != (self.prepared_tree.encode("ascii"),):
            raise GitCloseoutPublicationError(
                "prepared commit tree differs from its publication binding"
            )
        if self.prepared_commit != self.expected_old_commit and parents != (
            self.expected_old_commit.encode("ascii"),
        ):
            raise GitCloseoutPublicationError(
                "prepared publication requires its sole expected-old parent"
            )


@dataclass(frozen=True)
class GitCloseoutPublicationCapability:
    binding: GitCloseoutPublicationBinding
    authorize: Callable[[GitCloseoutPublicationBinding], None] = field(repr=False)
    _authority: object = field(repr=False)

    def require_authority(self) -> None:
        if self._authority is not _CLOSEOUT_PUBLICATION_AUTHORITY:
            raise GitCloseoutPublicationError("closeout publication capability was not admitted")
        self.binding.validate()
        self.authorize(self.binding)


@dataclass(frozen=True)
class GitCloseoutPublicationObservation:
    state: Literal["old", "new", "existing"]
    commit: str
    tree: str


@dataclass(frozen=True)
class GitCloseoutPublicationResult:
    before: GitCloseoutPublicationObservation
    after: GitCloseoutPublicationObservation
    command: subprocess.CompletedProcess[str] | None
