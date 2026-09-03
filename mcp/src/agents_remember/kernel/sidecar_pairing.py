"""Shared, side-effect-free code<->onboarding sidecar pairing + path confinement.

Extracted from :mod:`agents_remember.application.read_files` (the ``read_ar_files``
tool) so the same 1:1 sidecar resolution, governing-index walk, and repo-relative
path confinement back **both** the MCP tool and the dashboard ``serving.files``
HTTP API -- without importing the side-effecting application entry point (which
emits a ``read.packet`` event and mutates the served ledger). Every function here
is pure over ``(root, rel)`` arguments: it reads only the onboarding/route-index
files it is explicitly asked about, raises no domain events, and writes nothing.

A missing sidecar is never an error here: :func:`route_sidecar_status` returns
``"absent"`` and :func:`sidecar_body` returns ``None`` -- the caller decides how to
present "this source file has no onboarding yet".
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from agents_remember.errors import AuthorityError
from agents_remember.kernel import filesystem
from agents_remember.kernel.coordination_context_resolver import mirror_onboarding_path
from agents_remember.kernel.onboarding_doc import meaningful_body
from agents_remember.kernel.primitives.runtime_config import (
    path_is_relative_to,
)
from agents_remember.kernel.route_index import (
    ENTITY_CATALOG_NAME,
    INDEX_FILE_NAME,
    ROUTE_OVERVIEW_NAME,
    sidecar_status,
)


def confine_rel(code_root: Path, requested: str) -> str:
    """Confine a requested path to ``code_root`` and return its posix-relative form.

    Resolves the real path (following ``..`` and symlinks) before the check, so a
    traversal or symlink escape is rejected, not just a literal ``..`` token.
    """
    candidate = Path(requested)
    if candidate.is_absolute():
        raise AuthorityError("file paths must be repo-relative, not absolute")
    resolved = (code_root / candidate).resolve()
    if not path_is_relative_to(resolved, code_root):
        raise AuthorityError(f"path {requested!r} escapes the repository root")
    return resolved.relative_to(code_root.resolve()).as_posix()


def confine_non_symlink_rel(root: Path, requested: str) -> str:
    """Confine one existing POSIX-relative path without accepting symlink aliases.

    ``confine_rel`` intentionally follows in-root symlinks because code/onboarding
    pairing addresses the resolved repository file.  Artifact roots with immutable
    canonical addresses need the stricter posture: every requested component must
    already exist beneath ``root`` and none may be a symlink, even when it points
    back inside the root.
    """

    if not requested or "\\" in requested or "\x00" in requested:
        raise AuthorityError("artifact paths must be non-empty POSIX-relative paths")
    candidate = PurePosixPath(requested)
    windows_candidate = PureWindowsPath(requested)
    raw_parts = requested.split("/")
    if (
        candidate.is_absolute()
        or windows_candidate.is_absolute()
        or windows_candidate.drive
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise AuthorityError("artifact paths must be confined POSIX-relative paths")

    root.lstat()
    if root.is_symlink():
        raise AuthorityError("artifact root must not be a symlink")
    resolved_root = root.resolve(strict=True)
    cursor = root
    for part in raw_parts:
        cursor /= part
        cursor.lstat()  # Missing components remain FileNotFoundError for the caller.
        if cursor.is_symlink():
            raise AuthorityError(f"artifact path {requested!r} contains a symlink")
    resolved = cursor.resolve(strict=True)
    if not path_is_relative_to(resolved, resolved_root):
        raise AuthorityError(f"artifact path {requested!r} escapes its registered root")
    return resolved.relative_to(resolved_root).as_posix()


def route_sidecar_status(onboarding_root: Path, rel: str) -> str:
    """Map the route index's sidecar status, degrading gracefully when absent.

    Walks the governing route-index chain nearest-first; the first index whose
    scope covers the path decides. When no governing ``overview.index.json``
    exists, fall back to a direct sidecar-file probe of the mirror path so a
    repo with sidecars but no built index still resolves (the route index is the
    authority when present, never a crash when absent).
    """
    for index in _governing_indexes(onboarding_root, rel):
        status = sidecar_status(rel, index)
        if status in {"present", "absent"}:
            return status
        # out-of-scope: try the next (more general) governing index.
    # No governing index covered the path. Fall back to the mirror probe.
    return "present" if mirror_onboarding_path(onboarding_root, rel).is_file() else "absent"


def _governing_indexes(onboarding_root: Path, rel: str) -> list[dict[str, Any]]:
    """The route-index chain governing ``rel``, nearest folder first then up to root.

    Small private nearest-route prefix match (the route_index public surface is
    consumed read-only, never extended): for ``a/b/c.py`` we check
    ``a/b/overview.index.json``, ``a/overview.index.json``, then the root
    ``overview.index.json``.
    """
    indexes: list[dict[str, Any]] = []
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    routes = [parent]
    while parent:
        parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
        routes.append(parent)
    for route in routes:
        index = _load_route_index(onboarding_root, route)
        if index is not None:
            indexes.append(index)
    return indexes


def _load_route_index(onboarding_root: Path, route: str) -> dict[str, Any] | None:
    index_rel = INDEX_FILE_NAME if route == "" else f"{route}/{INDEX_FILE_NAME}"
    index_path = onboarding_root / index_rel
    if not index_path.is_file():
        return None
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def sidecar_body(onboarding_root: Path, rel: str) -> str | None:
    sidecar_path = mirror_onboarding_path(onboarding_root, rel)
    if not sidecar_path.is_file():
        return None
    try:
        return meaningful_body(filesystem.read_text(sidecar_path))
    except (UnicodeDecodeError, OSError):
        return None


def is_file_sidecar(onboarding_rel: str) -> bool:
    """True when an onboarding-relative ``.md`` path is a 1:1 source sidecar.

    Mirrors :func:`agents_remember.kernel.route_index._is_file_sidecar` on a
    posix-relative string: the route/entity overviews, the route index, and
    ``bootstrap/`` docs are NOT per-source sidecars -- they are
    overview-without-code nodes whose scope is a route, not a single file.
    """
    name = onboarding_rel.rsplit("/", 1)[-1]
    if name in {ROUTE_OVERVIEW_NAME, ENTITY_CATALOG_NAME, INDEX_FILE_NAME}:
        return False
    if onboarding_rel.startswith("bootstrap/"):
        return False
    return onboarding_rel.endswith(".md")


def source_path_from_sidecar(onboarding_rel: str) -> str:
    """The source path a 1:1 sidecar documents (strip the trailing ``.md``)."""
    return onboarding_rel[:-3]
