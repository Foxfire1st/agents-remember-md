"""Manifest and POSIX identity values for a citation source snapshot."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 9
MAX_READINESS_BYTES = 16 * 1024
MAX_SOURCE_FILES = 100_000
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 4 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


class SourceIndexError(ValueError):
    """The requested source snapshot cannot be indexed safely."""


def candidate_tree(value: object) -> str | None:
    """A null filesystem selection or one exact canonical Git tree identity."""
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise SourceIndexError("citation source candidate tree must be 40 lowercase hex digits")
    return value


class SourceIndexManifestError(ValueError):
    """A source-index manifest is obsolete or malformed."""


def canonical_hash(value: object) -> bool:
    """Whether ``value`` is one canonical SHA-256 spelling."""
    return isinstance(value, str) and HASH_PATTERN.fullmatch(value) is not None


def _bounded_integer(value: object, *, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SourceIndexManifestError(f"citation source-index readiness has invalid {name}")
    return value


def _canonical_root(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise SourceIndexManifestError(f"citation source-index readiness has invalid {name}")
    root = Path(value)
    if (
        not root.is_absolute()
        or root.as_posix() != value
        or any(part == ".." for part in root.parts)
    ):
        raise SourceIndexManifestError(f"citation source-index readiness has invalid {name}")
    return value


@dataclass(frozen=True)
class ReadyGeneration:
    """Constant-size authority that makes one database generation queryable.

    The full per-file manifest remains the default dirty-safety input. Frozen readers use
    only this bounded marker and matching database metadata, so they never deserialize a
    tree-sized payload. The random generation id prevents an old marker from blessing a
    subsequently replaced database that happens to describe the same roots.
    """

    generation_id: str
    snapshot_id: str
    code_root: str
    memory_root: str
    files_indexed: int
    source_bytes: int
    database_bytes: int
    candidate_tree: str | None = None

    @classmethod
    def from_json(cls, path: Path) -> ReadyGeneration:
        with path.open("rb") as handle:
            raw = handle.read(MAX_READINESS_BYTES + 1)
        if len(raw) > MAX_READINESS_BYTES:
            raise SourceIndexManifestError("citation source-index readiness marker is oversized")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "schemaVersion",
            "state",
            "generationId",
            "snapshotId",
            "codeRoot",
            "memoryRoot",
            "filesIndexed",
            "sourceBytes",
            "databaseBytes",
            "candidateTree",
        }:
            raise SourceIndexManifestError("citation source-index readiness marker is malformed")
        if type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != SCHEMA_VERSION:
            raise SourceIndexManifestError("citation source index schema is obsolete")
        if payload["state"] != "ready":
            raise SourceIndexManifestError("citation source-index generation is not ready")
        generation_id = payload["generationId"]
        snapshot_id = payload["snapshotId"]
        if not canonical_hash(generation_id) or not canonical_hash(snapshot_id):
            raise SourceIndexManifestError(
                "citation source-index readiness has a noncanonical generation identity"
            )
        return cls(
            generation_id=generation_id,
            snapshot_id=snapshot_id,
            code_root=_canonical_root(payload["codeRoot"], "code root"),
            memory_root=_canonical_root(payload["memoryRoot"], "memory root"),
            files_indexed=_bounded_integer(
                payload["filesIndexed"], minimum=0, maximum=MAX_SOURCE_FILES, name="file count"
            ),
            source_bytes=_bounded_integer(
                payload["sourceBytes"], minimum=0, maximum=MAX_SOURCE_BYTES, name="source bytes"
            ),
            database_bytes=_bounded_integer(
                payload["databaseBytes"],
                minimum=1,
                maximum=MAX_DATABASE_BYTES,
                name="database bytes",
            ),
            candidate_tree=candidate_tree(payload["candidateTree"]),
        )

    def to_json(self) -> str:
        payload = (
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "state": "ready",
                    "generationId": self.generation_id,
                    "snapshotId": self.snapshot_id,
                    "codeRoot": self.code_root,
                    "memoryRoot": self.memory_root,
                    "filesIndexed": self.files_indexed,
                    "sourceBytes": self.source_bytes,
                    "databaseBytes": self.database_bytes,
                    "candidateTree": self.candidate_tree,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        if len(payload.encode()) > MAX_READINESS_BYTES:
            raise SourceIndexManifestError("citation source-index readiness marker is oversized")
        return payload


@dataclass(frozen=True)
class Identity:
    """POSIX metadata used as the cheap trigger for authoritative content hashing."""

    path: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def read(cls, path: Path, relative: str) -> Identity:
        stat = path.stat()
        return cls(
            path=relative,
            device=stat.st_dev,
            inode=stat.st_ino,
            mode=stat.st_mode,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            ctime_ns=stat.st_ctime_ns,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> Identity:
        return cls(
            path=str(payload["path"]),
            device=int(str(payload["device"])),
            inode=int(str(payload["inode"])),
            mode=int(str(payload["mode"])),
            size=int(str(payload["size"])),
            mtime_ns=int(str(payload["mtimeNs"])),
            ctime_ns=int(str(payload["ctimeNs"])),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "size": self.size,
            "mtimeNs": self.mtime_ns,
            "ctimeNs": self.ctime_ns,
        }


def check_source_bounds(identities: Sequence[Identity]) -> None:
    """Enforce the one source-read budget from metadata before reading file bodies."""
    if len(identities) > MAX_SOURCE_FILES:
        raise SourceIndexError(
            f"citation source-index input has {len(identities)} files, above its "
            f"{MAX_SOURCE_FILES}-file cap"
        )
    oversized = [one.path for one in identities if one.size > MAX_SOURCE_FILE_BYTES]
    if oversized:
        raise SourceIndexError(
            f"citation source-index input exceeds the {MAX_SOURCE_FILE_BYTES}-byte per-file "
            f"cap: {oversized[:3]}"
        )
    total = sum(one.size for one in identities)
    if total > MAX_SOURCE_BYTES:
        raise SourceIndexError(
            f"citation source-index input is {total} bytes, above its {MAX_SOURCE_BYTES}-byte cap"
        )


@dataclass(frozen=True)
class SourceFile:
    """One indexed file's path, current metadata, and authoritative content digest."""

    absolute: Path
    identity: Identity
    content_sha256: str = ""

    @classmethod
    def from_dict(cls, root: Path, payload: dict[str, object]) -> SourceFile:
        identity = Identity.from_dict(payload)
        return cls(
            absolute=root / identity.path,
            identity=identity,
            content_sha256=str(payload["contentSha256"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {**self.identity.to_dict(), "contentSha256": self.content_sha256}


@dataclass(frozen=True)
class TreeState:
    """Every relevant directory entry and readable-file candidate in deterministic order."""

    directories: tuple[Identity, ...]
    files: tuple[SourceFile, ...]


@dataclass(frozen=True)
class Manifest:
    """The atomic metadata companion for one immutable database generation."""

    code_root: str
    memory_root: str
    snapshot_id: str
    source_bytes: int
    directories: tuple[Identity, ...]
    files: tuple[SourceFile, ...]
    candidate_tree: str | None = None

    @classmethod
    def from_json(cls, path: Path) -> Manifest:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload["schemaVersion"]) != SCHEMA_VERSION:
            raise SourceIndexManifestError("citation source index schema is obsolete")
        root = Path(str(payload["codeRoot"]))
        return cls(
            code_root=root.as_posix(),
            memory_root=str(payload["memoryRoot"]),
            snapshot_id=str(payload["snapshotId"]),
            source_bytes=int(payload["sourceBytes"]),
            directories=tuple(Identity.from_dict(one) for one in payload["directories"]),
            files=tuple(SourceFile.from_dict(root, one) for one in payload["files"]),
            candidate_tree=candidate_tree(payload["candidateTree"]),
        )

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "codeRoot": self.code_root,
                    "memoryRoot": self.memory_root,
                    "snapshotId": self.snapshot_id,
                    "sourceBytes": self.source_bytes,
                    "directories": [one.to_dict() for one in self.directories],
                    "files": [one.to_dict() for one in self.files],
                    "candidateTree": self.candidate_tree,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )


@dataclass(frozen=True)
class Validation:
    """Whether a generation is current, content-stale, or metadata-equivalent."""

    state: TreeState
    stale: bool
    metadata_changed: bool
