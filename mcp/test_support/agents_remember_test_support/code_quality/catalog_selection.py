"""Narrow consumer-list edits without losing consumers removed from the catalog."""

from __future__ import annotations

import tomllib
from pathlib import Path


def changed_catalog_consumers(before: str, after: str) -> frozenset[Path] | None:
    """Return old/new affected consumers; None means broader catalog semantics changed.

    Only explicit consumer-list changes can be narrowed. Schema, scope, contract,
    artifact additions/removals and lifecycle policy changes retain global invalidation.
    Invalid TOML raises instead of being treated as an empty dependency declaration.
    """
    old = tomllib.loads(before)
    new = tomllib.loads(after)
    old_artifacts = old.pop("artifact", [])
    new_artifacts = new.pop("artifact", [])
    if (
        old != new
        or not isinstance(old_artifacts, list)
        or not isinstance(new_artifacts, list)
        or len(old_artifacts) != len(new_artifacts)
    ):
        return None
    affected: set[Path] = set()
    for previous, current in zip(old_artifacts, new_artifacts, strict=True):
        if not isinstance(previous, dict) or not isinstance(current, dict):
            return None
        old_consumers = previous.pop("consumers", [])
        new_consumers = current.pop("consumers", [])
        if previous != current:
            return None
        if not isinstance(old_consumers, list) or not isinstance(new_consumers, list):
            return None
        if not all(isinstance(value, str) and value for value in (*old_consumers, *new_consumers)):
            return None
        if set(old_consumers) != set(new_consumers):
            affected.update(Path(value) for value in (*old_consumers, *new_consumers))
    return frozenset(affected)
