"""Read and write task documents: the JSON is the source, the markdown a render.

Every write persists the JSON (the source of truth) **and** its rendered
markdown through :mod:`agents_remember.kernel.atomic_write`, the package's one atomic
publish -- the same call the observer drift snapshot makes. Reading goes through
``model_validate_json``; the markdown is never parsed back into a document.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agents_remember.kernel.atomic_write import atomic_write_bytes, atomic_write_text
from agents_remember.models.task_intent import require_task_intent_identity

from .document import TaskDocument
from .execution_graph_titles import SprintGraphTitles
from .render import render_markdown


@dataclass(frozen=True)
class TaskDocSourceSnapshot:
    """Exact JSON/Markdown bytes accepted before a task publication is prepared."""

    json_path: Path
    json_bytes: bytes | None
    markdown_path: Path
    markdown_bytes: bytes | None

    def evidence(self) -> dict[str, object]:
        return {
            "json": _source_evidence(self.json_path, self.json_bytes),
            "markdown": _source_evidence(self.markdown_path, self.markdown_bytes),
        }


class TaskDocSourceReadError(OSError):
    """One exact task-document source side could not be read for publication CAS."""

    def __init__(self, *, side: str, name: str, error_type: str) -> None:
        self.side = side
        self.name = name
        self.error_type = error_type
        super().__init__(f"{side} task-document source is unreadable")

    def evidence(self) -> dict[str, str]:
        return {
            "side": self.side,
            "name": self.name,
            "errorType": self.error_type,
        }


def doc_stem(doc: TaskDocument) -> str:
    """``light`` and ``master`` docs are ``task.{json,md}``; a ``subTask`` keeps its slug."""
    return doc.slug if doc.kind == "subTask" else "task"


def json_path_for(task_root: Path, doc: TaskDocument) -> Path:
    return task_root / f"{doc_stem(doc)}.json"


def markdown_path_for(task_root: Path, doc: TaskDocument) -> Path:
    return task_root / f"{doc_stem(doc)}.md"


def read_task_doc(json_path: Path) -> TaskDocument:
    return TaskDocument.model_validate_json(json_path.read_text(encoding="utf-8"))


def capture_task_doc_source(json_path: Path) -> TaskDocSourceSnapshot:
    """Read one task JSON and rendered Markdown as a single accepted source pair."""

    markdown_path = json_path.with_suffix(".md")
    return TaskDocSourceSnapshot(
        json_path=json_path,
        json_bytes=_optional_source_bytes(json_path, side="json"),
        markdown_path=markdown_path,
        markdown_bytes=_optional_source_bytes(markdown_path, side="markdown"),
    )


def read_task_doc_with_source(
    json_path: Path,
) -> tuple[TaskDocument, TaskDocSourceSnapshot]:
    """Parse the exact JSON bytes retained with their paired Markdown CAS source."""

    snapshot = capture_task_doc_source(json_path)
    if snapshot.json_bytes is None:
        raise FileNotFoundError(json_path)
    return TaskDocument.model_validate_json(snapshot.json_bytes), snapshot


def missing_task_doc_source(json_path: Path) -> TaskDocSourceSnapshot:
    """Bind a create request to exact absence without reading a later competing file."""

    return TaskDocSourceSnapshot(json_path, None, json_path.with_suffix(".md"), None)


def current_task_doc_source(snapshot: TaskDocSourceSnapshot) -> TaskDocSourceSnapshot:
    """Re-read one accepted pair at its exact publication boundary."""

    return capture_task_doc_source(snapshot.json_path)


def write_task_doc(task_root: Path, doc: TaskDocument) -> tuple[Path, Path]:
    return write_task_docs(task_root, [doc])[0]


def write_task_docs(
    task_root: Path,
    docs: list[TaskDocument],
    *,
    graph_titles: SprintGraphTitles | None = None,
) -> list[tuple[Path, Path]]:
    """Write a prepared document set, restoring every prior file if publication fails.

    Each destination replacement is individually atomic. The snapshot-and-restore around
    the set supplies failure atomicity for composite leaf/master transitions: a failure on
    a later destination cannot leave earlier task-document files at the new generation.
    """
    return write_task_doc_batch([(task_root, doc) for doc in docs], graph_titles=graph_titles)


def write_task_docs_and_remove(
    task_root: Path,
    docs: list[TaskDocument],
    removals: list[Path],
    *,
    graph_titles: SprintGraphTitles | None = None,
) -> tuple[list[tuple[Path, Path]], list[Path]]:
    """Publish task documents and remove exact sibling sources as one rollback-safe batch.

    ``discard-unstarted`` must not expose a parent audit without converging the child-source
    removal, or remove the child while losing the audit. Individual replacements and deletions
    are not a filesystem transaction, so this boundary snapshots every touched path and restores
    the exact prior bytes if any write or unlink fails. The surrounding task CAS supplies
    serialization; this function supplies failure atomicity for its file set.
    """

    write_paths = {
        path
        for doc in docs
        for path in (json_path_for(task_root, doc), markdown_path_for(task_root, doc))
    }
    ordered_removals = list(dict.fromkeys(path.resolve(strict=False) for path in removals))
    overlap = write_paths.intersection(ordered_removals)
    if overlap:
        raise ValueError(
            "task-document write and removal targets overlap: "
            + ", ".join(sorted(path.as_posix() for path in overlap))
        )
    touched = [*sorted(write_paths), *ordered_removals]
    originals = {path: path.read_bytes() if path.exists() else None for path in touched}
    deleted: list[Path] = []
    try:
        written = write_task_docs(task_root, docs, graph_titles=graph_titles)
        for path in ordered_removals:
            if path.exists():
                path.unlink()
                deleted.append(path)
        return written, deleted
    except BaseException as publish_error:
        try:
            _restore_task_doc_batch(originals)
        except BaseException as rollback_error:
            raise RuntimeError(
                "task-document write/removal publication and rollback both failed: "
                f"{rollback_error}"
            ) from publish_error
        raise


def write_task_doc_batch(
    documents: list[tuple[Path, TaskDocument]],
    *,
    graph_titles: SprintGraphTitles | None = None,
) -> list[tuple[Path, Path]]:
    """Atomically publish task documents that may live under different task roots."""

    writes: list[tuple[Path, str]] = []
    paths: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for task_root, doc in documents:
        _require_publishable_task_document(doc)
        task_root.mkdir(parents=True, exist_ok=True)
        json_path = json_path_for(task_root, doc)
        markdown_path = markdown_path_for(task_root, doc)
        if json_path in seen or markdown_path in seen:
            raise ValueError(f"duplicate task document write target: {json_path}")
        seen.update({json_path, markdown_path})
        payload = doc.model_dump_json(by_alias=True, exclude_none=True, indent=2)
        writes.append((json_path, f"{payload}\n"))
        writes.append(
            (
                markdown_path,
                render_markdown(
                    doc,
                    graph_titles=graph_titles if doc.executionGraph is not None else None,
                ),
            )
        )
        paths.append((json_path, markdown_path))
    originals = {path: path.read_bytes() if path.exists() else None for path, _text in writes}
    try:
        for path, text in writes:
            atomic_write_text(path, text)
    except BaseException as publish_error:
        try:
            _restore_task_doc_batch(originals)
        except BaseException as rollback_error:
            raise RuntimeError(
                f"task-document batch publication and rollback both failed: {rollback_error}"
            ) from publish_error
        raise
    return paths


def _require_publishable_task_document(doc: TaskDocument) -> None:
    review = doc.routeReview
    if review is None:
        return
    require_task_intent_identity(
        review.taskIntent,
        owner="route-review",
        next_action="record_route_review",
    )


def _restore_task_doc_batch(originals: dict[Path, bytes | None]) -> None:
    """Restore the exact pre-batch bytes, including absence for newly created files."""
    for path, payload in originals.items():
        if payload is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_bytes(path, payload)


def _source_evidence(path: Path, payload: bytes | None) -> dict[str, object]:
    if payload is None:
        return {"name": path.name, "state": "missing"}
    return {
        "name": path.name,
        "state": "present",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _optional_source_bytes(path: Path, *, side: str) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TaskDocSourceReadError(
            side=side,
            name=path.name,
            error_type=type(exc).__name__,
        ) from exc
