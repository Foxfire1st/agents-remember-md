"""No-following filesystem boundaries for immutable quality-report publication."""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Collection
from pathlib import Path

from agents_remember.worktrees.modules.quality.published_manifest import (
    PublishedQualityManifest,
    is_safe_relative_report_path,
    require_real_directory_or_missing,
    require_real_file_or_missing,
)


def preflight_report_destination(
    destination: Path,
    generation_root: Path,
    *,
    generations_directory: str,
    exported_files: Collection[str],
    exported_directories: Collection[str],
) -> None:
    """Validate every path publication may traverse before moving the live pointer."""

    require_real_directory_or_missing(destination, purpose="quality report destination")
    require_real_directory_or_missing(
        destination / generations_directory,
        purpose="quality generation directory",
    )
    require_real_directory_or_missing(generation_root, purpose="quality generation")
    for directory in exported_directories:
        require_real_directory_or_missing(
            destination / directory,
            purpose="legacy report directory",
        )
    for name in exported_files:
        require_real_file_or_missing(
            destination / name,
            purpose="legacy report file",
        )


def remove_legacy_report_projection(
    destination: Path,
    *,
    exported_files: Collection[str],
    exported_directories: Collection[str],
) -> None:
    """Remove only the former verified top-level report projection."""

    for legacy_name in exported_files:
        (destination / legacy_name).unlink(missing_ok=True)
    for legacy_directory in sorted(exported_directories, reverse=True):
        try:
            (destination / legacy_directory).rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # A non-empty directory is not part of the legacy projection and is not ours to erase.
            pass


def report_tree_inventory(root: Path) -> tuple[set[str], set[str], set[str]]:
    """Inventory a report tree without following links or normalizing hidden paths."""

    files: set[str] = set()
    directories: set[str] = set()
    irregular: set[str] = set()
    for entry in root.rglob("*"):
        relative = entry.relative_to(root).as_posix()
        if entry.is_symlink():
            irregular.add(relative)
        elif entry.is_dir():
            directories.add(relative)
        elif entry.is_file():
            files.add(relative)
        else:
            irregular.add(relative)
    return files, directories, irregular


def published_report_path_from_manifest(
    destination: Path,
    manifest: PublishedQualityManifest,
    name: str,
) -> Path:
    """Resolve one report against an already accepted immutable generation snapshot."""

    if (
        not is_safe_relative_report_path(name)
        or re.fullmatch(r"[0-9a-f]{64}", manifest.generation) is None
    ):
        raise RuntimeError("published report locator is not confined")
    file_record = manifest.require_file(name)
    report = destination / ".quality-report-generations" / manifest.generation / name
    require_real_directory_or_missing(destination, purpose="quality report destination")
    relative_parent = report.parent
    while relative_parent != destination:
        require_real_directory_or_missing(relative_parent, purpose="published report directory")
        relative_parent = relative_parent.parent
    require_real_file_or_missing(report, purpose="published report file")
    try:
        with report.open("rb") as source:
            payload = source.read(file_record.size + 1)
    except OSError as error:
        raise RuntimeError(f"published Dagger report is incomplete: {name}") from error
    if (
        len(payload) != file_record.size
        or hashlib.sha256(payload).hexdigest() != file_record.sha256
    ):
        raise RuntimeError(f"published Dagger report failed generation verification: {name}")
    return report


def _prune_report_generations(
    generations: Path,
    current: str,
    *,
    protected: Collection[str] = (),
) -> None:
    candidates = [
        path for path in generations.iterdir() if re.fullmatch(r"[0-9a-f]{64}", path.name)
    ]
    for path in candidates:
        require_real_directory_or_missing(path, purpose="published quality generation")
    completed = sorted(
        candidates,
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    keep = {current, *protected, *(path.name for path in completed[:2])}
    for path in completed:
        if path.name not in keep:
            shutil.rmtree(path)
