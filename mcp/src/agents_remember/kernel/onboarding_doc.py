"""Shared onboarding-document parsing and body/history change helpers.

Owns the markdown-table metadata parsing, route normalization, and the
section-aware body/history change classification used by closeout gates and
carryover planning. The "meaningful body" of an onboarding document is its
content minus the verification metadata rows and the Update History section,
so metadata stamps and history appendices never count as content updates.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from agents_remember.kernel import filesystem

ROUTE_OVERVIEW_DOC_TYPES = {"repo-overview", "route-local-overview"}
METADATA_FIELDS = {"lastUpdated", "lastVerifiedCommitHash", "lastVerifiedCommitDate"}
UPDATE_HISTORY_HEADING = "## update history"
NO_IMPACT_MARKER_PATTERN = re.compile(r"\bno (?:content|route) impact:", re.IGNORECASE)


def onboarding_metadata_row(
    text: str, field: str, value: str, *, code: bool = False
) -> tuple[str, bool]:
    rendered = f"`{value}`" if code else value
    pattern = re.compile(rf"(\|\s*{re.escape(field)}\s*\|\s*)`?[^|`]*`?(\s*\|)")
    updated, count = pattern.subn(rf"\g<1>{rendered}\g<2>", text, count=1)
    return updated, count > 0


def markdown_table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def table_metadata(path: Path) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in filesystem.read_text(path, encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = markdown_table_cells(line)
        if len(cells) < 2:
            continue
        field = cells[0].strip()
        value = cells[1].strip().strip("`")
        if not field or set(field) <= {"-"} or field.lower() == "field":
            continue
        metadata[field] = value
    return metadata


def normalize_route(route: str) -> str:
    normalized = route.strip().strip("`").replace("\\", "/").strip("/")
    if normalized in {"", ".", "<repo-root>"}:
        return "."
    return normalized


def route_contains_changed_path(route: str, changed_paths: list[str]) -> bool:
    if not changed_paths:
        return False
    normalized_route = normalize_route(route)
    if normalized_route == ".":
        return True
    prefix = f"{normalized_route}/"
    return any(path == normalized_route or path.startswith(prefix) for path in changed_paths)


def discover_route_overviews(onboarding_root: Path) -> list[tuple[str, str]]:
    """(normalized route, onboarding-root-relative path) pairs for route overviews.

    Only overview.md files whose doc_type is a route overview count; the repo
    root overview normalizes to route '.'.
    """
    overviews: list[tuple[str, str]] = []
    if not onboarding_root.is_dir():
        return overviews
    for path in sorted(onboarding_root.rglob("overview.md")):
        if not path.is_file():
            continue
        metadata = table_metadata(path)
        if metadata.get("doc_type", "").strip("`") not in ROUTE_OVERVIEW_DOC_TYPES:
            continue
        route = normalize_route(metadata.get("sourceRoute", "."))
        overviews.append((route, path.relative_to(onboarding_root).as_posix()))
    return overviews


def fence_delimiter(line: str) -> tuple[str, int] | None:
    """``(character, length)`` if this line opens or closes a fenced code block.

    A fence is three or more backticks or tildes at an indent of at most three spaces.
    Document scanners skip fenced regions: a fence is where a document quotes
    somebody else's text, so a diff hunk or a broken table shown inside one is the
    document working correctly.
    """
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return None
    for character in ("`", "~"):
        if not stripped.startswith(character * 3):
            continue
        length = len(stripped) - len(stripped.lstrip(character))
        return character, length
    return None


def unfenced_lines(lines: list[str]) -> list[tuple[int, str]]:
    """``(zero-based index, line)`` for every line outside a fenced code block.

    The opening and closing fence lines are themselves excluded. A closing fence must use
    the same character and be at least as long as the opener, which is what keeps a
    ````` ````markdown ````` block containing a ```` ``` ```` from ending three lines early --
    ``notes/installer-design`` holds exactly that nesting.
    """
    kept: list[tuple[int, str]] = []
    open_fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        delimiter = fence_delimiter(line)
        if open_fence is None:
            if delimiter is not None:
                open_fence = delimiter
                continue
            kept.append((index, line))
            continue
        character, length = open_fence
        if delimiter is not None and delimiter[0] == character and delimiter[1] >= length:
            open_fence = None
    return kept


def _is_update_history_heading(line: str) -> bool:
    return line.strip().lower() == UPDATE_HISTORY_HEADING


def _section_lines(lines: list[str]) -> Iterable[tuple[str, bool, bool]]:
    """Line, history membership, and real H2 boundary under one fence-aware scope."""
    unfenced = {index for index, _line in unfenced_lines(lines)}
    in_history = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        heading = index in unfenced and stripped.startswith("## ")
        if heading:
            in_history = _is_update_history_heading(stripped)
        yield line, in_history, heading


def active_claim_lines(lines: list[str]) -> list[str]:
    """Blank historical sections without changing live bytes or original line indexes.

    A real Update History H2 starts the excluded section; the next unfenced H2
    resumes active claims. Quoted headings inside fences never change membership.
    This is a scanning view only: callers apply repairs to the original document.
    """
    return ["" if historical else line for line, historical, _heading in _section_lines(lines)]


def meaningful_body(text: str) -> str:
    """Document content minus verification metadata rows and Update History."""
    kept: list[str] = []
    for line, in_history, _heading in _section_lines(text.splitlines()):
        if in_history:
            continue
        cells = markdown_table_cells(line) if line.strip().startswith("|") else []
        if cells and cells[0] in METADATA_FIELDS:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def meaningful_body_changed(old_text: str | None, new_text: str) -> bool:
    return old_text is None or meaningful_body(old_text) != meaningful_body(new_text)


def update_history_section(text: str) -> list[str]:
    """Stripped, non-empty historical lines, excluding real H2 boundaries."""
    return [
        line.strip()
        for line, in_history, heading in _section_lines(text.splitlines())
        if in_history and not heading and line.strip()
    ]


def new_history_lines(old_text: str | None, new_text: str) -> list[str]:
    """Update History lines present in new_text but absent from old_text."""
    old_lines = set(update_history_section(old_text)) if old_text is not None else set()
    return [line for line in update_history_section(new_text) if line not in old_lines]


def has_no_impact_marker(lines: Iterable[str]) -> bool:
    """True when any line carries the explicit reviewed-no-impact marker.

    The marker is the in-band attestation that a document was reviewed against
    the changed sources and intentionally left unchanged: an Update History
    entry containing ``No content impact:`` (file sidecars) or
    ``No route impact:`` (route overviews).
    """
    return any(NO_IMPACT_MARKER_PATTERN.search(line) for line in lines)
