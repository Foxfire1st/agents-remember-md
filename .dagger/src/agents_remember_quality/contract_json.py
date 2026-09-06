"""Strict JSON primitives shared by frozen plan and suffix transport readers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _object(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path} must be a JSON object")
    return value


def _array(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a JSON array")
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{path} must be a nonblank unpadded string")
    return value


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    return value


def _safe_relative(value: object, path: str, *, file: bool = False) -> str:
    resolved = _string(value, path)
    if resolved == "." and not file:
        return resolved
    parts = resolved.split("/")
    if (
        resolved.startswith(("/", "\\"))
        or "\\" in resolved
        or (len(parts[0]) >= 2 and parts[0][1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"{path} must be a confined repository-relative POSIX path")
    return resolved
