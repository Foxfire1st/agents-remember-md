"""Canonical master-row to leaf-task binding for lifecycle admission."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import SubTaskRef, TaskDocument, read_task_doc
from agents_remember.tasks.leaf_binding import (
    CanonicalLeafBindingError,
    canonical_leaf_source,
    require_canonical_leaf_binding,
    require_leaf_parent_row,
)
from agents_remember.worktrees.task_resolver import leaf_enclosure_path


class TaskLeafBindingError(ValueError):
    """The parent row and exact child source do not form one canonical leaf identity."""

    def __init__(self, detail: str, *, status: str = "task-leaf-binding-invalid") -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class LeafTaskBinding:
    coordination_root: Path
    repo_id: str
    task_name: str
    task_root: Path
    parent_path: Path
    parent: TaskDocument
    row: SubTaskRef
    leaf_json_path: Path
    leaf_markdown_path: Path
    leaf: TaskDocument | None
    task_ref: TaskDocumentRef

    @property
    def contract_path(self) -> Path:
        return leaf_enclosure_path(self.task_root, self.row.number)


def resolve_leaf_task_binding(
    coordination_root: Path,
    repo_id: str,
    task_root: Path,
    leaf_id: str,
    *,
    task_name: str | None = None,
) -> LeafTaskBinding:
    """Resolve one parent row to its exact JSON-primary child without a directory scan."""

    root = task_root.resolve(strict=False)
    parent_path, parent = _load_leaf_parent(root)
    parent_ref = _task_ref_for_path(coordination_root, repo_id, parent_path)
    try:
        row = require_leaf_parent_row(parent, leaf_id)
        source = canonical_leaf_source(parent_ref, row)
    except CanonicalLeafBindingError as exc:
        raise TaskLeafBindingError(exc.detail, status=exc.status) from exc
    repository_root = (coordination_root / "tasks" / repo_id).resolve(strict=False)
    markdown = repository_root / source.markdown_path
    leaf_json = repository_root / source.json_ref.path
    leaf = _read_leaf_source(leaf_json, markdown)
    if leaf is not None:
        try:
            require_canonical_leaf_binding(parent_ref, parent, source.json_ref, leaf)
        except CanonicalLeafBindingError as exc:
            raise TaskLeafBindingError(exc.detail, status=exc.status) from exc
    return LeafTaskBinding(
        coordination_root=coordination_root.resolve(strict=False),
        repo_id=repo_id,
        task_name=(task_name or parent.id).strip(),
        task_root=root,
        parent_path=parent_path,
        parent=parent,
        row=row,
        leaf_json_path=leaf_json,
        leaf_markdown_path=markdown,
        leaf=leaf,
        task_ref=source.json_ref,
    )


def _load_leaf_parent(root: Path) -> tuple[Path, TaskDocument]:
    parent_path = root / "task.json"
    try:
        parent = read_task_doc(parent_path)
    except (OSError, ValueError) as exc:
        raise TaskLeafBindingError(
            f"canonical parent task document is missing or unreadable: {parent_path}: {exc}"
        ) from exc
    if parent.kind != "master":
        raise TaskLeafBindingError("discard-unstarted requires a master-owned leaf row")
    return parent_path, parent


def _read_leaf_source(
    leaf_json: Path,
    markdown: Path,
) -> TaskDocument | None:
    json_mode = _source_mode(leaf_json)
    markdown_mode = _source_mode(markdown)
    if json_mode is not None and not stat.S_ISREG(json_mode):
        raise TaskLeafBindingError(f"canonical leaf JSON source is not a regular file: {leaf_json}")
    if markdown_mode is not None and not stat.S_ISREG(markdown_mode):
        raise TaskLeafBindingError(
            f"canonical leaf Markdown source is not a regular file: {markdown}"
        )
    if json_mode is not None:
        try:
            leaf = read_task_doc(leaf_json)
        except (OSError, ValueError) as exc:
            raise TaskLeafBindingError(
                f"canonical leaf task document is unreadable: {leaf_json}: {exc}"
            ) from exc
        return leaf
    if markdown_mode is not None:
        raise TaskLeafBindingError(
            "the rendered leaf Markdown is present without its JSON-primary task source"
        )
    return None


def _task_ref_for_path(
    coordination_root: Path,
    repo_id: str,
    source_path: Path,
) -> TaskDocumentRef:
    repository_root = (coordination_root / "tasks" / repo_id).resolve(strict=False)
    if not source_path.is_relative_to(repository_root):
        raise TaskLeafBindingError("canonical task source escapes the repository task root")
    return TaskDocumentRef(
        repository=repo_id,
        path=source_path.relative_to(repository_root).as_posix(),
    )


def require_current_start_task_binding(
    coordination_root: Path,
    repo_id: str,
    task_root: Path,
    leaf_id: str,
    *,
    task_name: str | None = None,
) -> None:
    """Re-prove the task identity immediately before the start locator reservation."""

    root_path = task_root.resolve(strict=False) / "task.json"
    try:
        root = read_task_doc(root_path)
    except (OSError, ValueError) as exc:
        raise TaskLeafBindingError(
            f"worktree_start task authority is missing or unreadable: {root_path}: {exc}"
        ) from exc
    if root.kind == "master":
        binding = resolve_leaf_task_binding(
            coordination_root,
            repo_id,
            task_root,
            leaf_id,
            task_name=task_name,
        )
        if binding.leaf is None:
            raise TaskLeafBindingError(
                "worktree_start requires the exact JSON-primary leaf task source"
            )
        return
    if root.id != leaf_id:
        raise TaskLeafBindingError(
            f"worktree_start task identity changed: expected {leaf_id!r}, observed {root.id!r}"
        )


def _source_mode(path: Path) -> int | None:
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TaskLeafBindingError(
            f"canonical leaf source cannot be inspected: {path}: {type(exc).__name__}"
        ) from exc
