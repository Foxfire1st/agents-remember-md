"""Append / content-addressed owner for durable telemetry journal entries.

Closeout-generation and diagnostic-run envelopes live in separate store roots.
Each append is a CAS publication: the entry at an exact (envelope, execution,
revision) address must byte-match the previously stored entry, every read
revalidates the revision chain and each entry digest, and replay is strictly
read-only instrumentation that can never alter authority.
"""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Literal, Never, Self

from pydantic import Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CertificationContractFinding,
    FrozenContractModel,
    SemanticText,
)
from agents_remember.certification.telemetry.models import (
    TelemetryEvent,
    TelemetryExecutionKind,
)
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.atomic_write import atomic_write_bytes

_EXECUTION_DIRS: tuple[TelemetryExecutionKind, ...] = (
    "closeout-generation",
    "diagnostic-run",
)
_EXECUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class TelemetryStorePolicy(FrozenContractModel):
    """Operation-scoped capacity and the existing owner that reclaims the store."""

    scopeId: SemanticText = Field(max_length=512)
    maxEventsPerExecution: int = Field(gt=0, le=100_000)
    maxBytes: int = Field(gt=0, le=10_000_000_000)
    reclamationOwner: SemanticText = Field(max_length=512)


class TelemetryJournalEntry(FrozenContractModel):
    """One stored event bound to its own digest and its exact predecessor."""

    schemaVersion: Literal["closeout-telemetry-journal-entry/v1"] = (
        "closeout-telemetry-journal-entry/v1"
    )
    event: TelemetryEvent
    eventDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessorDigest: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _verify_event_digest(self) -> Self:
        if self.eventDigest != content_digest(self.event):
            raise ValueError("telemetry journal entry digest does not match its event")
        return self


class TelemetryReplay(FrozenContractModel):
    """Instrumentation-only replay: read-only and incapable of altering authority."""

    schemaVersion: Literal["closeout-telemetry-replay/v1"] = "closeout-telemetry-replay/v1"
    executionKind: TelemetryExecutionKind
    executionId: SemanticText = Field(max_length=512)
    entries: tuple[TelemetryJournalEntry, ...] = Field(max_length=100_000)
    instrumentationOnly: Literal[True] = True

    @property
    def eventCount(self) -> int:
        return len(self.entries)


class DurableTelemetryStore:
    """Publish and read the exact durable telemetry journal for one execution."""

    def __init__(self, root: Path, policy: TelemetryStorePolicy) -> None:
        self._root = root
        self._policy = policy

    def append(self, event: TelemetryEvent) -> Path:
        """Append the next revision and read it back under the CAS contract."""
        envelope = self._envelope_root(event.executionKind, event.executionId)
        entries = self._read_entries(envelope)
        expected_revision = len(entries) + 1
        if event.eventRevision != expected_revision:
            _raise(
                "telemetry append refused",
                "telemetry-event-revision-mismatch",
                f"events.{expected_revision}",
                f"monotonic eventRevision must be {expected_revision} for the next append",
            )
        predecessor = entries[-1].eventDigest if entries else ""
        entry = TelemetryJournalEntry(
            event=event,
            eventDigest=content_digest(event),
            predecessorDigest=predecessor,
        )
        path = self._entry_path(event.executionKind, event.executionId, event.eventRevision)
        payload = _canonical_bytes(entry)
        existing = _read_regular_file(path, missing_ok=True)
        if existing is not None:
            if existing != payload:
                _raise(
                    "telemetry append refused",
                    "telemetry-event-cas-collision",
                    path.as_posix(),
                    "an existing entry at this exact revision has different bytes",
                )
            return path
        self._require_capacity(envelope, len(payload))
        atomic_write_bytes(path, payload)
        if _read_regular_file(path, missing_ok=False) != payload:
            _raise(
                "telemetry append failed",
                "telemetry-event-readback-mismatch",
                path.as_posix(),
                "atomic append did not read back as the exact canonical bytes",
            )
        return path

    def read(
        self,
        execution_kind: TelemetryExecutionKind,
        execution_id: str,
    ) -> tuple[TelemetryJournalEntry, ...]:
        """Read the complete verified journal chain for one execution."""
        if execution_kind not in _EXECUTION_DIRS:
            _raise(
                "telemetry read refused",
                "telemetry-envelope-invalid",
                execution_kind,
                "the execution kind is not a supported telemetry envelope",
            )
        envelope = self._envelope_root(execution_kind, execution_id)
        return self._read_entries(envelope)

    def replay(
        self,
        execution_kind: TelemetryExecutionKind,
        execution_id: str,
    ) -> TelemetryReplay:
        """Instrumentation-only replay; never mutates store state."""
        entries = self.read(execution_kind, execution_id)
        return TelemetryReplay(
            executionKind=execution_kind,
            executionId=execution_id,
            entries=entries,
        )

    def _envelope_root(self, execution_kind: TelemetryExecutionKind, execution_id: str) -> Path:
        return self._root / execution_kind / _require_execution_id(execution_id)

    def _entry_path(
        self,
        execution_kind: TelemetryExecutionKind,
        execution_id: str,
        revision: int,
    ) -> Path:
        if execution_kind not in _EXECUTION_DIRS:
            _raise(
                "telemetry append refused",
                "telemetry-envelope-invalid",
                execution_kind,
                "the execution kind is not a supported telemetry envelope",
            )
        if revision < 1:
            _raise(
                "telemetry append refused",
                "telemetry-event-revision-invalid",
                f"events.{revision}",
                "event revisions are positive integers",
            )
        return self._envelope_root(execution_kind, execution_id) / f"{revision}.json"

    def _read_entries(self, envelope: Path) -> tuple[TelemetryJournalEntry, ...]:
        revisions = sorted(
            int(item.stem) for item in envelope.glob("*.json") if _DIGIT_RE.fullmatch(item.stem)
        )
        if revisions != list(range(1, len(revisions) + 1)):
            _raise(
                "telemetry read refused",
                "telemetry-event-chain-gap",
                envelope.as_posix(),
                "the durable journal is not one contiguous revision chain",
            )
        entries: list[TelemetryJournalEntry] = []
        for revision in revisions:
            path = envelope / f"{revision}.json"
            raw = _read_regular_file(path, missing_ok=False)
            assert raw is not None
            try:
                entry = TelemetryJournalEntry.model_validate(json.loads(raw))
            except (UnicodeError, json.JSONDecodeError, ValueError) as error:
                _raise(
                    "telemetry read refused",
                    "telemetry-event-invalid",
                    path.as_posix(),
                    f"stored entry failed exact schema or digest validation: {type(error).__name__}",
                )
            if _canonical_bytes(entry) != raw:
                _raise(
                    "telemetry read refused",
                    "telemetry-event-address-mismatch",
                    path.as_posix(),
                    "stored canonical bytes do not match the exact revision address",
                )
            if revision > 1 and entry.predecessorDigest != entries[-1].eventDigest:
                _raise(
                    "telemetry read refused",
                    "telemetry-event-chain-mismatch",
                    path.as_posix(),
                    "the journal entry does not cite its exact predecessor digest",
                )
            entries.append(entry)
        return tuple(entries)

    def _require_capacity(self, envelope: Path, additional_bytes: int) -> None:
        object_count = 0
        byte_count = 0
        entries = self._read_entries(envelope)
        object_count = len(entries)
        if object_count >= self._policy.maxEventsPerExecution:
            _capacity_error(self._policy)
        for entry_path in envelope.glob("*.json"):
            try:
                mode = entry_path.lstat().st_mode
                if not stat.S_ISREG(mode):
                    raise OSError("stored entry is not a regular file")
                byte_count += entry_path.stat().st_size
            except OSError:
                _raise(
                    "telemetry storage capacity check refused",
                    "telemetry-entry-invalid",
                    entry_path.as_posix(),
                    "capacity cannot be proven while a stored entry is unsafe or unreadable",
                )
        if byte_count + additional_bytes > self._policy.maxBytes:
            _capacity_error(self._policy)


_DIGIT_RE = re.compile(r"^[0-9]+$")


def _require_execution_id(execution_id: str) -> str:
    if not _EXECUTION_ID.fullmatch(execution_id):
        _raise(
            "telemetry envelope refused",
            "telemetry-execution-id-invalid",
            execution_id,
            "execution id must be a bounded path-safe identity",
        )
    return execution_id


def _canonical_bytes(entry: TelemetryJournalEntry) -> bytes:
    payload = entry.model_dump(mode="json")
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _read_regular_file(path: Path, *, missing_ok: bool) -> bytes | None:
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise OSError("entry is not a regular file")
        return path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return None
        _raise(
            "telemetry read refused",
            "telemetry-entry-missing",
            path.as_posix(),
            "the exact journal entry does not exist",
        )
    except OSError as error:
        _raise(
            "telemetry read refused",
            "telemetry-entry-unsafe",
            path.as_posix(),
            f"the exact journal entry is not a safe readable regular file: {type(error).__name__}",
        )


def _capacity_error(policy: TelemetryStorePolicy) -> None:
    _raise(
        "telemetry store capacity exceeded",
        "telemetry-store-capacity-exceeded",
        policy.scopeId,
        (
            "operation-scoped event or byte limits were reached; preserve required evidence and "
            f"invoke the declared reclamation owner {policy.reclamationOwner}"
        ),
    )


def _raise(detail: str, code: str, path: str, finding_detail: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=finding_detail)
    raise CertificationContractError(detail, (finding.model_dump(mode="json"),))


__all__ = [
    "DurableTelemetryStore",
    "TelemetryJournalEntry",
    "TelemetryReplay",
    "TelemetryStorePolicy",
]
