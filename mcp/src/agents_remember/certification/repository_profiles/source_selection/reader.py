"""Read one bounded, regular, self-verifying frozen rail selection record."""

from __future__ import annotations

import stat
from pathlib import Path

from agents_remember.certification.repository_profiles.source_selection.models import (
    RailSourceSelection,
)

MAX_DECISION_BYTES = 1_000_000


def read_rail_source_selection(path: Path) -> RailSourceSelection:
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_size > MAX_DECISION_BYTES:
        raise ValueError("frozen source selection is unsafe or exceeds its byte bound")
    with path.open("rb") as stream:
        raw = stream.read(MAX_DECISION_BYTES + 1)
    if len(raw) > MAX_DECISION_BYTES:
        raise ValueError("frozen source selection exceeds its byte bound")
    return RailSourceSelection.model_validate_json(raw)
