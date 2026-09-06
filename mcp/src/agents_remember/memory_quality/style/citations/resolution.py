"""Resolve citation sources against code and memory roots.

Sources are repo-relative ``path:start-end`` tokens. Resolution checks the code root first,
then the memory root (the onboarding root's parent). A path existing in both therefore
means code; the format cannot explicitly select the colliding memory path.

A source link to a sidecar resolves to the source file it documents. Memory documents are
excluded from the tree-wide symbol index because a card contains its own anchor text and
would become a false second location. Memory-target citations still receive bounds and
containment checks, but moves within memory are not searched.

Dependency paths in neither tree are counted as unresolved rather than gated on the local
installation environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from agents_remember.errors import CitationCacheError
from agents_remember.memory_quality.style.citations.candidate.git_source import GitSourceCandidate
from agents_remember.memory_quality.style.citations.source_index_cache import (
    ManagedCacheAuthority,
)
from agents_remember.memory_quality.style.citations.source_index_state import candidate_tree


@dataclass(frozen=True)
class Trees:
    """The two roots a source may name."""

    code_root: Path
    memory_root: Path
    cache_authority: ManagedCacheAuthority | None = None
    candidate_tree: str | None = None

    def __post_init__(self) -> None:
        candidate_tree(self.candidate_tree)

    @cached_property
    def source_candidate(self) -> GitSourceCandidate | None:
        if self.candidate_tree is None:
            return None
        return GitSourceCandidate(self.code_root.resolve(), self.candidate_tree)

    def resolve(self, path: str) -> Path | None:
        if self.source_candidate is not None:
            candidate = self.source_candidate.resolve(path)
            if candidate is not None:
                return candidate
            roots = (self.memory_root,)
        else:
            roots = (self.code_root, self.memory_root)
        for root in roots:
            candidate = root / path
            if self.candidate_tree is not None and not candidate.resolve().is_relative_to(
                self.memory_root.resolve()
            ):
                continue
            if candidate.is_file():
                return candidate
        return None

    def ours(self, path: str) -> bool:
        """Whether a path that did NOT resolve still names a location in these trees.

        This is what separates the two reasons a source resolves to nothing, and they need
        opposite treatment. ``uvicorn/main.py`` is a dependency's source: correct, useful,
        and absent by construction. A source naming a real top-level directory of this
        repository and a file under it that is gone is the other one, and it is the exact
        damage a package move does to a memory tree. The test is the first segment being a real top-level entry of one of
        the roots, read from the tree rather than listed here, so a new top-level directory
        is covered the day it is created.
        """
        first = path.split("/", maxsplit=1)[0]
        return any((root / first).exists() for root in (self.code_root, self.memory_root))


def operation_trees(onboarding_root: Path, code: Path | Trees) -> Trees:
    """Bind a core operation to either standalone roots or managed application authority."""
    if isinstance(code, Path):
        return Trees(code_root=code, memory_root=onboarding_root.parent)
    expected_memory = onboarding_root.parent.resolve()
    if code.memory_root.resolve() != expected_memory:
        raise CitationCacheError(
            "citation operation Trees belongs to a different onboarding memory root: "
            f"trees={code.memory_root.resolve()}, onboarding={expected_memory}"
        )
    if code.cache_authority is not None:
        code.cache_authority.validate_roots(code.code_root, code.memory_root)
    return code
