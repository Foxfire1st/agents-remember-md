"""Deterministic census for the bounded task-intent legacy decoder."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.tasks.document import TaskDocument

LegacyIntentRecordClass = Literal[
    "route-review",
    "curator-coherence",
    "closeout-door",
    "lifecycle-operation",
]
LEGACY_INTENT_RECORD_CLASSES: tuple[LegacyIntentRecordClass, ...] = (
    "route-review",
    "curator-coherence",
    "closeout-door",
    "lifecycle-operation",
)
_MAX_CENSUS_FILE_BYTES = 16 * 1024 * 1024
_MAX_CENSUS_PROBLEMS = 10_000


class _StrictCensusModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LegacyIntentClassCount(_StrictCensusModel):
    recordClass: LegacyIntentRecordClass
    scanned: int = Field(ge=0)
    missingIntent: int = Field(ge=0)


class TaskIntentLegacyCensus(_StrictCensusModel):
    schemaVersion: Literal["task-intent-legacy-census/v1"] = "task-intent-legacy-census/v1"
    classes: tuple[LegacyIntentClassCount, ...]
    unreadable: tuple[str, ...] = Field(default=(), max_length=_MAX_CENSUS_PROBLEMS)

    @model_validator(mode="after")
    def _classes_are_exact(self) -> Self:
        observed = tuple(item.recordClass for item in self.classes)
        if observed != LEGACY_INTENT_RECORD_CLASSES:
            raise ValueError("legacy task-intent census must enumerate every record class exactly")
        return self

    @property
    def remaining(self) -> int:
        return sum(item.missingIntent for item in self.classes)


class TaskIntentLegacyCensusError(RuntimeError):
    """Decoder removal is unsafe because current legacy state is not proven empty."""


def task_intent_legacy_census(coordination_root: Path) -> TaskIntentLegacyCensus:
    """Count only current live containers; immutable historical generations are excluded."""

    root = coordination_root.resolve()
    tasks_root = root / "tasks"
    worktrees_root = root / "worktrees"
    counts: dict[LegacyIntentRecordClass, list[int]] = {
        record_class: [0, 0] for record_class in LEGACY_INTENT_RECORD_CLASSES
    }
    unreadable: list[str] = []
    _scan_route_reviews(tasks_root, counts, unreadable)
    _scan_curator_coherence(tasks_root, counts, unreadable)
    _scan_closeout_doors(tasks_root, counts, unreadable)
    _scan_lifecycle_operations(worktrees_root, counts, unreadable)
    return TaskIntentLegacyCensus(
        classes=tuple(
            LegacyIntentClassCount(
                recordClass=record_class,
                scanned=counts[record_class][0],
                missingIntent=counts[record_class][1],
            )
            for record_class in LEGACY_INTENT_RECORD_CLASSES
        ),
        unreadable=tuple(sorted(unreadable)),
    )


def require_task_intent_decoder_removal(census: TaskIntentLegacyCensus) -> None:
    """Refuse removal until every enumerated owner is readable and has zero legacy rows."""

    if census.unreadable:
        raise TaskIntentLegacyCensusError(
            "task-intent decoder removal refused: census contains unreadable or unclassified rows"
        )
    if census.remaining:
        raise TaskIntentLegacyCensusError(
            f"task-intent decoder removal refused: {census.remaining} current rows lack intent"
        )


def _scan_route_reviews(
    tasks_root: Path,
    counts: dict[LegacyIntentRecordClass, list[int]],
    unreadable: list[str],
) -> None:
    for path in _task_document_files(tasks_root, unreadable):
        payload = _json_object(path, unreadable, owner="route-review")
        if payload is None:
            continue
        if payload.get("schema") != "ar-task-document/v1":
            _problem(unreadable, "route-review", path, "unsupported-task-document")
            continue
        review = payload.get("routeReview")
        if review is None:
            continue
        counts["route-review"][0] += 1
        _count_intent(review, path, "route-review", counts, unreadable)


def _scan_curator_coherence(
    tasks_root: Path,
    counts: dict[LegacyIntentRecordClass, list[int]],
    unreadable: list[str],
) -> None:
    for authority_path in _bounded_files(
        tasks_root,
        "*-curator-coherence.json",
        unreadable,
        owner="curator-coherence",
    ):
        authority = _json_object(
            authority_path,
            unreadable,
            owner="curator-coherence-authority",
        )
        if authority is None:
            continue
        if authority.get("schemaVersion") != "ar-curator-coherence-authority/v1":
            _problem(unreadable, "curator-coherence", authority_path, "unsupported-authority")
            continue
        task_root = authority_path.parents[2].resolve()
        relative = authority.get("recordPath")
        if not isinstance(relative, str):
            _problem(unreadable, "curator-coherence", authority_path, "record-path-missing")
            continue
        record_path = (task_root / relative).resolve(strict=False)
        if Path(relative).is_absolute() or not record_path.is_relative_to(task_root):
            _problem(unreadable, "curator-coherence", authority_path, "record-path-unconfined")
            continue
        record = _json_object(record_path, unreadable, owner="curator-coherence-record")
        if record is None:
            continue
        counts["curator-coherence"][0] += 1
        _count_intent(record, record_path, "curator-coherence", counts, unreadable)


def _scan_closeout_doors(
    tasks_root: Path,
    counts: dict[LegacyIntentRecordClass, list[int]],
    unreadable: list[str],
) -> None:
    for path in _bounded_files(tasks_root, "series-contract.md", unreadable, owner="closeout-door"):
        try:
            lines = _bounded_text(path).splitlines()
        except (OSError, UnicodeError, ValueError) as exc:
            _problem(unreadable, "closeout-door", path, type(exc).__name__)
            continue
        cells = [
            line.strip().partition(":")[2].strip()
            for line in lines
            if line.startswith("  closeout_door:")
        ]
        if not cells:
            continue
        if len(cells) != 1 or not cells[0]:
            _problem(unreadable, "closeout-door", path, "door-cell-ambiguous")
            continue
        try:
            door = json.loads(cells[0])
        except json.JSONDecodeError as exc:
            _problem(unreadable, "closeout-door", path, type(exc).__name__)
            continue
        counts["closeout-door"][0] += 1
        _count_intent(door, path, "closeout-door", counts, unreadable)


def _scan_lifecycle_operations(
    worktrees_root: Path,
    counts: dict[LegacyIntentRecordClass, list[int]],
    unreadable: list[str],
) -> None:
    if not worktrees_root.is_dir():
        _problem(unreadable, "lifecycle-operation", worktrees_root, "root-missing")
        return
    paths = {
        *worktrees_root.rglob("closeout-operation.json"),
        *worktrees_root.rglob("direct-landing-operation.json"),
    }
    for path in sorted(paths):
        record = _json_object(path, unreadable, owner="lifecycle-operation")
        if record is None:
            continue
        if record.get("schemaVersion") != "3.0" or record.get("operationKind") not in {
            "closeout",
            "direct-landing",
        }:
            _problem(unreadable, "lifecycle-operation", path, "unsupported-current-record")
            continue
        counts["lifecycle-operation"][0] += 1
        _count_intent(record, path, "lifecycle-operation", counts, unreadable)


def _count_intent(
    container: object,
    path: Path,
    owner: LegacyIntentRecordClass,
    counts: dict[LegacyIntentRecordClass, list[int]],
    unreadable: list[str],
) -> None:
    if not isinstance(container, dict):
        _problem(unreadable, owner, path, "container-not-object")
        return
    value = container.get("taskIntent")
    if value is None or value == {"state": "missing-intent"}:
        counts[owner][1] += 1
        return
    try:
        TaskIntentIdentity.model_validate(value)
    except ValidationError:
        _problem(unreadable, owner, path, "task-intent-invalid")


def _bounded_files(
    root: Path,
    pattern: str,
    unreadable: list[str],
    *,
    owner: str,
) -> tuple[Path, ...]:
    if not root.is_dir():
        _problem(unreadable, owner, root, "root-missing")
        return ()
    return tuple(sorted(root.rglob(pattern)))


def _task_document_files(tasks_root: Path, unreadable: list[str]) -> tuple[Path, ...]:
    if not tasks_root.is_dir():
        _problem(unreadable, "route-review", tasks_root, "root-missing")
        return ()
    masters = tuple(sorted(tasks_root.glob("*/*/task.json")))
    owned = set(masters)
    for master_path in masters:
        owned.update(_declared_task_document_files(master_path, unreadable))
    return tuple(sorted(owned))


def _declared_task_document_files(
    master_path: Path,
    unreadable: list[str],
) -> tuple[Path, ...]:
    try:
        payload = json.loads(_bounded_text(master_path))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict) or payload.get("schema") != "ar-task-document/v1":
        return ()
    try:
        document = TaskDocument.model_validate(payload)
    except ValidationError:
        _problem(unreadable, "route-review", master_path, "task-document-invalid")
        return ()
    if document.kind != "master":
        return ()
    candidates = (
        _declared_task_document_path(master_path, item.file, unreadable)
        for item in document.subTasks
    )
    return tuple(candidate for candidate in candidates if candidate is not None)


def _declared_task_document_path(
    master_path: Path,
    file: str,
    unreadable: list[str],
) -> Path | None:
    if not file:
        return None
    source = PurePosixPath(file)
    if source.is_absolute() or source.parent != PurePosixPath() or source.suffix != ".md":
        _problem(unreadable, "route-review", master_path, "subtask-ref-invalid")
        return None
    candidate = master_path.parent / source.with_suffix(".json")
    if (
        candidate.is_symlink()
        or candidate.resolve(strict=False).parent != master_path.parent.resolve()
    ):
        _problem(unreadable, "route-review", candidate, "subtask-ref-unconfined")
        return None
    return candidate if candidate.is_file() else None


def _json_object(
    path: Path,
    unreadable: list[str],
    *,
    owner: str,
) -> dict[str, object] | None:
    try:
        payload = json.loads(_bounded_text(path))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _problem(unreadable, owner, path, type(exc).__name__)
        return None
    if not isinstance(payload, dict):
        _problem(unreadable, owner, path, "not-object")
        return None
    return payload


def _bounded_text(path: Path) -> str:
    size = path.stat().st_size
    if size > _MAX_CENSUS_FILE_BYTES:
        raise ValueError("census file exceeds bounded size")
    return path.read_text(encoding="utf-8")


def _problem(unreadable: list[str], owner: str, path: Path, problem: str) -> None:
    if len(unreadable) >= _MAX_CENSUS_PROBLEMS:
        return
    unreadable.append(f"{owner}:{path.as_posix()}:{problem}")


__all__ = [
    "LEGACY_INTENT_RECORD_CLASSES",
    "LegacyIntentClassCount",
    "TaskIntentLegacyCensus",
    "TaskIntentLegacyCensusError",
    "require_task_intent_decoder_removal",
    "task_intent_legacy_census",
]
