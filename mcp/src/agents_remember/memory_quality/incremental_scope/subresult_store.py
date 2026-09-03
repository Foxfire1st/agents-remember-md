"""Bounded atomic storage for exact R07 affected-unit results."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import NoReturn

from pydantic import Field

from agents_remember.kernel.atomic_write import atomic_write_bytes

from .affected_models import AffectedUnitResult
from .errors import GateFiveClosureRefusedError, ScopeFailure
from .models import _StrictModel

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class SubresultStorePolicy(_StrictModel):
    """Operation-scoped capacity and the explicit owner that reclaims this store."""

    scopeId: str = Field(min_length=1)
    maxObjects: int = Field(gt=0, le=100_000)
    maxBytes: int = Field(gt=0, le=10_000_000_000)
    reclamationOwner: str = Field(min_length=1)


class ContentAddressedSubresultStore:
    """Publish immutable unit results by exact digest, with no newest-result lookup."""

    def __init__(self, root: Path, policy: SubresultStorePolicy) -> None:
        self._root = root
        self._policy = policy

    def publish(self, value: AffectedUnitResult) -> Path:
        try:
            value = AffectedUnitResult.model_validate(value.model_dump(mode="json", by_alias=True))
        except ValueError as error:
            _refuse(
                "subresult-invalid",
                f"subresult failed exact schema or digest validation: {type(error).__name__}",
            )
        path = self.exact_path(value.resultDigest)
        payload = _canonical_bytes(value)
        existing = _read_regular_file(path, missing_ok=True)
        if existing is not None:
            if existing != payload:
                _refuse(
                    "subresult-content-address-collision",
                    "an existing exact subresult address has different bytes",
                    node=value.unit.node.nodeId,
                )
            return path
        self._require_capacity(len(payload))
        atomic_write_bytes(path, payload)
        if _read_regular_file(path, missing_ok=False) != payload:
            _refuse(
                "subresult-readback-mismatch",
                "atomic subresult publication did not read back as exact canonical bytes",
                node=value.unit.node.nodeId,
            )
        return path

    def load(self, digest: str) -> AffectedUnitResult:
        path = self.exact_path(digest)
        raw = _read_regular_file(path, missing_ok=False)
        assert raw is not None
        try:
            value = AffectedUnitResult.model_validate_json(raw)
        except ValueError as error:
            _refuse(
                "subresult-invalid",
                f"stored subresult failed exact schema or digest validation: {type(error).__name__}",
            )
        if value.resultDigest != digest or _canonical_bytes(value) != raw:
            _refuse(
                "subresult-address-mismatch",
                "stored canonical bytes do not match the requested exact result address",
                node=value.unit.node.nodeId,
            )
        return value

    def exact_path(self, digest: str) -> Path:
        if not _DIGEST.fullmatch(digest):
            _refuse(
                "subresult-digest-invalid",
                "subresult lookup requires one exact lowercase SHA-256 digest",
            )
        return self._root / "sha256" / digest[:2] / f"{digest}.json"

    def _require_capacity(self, additional_bytes: int) -> None:
        root = self._root / "sha256"
        object_count = 0
        byte_count = 0
        if root.exists():
            for path in root.glob("*/*.json"):
                object_count += 1
                try:
                    mode = path.lstat().st_mode
                    if not stat.S_ISREG(mode):
                        raise OSError("stored object is not a regular file")
                    byte_count += path.stat().st_size
                except OSError as error:
                    _refuse(
                        "subresult-store-object-invalid",
                        f"stored object is unsafe or unreadable: {type(error).__name__}",
                    )
        if (
            object_count + 1 > self._policy.maxObjects
            or byte_count + additional_bytes > self._policy.maxBytes
        ):
            _refuse(
                "subresult-store-capacity-exceeded",
                "operation-scoped subresult capacity requires its reclamation owner",
                owner=self._policy.reclamationOwner,
            )


def _canonical_bytes(value: AffectedUnitResult) -> bytes:
    payload = value.model_dump(mode="json", by_alias=True)
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _read_regular_file(path: Path, *, missing_ok: bool) -> bytes | None:
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise OSError("object is not a regular file")
        return path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return None
        _refuse("subresult-missing", "the exact content-addressed subresult does not exist")
    except OSError as error:
        _refuse(
            "subresult-unsafe",
            f"the exact subresult is not a safe readable regular file: {type(error).__name__}",
        )


def _refuse(code: str, detail: str, **evidence: str | None) -> NoReturn:
    raise GateFiveClosureRefusedError(
        ScopeFailure(
            code=code,
            detail=detail,
            node=evidence.get("node"),
            owner=evidence.get("owner"),
        )
    )


__all__ = ["ContentAddressedSubresultStore", "SubresultStorePolicy"]
