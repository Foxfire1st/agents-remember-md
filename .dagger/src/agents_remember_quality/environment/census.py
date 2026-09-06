"""Bounded standalone census of only explicitly declared dependency directories.

Invoked in the candidate container by the frozen adapter; this file uses only the
standard library. It never follows directory symlinks or hashes before stat bounds.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def _canonical_bytes(value: object) -> bytes:
    """Encode canonical JSON; semantic digests exclude the file's final newline."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _relative(value: str) -> str:
    if (
        not value
        or value.startswith(("/", "\\"))
        or "\\" in value
        or (len(value) >= 2 and value[1] == ":")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("environment path must be a canonical relative path")
    return value


def _identity(path: Path):
    return _stat_identity(path.lstat())


def _stat_identity(observed):
    return (
        observed.st_mode,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
        observed.st_dev,
        observed.st_ino,
    )


def _real_scope(root: Path, relative: str) -> Path:
    current = root
    for part in _relative(relative).split("/"):
        current /= part
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError("environment directory is missing or unsafe")
    return current


def _stat_inventory(root: Path, definition: dict):
    scopes = tuple(_real_scope(root, item) for item in definition["directoryScopes"])
    entries = []
    total = 0
    pending = list(scopes)
    while pending:
        path = pending.pop()
        identity = _identity(path)
        mode, size = identity[:2]
        if stat.S_ISREG(mode):
            total += size
            if size > definition["maxFileBytes"] or total > definition["maxBytes"]:
                raise ValueError("environment content exceeds its byte bound")
        elif stat.S_ISDIR(mode):
            with os.scandir(path) as children:
                for child in children:
                    pending.append(Path(child.path))
                    if len(pending) + len(entries) > definition["maxEntries"]:
                        raise ValueError("environment exceeds its entry bound")
        elif stat.S_ISLNK(mode):
            target = path.resolve(strict=True)
            if not any(target.is_relative_to(scope) for scope in scopes):
                raise ValueError("environment symlink escapes the declared directory closure")
        else:
            raise ValueError("environment contains an unsupported filesystem type")
        entries.append((path, identity))
        if len(entries) > definition["maxEntries"]:
            raise ValueError("environment exceeds its entry bound")
    return sorted(entries, key=lambda item: item[0].relative_to(root).as_posix())


def _hash_file(path, identity):
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        if _stat_identity(os.fstat(source.fileno())) != identity:
            raise ValueError("environment changed before hashing")
        remaining = identity[1] + 1
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            hasher.update(chunk)
            remaining -= len(chunk)
        if remaining != 1 or _identity(path) != identity:
            raise ValueError("environment changed while hashing")
    return hasher.hexdigest()


def build_census(root: Path, request: dict) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("environment root must be a real directory")
    root = root.resolve(strict=True)
    definition = request["definition"]
    inventory = _stat_inventory(root, definition)
    entries = []
    for path, identity in inventory:
        mode = identity[0]
        row = {"path": path.relative_to(root).as_posix(), "mode": stat.S_IMODE(mode)}
        if stat.S_ISREG(mode):
            row.update(type="file", size=identity[1], sha256=_hash_file(path, identity))
        elif stat.S_ISLNK(mode):
            row.update(type="symlink", target=os.readlink(path))
        else:
            row.update(type="directory")
        entries.append(row)
    if _stat_inventory(root, definition) != inventory:
        raise ValueError("environment changed while hashing")
    payload = {
        "schemaVersion": "certification-environment-census/v1",
        "candidateIdentity": request["candidateIdentity"],
        "declarationDigest": digest(definition),
        "runtimeDigest": request["runtimeDigest"],
        "entries": entries,
    }
    payload["censusDigest"] = digest(payload)
    if len(_canonical_bytes(payload) + b"\n") > definition["maxManifestBytes"]:
        raise ValueError("environment census exceeds its manifest byte bound")
    return payload


def verify_census(root: Path, request: dict, expected: Path) -> dict:
    maximum = request["definition"]["maxManifestBytes"]
    if not stat.S_ISREG(expected.lstat().st_mode) or expected.stat().st_size > maximum:
        raise ValueError("original environment census is missing, unsafe, or oversized")
    with expected.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("original environment census exceeds its byte bound")
    original = json.loads(raw)
    actual = build_census(root, request)
    if actual != original:
        raise ValueError("reconstructed environment differs from its original certified census")
    return {
        "schemaVersion": "certification-environment-reconstruction/v1",
        "status": "verified",
        "censusDigest": actual["censusDigest"],
        "declarationDigest": actual["declarationDigest"],
    }


def main(arguments: list[str]) -> int:
    if len(arguments) not in (3, 4):
        raise ValueError(
            "census requires request, candidate root, output and optional original census"
        )
    request_path, root, output = map(Path, arguments[:3])
    with request_path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("environment request exceeds its byte bound")
    request = json.loads(raw)
    result = (
        verify_census(root, request, Path(arguments[3]))
        if len(arguments) == 4
        else build_census(root, request)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_canonical_bytes(result) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
