"""Byte-preserving edits shared by citation migration and steady-state repair."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Site:
    """The exact span of one claim's source list, in one line of one document."""

    line: int
    start: int
    end: int


@dataclass
class Documents:
    """The memory documents one run reads, split so that rejoining is lossless."""

    cache: dict[Path, list[str]] = field(default_factory=dict)

    def lines(self, path: Path) -> list[str]:
        if path not in self.cache:
            self.cache[path] = path.read_bytes().decode("utf-8").split("\n")
        return self.cache[path]


def spliced(line: str, site: Site, text: str) -> str:
    """``line`` with the source list replaced and the cell's own padding untouched."""
    body = line[site.start : site.end]
    start = site.start + len(body) - len(body.lstrip())
    return line[:start] + text + line[max(start, site.end - len(body) + len(body.rstrip())) :]


def rewritten(lines: list[str], edits: list[tuple[Site, str]]) -> list[str]:
    """``lines`` with each site's source list replaced, right to left so offsets hold."""
    updated = list(lines)
    for site, text in sorted(edits, key=lambda one: (one[0].line, one[0].start), reverse=True):
        updated[site.line - 1] = spliced(updated[site.line - 1], site, text)
    return updated
