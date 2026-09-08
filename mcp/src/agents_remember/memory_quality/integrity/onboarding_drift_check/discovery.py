"""Onboarding/source discovery and table-metadata parsing for drift detection."""

from __future__ import annotations

from pathlib import Path

from agents_remember.kernel.coordination_context_resolver import (
    clean_scalar,
    normalize_rel_path,
)
from agents_remember.memory_quality.integrity.onboarding_drift_check.models import (
    SIDECAR_DOC_TYPES,
    repo_root_placeholder,
)


def parse_table_metadata(path: Path) -> dict[str, str]:
    return parse_table_metadata_text(path.read_text(encoding="utf-8"))


def parse_table_metadata_text(text: str) -> dict[str, str]:
    """Canonical table metadata parsing for filesystem and exact Git tree readers."""
    metadata: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        key, value = cells[0], cells[1]
        if key in {"Field", "---", "----------------------"}:
            continue
        if value.startswith("`") and value.endswith("`"):
            value = value[1:-1]
        metadata[key] = value
    return metadata


def is_supported_sidecar_onboarding(path: Path) -> bool:
    try:
        metadata = parse_table_metadata(path)
    except UnicodeDecodeError:
        return False
    return metadata.get("doc_type") in SIDECAR_DOC_TYPES


def discover_onboarding_files(onboarding_root: Path) -> list[Path]:
    return sorted(
        path
        for path in onboarding_root.rglob("*.md")
        if path.is_file() and is_supported_sidecar_onboarding(path)
    )


def normalize_overview_route(source_route: str) -> str:
    source_route = clean_scalar(source_route).strip()
    if source_route in {"", ".", "`<repo-root>`", "<repo-root>", repo_root_placeholder()}:
        return "."
    return normalize_rel_path(source_route.strip("`"))


def rel(path: Path | str, base: Path) -> str:
    if isinstance(path, str):
        return path
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()
