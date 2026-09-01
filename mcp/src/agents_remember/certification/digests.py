"""Canonical content digests for immutable certification contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from pydantic import BaseModel


def content_digest(value: BaseModel | Mapping[str, object] | Sequence[object]) -> str:
    """Return a stable SHA-256 digest for one JSON-compatible contract value."""

    payload: object = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
