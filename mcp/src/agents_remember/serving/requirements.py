"""Read-only task-local requirement packet API.

The caller supplies a repository, master, and canonical task-document reference.
Those three values select exactly one ``tasks/<repo>/<master>/requirements/`` root;
the client never supplies a filesystem root.  Requirement addresses remain stable
``requirements/<path>.md`` values while every filesystem component is confined and
must be a real non-symlink node.
"""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from agents_remember.errors import AuthorityError
from agents_remember.kernel.authority import require_repo
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig, path_is_relative_to
from agents_remember.kernel.sidecar_pairing import confine_non_symlink_rel
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.serving.response_contract import (
    SCOPED_READ_RESPONSES,
    RequirementContents,
    RequirementsListing,
)
from agents_remember.tasks.document_refs import TaskDocumentRefError, TaskDocumentTopology

_MAX_INVENTORY_DEPTH = 8
_MAX_INVENTORY_FILES = 2_000


class RequirementContextError(ValueError):
    """The repository/master/document selector is not one canonical context."""


def _is_single_segment(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in {".", ".."}


def _selected_root(
    config: McpRuntimeConfig,
    repo_id: str,
    master: str,
    document: str,
) -> Path:
    """Resolve one canonical task document and derive its adjacent requirements root."""

    if not _is_single_segment(master):
        raise RequirementContextError("requirements need a single-segment master")
    try:
        ref = TaskDocumentRef(repository=repo_id, path=document)
    except ValueError as exc:
        raise RequirementContextError(f"invalid task-document reference: {exc}") from exc
    if ref.path != document:
        raise RequirementContextError("task-document reference must use its canonical POSIX form")
    try:
        resolved = TaskDocumentTopology(config.coordination_root).resolve(ref)
    except TaskDocumentRefError as exc:
        raise RequirementContextError(str(exc)) from exc

    expected_master = config.coordination_root / "tasks" / repo_id / master
    expected_resolved = expected_master.resolve(strict=False)
    if resolved.path.parent != expected_resolved:
        raise RequirementContextError(
            "task document does not belong to the selected repository and master"
        )
    return expected_master / "requirements"


def _registered_root(root: Path) -> Path | None:
    """Return the exact requirements directory, or ``None`` when it is absent."""

    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise AuthorityError("the registered requirements root must be a real directory")
    resolved = root.resolve(strict=True)
    if not path_is_relative_to(resolved, root.parent.resolve(strict=True)):
        raise AuthorityError("the registered requirements root escapes its task master")
    return resolved


def _packet(raw: bytes, rel: str) -> dict[str, Any]:
    raw.decode("utf-8")
    return {
        "name": rel.rsplit("/", 1)[-1],
        "path": rel,
        "address": f"requirements/{rel}",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _walk_packets(root: Path, base: Path, depth: int, out: list[dict[str, Any]]) -> None:
    if depth > _MAX_INVENTORY_DEPTH:
        raise AuthorityError("requirements inventory exceeds the supported directory depth")
    for child in sorted(base.iterdir(), key=lambda entry: entry.name.lower()):
        mode = child.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise AuthorityError("requirements inventory contains a symlink")
        if stat.S_ISDIR(mode):
            _walk_packets(root, child, depth + 1, out)
            continue
        if not stat.S_ISREG(mode) or child.suffix != ".md":
            continue
        rel = child.relative_to(root).as_posix()
        out.append(_packet(child.read_bytes(), rel))
        if len(out) > _MAX_INVENTORY_FILES:
            raise AuthorityError("requirements inventory exceeds the supported file count")


def list_requirements(
    config: McpRuntimeConfig, repo_id: str, master: str, document: str
) -> dict[str, Any]:
    root = _registered_root(_selected_root(config, repo_id, master, document))
    requirements: list[dict[str, Any]] = []
    if root is not None:
        _walk_packets(root, root, 1, requirements)
    return {
        "repo": repo_id,
        "master": master,
        "document": document,
        "registered": root is not None,
        "requirements": requirements,
    }


def read_requirement(
    config: McpRuntimeConfig,
    repo_id: str,
    master: str,
    document: str,
    rel: str,
) -> dict[str, Any]:
    root = _registered_root(_selected_root(config, repo_id, master, document))
    if root is None:
        raise FileNotFoundError(f"requirements/{rel}")
    confined = confine_non_symlink_rel(root, rel)
    if Path(confined).suffix != ".md":
        raise AuthorityError("requirements reads support Markdown packet files only")
    source = root / confined
    if not source.is_file():
        raise FileNotFoundError(f"requirements/{rel}")
    raw = source.read_bytes()
    metadata = _packet(raw, confined)
    return {
        "repo": repo_id,
        "master": master,
        "document": document,
        **metadata,
        "content": raw.decode("utf-8"),
    }


def _requirements_json(
    config: McpRuntimeConfig,
    repo_id: str,
    produce: Callable[[], dict[str, Any]],
) -> Response:
    try:
        require_repo(config, repo_id)
    except AuthorityError:
        return JSONResponse({"status": "unknown-repo", "repo": repo_id}, status_code=404)
    try:
        return JSONResponse(produce(), status_code=200)
    except RequirementContextError as exc:
        return JSONResponse({"status": "bad-context", "detail": str(exc)}, status_code=400)
    except FileNotFoundError as exc:
        return JSONResponse({"status": "not-found", "path": str(exc)}, status_code=404)
    except (AuthorityError, UnicodeDecodeError, ValueError, OSError) as exc:
        return JSONResponse({"status": "bad-path", "detail": str(exc)}, status_code=400)


def register_requirements_routes(app: FastAPI, config: McpRuntimeConfig) -> None:
    """Register the task-context-selected GET-only requirements surface."""

    @app.get(
        "/api/requirements/list",
        response_model=RequirementsListing,
        responses=SCOPED_READ_RESPONSES,
    )
    def api_requirements_list(repo: str, master: str = "", document: str = "") -> Response:
        return _requirements_json(
            config,
            repo,
            lambda: list_requirements(config, repo, master, document),
        )

    @app.get(
        "/api/requirements/read",
        response_model=RequirementContents,
        responses=SCOPED_READ_RESPONSES,
    )
    def api_requirements_read(
        repo: str, master: str = "", document: str = "", path: str = ""
    ) -> Response:
        return _requirements_json(
            config,
            repo,
            lambda: read_requirement(config, repo, master, document, path),
        )
